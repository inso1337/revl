#!/usr/bin/env python3
"""raw-Cordis-TS scoring for the paradigm benchmark (roadmap item 20).

The revl variants (v1/v2/v2host) are scored on *compile-rate*: the revl
compiler refuses, at compile time, any component that would leave residue on
unload. Raw TypeScript has no such gate — a raw Cordis plugin always "compiles";
the question is what it *leaks* at runtime. So the raw-ts variant is scored on
**lifecycle correctness**, using the item-18 residue probe
(`tools/residue-probe/`) as the oracle: mount/unmount the plugin N cycles on a
real cordis runtime and read back which of the four contract categories
(registry / provisions / effects / listeners) did not return to baseline.

A raw-ts generation is:
  - clean   — probe reports zero leaked categories (what revl would compile),
  - leaked  — probe reports ≥1 leaked category (what revl would REFUSE), or
  - error   — the module could not even be mounted (probe error / bad TS).

This module is imported by `run.py` (to score a fresh raw-ts generation) and is
runnable standalone to **re-score a committed corpus for free** — the probe
calls no model and costs nothing, the raw-ts analogue of `rescore.py`:

  python3 bench/score_raw_ts.py --run <label>            # re-probe a corpus
  python3 bench/score_raw_ts.py --run <label> --cycles 8
  python3 bench/score_raw_ts.py --run <label> --write    # refresh summary.md
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
RESULTS = BENCH / "results"
PROBE_DIR = ROOT / "tools" / "residue-probe"
RUNTIME_TS = ROOT / "backends" / "typescript" / "runtime.ts"
# scratch workspace INSIDE the probe dir so a bare `import 'cordis'` from the
# generated plugin resolves through the probe's own node_modules symlink, and
# the host shim can re-export the TS backend's runtime. Gitignored.
SCRATCH = PROBE_DIR / ".scoring"

RAW_TS_VARIANT = "raw-ts"
DEFAULT_CYCLES = 5
CATEGORIES = ["registry", "provisions", "effects", "listeners"]


def _ensure_scratch() -> None:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    # host shim: the generated plugins import `{ host } from './host.ts'`
    # (pinned in prompts/raw-ts.md); re-export the real host from the TS backend
    # runtime the probe already rides. Absolute specifier so it resolves
    # regardless of scratch depth.
    (SCRATCH / "host.ts").write_text(
        f"export {{ host }} from {json.dumps(str(RUNTIME_TS))}\n"
    )
    (SCRATCH / ".gitignore").write_text("*\n")


def probe_source(code: str, cycles: int = DEFAULT_CYCLES,
                 name: str = "plugin", export: str = "plugin") -> dict:
    """Mount/unmount `code`'s `export` N cycles via the residue probe and return
    a scoring record. Never raises for a bad plugin: a load/probe failure is
    reported as status 'error', which counts as NOT clean."""
    if not (PROBE_DIR / "run.mjs").is_file():
        raise SystemExit(f"residue-probe not found at {PROBE_DIR}")
    if not RUNTIME_TS.is_file():
        raise SystemExit(f"TS backend runtime not found at {RUNTIME_TS}")
    _ensure_scratch()
    plugin_path = SCRATCH / f"{name}.ts"
    plugin_path.write_text(code)
    try:
        proc = subprocess.run(
            ["node", "run.mjs", str(plugin_path), export,
             "--cycles", str(cycles), "--json"],
            capture_output=True, text=True, cwd=str(PROBE_DIR), timeout=120,
        )
    except FileNotFoundError:
        raise SystemExit("node not found on PATH — the residue probe needs Node >= 23.6")
    except subprocess.TimeoutExpired:
        return {"status": "error", "leaked": True, "leaked_categories": [],
                "cycles": cycles, "error": "probe timed out"}

    report = None
    for chunk in (proc.stdout, proc.stdout.strip()):
        try:
            report = json.loads(chunk)
            break
        except (json.JSONDecodeError, ValueError):
            continue
    if report is None:
        # exit 2 / no JSON: the plugin could not be mounted (bad TS, threw on
        # import, wrong export). Not clean.
        msg = (proc.stderr.strip() or proc.stdout.strip() or
               f"probe produced no report (exit {proc.returncode})")
        return {"status": "error", "leaked": True, "leaked_categories": [],
                "cycles": cycles, "error": msg.splitlines()[-1][:300]}

    leaked = bool(report.get("leaked"))
    cats = report.get("leakedCategories") or []
    leaks = report.get("leaks") or {}
    detail = {c: leaks.get(c, {}).get("detail") for c in cats}
    return {"status": "leaked" if leaked else "clean",
            "leaked": leaked, "leaked_categories": cats,
            "leak_detail": detail, "cycles": cycles, "error": None}


def collect_raw_ts(run: str, attempt: int = 1) -> list[tuple[str, Path]]:
    run_dir = RESULTS / run
    if not run_dir.is_dir():
        raise SystemExit(f"no such run: {run_dir}")
    cells = []
    for spec_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        path = spec_dir / RAW_TS_VARIANT / f"attempt-{attempt}.ts"
        if path.is_file():
            cells.append((spec_dir.name, path))
    return cells


def score_corpus(run: str, cycles: int = DEFAULT_CYCLES,
                 attempt: int = 1) -> list[dict]:
    rows = []
    for spec, path in collect_raw_ts(run, attempt):
        rec = probe_source(path.read_text(), cycles=cycles, name=f"{run}__{spec}")
        rows.append({"run": run, "spec": spec, "variant": RAW_TS_VARIANT,
                     "path": str(path.relative_to(ROOT)), **rec})
        flag = {"clean": "clean", "leaked": "LEAK", "error": "error"}[rec["status"]]
        extra = (" — " + ", ".join(rec["leaked_categories"])) if rec["leaked_categories"] \
            else (f" — {rec['error']}" if rec.get("error") else "")
        print(f"  {spec}/raw-ts: {flag}{extra}")
    return rows


def render_raw_ts_summary(rows: list[dict], cycles: int) -> list[str]:
    """The headline: what fraction of raw-TS attempts carry residue revl would
    have refused at compile time."""
    n = len(rows)
    if n == 0:
        return ["_no raw-ts cells scored._"]
    clean = [r for r in rows if r["status"] == "clean"]
    leaked = [r for r in rows if r["status"] == "leaked"]
    errored = [r for r in rows if r["status"] == "error"]
    # "residue-carrying" = leaked OR could-not-mount; both are what revl's
    # compile gate keeps out. Report both, and the pure-leak number too.
    carry = leaked + errored
    pct = 100 * len(carry) / n
    lines = [
        f"### raw-ts — lifecycle correctness (residue probe, {cycles} cycles/plugin)",
        "",
        f"**{len(carry)}/{n} ({pct:.0f}%) of raw-TS attempts carry residue that "
        f"revl would have refused at compile time.**",
        "",
        f"- clean (no residue — what revl compiles): {len(clean)}/{n} "
        f"({100 * len(clean) / n:.0f}%)",
        f"- leaked (≥1 category — revl would refuse): {len(leaked)}/{n}",
        f"- failed to mount (bad plugin — also refused): {len(errored)}/{n}",
        "",
    ]
    # per-category leak tally
    cat_counts = {c: sum(1 for r in leaked if c in r["leaked_categories"])
                  for c in CATEGORIES}
    if leaked:
        lines += ["| leaked category | count | contract (run.py) |",
                  "|---|---|---|",
                  f"| registry | {cat_counts['registry']} | `root.registry.size == 0` |",
                  f"| provisions | {cat_counts['provisions']} | `root.reflect.store == {{}}` |",
                  f"| effects | {cat_counts['effects']} | `root.fiber._disposables == base` |",
                  f"| listeners | {cat_counts['listeners']} | `root.events._hooks == base` |",
                  ""]
    if carry:
        lines.append("Residue-carrying cells:")
        lines.append("")
        for r in sorted(carry, key=lambda r: r["spec"]):
            if r["status"] == "leaked":
                detail = ", ".join(r["leaked_categories"])
                lines.append(f"- `{r['spec']}` — leaked: {detail}")
            else:
                lines.append(f"- `{r['spec']}` — did not mount: {r['error']}")
        lines.append("")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True,
                    help="run directory under bench/results (with raw-ts cells)")
    ap.add_argument("--cycles", type=int, default=DEFAULT_CYCLES,
                    help=f"mount/unmount cycles per plugin (default {DEFAULT_CYCLES})")
    ap.add_argument("--attempt", type=int, default=1)
    ap.add_argument("--json", default=None, help="also write per-cell records as JSONL")
    ap.add_argument("--write", action="store_true",
                    help="write results/<run>/summary.md (else print only)")
    args = ap.parse_args()

    rows = score_corpus(args.run, cycles=args.cycles, attempt=args.attempt)
    summary = render_raw_ts_summary(rows, args.cycles)
    header = [f"# raw-ts re-score — {args.run}",
              f"probe: `tools/residue-probe` · reproduce: "
              f"`python3 bench/score_raw_ts.py --run {args.run} --cycles {args.cycles}`",
              ""]
    text = "\n".join(header + summary) + "\n"
    print("\n" + text)
    if args.json:
        Path(args.json).write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    if args.write:
        (RESULTS / args.run / "summary.md").write_text(text)
        print(f"wrote {RESULTS / args.run / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
