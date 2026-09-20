"""Tests for `tools/evolution_controller.py` (roadmap item 520, issue #1194).

The three things these tests have to establish, in the repository's own terms:

  1. THE CONTROLLER REFUSES when a stage's evidence is missing. A controller
     that promotes on missing evidence is the fail-open shape, and this
     repository has measured ten separate checks that ran on every PR and could
     not fail.
  2. THE CONTROLLER PROMOTES on a complete proposal, so the refusals above are
     not a tool that refuses everything.
  3. THE CHECK IS NON-VACUOUS. `control_any_green` is a deliberately naive
     reducer written here in the test file: it promotes when any stage reports
     green, which is the shape a weighted lifecycle degenerates to. It returns
     PROMOTE on BOTH the clean tree and the authority-widened one. The real
     controller separates them. The difference is therefore caused by the
     barrier and not by the widened tree being obviously broken.

The trees are real git trees built in a tmp_path, because the authority stage
MEASURES the changed-file set with git and refuses a self-declared one. A test
that handed it a declared diff would be testing the path that cannot promote.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import evolution_controller as ec  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------- git fixtures

def _git(tree: Path, *args):
    proc = subprocess.run(
        ["git", "-C", str(tree)] + list(args),
        capture_output=True, text=True,
        env={"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
             "HOME": str(tree), "PATH": "/usr/bin:/bin:/usr/local/bin",
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def make_tree(root: Path, changed: dict) -> Path:
    """A git tree with one base commit and `changed` written on top of it.

    The base commit carries the files the changed set touches, so the diff
    against `HEAD~1` is exactly `changed.keys()` and nothing else.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    for name in sorted(changed):
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("base\n")
    (root / "README.md").write_text("base\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    for name, text in sorted(changed.items()):
        (root / name).write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "candidate")
    return root


EMPTY_DIFF = {axis: [] for axis in ec.AUTHORITY_AXES}


def proposal_record(tree: Path, **over) -> dict:
    """A COMPLETE proposal: every stage present, every one verified.

    Tests derive their cases by removing or spoiling exactly one thing, so the
    difference between a promotion and a refusal is always a single edit.
    """
    record = {
        "tree": str(tree),
        "base": "HEAD~1",
        "scope": ["src/**", "tests/**"],
        "granted": ["Clock"],
        "attempt": 1,
        "budget": 3,
        "stages": {
            "trigger": {"verified": True, "kind": "gap",
                        "gap": "selfhost/lower.rvl refuses a form the reference admits",
                        "artifact": "tools/gate_reference_census_baseline.json",
                        "reason": "a census divergence",
                        "evidence": ["tools/evolve_curriculum.py"]},
            "propose": {"verified": True, "reason": "candidate generated",
                        "evidence": ["candidate.rvl"]},
            "admit": {"verified": True,
                      "reason": "standalone self-extension compile admitted",
                      "evidence": ["Gate.propose: admitted"]},
            "authority": {"verified": True, "reason": "no authority moved",
                          "diff": dict(EMPTY_DIFF),
                          "evidence": ["capability-attenuation product"]},
            "shadow": {"verified": True, "reason": "held-out draw clean",
                       "evidence": ["tools/heldout_scoring.py: 200 cases"]},
            "canary": {"verified": True, "reason": "SLOs held at 1 percent",
                       "evidence": ["p99 41ms", "refusal rate 0.2 percent"]},
            "observe": {"retained": True, "blockers": [],
                        "components": [{"component": c} for c in
                                       ("compiles", "tests", "scope")]},
        },
    }
    record.update(over)
    return record


def run(record: dict) -> ec.LifecycleVerdict:
    return ec.decide(ec.load_proposal(record))


@pytest.fixture
def clean_tree(tmp_path) -> Path:
    return make_tree(tmp_path / "clean", {"src/svc.rvl": "candidate\n"})


# ------------------------------------------------------------ the happy path

def test_a_complete_proposal_is_promoted(clean_tree):
    verdict = run(proposal_record(clean_tree))
    assert verdict.decision == "PROMOTE", verdict.render()
    assert verdict.code == ""
    assert verdict.measurements_read is True
    assert [v.component for v in verdict.verdicts] == list(ec.STAGES)
    assert all(v.verified for v in verdict.verdicts)


def test_the_verdict_binds_proposal_evidence_and_decision(clean_tree):
    """Item 520's exit test: ONE recorded verdict, carrying all of it."""
    verdict = run(proposal_record(clean_tree)).as_dict()
    assert verdict["decision"] == "PROMOTE"
    assert verdict["base"] == "HEAD~1"
    assert verdict["scope"] == ["src/**", "tests/**"]
    assert verdict["attempt"] == 1 and verdict["budget"] == 3
    assert [s["component"] for s in verdict["stages"]] == list(ec.STAGES)
    # every stage cites the mechanism that supplies it
    assert set(verdict["supplied_by"]) == set(ec.STAGES)
    # and every stage carries its own evidence, not a shared blob
    for stage in verdict["stages"]:
        assert stage["evidence"], stage


# -------------------------------------------- missing evidence is a refusal

@pytest.mark.parametrize("stage", ec.PRECONDITIONS)
def test_a_skipped_precondition_refuses_by_name(clean_tree, stage):
    record = proposal_record(clean_tree)
    del record["stages"][stage]
    verdict = run(record)
    assert verdict.decision == "REFUSE"
    assert verdict.code == "STAGE_SKIPPED"
    assert verdict.refusing_stage == stage
    assert verdict.measurements_read is False


@pytest.mark.parametrize("stage", ec.PRECONDITIONS)
def test_a_precondition_with_no_evidence_refuses_by_name(clean_tree, stage):
    """The shape the item warns about: the record is PRESENT and says yes, and
    cites nothing. A verdict with no evidence is an assertion."""
    record = proposal_record(clean_tree)
    record["stages"][stage]["evidence"] = []
    verdict = run(record)
    assert verdict.decision == "REFUSE"
    assert verdict.code == "EVIDENCE_MISSING"
    assert verdict.refusing_stage == stage


def test_a_missing_measured_stage_is_not_a_pass(clean_tree):
    record = proposal_record(clean_tree)
    del record["stages"]["shadow"]
    verdict = run(record)
    assert verdict.decision != "PROMOTE"
    assert verdict.code == "STAGE_SKIPPED"
    assert verdict.by_name()["shadow"].verified is False


def test_every_stage_has_a_named_supplier():
    """No stage may exist without the mechanism that supplies it being named:
    item 520 requires each stage to cite one."""
    assert set(ec.SUPPLIED_BY) == set(ec.STAGES)
    for stage, text in ec.SUPPLIED_BY.items():
        assert text.strip(), stage


# ------------------------------------------- the barrier (item 543, #1222)

def widened_record(tree: Path, axis: str = "capability") -> dict:
    """PERFECT canary numbers, one widened reach. Issue #1222's exit test."""
    record = proposal_record(tree)
    record["stages"]["authority"]["diff"][axis] = ["fs.Write reached from Report"]
    record["stages"]["canary"] = {
        "verified": True, "reason": "SLOs excellent at 1 percent",
        "evidence": ["p99 18ms (base 41ms)", "refusal rate 0.0 percent",
                     "answer quality +4 percent"]}
    return record


def test_a_widened_reach_is_refused_despite_perfect_canary_numbers(clean_tree):
    verdict = run(widened_record(clean_tree))
    assert verdict.decision == "REFUSE"
    assert verdict.code == "AUTHORITY_WIDENED"
    assert verdict.refusing_stage == "authority"
    # the barrier held: the canary numbers were never read
    assert verdict.measurements_read is False
    canary = verdict.by_name()["canary"]
    assert canary.code == "NOT_REACHED"
    assert "not reached" in canary.reason
    assert "18ms" not in verdict.render()


def test_the_control_candidate_promotes_on_the_same_measured_evidence(clean_tree):
    """The other half of #1222's exit test. Same canary evidence, reach
    unchanged, and it promotes. Without this the refusal above could be a tool
    that refuses everything."""
    record = widened_record(clean_tree)
    record["stages"]["authority"]["diff"]["capability"] = []
    verdict = run(record)
    assert verdict.decision == "PROMOTE", verdict.render()


@pytest.mark.parametrize("axis", ec.AUTHORITY_AXES)
def test_every_authority_axis_gates_entry(clean_tree, axis):
    verdict = run(widened_record(clean_tree, axis))
    assert verdict.code == "AUTHORITY_WIDENED"
    assert axis in verdict.by_name()["authority"].reason


def test_an_absent_axis_counts_as_moved_not_as_empty(clean_tree):
    """An unmeasured axis that promotes is the fail-open shape. `retention` is
    the axis issue #1223 says the wave files on the wrong side, so a record
    that simply omits it must not read as clean."""
    record = proposal_record(clean_tree)
    del record["stages"]["authority"]["diff"]["retention"]
    verdict = run(record)
    assert verdict.decision == "REFUSE"
    assert verdict.code == "AUTHORITY_WIDENED"
    assert "retention" in verdict.by_name()["authority"].reason


def test_an_authority_record_with_no_diff_object_is_refused(clean_tree):
    record = proposal_record(clean_tree)
    del record["stages"]["authority"]["diff"]
    verdict = run(record)
    assert verdict.code == "AUTHORITY_WIDENED"


def test_measured_evidence_supplied_past_a_failed_precondition_is_a_finding(
        clean_tree):
    """A shadow run that happened although the authority diff refuses is not
    reassurance. It is a report that a stage ran without the authority to."""
    verdict = run(widened_record(clean_tree))
    assert any(f.startswith("OUT_OF_ORDER") for f in verdict.findings), \
        verdict.findings


# --------------------------------------------- the fence (the invariant)

def test_a_diff_reaching_the_controller_is_refused(tmp_path):
    """The invariant as a check: a proposal that edited the judge has not been
    judged. 'Unilaterally' is the operative word, so it is routed to a human
    rather than being judged by the rules it just rewrote."""
    tree = make_tree(tmp_path / "fence",
                     {"tools/evolution_controller.py": "changed\n"})
    verdict = run(proposal_record(tree))
    assert verdict.decision == "REFUSE"
    assert verdict.code == "FENCE_TOUCHED"
    assert verdict.measurements_read is False


def test_a_diff_reaching_the_controllers_own_tests_is_refused(tmp_path):
    tree = make_tree(tmp_path / "fencetests",
                     {"tests/test_evolution_controller.py": "changed\n"})
    assert run(proposal_record(tree)).code == "FENCE_TOUCHED"


@pytest.mark.parametrize("path", ec.AUTHORITY_FENCE)
def test_every_fenced_path_exists_in_this_tree(path):
    """A fence naming a file that does not exist protects nothing. This is the
    check that a rename cannot silently empty the fence."""
    assert (ROOT / path).exists(), path


# ----------------------------------------- the kernel (item 544, #1223)

@pytest.mark.parametrize("path", [
    "src/revl/admission.py",
    "src/revl/attest.py",
    "src/revl/taint.py",
    "src/revl/retention.py",
    "crates/revl-gate/src/session.rs",
    "formal/Oracle.lean",
    "tools/gate_reference_census_baseline.json",
])
def test_a_diff_reaching_the_admission_kernel_is_refused(tmp_path, path):
    tree = make_tree(tmp_path / ("kernel" + path.replace("/", "_")),
                     {path: "changed\n"})
    verdict = run(proposal_record(tree))
    assert verdict.decision == "REFUSE"
    assert verdict.code == "KERNEL_INTERSECTION"
    assert path in verdict.by_name()["authority"].reason


def test_the_kernel_refusal_cites_the_guarantee_it_defends(tmp_path):
    """Issue #1223's exit test asks for a NAMED refusal citing the guarantee."""
    tree = make_tree(tmp_path / "ker2", {"src/revl/retention.py": "changed\n"})
    reason = run(proposal_record(tree)).by_name()["authority"].reason
    assert "G8" in reason and "G-RETAIN" in reason


def test_the_control_component_with_a_disjoint_set_is_admitted(clean_tree):
    """The other half of #1223's exit test: a component whose declared set is
    disjoint from the kernel promotes on identical evidence."""
    assert run(proposal_record(clean_tree)).decision == "PROMOTE"


@pytest.mark.parametrize("path", ec.KERNEL_PATHS)
def test_every_enumerated_kernel_path_exists_in_this_tree(path):
    assert (ROOT / path).exists(), path


def test_a_path_that_only_prefixes_a_kernel_path_is_not_a_hit(tmp_path):
    """Segment-wise, so `formal` does not match `formalities.py`. A kernel
    check that over-matches is a check nobody can keep, and one that
    under-matches is a hole; this pins the boundary."""
    tree = make_tree(tmp_path / "prefix", {"formalities.py": "changed\n"})
    assert run(proposal_record(tree)).decision == "PROMOTE"
    assert ec.kernel_hits(["formal/Oracle.lean"]) == ["formal/Oracle.lean"]
    assert ec.kernel_hits(["formalities.py"]) == []


# -------------------------------------------------- the diff is measured

def test_a_declared_diff_that_understates_the_tree_is_refused(clean_tree):
    """A self-reported diff is the candidate's account of itself. If it
    disagrees with git, git wins and the disagreement is the refusal."""
    record = proposal_record(clean_tree)
    record["changed"] = ["docs/notes.md"]
    verdict = run(record)
    assert verdict.decision == "REFUSE"
    assert verdict.code == "DIFF_DISAGREES"
    assert "src/svc.rvl" in verdict.by_name()["authority"].reason


def test_a_tree_whose_diff_cannot_be_measured_cannot_promote(tmp_path):
    record = proposal_record(tmp_path / "not-a-git-tree")
    record["changed"] = ["src/svc.rvl"]
    verdict = run(record)
    assert verdict.decision == "REFUSE"
    assert verdict.code == "DIFF_UNVERIFIED"


def test_the_kernel_hit_is_found_even_when_the_candidate_hid_it(tmp_path):
    """The whole point of measuring rather than believing: a candidate that
    declares a clean diff while the tree edits the kernel is still caught."""
    tree = make_tree(tmp_path / "hidden", {"src/revl/attest.py": "changed\n"})
    record = proposal_record(tree)
    record["changed"] = ["src/revl/attest.py", "docs/notes.md"]
    verdict = run(record)
    assert verdict.decision == "REFUSE"
    assert verdict.code == "KERNEL_INTERSECTION"


# --------------------------------------------------- trigger and termination

def test_a_scheduled_trigger_is_refused(clean_tree):
    record = proposal_record(clean_tree)
    record["stages"]["trigger"]["kind"] = "schedule"
    verdict = run(record)
    assert verdict.decision == "REFUSE"
    assert verdict.code == "SCHEDULED_TRIGGER"


@pytest.mark.parametrize("field", ["gap", "artifact"])
def test_a_trigger_that_names_no_artifact_is_refused(clean_tree, field):
    record = proposal_record(clean_tree)
    record["stages"]["trigger"][field] = ""
    verdict = run(record)
    assert verdict.decision == "REFUSE"
    assert verdict.code == "NO_TRIGGER"


def test_a_proposal_past_its_budget_halts(clean_tree):
    record = proposal_record(clean_tree)
    record["attempt"] = 4
    verdict = run(record)
    assert verdict.decision == "REFUSE"
    assert verdict.code == "BUDGET_EXHAUSTED"
    assert verdict.refusing_stage == "propose"


@pytest.mark.parametrize("over", [{"budget": None}, {"budget": 0},
                                  {"attempt": None}, {"budget": True}])
def test_an_unbounded_retry_count_is_refused(clean_tree, over):
    record = proposal_record(clean_tree)
    record.update(over)
    assert run(record).code == "UNBOUNDED_RETRY"


def test_an_unscoped_proposal_is_refused(clean_tree):
    record = proposal_record(clean_tree)
    record["scope"] = []
    assert run(record).code == "UNSCOPED"


# ------------------------------------------------------- forbidden grant

@pytest.mark.parametrize("service", ec.DECIDER_SERVICES)
def test_a_granted_decider_service_is_refused(clean_tree, service):
    record = proposal_record(clean_tree)
    record["granted"] = ["Clock", service]
    verdict = run(record)
    assert verdict.decision == "REFUSE"
    assert verdict.code == "FORBIDDEN_GRANT"
    assert verdict.refusing_stage == "admit"


def test_the_decider_names_do_not_drift_from_the_gates(clean_tree):
    """`src/revl/gate.py` owns the rule; this module restates its name half.
    A name dropped there and not here (or vice versa) is the drift that makes a
    two-entry-point rule into a one-entry-point rule."""
    sys.path.insert(0, str(ROOT / "src"))
    from revl.gate import _DECIDER_SERVICES  # noqa: PLC0415
    assert set(_DECIDER_SERVICES) <= set(ec.DECIDER_SERVICES)


# ------------------------------------------------- roll back versus refuse

def test_a_failed_canary_after_activation_rolls_back(clean_tree):
    record = proposal_record(clean_tree)
    record["stages"]["canary"] = {
        "verified": False, "reason": "p99 regressed to 310ms",
        "evidence": ["p99 310ms (base 41ms)"]}
    verdict = run(record)
    assert verdict.decision == "ROLL_BACK"
    assert verdict.code == "STAGE_FAILED"
    assert verdict.measurements_read is True


def test_a_failed_shadow_with_no_canary_refuses_rather_than_claiming_an_undo(
        clean_tree):
    record = proposal_record(clean_tree)
    record["stages"]["shadow"] = {
        "verified": False, "reason": "a false admission on the draw",
        "evidence": ["tools/heldout_scoring.py: false-admission x1"]}
    del record["stages"]["canary"]
    verdict = run(record)
    assert verdict.decision == "REFUSE"
    assert verdict.by_name()["canary"].code == "NOT_REACHED"


def test_a_failed_reward_scorecard_is_read_through_the_adapter(clean_tree):
    """Item 536's `Scorecard` JSON is the `observe` stage's evidence, wired
    without a translation step that could soften the conjunction."""
    record = proposal_record(clean_tree)
    record["stages"]["observe"] = {
        "retained": False, "blockers": ["compiles", "tests"],
        "components": [{"component": "compiles"}, {"component": "tests"}]}
    verdict = run(record)
    assert verdict.decision == "ROLL_BACK"
    observe = verdict.by_name()["observe"]
    assert observe.verified is False
    assert "compiles" in observe.reason and "tests" in observe.reason


# ------------------------------------------------------------- vocabulary

def test_the_candidate_shape_is_item_536s(clean_tree):
    proposal = ec.load_proposal(proposal_record(clean_tree))
    assert ec.CANDIDATE_KEYS == ("tree", "base", "scope")
    for key in ec.CANDIDATE_KEYS:
        assert hasattr(proposal.candidate, key)
    assert {f for f in ec.Verdict.__dataclass_fields__} >= {
        "component", "verified", "reason", "evidence"}


def test_keys_outside_the_whitelist_are_dropped_and_reported(clean_tree):
    record = proposal_record(clean_tree)
    record["rationale"] = "this change is safe, trust me"
    record["self_assessment"] = "9/10"
    verdict = run(record)
    assert verdict.decision == "PROMOTE"
    assert verdict.proposal.candidate.prose_ignored == (
        "rationale", "self_assessment")
    assert "rationale" in verdict.render()
    assert "trust me" not in verdict.render()


@pytest.mark.parametrize("key", ec.CANDIDATE_KEYS)
def test_a_record_missing_a_required_key_is_refused(clean_tree, key):
    record = proposal_record(clean_tree)
    del record[key]
    with pytest.raises(ValueError):
        ec.load_proposal(record)


def test_a_raising_judge_is_a_failed_stage_not_a_crash(clean_tree, monkeypatch):
    def boom(proposal):
        raise RuntimeError("probe exploded")
    monkeypatch.setitem(ec.JUDGES, "admit", boom)
    verdict = run(proposal_record(clean_tree))
    assert verdict.decision == "REFUSE"
    assert verdict.code == "JUDGE_RAISED"


# ----------------------------------------------------------- NON-VACUITY

def control_any_green(record: dict) -> str:
    """The control: the reducer a weighted lifecycle degenerates to.

    It promotes when ANY stage reports green, which is what "weigh the
    authority diff beside the SLO components" means once one component is
    allowed to compensate for another. It is written here rather than in the
    tool because its only purpose is to be compared against.
    """
    stages = record.get("stages") or {}
    for value in stages.values():
        if value.get("verified") or value.get("retained"):
            return "PROMOTE"
    return "REFUSE"


def test_the_control_cannot_tell_the_two_trees_apart(clean_tree):
    clean = proposal_record(clean_tree)
    widened = widened_record(clean_tree)
    assert control_any_green(clean) == control_any_green(widened) == "PROMOTE"


def test_the_controller_can(clean_tree):
    """The pair that makes the refusal non-vacuous: identical measured
    evidence, one widened reach, and only the controller separates them."""
    assert run(proposal_record(clean_tree)).decision == "PROMOTE"
    assert run(widened_record(clean_tree)).decision == "REFUSE"


def test_the_control_also_passes_a_kernel_edit(tmp_path):
    tree = make_tree(tmp_path / "ctlkernel", {"src/revl/attest.py": "changed\n"})
    record = proposal_record(tree)
    assert control_any_green(record) == "PROMOTE"
    assert run(record).decision == "REFUSE"


# ------------------------------------------------------------------- the CLI

def test_the_cli_exit_status_distinguishes_the_three_decisions(
        clean_tree, tmp_path):
    cases = [
        (proposal_record(clean_tree), ec.PROMOTE, "PROMOTE"),
        (widened_record(clean_tree), ec.REFUSE, "REFUSE"),
    ]
    rolled = proposal_record(clean_tree)
    rolled["stages"]["canary"] = {"verified": False, "reason": "p99 regressed",
                                  "evidence": ["p99 310ms"]}
    cases.append((rolled, ec.ROLL_BACK, "ROLL_BACK"))

    for index, (record, status, decision) in enumerate(cases):
        src = tmp_path / f"proposal-{index}.json"
        out = tmp_path / f"verdict-{index}.json"
        src.write_text(json.dumps(record))
        assert ec.main(["--proposal", str(src), "--json", str(out)]) == status
        assert json.loads(out.read_text())["decision"] == decision


def test_an_unreadable_proposal_refuses(tmp_path):
    missing = tmp_path / "nope.json"
    assert ec.main(["--proposal", str(missing)]) == ec.REFUSE
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert ec.main(["--proposal", str(bad)]) == ec.REFUSE


def test_the_verdict_json_shows_that_measurements_were_not_read(
        clean_tree, tmp_path):
    src = tmp_path / "p.json"
    out = tmp_path / "v.json"
    src.write_text(json.dumps(widened_record(clean_tree)))
    assert ec.main(["--proposal", str(src), "--json", str(out)]) == ec.REFUSE
    verdict = json.loads(out.read_text())
    assert verdict["measurements_read"] is False
    assert verdict["code"] == "AUTHORITY_WIDENED"
    assert verdict["refusing_stage"] == "authority"
    measured = {s["component"]: s for s in verdict["stages"]}
    for stage in ec.MEASURED:
        assert measured[stage]["code"] == "NOT_REACHED"
