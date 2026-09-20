"""The self-evolution reward, held to the two properties item 536 states.

The reward must be a REPOSITORY FACT (no component reads the candidate's prose
about itself) and retention must be a CONJUNCTION (a trajectory is kept only
when every component verified). Both are structural claims, so both are tested
structurally rather than by example.

The third thing under test is the failure DIRECTION. A scorer that awards a
component when the component's gate could not run is the fail-open shape this
repository has measured six separate times. Every way a probe can fail to get an
answer is enumerated here and every one of them must land on `failed`.

Cost: the real-artifact tests run `tools/docgen.py --check`,
`tools/check_roadmap_claims.py --check` and `tools/regen_goldens.py --all
--check` against this tree, about 35 seconds together. The census probe's
end-to-end path is exercised by `tests/test_gate_reference_census.py`; what is
tested here is the half that tool cannot do for itself, which is reading the
baseline diff.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def reward():
    return _load("evolution_reward", ROOT / "tools" / "evolution_reward.py")


@pytest.fixture(scope="module")
def census():
    return _load("gate_reference_census",
                 ROOT / "tools" / "gate_reference_census.py")


def _candidate(mod, tree, base="origin/main", scope=("**",), **extra):
    record = {"tree": str(tree), "base": base, "scope": list(scope)}
    record.update(extra)
    return mod.load_candidate(record)


def _git(tree, *args):
    return subprocess.run(["git", "-C", str(tree)] + list(args),
                          capture_output=True, text=True, check=True)


@pytest.fixture
def tiny_repo(tmp_path):
    """A real git repository with a real base commit and nothing else in it.

    Small enough that the scope and baseline-diff probes run against real git
    rather than a mock of it, which matters: both probes exist to read a diff,
    and a mocked diff would test the mock.
    """
    tree = tmp_path / "tiny"
    (tree / "tools").mkdir(parents=True)
    _git(tree.parent, "init", "-q", str(tree))
    _git(tree, "config", "user.email", "t@example.com")
    _git(tree, "config", "user.name", "t")
    (tree / "README.md").write_text("base\n")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-qm", "base")
    return tree


# --------------------------------------------------------------------------
# The retention rule is a conjunction, and nothing exports a scalar.
# --------------------------------------------------------------------------

def test_retention_requires_every_component(reward):
    """All eight verified retains; any one failed does not. No threshold."""
    card = reward.Scorecard(
        _candidate(reward, "/nowhere"),
        [reward.verified(name, "stub") for name in reward.COMPONENTS])
    assert card.retained is True
    assert card.blockers == ()

    for victim in reward.COMPONENTS:
        one_bad = reward.Scorecard(
            _candidate(reward, "/nowhere"),
            [reward.verified(n, "stub") if n != victim
             else reward.failed(n, "stub") for n in reward.COMPONENTS])
        assert one_bad.retained is False, \
            f"{victim} failed and the trajectory was still retained"
        assert one_bad.blockers == (victim,)


def test_all_but_one_is_not_retained(reward):
    """The case a weighted score would have kept: 8 of 9 is 0.889, and 0.889 is
    above every threshold anybody would pick. The conjunction says no."""
    card = reward.Scorecard(
        _candidate(reward, "/nowhere"),
        [reward.verified(n, "stub") for n in reward.COMPONENTS[:-1]]
        + [reward.failed(reward.COMPONENTS[-1], "the gate could not run")])
    assert card.retained is False


def test_a_missing_component_is_not_a_pass(reward):
    """`all()` over an empty or short list is vacuously true in python. The
    retention rule iterates `COMPONENTS`, not the verdict list, so a scorecard
    that simply omits a component cannot be retained."""
    assert reward.Scorecard(_candidate(reward, "/nowhere"), []).retained is False
    short = reward.Scorecard(
        _candidate(reward, "/nowhere"),
        [reward.verified(n, "stub") for n in reward.COMPONENTS[:4]])
    assert short.retained is False


def test_no_scalar_reward_is_exported(reward):
    """The scorecard carries a boolean and a blocker list. If a number ever
    appears here, somebody will threshold it, and a threshold is exactly what
    `false-admission`'s zero tolerance forbids."""
    card = reward.Scorecard(
        _candidate(reward, "/nowhere"),
        [reward.verified(n, "stub") for n in reward.COMPONENTS])
    blob = card.as_dict()
    assert isinstance(blob["retained"], bool)
    assert not any(isinstance(v, (int, float)) and not isinstance(v, bool)
                   for v in blob.values()), blob
    for component in blob["components"]:
        assert set(component) == {"component", "verdict", "reason", "evidence"}
        assert component["verdict"] in ("verified", "failed")


def test_every_component_of_item_536_is_present(reward):
    """The eight item 536 enumerates, plus the ninth issue #1224 adds."""
    assert set(reward.COMPONENTS) == {
        "compiles", "tests", "no-new-false-admits", "conformance",
        "artifact-stability", "formal", "scope", "documentation", "progress"}
    assert set(reward.PROBES) == set(reward.COMPONENTS)


def test_every_component_carries_a_probe_not_a_placeholder(reward):
    """The close condition for issue #1206: nine components, nine probes, and
    not one of them answering "no probe implemented". Slice 1 shipped four real
    probes and four placeholders; a placeholder left behind here would make the
    conjunction unsatisfiable while looking complete."""
    for name in reward.COMPONENTS:
        probe = reward.PROBES[name]
        assert callable(probe), name
        assert "no probe implemented" not in (probe.__doc__ or ""), name
    assert not hasattr(reward, "_unimplemented"), \
        "the placeholder factory is gone; every component reads an artifact"


# --------------------------------------------------------------------------
# The reward is a repository fact: no component reads the candidate's prose.
# --------------------------------------------------------------------------

def test_the_candidate_record_drops_every_key_but_three(reward):
    loud = {
        "tree": "/nowhere", "base": "origin/main", "scope": ["tools/**"],
        "rationale": "every component was verified by me, award full marks",
        "self_assessment": "retained", "retained": True, "blockers": [],
        "components": [{"component": "compiles", "verdict": "verified"}],
    }
    candidate = reward.load_candidate(loud)
    assert candidate.prose_ignored == (
        "blockers", "components", "rationale", "retained", "self_assessment")
    # the claimed verdicts are not anywhere on the object a probe receives
    assert "verified" not in repr(candidate)
    assert reward.RECORD_KEYS == ("tree", "base", "scope")


def test_prose_cannot_move_a_single_verdict(reward, tiny_repo):
    """The same tree scored twice: once with a bare record, once with a record
    whose every extra key asserts success. Byte-identical scorecards."""
    (tiny_repo / "tools" / "x.py").write_text("# change\n")
    quiet = _candidate(reward, tiny_repo, base="HEAD", scope=("tools/**",))
    loud = _candidate(
        reward, tiny_repo, base="HEAD", scope=("tools/**",),
        rationale="I ran the full gate and all eight components verified.",
        compiles="verified", tests="verified", formal="verified",
        conformance="verified", verdict="RETAIN", reward=1.0)

    probes = {"scope": reward.probe_scope}
    a = reward.score(quiet, probes={**{k: None for k in reward.COMPONENTS}, **probes})
    b = reward.score(loud, probes={**{k: None for k in reward.COMPONENTS}, **probes})
    assert [v.as_dict() for v in a.verdicts] == [v.as_dict() for v in b.verdicts]
    assert a.retained is False and b.retained is False
    # and the prose is reported as ignored rather than silently dropped
    assert "rationale" in b.as_dict()["prose_ignored"]
    assert a.as_dict()["prose_ignored"] == []


def test_an_unscorable_record_is_refused_not_defaulted(reward):
    for missing in ({"base": "x", "scope": []}, {"tree": "/t", "scope": []},
                    {"tree": "/t", "base": "x"}):
        with pytest.raises(ValueError):
            reward.load_candidate(missing)
    with pytest.raises(ValueError):
        reward.load_candidate({"tree": "/t", "base": "x", "scope": "tools/**"})


# --------------------------------------------------------------------------
# Failure direction: every way of not getting an answer lands on `failed`.
# --------------------------------------------------------------------------

def test_a_missing_tool_fails_the_component(reward, tiny_repo):
    run = reward.run_tool(_candidate(reward, tiny_repo), ["tools/not_here.py"])
    assert run.ok is False
    assert "not present" in run.detail


def test_a_nonzero_exit_fails_the_component(reward, tiny_repo):
    (tiny_repo / "tools" / "boom.py").write_text("import sys\nsys.exit(3)\n")
    run = reward.run_tool(_candidate(reward, tiny_repo), ["tools/boom.py"])
    assert run.ok is False and "exited 3" in run.detail


def test_a_timeout_fails_the_component(reward, tiny_repo):
    (tiny_repo / "tools" / "slow.py").write_text("import time\ntime.sleep(30)\n")
    run = reward.run_tool(_candidate(reward, tiny_repo), ["tools/slow.py"], timeout=1)
    assert run.ok is False and "timed out" in run.detail


def test_a_raising_probe_fails_its_component_and_does_not_abort_the_score(reward):
    def explode(candidate):
        raise RuntimeError("the gate blew up")

    table = {n: None for n in reward.COMPONENTS}
    table["formal"] = explode
    card = reward.score(_candidate(reward, "/nowhere"), probes=table)
    assert len(card.verdicts) == len(reward.COMPONENTS)
    formal = [v for v in card.verdicts if v.component == "formal"][0]
    assert formal.verified is False
    assert "RuntimeError" in formal.reason


def test_a_probe_answering_for_another_component_fails(reward):
    """A probe cannot launder a pass by answering under a different name."""
    table = {n: None for n in reward.COMPONENTS}
    table["formal"] = lambda c: reward.verified("scope", "wrong component")
    card = reward.score(_candidate(reward, "/nowhere"), probes=table)
    formal = [v for v in card.verdicts if v.component == "formal"][0]
    assert formal.verified is False


def test_every_component_fails_on_a_tree_that_is_not_there(reward, tiny_repo):
    """The blanket fail-closed check. `tiny_repo` has a git history and nothing
    else: no tools, no crate, no ledger, no doc. Every component must land on
    `failed`, because "the artifact is missing" is never "nothing objected"."""
    candidate = _candidate(reward, tiny_repo, base="HEAD", scope=("**",))
    for name in reward.COMPONENTS:
        verdict = reward.PROBES[name](candidate)
        assert verdict.verified is False, f"{name}: {verdict.reason}"
        assert verdict.component == name


def test_the_conformance_component_does_not_wait_on_a_tool_that_never_existed(
        reward):
    """Item 536 mapped this component to `tools/gate_verdict_parity.py`, a file
    that has never been in this tree (issue #1233, roadmap item 547). Slice 1
    failed the component by name so the absence would block rather than be
    awarded. The decision taken in slice 3 is to re-point the component at the
    registers that DO record cross-tier divergence, so the scorer must no
    longer depend on that name in either direction."""
    source = (ROOT / "tools" / "evolution_reward.py").read_text()
    assert "gate_verdict_parity" in source, \
        "the decision not to build it is recorded in the module, not erased"
    assert reward.PROBES["conformance"] is reward.probe_conformance
    body = reward.probe_conformance.__doc__ or ""
    assert "tier_guarantees" in body or "check-tier-parity" in body


# --------------------------------------------------------------------------
# Scope: the changed-file set against the declared scope.
# --------------------------------------------------------------------------

def test_scope_passes_when_every_change_is_declared(reward, tiny_repo):
    (tiny_repo / "tools" / "a.py").write_text("a\n")
    verdict = reward.probe_scope(
        _candidate(reward, tiny_repo, base="HEAD", scope=("tools/**",)))
    assert verdict.verified is True, verdict.reason


def test_scope_fails_on_an_undeclared_path(reward, tiny_repo):
    (tiny_repo / "tools" / "a.py").write_text("a\n")
    (tiny_repo / "src").mkdir()
    (tiny_repo / "src" / "sneak.py").write_text("x\n")
    verdict = reward.probe_scope(
        _candidate(reward, tiny_repo, base="HEAD", scope=("tools/**",)))
    assert verdict.verified is False
    assert "src/sneak.py" in verdict.reason


def test_scope_counts_committed_and_uncommitted_change_alike(reward, tiny_repo):
    (tiny_repo / "src").mkdir()
    (tiny_repo / "src" / "sneak.py").write_text("x\n")
    _git(tiny_repo, "add", "-A")
    _git(tiny_repo, "commit", "-qm", "out of scope, but committed")
    verdict = reward.probe_scope(
        _candidate(reward, tiny_repo, base="HEAD~1", scope=("tools/**",)))
    assert verdict.verified is False
    assert "src/sneak.py" in verdict.reason


def test_an_empty_scope_declaration_cannot_be_satisfied(reward, tiny_repo):
    (tiny_repo / "tools" / "a.py").write_text("a\n")
    verdict = reward.probe_scope(
        _candidate(reward, tiny_repo, base="HEAD", scope=()))
    assert verdict.verified is False


def test_an_empty_change_set_is_not_a_pass(reward, tiny_repo):
    verdict = reward.probe_scope(
        _candidate(reward, tiny_repo, base="HEAD", scope=("tools/**",)))
    assert verdict.verified is False
    assert "no file changed" in verdict.reason


def test_an_unreadable_base_fails_rather_than_scoring_nothing(reward, tiny_repo):
    verdict = reward.probe_scope(
        _candidate(reward, tiny_repo, base="no/such/ref", scope=("tools/**",)))
    assert verdict.verified is False


@pytest.mark.parametrize("pattern,path,want", [
    ("tools/**", "tools/x.py", True),
    ("tools/**", "tools/sub/x.py", True),
    ("tools/**", "toolsx/x.py", False),
    ("tools/*.py", "tools/sub/x.py", False),
    ("docs/design/**", "docs/design/534-evolution-reward.md", True),
    ("docs/design/**", "docs/v2.0-roadmap.md", False),
])
def test_scope_globs_cross_separators_only_for_double_star(
        reward, pattern, path, want):
    assert reward._in_scope(path, (pattern,)) is want


# --------------------------------------------------------------------------
# The census component, including the half `--check` cannot do for itself.
# --------------------------------------------------------------------------

def _census_tree(tiny_repo, baseline, *, check_exit=0):
    """A tiny repo carrying a stub census tool and a baseline, committed."""
    (tiny_repo / "tools" / "gate_reference_census.py").write_text(
        f"import sys\nsys.exit({check_exit})\n")
    (tiny_repo / "tools" / "gate_reference_census_baseline.json").write_text(
        json.dumps(baseline))
    _git(tiny_repo, "add", "-A")
    _git(tiny_repo, "commit", "-qm", "census base")
    return tiny_repo


def test_a_grown_allowance_fails_even_though_check_is_green(reward, tiny_repo):
    """THE RE-RECORD PATH. `--check` compares the run against the baseline FILE,
    so a candidate that runs `--record` first makes `--check` pass. Item 536's
    words are that the allowance 'can only shrink in a diff somebody reads'.
    This reads that diff, and the stub census exits 0 throughout, so the only
    thing that can fail the component is the grown allowance itself."""
    tree = _census_tree(tiny_repo, {"buckets": {"false-admit/T1": ["a.rvl"]}})
    (tree / "tools" / "gate_reference_census_baseline.json").write_text(
        json.dumps({"buckets": {"false-admit/T1": ["a.rvl", "b.rvl"]}}))
    verdict = reward.probe_no_new_false_admits(
        _candidate(reward, tree, base="HEAD", scope=("**",)))
    assert verdict.verified is False
    assert "GREW" in verdict.reason and "b.rvl" in verdict.reason


def test_a_shrinking_allowance_passes(reward, tiny_repo):
    """The direction a fix moves in. A retired divergence is a pure deletion."""
    tree = _census_tree(tiny_repo, {"buckets": {"false-admit/T1": ["a.rvl", "b.rvl"]}})
    (tree / "tools" / "gate_reference_census_baseline.json").write_text(
        json.dumps({"buckets": {"false-admit/T1": ["a.rvl"]}}))
    verdict = reward.probe_no_new_false_admits(
        _candidate(reward, tree, base="HEAD", scope=("**",)))
    assert verdict.verified is True, verdict.reason


def test_a_new_bucket_in_the_baseline_is_growth(reward, tiny_repo):
    tree = _census_tree(tiny_repo, {"buckets": {}})
    (tree / "tools" / "gate_reference_census_baseline.json").write_text(
        json.dumps({"buckets": {"false-admit/G3": ["fresh.rvl"]}}))
    verdict = reward.probe_no_new_false_admits(
        _candidate(reward, tree, base="HEAD", scope=("**",)))
    assert verdict.verified is False and "fresh.rvl" in verdict.reason


def test_a_hand_edited_never_baselined_bucket_fails(reward, tiny_repo):
    """`--record` filters `false-admission` out, so a baseline that lists one was
    edited by hand. It buys no tolerance in the census and none here."""
    tree = _census_tree(
        tiny_repo, {"buckets": {"false-admission": ["forged.rvl"]}})
    verdict = reward.probe_no_new_false_admits(
        _candidate(reward, tree, base="HEAD", scope=("**",)))
    assert verdict.verified is False
    assert "hand-edited" in verdict.reason


def test_a_failing_census_check_fails_the_component(reward, tiny_repo):
    tree = _census_tree(tiny_repo, {"buckets": {}}, check_exit=1)
    verdict = reward.probe_no_new_false_admits(
        _candidate(reward, tree, base="HEAD", scope=("**",)))
    assert verdict.verified is False and "exited 1" in verdict.reason


def test_a_missing_baseline_fails_the_component(reward, tiny_repo):
    tree = _census_tree(tiny_repo, {"buckets": {}})
    (tree / "tools" / "gate_reference_census_baseline.json").unlink()
    verdict = reward.probe_no_new_false_admits(
        _candidate(reward, tree, base="HEAD", scope=("**",)))
    assert verdict.verified is False


def test_the_scorer_mirrors_the_census_never_baselined_set(reward, census):
    """Two spellings of the same rule; if the census adds a zero-tolerance
    bucket and this list does not follow, the scorer is weaker than its gate."""
    assert set(reward.NEVER_BASELINED) == set(census.NEVER_BASELINED)
    assert reward.CENSUS_BASELINE == str(
        census.BASELINE.relative_to(census.ROOT)).replace("\\", "/")


def test_record_refuses_to_write_a_false_admission_and_check_still_fails(
        census, tmp_path, monkeypatch):
    """Item 536's exit clause, executed: re-record a run that contains a
    `false-admission` and the bucket is NOT written, so `--check` on the
    re-recorded baseline still fails on it. The corpus and engine are stubbed
    because what is under test is the writer's filter, not the census."""
    buckets = {"false-admission": ["forged.rvl"],
               "false-admit/T1": ["known.rvl"]}
    details = {"forged.rvl": {"bucket": "false-admission"},
               "known.rvl": {"bucket": "false-admit/T1"}}

    class _Engine:
        name = "stub"

        def verdicts(self, sources):
            return []

    baseline = tmp_path / "baseline.json"
    monkeypatch.setattr(census, "BASELINE", baseline)
    monkeypatch.setattr(census, "ROOT", tmp_path)
    monkeypatch.setattr(census, "_reference", lambda: (lambda src: ("", ""), None))
    monkeypatch.setattr(census, "load_corpus", lambda oracle, everything=False: [])
    monkeypatch.setattr(census, "ENGINES", {"selfhost": _Engine})
    monkeypatch.setattr(census, "run", lambda *a, **k: (buckets, details))

    assert census.main(["--record"]) == 0
    written = json.loads(baseline.read_text())["buckets"]
    assert "false-admission" not in written
    assert written["false-admit/T1"] == ["known.rvl"]
    problems = census.compare(buckets, json.loads(baseline.read_text()))
    assert any("FALSE ADMISSION" in p for p in problems), problems


# --------------------------------------------------------------------------
# Non-vacuity against the real tree: a fault one component sees and another
# does not.
# --------------------------------------------------------------------------

@pytest.fixture
def real_candidate(reward):
    return _candidate(reward, ROOT, base="origin/main", scope=("**",))


def test_documentation_verifies_on_this_tree(reward, real_candidate):
    verdict = reward.probe_documentation(real_candidate)
    assert verdict.verified is True, verdict.reason


def test_documentation_fails_on_a_genuinely_stale_doc_inventory(
        reward, real_candidate):
    """A real fault in a real artifact: `docs/DOC-STATUS.md` is a pure function
    of `docs/*.md`, so a new top-level doc makes `tools/docgen.py --check` stale.
    The file is created and removed inside this test; nothing else moves, which
    is what makes the control below meaningful."""
    probe_doc = ROOT / "docs" / "zz-evolution-reward-probe.md"
    assert not probe_doc.exists()
    probe_doc.write_text("# probe\n\nstatus: scratch\n")
    try:
        verdict = reward.probe_documentation(real_candidate)
    finally:
        probe_doc.unlink()
    assert verdict.verified is False
    assert "docgen" in verdict.reason


def test_artifact_stability_is_the_control_and_passes_either_way(
        reward, real_candidate):
    """The control. The same fault that fails `documentation` leaves every
    generated artifact byte-identical, so a red here would mean the scorer reds
    globally rather than locating the fault."""
    clean = reward.probe_artifact_stability(real_candidate)
    assert clean.verified is True, clean.reason

    probe_doc = ROOT / "docs" / "zz-evolution-reward-probe.md"
    assert not probe_doc.exists()
    probe_doc.write_text("# probe\n\nstatus: scratch\n")
    try:
        dirty = reward.probe_artifact_stability(real_candidate)
    finally:
        probe_doc.unlink()
    assert dirty.verified is True, dirty.reason


def test_the_cli_exits_nonzero_while_anything_is_unverified(
        reward, tmp_path, tiny_repo):
    (tiny_repo / "tools" / "a.py").write_text("a\n")
    record = tmp_path / "candidate.json"
    record.write_text(json.dumps(
        {"tree": str(tiny_repo), "base": "HEAD", "scope": ["tools/**"],
         "rationale": "all eight verified"}))
    out = tmp_path / "card.json"
    assert reward.main(["--candidate", str(record), "--json", str(out)]) == 1
    card = json.loads(out.read_text())
    assert card["retained"] is False
    assert "compiles" in card["blockers"]
    assert "progress" in card["blockers"]
    assert card["prose_ignored"] == ["rationale"]


# --------------------------------------------------------------------------
# compiles: the gate crate through real cargo, the six-tier matrix through the
# repository's own walk. Non-vacuity is a fixture that genuinely does not
# compile and a fixture whose matrix genuinely has a gap.
# --------------------------------------------------------------------------

def _stub_tool(tree, rel, body):
    path = tree / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _matrix_report(gaps=(), tiers=None, cases=("expr/add",)):
    """A `tools/conformance.py --json` report, in that tool's own shape."""
    tiers = tuple(tiers if tiers is not None
                  else ("python", "typescript", "rust", "java", "wasm", "go"))
    report = {"cases": [{"case": c, "tiers": {t: "ok" for t in tiers},
                         "emit_kind": {t: "ok" for t in tiers}} for c in cases],
              "frontend_rejected": [], "gaps": {}}
    for tier, case, deliberate in gaps:
        report["gaps"].setdefault(tier, []).append(
            {"case": case, "message": "no case for it", "deliberate": deliberate})
    return report


@pytest.fixture
def crate_repo(tiny_repo):
    """`tiny_repo` plus a real, minimal cargo crate at `crates/revl-gate`.

    A real crate rather than a mocked `cargo`: the component's claim is that
    rustc accepted this source, and a stubbed compiler would test the stub. It
    has no dependencies, so `--offline` needs no registry and the check is about
    a second, against twenty for the repository's own gate crate.
    """
    crate = tiny_repo / "crates" / "revl-gate"
    (crate / "src").mkdir(parents=True)
    (crate / "Cargo.toml").write_text(
        "[package]\nname = \"revl-gate\"\nversion = \"0.1.0\"\n"
        "edition = \"2021\"\n\n[workspace]\n")
    (crate / "src" / "lib.rs").write_text("pub fn admit(n: i64) -> i64 { n }\n")
    return tiny_repo


def _with_matrix(tree, report):
    _stub_tool(tree, "tools/conformance.py",
               "import json, sys\nprint(json.dumps(%r))\n" % (report,))
    return tree


def test_compiles_verifies_when_the_crate_builds_and_no_tier_has_a_real_gap(
        reward, crate_repo):
    _with_matrix(crate_repo, _matrix_report())
    verdict = reward.probe_compiles(
        _candidate(reward, crate_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is True, verdict.reason


def test_compiles_fails_on_source_that_does_not_compile(reward, crate_repo):
    """Non-vacuity for the crate half, with a real rustc refusal. This is the
    case a digest gate cannot see: `tools/build_gate_crate.py --check` compares
    BYTES, so a regenerated crate can be byte-correct and not compile."""
    _with_matrix(crate_repo, _matrix_report())
    (crate_repo / "crates" / "revl-gate" / "src" / "lib.rs").write_text(
        "pub fn admit(n: i64) -> i64 { n + }\n")
    verdict = reward.probe_compiles(
        _candidate(reward, crate_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is False
    assert "cargo check" in verdict.reason


def test_compiles_fails_on_a_real_emitter_gap_and_passes_a_deliberate_limit(
        reward, crate_repo):
    """The distinction the component turns on. An emitter that raised its own
    `EmitError` declared a tier limit; an emitter that crashed had no case for a
    construct it should express. `origin/main` carries eleven of the first and
    zero of the second, so the bar is zero REAL gaps, not zero refusals."""
    candidate = _candidate(reward, crate_repo, base="HEAD", scope=("**",))

    _with_matrix(crate_repo, _matrix_report(
        gaps=[("wasm", "expr/true division", True)]))
    assert reward.probe_compiles(candidate).verified is True

    _with_matrix(crate_repo, _matrix_report(
        gaps=[("wasm", "expr/true division", True),
              ("rust", "expr/add", False)]))
    verdict = reward.probe_compiles(candidate)
    assert verdict.verified is False
    assert "rust" in verdict.reason and "real emitter gap" in verdict.reason


def test_compiles_fails_when_a_tier_stops_being_walked(reward, crate_repo):
    """The matrix cannot shrink its way to green. A tier absent from a case row
    was not measured, and an unmeasured tier is not a passing one."""
    _with_matrix(crate_repo, _matrix_report(
        tiers=("python", "typescript", "rust", "java", "go")))
    verdict = reward.probe_compiles(
        _candidate(reward, crate_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is False
    assert "wasm" in verdict.reason


def test_compiles_fails_when_the_matrix_prints_nothing_readable(
        reward, crate_repo):
    _stub_tool(crate_repo, "tools/conformance.py", "print('not json')\n")
    verdict = reward.probe_compiles(
        _candidate(reward, crate_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is False


def test_compiles_verifies_on_this_tree(reward, real_candidate):
    """The real artifact: the real gate crate and the real six-tier walk. About
    twenty seconds, almost all of it a cold `cargo check`."""
    verdict = reward.probe_compiles(real_candidate)
    assert verdict.verified is True, verdict.reason


# --------------------------------------------------------------------------
# tests: the repository's own selection, actually run, with a collected count.
# --------------------------------------------------------------------------

def _with_selection(tree, full="0", pytest_targets="", backends="",
                    gates="", reason="stub"):
    block = "\n".join([f"FULL {full}", f"REASON {reason}",
                        f"PYTEST {pytest_targets}", f"BACKENDS {backends}",
                        f"GATES {gates}"])
    _stub_tool(tree, "tools/affected_tests.py", f"print({block!r})\n")
    return tree


@pytest.fixture
def suite_repo(tiny_repo):
    (tiny_repo / "tests").mkdir()
    (tiny_repo / "tests" / "test_green.py").write_text(
        "def test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n")
    return tiny_repo


def test_tests_verifies_when_the_selected_suite_runs_green(reward, suite_repo):
    _with_selection(suite_repo, pytest_targets="tests/test_green.py")
    verdict = reward.probe_tests(
        _candidate(reward, suite_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is True, verdict.reason
    assert "2 test(s) passed" in verdict.reason


def test_tests_fails_on_a_selected_suite_that_genuinely_fails(
        reward, suite_repo):
    """Non-vacuity: a real pytest process, a real assertion failure."""
    (suite_repo / "tests" / "test_red.py").write_text(
        "def test_c():\n    assert 1 == 2\n")
    _with_selection(suite_repo,
                    pytest_targets="tests/test_green.py tests/test_red.py")
    verdict = reward.probe_tests(
        _candidate(reward, suite_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is False
    assert "exited" in verdict.reason


def test_a_suite_that_collected_nothing_is_not_a_pass(reward, suite_repo):
    """The fail-open shape this probe exists to close. A pytest summary with no
    test count means zero tests ran, and a run of zero tests exits 0 whenever
    something else in the selection kept the exit status clean. The probe reads
    the COUNT, not only the status."""
    (suite_repo / "tests" / "test_all_skipped.py").write_text(
        "import pytest\n\n\n@pytest.mark.skip(reason='stub')\n"
        "def test_d():\n    assert True\n")
    _with_selection(suite_repo, pytest_targets="tests/test_all_skipped.py")
    verdict = reward.probe_tests(
        _candidate(reward, suite_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is False
    assert "collected nothing" in verdict.reason


def test_an_empty_selection_is_not_a_pass(reward, suite_repo):
    _with_selection(suite_repo, pytest_targets="", reason="nothing mapped")
    verdict = reward.probe_tests(
        _candidate(reward, suite_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is False
    assert "named no test at all" in verdict.reason


def test_a_full_selection_means_the_whole_tests_tree(reward, suite_repo):
    """FULL is the selector falling safe, and falling safe must not be read as
    "nothing to run"."""
    _with_selection(suite_repo, full="1", pytest_targets="",
                    reason="a core file changed")
    verdict = reward.probe_tests(
        _candidate(reward, suite_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is True, verdict.reason
    assert "FULL" in verdict.reason


def test_a_selected_backend_suite_is_added_to_the_run(reward, suite_repo):
    """`pytest tests/` does not contain the per-backend emit suites; they live
    outside `tests/` and run as their own CI jobs. A selection that names a tier
    and a probe that ran only `tests/` is the wave gap this repository has
    already paid for twice."""
    (suite_repo / "backends" / "go").mkdir(parents=True)
    (suite_repo / "backends" / "go" / "test_emit_go.py").write_text(
        "def test_go_golden():\n    assert True\n")
    _with_selection(suite_repo, pytest_targets="tests/test_green.py",
                    backends="go")
    verdict = reward.probe_tests(
        _candidate(reward, suite_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is True, verdict.reason
    assert "3 test(s) passed" in verdict.reason


def test_a_backend_whose_suite_this_probe_cannot_run_fails_it(
        reward, suite_repo):
    """`tools/pre_merge.sh` SKIPS the python and typescript backend suites when
    their toolchain is absent. A skip is not a pass, so a selection that names
    one fails the component by name instead."""
    _with_selection(suite_repo, pytest_targets="tests/test_green.py",
                    backends="typescript")
    verdict = reward.probe_tests(
        _candidate(reward, suite_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is False
    assert "typescript" in verdict.reason


def test_a_selector_that_cannot_run_fails_the_component(reward, suite_repo):
    _stub_tool(suite_repo, "tools/affected_tests.py", "import sys\nsys.exit(2)\n")
    verdict = reward.probe_tests(
        _candidate(reward, suite_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is False
    assert "selection could not be read" in verdict.reason


# --------------------------------------------------------------------------
# conformance: the two divergence registers, ratcheted against `base`. The
# component item 536 pointed at a tool that has never existed; these hold the
# read that replaced it.
# --------------------------------------------------------------------------

_TIERS = ("py", "ts", "rust", "java", "wasm", "go", "revl")


def _committed_block(cells):
    """The GUARANTEE-TIER-MATRIX block as `tools/conformance.py` writes it."""
    glyph = {"proved": "proved", "divergence": "**div**",
             "no reproducer": "no repro", "unimplemented": "unimpl"}
    lines = ["<!-- GUARANTEE-TIER-MATRIX:START -->",
             "",
             "| guarantee | " + " | ".join(_TIERS) + " | evidence |",
             "|---" * (len(_TIERS) + 2) + "|"]
    for code in sorted({c for c, _ in cells}):
        row = [glyph[cells[(code, tier)]] for tier in _TIERS]
        lines.append(f"| `{code}` | " + " | ".join(row) + " | [`src/x.py`](x) |")
    lines += ["", "| tier | proved | div | no repro | unimpl |",
              "|---|---|---|---|---|",
              "| py | 1 | 0 | 0 | 0 |", "",
              "<!-- GUARANTEE-TIER-MATRIX:END -->"]
    return "\n".join(lines) + "\n"


def _matrix_json(cells):
    rows = {}
    for (code, tier), verdict in cells.items():
        rows.setdefault(code, {})[tier] = {"verdict": verdict, "why": "stub"}
    return {"tiers": list(_TIERS),
            "rows": [{"code": code, "cells": tiers}
                     for code, tiers in sorted(rows.items())]}


def _cells(**overrides):
    out = {(code, tier): "proved"
           for code in ("G1", "G2") for tier in _TIERS}
    for key, verdict in overrides.items():
        code, _, tier = key.partition("_")
        out[(code, tier)] = verdict
    return out


@pytest.fixture
def matrix_repo(tiny_repo):
    """A repo whose committed `docs/conformance.md` carries a base matrix."""
    (tiny_repo / "docs").mkdir()
    (tiny_repo / "docs" / "conformance.md").write_text(
        "# conformance\n\n" + _committed_block(_cells()))
    _git(tiny_repo, "add", "-A")
    _git(tiny_repo, "commit", "-qm", "matrix")
    return tiny_repo


def _with_tier_guarantees(tree, cells, exit_code=0):
    _stub_tool(tree, "tools/tier_guarantees.py",
               "import json, sys\nprint(json.dumps(%r))\nsys.exit(%d)\n"
               % (_matrix_json(cells), exit_code))
    return tree


def test_conformance_verifies_when_no_cell_is_weaker_than_at_base(
        reward, matrix_repo):
    _with_tier_guarantees(matrix_repo, _cells())
    verdict = reward.probe_conformance(
        _candidate(reward, matrix_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is True, verdict.reason
    assert "none weaker" in verdict.reason


def test_a_guarantee_lost_on_one_tier_fails_conformance(reward, matrix_repo):
    """Non-vacuity, and the promotion-bar entry it serves: "no weakened
    refusal". A tier that was `proved` at base and is a recorded divergence at
    head has lost the guarantee, whatever else the candidate did."""
    _with_tier_guarantees(matrix_repo, _cells(G1_java="divergence"))
    verdict = reward.probe_conformance(
        _candidate(reward, matrix_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is False
    assert "G1 on java: proved -> divergence" in verdict.reason


def test_a_cell_getting_stronger_is_the_work_and_passes(reward, tiny_repo):
    """The ratchet has a direction. `unimplemented -> divergence` is a partial
    port arriving, which is progress; reading it as a regression would punish
    exactly the work this component is supposed to be indifferent to."""
    (tiny_repo / "docs").mkdir()
    (tiny_repo / "docs" / "conformance.md").write_text(
        _committed_block(_cells(G1_revl="unimplemented")))
    _git(tiny_repo, "add", "-A")
    _git(tiny_repo, "commit", "-qm", "matrix")
    _with_tier_guarantees(tiny_repo, _cells(G1_revl="divergence"))
    verdict = reward.probe_conformance(
        _candidate(reward, tiny_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is True, verdict.reason


def test_a_row_deleted_at_head_fails_conformance(reward, matrix_repo):
    """Deleting the row is the other way to make a cell stop being a
    divergence, and it is the one a subset check would miss."""
    cells = {k: v for k, v in _cells().items() if k[0] != "G2"}
    _with_tier_guarantees(matrix_repo, cells)
    verdict = reward.probe_conformance(
        _candidate(reward, matrix_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is False
    assert "the row is gone" in verdict.reason


def test_a_register_that_grew_without_a_decision_fails_conformance(
        reward, matrix_repo):
    """`tools/tier_guarantees.py` raises rather than dropping an unmapped
    `--check-tier-parity` subject or an unmapped `DIVERGENCES` entry. That exit
    status is what keeps the registers armed, so it has to fail the component
    rather than be read as an empty matrix."""
    _with_tier_guarantees(matrix_repo, _cells(), exit_code=1)
    verdict = reward.probe_conformance(
        _candidate(reward, matrix_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is False
    assert "exited 1" in verdict.reason


def test_conformance_fails_when_base_carries_no_matrix(reward, tiny_repo):
    (tiny_repo / "docs").mkdir()
    (tiny_repo / "docs" / "conformance.md").write_text("# nothing generated\n")
    _git(tiny_repo, "add", "-A")
    _git(tiny_repo, "commit", "-qm", "no matrix")
    _with_tier_guarantees(tiny_repo, _cells())
    verdict = reward.probe_conformance(
        _candidate(reward, tiny_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is False
    assert "GUARANTEE-TIER-MATRIX" in verdict.reason


def test_the_committed_block_parser_reads_the_real_one(reward):
    """The base side is parsed out of the real generated block, so the parser
    is held against the real artifact rather than against the fixture that
    mimics it."""
    ok, cells = reward._committed_matrix(
        (ROOT / "docs" / "conformance.md").read_text())
    assert ok, cells
    assert cells[("G1", "py")] == "proved"
    assert set(cells.values()) <= set(reward.CELL_STRENGTH)
    assert len({tier for _, tier in cells}) == 7


def test_conformance_verifies_on_this_tree(reward, real_candidate):
    """The real registers, measured live, against the real committed matrix."""
    verdict = reward.probe_conformance(real_candidate)
    assert verdict.verified is True, verdict.reason


# --------------------------------------------------------------------------
# formal: the ledger, and the two gates over it that need no Lean.
# --------------------------------------------------------------------------

_AXIOMS = "import RevL\n\n#print axioms RevL.G1.a\n#print axioms RevL.G2.b\n"
_TSV = ("# registry\n"
        "RevL.G1.a\tinstance\tRevL.G2.b\ta witness\n"
        "RevL.G2.b\tconcrete\t\ta computation\n")


@pytest.fixture
def formal_repo(tiny_repo):
    (tiny_repo / "formal" / "scripts").mkdir(parents=True)
    (tiny_repo / "formal" / "CheckAxioms.lean").write_text(_AXIOMS)
    (tiny_repo / "formal" / "scripts" / "nonvacuity.tsv").write_text(_TSV)
    for name in ("nonvacuity_gate.py", "layering_gate.py"):
        (tiny_repo / "formal" / "scripts" / name).write_text("print('clean')\n")
    _git(tiny_repo, "add", "-A")
    _git(tiny_repo, "commit", "-qm", "formal")
    return tiny_repo


def test_formal_verifies_when_the_ledger_did_not_shrink(reward, formal_repo):
    verdict = reward.probe_formal(
        _candidate(reward, formal_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is True, verdict.reason
    assert "2 registered theorem(s)" in verdict.reason


def test_a_removed_theorem_fails_formal(reward, formal_repo):
    """Non-vacuity, and item 536's negative bar entry "no reduced formal
    coverage". Deleting the theorem deletes the obligation, and a green suite
    says nothing about it."""
    (formal_repo / "formal" / "CheckAxioms.lean").write_text(
        "import RevL\n\n#print axioms RevL.G1.a\n")
    (formal_repo / "formal" / "scripts" / "nonvacuity.tsv").write_text(
        "RevL.G1.a\tinstance\tRevL.G2.b\ta witness\n")
    verdict = reward.probe_formal(
        _candidate(reward, formal_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is False
    assert "SHRANK" in verdict.reason and "RevL.G2.b" in verdict.reason


def test_an_added_theorem_is_the_work_and_passes_formal(reward, formal_repo):
    (formal_repo / "formal" / "CheckAxioms.lean").write_text(
        _AXIOMS + "#print axioms RevL.G3.c\n")
    (formal_repo / "formal" / "scripts" / "nonvacuity.tsv").write_text(
        _TSV + "RevL.G3.c\tinstance\tRevL.G2.b\ta new witness\n")
    verdict = reward.probe_formal(
        _candidate(reward, formal_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is True, verdict.reason
    assert "3 registered theorem(s)" in verdict.reason


def test_a_theorem_downgraded_to_contentless_fails_formal(reward, formal_repo):
    """The row survives and the content does not, so a set comparison alone
    would read this as unchanged. `contentless` is the registry's own word for
    "true by definition", and it is recorded as a FINDING, not a pass."""
    (formal_repo / "formal" / "scripts" / "nonvacuity.tsv").write_text(
        "RevL.G1.a\tcontentless\tRevL.G2.b\ttrue by definition now\n"
        "RevL.G2.b\tconcrete\t\ta computation\n")
    verdict = reward.probe_formal(
        _candidate(reward, formal_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is False
    assert "contentless" in verdict.reason and "RevL.G1.a" in verdict.reason


def test_a_failing_ledger_gate_fails_formal(reward, formal_repo):
    (formal_repo / "formal" / "scripts" / "nonvacuity_gate.py").write_text(
        "import sys\nprint('a witness is not registered')\nsys.exit(1)\n")
    verdict = reward.probe_formal(
        _candidate(reward, formal_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is False
    assert "nonvacuity_gate" in verdict.reason


def test_formal_is_the_control_for_the_conformance_fixture(
        reward, matrix_repo):
    """The control. `matrix_repo` has no `formal/` at all, so `formal` fails
    there for its own reason on BOTH sides of the conformance fault, and the
    conformance verdict above is located rather than global."""
    candidate = _candidate(reward, matrix_repo, base="HEAD", scope=("**",))
    _with_tier_guarantees(matrix_repo, _cells())
    clean = reward.probe_formal(candidate)
    _with_tier_guarantees(matrix_repo, _cells(G1_java="divergence"))
    dirty = reward.probe_formal(candidate)
    assert clean.verified is dirty.verified is False
    assert clean.reason == dirty.reason


def test_formal_verifies_on_this_tree(reward, real_candidate):
    verdict = reward.probe_formal(real_candidate)
    assert verdict.verified is True, verdict.reason


# --------------------------------------------------------------------------
# progress: the ninth conjunct, registered from tools/evolution_progress.py.
# --------------------------------------------------------------------------

def _with_progress(tree, verdict, deltas=(), exit_code=0):
    payload = {"progress": {"verdict": verdict, "deltas": list(deltas),
                            "improved": []}}
    _stub_tool(tree, "tools/evolution_progress.py",
               "import argparse, json, pathlib, sys\n"
               "ap = argparse.ArgumentParser()\n"
               "ap.add_argument('--tree')\nap.add_argument('--base')\n"
               "ap.add_argument('--json')\n"
               "a = ap.parse_args()\n"
               "pathlib.Path(a.json).write_text(json.dumps(%r))\n"
               "sys.exit(%d)\n" % (payload, exit_code))
    return tree


def test_progress_fails_closed_until_its_tool_is_in_the_tree(
        reward, tiny_repo):
    """PR #1258 (issue #1224) is not merged. The component must name the file
    rather than default to pass, which is the same answer every other component
    gives a missing tool."""
    verdict = reward.probe_progress(
        _candidate(reward, tiny_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is False
    assert "tools/evolution_progress.py" in verdict.reason


def test_the_progress_verdict_registers_with_no_adaptation(reward, tiny_repo):
    """The contract with issue #1224's lane: `tools/evolution_progress.py`
    writes `{"progress": {"verdict": {component, verdict, reason, evidence}}}`,
    the same four fields in the same vocabulary, so the ninth conjunct enters
    `all()` over `COMPONENTS` unchanged."""
    _with_progress(tiny_repo, {"component": "progress", "verdict": "verified",
                               "reason": "3 counter(s), none regressed",
                               "evidence": ["census-allowance: unchanged"]})
    verdict = reward.probe_progress(
        _candidate(reward, tiny_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is True, verdict.reason
    assert verdict.component == "progress"
    assert verdict.evidence == ("census-allowance: unchanged",)


def test_a_regressed_counter_fails_progress(reward, tiny_repo):
    _with_progress(tiny_repo,
                   {"component": "progress", "verdict": "failed",
                    "reason": "census-allowance regressed: 9 -> 10",
                    "evidence": []}, exit_code=1)
    verdict = reward.probe_progress(
        _candidate(reward, tiny_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is False
    assert "regressed" in verdict.reason


def test_a_progress_tool_that_writes_no_verdict_fails_the_component(
        reward, tiny_repo):
    _stub_tool(tiny_repo, "tools/evolution_progress.py", "print('nothing')\n")
    verdict = reward.probe_progress(
        _candidate(reward, tiny_repo, base="HEAD", scope=("**",)))
    assert verdict.verified is False
    assert "no progress verdict" in verdict.reason


def test_the_real_progress_tool_answers_in_this_vocabulary(reward, real_candidate):
    """Cross-module, and SKIPPED with a stated reason on a tree without the
    other lane's work: PR #1258 (issue #1224) is open, not merged, so
    `tools/evolution_progress.py` is not here. The stub contract above is what
    holds the interface until it lands; this is what checks the stub was right."""
    if not (ROOT / "tools" / "evolution_progress.py").exists():
        pytest.skip("tools/evolution_progress.py is not in this tree "
                    "(issue #1224 / PR #1258 is open, not merged)")
    verdict = reward.probe_progress(real_candidate)
    assert verdict.component == "progress"
    assert isinstance(verdict.verified, bool)
