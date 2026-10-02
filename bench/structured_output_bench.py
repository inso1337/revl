#!/usr/bin/env python3
"""Issue #1462: constrained decoding through the runtime adapters, measured.

Where `bench/decode_grammar_probe.py` (item 513) attached an artifact by hand,
this goes through the whole runtime path a `revl run --providers` crossing
takes: a compiled `validated` operation, the py runtime's grammar registry, the
model host from `revl.providers`, the OpenAI-compatible adapter in one
`structured_output` mode, and item 257's `validate_retry` with no retry budget.
Each sample is one crossing.

Arms (the binding's `structured_output` mode):

  * `none`        - no constraint attached; the status quo item 257 validates.
  * `json-schema` - the wire schema as `response_format`, CLAIMED.
  * `gbnf`        - the GBNF text as llama.cpp's `grammar` field, CLAIMED.

Per sample: whether the crossing admitted the value (`valid`), the fault it
raised otherwise, whether the raw completion is inside the GBNF byte for byte
(`in_grammar`, needs llguidance), completion tokens, prompt tokens and wall
clock latency. Every raw completion is written out so a number can be re-derived
rather than trusted. It needs a running local server and is never run by CI.

    python3 bench/structured_output_bench.py --model <tag> \\
        --arms none,json-schema,gbnf --n 3 --write

With `--write` it writes
`bench/results/structured-output/<model>-<case>-<arms>.json` and a markdown
summary beside it, stamped with the machine and the model's
digest from the server's `/api/tags` when it has one (Ollama).
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import platform
import sys
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from revl.compiler import compile_source  # noqa: E402
from revl.decode_grammar import json_schema_grammar_for  # noqa: E402
from revl.parser import Parser  # noqa: E402
from revl.providers import (  # noqa: E402
    ProviderError, build_hosts, parse_config, placement_of_program,
    request_json,
)
from revl.providers import structured as st  # noqa: E402

# `from runtime import`, the spelling `bench/decode_grammar_probe.py` uses;
# the module object itself is what the model host attaches to, so it is taken
# from `sys.modules` rather than imported a second time under another name.
from runtime import register_grammars  # noqa: E402

rt = sys.modules["runtime"]

RESULTS = BENCH / "results" / "structured-output"


def _probe_cases():
    """The prompts and types item 513's probe measured, loaded by path so the
    two tools score the same questions (and no bare `import` can bind the
    wrong module)."""
    spec = importlib.util.spec_from_file_location(
        "revl_bench_decode_grammar_probe", BENCH / "decode_grammar_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CASES


def _render_type(name: str, decl: dict) -> str:
    if decl["kind"] == "record":
        fields = ", ".join(f"{k}: {v}" for k, v in decl["fields"].items())
        return f"type {name} = {{ {fields} }}"
    cases = " | ".join(f"{c['name']}({c['payload']})" if c.get("payload")
                       else c["name"] for c in decl["cases"])
    return f"type {name} = {cases}"


def program_for(case: dict) -> str:
    """A one-operation revl program whose `validated` crossing returns the
    case's type, placed on an on_device role."""
    types = "\n".join(_render_type(n, d) for n, d in case["types"].items())
    return f"""
model role local on_device reaches []
{types}
service Model {{
  emission[model.local] validated fn answer(system: Str, prompt: Str) -> {case['surface']}
}}
service Loop {{ emission fn run(p: Str) -> Int }}
component Bench requires model: Model provides loop: Loop {{
  provide loop {{
    fn run(p) {{
      let a = emit model.answer("s", p)
      return 1
    }}
  }}
}}
"""


def _machine() -> str:
    """Coarse, like `bench/model_pin.py`'s: enough to read a latency in
    context. No hostname and no CPU serial: this file is published."""
    return (f"{platform.system()} {platform.release()} {platform.machine()} · "
            f"Python {platform.python_version()}")


def _model_digest(base_url: str, model: str):
    root = base_url.rstrip("/").removesuffix("/v1")
    try:
        tags = request_json(root + "/api/tags", timeout=10, label="bench")
    except ProviderError:
        return None
    for entry in tags.get("models") or ():
        if entry.get("name") == model or entry.get("model") == model:
            return {"digest": entry.get("digest"),
                    "size": entry.get("size"),
                    "quantization": (entry.get("details") or {})
                    .get("quantization_level"),
                    "parameter_size": (entry.get("details") or {})
                    .get("parameter_size")}
    return None


def run_arm(case, ir, placement, arm, args, recogniser):
    config = parse_config({"roles": {"local": {
        "provider": "openai-compatible", "base_url": args.base_url,
        "model": args.model, "max_tokens": args.max_tokens,
        "timeout": args.timeout, "structured_output": arm}}}, "bench")
    host = build_hosts(ir, placement, config)["model"]
    host._revl_attach_runtime(rt)
    spec = ir["services"]["Model"]["methods"]["answer"]
    rows = []
    for prompt in case["prompts"][:args.n]:
        started = time.monotonic()
        row = {"arm": arm, "prompt": prompt}
        try:
            rt.validate_retry(lambda: host.answer(case["system"], prompt), 0,
                              spec["response_schema"], "Model.answer", None,
                              grammar="Model.answer")
            row["valid"], row["fault"] = True, None
        except rt.ResponseValidationError as exc:
            row["valid"] = False
            row["fault"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        except ProviderError as exc:
            row["valid"] = False
            row["fault"] = f"ProviderError: {str(exc)[:300]}"
        row["secs"] = round(time.monotonic() - started, 1)
        completion = host.last_completion
        if completion is not None:
            row.update(text=completion.text,
                       tokens_out=completion.tokens_out,
                       tokens_in=completion.tokens_in,
                       reasoning_tokens=completion.reasoning_tokens,
                       reasoning_chars=len(completion.reasoning or ""),
                       finish_reason=completion.finish_reason)
            if recogniser is not None:
                err = recogniser.error(completion.text)
                row["in_grammar"], row["grammar_error"] = err is None, err
        host._revl_last = None
        rows.append(row)
        print(f"[{arm:11s}] valid={row['valid']:d} "
              f"in_grammar={row.get('in_grammar')} {row['secs']}s "
              f"tokens_out={row.get('tokens_out')} "
              f"{(row.get('text') or row.get('fault') or '')!r:.80}",
              flush=True)
    return rows


def summarise(rows) -> list:
    out = []
    for arm in dict.fromkeys(r["arm"] for r in rows):
        mine = [r for r in rows if r["arm"] == arm]
        toks = [r["tokens_out"] for r in mine
                if isinstance(r.get("tokens_out"), int)]
        secs = [r["secs"] for r in mine]
        grammar = [r for r in mine if "in_grammar" in r]
        out.append({
            "arm": arm, "n": len(mine),
            "valid": sum(r["valid"] for r in mine),
            "in_grammar": (sum(r["in_grammar"] for r in grammar)
                           if grammar else None),
            "tokens_out_mean": round(sum(toks) / len(toks), 1) if toks else None,
            "latency_s_mean": round(sum(secs) / len(secs), 1) if secs else None,
            "latency_s_max": max(secs) if secs else None,
        })
    return out


def render(doc: dict) -> str:
    lines = [
        f"# Structured output through the runtime adapter: {doc['case']}",
        "",
        f"Measured {doc['date']} with `bench/structured_output_bench.py` "
        f"(issue #1462).",
        "",
        f"- Machine: {doc['machine']}",
        f"- Server: OpenAI-compatible endpoint at `{doc['base_url']}` "
        f"({doc.get('server') or 'server version not reported'})",
        f"- Model: `{doc['model']}`"
        + (f", digest `{doc['model_pin']['digest'][:12]}`, "
           f"{doc['model_pin'].get('parameter_size')}, "
           f"{doc['model_pin'].get('quantization')}"
           if doc.get("model_pin") else ", digest not reported"),
        f"- Temperature 0, `max_tokens` {doc['max_tokens']}, no retry "
        f"budget, {doc['n']} prompts per arm",
        f"- Load average at start: {doc.get('load_average')}",
        "",
        "| arm | n | valid | inside the GBNF | mean completion tokens "
        "| mean latency (s) | max latency (s) |",
        "| --- | - | ----- | --------------- | ---------------------- "
        "| ---------------- | --------------- |",
    ]
    for s in doc["summary"]:
        lines.append(
            f"| {s['arm']} | {s['n']} | {s['valid']} | "
            f"{'n/a' if s['in_grammar'] is None else s['in_grammar']} | "
            f"{s['tokens_out_mean']} | {s['latency_s_mean']} | "
            f"{s['latency_s_max']} |")
    lines += ["", "Every raw completion is in the JSON file beside this one.",
              ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    ap.add_argument("--model", required=True)
    ap.add_argument("--case", default="AgentTurn")
    ap.add_argument("--arms", default="none,json-schema,gbnf")
    ap.add_argument("--n", type=int, default=3, help="prompts per arm")
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    case = _probe_cases()[args.case]
    source = program_for(case)
    ir = compile_source(source, "bench.rvl")
    placement = placement_of_program(Parser(source, "bench.rvl").parse())
    spec = ir["services"]["Model"]["methods"]["answer"]
    entry = dict(spec["response_grammar"])
    entry["wire_schema"] = json_schema_grammar_for(spec["response_schema"])
    register_grammars({"Model.answer": entry})
    recogniser = (st.recogniser_for(entry["text"], entry["digest"])
                  if st.engine_available() else None)

    try:
        import os  # noqa: PLC0415
        load = round(os.getloadavg()[0], 1)
    except (AttributeError, OSError):
        load = None
    root = args.base_url.rstrip("/").removesuffix("/v1")
    try:
        server = request_json(root + "/api/version", timeout=10,
                              label="bench").get("version")
        server = f"Ollama {server}" if server else None
    except ProviderError:
        server = None

    # pinned BEFORE the run: a server that restarts mid-run must not leave the
    # record without the identity of the weights that answered
    pin = _model_digest(args.base_url, args.model)
    rows = []
    for arm in [a.strip() for a in args.arms.split(",") if a.strip()]:
        rows += run_arm(case, ir, placement, arm, args, recogniser)

    doc = {
        "date": datetime.date.today().isoformat(),
        "machine": _machine(), "load_average": load,
        "base_url": args.base_url, "server": server,
        "model": args.model, "model_pin": pin,
        "case": args.case, "n": args.n, "max_tokens": args.max_tokens,
        "grammar_digest": entry["digest"],
        "wire_schema_digest": entry["wire_schema"]["digest"],
        "engine": "llguidance" if recogniser is not None else None,
        "summary": summarise(rows), "samples": rows,
    }
    print()
    print(render(doc))
    unreached = [r for r in rows
                 if (r.get("fault") or "").startswith("ProviderError")]
    if args.write and unreached:
        # a sample that never reached the model is not a measurement, and a
        # results file is read as one; nothing is written
        print(f"not written: {len(unreached)} of {len(rows)} samples never "
              f"got an answer from the server (first: "
              f"{unreached[0]['fault'][:160]})", file=sys.stderr)
        return 1
    if args.write:
        RESULTS.mkdir(parents=True, exist_ok=True)
        # the arms are in the name, so a run split across invocations (a
        # call can take minutes on a local model) does not overwrite itself
        arms = "+".join(a.strip() for a in args.arms.split(",") if a.strip())
        stem = (f"{args.model.replace('/', '_').replace(':', '-')}-"
                f"{args.case}-{arms}")
        (RESULTS / f"{stem}.json").write_text(json.dumps(doc, indent=2) + "\n",
                                              encoding="utf-8")
        (RESULTS / f"{stem}.md").write_text(render(doc), encoding="utf-8")
        print(f"wrote {RESULTS / stem}.{{json,md}}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
