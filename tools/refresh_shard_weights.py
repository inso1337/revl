#!/usr/bin/env python3
"""Refresh tests/shard_weights.json from a sharded CI run (issue #1774).

Each `root-suite-affected (k)` job of a FULL selection ends by printing one
`REVL_SHARD_SECONDS <seconds> <file>` line per test file it ran
(tests/conftest.py). This reads those lines back, from the run's job logs or
from saved log files, and writes them over the recorded weights. A file the run
did not time keeps its old weight; a file that no longer exists is dropped.

    python tools/refresh_shard_weights.py --run 37126735065
    python tools/refresh_shard_weights.py --log shard1.log shard2.log ...

Without --write it only prints what it found and the predicted shard loads.
The weights only balance the shards; they never decide which tests run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import _shard  # noqa: E402

JOB_PREFIX = "root-suite-affected ("


def _gh(repo: str, path: str) -> str:
    return subprocess.run(["gh", "api", f"repos/{repo}/{path}"], check=True,
                          capture_output=True, text=True).stdout


def run_logs(repo: str, run: str) -> dict:
    """job name -> log text, for every root-suite-affected shard of `run`."""
    jobs = json.loads(_gh(repo, f"actions/runs/{run}/jobs?per_page=100"))["jobs"]
    shards = [j for j in jobs if j["name"].startswith(JOB_PREFIX)]
    return {j["name"]: _gh(repo, f"actions/jobs/{j['id']}/logs") for j in shards}


def measure(logs: dict) -> dict:
    measured = {}
    for name in sorted(logs):
        found = _shard.parse_seconds(logs[name])
        print(f"{name}: {len(found)} file(s), {sum(found.values()):.0f}s")
        measured.update(found)
    return measured


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--run", help="a CI run id whose shards ran a FULL selection")
    src.add_argument("--log", nargs="+", type=Path, help="saved shard job logs")
    ap.add_argument("--repo", default="inso1337/revl")
    ap.add_argument("--write", action="store_true", help="rewrite the weights file")
    args = ap.parse_args(argv)

    if args.run:
        logs, origin = run_logs(args.repo, args.run), f"CI run {args.run}"
    else:
        logs = {str(p): p.read_text(encoding="utf-8") for p in args.log}
        origin = "saved CI shard logs"
    measured = measure(logs)
    if not measured:
        print("no REVL_SHARD_SECONDS lines: was the selection FULL and did a shard finish?")
        return 1

    doc = json.loads(_shard.WEIGHTS.read_text(encoding="utf-8"))
    note = (f"Issue #1774: per-file seconds that balance REVL_TEST_SHARD (tests/_shard.py). "
            f"{len(measured)} files were timed in {origin} (setup + call + teardown); a file "
            f"it did not time keeps its earlier weight. Only shard balance depends on these "
            f"numbers, never which tests run. Refresh: tools/refresh_shard_weights.py --run <id>.")
    new = _shard.refreshed(doc, measured, note, keep=lambda f: (ROOT / f).is_file())
    files = list(new["seconds"])
    loads = _shard.loads(files, new["seconds"], 4)
    print("predicted shard seconds: " + ", ".join(f"{x:.0f}" for x in loads))
    if args.write:
        _shard.WEIGHTS.write_text(json.dumps(new, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {_shard.WEIGHTS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
