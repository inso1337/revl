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


def test_seven_of_eight_is_not_retained(reward):
    """The case a weighted score would have kept: 7/8 is 0.875, and 0.875 is
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
    assert set(reward.COMPONENTS) == {
        "compiles", "tests", "no-new-false-admits", "conformance",
        "artifact-stability", "formal", "scope", "documentation"}
    assert set(reward.PROBES) == set(reward.COMPONENTS)


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


def test_an_unimplemented_component_fails_rather_than_defaulting(reward):
    for name in ("compiles", "tests", "conformance", "formal"):
        verdict = reward.PROBES[name](_candidate(reward, "/nowhere"))
        assert verdict.verified is False
        assert "no probe implemented" in verdict.reason


def test_the_conformance_component_names_the_tool_that_does_not_exist(reward):
    """Item 536 maps this component to `tools/gate_verdict_parity.py`, which is
    not in the tree. Fail-closed means the component says so rather than being
    quietly dropped, and this test reds when the tool arrives, which is when the
    probe has to be written."""
    assert not (ROOT / "tools" / "gate_verdict_parity.py").exists()
    verdict = reward.PROBES["conformance"](_candidate(reward, "/nowhere"))
    assert verdict.verified is False
    assert "gate_verdict_parity.py" in verdict.reason


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
    ("docs/design/**", "docs/design/531-evolution-reward.md", True),
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
    assert card["prose_ignored"] == ["rationale"]
