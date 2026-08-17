#!/usr/bin/env python3
"""syntax-2.0 acceptance benchmark (docs/syntax-2.0.md §10).

30 component specs x {v1, v2, v2host} x models; measures first-pass compile
rate and iterations-to-green against the CURRENT checker in ../src/revl.

Runners:
  cline  — drives the `cline` CLI non-interactively (real model calls; uses
           whatever provider/model cline is configured with, override with
           --model/--provider). Costs real money.
  mock   — no model; attempt 1 is deliberately broken (exercises the retry
           loop), attempt 2 is a minimal valid file. Validates the pipeline.

Usage:
  python3 bench/run.py --runner mock                       # pipeline check
  python3 bench/run.py --runner cline --specs 3 --variants v2   # pilot
  python3 bench/run.py --runner cline                      # full matrix
"""

import argparse
import datetime
import json
import re
import subprocess
import sys
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent

OUTPUT_REMINDER = (
    "\n\n## Task\n\n{brief}\n\nUse these service interfaces verbatim:\n\n"
    "```revl\n{services}\n```\n\n"
    "Reply with exactly one fenced code block containing the complete .rvl file."
)

RETRY_TEMPLATE = (
    "{task}\n\nYour previous attempt:\n\n```revl\n{code}\n```\n\n"
    "The revl compiler rejected it:\n\n```\n{error}\n```\n\n"
    "Reply with the corrected complete .rvl file in one fenced code block."
)


def load_variant_prompt(variant: str) -> str:
    if variant == "v1":
        return (BENCH / "prompts" / "v1.md").read_text()
    if variant == "v2":
        return (BENCH / "prompts" / "v2.md").read_text()
    if variant == "v2host":
        return (
            (BENCH / "prompts" / "v2.md").read_text()
            + "\n\n"
            + (BENCH / "prompts" / "v2host.md").read_text()
        )
    raise SystemExit(f"unknown variant {variant!r}")


def extract_code(reply: str) -> str:
    blocks = re.findall(r"```[a-zA-Z]*\n(.*?)```", reply, re.DOTALL)
    return blocks[-1].strip() + "\n" if blocks else reply.strip() + "\n"


COMPILER_ROOT = ROOT  # overridable via --compiler-root


def compile_check(code: str, name: str):
    """Returns (ok, error_message). Imports the compiler each call so a
    concurrent edit to src/revl is picked up, and an import-time breakage is
    reported rather than crashing the run."""
    src = str(COMPILER_ROOT / "src")
    if src in sys.path:
        sys.path.remove(src)
    sys.path.insert(0, src)
    for mod in [m for m in list(sys.modules) if m == "revl" or m.startswith("revl.")]:
        del sys.modules[mod]
    try:
        from revl import RevlError, compile_source
    except Exception as exc:  # compiler tree mid-edit
        return False, f"[compiler import failed] {exc}"
    try:
        compile_source(code, name)
        return True, None
    except RevlError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"[compiler crash] {type(exc).__name__}: {exc}"


# --- runners ---------------------------------------------------------------

def run_cline(system: str, prompt: str, model: str | None, provider: str | None,
              timeout: int, inline_system: bool):
    cmd = ["cline", "--json", "--auto-approve", "false", "-t", str(timeout)]
    if model:
        cmd += ["-m", model]
    if provider:
        cmd += ["-P", provider]
    if inline_system:
        cmd.append(system + "\n\n" + prompt)
    else:
        cmd += ["-s", system, prompt]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout + 60, cwd=str(BENCH))
    result = None
    for line in proc.stdout.splitlines():
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if obj.get("type") == "run_result":
            result = obj
    if result is None:
        raise RuntimeError(
            f"cline produced no run_result (exit {proc.returncode}); "
            f"stderr tail: {proc.stderr[-400:]}"
        )
    usage = result.get("aggregateUsage") or result.get("usage") or {}
    model_info = result.get("model") or {}
    return {
        "text": result.get("text", ""),
        "cost": usage.get("totalCost"),
        "model": model_info.get("id"),
        "provider": (model_info.get("provider") if isinstance(model_info, dict) else None),
    }


def run_mock(system: str, prompt: str, spec: dict, attempt: int):
    if attempt == 1:
        code = spec["services"] + "\n\ncomponent Broken {\n  effect ghost.poke() undo ghost.unpoke()\n}\n"
    else:
        code = spec["services"] + "\n\ncomponent Probe {\n  let m = effect Map.new() undo m.drop()\n}\n"
    return {"text": f"```revl\n{code}```", "cost": 0.0, "model": "mock", "provider": "mock"}


# --- orchestration ---------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runner", choices=["cline", "mock"], required=True)
    ap.add_argument("--variants", default="v1,v2,v2host")
    ap.add_argument("--specs", default="all",
                    help="'all', a count (first N), or comma-separated ids")
    ap.add_argument("--model", default=None, help="cline -m override")
    ap.add_argument("--provider", default=None, help="cline -P override")
    ap.add_argument("--max-iters", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=240, help="per-call seconds")
    ap.add_argument("--inline-system", action="store_true",
                    help="merge grammar into the user prompt instead of cline -s")
    ap.add_argument("--label", default=None, help="run directory name")
    ap.add_argument("--compiler-root", default=None,
                    help="score against <dir>/src/revl instead of the live tree "
                         "(e.g. a clean export of a pinned commit)")
    args = ap.parse_args()

    if args.compiler_root:
        global COMPILER_ROOT
        COMPILER_ROOT = Path(args.compiler_root).resolve()
        if not (COMPILER_ROOT / "src" / "revl").is_dir():
            raise SystemExit(f"no src/revl under {COMPILER_ROOT}")

    all_specs = json.loads((BENCH / "specs.json").read_text())["specs"]
    if args.specs == "all":
        specs = all_specs
    elif args.specs.isdigit():
        specs = all_specs[: int(args.specs)]
    else:
        wanted = set(args.specs.split(","))
        specs = [s for s in all_specs if s["id"] in wanted]
        missing = wanted - {s["id"] for s in specs}
        if missing:
            raise SystemExit(f"unknown spec ids: {sorted(missing)}")

    variants = args.variants.split(",")
    prompts = {v: load_variant_prompt(v) for v in variants}

    label = args.label or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = BENCH / "results" / label
    run_dir.mkdir(parents=True, exist_ok=True)
    results_path = run_dir / "results.jsonl"
    rows = []

    for spec in specs:
        for variant in variants:
            system = prompts[variant]
            task = OUTPUT_REMINDER.format(brief=spec["brief"], services=spec["services"])
            prompt, code, error = task, None, None
            green_at, cost_total, model_seen = None, 0.0, None
            for attempt in range(1, args.max_iters + 1):
                t0 = time.time()
                try:
                    if args.runner == "mock":
                        reply = run_mock(system, prompt, spec, attempt)
                    else:
                        reply = run_cline(system, prompt, args.model, args.provider,
                                          args.timeout, args.inline_system)
                except Exception as exc:
                    print(f"  !! {spec['id']}/{variant} attempt {attempt}: runner error: {exc}",
                          file=sys.stderr)
                    rows.append({"spec": spec["id"], "variant": variant,
                                 "attempt": attempt, "ok": False,
                                 "error": f"[runner error] {exc}",
                                 "duration_s": round(time.time() - t0, 2)})
                    break
                dur = round(time.time() - t0, 2)
                cost_total += reply.get("cost") or 0.0
                model_seen = reply.get("model") or model_seen
                code = extract_code(reply["text"])
                adir = run_dir / spec["id"] / variant
                adir.mkdir(parents=True, exist_ok=True)
                (adir / f"attempt-{attempt}.rvl").write_text(code)
                ok, error = compile_check(code, f"{spec['id']}.rvl")
                rows.append({"spec": spec["id"], "variant": variant,
                             "model": model_seen, "attempt": attempt, "ok": ok,
                             "error": error, "duration_s": dur,
                             "cost": reply.get("cost")})
                status = "green" if ok else "red"
                print(f"  {spec['id']}/{variant} attempt {attempt}: {status}"
                      + (f" — {error.splitlines()[0][:100]}" if error else ""))
                if ok:
                    green_at = attempt
                    break
                prompt = RETRY_TEMPLATE.format(task=task, code=code, error=error)
            rows.append({"spec": spec["id"], "variant": variant, "model": model_seen,
                         "summary": True, "green_at": green_at,
                         "cost_total": round(cost_total, 6)})
            results_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    write_summary(run_dir, rows, args)
    print(f"\nresults: {results_path}\nsummary: {run_dir / 'summary.md'}")


def write_summary(run_dir: Path, rows: list, args):
    finals = [r for r in rows if r.get("summary")]
    lines = ["# syntax-2.0 acceptance benchmark — run summary", "",
             f"runner: `{args.runner}`" + (f" · model: `{args.model}`" if args.model else ""),
             f"max iterations: {args.max_iters}", "",
             "| variant | specs | first-pass compile | green ≤ max iters | mean iters-to-green | cost |",
             "|---|---|---|---|---|---|"]
    for variant in args.variants.split(","):
        vs = [r for r in finals if r["variant"] == variant]
        if not vs:
            continue
        n = len(vs)
        first = sum(1 for r in vs if r["green_at"] == 1)
        green = [r["green_at"] for r in vs if r["green_at"]]
        mean_iters = f"{sum(green) / len(green):.2f}" if green else "—"
        cost = sum(r.get("cost_total") or 0 for r in vs)
        lines.append(f"| {variant} | {n} | {first}/{n} ({100 * first / n:.0f}%) "
                     f"| {len(green)}/{n} | {mean_iters} | ${cost:.2f} |")
    unsolved = [(r["spec"], r["variant"]) for r in finals if not r["green_at"]]
    if unsolved:
        lines += ["", "Unsolved: " + ", ".join(f"{s}/{v}" for s, v in unsolved)]
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
