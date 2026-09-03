#!/usr/bin/env python3
"""syntax-2.0 acceptance benchmark (docs/syntax-2.0.md §10).

30 component specs x {v1, v2, v2host} x models; measures first-pass compile
rate and iterations-to-green against the CURRENT checker in ../src/revl.

Runners:
  cline  — drives the `cline` CLI non-interactively (real model calls; uses
           whatever provider/model cline is configured with, override with
           --model/--provider). Costs real money.
  local  — posts directly to an OpenAI-compatible /chat/completions endpoint
           (LM Studio, ollama's OpenAI shim, etc) via stdlib urllib. Free,
           override with --model/--base-url (default openai/gpt-oss-20b @
           http://localhost:1234/v1).
  mock   — no model; attempt 1 is deliberately broken (exercises the retry
           loop), attempt 2 is a minimal valid file. Validates the pipeline.

Usage:
  python3 bench/run.py --runner mock                       # pipeline check
  python3 bench/run.py --runner cline --specs 3 --variants v2   # pilot
  python3 bench/run.py --runner cline                      # full matrix
  python3 bench/run.py --runner local --specs 3 --variants v2   # local pilot
"""

import argparse
import datetime
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent

# raw-ts (the paradigm baseline) is scored on lifecycle correctness via the
# residue probe, not on compile-rate — see score_raw_ts.py. The revl variants'
# compile-rate path below is untouched by it.
sys.path.insert(0, str(BENCH))
from score_raw_ts import (  # noqa: E402
    RAW_TS_VARIANT, DEFAULT_CYCLES, probe_source, render_raw_ts_summary,
)

OUTPUT_REMINDER = (
    "\n\n## Task\n\n{brief}\n\nUse these service interfaces verbatim:\n\n"
    "```revl\n{services}\n```\n\n"
    "Reply with exactly one fenced code block containing the complete .rvl file."
)

RAW_TS_REMINDER = (
    "\n\n## Task\n\n{brief}\n\nThe service interface(s), in revl notation "
    "(implement the same operations from your Cordis `ctx.provide`):\n\n"
    "```revl\n{services}\n```\n\n"
    "Reply with exactly one fenced ```ts code block: the complete raw Cordis "
    "plugin as `export const plugin`. No revl."
)

RETRY_TEMPLATE = (
    "{task}\n\nYour previous attempt:\n\n```revl\n{code}\n```\n\n"
    "The revl compiler rejected it:\n\n```\n{error}\n```\n\n"
    "Reply with the corrected complete .rvl file in one fenced code block."
)


def load_variant_prompt(variant: str) -> str:
    if variant == RAW_TS_VARIANT:
        return (BENCH / "prompts" / "raw-ts.md").read_text()
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
    # strip a <think>...</think> reasoning preamble (gpt-oss and similar local
    # models emit this ahead of the actual answer) before looking for fences.
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL | re.IGNORECASE)
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
        # the exact output-token count behind the cost — the tokens-to-green
        # metric's real number (bench/tokens.py), which the committed corpora
        # only carry a proxy for. cline names it differently across versions.
        "output_tokens": _output_tokens(usage),
        "model": model_info.get("id"),
        "provider": (model_info.get("provider") if isinstance(model_info, dict) else None),
    }


def run_local(system: str, prompt: str, model: str, base_url: str, timeout: int):
    """OpenAI-compatible chat-completions runner for a local server (LM Studio,
    ollama's OpenAI shim, etc). Mirrors run_cline's call/return shape but posts
    directly to `{base_url}/chat/completions` with stdlib urllib — no new
    dependency, no cost, no cline process to spawn."""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 4096,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    # The runner is pointed at a base URL the operator names, and urllib
    # follows a redirect off it by default — re-issuing this POST as a GET,
    # body dropped, at whatever host `Location` says. A benchmark that silently
    # measured a different server is not a benchmark, so nothing is followed:
    # the same policy the emitted crossings carry (`revl.crossing_redirect`),
    # applied to the one other place in this tree that makes an outbound
    # request.
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise RuntimeError(
                f"local runner: HTTP {code} redirect refused — "
                f"{url} is the endpoint you named")

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:400].decode("utf-8", "replace")
        raise RuntimeError(f"local runner: HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"local runner: cannot reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError(f"local runner: timed out after {timeout}s: {exc}") from exc

    if status != 200:
        raise RuntimeError(f"local runner: HTTP {status} from {url}: {raw[:400]!r}")

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"local runner: unparseable JSON from {url}: {exc}") from exc

    try:
        text = parsed["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            f"local runner: unexpected response shape from {url}: {exc}; "
            f"body={str(parsed)[:400]}"
        ) from exc
    if not text or not text.strip():
        raise RuntimeError(f"local runner: empty completion content from {url}")

    usage = parsed.get("usage") or {}
    return {
        "text": text,
        "cost": 0.0,
        "output_tokens": usage.get("completion_tokens"),
        "model": parsed.get("model") or model,
        "provider": "local",
    }


def _output_tokens(usage: dict):
    """Model completion (output) tokens from a cline usage block, across the
    key names cline has used. Returns None when none is present, so a run
    against a provider that does not report it degrades to the proxy rather
    than recording a wrong zero."""
    for key in ("outputTokens", "completionTokens", "totalTokensOut",
                "output_tokens", "completion_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return None


def run_mock(system: str, prompt: str, spec: dict, attempt: int):
    if attempt == 1:
        code = spec["services"] + "\n\ncomponent Broken {\n  effect ghost.poke() undo ghost.unpoke()\n}\n"
    else:
        code = spec["services"] + "\n\ncomponent Probe {\n  let m = effect Map.new() undo m.drop()\n}\n"
    return {"text": f"```revl\n{code}```", "cost": 0.0, "model": "mock", "provider": "mock"}


# A leaky mock plugin roughly every third spec, so the mock run exercises both
# outcomes of the probe (clean vs residue) end-to-end. The clean form scopes its
# provision + resource to its own fiber (revl's emitted shape); the leaky form
# binds a listener to the root (the ordinary Cordis mistake the probe catches).
def _mock_raw_ts(spec: dict) -> str:
    try:
        leaky = int(spec["id"][:2]) % 3 == 0
    except ValueError:
        leaky = False
    body = (
        "    ctx.root.on('internal/info' as never, () => {})\n"
        if leaky else
        "    ctx.effect(function* () {\n"
        "      const m = host.Map.new()\n"
        "      yield () => m.drop()\n"
        "      yield (ctx as unknown as { provide(k: string, v: unknown): () => void })\n"
        "        .provide('svc', { get: (k: string) => m.get(k) })\n"
        "    }, 'MockPlugin.body')\n"
    )
    return (
        "import type { Context } from 'cordis'\n"
        "import { host } from './host.ts'\n\n"
        "export const plugin = {\n"
        f"  name: 'Mock_{spec['id']}',\n"
        "  apply(ctx: Context) {\n"
        f"{body}"
        "  },\n"
        "}\n"
    )


def run_mock_raw_ts(spec: dict):
    return {"text": f"```ts\n{_mock_raw_ts(spec)}```",
            "cost": 0.0, "model": "mock", "provider": "mock"}


# --- orchestration ---------------------------------------------------------

def run_one_raw_ts(spec: dict, system: str, run_dir: Path, args) -> dict:
    """Generate one raw Cordis plugin for `spec`, save it, and score it with the
    residue probe. Returns a single row (there is no retry loop)."""
    task = RAW_TS_REMINDER.format(brief=spec["brief"], services=spec["services"])
    t0 = time.time()
    try:
        if args.runner == "mock":
            reply = run_mock_raw_ts(spec)
        elif args.runner == "local":
            reply = run_local(system, task, args.model, args.base_url, args.timeout)
        else:
            reply = run_cline(system, task, args.model, args.provider,
                              args.timeout, args.inline_system)
    except Exception as exc:
        print(f"  !! {spec['id']}/{RAW_TS_VARIANT}: runner error: {exc}", file=sys.stderr)
        return {"spec": spec["id"], "variant": RAW_TS_VARIANT, "summary": True,
                "status": "error", "leaked": True, "leaked_categories": [],
                "error": f"[runner error] {exc}", "duration_s": round(time.time() - t0, 2)}
    dur = round(time.time() - t0, 2)
    code = extract_code(reply["text"])
    adir = run_dir / spec["id"] / RAW_TS_VARIANT
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "attempt-1.ts").write_text(code)
    rec = probe_source(code, cycles=args.raw_ts_cycles, name=f"{run_dir.name}__{spec['id']}")
    flag = {"clean": "clean", "leaked": "LEAK", "error": "error"}[rec["status"]]
    extra = (" — " + ", ".join(rec["leaked_categories"])) if rec["leaked_categories"] \
        else (f" — {rec['error']}" if rec.get("error") else "")
    print(f"  {spec['id']}/{RAW_TS_VARIANT}: {flag}{extra}")
    return {"spec": spec["id"], "variant": RAW_TS_VARIANT, "summary": True,
            "model": reply.get("model"), "duration_s": dur,
            "cost": reply.get("cost"), "cost_total": reply.get("cost") or 0.0, **rec}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runner", choices=["cline", "mock", "local"], required=True)
    ap.add_argument("--variants", default="v1,v2,v2host")
    ap.add_argument("--specs", default="all",
                    help="'all', a count (first N), or comma-separated ids")
    ap.add_argument("--model", default=None,
                    help="cline -m override, or the model id for --runner local "
                         "(default openai/gpt-oss-20b)")
    ap.add_argument("--provider", default=None, help="cline -P override")
    ap.add_argument("--base-url", default="http://localhost:1234/v1",
                    help="--runner local: OpenAI-compatible base URL "
                         "(no trailing /chat/completions)")
    ap.add_argument("--max-iters", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=240, help="per-call seconds")
    ap.add_argument("--inline-system", action="store_true",
                    help="merge grammar into the user prompt instead of cline -s")
    ap.add_argument("--label", default=None, help="run directory name")
    ap.add_argument("--compiler-root", default=None,
                    help="score against <dir>/src/revl instead of the live tree "
                         "(e.g. a clean export of a pinned commit)")
    ap.add_argument("--raw-ts-cycles", type=int, default=DEFAULT_CYCLES,
                    help="mount/unmount cycles for the raw-ts residue probe "
                         f"(default {DEFAULT_CYCLES}); only used by the raw-ts variant")
    args = ap.parse_args()

    if args.runner == "local" and args.model is None:
        args.model = "openai/gpt-oss-20b"

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
    raw_rows = []  # raw-ts scoring records, for the paradigm-comparison summary

    for spec in specs:
        for variant in variants:
            system = prompts[variant]

            # raw-ts: the paradigm baseline. One generation, no compiler retry
            # loop — a raw Cordis plugin always "compiles". Scored on lifecycle
            # correctness via the residue probe (score_raw_ts.py), NOT on
            # compile-rate. The revl compile-rate path below is left untouched.
            if variant == RAW_TS_VARIANT:
                raw = run_one_raw_ts(spec, system, run_dir, args)
                rows.append(raw)
                raw_rows.append(raw)
                results_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
                continue

            task = OUTPUT_REMINDER.format(brief=spec["brief"], services=spec["services"])
            prompt, code, error = task, None, None
            green_at, cost_total, model_seen = None, 0.0, None
            tokens_total, tokens_seen = 0, False  # output tokens across attempts
            for attempt in range(1, args.max_iters + 1):
                t0 = time.time()
                try:
                    if args.runner == "mock":
                        reply = run_mock(system, prompt, spec, attempt)
                    elif args.runner == "local":
                        reply = run_local(system, prompt, args.model, args.base_url, args.timeout)
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
                if reply.get("output_tokens") is not None:
                    tokens_total += reply["output_tokens"]
                    tokens_seen = True
                model_seen = reply.get("model") or model_seen
                code = extract_code(reply["text"])
                adir = run_dir / spec["id"] / variant
                adir.mkdir(parents=True, exist_ok=True)
                (adir / f"attempt-{attempt}.rvl").write_text(code)
                ok, error = compile_check(code, f"{spec['id']}.rvl")
                rows.append({"spec": spec["id"], "variant": variant,
                             "model": model_seen, "attempt": attempt, "ok": ok,
                             "error": error, "duration_s": dur,
                             "cost": reply.get("cost"),
                             "output_tokens": reply.get("output_tokens")})
                status = "green" if ok else "red"
                print(f"  {spec['id']}/{variant} attempt {attempt}: {status}"
                      + (f" — {error.splitlines()[0][:100]}" if error else ""))
                if ok:
                    green_at = attempt
                    break
                prompt = RETRY_TEMPLATE.format(task=task, code=code, error=error)
            rows.append({"spec": spec["id"], "variant": variant, "model": model_seen,
                         "summary": True, "green_at": green_at,
                         "cost_total": round(cost_total, 6),
                         # tokens-to-green: the token economy's metric, recorded
                         # beside iterations-to-green (bench/tokens.py). None when
                         # the provider reported no token usage.
                         "tokens_to_green": tokens_total if tokens_seen else None})
            results_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    write_summary(run_dir, rows, raw_rows, args)
    print(f"\nresults: {results_path}\nsummary: {run_dir / 'summary.md'}")


def write_summary(run_dir: Path, rows: list, raw_rows: list, args):
    finals = [r for r in rows if r.get("summary") and r["variant"] != RAW_TS_VARIANT]
    lines = ["# syntax-2.0 acceptance benchmark — run summary", "",
             f"runner: `{args.runner}`" + (f" · model: `{args.model}`" if args.model else ""),
             f"max iterations: {args.max_iters}", ""]
    revl_variants = [v for v in args.variants.split(",") if v != RAW_TS_VARIANT]
    if revl_variants:
        lines += ["## revl variants — compile-gated (residue refused at compile)", "",
                  "| variant | specs | first-pass compile | green ≤ max iters | mean iters-to-green | mean tokens-to-green | cost |",
                  "|---|---|---|---|---|---|---|"]
    for variant in revl_variants:
        vs = [r for r in finals if r["variant"] == variant]
        if not vs:
            continue
        n = len(vs)
        first = sum(1 for r in vs if r["green_at"] == 1)
        green = [r["green_at"] for r in vs if r["green_at"]]
        mean_iters = f"{sum(green) / len(green):.2f}" if green else "—"
        # tokens-to-green over the admitted cells that reported token usage;
        # "—" when the provider gave none (see bench/tokens.py for the proxy).
        toks = [r["tokens_to_green"] for r in vs
                if r["green_at"] and r.get("tokens_to_green") is not None]
        mean_toks = f"{sum(toks) / len(toks):.0f}" if toks else "—"
        cost = sum(r.get("cost_total") or 0 for r in vs)
        lines.append(f"| {variant} | {n} | {first}/{n} ({100 * first / n:.0f}%) "
                     f"| {len(green)}/{n} | {mean_iters} | {mean_toks} | ${cost:.2f} |")
    unsolved = [(r["spec"], r["variant"]) for r in finals if not r["green_at"]]
    if unsolved:
        lines += ["", "Unsolved: " + ", ".join(f"{s}/{v}" for s, v in unsolved)]

    if raw_rows:
        cycles = raw_rows[0].get("cycles", DEFAULT_CYCLES)
        lines += ["", "## raw-ts — probe-scored (does revl earn its keep?)", ""]
        lines += render_raw_ts_summary(raw_rows, cycles)
        lines += ["> The revl variants are compile-gated: a residue-carrying "
                  "component never reaches this corpus — the compiler refuses it. "
                  "The raw-ts column is what that gate would have caught.", ""]

    (run_dir / "summary.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
