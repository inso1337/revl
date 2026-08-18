#!/usr/bin/env python3
"""Re-score committed benchmark generations against the current compiler.

This is *not* a model run. It costs nothing and calls no provider: it takes
the `.rvl` files already committed under `bench/results/<run>/` and compiles
each one with the checker in this working tree, so that a change to the
language can be measured against a fixed corpus of generations.

The headline number is **first-pass compile rate** — `attempt-1.rvl` only,
the file the model produced before it ever saw a compiler message. Later
attempts are the error-feedback loop and are not what this measures.

Usage:
  python3 bench/rescore.py                                   # default run
  python3 bench/rescore.py --run baseline-deepseek-v4-pro
  python3 bench/rescore.py --run all
  python3 bench/rescore.py --attempt 1 --json out.jsonl
  python3 bench/rescore.py --compiler-root DIR               # score another tree

Every table it prints names the compiler commit it was measured at, so a
published number can always be traced back to a sha.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
RESULTS = BENCH / "results"

# runs produced by a real model; the `mock-*`/`pilot-*` directories are
# pipeline self-checks and partial pilots, not scoreable corpora.
MODEL_RUNS = ["typed-deepseek-v4-pro", "baseline-deepseek-v4-pro"]
VARIANTS = ["v1", "v2", "v2host"]


def compiler_sha(root: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=30)
        sha = out.stdout.strip() or "unknown"
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain",
                                "--", "src/revl"],
                               capture_output=True, text=True, timeout=30)
        return sha + ("-dirty" if dirty.stdout.strip() else "")
    except Exception:
        return "unknown"


def load_compiler(root: Path):
    src = str(root / "src")
    if src in sys.path:
        sys.path.remove(src)
    sys.path.insert(0, src)
    for mod in [m for m in list(sys.modules) if m == "revl" or m.startswith("revl.")]:
        del sys.modules[mod]
    from revl import RevlError, compile_source  # noqa: PLC0415
    from revl.diagnostics import classify  # noqa: PLC0415
    return compile_source, RevlError, classify


def score_one(path: Path, compile_source, RevlError, classify) -> dict:
    code = path.read_text()
    try:
        compile_source(code, path.parent.parent.name + ".rvl")
        return {"ok": True}
    except RevlError as exc:
        record = classify(exc)
        return {"ok": False, "code": record["code"], "category": record["category"],
                "message": record["message"], "line": record.get("line")}
    except Exception as exc:  # a crash is a failure, and a compiler bug
        return {"ok": False, "code": "CRASH", "category": "compiler-crash",
                "message": f"{type(exc).__name__}: {exc}", "line": None}


def collect(run: str, attempt: int) -> list[tuple[str, str, Path]]:
    run_dir = RESULTS / run
    if not run_dir.is_dir():
        raise SystemExit(f"no such run: {run_dir}")
    cells = []
    for spec_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        for variant in VARIANTS:
            path = spec_dir / variant / f"attempt-{attempt}.rvl"
            if path.is_file():
                cells.append((spec_dir.name, variant, path))
    return cells


def render(run: str, rows: list[dict], sha: str, attempt: int) -> list[str]:
    lines = [f"## {run} — attempt-{attempt} re-score",
             f"compiler: `{sha}`  ·  reproduce: `python3 bench/rescore.py --run {run}`",
             "",
             "| variant | first-pass compile |",
             "|---|---|"]
    for variant in VARIANTS:
        vs = [r for r in rows if r["variant"] == variant]
        if not vs:
            continue
        ok = sum(1 for r in vs if r["ok"])
        lines.append(f"| {variant} | {ok}/{len(vs)} ({100 * ok / len(vs):.0f}%) |")

    fails = [r for r in rows if not r["ok"]]
    lines += ["", f"### failure taxonomy ({len(fails)} of {len(rows)} cells)", ""]
    if not fails:
        lines.append("_no failures._")
        return lines
    lines += ["| code | category | count | variants | example |", "|---|---|---|---|---|"]
    buckets = Counter((r["code"], r["category"]) for r in fails)
    for (code, category), count in buckets.most_common():
        members = [r for r in fails if (r["code"], r["category"]) == (code, category)]
        spread = ", ".join(
            f"{v}×{sum(1 for m in members if m['variant'] == v)}"
            for v in VARIANTS if any(m["variant"] == v for m in members))
        example = members[0]["message"].splitlines()[0]
        example = example.replace("|", "\\|")
        if len(example) > 90:
            example = example[:87] + "..."
        lines.append(f"| {code} | {category} | {count} | {spread} | {example} |")
    lines += ["", "Failing cells:", ""]
    for r in sorted(fails, key=lambda r: (r["variant"], r["spec"])):
        lines.append(f"- `{r['variant']}/{r['spec']}` — {r['code']}/{r['category']}: "
                     f"{r['message'].splitlines()[0]}")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", default="typed-deepseek-v4-pro",
                    help="run directory under bench/results, or 'all' for "
                         f"{' + '.join(MODEL_RUNS)}")
    ap.add_argument("--attempt", type=int, default=1,
                    help="which attempt to score (default 1 — the first-pass number)")
    ap.add_argument("--compiler-root", default=None,
                    help="score against <dir>/src/revl instead of this tree")
    ap.add_argument("--json", default=None, help="also write per-cell records as JSONL")
    args = ap.parse_args()

    root = Path(args.compiler_root).resolve() if args.compiler_root else ROOT
    if not (root / "src" / "revl").is_dir():
        raise SystemExit(f"no src/revl under {root}")
    compile_source, RevlError, classify = load_compiler(root)
    sha = compiler_sha(root)

    runs = MODEL_RUNS if args.run == "all" else [args.run]
    out, all_rows = [], []
    for run in runs:
        rows = []
        for spec, variant, path in collect(run, args.attempt):
            result = score_one(path, compile_source, RevlError, classify)
            rows.append({"run": run, "spec": spec, "variant": variant,
                         "path": str(path.relative_to(ROOT)), **result})
        all_rows += rows
        out += render(run, rows, sha, args.attempt) + [""]

    print("\n".join(out))
    if args.json:
        Path(args.json).write_text("\n".join(json.dumps(r) for r in all_rows) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
