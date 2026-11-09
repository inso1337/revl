"""The written federation plan, its command, and the prior-generation restore.

Issue #2718. `federation_admission` and `run_deploy` already existed, but only as
a Python API: nothing took a plan file, nothing checked health after the commit,
and nothing compared a composition's capabilities with the generation it
replaces. This file covers the new surface end to end:

  * admission refuses a plan whose provider breaks a pinned consumer surface,
    and the refusal happens BEFORE any boundary opens, so the first composition
    is still on its prior generation (a real generation-1 deploy's state is
    byte-identical after the refused generation-2 run);
  * a health probe that fails after the commit restores every composition to
    its prior generation: the durable abort decision is written, each
    participant runs its own LIFO unwind, and the failed generation's world
    files are empty while the prior generation's are untouched;
  * a capability the new generation reaches and the old one does not is
    reported, and refused unless the plan names it in `allow_capabilities`;
  * the command exits non-zero with the refusal named, on stdout for a human and
    in JSON for a pipeline.

The contract and irreversible-crossing verdicts are `deploy.federation_admission`'s
own; these tests pin the plan surface around it, not a second copy of the rule.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl import deploy  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.federation import consumer_surface  # noqa: E402
from revl.federation_apply import (  # noqa: E402
    REFUSE_CAPABILITY_WIDENING,
    apply_federation_plan,
    federation_plan_admission,
    load_federation_plan,
)

# --- the pinned consumer surface: App requires Store from another composition.
CONSUMER = """
service Store { fn get(key: Str) -> Str }
component App requires s: Store { }
"""

# --- provider v1 satisfies the pin; v2 narrows the return type (a MAJOR drift).
PROVIDER_V1 = """
service Store { fn get(key: Str) -> Str }
component Impl provides s: Store {
  provide s { fn get(key) = "v" }
}
"""

PROVIDER_V2 = """
service Store { fn get(key: Str) -> Int }
component Impl provides s: Store {
  provide s { fn get(key) = 0 }
}
"""

# --- an admissible capability-widening pair. App calls a mediated operation
# the provider supplies LOCALLY, so `federation_admission` walks the provider's
# own scope (a deferrable crossing) and does not refuse the mediated call; the
# derived capability surface still reports the new `host_put` scope.
IMPL_PRIOR = """
extern emission deferred fn host_put(k: Str) = @py { pass }
service Store { emission[host_put] fn put(k: Str) -> Unit }
component Impl provides store: Store {
  provide store { fn put(k) = emit host_put(k) }
}
"""

IMPL_NEW = IMPL_PRIOR + """
component App requires store: Store {
  emit store.put("a")
}
"""

EMPTY_ALPHA = "component Alpha { }\n"
EMPTY_BETA = "component Beta { }\n"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _plan(tmp_path: Path, name: str, body: str) -> Path:
    return _write(tmp_path / f"{name}.toml", body)


def _surface(tmp_path: Path, source: str = CONSUMER,
             consumer: str = "app") -> Path:
    document = consumer_surface(compile_source(source, "<consumer>"),
                                consumer=consumer)
    return _write(tmp_path / "app.surface.json", json.dumps(document))


def _world(state_dir: Path, identity: str) -> dict:
    path = state_dir / f"{identity}.world.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _snapshot(directory: Path) -> dict:
    if not directory.exists():
        return {}
    return {path.relative_to(directory): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


def _last_decision(state_dir: Path) -> tuple:
    return deploy.read_decision(str(state_dir / "decision.jsonl"))


def _contract_plan(tmp_path: Path, provider_source: str,
                   generation: int) -> Path:
    _write(tmp_path / "app.revl", CONSUMER)
    _write(tmp_path / "billing.revl", provider_source)
    _surface(tmp_path)
    return _plan(tmp_path, f"contract-gen{generation}", f"""
federation = "shop"
generation = {generation}

[compositions.app]
source = "app.revl"

[compositions.billing]
source = "billing.revl"

[[contracts]]
surface = "app.surface.json"
provider = "billing"
""")


# ==========================================================================
# 1. admission: the contract break refuses before anything moves
# ==========================================================================


def test_a_plan_whose_second_composition_breaks_a_pinned_contract_is_refused(
        tmp_path):
    plan = load_federation_plan(_contract_plan(tmp_path, PROVIDER_V2, 2))
    verdict = federation_plan_admission(plan)
    assert verdict["admitted"] is False
    (refusal,) = [row for row in verdict["refusals"]
                  if row["kind"] == deploy.REFUSE_CONTRACT]
    assert refusal["composition"] == "billing"
    assert "pinned surface" in refusal["reason"]
    assert verdict["widened"] == []


def test_a_refused_update_leaves_the_prior_generation_untouched(tmp_path):
    """Generation 1 is applied for real; generation 2 breaks the pin. The refused
    run opens no boundary, so generation 1's state is byte-identical afterwards
    and generation 2 has no state directory at all."""
    state = tmp_path / "state"
    good = load_federation_plan(_contract_plan(tmp_path, PROVIDER_V1, 1))
    applied = apply_federation_plan(good, state_dir=state / "1")
    assert applied["verdict"] == deploy.DEPLOY_APPLIED, applied
    before = _snapshot(state / "1")
    assert before, "the generation-1 deploy must have written participant state"

    broken = load_federation_plan(_contract_plan(tmp_path, PROVIDER_V2, 2))
    refused = apply_federation_plan(broken, state_dir=state / "2")

    assert refused["verdict"] == deploy.DEPLOY_REFUSED
    assert refused["phase"] == "admit"
    assert any(row["kind"] == deploy.REFUSE_CONTRACT
               for row in refused["refusals"])
    assert not (state / "2").exists(), "admission must open no boundary"
    assert _snapshot(state / "1") == before, "the prior generation moved"
    # and the first composition is still exactly where generation 1 left it
    assert _world(state / "1", "app") != {}


# ==========================================================================
# 2. the health probe runs after the commit and restores the prior generation
# ==========================================================================


def _probe(tmp_path: Path, name: str, code: int) -> Path:
    return _write(tmp_path / name, f"import sys\nsys.exit({code})\n")


def _probe_plan(tmp_path: Path, generation: int, beta_code: int) -> Path:
    _write(tmp_path / "alpha.revl", EMPTY_ALPHA)
    _write(tmp_path / "beta.revl", EMPTY_BETA)
    _probe(tmp_path, "probe_ok.py", 0)
    _probe(tmp_path, "probe_fail.py", 1)
    beta_probe = "probe_fail.py" if beta_code else "probe_ok.py"
    return _plan(tmp_path, f"probe-gen{generation}", f"""
federation = "probe-fed"
generation = {generation}

[compositions.alpha]
source = "alpha.revl"
health_probe = ["{sys.executable}", "probe_ok.py"]

[compositions.beta]
source = "beta.revl"
health_probe = ["{sys.executable}", "{beta_probe}"]
""")


def test_a_failing_health_probe_restores_every_composition(tmp_path):
    state = tmp_path / "state"
    # Generation 1 is healthy: both compositions apply and stay applied.
    healthy = load_federation_plan(_probe_plan(tmp_path, 1, beta_code=0))
    first = apply_federation_plan(healthy, state_dir=state / "1")
    assert first["verdict"] == deploy.DEPLOY_APPLIED, first
    prior = _snapshot(state / "1")
    assert _world(state / "1", "alpha") == {"alpha:Alpha": True}

    # Generation 2 commits, then beta's probe fails: every composition must be
    # back on generation 1.
    failing = load_federation_plan(_probe_plan(tmp_path, 2, beta_code=1))
    report = apply_federation_plan(failing, state_dir=state / "2")

    assert report["verdict"] == deploy.DEPLOY_ABORTED_CLEAN, report
    assert report["phase"] == "health"
    assert report["failedProbe"]["composition"] == "beta"
    assert report["abortOrder"] == ["beta", "alpha"]
    assert report["participants"]["alpha"]["outcome"] == deploy.ROLLED_BACK_CLEAN
    assert report["participants"]["beta"]["outcome"] == deploy.ROLLED_BACK_CLEAN
    assert report["residue"]["clean"] is True
    # the failed generation unwound itself ...
    assert _world(state / "2", "alpha") == {}
    assert _world(state / "2", "beta") == {}
    # ... and the durable decision is an abort, appended after the approval
    kind, record = _last_decision(state / "2")
    assert kind == deploy.FEDERATION_ABORTED
    assert record["failed"] == "beta"
    assert record["generation"] == 2
    # the prior generation never moved
    assert _snapshot(state / "1") == prior


def test_a_healthy_update_reports_its_probes_and_keeps_the_commit(tmp_path):
    state = tmp_path / "state"
    plan = load_federation_plan(_probe_plan(tmp_path, 3, beta_code=0))
    report = apply_federation_plan(plan, state_dir=state / "3")
    assert report["verdict"] == deploy.DEPLOY_APPLIED, report
    assert [probe["composition"] for probe in report["probes"]] == ["alpha", "beta"]
    assert all(probe["ok"] for probe in report["probes"])
    assert _world(state / "3", "beta") == {"beta:Beta": True}
    # the generation is carried into the participant's own durable record, not
    # just the coordinator's decision file.
    header = json.loads((state / "3" / "alpha.wal").read_text(
        encoding="utf-8").splitlines()[0])
    assert header["generation"] == 3
    kind, record = _last_decision(state / "3")
    assert kind == deploy.FEDERATION_APPROVED
    assert record["generation"] == 3


def test_a_composition_with_no_probe_is_reported_not_failed(tmp_path):
    _write(tmp_path / "alpha.revl", EMPTY_ALPHA)
    plan = load_federation_plan(_plan(tmp_path, "noprobe", """
federation = "probe-fed"
generation = 1

[compositions.alpha]
source = "alpha.revl"
"""))
    report = apply_federation_plan(plan, state_dir=tmp_path / "state")
    assert report["verdict"] == deploy.DEPLOY_APPLIED
    (probe,) = report["probes"]
    assert probe["declared"] is False and probe["ok"] is True


# ==========================================================================
# 3. capability widening is reported, and refused unless the plan allows it
# ==========================================================================


def _widening_plan(tmp_path: Path, *, allow: bool) -> dict:
    _write(tmp_path / "impl_prior.revl", IMPL_PRIOR)
    _write(tmp_path / "impl_new.revl", IMPL_NEW)
    allow_line = ('allow_capabilities = ["host_put"]' if allow else "")
    path = _plan(tmp_path, "widening", f"""
federation = "shop"
generation = 2

[compositions.impl]
source = "impl_new.revl"
prior = "impl_prior.revl"
{allow_line}
""")
    return load_federation_plan(path)


def test_a_widened_capability_set_is_refused_and_reported(tmp_path):
    verdict = federation_plan_admission(_widening_plan(tmp_path, allow=False))
    assert verdict["admitted"] is False
    (row,) = verdict["widened"]
    assert row["composition"] == "impl"
    assert row["capabilities"] == ["host_put"]
    (refusal,) = [r for r in verdict["refusals"]
                  if r["kind"] == REFUSE_CAPABILITY_WIDENING]
    assert refusal["capabilities"] == ["host_put"]
    assert "allow_capabilities" in refusal["reason"]


def test_a_plan_that_allows_the_widening_admits_it(tmp_path):
    plan = _widening_plan(tmp_path, allow=True)
    verdict = federation_plan_admission(plan)
    assert verdict["admitted"] is True, verdict
    (row,) = verdict["widened"]
    assert row["capabilities"] == ["host_put"]
    assert row["allowed"] == ["host_put"]
    report = apply_federation_plan(plan, state_dir=tmp_path / "state")
    assert report["verdict"] == deploy.DEPLOY_APPLIED, report
    assert report["widened"][0]["composition"] == "impl"


def test_a_narrowed_capability_set_is_not_reported(tmp_path):
    _write(tmp_path / "impl_prior.revl", IMPL_NEW)
    _write(tmp_path / "impl_new.revl", IMPL_PRIOR)
    plan = load_federation_plan(_plan(tmp_path, "narrowed", """
generation = 2

[compositions.impl]
source = "impl_new.revl"
prior = "impl_prior.revl"
"""))
    verdict = federation_plan_admission(plan)
    assert verdict["admitted"] is True, verdict
    assert verdict["widened"] == []


# ==========================================================================
# 4. the command
# ==========================================================================


def test_the_command_refuses_a_breaking_plan_non_zero_and_names_the_refusal(
        tmp_path, capsys):
    plan = _contract_plan(tmp_path, PROVIDER_V2, 2)
    assert main(["federation-apply", str(plan), "--dry-run"]) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out
    assert deploy.REFUSE_CONTRACT in out
    assert "billing" in out


def test_the_command_json_admission_is_machine_readable(tmp_path, capsys):
    plan = _contract_plan(tmp_path, PROVIDER_V2, 2)
    assert main(["federation-apply", str(plan), "--dry-run", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["admitted"] is False
    assert any(row["kind"] == deploy.REFUSE_CONTRACT
               for row in report["refusals"])


def test_the_command_applies_a_plan_and_exits_zero(tmp_path, capsys):
    _write(tmp_path / "alpha.revl", EMPTY_ALPHA)
    plan = _plan(tmp_path, "apply", """
federation = "probe-fed"
generation = 1

[compositions.alpha]
source = "alpha.revl"
""")
    state = tmp_path / "state"
    assert main(["federation-apply", str(plan),
                 "--state-dir", str(state)]) == 0
    assert "applied" in capsys.readouterr().out
    assert (state / "alpha.wal").exists()


# ==========================================================================
# 5. the plan is validated like the unauthenticated input it is
# ==========================================================================


def test_an_unknown_plan_key_is_refused_not_ignored(tmp_path):
    path = _plan(tmp_path, "unknown", """
generation = 1

[compositions.alpha]
source = "alpha.revl"
probe = "typo"
""")
    try:
        load_federation_plan(path)
    except ValueError as error:
        assert "unrecognized key" in str(error) and "probe" in str(error)
    else:  # pragma: no cover — the assertion above is the test
        raise AssertionError("an unknown key was ignored")


def test_a_plan_with_no_compositions_is_refused(tmp_path):
    path = _plan(tmp_path, "empty", 'generation = 1\n')
    try:
        load_federation_plan(path)
    except ValueError as error:
        assert "compositions" in str(error)
    else:  # pragma: no cover
        raise AssertionError("an empty plan was accepted")


def test_relative_sources_resolve_against_the_plan_directory(tmp_path):
    plan_dir = tmp_path / "plans"
    _write(plan_dir / "alpha.revl", EMPTY_ALPHA)
    path = _plan(plan_dir, "relative", """
generation = 1

[compositions.alpha]
source = "alpha.revl"
""")
    plan = load_federation_plan(path)
    assert plan["compositions"]["alpha"]["source"] == plan_dir / "alpha.revl"
