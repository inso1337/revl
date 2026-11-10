"""bench/run.py applies the compiler's verified edits before re-asking the model.

A refused candidate whose diagnostic carries a `fixgen` edit is patched, one
edit at a time, and recompiled; a green reached that way is green on that
attempt, with no model turn. `--no-autofix` keeps the loop as it was, so a run
can report both arms. These run the mock runner against the real compiler."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_PATH = ROOT / "bench" / "run.py"
SPEC = importlib.util.spec_from_file_location("revl_bench_run_autofix", RUN_PATH)
bench_run = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench_run)

SPEC_ID = "01-kv-provider"

T2_NULL = (
    "service Store { fn get() -> Str }\n\n"
    "fn first() -> Opt[Int] {\n  return null\n}\n"
)
A9_KEY = (
    "service Store {\n  fn get(k: Str) -> Str\n}\n"
    "component Cache provides db: Store {\n  provide cache {\n"
    "    fn get(k: Str) -> Str { return k }\n  }\n}\n"
)
# T2 is the only refusal the first compile reports; A9 surfaces once it is fixed.
T2_THEN_A9 = (
    "service Store {\n  fn get(k: Str) -> Str\n}\n"
    "fn first() -> Opt[Int] {\n  return null\n}\n"
    "component Cache provides db: Store {\n  provide cache {\n"
    "    fn get(k: Str) -> Str { return k }\n  }\n}\n"
)
# G1 (a mutation with no declared inverse) carries no mechanical edit.
NO_EDIT = (
    "service Store { fn get() -> Str }\n\n"
    "component Broken {\n  effect ghost.poke() undo ghost.unpoke()\n}\n"
)
CLEAN = (
    "service Store { fn get() -> Str }\n\n"
    "component Probe {\n  let m = effect Map.new() undo m.drop()\n}\n"
)


def _run(tmp_path, monkeypatch, candidates, *extra):
    bench = tmp_path / "bench"
    bench.mkdir()
    (bench / "specs.json").write_text(json.dumps({"specs": [{
        "id": SPEC_ID, "brief": "implement the service",
        "services": "service Store { fn get() -> Str }",
    }]}))
    monkeypatch.setattr(bench_run, "BENCH", bench)
    monkeypatch.setattr(bench_run, "load_variant_prompt", lambda variant: "system")
    monkeypatch.setattr(bench_run, "scoring_compiler", lambda: "test compiler")
    prompts = []

    def run_mock(system, prompt, spec, attempt):
        prompts.append(prompt)
        return {"text": f"```revl\n{candidates[attempt - 1]}```",
                "cost": 0.0, "model": "mock", "provider": "mock"}

    monkeypatch.setattr(bench_run, "run_mock", run_mock)
    monkeypatch.setattr(bench_run.sys, "argv", [
        str(RUN_PATH), "--runner", "mock", "--variants", "v2",
        "--specs", SPEC_ID, "--label", "case", "--max-iters", "2", *extra])
    bench_run.main()
    results = bench / "results" / "case"
    rows = [json.loads(line) for line in
            (results / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    attempts = [r for r in rows if not r.get("summary")]
    final = next(r for r in rows if r.get("summary"))
    summary = (results / "summary.md").read_text(encoding="utf-8")
    return prompts, attempts, final, summary, results / SPEC_ID / "v2"


def _task():
    return bench_run.OUTPUT_REMINDER.format(
        brief="implement the service",
        services="service Store { fn get() -> Str }")


def test_t2_null_goes_green_without_a_model_turn(tmp_path, monkeypatch):
    prompts, attempts, final, summary, cell = _run(
        tmp_path, monkeypatch, [T2_NULL, CLEAN])

    assert len(prompts) == 1
    assert len(attempts) == 1
    row = attempts[0]
    assert row["ok"] is True
    assert row["error"] is None
    assert [edit["code"] for edit in row["autofixed"]] == ["T2"]
    assert row["autofixed"][0]["edits"][0]["newText"] == "None"
    assert final["green_at"] == 1
    assert final["green_by_autofix"] is True
    # the model's own text stays the attempt; the patched file sits beside it
    assert (cell / "attempt-1.rvl").read_text(encoding="utf-8") == T2_NULL
    assert "return None" in (cell / "autofix-1.rvl").read_text(encoding="utf-8")
    assert "autofix: on" in summary


def test_a9_provide_key_goes_green_without_a_model_turn(tmp_path, monkeypatch):
    prompts, attempts, final, _summary, _cell = _run(
        tmp_path, monkeypatch, [A9_KEY, CLEAN])

    assert len(prompts) == 1
    assert [edit["code"] for edit in attempts[0]["autofixed"]] == ["A9"]
    assert attempts[0]["autofixed"][0]["edits"][0]["newText"] == "db"
    assert attempts[0]["ok"] is True
    assert final["green_at"] == 1


def test_edits_are_applied_one_at_a_time_until_clean(tmp_path, monkeypatch):
    prompts, attempts, final, _summary, _cell = _run(
        tmp_path, monkeypatch, [T2_THEN_A9, CLEAN])

    assert len(prompts) == 1
    assert [edit["code"] for edit in attempts[0]["autofixed"]] == ["T2", "A9"]
    assert final["green_at"] == 1


def test_error_without_an_edit_is_reprompted_unchanged(tmp_path, monkeypatch):
    prompts, attempts, final, _summary, cell = _run(
        tmp_path, monkeypatch, [NO_EDIT, CLEAN])

    first = attempts[0]
    assert first["ok"] is False
    assert first["autofixed"] == []
    assert prompts[1] == bench_run.RETRY_TEMPLATE.format(
        task=_task(), code=NO_EDIT, error=first["error"])
    assert not (cell / "autofix-1.rvl").exists()
    assert final["green_at"] == 2
    assert final["green_by_autofix"] is False


def test_partial_fix_reprompts_with_the_patched_candidate(tmp_path, monkeypatch):
    broken = T2_NULL + "\ncomponent Broken {\n  effect ghost.poke() undo ghost.unpoke()\n}\n"
    prompts, attempts, _final, _summary, _cell = _run(
        tmp_path, monkeypatch, [broken, CLEAN])

    first = attempts[0]
    assert first["ok"] is False
    assert [edit["code"] for edit in first["autofixed"]] == ["T2"]
    patched = broken.replace("return null", "return None")
    assert prompts[1] == bench_run.RETRY_TEMPLATE.format(
        task=_task(), code=patched, error=first["error"])


def test_no_autofix_flag_restores_the_plain_retry_loop(tmp_path, monkeypatch):
    prompts, attempts, final, summary, cell = _run(
        tmp_path, monkeypatch, [T2_NULL, CLEAN], "--no-autofix")

    assert len(prompts) == 2
    first = attempts[0]
    assert first["ok"] is False
    assert "autofixed" not in first
    assert "green_by_autofix" not in final
    assert prompts[1] == bench_run.RETRY_TEMPLATE.format(
        task=_task(), code=T2_NULL, error=first["error"])
    assert not (cell / "autofix-1.rvl").exists()
    assert final["green_at"] == 2
    assert "autofix: off" in summary


def test_summary_counts_cells_greened_by_autofix(tmp_path, monkeypatch):
    _prompts, _attempts, _final, summary, _cell = _run(
        tmp_path, monkeypatch, [T2_NULL, CLEAN])

    header = next(line for line in summary.splitlines()
                  if line.startswith("| variant |"))
    row = next(line for line in summary.splitlines() if line.startswith("| v2 |"))
    columns = [c.strip() for c in header.strip("|").split("|")]
    values = [c.strip() for c in row.strip("|").split("|")]
    assert columns[-1] == "green via autofix"
    assert values[-1] == "1/1"


def test_autofix_stops_at_the_edit_bound(monkeypatch):
    calls = []

    def endless_fix(code, name):
        calls.append(code)
        return {"code": "T2", "title": "t", "edits": []}, code + "x"

    monkeypatch.setattr(bench_run, "next_fix", endless_fix)
    monkeypatch.setattr(bench_run, "compile_check",
                        lambda code, name: (False, "still refused"))
    _code, ok, error, applied = bench_run.autofix(
        "start", "x.rvl", False, "refused")

    assert ok is False
    assert error == "still refused"
    assert len(applied) == bench_run.AUTOFIX_MAX_EDITS
    assert len(calls) == bench_run.AUTOFIX_MAX_EDITS


def test_next_fix_puts_the_process_revl_back():
    import sys
    import revl
    before = {m: mod for m, mod in sys.modules.items()
              if m == "revl" or m.startswith("revl.")}
    path_before = list(sys.path)

    found = bench_run.next_fix(T2_NULL, f"{SPEC_ID}.rvl")

    assert found is not None
    assert sys.modules["revl"] is revl
    after = {m: mod for m, mod in sys.modules.items()
             if m == "revl" or m.startswith("revl.")}
    assert after == before
    assert sys.path == path_before
