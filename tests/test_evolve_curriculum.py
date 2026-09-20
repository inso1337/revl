"""The evolution curriculum: derived tasks, a computed tier, and a gate that fires.

Roadmap item 535 / issue #1205. Three things are worth testing here and the
order matters:

  1. every task names an artifact that is in the tree (derived, not invented);
  2. the gap each task names is really exhibited at HEAD -- re-derived HERE, by
     a different route than the generator uses, so a generator that emitted
     unfalsifiable tasks would not be able to make these pass;
  3. the `--check` gate FIRES. This repository has measured five separate
     checks that ran on every PR and could not fail; a sixth is worth nothing.
     `test_check_reds_when_a_rung_cannot_be_populated` makes it red on the real
     code path, and `test_check_is_green_on_the_whole_curriculum` is the
     control that passes on the same tree.
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
def curriculum():
    tool = _tool()
    return tool, tool.generate()


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
def test_every_rung_is_non_vacuously_populated(curriculum):
    tool, tasks = curriculum
    counts = tool.populations(tasks)
    assert set(counts) == set(tool.TIERS)
    for rung, n in counts.items():
        assert n > 0, f"rung {rung!r} has no task at HEAD"


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


def test_hard_tasks_name_a_program_the_reference_refuses(curriculum):
    """The census bypasses: the named program must exist AND the reference must
    really refuse it, which is the half that makes the task a task."""
    from revl import compile_files
    from revl.errors import RevlError

    _, tasks = curriculum
    hard = [t for t in tasks if t.task_id.startswith("census/")]
    assert hard
    for task in hard:
        path = ROOT / task.artifact
        assert path.is_file(), f"{task.task_id}: {task.artifact} is missing"
        with pytest.raises(RevlError):
            compile_files([str(path)])


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
def test_check_is_green_on_the_whole_curriculum(curriculum):
    """The control. Same tree, same code path, no problems."""
    tool, tasks = curriculum
    assert tool.check(tasks) == []


def test_check_reds_when_a_rung_cannot_be_populated(curriculum):
    """The firing proof. Restrict the curriculum to the census source alone and
    the three rungs it cannot reach are named, one line each."""
    tool, _ = curriculum
    only_hard = tool.generate(["census-bypass"])
    assert only_hard
    assert {t.tier for t in only_hard} == {"hard"}
    problems = tool.check(only_hard)
    assert len(problems) == 3
    for rung in ("easy", "medium", "expert"):
        assert any(f"tier {rung!r} has no task" in p for p in problems), problems


def test_check_reds_on_a_task_whose_artifact_left_the_tree(curriculum):
    """The other firing direction: an invented task. Rewrite one task's artifact
    to a path that is not in the tree and the gate names it."""
    tool, tasks = curriculum
    import dataclasses
    mutated = list(tasks)
    mutated[0] = dataclasses.replace(
        mutated[0], artifact="docs/design/this-file-does-not-exist.md")
    problems = tool.check(mutated)
    assert any("is not in the tree" in p for p in problems), problems


def test_cli_check_exits_zero_and_the_restricted_run_exits_one():
    """Through the CLI, because that is how a gate is actually invoked."""
    tool_path = ROOT / "tools" / "evolve_curriculum.py"
    green = subprocess.run(
        [sys.executable, str(tool_path), "--check"],
        capture_output=True, text=True, cwd=ROOT)
    assert green.returncode == 0, green.stderr[-2000:]
    red = subprocess.run(
        [sys.executable, str(tool_path), "--check",
         "--adapter", "census-bypass"],
        capture_output=True, text=True, cwd=ROOT)
    assert red.returncode == 1
    assert "CURRICULUM-RED" in red.stderr


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
