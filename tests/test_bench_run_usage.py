"""Issue #2612: keep model turns beside input-token usage in bench records."""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUN_PATH = ROOT / "bench" / "run.py"
SPEC = importlib.util.spec_from_file_location("revl_bench_run_2612", RUN_PATH)
bench_run = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench_run)


def test_cline_runner_records_input_tokens_and_model_iterations(monkeypatch):
    fixture = ROOT / "tests" / "fixtures" / "cline-run-result.jsonl"
    completed = subprocess.CompletedProcess(
        args=["cline"], returncode=0, stdout=fixture.read_text(), stderr=""
    )
    monkeypatch.setattr(bench_run.subprocess, "run", lambda *args, **kwargs: completed)

    row = bench_run.run_cline("system", "prompt", "model-x", None, 30, False)

    assert row["tokens_in"] == 5022
    assert row["turn_count"] == 1
    assert row["output_tokens"] == 28
    assert row["cost"] == 0.00071092


@pytest.mark.parametrize(("usage", "expected"), [
    ({"inputTokens": -1, "tokensIn": 7}, 7),
    ({"input_tokens": 17}, 17),
    ({"totalTokensIn": 23}, 23),
    ({"inputTokens": True}, None),
    ({}, None),
])
def test_usage_counts_leave_missing_or_invalid_values_unavailable(usage, expected):
    assert bench_run._input_tokens(usage) == expected
    assert bench_run._turn_count(None) is None
    assert bench_run._turn_count(True) is None
    assert bench_run._turn_count(0) is None


def test_run_summary_reports_turn_count_and_tokens_in_per_invocation(tmp_path, monkeypatch):
    monkeypatch.setattr(bench_run, "scoring_compiler", lambda: "test compiler")
    rows = [
        {"spec": "01-kv-provider", "variant": "v2", "attempt": 1,
         "turn_count": 4, "tokens_in": 500, "output_tokens": 120},
        {"spec": "01-kv-provider", "variant": "v2", "summary": True,
         "green_at": 1, "cost_total": 0.25, "tokens_to_green": 120,
         "turn_count_to_green": 4, "tokens_in_to_green": 500},
        {"spec": "probe", "variant": "raw-ts", "attempt": 1,
         "summary": True, "turn_count": 8, "tokens_in": 900},
    ]
    args = SimpleNamespace(
        runner="cline", model="model-x", max_iters=3, variants="v2",
        raw_ts_cycles=6,
    )

    bench_run.write_summary(tmp_path, rows, [], args)

    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "mean model turns" in summary
    assert "mean tokens_in" in summary
    assert "## Model turns and input tokens per invocation" in summary
    assert "| 01-kv-provider | v2 | 1 | 4 | 500 | 125 |" in summary
    assert "| probe | raw-ts |" not in summary


def _prepare_main_case(tmp_path, monkeypatch, replies, compile_results,
                       *, resume=False, existing_rows=None):
    bench = tmp_path / "bench"
    bench.mkdir()
    (bench / "specs.json").write_text(json.dumps({"specs": [{
        "id": "01-kv-provider", "brief": "implement the service",
        "services": "service Store { fn get() -> Str }",
    }]}))
    (bench / "results").mkdir()
    results = bench / "results" / "case"
    results.mkdir()
    if existing_rows:
        (results / "results.jsonl").write_text(
            "\n".join(json.dumps(row) for row in existing_rows) + "\n")

    monkeypatch.setattr(bench_run, "BENCH", bench)
    monkeypatch.setattr(bench_run, "load_variant_prompt", lambda variant: "system")
    monkeypatch.setattr(bench_run, "scoring_compiler", lambda: "test compiler")
    reply_iter = iter(replies)

    def run_cline(*_args):
        reply = next(reply_iter)
        if isinstance(reply, Exception):
            raise reply
        return reply

    result_iter = iter(compile_results)
    monkeypatch.setattr(bench_run, "run_cline", run_cline)
    monkeypatch.setattr(bench_run, "compile_check",
                        lambda *_args: next(result_iter))
    argv = [str(RUN_PATH), "--runner", "cline", "--variants", "v2",
            "--specs", "01-kv-provider", "--label", "case", "--max-iters", "2"]
    if resume:
        argv.append("--resume")
    monkeypatch.setattr(bench_run.sys, "argv", argv)

    bench_run.main()
    rows = [json.loads(line) for line in
            (results / "results.jsonl").read_text().splitlines()]
    return rows, (results / "summary.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(("reply", "compile_results", "expected_means"), [
    ({"text": "```revl\ncomponent Good {}\n```", "tokens_in": 500,
      "turn_count": 4, "output_tokens": 120, "cost": 0.25},
     [(True, None)], ("4.00", "500")),
    ({"text": "```revl\ncomponent Good {}\n```", "output_tokens": 120},
     [(True, None)], (r"\u2014", r"\u2014")),
    ({"text": "```revl\ncomponent Good {}\n```", "tokens_in": 500,
      "output_tokens": 120}, [(True, None)], (r"\u2014", r"\u2014")),
    ({"text": "```revl\ncomponent StillBad {}\n```", "tokens_in": 500,
      "turn_count": 4, "output_tokens": 120},
     [(False, "rejected"), (False, "rejected")],
     (r"\u2014", r"\u2014")),
])
def test_main_summary_keeps_paired_usage_only_for_admitted_cells(
        tmp_path, monkeypatch, reply, compile_results, expected_means):
    replies = [reply] * len(compile_results)
    rows, summary = _prepare_main_case(
        tmp_path, monkeypatch, replies, compile_results)

    variant_row = next(line for line in summary.splitlines()
                       if line.startswith("| v2 |"))
    columns = [column.strip() for column in variant_row.strip("|").split("|")]
    means = tuple(column.encode("unicode_escape").decode("ascii")
                  for column in columns[5:7])
    assert means == expected_means
    final = next(row for row in rows if row.get("summary"))
    if compile_results[0][0]:
        assert final["green_at"] == 1
    else:
        assert final["green_at"] is None


def test_main_runner_error_records_unavailable_usage_and_does_not_mark_green(
        tmp_path, monkeypatch):
    rows, summary = _prepare_main_case(
        tmp_path, monkeypatch, [RuntimeError("runner stopped")], [])

    variant_row = next(line for line in summary.splitlines()
                       if line.startswith("| v2 |"))
    columns = [column.strip() for column in variant_row.strip("|").split("|")]
    means = tuple(column.encode("unicode_escape").decode("ascii")
                  for column in columns[5:7])
    assert means == (r"\u2014", r"\u2014")
    final = next(row for row in rows if row.get("summary"))
    assert final["green_at"] is None
    assert final["turn_count_to_green"] is None
    assert final["tokens_in_to_green"] is None


def test_resume_replaces_interrupted_attempt_rows(tmp_path, monkeypatch):
    stale = {"spec": "01-kv-provider", "variant": "v2", "attempt": 1,
             "ok": False, "turn_count": 99, "tokens_in": 999}
    reply = {"text": "```revl\ncomponent Good {}\n```", "tokens_in": 500,
             "turn_count": 4, "output_tokens": 120}
    rows, summary = _prepare_main_case(
        tmp_path, monkeypatch, [reply], [(True, None)],
        resume=True, existing_rows=[stale])

    attempts = [row for row in rows if not row.get("summary")]
    assert len(attempts) == 1
    assert attempts[0]["tokens_in"] == 500
    assert attempts[0]["turn_count"] == 4
    assert "| 4.00 | 500 |" in summary
