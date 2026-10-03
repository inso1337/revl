"""The blast-radius benchmark (issue #1702): bench/blast_radius/.

Exit tests: every task's ground-truth cascade (and answer) equals the
compiler query's; every expected final state compiles and touches exactly the
cascade; the TypeScript renderings are what the ts emitter produces now (their
type-check runs in backends/typescript/test_blast_radius_ts.py, where node is);
and the scorer reproduces the hand-scored examples. No model runs.
"""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "bench" / "blast_radius"


def _load():
    spec = importlib.util.spec_from_file_location(
        "blast_radius_bench", BENCH / "blast_radius.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = _load()
TASKS = B.load_tasks()
SCORED = sorted((BENCH / "scored").glob("*.score.json"))


@pytest.mark.parametrize("task", TASKS, ids=[t["id"] for t in TASKS])
def test_ground_truth_is_the_query_answer(task):
    assert B.check_task(task) == []


_TOOL = {"withdraw": "revl_query_withdraw", "drift": "revl_query_drift",
         "emitters": "revl_query_emitters"}


@pytest.mark.parametrize("task", TASKS, ids=[t["id"] for t in TASKS])
def test_the_bench_query_is_the_mcp_tool_answer(task):
    """The bench calls `revl.query` directly. Pin that it is the same answer
    the agent-facing MCP tool gives for the same composition file."""
    from revl.mcp.query_tools import QUERY_TOOLS

    handlers = {tool["name"]: tool["handler"] for tool in QUERY_TOOLS}
    path = str(B.composition_path(task["composition"]))
    ir = B.compile_path(B.composition_path(task["composition"]))
    for spec in B._queries(task):
        arguments = {k: v for k, v in spec.items() if k != "verb"}
        got = handlers[_TOOL[spec["verb"]]]({"files": [path], **arguments})
        assert got == B.run_query(ir, spec)


def test_the_task_set_covers_the_issue():
    kinds = {t["kind"] for t in TASKS}
    assert {"withdraw", "interface", "try-and-revert", "no-change"} <= kinds
    for name in {t["composition"] for t in TASKS}:
        ir = B.compile_path(B.composition_path(name))
        assert len(ir["components"]) >= 20, name


def test_a_recorded_cascade_that_disagrees_is_caught():
    task = copy.deepcopy(B.task_by_id("commerce-withdraw-mail"))
    task["groundTruth"]["cascade"] = ["Notifier"]
    problems = B.check_task(task)
    assert any("cascade is ['Notifier', 'ShippingDesk']" in p for p in problems)


def test_a_wrong_recorded_answer_is_caught():
    task = copy.deepcopy(B.task_by_id("tenants-try-b-store-a-keys"))
    task["groundTruth"]["answer"] = "yes"
    assert any("answer is 'no'" in p for p in B.check_task(task))


def test_an_expected_state_that_over_touches_is_caught(tmp_path):
    task = copy.deepcopy(B.task_by_id("commerce-withdraw-metrics"))
    text = (BENCH / task["expected"]).read_text(encoding="utf-8")
    edited = text.replace("    fn track(order) = orders.read(order)",
                          "    fn track(order) = orders.read(\"x\")")
    assert edited != text
    (tmp_path / "over.rvl").write_text(edited, encoding="utf-8")
    task["expected"] = str(tmp_path / "over.rvl")
    problems = B.check_task(task)
    assert any("changed surviving components: ['ShippingDesk']" in p for p in problems)


def test_the_typescript_renderings_are_current():
    assert B.check_renderings() == []


def test_the_ts_reader_sees_every_component():
    for name in {t["composition"] for t in TASKS}:
        ir = B.compile_path(B.composition_path(name))
        text = (B.TS_DIR / f"{name}.ts").read_text(encoding="utf-8")
        assert set(B.ts_components(text)) == {c["name"] for c in ir["components"]}
        assert B.ts_link_problems(B.ts_components(text)) == []


@pytest.mark.parametrize("expected", SCORED, ids=[p.name for p in SCORED])
def test_the_scorer_reproduces_a_hand_scored_example(expected):
    submission = expected.with_name(expected.name.replace(".score.json", ".json"))
    got = B.score(json.loads(submission.read_text(encoding="utf-8")), submission.parent)
    assert got == json.loads(expected.read_text(encoding="utf-8"))


def test_there_are_hand_scored_examples_for_both_arms():
    arms = {json.loads(p.read_text(encoding="utf-8"))["arm"] for p in SCORED}
    assert arms == {"revl", "typescript"}


def test_an_unreported_gate_axis_is_null_not_zero():
    axes = B.gate_axes({"ok": True, "loaded": True, "loopAxes": {
        "reversibilityRate": {"numerator": 1, "denominator": 2, "value": 0.5}}})
    assert axes["reversibilityRate"] == {"numerator": 1, "denominator": 2, "value": 0.5}
    assert axes["promptsPerSession"] is None
    assert "promptsPerSession" in axes["notInRevlState"]
    assert "reversibilityRate" not in axes["notInRevlState"]
    assert B.gate_axes(None)["notInRevlState"] == list(B.STATE_AXES)
