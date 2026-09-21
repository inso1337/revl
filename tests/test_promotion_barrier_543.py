"""The promotion barrier as a general rule (item 543, issue #1222).

The item's property is that the authority diff gates ENTRY to shadow and
canary rather than being weighed beside their output. Two modules already
implement that for their own surface and both are correct, so what this file
tests is the part neither of them claims: that the rule is GENERAL.

Four things, and each one fails on a tree without this change:

  1. `check_path` refuses the wrong shape BY NAME, and the wrong shape it is
     handed is the item's own text: the nine-stage lifecycle issue #1222
     quotes, which "lists the capability and reachability diff as one stage
     among those nine". A rule that only ever sees the right shape is a check
     that cannot fail, and this repository has measured eleven of those.
  2. The sweep finds a promotion path nobody registered, so a third path added
     tomorrow is held to the rule instead of being trusted to have read it.
  3. The two landed implementations are BOUND to their registry entries: the
     entries are compared against the modules' own stage tuples, so moving
     `authority` after `shadow` in either one reds here.
  4. `revl canary` refuses a candidate with a clean replay comparison and one
     widened reach, and promotes the control whose reach is unchanged on the
     same evidence. That is the item's exit test, on the promotion path that
     had no barrier at all.

THE FAILURE DIRECTION is fail-closed throughout, and the tests assert it in
the direction that matters: an axis missing from a diff is MOVED, not empty.
Reading a missing authority diff as an empty one is the fail-open shape and is
the whole bug.
"""

import ast
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from revl import promotion_barrier as pb  # noqa: E402
from revl.compiler import compile_files  # noqa: E402
from revl.mcp import canary  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")
BASELINE = os.path.join(FIX, "canary_tenants.rvl")
CONTROL = os.path.join(FIX, "canary_candidate_same.rvl")
WIDER = os.path.join(FIX, "canary_candidate_wider_reach.rvl")


@pytest.fixture(scope="module")
def baseline_ir():
    return compile_files([BASELINE])


# --------------------------------------------------------------- the axes

def test_the_two_vocabularies_are_five_names_that_agree_on_two():
    """The measurement that makes this module necessary rather than tidy.

    `tools/evolution_controller.py` spells its axes `capability, taint, budget,
    realm, retention`; `src/revl/shadow_promotion.py` spells the same five
    `budget, capabilities, origins, residence, retention`. Two names in common.
    An authority diff produced for one is unreadable by the other, and the
    direction it fails in is closed (each reads the other's three as
    unmeasured, which refuses) rather than open, so nothing was broken. It is
    still not composition.
    """
    controller = ("capability", "taint", "budget", "realm", "retention")
    shadow = ("budget", "capabilities", "origins", "residence", "retention")
    assert len(set(controller) & set(shadow)) == 2
    assert sorted(set(controller) & set(shadow)) == ["budget", "retention"]
    # both normalise onto the one canonical set
    assert {pb.canonical_axis(a) for a in controller} == set(pb.AUTHORITY_AXES)
    assert {pb.canonical_axis(a) for a in shadow} == set(pb.AUTHORITY_AXES)


def test_an_unknown_axis_is_refused_not_mapped_to_a_neighbour():
    assert pb.canonical_axis("capacity") is None
    assert pb.canonical_axis("") is None
    assert pb.canonical_axis(None) is None
    assert pb.canonical_axis(7) is None
    canonical, unknown = pb.normalise_diff({"capabilities": [], "capacity": []})
    assert canonical == {"capability": []}
    assert unknown == ["capacity"]


# ------------------------------------------------- the fail-closed reading

def test_an_absent_axis_has_moved_and_is_not_read_as_empty():
    """The fail-open shape, refused. A diff naming four axes empty and omitting
    the fifth is not a clean diff; the fifth was never measured."""
    four = {axis: [] for axis in pb.AUTHORITY_AXES if axis != "taint"}
    moved, why = pb.authority_moved(four)
    assert moved == ["taint"]
    assert "unmeasured" in why
    # and the control: all five present and empty is still
    assert pb.authority_moved({a: [] for a in pb.AUTHORITY_AXES})[0] == []


@pytest.mark.parametrize("diff", [
    None, [], "clean", 0,
    {"capability": None},
    {"capability": "none"},
])
def test_a_diff_that_is_not_a_measurement_moves_every_axis_it_covers(diff):
    moved, _ = pb.authority_moved(diff)
    assert moved, f"{diff!r} was read as a clean authority diff"


def test_a_nonempty_axis_moves_in_either_spelling():
    assert pb.authority_moved(
        {**{a: [] for a in pb.AUTHORITY_AXES},
         "capabilities": ["host:C:x"]})[0] == ["capability"]


# --------------------------------------------- the general rule: check_path

def test_the_items_own_wrong_lifecycle_is_refused_by_name():
    """Issue #1222's text: the proposed lifecycle is "observe, diagnose,
    propose, compile, admission-check, shadow-test, canary, measure, promote or
    roll back" and "lists the capability and reachability diff as one stage
    among those nine". Handed exactly that, the rule refuses it and says why.
    """
    wrong = pb.PromotionPath(
        module="<the lifecycle issue #1222 describes>",
        stages=("observe", "diagnose", "propose", "compile", "admission-check",
                "shadow-test", "canary", "measure", "authority"),
        measured=("shadow-test", "canary", "measure"),
        authority="authority",
        covers=pb.AUTHORITY_AXES,
    )
    refusal = pb.check_path(wrong)
    assert refusal is not None
    assert refusal.link == pb.AUTHORITY_AFTER_MEASUREMENT
    assert "canary, measure, shadow-test" in refusal.reason
    assert "gates ENTRY" in refusal.reason

    # the control: the SAME nine stages with the authority diff moved in front
    # of the measured three. Nothing else changes, and it is admitted.
    right = pb.PromotionPath(
        module="<the same lifecycle, reordered>",
        stages=("observe", "diagnose", "propose", "compile", "admission-check",
                "authority", "shadow-test", "canary", "measure"),
        measured=("shadow-test", "canary", "measure"),
        authority="authority",
        covers=pb.AUTHORITY_AXES,
    )
    assert pb.check_path(right) is None


def test_a_path_that_declares_no_authority_stage_is_refused():
    refusal = pb.check_path(pb.PromotionPath(
        module="<a third path added tomorrow>",
        stages=("admit", "canary", "observe"),
        measured=("canary", "observe"),
        authority="",
        covers=pb.AUTHORITY_AXES))
    assert refusal is not None and refusal.link == pb.NO_AUTHORITY_STAGE
    assert "refused rather than defaulted" in refusal.reason


def test_a_path_that_names_a_stage_it_does_not_walk_is_refused():
    refusal = pb.check_path(pb.PromotionPath(
        module="<a path whose barrier is prose>",
        stages=("admit", "canary"), measured=("canary",),
        authority="authority", covers=pb.AUTHORITY_AXES))
    assert refusal is not None and refusal.link == pb.NO_AUTHORITY_STAGE


def test_a_path_that_reads_authority_as_a_measurement_is_refused():
    refusal = pb.check_path(pb.PromotionPath(
        module="<a path that scores the authority diff>",
        stages=("admit", "authority", "canary"),
        measured=("authority", "canary"),
        authority="authority", covers=pb.AUTHORITY_AXES))
    assert refusal is not None and refusal.link == pb.AUTHORITY_IS_MEASURED


def test_a_path_with_no_measured_stage_may_not_claim_a_barrier():
    """The anti-vacuity clause, applied to the rule itself. A path declaring no
    measured stage would pass the ordering check whatever its order was, so
    claiming the barrier on it is a check that cannot fail."""
    refusal = pb.check_path(pb.PromotionPath(
        module="<a path with nothing to hold back>",
        stages=("admit", "authority"), measured=(),
        authority="authority", covers=pb.AUTHORITY_AXES))
    assert refusal is not None and refusal.link == pb.NO_MEASURED_STAGE


def test_an_axis_may_not_leave_a_paths_account_by_being_forgotten():
    refusal = pb.check_axes(pb.PromotionPath(
        module="<a path measuring four and calling it five>",
        stages=("authority", "canary"), measured=("canary",),
        authority="authority",
        covers=("capability", "taint", "budget", "realm")))
    assert refusal is not None and refusal.link == pb.AXIS_DROPPED
    assert "retention" in refusal.reason

    named = pb.PromotionPath(
        module="<a path that names what it does not cover>",
        stages=("authority", "canary"), measured=("canary",),
        authority="authority",
        covers=("capability", "taint", "budget", "realm"),
        uncovered=("retention",))
    assert pb.check_axes(named) is None


def test_every_registered_path_has_the_barrier_and_accounts_for_every_axis():
    assert pb.REGISTRY, "the registry is empty, so this file proves nothing"
    for path in pb.REGISTRY:
        assert pb.check_path(path) is None, f"{path.module}: {pb.check_path(path)}"
        assert pb.check_axes(path) is None, f"{path.module}: {pb.check_axes(path)}"


# ------------------------------------------------------------- the sweep

def test_no_promotion_path_in_the_tree_is_unregistered():
    """The answer to "if a third promotion path is added tomorrow, does
    anything force it to have the barrier?". This is that thing."""
    stray = pb.discover(ROOT)
    assert stray == [], pb.unregistered_refusal(stray).reason


def test_the_sweep_finds_an_unregistered_path_rather_than_passing_on_an_empty_set(tmp_path):
    """Non-vacuity for the sweep. A module that renders a promotion verdict and
    is not in the registry is found, and the refusal names it."""
    pkg = tmp_path / "src" / "revl"
    pkg.mkdir(parents=True)
    (tmp_path / "tools").mkdir()
    (pkg / "brand_new_promoter.py").write_text(
        'def decide(slo):\n'
        '    return "promote" if slo["p99"] < 20 else "hold"\n',
        encoding="utf-8")
    (pkg / "innocent.py").write_text(
        '"""Talks about promotion and decides none."""\n'
        'NOTE = "the promotion barrier is documented in promotion_barrier.py"\n',
        encoding="utf-8")
    stray = pb.discover(tmp_path)
    assert stray == ["src/revl/brand_new_promoter.py"]
    refusal = pb.unregistered_refusal(stray)
    assert refusal.link == pb.PATH_UNREGISTERED
    assert "brand_new_promoter" in refusal.reason


def test_the_registry_covers_the_modules_the_sweep_selects_on_this_tree():
    """The two halves agree: every registered module that exists is one the
    detector would have selected, so the registry is not a list of modules the
    sweep could never have found."""
    seen = 0
    for path in pb.REGISTRY:
        full = os.path.join(ROOT, *path.module.split("/"))
        if not os.path.exists(full):
            continue
        seen += 1
        with open(full, encoding="utf-8") as handle:
            assert pb.renders_promotion(handle.read()), \
                f"{path.module} is registered and the sweep would not find it"
    assert seen >= 2, "fewer than two registered paths exist on this tree"


# ---------------------------------------- binding: the rule holds the modules

def test_the_controller_module_matches_its_registry_entry():
    """The coupling. `tools/evolution_controller.py`'s own PRECONDITIONS and
    MEASURED tuples are read off the module and compared to what the registry
    declares, so a change to its stage order that loses the barrier reds here
    rather than being noticed by nobody."""
    path = pb.BY_MODULE["tools/evolution_controller.py"]
    full = os.path.join(ROOT, "tools", "evolution_controller.py")
    consts = _module_tuples(full)
    assert consts["PRECONDITIONS"] + consts["MEASURED"] == path.stages
    assert consts["MEASURED"] == path.measured
    assert path.authority in consts["PRECONDITIONS"]
    # The axis set is no longer a literal to read off this module. Issue #1332
    # collapsed the controller's own `AUTHORITY_AXES` tuple onto this module's,
    # so the set it measures IS the registry's rather than being compared to
    # it. What is left to hold is that the import is really what stands there:
    # a fresh tuple would restore the drift surface the collapse removed.
    assert "AUTHORITY_AXES" not in consts, (
        "tools/evolution_controller.py declares its own AUTHORITY_AXES tuple "
        "again; it must import promotion_barrier's")
    assert _imports_name(full, "revl.promotion_barrier", "AUTHORITY_AXES"), (
        "tools/evolution_controller.py no longer imports AUTHORITY_AXES from "
        "revl.promotion_barrier")
    assert {pb.canonical_axis(a) for a in pb.AUTHORITY_AXES} \
        == {pb.canonical_axis(a) for a in path.covers}


def test_the_shadow_promotion_module_matches_its_registry_entry():
    """PR #1250 is unmerged at the time this landed, so this SKIPS with a
    stated reason on a tree without it and binds when it arrives."""
    full = os.path.join(ROOT, "src", "revl", "shadow_promotion.py")
    if not os.path.exists(full):
        pytest.skip("src/revl/shadow_promotion.py (item 518, PR #1250) is not "
                    "on this tree; its registry entry is checked for shape by "
                    "test_every_registered_path_has_the_barrier and bound to "
                    "the module's own stage tuples once the PR lands")
    path = pb.BY_MODULE["src/revl/shadow_promotion.py"]
    consts = _module_tuples(full)
    assert path.authority in consts["STAGES"]
    assert consts["STAGES"].index(path.authority) < len(consts["STAGES"])
    assert {pb.canonical_axis(a) for a in consts["AUTHORITY_AXES"]} \
        == {pb.canonical_axis(a) for a in path.covers}


def _imports_name(path: str, module: str, name: str) -> bool:
    """Does this module do `from <module> import <name>`, read without
    importing it? The same AST read as `_module_tuples`, for the constants
    that are imported rather than restated."""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    return any(isinstance(node, ast.ImportFrom) and node.module == module
               and any(alias.name == name for alias in node.names)
               for node in ast.walk(tree))


def _module_tuples(path: str) -> dict:
    """Top-level string-tuple constants of a module, read without importing it.

    `tools/` is not a package and importing the controller for its constants
    would run its argument parser's module-level work; an AST read is enough
    for tuples of string literals and cannot execute anything.
    """
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    out: dict = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        value = node.value
        if isinstance(value, ast.Tuple) and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str)
                for e in value.elts):
            out[target.id] = tuple(e.value for e in value.elts)
        elif isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
            left = out.get(getattr(value.left, "id", None))
            right = out.get(getattr(value.right, "id", None))
            if left is not None and right is not None:
                out[target.id] = left + right
    return out


# ------------------------------------------- the exit test, on `revl canary`

def test_a_clean_comparison_does_not_buy_a_widened_reach(baseline_ir):
    """Issue #1222's exit test, on the promotion path that had no barrier.

    The candidate's recorded world is identical to the baseline's step for
    step, which is the strongest possible behavioural result, and its G8
    boundary surface has one more crossing. It is refused BY NAME on the
    capability axis, and the divergence comparison is not merely outweighed:
    it is never run, so the report carries no divergence at all.
    """
    report = canary.run_canary(baseline_ir, candidate_files=[WIDER],
                               realm="tenant_a", over_the_transport=False)
    assert report["ok"] and report["admitted"]
    assert report["recommendation"] == "refuse"
    assert report["authority"]["ok"] is False
    assert report["authority"]["moved"] == ["capability"]
    assert report["authority"]["diff"]["capability"] == [
        "host:TenantAStore:host_fmt"]
    assert report["authority"]["link"] == "authority-widened"

    # the barrier, as an absence. The measured result is not in the report
    # because the function returned before it existed.
    assert report["divergenceRead"] is False
    assert "divergence" not in report

    rendered = canary.render(report)
    assert "AUTHORITY WIDENED" in rendered
    assert "RECOMMENDATION: refuse" in rendered
    assert "[NO DIVERGENCE]" not in rendered


def test_the_control_with_unchanged_reach_promotes_on_the_same_evidence(baseline_ir):
    """The other half of the exit test, and the thing that keeps the first half
    from being satisfiable by refusing everything. Same baseline, same kind of
    candidate, same clean comparison; reach unchanged, so it promotes."""
    report = canary.run_canary(baseline_ir, candidate_files=[CONTROL],
                               realm="tenant_a", over_the_transport=False)
    assert report["recommendation"] == "promote"
    assert report["authority"]["ok"] is True
    assert report["authority"]["moved"] == []
    assert report["divergenceRead"] is True
    assert report["divergence"]["diverged"] is False


def test_the_two_candidates_are_behaviourally_indistinguishable(baseline_ir):
    """The non-vacuity of the pair. If the widened candidate also diverged, the
    refusal above would prove nothing about the barrier: the old code would
    have refused it too, on the comparison. Both timelines are identical to the
    baseline's, so the ONLY thing separating the two verdicts is the authority
    diff.
    """
    base_tl = canary.slice_timeline(baseline_ir, "TenantAStore")
    for fixture in (CONTROL, WIDER):
        cand = compile_files([fixture], manifest=baseline_ir,
                             replacing=("TenantAStore",))
        cand_tl = canary.slice_timeline(cand, "TenantAStore")
        assert canary.compare_timelines(base_tl, cand_tl)["diverged"] is False, \
            f"{os.path.basename(fixture)} diverges, so it is not a test of the barrier"


def test_every_axis_is_measured_and_named(baseline_ir):
    """The canary's authority diff covers all five canonical axes. An axis it
    stopped producing would be `unmeasured`, and `authority_moved` reads the
    canonical set rather than the keys the diff happens to have, so it would
    refuse rather than pass."""
    cand = compile_files([CONTROL], manifest=baseline_ir,
                         replacing=("TenantAStore",))
    measured = canary.authority_diff(baseline_ir, cand)
    assert set(measured["diff"]) == set(pb.AUTHORITY_AXES)
    assert measured["unmeasured"] == []
    # drop an axis and the shared reading refuses
    partial = {k: v for k, v in measured["diff"].items() if k != "retention"}
    assert pb.authority_moved(partial)[0] == ["retention"]


def test_the_authority_verdict_fails_closed_when_the_diff_cannot_be_built(baseline_ir):
    """A generation whose audit cannot be built has not been shown to keep the
    incumbent's authority, so it is refused rather than compared."""
    verdict = canary.judge_authority(baseline_ir, {"components": "not an ir"})
    assert verdict["ok"] is False
    assert verdict["widened"] is True
