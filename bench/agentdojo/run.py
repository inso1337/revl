#!/usr/bin/env python3
"""Baseline vs admitted-revl on AgentDojo (issue #2683).

One command runs both arms over the same user tasks, injection tasks, models and
seeds, and appends one raw record per task to
``bench/results/agentdojo/<label>/records.jsonl``::

    python3 bench/agentdojo/run.py --suite banking \\
        --arms baseline,revl --models flint-bench:latest,ornith-revl:latest \\
        --runs 2 --tasks user_task_0,user_task_1 \\
        --injection-tasks injection_task_0 --label pilot-banking

Requirements: AgentDojo 0.1.35, a revl checkout whose interpreter can import
``cordis`` (``sh backends/python/setup.sh`` installs both), and an
OpenAI-compatible endpoint with the pinned models. The port records the model
digest it ran against; a model name without a digest is not a pin.

The baseline arm is AgentDojo's own reference pipeline. The revl arm is
``bench.agentdojo.pipelines.RevlPipeline``: one admitted revl component per tool
call, compiled under the untrusted-author profile against only the suite's own
tools, then run on a live session. Both arms are scored by AgentDojo's task
checks on the environment they produced.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parents[1]
RESULTS = ROOT / "bench" / "results" / "agentdojo"
sys.path.insert(0, str(ROOT / "src"))

SCHEMA = "AGENTDOJO-1"


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:
        return None


def _client(base_url: str) -> object:
    import openai

    return openai.OpenAI(base_url=base_url, api_key=os.getenv("OPENAI_API_KEY", "EMPTY"), timeout=900)


def _pins(base_url: str, models: list[str]) -> dict[str, str | None]:
    """Digest per model, read from the endpoint's own catalogue."""
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    try:
        with urllib.request.urlopen(root + "/api/tags", timeout=15) as fh:
            tags = json.load(fh)["models"]
    except Exception:
        return {m: None for m in models}
    by_name = {m["name"]: m.get("digest") for m in tags}
    return {m: by_name.get(m) for m in models}


def _load_agentdojo():
    import agentdojo
    from agentdojo.attacks import load_attack
    from agentdojo.task_suite.load_suites import get_suite

    return agentdojo, load_attack, get_suite


def _key(record: dict) -> tuple:
    return (
        record["arm"],
        record["model"],
        record["run"],
        record["task"],
        record["condition"],
        record["injection_task"],
        record.get("attack"),
    )


def _existing(path: Path) -> set[tuple]:
    done: set[tuple] = set()
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            done.add(_key(json.loads(line)))
        except Exception:
            continue
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite", default="banking")
    parser.add_argument("--arms", default="baseline,revl")
    parser.add_argument("--models", required=True, help="comma-separated endpoint model names")
    parser.add_argument("--runs", type=int, default=1, help="independent runs per (task, condition)")
    parser.add_argument("--tasks", default="", help="comma-separated user task ids (default: all)")
    parser.add_argument("--task-limit", type=int, default=0, help="take the first N user tasks")
    parser.add_argument(
        "--injection-tasks", default="", help="comma-separated injection task ids (default: all)"
    )
    parser.add_argument("--conditions", default="benign,attack")
    parser.add_argument("--attack", default="important_instructions")
    parser.add_argument("--label", required=True, help="results subdirectory")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-steps", type=int, default=6)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--max-repairs", type=int, default=2)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    conditions = [c.strip() for c in args.conditions.split(",") if c.strip()]
    outdir = RESULTS / args.label
    outdir.mkdir(parents=True, exist_ok=True)
    records_path = outdir / "records.jsonl"
    meta_path = outdir / "meta.json"

    _, load_attack, get_suite = _load_agentdojo()
    import agentdojo

    suite = get_suite("v1.2", args.suite)
    if args.tasks:
        wanted = [t.strip() for t in args.tasks.split(",") if t.strip()]
        user_tasks = [suite.get_user_task_by_id(t) for t in wanted]
    else:
        user_tasks = list(suite.user_tasks.values())
        if args.task_limit:
            user_tasks = user_tasks[: args.task_limit]
    if args.injection_tasks:
        injection_ids = [t.strip() for t in args.injection_tasks.split(",") if t.strip()]
    else:
        injection_ids = list(suite.injection_tasks.keys())
    injection_tasks = [suite.get_injection_task_by_id(t) for t in injection_ids]

    client = _client(args.base_url)
    pins = _pins(args.base_url, models)
    meta = {
        "schema": SCHEMA,
        "started": _now(),
        "suite": args.suite,
        "agentdojo": getattr(agentdojo, "__version__", "0.1.35"),
        "git_commit": _git_commit(),
        "arms": arms,
        "models": models,
        "pins": pins,
        "runs": args.runs,
        "temperature": args.temperature,
        "max_steps": args.max_steps,
        "max_tokens": args.max_tokens,
        "tasks": [t.ID for t in user_tasks],
        "injection_tasks": injection_ids,
        "conditions": conditions,
        "command": " ".join(sys.argv),
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    import importlib.util

    pkg_spec = importlib.util.spec_from_file_location(
        "bench_agentdojo", BENCH / "__init__.py", submodule_search_locations=[str(BENCH)]
    )
    pkg = importlib.util.module_from_spec(pkg_spec)
    sys.modules["bench_agentdojo"] = pkg
    pkg_spec.loader.exec_module(pkg)
    from bench_agentdojo import pipelines

    def pipeline_for(arm: str, model: str):
        if arm == "baseline":
            return pipelines.baseline_pipeline(client, model, temperature=args.temperature)
        if arm == "revl":
            return pipelines.RevlPipeline(
                client,
                model,
                args.suite,
                list(suite.tools),
                max_steps=args.max_steps,
                max_repairs=args.max_repairs,
                max_tokens=args.max_tokens,
                temperature=args.temperature,
            )
        raise SystemExit(f"unknown arm {arm!r}")

    done = _existing(records_path) if args.resume else set()
    total = 0
    with records_path.open("a", encoding="utf-8") as out:
        def record(**fields: object) -> None:
            nonlocal total
            row = {"schema": SCHEMA, "suite": args.suite, "at": _now(), **fields}
            if _key(row) in done:
                return
            out.write(json.dumps(row, default=str) + "\n")
            out.flush()
            total += 1
            print(
                f"{row['arm']:8s} {row['model']:24s} run{row['run']} "
                f"{row['task']:14s} {row['condition']:6s} "
                f"{row.get('injection_task') or '-':18s} "
                f"U={row.get('utility')} S={row.get('security')} "
                f"{row.get('duration_s')}s",
                flush=True,
            )

        for model in models:
            for run in range(args.runs):
                for arm in arms:
                    pipeline = pipeline_for(arm, model)
                    # The appended attack text names the model from AgentDojo's
                    # fixed catalogue, not the endpoint tag. Keep that wording
                    # the same for both arms so an attack difference is not a
                    # wording difference; the real model and digest are in the
                    # record.
                    pipeline.name = "local"
                    for user_task in user_tasks:
                        for condition in conditions:
                            if condition == "benign":
                                started = time.time()
                                try:
                                    utility, security = suite.run_task_with_pipeline(
                                        pipeline, user_task, None, {}
                                    )
                                    error = None
                                except Exception as exc:  # noqa: BLE001
                                    utility = security = None
                                    error = f"{type(exc).__name__}: {exc}".replace(
                                        os.getcwd(), "<checkout>"
                                    )
                                    traceback.print_exc()
                                stats = dict(getattr(pipeline, "stats", {}) or {})
                                record(
                                    arm=arm,
                                    model=model,
                                    model_digest=pins.get(model),
                                    run=run,
                                    task=user_task.ID,
                                    condition="benign",
                                    injection_task=None,
                                    attack=None,
                                    utility=utility,
                                    security=None,
                                    duration_s=round(time.time() - started, 1),
                                    stats=stats,
                                    error=error,
                                )
                                continue
                            attack = load_attack(args.attack, suite, pipeline)
                            for injection_task in injection_tasks:
                                injections = attack.attack(user_task, injection_task)
                                started = time.time()
                                try:
                                    utility, security = suite.run_task_with_pipeline(
                                        pipeline, user_task, injection_task, injections
                                    )
                                    error = None
                                except Exception as exc:  # noqa: BLE001
                                    utility = security = None
                                    error = f"{type(exc).__name__}: {exc}".replace(
                                        os.getcwd(), "<checkout>"
                                    )
                                    traceback.print_exc()
                                stats = dict(getattr(pipeline, "stats", {}) or {})
                                record(
                                    arm=arm,
                                    model=model,
                                    model_digest=pins.get(model),
                                    run=run,
                                    task=user_task.ID,
                                    condition="attack",
                                    injection_task=injection_task.ID,
                                    attack=args.attack,
                                    utility=utility,
                                    security=security,
                                    duration_s=round(time.time() - started, 1),
                                    stats=stats,
                                    error=error,
                                )
    meta["finished"] = _now()
    meta["records"] = total
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {total} records to {records_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
