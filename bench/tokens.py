#!/usr/bin/env python3
"""Tokens-to-green — the token economy's measurement (roadmap item 50).

`run.py`/`rescore.py` record **iterations-to-green**: how many compiler
round-trips a component needed before it was admitted. That counts *turns*.
It does not count *spend*: a two-iteration component that emitted 900 output
tokens across its two drafts cost more than a three-iteration component that
emitted 300. The token economy's house rule is "measured, not assumed", so
before any optimization (compound MCP verbs, a terser wire-form, structured
edits) can claim it pays, there has to be a number it moves.

That number is **output tokens spent per admitted component** — every token the
model emitted, across *all* attempts, up to and including the one that was
admitted. Retries are not free; a refused draft still cost the tokens it took to
write. This module computes it deterministically from the committed corpus,
exactly the way `rescore.py`/`demand.py` recompute their numbers — no model, no
provider, no cost.

## What is real here, and what needs a funded run

The committed `results.jsonl` records a real per-attempt **cost** (dollars cline
was billed, from the provider's own usage) but it does *not* record the token
counts behind that cost — `run.py` only kept `totalCost`. So this module reports
two things, and is explicit about which is which:

  * **est. output tokens** — a DETERMINISTIC PROXY tokenised from the committed
    generation files (the exact source each attempt emitted, saved under
    `results/<run>/<spec>/<variant>/attempt-N.rvl`). The artefact is real; the
    tokeniser is a BPE-shaped proxy (see `count_tokens`), because the model's
    own output-token count was never recorded. A funded run now records the
    exact figure — `run.py` captures `output_tokens` from cline usage into every
    row, and this module prefers that recorded value wherever it is present.
  * **as-run cost-to-green** — the REAL dollars from the committed `cost_total`,
    surfaced alongside as the un-proxied corroborating signal.

Green is recomputed against the CURRENT checker (like `rescore.py`): the first
committed attempt that compiles is the admitted one, and tokens-to-green sums
attempts 1..that. So the number tracks the language as it is today, and stays in
step with the iterations-to-green it sits beside.

Usage:
  python3 bench/tokens.py                       # both model runs
  python3 bench/tokens.py --run typed-deepseek-v4-pro
  python3 bench/tokens.py --json tokens.json    # machine-readable
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

# reuse rescore's corpus loader + per-file scorer, so tokens-to-green is scored
# against the compiler the very same way first-pass compile rate is.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rescore  # noqa: E402


def _load_compiler(root: Path):
    """The compiler entry points, in step with `rescore.py` — but importing
    `revl` IN PLACE for the live in-tree root, exactly as `demand.py` does.

    `rescore.load_compiler` purges and re-imports `revl` to be able to swap to a
    foreign `--compiler-root`. Doing that against the *same* tree while running
    inside a test suite would evict a `revl` other tests already imported out
    from under the references they hold. So for the in-tree root we import in
    place; only a foreign root takes the purge-and-reload path.
    """
    if root == rescore.ROOT:
        # make `revl` importable from a bare checkout (no editable install)
        # WITHOUT purging already-imported modules — appending to sys.path is
        # safe; the purge-and-reload is only rescore's foreign-tree path.
        src = str(root / "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        from revl import RevlError, compile_source  # noqa: PLC0415
        from revl.diagnostics import classify  # noqa: PLC0415
        return compile_source, RevlError, classify
    return rescore.load_compiler(root)


# -- the tokeniser ----------------------------------------------------------
#
# A deterministic, dependency-free BPE-shaped proxy. It is NOT the model's
# tokeniser (that lives behind the provider and its output-token count was never
# recorded for these corpora); it is a fixed function that approximates a
# byte-BPE encoder closely enough to rank and to track a corpus over time:
#
#   1. GPT-style pre-tokenisation — the same word / number / punctuation /
#      whitespace split byte-BPE tokenisers run first.
#   2. Each pre-token costs ceil(len/4) tokens, floored at 1. Short pieces
#      (`fn`, `=`, `{`) stay one token; a long identifier like
#      `leaked_categories` fragments into several, the way BPE actually splits
#      code that is not in its merge table.
#
# Deterministic and total: same bytes in, same count out. The raw inputs
# (chars, bytes) ride along in the JSON so the number can be recomputed with a
# different divisor — or a real tokeniser — without re-reading the corpus.
_PRETOKEN = re.compile(
    r"""'(?:[sdmt]|ll|ve|re)|[^\r\n\w]?\w+|[^\s\w]+|\s+""",
    re.UNICODE,
)


def count_tokens(text: str) -> int:
    """Deterministic BPE-proxy output-token count for one generation."""
    total = 0
    for piece in _PRETOKEN.findall(text):
        stripped = piece.strip()
        if not stripped:  # a run of whitespace ~ one token
            total += 1
            continue
        total += max(1, math.ceil(len(stripped) / 4))
    return total


# -- per-run computation ----------------------------------------------------

# scored on the revl variants, the ones that have an iterations-to-green to sit
# beside. raw-ts is one-shot and probe-scored, not compile-gated — it has no
# tokens-*-to-green because it has no green.
VARIANTS = rescore.VARIANTS


def _attempts_in_order(variant_dir: Path) -> list[Path]:
    return sorted(
        variant_dir.glob("attempt-*.rvl"),
        key=lambda p: int(re.search(r"attempt-(\d+)", p.name).group(1)),
    )


def _as_run_cost(run: str) -> dict[tuple[str, str], dict]:
    """Read the committed results.jsonl summary rows for the real as-run
    numbers: `green_at` (the original run's green) and `cost_total` (the real
    dollars billed). Keyed by (spec, variant). Absent file -> empty."""
    path = rescore.RESULTS / run / "results.jsonl"
    out: dict[tuple[str, str], dict] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row.get("summary") and row.get("variant") in VARIANTS:
            out[(row["spec"], row["variant"])] = {
                "as_run_green_at": row.get("green_at"),
                "as_run_cost_total": row.get("cost_total"),
            }
    return out


def _recorded_output_tokens(run: str) -> dict[tuple[str, str, int], int]:
    """Exact per-attempt output tokens, IF a funded run recorded them.

    Keyed by (spec, variant, attempt). Empty for every corpus committed before
    `run.py` learned to capture cline's `output_tokens` — which is all of the
    ones in-tree today; this is the hook a funded run lights up."""
    path = rescore.RESULTS / run / "results.jsonl"
    out: dict[tuple[str, str, int], int] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        tok = row.get("output_tokens")
        if tok is not None and not row.get("summary") and row.get("attempt"):
            out[(row["spec"], row["variant"], row["attempt"])] = tok
    return out


def compute_run(run: str, compiler_root: Path) -> list[dict]:
    """One cell per (spec, variant): tokens-to-green over the committed attempts,
    with green recomputed against the current checker."""
    run_dir = rescore.RESULTS / run
    if not run_dir.is_dir():
        raise SystemExit(f"no such run: {run_dir}")
    compile_source, RevlError, classify = _load_compiler(compiler_root)
    as_run = _as_run_cost(run)
    recorded = _recorded_output_tokens(run)

    cells: list[dict] = []
    for spec_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        for variant in VARIANTS:
            variant_dir = spec_dir / variant
            if not variant_dir.is_dir():
                continue
            attempts = _attempts_in_order(variant_dir)
            if not attempts:
                continue
            spent = 0          # output tokens across attempts, running total
            recorded_spent = 0  # same, from recorded tokens (0 until funded)
            have_recorded = True
            green_at = None
            for path in attempts:
                n = int(re.search(r"attempt-(\d+)", path.name).group(1))
                est = count_tokens(path.read_text())
                spent += est
                key = (spec_dir.name, variant, n)
                if key in recorded:
                    recorded_spent += recorded[key]
                else:
                    have_recorded = False
                result = rescore.score_one(path, compile_source, RevlError, classify)
                if result["ok"]:
                    green_at = n
                    break
            row = as_run.get((spec_dir.name, variant), {})
            cells.append({
                "run": run,
                "spec": spec_dir.name,
                "variant": variant,
                "green_at": green_at,             # recomputed vs current checker
                "admitted": green_at is not None,
                "attempts_to_green": green_at if green_at else len(attempts),
                "est_tokens_to_green": spent,
                "recorded_tokens_to_green": recorded_spent if have_recorded else None,
                "as_run_green_at": row.get("as_run_green_at"),
                "as_run_cost_total": row.get("as_run_cost_total"),
            })
    return cells


# -- rendering --------------------------------------------------------------

def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _median(xs: list[int]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    mid = len(s) // 2
    return float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def render(runs: list[str], all_cells: list[dict], sha: str) -> list[str]:
    any_recorded = any(c["recorded_tokens_to_green"] is not None for c in all_cells)
    lines = [
        "## tokens-to-green — output tokens spent per admitted component",
        f"compiler: `{sha}`  ·  reproduce: `python3 bench/tokens.py`",
        "",
        "The token every optimization must move: output tokens the model emitted "
        "across ALL attempts, up to and including the admitted one (retries "
        "included — a refused draft still cost its tokens). `est.` is a "
        "deterministic BPE-proxy over the committed generation files; "
        "**as-run $** is the real billed cost from the corpus. Green is "
        "recomputed against the current checker.",
        "",
        "| run | variant | admitted | mean est. tokens-to-green | median | total est. | as-run $ |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for run in runs:
        for variant in VARIANTS:
            vs = [c for c in all_cells if c["run"] == run and c["variant"] == variant]
            if not vs:
                continue
            admitted = [c for c in vs if c["admitted"]]
            toks = [c["est_tokens_to_green"] for c in admitted]
            mean = _mean(toks)
            median = _median(toks)
            total = sum(toks)
            cost = sum(c["as_run_cost_total"] or 0 for c in admitted)
            lines.append(
                f"| {run} | {variant} | {len(admitted)}/{len(vs)} | "
                f"{mean:.0f} | {median:.0f} | {total} | ${cost:.4f} |"
                if mean is not None else
                f"| {run} | {variant} | 0/{len(vs)} | — | — | 0 | $0 |"
            )

    # the corpus-wide headline: one number across every admitted revl cell.
    admitted = [c for c in all_cells if c["admitted"]]
    toks = [c["est_tokens_to_green"] for c in admitted]
    mean = _mean(toks)
    cost = sum(c["as_run_cost_total"] or 0 for c in admitted)
    lines += ["", "### headline"]
    if mean is not None:
        lines.append(
            f"Across **{len(admitted)}** admitted components in "
            f"{'+'.join(runs)}: **mean {mean:.0f} est. output tokens-to-green** "
            f"(median {_median(toks):.0f}, total {sum(toks)}), real as-run cost "
            f"${cost:.4f}."
        )
    else:
        lines.append("_no admitted components in scope._")

    # the most-expensive cells: where an optimization would pay the most.
    top = sorted(admitted, key=lambda c: -c["est_tokens_to_green"])[:8]
    if top:
        lines += ["", "### costliest admitted cells (est. output tokens-to-green)", ""]
        for c in top:
            iters = c["green_at"]
            lines.append(
                f"- `{c['run']}/{c['spec']}/{c['variant']}` — "
                f"{c['est_tokens_to_green']} tokens over {iters} attempt(s)"
            )

    lines += [
        "",
        "### real vs needs-a-funded-run",
        "",
        "- **real now:** the generation artefacts (every `attempt-N.rvl`) and "
        "the as-run dollar cost are committed and exact.",
        "- **proxy now:** `est. tokens` is a deterministic BPE-shaped estimate "
        "over those artefacts — the model's own output-token count was never "
        "recorded for these corpora.",
    ]
    if any_recorded:
        lines.append(
            "- **recorded:** some cells carry an exact `output_tokens` from a "
            "funded run; where present it is used verbatim instead of the proxy."
        )
    else:
        lines.append(
            "- **needs a funded run:** no committed cell carries a recorded "
            "`output_tokens` yet. `run.py` now captures cline's `output_tokens` "
            "per attempt, so the next paid run replaces the proxy with the exact "
            "figure with no code change here."
        )
    return lines


def to_json(runs: list[str], all_cells: list[dict], sha: str) -> dict:
    admitted = [c for c in all_cells if c["admitted"]]
    toks = [c["est_tokens_to_green"] for c in admitted]
    return {
        "compiler": sha,
        "runs": runs,
        "unit": "est_output_tokens (deterministic BPE-proxy; see count_tokens)",
        "headline": {
            "admitted_components": len(admitted),
            "mean_est_tokens_to_green": _mean(toks),
            "median_est_tokens_to_green": _median(toks),
            "total_est_tokens_to_green": sum(toks),
            "as_run_cost_total": sum(c["as_run_cost_total"] or 0 for c in admitted),
            "any_recorded_output_tokens": any(
                c["recorded_tokens_to_green"] is not None for c in all_cells),
        },
        "cells": all_cells,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", default="all",
                    help="run dir under bench/results, or 'all' for "
                         f"{' + '.join(rescore.MODEL_RUNS)} (default: all)")
    ap.add_argument("--compiler-root", default=None,
                    help="score against <dir>/src/revl instead of this tree")
    ap.add_argument("--json", default=None, help="also write the numbers as JSON")
    args = ap.parse_args()

    root = Path(args.compiler_root).resolve() if args.compiler_root else rescore.ROOT
    if not (root / "src" / "revl").is_dir():
        raise SystemExit(f"no src/revl under {root}")

    runs = rescore.MODEL_RUNS if args.run == "all" else [args.run]
    all_cells: list[dict] = []
    for run in runs:
        all_cells += compute_run(run, root)

    sha = rescore.compiler_sha(root)
    print("\n".join(render(runs, all_cells, sha)))
    if args.json:
        Path(args.json).write_text(json.dumps(to_json(runs, all_cells, sha), indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
