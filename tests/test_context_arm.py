"""bench/context_arm.py: rounds-to-green with and without the declarations
pack (docs/context.md). No model is called here: the task set is checked to
be answerable, the two arms to differ only in what they read, and the mock
runner drives the whole loop."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _arm():
    spec = importlib.util.spec_from_file_location(
        "bench_context_arm", ROOT / "bench" / "context_arm.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


arm = _arm()
TASKS = arm.load_tasks()


def test_there_are_eight_tasks_over_at_least_five_compositions():
    assert len(TASKS) == 8
    assert len({t["id"] for t in TASKS}) == 8
    assert len({t["file"] for t in TASKS}) >= 5


@pytest.mark.parametrize("task", TASKS, ids=[t["id"] for t in TASKS])
def test_the_removed_component_compiles_back_where_it_stood(task):
    piece = arm.cut(task)
    assert f"component {task['component']} " not in piece["holed"]
    assert piece["block"].lstrip().startswith(("component", "pub component"))
    assert arm.grade(piece, piece["block"], task["component"]) == (True, None)


@pytest.mark.parametrize("task", TASKS, ids=[t["id"] for t in TASKS])
@pytest.mark.parametrize("reply", ["", "component Zzz {}", "WHOLE", "EXTRA"])
def test_a_reply_that_is_not_the_component_alone_is_red(task, reply):
    # nothing else in several of these files depends on the removed
    # component, so the file compiles without it: the shape check is what
    # keeps an empty or renamed reply from being graded green
    piece = arm.cut(task)
    reply = {"WHOLE": arm.splice(piece["holed"], piece["at"], piece["block"]),
             "EXTRA": piece["block"] + "\ncomponent Zzz {}\n"}.get(reply, reply)
    ok, error = arm.grade(piece, reply, task["component"])
    assert not ok and f"component {task['component']}" in error


@pytest.mark.parametrize("task", TASKS, ids=[t["id"] for t in TASKS])
def test_the_arms_differ_only_in_what_they_read(task):
    piece = arm.cut(task)
    packs = {a: arm.pack_for(task, piece, a) for a in arm.ARMS}
    prompts = {a: arm.prompt(task, a, piece, packs[a]) for a in arm.ARMS}
    # the task, the header and the instructions are the same text in all
    tail = prompts["raw"][prompts["raw"].index("Write the component"):]
    assert all(p.endswith(tail) for p in prompts.values())
    read = {a: arm.reading(a, piece, packs[a], task) for a in arm.ARMS}
    # the pack arm reads no provider body; the others add exactly their part
    assert "provide " not in read["pack"]
    # the card arms read what `revl context` ships, the illustration included
    assert packs["pack-card"]["pack"] == arm.context(
        [str(piece["path"])], component=task["component"])["pack"]
    assert read["pack-card"] == arm.PACK_READING.format(
        text=packs["pack-card"]["pack"])
    assert arm.IDIOMS.strip() in read["pack-card"]
    assert arm.IDIOMS.strip() not in read["pack"]
    name, block = arm.sibling(task, packs["pack"])
    near = arm.SIBLING_READING.format(name=name, text=block)
    assert read["pack-sibling"] == read["pack"] + near
    assert read["pack-both"] == read["pack-card"] + near
    # the declarations read less than the files; the illustration is a fixed
    # cost that can outweigh the smallest file (docs/context.md)
    assert len(read["pack"]) < len(read["raw"])


@pytest.mark.parametrize("task", TASKS, ids=[t["id"] for t in TASKS])
def test_the_sibling_is_another_providing_component(task):
    pack = arm.context([str(arm.ROOT / task["file"])],
                       component=task["component"])
    name, block = arm.sibling(task, pack)
    assert name != task["component"]
    assert block.lstrip().startswith(f"component {name}") and "provide " in block
    # the provider of a required service is preferred when there is one
    required = {p["component"] for s in pack["services"]
                if s["role"] == "requires" for p in s["providers"]}
    assert name in required or not required


def test_a_reply_that_does_not_check_is_graded_red_with_the_compiler_text():
    task = next(t for t in TASKS if t["id"] == "audit-seam")
    piece = arm.cut(task)
    broken = piece["block"].replace("audit.record(q)", "audit.missing(q)")
    ok, error = arm.grade(piece, broken, task["component"])
    assert not ok and "missing" in error
    # the path is the repository's, not this machine's
    assert str(arm.ROOT) not in error and task["file"] in error


def test_a_retry_asks_again_for_the_block_alone(tmp_path, monkeypatch):
    prompts = []

    def local(system, current, *rest):
        prompts.append(current)
        return {"text": "```revl\ncomponent Seam {\n```", "tokens_in": 5,
                "output_tokens": 1}
    monkeypatch.setattr(arm.run, "run_local", local)
    rows = arm.main(["--runner", "local", "--model", "m", "--out", str(tmp_path),
                     "--label", "t", "--tasks", "audit-seam", "--arms", "pack",
                     "--max-iters", "2"])
    assert rows[0]["rounds"] == 3 and rows[0]["tokens_in"] == 10
    assert prompts[1].startswith(prompts[0])
    retry = prompts[1][len(prompts[0]):]
    assert "only the corrected `Seam` block" in retry
    assert "complete .rvl file" not in retry


def test_the_mock_run_is_green_at_round_two_in_both_arms(tmp_path):
    rows = arm.main(["--runner", "mock", "--out", str(tmp_path),
                     "--label", "m"])
    assert len(rows) == len(arm.ARMS) * len(TASKS)
    assert all(r["solved"] and r["rounds"] == 2 for r in rows)
    lines = (tmp_path / "m" / "results.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(rows)
    summary = (tmp_path / "m" / "summary.md").read_text(encoding="utf-8")
    assert "pack minus raw, mean rounds per task: +0.00 (does not lose rounds)" \
        in summary
    assert "0 of 8" in summary


def test_an_unsolved_cell_counts_max_iters_plus_one(tmp_path, monkeypatch):
    monkeypatch.setattr(arm, "_mock", lambda task, piece, attempt: {
        "text": "```revl\ncomponent Nope {\n```", "tokens_in": 10,
        "output_tokens": 1})
    rows = arm.main(["--runner", "mock", "--out", str(tmp_path), "--label", "u",
                     "--tasks", "audit-seam", "--arms", "pack",
                     "--max-iters", "2"])
    assert [(r["solved"], r["rounds"], r["tokens_in"]) for r in rows] \
        == [(False, 3, 20)]


def _row(task, a, rounds, tokens, error=False):
    return {"task": task, "arm": a, "rep": 1, "rounds": rounds,
            "solved": rounds <= 3, "tokens_in": tokens, "reading_tokens": 1,
            "runner_error": error}


def test_the_verdict_needs_no_lost_rounds_and_a_token_win():
    promote = [_row("a", "raw", 2, 1000), _row("a", "pack", 2, 300)]
    assert "verdict: PROMOTE" in arm.summarize(promote, 3)
    slower = [_row("a", "raw", 1, 1000), _row("a", "pack", 2, 300)]
    assert "verdict: HOLD (loses rounds)" in arm.summarize(slower, 3)
    dearer = [_row("a", "raw", 2, 300), _row("a", "pack", 2, 1000)]
    assert "verdict: HOLD (no token win)" in arm.summarize(dearer, 3)


def test_runner_errors_are_left_out_and_withhold_the_verdict():
    rows = [_row("a", "raw", 1, 1000), _row("a", "pack", 1, 300),
            _row("a", "pack", 4, 0, error=True)]
    summary = arm.summarize(rows, 3)
    assert "runner error: 1" in summary and "verdict:" not in summary


def test_unreported_usage_makes_the_token_ratio_unknown():
    rows = [_row("a", "raw", 1, 1000), _row("a", "pack", 1, None)]
    summary = arm.summarize(rows, 3)
    assert "unknown" in summary and "verdict: none" in summary


def test_resume_drops_rows_of_another_model(tmp_path):
    arm.main(["--runner", "mock", "--out", str(tmp_path), "--label", "x",
              "--tasks", "audit-seam", "--arms", "raw,pack"])
    path = tmp_path / "x" / "results.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    path.write_text("".join(json.dumps({**r, "model": "other"}) + "\n"
                            for r in rows))
    again = arm.main(["--runner", "mock", "--out", str(tmp_path), "--label",
                      "x", "--tasks", "audit-seam", "--resume",
                      "--arms", "raw,pack"])
    assert [r["model"] for r in again] == ["mock", "mock"]


def test_resume_runs_only_the_cells_without_a_row(tmp_path, monkeypatch):
    arm.main(["--runner", "mock", "--out", str(tmp_path), "--label", "r",
              "--tasks", "audit-seam", "--arms", "raw,pack"])
    calls = []
    real = arm.run_cell
    monkeypatch.setattr(arm, "run_cell", lambda *a: calls.append(a) or real(*a))
    rows = arm.main(["--runner", "mock", "--out", str(tmp_path), "--label", "r",
                     "--tasks", "audit-seam", "--reps", "2", "--resume",
                     "--arms", "raw,pack"])
    assert [(c[0]["id"], c[1], c[2]) for c in calls] \
        == [("audit-seam", "raw", 2), ("audit-seam", "pack", 2)]
    assert len(rows) == 4
    stored = [json.loads(x) for x in
              (tmp_path / "r" / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(stored) == 4
