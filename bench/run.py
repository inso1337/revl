#!/usr/bin/env python3
"""syntax-2.0 acceptance benchmark (docs/syntax-2.0.md §10).

30 component specs x {v1, v2, v2host} x models; measures first-pass compile
rate and iterations-to-green against the CURRENT checker in ../src/revl.

Runners:
  cline  — drives the `cline` CLI non-interactively (real model calls; uses
           whatever provider/model cline is configured with, override with
           --model/--provider). Costs real money.
  local  — posts directly to an OpenAI-compatible /chat/completions endpoint
           (LM Studio, ollama's OpenAI shim, etc) via revl's own adapter. Free,
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
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent

# raw-ts (the paradigm baseline) is scored on lifecycle correctness via the
# residue probe, not on compile-rate — see score_raw_ts.py. The revl variants'
# compile-rate path below is untouched by it.
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(ROOT / "src"))
from revl.providers import (  # noqa: E402
    Adapter, CompletionRequest, ProviderError, parse_config,
)
from revl.providers.wire_openai import REASONING_KEYS  # noqa: E402,F401
from score_raw_ts import (  # noqa: E402
    RAW_TS_VARIANT, DEFAULT_CYCLES, probe_source, render_raw_ts_summary,
)


def _load_by_path(name: str, rel: str):
    """A sibling bench module, by path. A bare `import` binds whichever module
    of that name is found first, and several basenames repeat in this tree."""
    import importlib.util  # noqa: PLC0415
    spec = importlib.util.spec_from_file_location(name, BENCH / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# mcp (the FRAMEWORK-BENCH-1 third host) is probe-scored the same way raw-ts is,
# by bench/mcp_host/probe.mjs through score_mcp.py.
score_mcp = _load_by_path("bench_score_mcp", "score_mcp.py")
MCP_VARIANT = score_mcp.MCP_VARIANT

# The variants that are generated once and scored on what they leave behind,
# rather than compiled and retried.
PROBED_VARIANTS = (RAW_TS_VARIANT, MCP_VARIANT)

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

MCP_REMINDER = (
    "\n\n## Task\n\n{brief}\n\nThe service interface(s), in revl notation "
    "(expose each operation as one tool named <service>_<operation>):\n\n"
    "```revl\n{services}\n```\n\n"
    "Reply with exactly one fenced ```ts code block: the complete tool pack "
    "module, exporting `install`. No revl."
)

RETRY_TEMPLATE = (
    "{task}\n\nYour previous attempt:\n\n```revl\n{code}\n```\n\n"
    "The revl compiler rejected it:\n\n```\n{error}\n```\n\n"
    "Reply with the corrected complete .rvl file in one fenced code block."
)


def load_variant_prompt(variant: str) -> str:
    if variant == RAW_TS_VARIANT:
        return (BENCH / "prompts" / "raw-ts.md").read_text()
    if variant == MCP_VARIANT:
        return (BENCH / "prompts" / "mcp.md").read_text()
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


def _is_revl(name: str) -> bool:
    return name == "revl" or name.startswith("revl.")


# The `revl/__init__.py` the last `compile_check` graded with, for
# `scoring_compiler`. `compile_check` puts the process's own `revl` back when
# it returns, so `sys.modules` no longer says which one graded.
_GRADED_BY: str | None = None


def compile_check(code: str, name: str):
    """Returns (ok, error_message). Imports the compiler each call so a
    concurrent edit to src/revl is picked up, and an import-time breakage is
    reported rather than crashing the run.

    The fresh import is scoped to this call. Every `revl` module already
    loaded, and `sys.path`, are put back before it returns. Leaving the fresh
    copy installed split the process in two (issue #1800): code that had
    imported `revl` earlier kept the old `RevlError` class while every later
    lazy import inside the compiler resolved to the new one, so
    `except RevlError` stopped catching the compiler's own refusals."""
    global _GRADED_BY
    src = str(COMPILER_ROOT / "src")
    saved_path = list(sys.path)
    saved = {m: mod for m, mod in sys.modules.items() if _is_revl(m)}
    if src in sys.path:
        sys.path.remove(src)
    sys.path.insert(0, src)
    for mod in saved:
        del sys.modules[mod]
    try:
        try:
            import revl  # noqa: PLC0415
            from revl import RevlError, compile_source  # noqa: PLC0415
        except Exception as exc:  # compiler tree mid-edit
            return False, f"[compiler import failed] {exc}"
        _GRADED_BY = getattr(revl, "__file__", None)
        try:
            compile_source(code, name)
            return True, None
        except RevlError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"[compiler crash] {type(exc).__name__}: {exc}"
    finally:
        for mod in [m for m in sys.modules if _is_revl(m)]:
            del sys.modules[mod]
        sys.modules.update(saved)
        sys.path[:] = saved_path


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


DEFAULT_LOCAL_MAX_TOKENS = 8192

# Servers that separate a reasoning channel from the answer do not agree on the
# key: `reasoning` is ollama's, `reasoning_content` the deepseek-style name
# several OpenAI-compatible servers copied. The adapter reads both, in the order
# `REASONING_KEYS` (imported above from `revl.providers.wire_openai`) gives.


def run_local(system: str, prompt: str, model: str, base_url: str, timeout: int,
              max_tokens: int = DEFAULT_LOCAL_MAX_TOKENS):
    """OpenAI-compatible chat-completions runner for a local server (LM Studio,
    ollama's OpenAI shim, etc). Mirrors run_cline's call/return shape but posts
    directly to `{base_url}/chat/completions` through the runtime's
    OpenAI-compatible adapter (`revl.providers`): no new dependency, no cost,
    no cline process to spawn.

    Two things here are about reasoning models and were measured, not guessed.

    The output cap is large by default. A reasoning model spends the cap on the
    reasoning channel first, so a small cap does not truncate the answer, it
    deletes it: the pinned model answered spec `01-kv-provider` with 3693
    completion tokens of which the answer was 358 characters, and a 16-token cap
    returned reasoning and an empty `content`. The previous 4096 was close
    enough to that figure to truncate a longer spec, so the default is the
    pin's `max_output_tokens`.

    The answer is read from `content`, and from the reasoning channel only when
    `content` is empty. That ordering matters: when a server sends both, the
    fenced block in `content` is the answer and the one in the reasoning is a
    draft the model then revised. Falling back is recorded on the row
    (`answer_from_reasoning`) rather than done silently, because a corpus
    scored off draft code is not the same measurement.
    """
    # One client: the runtime's OpenAI-compatible adapter (issue #1461). It
    # refuses redirects, so a benchmark cannot silently measure a server other
    # than the one named, and it reads `content` and the reasoning channel
    # separately, which is what the fallback below needs.
    binding = parse_config({"roles": {"bench": {
        "provider": "openai-compatible", "base_url": base_url, "model": model,
        "timeout": timeout, "max_tokens": max_tokens,
    }}}, "bench/run.py --base-url").binding("bench")
    try:
        completion = Adapter(binding).complete(
            CompletionRequest(prompt=prompt, system=system, temperature=0))
    except ProviderError as exc:
        raise RuntimeError(f"local runner: {exc}") from None

    text, reasoning = completion.text, completion.reasoning
    from_reasoning = False
    if not text or not text.strip():
        if reasoning:
            text, from_reasoning = reasoning, True
        else:
            raise RuntimeError(
                f"local runner: empty completion content from {binding.base_url} "
                f"(finish_reason={completion.finish_reason!r}, no reasoning "
                f"channel either)")

    return {
        "text": text,
        "cost": 0.0,
        # completion_tokens counts the reasoning channel too, and that is the
        # number tokens-to-green wants: reasoning tokens are paid for.
        "output_tokens": completion.tokens_out,
        "reasoning_tokens": completion.reasoning_tokens,
        "reasoning_chars": len(reasoning),
        "answer_from_reasoning": from_reasoning,
        "finish_reason": completion.finish_reason,
        "model": completion.model or model,
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


# The same split for the mcp host: a clean pack releases its Map from the
# teardown it returns; a leaky one returns nothing, so the Map outlives it.
def _mock_mcp(spec: dict) -> str:
    try:
        leaky = int(spec["id"][:2]) % 3 == 0
    except ValueError:
        leaky = False
    tail = "" if leaky else "  return () => store.drop()\n"
    return (
        "import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'\n"
        "import { z } from 'zod'\n"
        "import { host } from './host.ts'\n\n"
        "export function install(server: McpServer) {\n"
        "  const store = host.Map.new()\n"
        "  server.registerTool('svc_get', { description: 'get', "
        "inputSchema: { key: z.string() } },\n"
        "    async ({ key }) => ({ content: [{ type: 'text', "
        "text: String(store.get(key) ?? '') }] }))\n"
        f"{tail}"
        "}\n"
    )


def run_mock_mcp(spec: dict):
    return {"text": f"```ts\n{_mock_mcp(spec)}```",
            "cost": 0.0, "model": "mock", "provider": "mock"}


# --- orchestration ---------------------------------------------------------

def run_one_raw_ts(spec: dict, system: str, run_dir: Path, args) -> dict:
    """Generate one raw Cordis plugin for `spec`, save it, and score it with the
    residue probe. Returns a single row (there is no retry loop)."""
    return run_one_probed(spec, system, run_dir, args, RAW_TS_VARIANT)


def run_one_probed(spec: dict, system: str, run_dir: Path, args,
                   variant: str) -> dict:
    """Generate one module for a probe-scored host, save it, score it.

    raw-ts and mcp share everything but the task wording, the mock, and the
    probe, so they share this path: one generation, no compiler retry loop,
    scored on what the module leaves behind after N load/unload cycles.
    """
    reminder, mock, probe, cycles = {
        RAW_TS_VARIANT: (RAW_TS_REMINDER, run_mock_raw_ts,
                         lambda code, n, name: probe_source(code, cycles=n, name=name),
                         args.raw_ts_cycles),
        MCP_VARIANT: (MCP_REMINDER, run_mock_mcp,
                      lambda code, n, name: score_mcp.probe_source(code, cycles=n, name=name),
                      args.raw_ts_cycles),
    }[variant]
    task = reminder.format(brief=spec["brief"], services=spec["services"])
    t0 = time.time()
    try:
        if args.runner == "mock":
            reply = mock(spec)
        elif args.runner == "local":
            reply = run_local(system, task, args.model, args.base_url,
                              args.timeout, args.max_tokens)
        else:
            reply = run_cline(system, task, args.model, args.provider,
                              args.timeout, args.inline_system)
    except Exception as exc:
        print(f"  !! {spec['id']}/{variant}: runner error: {exc}", file=sys.stderr)
        return {"spec": spec["id"], "variant": variant, "summary": True,
                "status": "error", "leaked": True, "leaked_categories": [],
                "error": f"[runner error] {exc}", "duration_s": round(time.time() - t0, 2)}
    dur = round(time.time() - t0, 2)
    code = extract_code(reply["text"])
    adir = run_dir / spec["id"] / variant
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "attempt-1.ts").write_text(code)
    rec = probe(code, cycles, f"{run_dir.name}__{spec['id']}")
    flag = {"clean": "clean", "leaked": "LEAK", "error": "error"}[rec["status"]]
    extra = (" — " + ", ".join(rec["leaked_categories"])) if rec["leaked_categories"] \
        else (f" — {rec['error']}" if rec.get("error") else "")
    print(f"  {spec['id']}/{variant}: {flag}{extra}")
    return {"spec": spec["id"], "variant": variant, "summary": True,
            "model": reply.get("model"), "duration_s": dur,
            "cost": reply.get("cost"), "cost_total": reply.get("cost") or 0.0,
            "output_tokens": reply.get("output_tokens"),
            "reasoning_tokens": reply.get("reasoning_tokens"),
            "answer_from_reasoning": reply.get("answer_from_reasoning"),
            "finish_reason": reply.get("finish_reason"), **rec}


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
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_LOCAL_MAX_TOKENS,
                    help="--runner local: output-token cap per call (default "
                         f"{DEFAULT_LOCAL_MAX_TOKENS}). A reasoning model spends "
                         "this on the reasoning channel before the answer, so a "
                         "small cap returns reasoning and an empty answer rather "
                         "than a truncated one.")
    ap.add_argument("--timeout", type=int, default=240, help="per-call seconds")
    ap.add_argument("--inline-system", action="store_true",
                    help="merge grammar into the user prompt instead of cline -s")
    ap.add_argument("--label", default=None, help="run directory name")
    ap.add_argument("--compiler-root", default=None,
                    help="score against <dir>/src/revl instead of the live tree "
                         "(e.g. a clean export of a pinned commit)")
    ap.add_argument("--raw-ts-cycles", type=int, default=DEFAULT_CYCLES,
                    help="load/unload cycles for the probe-scored hosts, raw-ts "
                         f"and mcp (default {DEFAULT_CYCLES})")
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
            if variant in PROBED_VARIANTS:
                raw = run_one_probed(spec, system, run_dir, args, variant)
                rows.append(raw)
                if variant == RAW_TS_VARIANT:
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
                        reply = run_local(system, prompt, args.model, args.base_url,
                                          args.timeout, args.max_tokens)
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
                             "output_tokens": reply.get("output_tokens"),
                             # reasoning-model bookkeeping: output_tokens above
                             # includes the reasoning channel, so the split is
                             # recorded rather than left for a reader to guess,
                             # and a `content`-empty reply is flagged where it
                             # happened instead of silently scored as an answer.
                             "reasoning_tokens": reply.get("reasoning_tokens"),
                             "reasoning_chars": reply.get("reasoning_chars"),
                             "answer_from_reasoning": reply.get("answer_from_reasoning"),
                             "finish_reason": reply.get("finish_reason")})
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


def scoring_compiler() -> str:
    """The directory `compile_check` actually imported revl from.

    Asked after the run, not before, so it reports what graded the corpus."""
    path = _GRADED_BY
    if path is None:
        mod = sys.modules.get("revl")
        path = getattr(mod, "__file__", None) if mod else None
    if not path:
        return "revl was never imported"
    parent = Path(path).parent
    # Reported relative to the repository when it is inside it. Summaries are
    # committed and this repository is public, so an absolute path here would
    # publish the operator's directory layout. A compiler from outside the tree
    # is the case a reader needs to see, so it is named as such, but by its
    # directory name only: the fact a reader needs is that it was not this
    # checkout, and the rest of the path is the operator's layout again.
    try:
        return str(parent.relative_to(ROOT))
    except ValueError:
        return f"{parent.name} (outside this checkout)"


def write_summary(run_dir: Path, rows: list, raw_rows: list, args):
    finals = [r for r in rows if r.get("summary")
              and r["variant"] not in PROBED_VARIANTS]
    lines = ["# syntax-2.0 acceptance benchmark — run summary", "",
             f"runner: `{args.runner}`" + (f" · model: `{args.model}`" if args.model else ""),
             f"max iterations: {args.max_iters}",
             # Which compiler graded this. An editable install of revl registers
             # a meta-path finder that is consulted before sys.path, so a run
             # launched from the wrong interpreter can score against a different
             # checkout than the one it was pointed at and produce plausible,
             # wrong numbers. The path is printed rather than assumed.
             f"scored by: `{scoring_compiler()}`", ""]
    revl_variants = [v for v in args.variants.split(",")
                     if v not in PROBED_VARIANTS]
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

    mcp_rows = [r for r in rows if r.get("variant") == MCP_VARIANT]
    if mcp_rows:
        lines += ["", "## mcp: probe-scored (the third host)", ""]
        lines += score_mcp.render_summary(mcp_rows, args.raw_ts_cycles)
        lines.append("")

    (run_dir / "summary.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
