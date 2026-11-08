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


def make_policy_tree(root: Path, before: str, after: str | None,
                     path: str = "boundary.policy") -> Path:
    """A git tree whose one changed document is a policy with known base text.

    `make_tree` writes `base` into every file's base commit, which is fine for
    a source diff and useless for a policy classification: the base text is what
    the classifier compares against. `after=None` deletes the document.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(before)
    (root / "README.md").write_text("base\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    if after is None:
        (root / path).unlink()
    else:
        (root / path).write_text(after)
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


def test_a_diff_that_changes_only_the_reward_is_refused(tmp_path):
    """Issue #2802's acceptance: the scorecard assembler was on no list.

    `tools/evolution_reward.py` holds `COMPONENTS` and the retention rule, so a
    proposal that edits it and is then judged by it has not been judged. Before
    issue #2802 this diff promoted.
    """
    tree = make_tree(tmp_path / "rewardfence",
                     {"tools/evolution_reward.py": "changed\n"})
    verdict = run(proposal_record(tree))
    assert verdict.decision == "REFUSE"
    assert verdict.code == "FENCE_TOUCHED"
    assert "tools/evolution_reward.py" in verdict.by_name()["authority"].reason


def test_the_controller_fence_is_a_view_of_the_evaluator_set():
    """The fence is derived, not restated: a path added to `EVALUATOR_PATHS` and
    tagged `authority` arrives here without a second list being edited. The AST
    test in `tests/test_evaluator_protocol.py` holds the assignment itself, so
    a literal copy of today's members does not pass either."""
    from revl import kernel_boundary as kb  # noqa: PLC0415

    assert tuple(ec.AUTHORITY_FENCE) == kb.evaluator_fence("authority")
    assert set(ec.KERNEL_PATHS) == set(kb.KERNEL_PATHS)
    for path in ("tools/evolution_reward.py", "tools/evolution_progress.py",
                 "tests/test_evolution_reward.py",
                 "tests/test_evolution_progress.py"):
        assert path in ec.AUTHORITY_FENCE, path


def test_the_verdict_carries_the_protocol_digest(clean_tree):
    """Item 520's one verdict now binds the protocol that produced it."""
    from revl import kernel_boundary as kb  # noqa: PLC0415

    blob = run(proposal_record(clean_tree)).as_dict()
    assert blob["protocol_digest"] == kb.protocol_digest(ROOT)
    assert blob["protocol_digest"].startswith("sha256:")


def test_a_controller_that_cannot_name_its_protocol_refuses(
        clean_tree, monkeypatch):
    """A broken checkout is a named refusal, not an import traceback, and not
    a verdict that omits the identity it could not read."""
    def boom(*_args, **_kwargs):
        raise RuntimeError("git is not available")

    monkeypatch.setattr(ec, "protocol_digest", boom)
    verdict = run(proposal_record(clean_tree))
    assert verdict.decision == "REFUSE"
    assert verdict.code == "PROTOCOL_UNAVAILABLE"
    assert verdict.measurements_read is False
    assert verdict.as_dict()["protocol_digest"] is None


def test_a_stage_record_from_another_protocol_is_refused(clean_tree):
    """A record from a different evaluator is not evidence about this one."""
    record = proposal_record(clean_tree)
    record["stages"]["authority"]["protocol_digest"] = "sha256:" + "0" * 64
    verdict = run(record)
    assert verdict.decision == "REFUSE"
    assert verdict.code == "PROTOCOL_MISMATCH"
    assert verdict.refusing_stage == "authority"
    assert ec.own_protocol_digest() in verdict.by_name()["authority"].reason


def test_a_stage_record_from_this_protocol_is_read(clean_tree):
    """The control for the case above: the same digest this checkout computes
    is not refused, so the check discriminates rather than refusing every
    record that names a protocol."""
    record = proposal_record(clean_tree)
    record["stages"]["shadow"]["protocol_digest"] = ec.own_protocol_digest()
    assert run(record).decision == "PROMOTE"


def test_the_scorecard_adapter_carries_the_protocol_digest_through(clean_tree):
    """`_normalise` rebuilds the observe record; it must not drop the field the
    mismatch check reads, or a scorecard from another protocol would pass."""
    record = proposal_record(clean_tree)
    record["stages"]["observe"] = {
        "retained": True, "blockers": [], "components": [],
        "protocol_digest": "sha256:" + "0" * 64,
    }
    verdict = run(record)
    assert verdict.code == "PROTOCOL_MISMATCH"
    assert verdict.refusing_stage == "observe"


def test_a_scorecard_whose_protocol_is_null_is_refused(clean_tree):
    """A scorer that could not name its protocol is not a pass.

    `evolution_reward.score()` writes `protocol_digest: null` when its own
    checkout cannot be read (a missing git, a missing member). The adapter used
    to treat that null like an ABSENT key, so the record was read and could
    promote; the held-out scorer refuses the same situation, so one of the two
    was failing open.
    """
    record = proposal_record(clean_tree)
    record["stages"]["observe"] = {
        "retained": True, "blockers": [],
        "components": [{"component": "tests"}],
        "protocol_digest": None,
    }
    verdict = run(record)
    assert verdict.code == "PROTOCOL_MISMATCH"
    assert verdict.refusing_stage == "observe"
    assert "null" in verdict.by_name()["observe"].reason


def test_a_scorecard_from_this_protocol_promotes(clean_tree):
    """The control for the two cases above: a present digest equal to this
    checkout's is read, so the adapter did not turn the check into a refusal of
    every scorecard."""
    record = proposal_record(clean_tree)
    record["stages"]["observe"] = {
        "retained": True, "blockers": [],
        "components": [{"component": "tests"}],
        "protocol_digest": ec.own_protocol_digest(),
    }
    assert run(record).decision == "PROMOTE"


# ----------------------------------------- the kernel (item 544, #1223)

@pytest.mark.parametrize("path", [
    "src/revl/admission.py",
    "src/revl/attest.py",
    "src/revl/taint.py",
    "src/revl/retention.py",
    "crates/revl-gate/src/session.rs",
    "formal/harness/Oracle.lean",
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
    assert ec.kernel_hits(["formal/RevL.lean"]) == ["formal/RevL.lean"]
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


def test_the_stage_verdict_inherits_item_536s_rather_than_restating_it():
    """Issue #1336. This was a third hand-kept copy of item 536's four verdict
    fields whose docstring said they were item 536's. They were not: it had
    already grown `code`, and the mirror gate groups by exact equality, so it
    paired the two copies that agreed and never looked at this one.

    The relation is now structural in the direction that matters. A field or
    key item 536 adds or renames arrives here on the next import; only `code`
    is declared here, and only once."""
    import evolution_reward as er  # noqa: PLC0415

    assert issubclass(ec.Verdict, er.Verdict)
    # `code` is the ONE difference, and it is the lifecycle's question: issue
    # #1222 asks for a refusal BY NAME, and item 536's reward has no codes.
    own = set(ec.Verdict.__dataclass_fields__) - set(er.Verdict.__dataclass_fields__)
    assert own == {"code"}
    stage = ec.failed("authority", "AUTHORITY_WIDENED", "capabilities grew",
                      ("docs/capability-attenuation.md",))
    component = er.Verdict("authority", False, "capabilities grew",
                           ("docs/capability-attenuation.md",))
    serialised = stage.as_dict()
    assert list(serialised) == list(component.as_dict()) + ["code"]
    assert {k: serialised[k] for k in component.as_dict()} == component.as_dict()
    assert serialised["code"] == "AUTHORITY_WIDENED"


def test_the_inheritance_holds_when_item_536s_tests_ran_first(tmp_path):
    """The test above in the order that used to break it: a
    `tests/test_evolution_reward.py` test first, in the same session.

    That file loaded `tools/evolution_reward.py` by path and REPLACED the
    `sys.modules` entry this module's import had already made, so
    `ec.Verdict` subclassed one `Verdict` class and `import evolution_reward`
    returned another. Run in a subprocess so the order is fixed here rather
    than left to collection."""
    first = ("tests/test_evolution_reward.py::"
             "test_retention_requires_every_component")
    then = ("tests/test_evolution_controller.py::"
            "test_the_stage_verdict_inherits_item_536s_rather_than_restating_it")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-p", "no:randomly", f"--basetemp={tmp_path / 'run'}", first, then],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    assert "2 passed" in proc.stdout, proc.stdout[-3000:] + proc.stderr[-2000:]


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
        assert json.loads(out.read_text(encoding="utf-8"))["decision"] == decision


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
    verdict = json.loads(out.read_text(encoding="utf-8"))
    assert verdict["measurements_read"] is False
    assert verdict["code"] == "AUTHORITY_WIDENED"
    assert verdict["refusing_stage"] == "authority"
    measured = {s["component"]: s for s in verdict["stages"]}
    for stage in ec.MEASURED:
        assert measured[stage]["code"] == "NOT_REACHED"


# ------------------------------- the change classifier (issue #2752, item 540)

_POLICY_BASE = (
    "wasm components confined\n"
    "wasm component limits fuel = 1000, memory = 64MiB, wall = 10s\n"
    "capability net requires approval ttl 5m\n"
)


def _without_approval(policy: str) -> str:
    return "\n".join(line for line in policy.splitlines()
                     if "requires approval" not in line) + "\n"


def test_the_classifier_calls_a_threshold_change_safe_adaptive():
    """The first of the two classes: a threshold moves, no approval does."""
    result = ec.classify_policy_change(
        _POLICY_BASE, _POLICY_BASE.replace("fuel = 1000", "fuel = 5000"))
    assert result.kind == ec.SAFE_ADAPTIVE
    assert result.kind != ec.AUTHORITY_EVOLVING
    assert "4" in result.surface


def test_the_classifier_calls_a_dropped_approval_rule_authority_evolving():
    """The second class: the rule the operator wrote is gone."""
    result = ec.classify_policy_change(_POLICY_BASE, _without_approval(_POLICY_BASE))
    assert result.kind == ec.AUTHORITY_EVOLVING
    assert "4" in result.surface
    assert "net" in result.reason


def test_a_candidate_that_removes_an_approval_requirement_is_refused(tmp_path):
    """Issue #2752's exit test: a NAMED refusal on the diff.

    Before this slice the change moved the deploy chain's `policy` facet and a
    model record's `policy_digest`, and both are detection by a verifier who
    already holds the old value. Nothing refused it before activation.
    """
    tree = make_policy_tree(tmp_path / "approval", _POLICY_BASE,
                            _without_approval(_POLICY_BASE))
    verdict = run(proposal_record(tree))
    assert verdict.decision == "REFUSE"
    assert verdict.code == "APPROVAL_WEAKENED"
    assert verdict.measurements_read is False
    assert verdict.refusing_stage == "authority"
    reason = verdict.by_name()["authority"].reason
    assert "surface 4" in reason.lower()
    assert "net requires approval" in reason


def test_a_candidate_that_only_moves_a_threshold_promotes(tmp_path):
    """The classifier is not a refusal for every policy edit: a safe-adaptive
    change on the same document promotes on identical evidence."""
    tree = make_policy_tree(
        tmp_path / "threshold", _POLICY_BASE,
        _POLICY_BASE.replace("fuel = 1000", "fuel = 5000"))
    assert run(proposal_record(tree)).decision == "PROMOTE"


def test_a_longer_approval_ttl_is_authority_evolving():
    result = ec.classify_policy_change(
        _POLICY_BASE, _POLICY_BASE.replace("ttl 5m", "ttl 5h"))
    assert result.kind == ec.AUTHORITY_EVOLVING
    assert "ttl" in result.reason


def test_adding_an_approval_rule_is_safe_adaptive():
    result = ec.classify_policy_change(
        _POLICY_BASE, _POLICY_BASE + "capability pay requires approval\n")
    assert result.kind == ec.SAFE_ADAPTIVE


def test_a_candidate_that_edits_the_policy_module_is_refused(tmp_path):
    """The parser that gives the classifier its vocabulary is an authority.

    `src/revl/policy.py` is in neither `KERNEL_PATHS` nor `RULESET_MODULES`,
    which is the design note's finding for surface 4: a candidate could change
    what `requires approval` means without touching a policy document. The
    classifier refuses it on the diff.
    """
    tree = make_tree(tmp_path / "policymodule", {"src/revl/policy.py": "changed\n"})
    verdict = run(proposal_record(tree))
    assert verdict.decision == "REFUSE"
    assert verdict.code == "APPROVAL_WEAKENED"
    assert "policy.py" in verdict.by_name()["authority"].reason


def test_a_deleted_policy_document_is_refused(tmp_path):
    """A deleted document keeps nothing: the whole rule set went with it."""
    tree = make_policy_tree(tmp_path / "deleted", _POLICY_BASE, None)
    verdict = run(proposal_record(tree))
    assert verdict.decision == "REFUSE"
    assert verdict.code == "APPROVAL_WEAKENED"
    assert "boundary.policy" in verdict.by_name()["authority"].reason


def test_a_renamed_policy_that_drops_a_rule_is_refused(tmp_path):
    """`git diff --name-only` detects a rename and reports only the new path.

    Left on, that hides the old path from the classifier: the candidate looks
    like an added policy and the dropped rule is never seen. The measured diff
    is read with `--no-renames`, so a rename is a delete plus an add and the
    deleted document is classified.
    """
    root = tmp_path / "renamed"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "boundary.policy").write_text("capability net requires approval\n")
    (root / "README.md").write_text("base\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "mv", "boundary.policy", "net.policy")
    (root / "net.policy").write_text("# the approval requirement is gone\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "candidate")
    verdict = run(proposal_record(root))
    assert verdict.decision == "REFUSE"
    assert verdict.code == "APPROVAL_WEAKENED"
    assert "boundary.policy" in verdict.by_name()["authority"].reason


def test_a_removed_approval_rule_in_a_json_policy_is_refused(tmp_path):
    """The policy file has a JSON form, and the classifier reads it too."""
    tree = make_policy_tree(
        tmp_path / "json",
        '{"approvals": [{"capability": "net", "require": 2, '
        '"of": ["alice", "bob"]}]}\n',
        "{}\n", path="boundary.json")
    verdict = run(proposal_record(tree))
    assert verdict.decision == "REFUSE"
    assert verdict.code == "APPROVAL_WEAKENED"
    assert "boundary.json" in verdict.by_name()["authority"].reason


def test_a_lower_quorum_is_authority_evolving():
    base = ("capability net requires approval require 2 of {alice, bob}\n")
    after = ("capability net requires approval require 1 of {alice, bob}\n")
    result = ec.classify_policy_change(base, after)
    assert result.kind == ec.AUTHORITY_EVOLVING
    assert "require 2" in result.reason


def test_a_changed_approver_set_is_authority_evolving():
    base = ("capability net requires approval require 2 of {alice, bob}\n")
    after = ("capability net requires approval require 2 of {alice, carol}\n")
    assert ec.classify_policy_change(base, after).kind == ec.AUTHORITY_EVOLVING


def test_reordering_the_same_approvers_is_safe_adaptive():
    """`{a, b}` and `{b, a}` name the same quorum; order carries no meaning."""
    base = ("capability net requires approval require 2 of {alice, bob}\n")
    after = ("capability net requires approval require 2 of {bob, alice}\n")
    assert ec.classify_policy_change(base, after).kind == ec.SAFE_ADAPTIVE


def test_reordering_duplicate_patterns_is_authority_evolving():
    """`approval_rule_for` returns the FIRST covering rule, so moving a weaker
    rule ahead of a stronger one weakens the effective approval even though the
    stronger rule is still in the file."""
    base = ("capability net requires approval require 2 of {alice, bob}\n"
            "capability net requires approval\n")
    after = ("capability net requires approval\n"
             "capability net requires approval require 2 of {alice, bob}\n")
    result = ec.classify_policy_change(base, after)
    assert result.kind == ec.AUTHORITY_EVOLVING
    assert "require 2" in result.reason
