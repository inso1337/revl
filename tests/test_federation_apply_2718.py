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
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl import deploy  # noqa: E402
from revl import federation_apply  # noqa: E402
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


def _decision_kinds(state_dir: Path) -> list:
    path = state_dir / "decision.jsonl"
    return [json.loads(line)["record"]
            for line in path.read_text(encoding="utf-8").splitlines()]


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
    # ... and the durable decision is an abort, and no approval was ever
    # written for the unhealthy generation
    assert _decision_kinds(state / "2") == [deploy.FEDERATION_PENDING,
                                            deploy.FEDERATION_ABORTED]
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
    assert _decision_kinds(state / "3") == [deploy.FEDERATION_PENDING,
                                            deploy.FEDERATION_APPROVED]


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
    else:  # pragma: no cover (the assertion above is the test)
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


def test_a_json_plan_loads_like_a_toml_plan(tmp_path):
    _write(tmp_path / "alpha.revl", EMPTY_ALPHA)
    path = _write(tmp_path / "plan.json", json.dumps({
        "federation": "shop",
        "generation": 4,
        "compositions": {"alpha": {"source": "alpha.revl"}},
        "contracts": [],
    }))
    plan = load_federation_plan(path)
    assert plan["federation"] == "shop"
    assert plan["generation"] == 4
    assert plan["compositions"]["alpha"]["source"] == tmp_path / "alpha.revl"


# ==========================================================================
# 6. the decision file, the generation directory and the lock
# ==========================================================================

posix_only = pytest.mark.skipif(os.name != "posix",
                                reason="signals and process groups are POSIX")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _wait_gone(pids, seconds: float = 10.0) -> set:
    deadline = time.monotonic() + seconds
    alive = {pid for pid in pids if _alive(pid)}
    while alive and time.monotonic() < deadline:
        time.sleep(0.05)
        alive = {pid for pid in alive if _alive(pid)}
    return alive


def _kill_all(pids) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


CONDUCTOR = """
import os, signal, sys
sys.path.insert(0, sys.argv[1])
from revl import deploy, federation_apply as F

plan, state, pidfile = sys.argv[2:5]
real_launch = deploy.launch_local_participant


def launch(target, **kwargs):
    participant, error = real_launch(target, **kwargs)
    if participant is not None:
        with open(pidfile, "a", encoding="utf-8") as handle:
            handle.write(f"{participant.process.pid}\\n")
    return participant, error


real_commit = deploy.ProcessParticipant.commit


def commit(self):
    report = real_commit(self)
    os.kill(os.getpid(), signal.SIGKILL)   # the conductor dies right here
    return report


deploy.launch_local_participant = launch
deploy.ProcessParticipant.commit = commit
F.apply_federation_plan(F.load_federation_plan(plan), state_dir=state)
"""


def _kill_after_first_commit(tmp_path: Path) -> Path:
    plan = _probe_plan(tmp_path, 1, beta_code=0)
    state = tmp_path / "state" / "1"
    script = _write(tmp_path / "conductor.py", CONDUCTOR)
    pidfile = tmp_path / "participants.pid"
    pids: list[int] = []
    try:
        done = subprocess.run(
            [sys.executable, str(script), str(ROOT / "src"), str(plan),
             str(state), str(pidfile)],
            capture_output=True, text=True, timeout=120, check=False)
        if pidfile.exists():
            pids = [int(line) for line in
                    pidfile.read_text(encoding="utf-8").split()]
        assert done.returncode == -signal.SIGKILL, done.stderr
        assert len(pids) == 2
        assert not _wait_gone(pids), "a participant outlived its conductor"
    finally:
        _kill_all(pids)
    return state


@posix_only
def test_a_conductor_killed_after_the_first_commit_leaves_no_approval(
        tmp_path):
    """The conductor is SIGKILLed after alpha commits and before beta does.
    The pending record written before the first launch is the last record, so
    the stranded alpha settles against "no decision", never an approval."""
    state = _kill_after_first_commit(tmp_path)
    assert _world(state, "alpha") == {"alpha:Alpha": True}   # stranded
    assert _world(state, "beta") == {}                       # never committed
    kind, record = _last_decision(state)
    assert kind is None
    assert record["record"] == deploy.FEDERATION_PENDING
    assert record["generation"] == 1
    settled = deploy.settle_stranded(str(state / "alpha.wal"),
                                     str(state / "decision.jsonl"))
    assert settled["federationDecision"] == "none", settled


@posix_only
def test_a_conductor_killed_after_the_first_commit_settles_back(tmp_path):
    state = _kill_after_first_commit(tmp_path)
    settled = deploy.settle_stranded(str(state / "alpha.wal"),
                                     str(state / "decision.jsonl"))
    assert settled["verdict"] == "rolled-back", settled


def test_reapplying_a_generation_into_its_own_directory_is_refused(tmp_path):
    """Generation 1 applied and was approved. A second generation-1 plan aimed
    at the same directory is refused before anything is written, so its failing
    probe cannot unwind the generation that is live."""
    state = tmp_path / "state" / "1"
    first = apply_federation_plan(
        load_federation_plan(_probe_plan(tmp_path, 1, beta_code=0)),
        state_dir=state)
    assert first["verdict"] == deploy.DEPLOY_APPLIED, first
    before = _snapshot(state)

    again = apply_federation_plan(
        load_federation_plan(_probe_plan(tmp_path, 1, beta_code=1)),
        state_dir=state)

    assert again["verdict"] == deploy.DEPLOY_REFUSED, again
    assert again["phase"] == "generation"
    assert "already holds a decision record" in again["reason"]
    assert _snapshot(state) == before
    assert _world(state, "alpha") == {"alpha:Alpha": True}
    assert _last_decision(state)[0] == deploy.FEDERATION_APPROVED


def test_a_reused_state_dir_is_refused_by_the_command(tmp_path, capsys):
    state = tmp_path / "state"
    assert main(["federation-apply", str(_probe_plan(tmp_path, 1, 0)),
                 "--state-dir", str(state)]) == 0
    capsys.readouterr()
    before = _snapshot(state)
    assert main(["federation-apply", str(_probe_plan(tmp_path, 2, 1)),
                 "--state-dir", str(state)]) == 1
    out = capsys.readouterr().out
    assert "refused" in out and "already holds a decision record" in out
    assert _snapshot(state) == before


GRANDCHILD_PROBE = """
import subprocess, sys, time
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL)
with open("grandchild.pid", "w", encoding="utf-8") as handle:
    handle.write(str(child.pid))
time.sleep(60)
"""


def _one_probe_plan(tmp_path: Path, name: str, probe_source: str,
                    generation: int = 1) -> Path:
    _write(tmp_path / "alpha.revl", EMPTY_ALPHA)
    _write(tmp_path / "beta.revl", EMPTY_BETA)
    _write(tmp_path / f"{name}.py", probe_source)
    return _plan(tmp_path, name, f"""
federation = "probe-fed"
generation = {generation}

[compositions.alpha]
source = "alpha.revl"
health_probe = ["{sys.executable}", "{name}.py"]

[compositions.beta]
source = "beta.revl"
""")


@posix_only
def test_a_timed_out_probe_is_killed_with_its_children_and_restores(tmp_path):
    plan = load_federation_plan(
        _one_probe_plan(tmp_path, "probe_hang", GRANDCHILD_PROBE))
    state = tmp_path / "state" / "1"
    pidfile = tmp_path / "grandchild.pid"
    grandchild: list[int] = []
    try:
        report = apply_federation_plan(plan, state_dir=state, probe_timeout=3)
        if pidfile.exists():
            grandchild = [int(pidfile.read_text(encoding="utf-8"))]
        assert report["verdict"] == deploy.DEPLOY_ABORTED_CLEAN, report
        assert report["phase"] == "health"
        assert "did not finish within 3s" in report["failedProbe"]["reason"]
        assert grandchild, "the probe never started its child"
        assert not _wait_gone(grandchild, 5.0), (
            "the timed-out probe's child is still running")
    finally:
        _kill_all(grandchild)
    assert _world(state, "alpha") == {} and _world(state, "beta") == {}
    assert _last_decision(state)[0] == deploy.FEDERATION_ABORTED


INTERRUPTING_PROBE = """
import os, signal, time
os.kill(os.getppid(), signal.SIGINT)   # the operator presses Ctrl-C
time.sleep(60)                          # while the probe hangs
"""


@posix_only
def test_an_interrupted_hung_probe_writes_the_abort_and_unwinds(tmp_path):
    plan = load_federation_plan(
        _one_probe_plan(tmp_path, "probe_interrupt", INTERRUPTING_PROBE))
    state = tmp_path / "state" / "1"
    with pytest.raises(KeyboardInterrupt):
        apply_federation_plan(plan, state_dir=state, probe_timeout=30)
    kind, record = _last_decision(state)
    assert kind == deploy.FEDERATION_ABORTED, record
    assert "KeyboardInterrupt" in record["reason"]
    assert deploy.FEDERATION_APPROVED not in _decision_kinds(state)
    assert _world(state, "alpha") == {} and _world(state, "beta") == {}


def test_a_probe_that_raises_writes_the_abort_and_unwinds(tmp_path,
                                                          monkeypatch):
    """Anything a probe raises, not only a timeout or an OSError, aborts the
    update before the error goes on up."""
    plan = load_federation_plan(
        _one_probe_plan(tmp_path, "probe_marker", "raise SystemExit(0)\n"))
    real_popen = subprocess.Popen

    def popen(args, *rest, **kwargs):
        if isinstance(args, (list, tuple)) and any(
                str(word).endswith("probe_marker.py") for word in args):
            raise ValueError("embedded null byte")
        return real_popen(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    state = tmp_path / "state" / "1"
    with pytest.raises(ValueError, match="embedded null byte"):
        apply_federation_plan(plan, state_dir=state)
    assert _last_decision(state)[0] == deploy.FEDERATION_ABORTED
    assert _world(state, "alpha") == {} and _world(state, "beta") == {}


def test_a_nul_byte_in_a_probe_is_refused_when_the_plan_is_loaded(tmp_path):
    _write(tmp_path / "alpha.revl", EMPTY_ALPHA)
    path = _plan(tmp_path, "nul", """
generation = 1

[compositions.alpha]
source = "alpha.revl"
health_probe = ["true\\u0000x"]
""")
    with pytest.raises(ValueError, match="NUL byte"):
        load_federation_plan(path)


def _interrupt_beta_abort(tmp_path: Path, monkeypatch) -> Path:
    real_abort = deploy.ProcessParticipant.abort

    def abort(self):
        if self.identity == "beta":
            raise KeyboardInterrupt("Ctrl-C during the abort")
        return real_abort(self)

    monkeypatch.setattr(deploy.ProcessParticipant, "abort", abort)
    state = tmp_path / "state" / "1"
    with pytest.raises(KeyboardInterrupt):
        apply_federation_plan(
            load_federation_plan(_probe_plan(tmp_path, 1, beta_code=1)),
            state_dir=state)
    return state


def test_an_interrupted_abort_still_unwinds_the_other_participants(
        tmp_path, monkeypatch):
    """beta's probe fails, so beta then alpha are asked to unwind. An interrupt
    while beta unwinds is recorded, alpha is still unwound, and the interrupt
    is re-raised; beta is stranded under the abort decision."""
    state = _interrupt_beta_abort(tmp_path, monkeypatch)
    assert _world(state, "alpha") == {}
    assert _world(state, "beta") == {"beta:Beta": True}      # stranded
    assert _last_decision(state)[0] == deploy.FEDERATION_ABORTED
    settled = deploy.settle_stranded(str(state / "beta.wal"),
                                     str(state / "decision.jsonl"))
    assert settled["federationDecision"] == deploy.FEDERATION_ABORTED, settled


def test_a_participant_stranded_under_the_abort_decision_settles_back(
        tmp_path, monkeypatch):
    state = _interrupt_beta_abort(tmp_path, monkeypatch)
    settled = deploy.settle_stranded(str(state / "beta.wal"),
                                     str(state / "decision.jsonl"))
    assert settled["verdict"] == "rolled-back", settled


def test_a_second_apply_on_a_locked_state_dir_is_refused(tmp_path,
                                                         monkeypatch):
    """Two applies on one directory: the second starts while the first is in
    its health probes, holding the lock, and is refused without writing."""
    state = tmp_path / "state" / "1"
    other = tmp_path / "other"
    second_plan = load_federation_plan(_probe_plan(other, 1, beta_code=0))
    seen: dict = {}
    real_probes = federation_apply.run_health_probes

    def probes(plan, **kwargs):
        seen["kinds"] = _decision_kinds(state)
        seen["second"] = apply_federation_plan(second_plan, state_dir=state)
        return real_probes(plan, **kwargs)

    monkeypatch.setattr(federation_apply, "run_health_probes", probes)
    first = apply_federation_plan(
        load_federation_plan(_probe_plan(tmp_path, 1, beta_code=0)),
        state_dir=state)

    assert first["verdict"] == deploy.DEPLOY_APPLIED, first
    assert seen["second"]["verdict"] == deploy.DEPLOY_REFUSED, seen["second"]
    assert seen["second"]["phase"] == "lock"
    assert seen["kinds"] == [deploy.FEDERATION_PENDING]
    assert _decision_kinds(state) == [deploy.FEDERATION_PENDING,
                                      deploy.FEDERATION_APPROVED]


# ==========================================================================
# 7. reporting: no prior, the default state dir, probe output
# ==========================================================================


def test_a_composition_with_no_prior_is_reported_not_compared(tmp_path,
                                                             capsys):
    _write(tmp_path / "alpha.revl", EMPTY_ALPHA)
    path = _plan(tmp_path, "noprior", """
generation = 1

[compositions.alpha]
source = "alpha.revl"
""")
    verdict = federation_plan_admission(load_federation_plan(path))
    assert verdict["admitted"] is True
    assert verdict["notCompared"] == ["alpha"]
    assert main(["federation-apply", str(path), "--dry-run"]) == 0
    assert "alpha: capability widening not compared" in capsys.readouterr().out
    assert main(["federation-apply", str(path), "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["notCompared"] == ["alpha"]
    compared = federation_plan_admission(_widening_plan(tmp_path, allow=True))
    assert compared["notCompared"] == []


def test_the_default_state_dir_is_keyed_on_the_federation_name(
        tmp_path, monkeypatch, capsys):
    """Two plans both called `plan.toml` are two updates, not one."""
    from revl import wal as wal_module

    root = tmp_path / "wal"
    monkeypatch.setattr(wal_module, "default_wal_dir", lambda: str(root))
    for name in ("fed-a", "fed-b"):
        _write(tmp_path / name / "alpha.revl", EMPTY_ALPHA)
        plan = _write(tmp_path / name / "plan.toml", f"""
federation = "{name}"
generation = 1

[compositions.alpha]
source = "alpha.revl"
""")
        assert main(["federation-apply", str(plan)]) == 0, capsys.readouterr()
    for name in ("fed-a", "fed-b"):
        assert (root / "federation" / name / "1" / "alpha.wal").is_file()


def test_probe_output_is_not_copied_into_the_report(tmp_path):
    plan = load_federation_plan(_one_probe_plan(
        tmp_path, "probe_secret",
        "import sys\nsys.stderr.write('token=hunter2-SECRET\\n')\n"
        "sys.exit(1)\n"))
    report = apply_federation_plan(plan, state_dir=tmp_path / "state" / "1")
    assert report["verdict"] == deploy.DEPLOY_ABORTED_CLEAN, report
    reason = report["failedProbe"]["reason"]
    assert reason.startswith("the probe exited 1")
    assert "not copied" in reason
    assert "hunter2" not in json.dumps(report)


def test_a_failed_commit_writes_the_abort_before_any_abort_message(
        tmp_path, monkeypatch):
    real_commit = deploy.ProcessParticipant.commit
    real_abort = deploy.ProcessParticipant.abort
    state = tmp_path / "state" / "1"
    seen: list = []

    def commit(self):
        if self.identity == "beta":
            return {"ok": False, "reason": "beta: applying failed"}
        return real_commit(self)

    def abort(self):
        seen.append(_decision_kinds(state))
        return real_abort(self)

    monkeypatch.setattr(deploy.ProcessParticipant, "commit", commit)
    monkeypatch.setattr(deploy.ProcessParticipant, "abort", abort)
    report = apply_federation_plan(
        load_federation_plan(_probe_plan(tmp_path, 1, beta_code=0)),
        state_dir=state)
    assert report["verdict"] == deploy.DEPLOY_ABORTED_CLEAN, report
    assert seen == [[deploy.FEDERATION_PENDING, deploy.FEDERATION_ABORTED]]
    kind, record = _last_decision(state)
    assert kind == deploy.FEDERATION_ABORTED and record["failed"] == "beta"
    assert _world(state, "alpha") == {}


GROUP_INTERRUPT_CONDUCTOR = """
import sys
sys.path.insert(0, sys.argv[1])
from revl import deploy, federation_apply as F

plan, state, pidfile = sys.argv[2:5]
real_launch = deploy.launch_local_participant


def launch(target, **kwargs):
    participant, error = real_launch(target, **kwargs)
    if participant is not None:
        with open(pidfile, "a", encoding="utf-8") as handle:
            handle.write(f"{participant.process.pid}\\n")
    return participant, error


deploy.launch_local_participant = launch
try:
    F.apply_federation_plan(F.load_federation_plan(plan), state_dir=state,
                            probe_timeout=60)
except KeyboardInterrupt:
    sys.exit(130)
sys.exit(0)
"""

GROUP_INTERRUPTING_PROBE = """
import os, signal, time
# A terminal's Ctrl-C: SIGINT to the conductor's whole process group, which
# holds the conductor and its participants (this probe has its own session).
os.killpg(os.getpgid(os.getppid()), signal.SIGINT)
time.sleep(60)
"""


@posix_only
def test_a_ctrl_c_to_the_whole_process_group_still_unwinds(tmp_path):
    """The conductor runs in a process group of its own, as a terminal's
    foreground job does; the probe sends SIGINT to that whole group. The
    participants must survive it, so the conductor can ask them to unwind."""
    plan = _one_probe_plan(tmp_path, "probe_group", GROUP_INTERRUPTING_PROBE)
    state = tmp_path / "state" / "1"
    script = _write(tmp_path / "group_conductor.py", GROUP_INTERRUPT_CONDUCTOR)
    pidfile = tmp_path / "participants.pid"
    conductor = subprocess.Popen(
        [sys.executable, str(script), str(ROOT / "src"), str(plan),
         str(state), str(pidfile)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True)
    try:
        _, err = conductor.communicate(timeout=120)
    finally:
        try:
            os.killpg(conductor.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        conductor.wait()
    pids = ([int(line) for line in pidfile.read_text(encoding="utf-8").split()]
            if pidfile.exists() else [])
    _kill_all(pids)
    assert conductor.returncode == 130, err
    assert len(pids) == 2
    assert _decision_kinds(state) == [deploy.FEDERATION_PENDING,
                                      deploy.FEDERATION_ABORTED]
    assert _world(state, "alpha") == {} and _world(state, "beta") == {}


def test_a_dangling_symlink_in_place_of_the_decision_file_is_refused(
        tmp_path):
    state = tmp_path / "state" / "1"
    state.mkdir(parents=True)
    target = tmp_path / "elsewhere.jsonl"
    try:
        (state / "decision.jsonl").symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover
        pytest.skip("this platform cannot make a symlink here")
    report = apply_federation_plan(
        load_federation_plan(_probe_plan(tmp_path, 1, beta_code=0)),
        state_dir=state)
    assert report["verdict"] == deploy.DEPLOY_REFUSED, report
    assert report["phase"] == "generation"
    assert not target.exists(), "the apply wrote through the symlink"


LINGERING_CHILD_PROBE = """
import subprocess, sys
# Exit 0 at once, leaving a child that inherited stdout and stderr.
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                         stdin=subprocess.DEVNULL)
with open("lingering.pid", "w", encoding="utf-8") as handle:
    handle.write(str(child.pid))
"""


@posix_only
def test_a_probe_that_exits_0_leaving_a_child_is_healthy(tmp_path):
    """The probe's own exit decides, not the end of its output: a child that
    still holds the output does not turn a passed probe into a timeout, and
    it is killed with the probe's group."""
    plan = load_federation_plan(
        _one_probe_plan(tmp_path, "probe_linger", LINGERING_CHILD_PROBE))
    pidfile = tmp_path / "lingering.pid"
    lingering: list[int] = []
    try:
        report = apply_federation_plan(plan, state_dir=tmp_path / "state" / "1",
                                       probe_timeout=5)
        if pidfile.exists():
            lingering = [int(pidfile.read_text(encoding="utf-8"))]
        assert report["verdict"] == deploy.DEPLOY_APPLIED, report
        assert lingering and not _wait_gone(lingering, 5.0)
    finally:
        _kill_all(lingering)


PARENT_KILLING_PROBE = """
import os, signal
# The conductor is SIGKILLed while it waits for this probe: no approval was
# written, and no participant was told to unwind.
os.kill(os.getppid(), signal.SIGKILL)
"""


@posix_only
def test_a_conductor_killed_during_the_probes_settles_every_participant_back(
        tmp_path):
    """No approval was written, so settle returns `rolled-back` for both
    committed participants. The verdict is computed against the model world
    handed in; it does not claim their world files were rewritten."""
    from revl.recovery import DictWorld

    plan = _one_probe_plan(tmp_path, "probe_kill", PARENT_KILLING_PROBE)
    state = tmp_path / "state" / "1"
    script = _write(tmp_path / "group_conductor.py", GROUP_INTERRUPT_CONDUCTOR)
    pidfile = tmp_path / "participants.pid"
    pids: list[int] = []
    try:
        done = subprocess.run(
            [sys.executable, str(script), str(ROOT / "src"), str(plan),
             str(state), str(pidfile)],
            capture_output=True, text=True, timeout=120, check=False)
        if pidfile.exists():
            pids = [int(line) for line in
                    pidfile.read_text(encoding="utf-8").split()]
        assert done.returncode == -signal.SIGKILL, done.stderr
        assert len(pids) == 2
        assert not _wait_gone(pids), "a participant outlived its conductor"
    finally:
        _kill_all(pids)

    assert _decision_kinds(state) == [deploy.FEDERATION_PENDING]
    for identity in ("alpha", "beta"):
        # both committed: their own WALs reached activation-complete
        assert _world(state, identity) == {f"{identity}:{identity.title()}": True}
        world = DictWorld()
        settled = deploy.settle_stranded(str(state / f"{identity}.wal"),
                                         str(state / "decision.jsonl"),
                                         world=world)
        assert settled["verdict"] == "rolled-back", settled
        assert settled["residue"]["clean"] is True, settled
        assert settled["world"] == "model"
        assert world.remaining() == []   # the model world, not the file


def test_settle_refuses_while_the_apply_still_holds_its_lock(tmp_path):
    """A settle run while the apply is still in its probes would report a
    participant rolled back just before the conductor approves it."""
    state = tmp_path / "state" / "1"
    report = apply_federation_plan(
        load_federation_plan(_probe_plan(tmp_path, 1, beta_code=0)),
        state_dir=state)
    assert report["verdict"] == deploy.DEPLOY_APPLIED, report
    wal = state / "alpha.wal"
    before = wal.read_bytes()
    with federation_apply._generation_lock(state) as held:
        assert held is True
        settled = deploy.settle_stranded(str(wal),
                                         str(state / "decision.jsonl"))
    assert settled["verdict"] == deploy.UNRESOLVED, settled
    assert "is held" in settled["decision"]
    assert wal.read_bytes() == before, "settle wrote while the lock was held"
    # once the lock is free the same call settles against the approval
    assert deploy.settle_stranded(
        str(wal), str(state / "decision.jsonl"))["verdict"] == "rolled-forward"
