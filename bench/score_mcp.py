#!/usr/bin/env python3
"""Scoring for the FRAMEWORK-BENCH-1 third host: an MCP tool pack.

The `mcp` variant asks the model for the same component every other host is
asked for, written as a tool pack for `@modelcontextprotocol/sdk` 1.30.0
(`bench/prompts/mcp.md`). Like raw Cordis TypeScript it always "compiles", so
it is scored on what it leaves behind: `bench/mcp_host/probe.mjs` installs the
pack on one live `McpServer` N times, unloads it the way the framework allows
(`remove()` on every registration handle, then the teardown `install`
returned, if any), and reports which of four categories did not return to the
baseline: registry, resources, listeners, timers.

A generation is:
  - clean   no category leaked,
  - leaked  at least one category leaked,
  - error   the pack could not be probed (bad TypeScript, no `install` export,
            `install` threw, or it registered nothing).

Imported by `run.py` to score a fresh generation, and runnable standalone to
re-score a committed corpus without a model:

  python3 bench/score_mcp.py --run <label>
  python3 bench/score_mcp.py --run <label> --cycles 6 --json cells.jsonl

Prereq, once: `cd bench/mcp_host && npm ci` (the SDK and zod at the versions in
its package-lock.json) and `cd backends/typescript && npm ci` (the host
resources come from that runtime).
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
RESULTS = BENCH / "results"
HOST_DIR = BENCH / "mcp_host"
RUNTIME_TS = ROOT / "backends" / "typescript" / "runtime.ts"
# Inside the host dir, so a generated pack's bare `import 'zod'` resolves
# through bench/mcp_host/node_modules. Gitignored by the file written into it.
SCRATCH = HOST_DIR / ".scoring"

MCP_VARIANT = "mcp"
DEFAULT_CYCLES = 5
CATEGORIES = ["registry", "resources", "listeners", "timers"]
PROBE_TIMEOUT_S = 120


def prerequisites() -> list[str]:
    """What is missing before a pack can be probed, as install commands."""
    missing = []
    if not (HOST_DIR / "node_modules" / "@modelcontextprotocol" / "sdk").is_dir():
        missing.append("cd bench/mcp_host && npm ci")
    if not (ROOT / "backends" / "typescript" / "node_modules" / "cordis").is_dir():
        missing.append("cd backends/typescript && npm ci")
    return missing


def _ensure_scratch() -> None:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    (SCRATCH / "host.ts").write_text(
        "export { host } from '../../../backends/typescript/runtime.ts'\n")
    (SCRATCH / ".gitignore").write_text("*\n")


def parse_report(stdout: str, stderr: str, returncode: int, cycles: int) -> dict:
    """A scoring record from the probe's output. Never raises: a pack that the
    probe could not run is an `error`, which counts as not clean."""
    try:
        report = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        report = None
    if not isinstance(report, dict):
        msg = (stderr.strip() or stdout.strip()
               or f"probe produced no report (exit {returncode})")
        return {"status": "error", "leaked": True, "leaked_categories": [],
                "cycles": cycles, "registered": 0,
                "error": msg.splitlines()[-1][:300]}
    cats = [c for c in CATEGORIES if c in (report.get("leakedCategories") or [])]
    leaks = report.get("leaks") or {}
    return {
        "status": "leaked" if cats else "clean",
        "leaked": bool(cats),
        "leaked_categories": cats,
        "leak_detail": {c: leaks.get(c) for c in cats},
        "registered": report.get("registered", 0),
        "returned_teardown": bool(report.get("returnedTeardown")),
        "cycles": cycles,
        "error": None,
    }


def probe_source(code: str, cycles: int = DEFAULT_CYCLES,
                 name: str = "pack") -> dict:
    """Install and unload `code` N times on a live McpServer and score it."""
    missing = prerequisites()
    if missing:
        raise SystemExit("the MCP host is not installed: " + "; ".join(missing))
    _ensure_scratch()
    path = SCRATCH / f"{name}.ts"
    path.write_text(code)
    try:
        proc = subprocess.run(
            ["node", "probe.mjs", str(path), "--cycles", str(cycles), "--json"],
            capture_output=True, text=True, cwd=str(HOST_DIR),
            timeout=PROBE_TIMEOUT_S)
    except FileNotFoundError:
        raise SystemExit("node not found on PATH; the MCP probe needs Node >= 23.6")
    except subprocess.TimeoutExpired:
        return {"status": "error", "leaked": True, "leaked_categories": [],
                "cycles": cycles, "registered": 0, "error": "probe timed out"}
    return parse_report(proc.stdout, proc.stderr, proc.returncode, cycles)


def collect(run: str, attempt: int = 1) -> list[tuple[str, Path]]:
    run_dir = RESULTS / run
    if not run_dir.is_dir():
        raise SystemExit(f"no such run: {run_dir}")
    cells = []
    for spec_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        path = spec_dir / MCP_VARIANT / f"attempt-{attempt}.ts"
        if path.is_file():
            cells.append((spec_dir.name, path))
    return cells


def score_corpus(run: str, cycles: int = DEFAULT_CYCLES,
                 attempt: int = 1) -> list[dict]:
    rows = []
    for spec, path in collect(run, attempt):
        rec = probe_source(path.read_text(), cycles=cycles, name=f"{run}__{spec}")
        rows.append({"run": run, "spec": spec, "variant": MCP_VARIANT,
                     "path": path.relative_to(ROOT).as_posix(), **rec})
        print(f"  {spec}/{MCP_VARIANT}: {describe(rec)}")
    return rows


def describe(rec: dict) -> str:
    if rec["status"] == "clean":
        return "clean"
    if rec["status"] == "leaked":
        return "LEAK: " + ", ".join(rec["leaked_categories"])
    return f"error: {rec.get('error')}"


def render_summary(rows: list[dict], cycles: int) -> list[str]:
    """The MCP host's residue, stated with its n and its unload convention."""
    n = len(rows)
    if n == 0:
        return ["_no mcp cells scored._"]
    clean = [r for r in rows if r["status"] == "clean"]
    leaked = [r for r in rows if r["status"] == "leaked"]
    errored = [r for r in rows if r["status"] == "error"]
    lines = [
        f"### mcp: residue after unload ({cycles} install/unload cycles per pack)",
        "",
        f"- clean: {len(clean)}/{n}",
        f"- leaked (at least one category): {len(leaked)}/{n}",
        f"- could not be probed: {len(errored)}/{n}",
        "",
        "Unload is `remove()` on every registration handle, then the teardown "
        "`install` returned, if any. The return convention is this benchmark's, "
        "not the SDK's.",
        "",
    ]
    if leaked:
        lines += ["| category | packs leaking it |", "|---|---|"]
        for cat in CATEGORIES:
            lines.append(f"| {cat} | "
                         f"{sum(1 for r in leaked if cat in r['leaked_categories'])} |")
        lines.append("")
    for r in sorted(leaked + errored, key=lambda r: r["spec"]):
        lines.append(f"- `{r['spec']}`: {describe(r)}")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True,
                    help="run directory under bench/results holding mcp cells")
    ap.add_argument("--cycles", type=int, default=DEFAULT_CYCLES)
    ap.add_argument("--attempt", type=int, default=1)
    ap.add_argument("--json", default=None,
                    help="also write per-cell records as JSONL")
    args = ap.parse_args()
    rows = score_corpus(args.run, cycles=args.cycles, attempt=args.attempt)
    print("\n".join(render_summary(rows, args.cycles)))
    if args.json:
        Path(args.json).write_text("".join(json.dumps(r) + "\n" for r in rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
