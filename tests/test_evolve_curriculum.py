"""The evolution curriculum: derived tasks, a computed tier, and a gate that fires.

Roadmap item 535 / issue #1205. Three things are worth testing here and the
order matters:

  1. every task names an artifact that is in the tree (derived, not invented);
  2. the gap each task names is really exhibited at HEAD -- re-derived HERE, by
     a different route than the generator uses, so a generator that emitted
     unfalsifiable tasks would not be able to make these pass;
  3. the `--check` gate FIRES. This repository has measured five separate
     checks that ran on every PR and could not fail; a sixth is worth nothing.
     `test_check_reds_when_a_rung_is_unreachable` makes it red on the real code
     path, and `test_check_is_green_on_the_whole_curriculum` is the control
     that passes on the same tree.

A fourth thing became worth testing with issue #1410. `hard` is derived from
recorded gate bypasses and the census baseline records none: PR #1396 met item
391's exit and PR #1404 took the go carried set to zero, so the rung is empty
because the work behind it was finished. What is gated is therefore whether a
rung is REACHABLE, and the population is reported beside it. The tests that
depended on `hard` being occupied would otherwise have been a third instance
this week of a guard that only worked while something was broken, so each one
now CONSTRUCTS the state it is about: `test_a_recorded_bypass_fills_the_hard_
rung_and_removing_it_empties_it` stages a baseline entry and watches the rung
fill and empty again, and `test_an_empty_rung_and_a_source_that_could_not_run_
do_not_print_the_same_thing` holds the two apart at the output.
"""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _tool():
    spec = importlib.util.spec_from_file_location(
        "evolve_curriculum", ROOT / "tools" / "evolve_curriculum.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def derived():
    """One run of every source, with the measurement kept beside the tasks."""
    tool = _tool()
    return tool, tool.derive()


@pytest.fixture(scope="module")
def curriculum(derived):
    tool, derivations = derived
    return tool, tool.tasks_of(derivations)


# --------------------------------------------------------------------------- #
# 1. Derived, not invented.                                                     #
# --------------------------------------------------------------------------- #
def test_every_task_names_an_artifact_that_is_in_the_tree(curriculum):
    _, tasks = curriculum
    assert tasks
    for task in tasks:
        assert (ROOT / task.artifact).exists(), \
            f"{task.task_id} names {task.artifact}, which is not in the tree"
        assert task.locator, f"{task.task_id} does not say where in {task.artifact}"
        assert task.mechanism, f"{task.task_id} names no verifying mechanism"
        assert task.gap, f"{task.task_id} states no gap"


def test_task_ids_are_unique(curriculum):
    _, tasks = curriculum
    ids = [t.task_id for t in tasks]
    assert len(ids) == len(set(ids))


# --------------------------------------------------------------------------- #
# 2. The tier is computed, and every rung is populated.                         #
# --------------------------------------------------------------------------- #
def test_every_rung_is_reachable_and_the_population_is_measured(derived):
    """What is gated is reachability; the population is a measurement.

    Every rung must be stamped by some source that is reading its artifacts at
    HEAD. How many instances that source currently finds is a fact about the
    tree, not about the ladder: `hard` is 0 at HEAD because the recorded gate
    bypasses it derives from were all closed.
    """
    tool, derivations = derived
    counts = tool.populations(tool.tasks_of(derivations))
    assert set(counts) == set(tool.TIERS)
    who = tool.reached_by(derivations)
    for rung in tool.TIERS:
        assert who[rung], f"rung {rung!r} is stamped by no source at HEAD"
    assert sum(counts.values()) == len(tool.tasks_of(derivations))


def test_every_task_signal_is_one_its_source_declares_it_reaches(derived):
    """The anti-drift guard for the split above.

    Reachability is only worth gating if it is the same measurement the tasks
    carry. A source that declared one set of signals and stamped another would
    make a rung look reachable that nothing feeds, which is the decorative rung
    the gate exists to catch.
    """
    _, derivations = derived
    for d in derivations:
        declared = set(d.signals)
        for task in d.tasks:
            assert task.signal in declared, \
                f"{d.source} stamps {task.signal} on {task.task_id} and does " \
                f"not declare it reachable"


def test_the_rungs_come_from_different_sources(curriculum):
    """A ladder whose four rungs all come from one source is one source with
    four labels on it. Each rung must be reachable from a different derivation."""
    _, tasks = curriculum
    by_rung: dict[str, set[str]] = {}
    for task in tasks:
        by_rung.setdefault(task.tier, set()).add(task.source)
    assert len({frozenset(v) for v in by_rung.values()}) == len(by_rung)


def test_tier_is_a_total_function_of_the_signal(curriculum):
    """No adapter may assign a rung: the same measured signal must always give
    the same rung, whichever source produced it."""
    tool, tasks = curriculum
    for task in tasks:
        assert task.tier == tool.tier(task.signal)
    seen: dict[tuple, str] = {}
    for task in tasks:
        key = (task.signal.impls, task.signal.breadth, task.signal.proved)
        seen.setdefault(key, task.tier)
        assert seen[key] == task.tier


def test_the_ladder_separates_the_signals_it_is_supposed_to_separate(curriculum):
    """Spot the four rungs directly off the signal, so a `tier` that collapsed
    to a constant (the way a vacuous classifier does) fails here."""
    tool = curriculum[0]
    Signal = tool.Signal
    assert tool.tier(Signal(impls=("reference",), breadth=1)) == "easy"
    assert tool.tier(Signal(impls=("reference", "selfhost"), breadth=1)) == "medium"
    assert tool.tier(Signal(impls=("reference", "selfhost"), breadth=8)) == "hard"
    assert tool.tier(Signal(impls=("reference", "selfhost", "native"),
                            breadth=8)) == "expert"
    assert tool.tier(Signal(impls=("reference", "formal"), breadth=0,
                            proved=True)) == "expert"


# --------------------------------------------------------------------------- #
# 3. The gap each rung names is exhibited at HEAD, re-derived independently.    #
# --------------------------------------------------------------------------- #
def test_easy_tasks_name_a_code_revl_explain_cannot_answer(curriculum):
    """Re-derived through the shipped `explain` entry point rather than through
    the generator's AST scan: the code really is one an agent cannot look up."""
    from revl.diagnostics import explain

    _, tasks = curriculum
    easy = [t for t in tasks if t.task_id.startswith("diagnostic/")]
    assert easy
    for task in easy:
        code = task.task_id.split("/", 1)[1]
        answer = explain(code)
        assert answer["ok"] is False, \
            f"{task.task_id} claims `revl explain {code}` has no answer, but it does"
        assert answer["message"].startswith("no diagnostic code")


def test_easy_tasks_name_a_real_refusal_site(curriculum):
    """And the code is one the reference actually stamps on a refusal, at the
    file and line the task names."""
    _, tasks = curriculum
    easy = [t for t in tasks if t.task_id.startswith("diagnostic/")]
    for task in easy:
        code = task.task_id.split("/", 1)[1]
        lineno = int(task.locator.split()[-1])
        text = (ROOT / task.artifact).read_text(encoding="utf-8").splitlines()
        window = "\n".join(text[lineno - 1:lineno + 12])
        assert f'"{code}"' in window or f"({code})" in window, \
            f"{task.task_id}: {task.artifact}:{lineno} does not stamp {code}"


def test_expert_tasks_name_a_guarantee_no_lean_file_mentions(curriculum):
    """Re-derived with a plain grep over formal/, which is what a reviewer
    would do, rather than through the generator's own scan."""
    _, tasks = curriculum
    expert = [t for t in tasks if t.task_id.startswith("formal/")]
    assert expert
    lean = list((ROOT / "formal").rglob("*.lean"))
    assert lean, "formal/ has no .lean files; this test would be vacuous"
    for task in expert:
        code = task.task_id.split("/", 1)[1]
        word = re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(code)
                          + r"(?![A-Za-z0-9_-])")
        hits = [p.name for p in lean
                if word.search(p.read_text(encoding="utf-8"))]
        assert not hits, f"{task.task_id} claims formal/ never names {code}, " \
                         f"but {hits} do"
        from revl.diagnostics import GUARANTEES
        assert code in GUARANTEES, \
            f"{task.task_id} claims {code} is a guarantee the reference enforces"


def _refuses_every_named_program(tasks):
    """The census predicate: the named program exists AND the reference really
    refuses it, which is the half that makes the task a task."""
    from revl import compile_files
    from revl.errors import RevlError

    for task in tasks:
        path = ROOT / task.artifact
        assert path.is_file(), f"{task.task_id}: {task.artifact} is missing"
        with pytest.raises(RevlError):
            compile_files([str(path)])
    return len(tasks)


def test_hard_tasks_name_a_program_the_reference_refuses(curriculum):
    """Holds over whatever census tasks HEAD has, which is currently none.

    The assertion is not allowed to be vacuous just because it is empty here,
    so its teeth are shown on a constructed baseline in
    `test_a_recorded_bypass_fills_the_hard_rung_and_removing_it_empties_it`
    rather than by requiring the tree to still have an open gate bypass.
    """
    _, tasks = curriculum
    _refuses_every_named_program(
        [t for t in tasks if t.task_id.startswith("census/")])


# A baseline entry is a recorded divergence, so it cannot be conjured from
# nothing honestly: these are programs the reference genuinely refuses (they
# are `examples/rejections/`, which exist to be refused), staged into a
# baseline of this test's own so the generator has an entry to derive from.
# The staged file says nothing about HEAD and is never written into the tree.
_STAGED_BYPASSES = (
    "examples/rejections/t13_unknown_match_case.rvl",
    "examples/rejections/t18_type_alias_cycle.rvl",
)


def _staged_baseline(tmp_path, cases):
    import json
    live = json.loads((ROOT / "tools" / "gate_reference_census_baseline.json")
                      .read_text())
    staged = tmp_path / "baseline.json"
    staged.write_text(json.dumps(
        {**live, "buckets": {"false-admit/TYPE": list(cases)} if cases else {}}))
    return staged


def test_a_recorded_bypass_fills_the_hard_rung_and_removing_it_empties_it(
        derived, tmp_path):
    """The demonstration issue #1410 asks for, in both directions.

    A generator nobody has watched produce an empty rung deliberately is not
    evidence, so: stage two recorded bypasses, watch `hard` fill with them and
    the gate stay green; take them away, watch the rung empty, the source keep
    declaring that it reaches `hard`, and the gate stay green for the other
    reason. What changes between the two runs is one file this test owns.
    """
    tool, live = derived

    filled = tool.adapter_census_bypass(
        _staged_baseline(tmp_path, _STAGED_BYPASSES))
    assert {t.tier for t in filled.tasks} == {"hard"}
    assert _refuses_every_named_program(filled.tasks) == len(_STAGED_BYPASSES)

    others = [d for d in live if d.source != "census-bypass"]
    with_entries = others + [filled]
    assert tool.populations(tool.tasks_of(with_entries))["hard"] == \
        len(_STAGED_BYPASSES)
    assert "hard" not in tool.empty_rungs(with_entries)
    assert tool.check(with_entries) == []

    emptied = tool.adapter_census_bypass(_staged_baseline(tmp_path, ()))
    assert emptied.tasks == ()
    assert emptied.signals == filled.signals      # the same measurement
    assert "hard" in emptied.rungs()
    without = others + [emptied]
    assert tool.populations(tool.tasks_of(without))["hard"] == 0
    assert tool.empty_rungs(without)["hard"] == ["census-bypass"]
    assert tool.check(without) == [], \
        "an empty but reachable rung is a measurement, not a failure"


def test_medium_tasks_name_a_dispatch_the_reference_really_has(curriculum):
    """A construct-reach task is only a task if the reference emitter carries
    the dispatch and the tier's corpus never reaches it. Both halves are
    re-read here from the emitter source and the report."""
    tool, tasks = curriculum
    reach = tool._load("_ec_reach_test", "tools/oracle_construct_reach.py")
    report = reach.survey()
    medium = [t for t in tasks if t.task_id.startswith("reach/")]
    assert medium
    checked = 0
    for task in medium:
        _, oracle, construct = task.task_id.split("/", 2)
        assert construct in report[oracle]["unreached"], \
            f"{task.task_id} is not unreached in the report it was derived from"
        assert construct not in report[oracle]["reached"]
        field, _, value = construct.partition("=")
        emitter = task.subject.split(" + ")[0]
        if emitter.startswith("backends/") and value and value != "<true>":
            source = (ROOT / emitter).read_text(encoding="utf-8")
            assert f'"{value}"' in source, \
                f"{task.task_id}: {emitter} never names {value}"
            checked += 1
    assert checked > 10, "too few dispatches confirmed against the emitter source"


# --------------------------------------------------------------------------- #
# 4. The gate fires.                                                            #
# --------------------------------------------------------------------------- #
def test_check_is_green_on_the_whole_curriculum(derived):
    """The control. Same tree, same code path, no problems."""
    tool, derivations = derived
    assert tool.check(derivations) == []


def test_check_reds_when_a_rung_is_unreachable(derived):
    """The firing proof. Restrict the curriculum to one source and the rungs
    nothing then stamps are named, one line each.

    `unexplained-refusal` is used rather than `census-bypass` on purpose: the
    proof must not itself depend on a source having live instances, which is
    the trap the whole issue is about. It holds on the signals, so it would
    still hold if this source's population went to zero too.
    """
    tool, _ = derived
    only_easy = tool.derive(["unexplained-refusal"])
    assert tool.reached_by(only_easy)["easy"] == ["unexplained-refusal"]
    problems = tool.check(only_easy)
    assert len(problems) == 3, problems
    for rung in ("medium", "hard", "expert"):
        assert any(f"tier {rung!r} is unreachable" in p for p in problems), \
            problems


def test_check_reds_on_a_source_that_ran_and_measured_nothing(derived):
    """The state that used to be indistinguishable from a finished rung.

    A source whose artifacts were renamed away skips every branch and returns
    empty. That must not read as `hard` being done, so a derivation with no
    signal is named, and the rung it fed reds as unreachable.
    """
    tool, derivations = derived
    import dataclasses
    blinded = [dataclasses.replace(d, signals=(), tasks=())
               if d.source == "census-bypass" else d for d in derivations]
    problems = tool.check(blinded)
    assert any("'census-bypass' ran and measured no signal" in p
               for p in problems), problems
    assert any("tier 'hard' is unreachable" in p for p in problems), problems
    assert tool.empty_rungs(blinded) == {}, \
        "a source that measured nothing must not be reported as merely empty"


def test_check_reds_on_a_source_that_could_not_be_derived(derived):
    """And the third state: the generator could not run at all."""
    tool, _ = derived

    def explodes():
        raise FileNotFoundError("tools/gate_reference_census_baseline.json")

    saved = tool.ADAPTERS["census-bypass"]
    tool.ADAPTERS["census-bypass"] = explodes
    try:
        broken = tool.derive(["census-bypass", "unexplained-refusal"])
    finally:
        tool.ADAPTERS["census-bypass"] = saved
    problems = tool.check(broken)
    assert any("'census-bypass' could not be derived at HEAD: "
               "FileNotFoundError" in p for p in problems), problems


def test_check_reds_on_a_task_whose_artifact_left_the_tree(derived):
    """The other firing direction: an invented task. Rewrite one task's artifact
    to a path that is not in the tree and the gate names it."""
    tool, derivations = derived
    import dataclasses
    mutated = list(derivations)
    first = mutated[0]
    mutated[0] = dataclasses.replace(first, tasks=(
        dataclasses.replace(first.tasks[0],
                            artifact="docs/design/this-file-does-not-exist.md"),
    ) + first.tasks[1:])
    problems = tool.check(mutated)
    assert any("is not in the tree" in p for p in problems), problems


def _cli(*args):
    return subprocess.run(
        [sys.executable, str(ROOT / "tools" / "evolve_curriculum.py"), *args],
        capture_output=True, text=True, cwd=ROOT)


def test_cli_check_exits_zero_and_the_restricted_run_exits_one():
    """Through the CLI, because that is how a gate is actually invoked."""
    green = _cli("--check")
    assert green.returncode == 0, green.stderr[-2000:]
    red = _cli("--check", "--adapter", "census-bypass")
    assert red.returncode == 1
    assert "CURRICULUM-RED" in red.stderr


def test_an_empty_rung_and_a_source_that_could_not_run_do_not_print_the_same_thing():
    """The output-level half of issue #1410.

    Two different events reach the reader of a `--check` run today: `hard` is
    empty because the bypasses behind it were closed, and a source that cannot
    read its artifacts. They are held apart by prefix and by exit code, and
    both halves are read off real runs rather than asserted.
    """
    green = _cli("--check")
    assert green.returncode == 0
    empty = [ln for ln in green.stderr.splitlines()
             if ln.startswith("CURRICULUM-EMPTY")]
    assert len(empty) == 1, green.stderr[-2000:]
    assert "tier 'hard' has 0 tasks at HEAD" in empty[0]
    assert "census-bypass" in empty[0], "the empty rung does not say who feeds it"
    assert "CURRICULUM-RED" not in green.stderr
    assert "  hard       0  empty, and reachable" in green.stdout, green.stdout[:400]

    red = _cli("--check", "--adapter", "unexplained-refusal")
    assert red.returncode == 1
    reds = [ln for ln in red.stderr.splitlines()
            if ln.startswith("CURRICULUM-RED")]
    assert len(reds) == 3, red.stderr[-2000:]
    assert all("is unreachable at HEAD" in ln for ln in reds)
    # and the two never collide: no line carries both prefixes, and the wording
    # of an empty rung never appears on a red one.
    assert not any("has 0 tasks at HEAD" in ln for ln in reds)


def test_the_non_vacuity_assertions_would_reject_an_invented_task(curriculum):
    """The negative control for the three re-derivation tests above.

    Those tests pass on every derived task. That is only evidence if the same
    assertions REJECT a task that names a gap the tree does not exhibit, so
    here are three that do not exhibit it, checked with the same predicates:

      * `G1` is a code `revl explain` answers, so it is no diagnostic gap;
      * `G1` is named by .lean files under formal/, so it is no formal gap;
      * `examples/counter_pair.rvl` compiles, so it is no census bypass.
    """
    from revl import compile_files
    from revl.diagnostics import explain

    assert explain("G1")["ok"] is True

    word = re.compile(r"(?<![A-Za-z0-9_-])G1(?![A-Za-z0-9_-])")
    assert any(word.search(p.read_text(encoding="utf-8"))
               for p in (ROOT / "formal").rglob("*.lean"))

    admitted = ROOT / "examples" / "counter_pair.rvl"
    assert admitted.is_file()
    compile_files([str(admitted)])        # no RevlError: not a bypass

    # and none of the three is in the curriculum.
    _, tasks = curriculum
    ids = {t.task_id for t in tasks}
    assert "diagnostic/G1" not in ids
    assert "formal/G1" not in ids
    assert not any(t.artifact == "examples/counter_pair.rvl" for t in tasks)
