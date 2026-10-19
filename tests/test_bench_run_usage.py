"""Issue #2612: keep model turns beside input-token usage in bench records."""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
RUN_PATH = ROOT / "bench" / "run.py"
SPEC = importlib.util.spec_from_file_location("revl_bench_run_2612", RUN_PATH)
bench_run = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench_run)


def test_cline_runner_records_input_tokens_and_model_iterations(monkeypatch):
    result = {
        "type": "run_result",
        "text": "```revl\ncomponent Good {}\n```",
        "iterations": 4,
        "aggregateUsage": {
            "inputTokens": 500,
            "outputTokens": 120,
            "totalCost": 0.25,
        },
        "model": {"id": "model-x", "provider": "provider-y"},
    }
    completed = subprocess.CompletedProcess(
        args=["cline"], returncode=0, stdout=json.dumps(result), stderr=""
    )
    monkeypatch.setattr(bench_run.subprocess, "run", lambda *args, **kwargs: completed)

    row = bench_run.run_cline("system", "prompt", "model-x", None, 30, False)

    assert row["tokens_in"] == 500
    assert row["turn_count"] == 4
    assert row["output_tokens"] == 120
    assert row["cost"] == 0.25


def test_usage_counts_leave_missing_or_invalid_values_unavailable():
    assert bench_run._input_tokens({}) is None
    assert bench_run._input_tokens({"inputTokens": -1, "tokensIn": 7}) == 7
    assert bench_run._input_tokens({"inputTokens": True}) is None
    assert bench_run._turn_count(None) is None
    assert bench_run._turn_count(True) is None


def test_run_summary_reports_turn_count_and_tokens_in_per_invocation(tmp_path, monkeypatch):
    monkeypatch.setattr(bench_run, "scoring_compiler", lambda: "test compiler")
    rows = [
        {"spec": "01-kv-provider", "variant": "v2", "attempt": 1,
         "turn_count": 4, "tokens_in": 500, "output_tokens": 120},
        {"spec": "01-kv-provider", "variant": "v2", "summary": True,
         "green_at": 1, "cost_total": 0.25, "tokens_to_green": 120,
         "turn_count_to_green": 4, "tokens_in_to_green": 500},
    ]
    args = SimpleNamespace(
        runner="cline", model="model-x", max_iters=3, variants="v2",
        raw_ts_cycles=6,
    )

    bench_run.write_summary(tmp_path, rows, [], args)

    summary = (tmp_path / "summary.md").read_text()
    assert "mean model turns" in summary
    assert "mean tokens_in" in summary
    assert "## Model turns and input tokens per invocation" in summary
    assert "| 01-kv-provider | v2 | 1 | 4 | 500 | 125 |" in summary
