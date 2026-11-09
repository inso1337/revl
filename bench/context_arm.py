#!/usr/bin/env python3
"""Rounds-to-green with and without the declarations pack (docs/context.md).

Each task in `context_tasks.json` removes one component from a real
composition and asks a model to write it back. The two arms differ in one
thing, what the model reads before writing:

  raw   the composition with the component removed, as a file;
  pack  `revl context FILE --component C`: the declarations C compiles
        against and no provider body.

Everything else is shared: the system prompt (`prompts/v2.md`), the task
text, the component's header, temperature 0, and the retry loop. The reply is
the component block alone; it is spliced back where the original stood and
the whole file is compiled under the composition's own path, so `use`
lines and `asset` paths resolve as they do for the original. A rejection goes back to the model with the
compiler's text, `bench/run.py`'s RETRY_TEMPLATE, up to `--max-iters`.

A cell's rounds are the attempt that compiled, or `max_iters + 1` when none
did, so an unsolved cell counts against its arm instead of dropping out.
Input tokens are what the endpoint reports, summed over the cell's attempts.

The pack is promoted into the authoring loop only if it does not lose rounds
and still wins on tokens (docs/context.md). `summary.md` states both.

Usage:
  python3 bench/context_arm.py --runner mock                 # pipeline check
  python3 bench/context_arm.py --runner local \\
      --base-url https://api.deepseek.com/v1 --model deepseek-chat \\
      --api-key-env DEEPSEEK_API_KEY --reps 5 --label ctx-deepseek
"""

import argparse
import json
import statistics
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
TASKS = BENCH / "context_tasks.json"
ARMS = ("raw", "pack")

sys.path.insert(0, str(ROOT / "src"))
from revl import RevlError, compile_files  # noqa: E402
from revl.context import context, count_tokens  # noqa: E402
from revl.lexer import lex  # noqa: E402


def _load_run():
    import importlib.util  # noqa: PLC0415
    spec = importlib.util.spec_from_file_location("bench_run_ctx", BENCH / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run = _load_run()

FIRST_TEMPLATE = (
    "{reading}\n\n"
    "Write the component `{component}`, with exactly this header:\n\n"
    "```revl\n{header}\n```\n\n"
    "{task}\n\n"
    "Reply with only the `{component}` block (its header and body) in one "
    "fenced code block. It is placed back into the composition where it "
    "stood, and the whole file is compiled."
)

# run.py's RETRY_TEMPLATE asks for the whole file, which the splice can never
# take; this one asks again for the block alone
RETRY_TEMPLATE = (
    "{task}\n\nYour previous attempt:\n\n```revl\n{code}```\n\n"
    "It was rejected:\n\n```\n{error}\n```\n\n"
    "Reply with only the corrected `{component}` block (its header and body) "
    "in one fenced code block."
)

RAW_READING = ("Here is the composition the component belongs to, with the "
               "component removed:\n\n```revl\n{text}```")
IMPORTED_READING = "\n\nAnd `{path}`, which it imports:\n\n```revl\n{text}```"
PACK_READING = ("Here are the declarations the component compiles against "
                "(`revl context`: no provider body):\n\n```revl\n{text}```")


def load_tasks(ids=None) -> list[dict]:
    tasks = json.loads(TASKS.read_text(encoding="utf-8"))["tasks"]
    if ids:
        wanted = set(ids)
        missing = wanted - {t["id"] for t in tasks}
        if missing:
            raise SystemExit(f"unknown task ids: {sorted(missing)}")
        tasks = [t for t in tasks if t["id"] in wanted]
    return tasks


def cut(task: dict) -> dict:
    """The composition with the component's lines removed, the block those
    lines held, and where it goes back. The comment lines directly above the
    component go too, so the raw arm does not read a description of the
    answer the pack arm never sees."""
    path = ROOT / task["file"]
    text = path.read_text(encoding="utf-8")
    tokens = [t for t in lex(text, str(path)) if t.kind != "eof"]
    at = next(k for k, t in enumerate(tokens)
              if t.kind == "kw" and t.value == "component"
              and tokens[k + 1].value == task["component"])
    if at and tokens[at - 1].kind == "kw" and tokens[at - 1].value == "pub":
        at -= 1
    if at and tokens[at - 1].line == tokens[at].line:
        raise SystemExit(f"{task['id']}: the component shares its first line")
    depth, end = 0, None
    for k in range(at, len(tokens)):
        if tokens[k].kind == "{":
            depth += 1
        elif tokens[k].kind == "}":
            depth -= 1
            if depth == 0:
                end = k
                break
    if end + 1 < len(tokens) and tokens[end + 1].line == tokens[end].line:
        raise SystemExit(f"{task['id']}: the component shares its last line")
    lines = text.splitlines(keepends=True)
    first, last = tokens[at].line - 1, tokens[end].line
    block = "".join(lines[first:last])
    while first and lines[first - 1].lstrip().startswith("//"):
        first -= 1
    # an in-memory compile reads nothing from disk, so every file an `asset`
    # in the original names rides along in the sources map
    assets = {}
    for k, t in enumerate(tokens[:-1]):
        if t.value == "asset" and tokens[k + 1].kind == "string":
            asset = (path.parent / tokens[k + 1].value).resolve()
            assets[str(asset)] = asset.read_text(encoding="utf-8")
    return {"path": path, "holed": "".join(lines[:first] + lines[last:]),
            "at": first, "block": block, "assets": assets}


def splice(holed: str, at: int, code: str) -> str:
    lines = holed.splitlines(keepends=True)
    return "".join(lines[:at]) + code.rstrip("\n") + "\n" + "".join(lines[at:])


def raw_reading(piece: dict, pack: dict) -> str:
    """The holed composition, then every other file the pack drew a
    declaration from, verbatim: the raw arm reads the same sources the pack
    was cut from, which is what `tokens.raw` counts (docs/context.md)."""
    text = RAW_READING.format(text=piece["holed"])
    for other in pack["tokens"]["rawFiles"]:
        if Path(other).resolve() != piece["path"].resolve():
            body = Path(other).resolve().read_text(encoding="utf-8")
            text += IMPORTED_READING.format(
                path=Path(other).resolve().relative_to(ROOT), text=body)
    return text


def reading(arm: str, piece: dict, pack: dict) -> str:
    return (raw_reading(piece, pack) if arm == "raw"
            else PACK_READING.format(text=pack["pack"].rstrip("\n") + "\n"))


def prompt(task: dict, arm: str, piece: dict, pack: dict) -> str:
    return FIRST_TEMPLATE.format(reading=reading(arm, piece, pack),
                                 component=task["component"],
                                 header=pack["header"], task=task["task"])


def shape(component: str, code: str) -> str | None:
    """Why the reply is not exactly one `component <C>` block, or None. A
    composition whose removed component nothing depends on still compiles
    without it, so an empty reply, a renamed component or an extra
    declaration would otherwise be graded green."""
    want = f"exactly one `component {component}` block and nothing else"
    try:
        tokens = [t for t in lex(code, "<reply>") if t.kind != "eof"]
    except RevlError as exc:
        return f"the reply does not lex ({exc}); send {want}"
    k = 1 if tokens and tokens[0].kind == "kw" and tokens[0].value == "pub" else 0
    if len(tokens) < k + 2 or tokens[k].value != "component" \
            or tokens[k + 1].value != component:
        return f"the reply must be {want}"
    depth = 0
    for i in range(k, len(tokens)):
        if tokens[i].kind == "{":
            depth += 1
        elif tokens[i].kind == "}":
            depth -= 1
            if depth == 0:
                return None if i == len(tokens) - 1 else \
                    f"the reply has more after the component; send {want}"
    return None  # an unclosed block: the compiler names it


def grade(piece: dict, code: str, component: str):
    """(ok, error). The reply must be the component alone; it is then spliced
    and compiled under the composition's own path, so its `use` lines and
    `asset` paths resolve the way the original's do, and nothing is written
    to disk."""
    wrong = shape(component, code)
    if wrong:
        return False, wrong
    path = str(piece["path"])
    try:
        compile_files([path], sources={
            **piece["assets"],
            path: splice(piece["holed"], piece["at"], code)})
        return True, None
    except RevlError as exc:
        return False, str(exc)
    except Exception as exc:  # a compiler crash is the compiler's, not the model's
        return False, f"[compiler crash] {type(exc).__name__}: {exc}"


def _mock(task: dict, piece: dict, attempt: int) -> dict:
    # attempt 1 is cut short (a parse error), attempt 2 is the original: the
    # retry loop, the splice and the grader all run, no model is called
    code = (piece["block"] if attempt > 1
            else piece["block"].split("{", 1)[0] + "{\n")
    return {"text": f"```revl\n{code}```", "tokens_in": 0, "output_tokens": 0}


def _model(args) -> str:
    return "mock" if args.runner == "mock" else args.model


def _total(attempts: list, key: str):
    values = [a.get(key) for a in attempts if not a.get("runner_error")]
    return None if any(v is None for v in values) else sum(values)


def run_cell(task: dict, arm: str, rep: int, args, system: str) -> dict:
    piece = cut(task)
    pack = context([str(piece["path"])], component=task["component"])
    first = prompt(task, arm, piece, pack)
    current, attempts = first, []
    for attempt in range(1, args.max_iters + 1):
        try:
            if args.runner == "mock":
                reply = _mock(task, piece, attempt)
            else:
                reply = run.run_local(system, current, args.model, args.base_url,
                                      args.timeout, args.max_tokens,
                                      args.api_key_env)
        except RuntimeError as exc:
            attempts.append({"attempt": attempt, "ok": False, "error": str(exc),
                             "runner_error": True})
            break
        code = run.extract_code(reply["text"])
        ok, error = grade(piece, code, task["component"])
        attempts.append({"attempt": attempt, "ok": ok, "error": error,
                         "code": code,
                         "tokens_in": reply.get("tokens_in"),
                         "output_tokens": reply.get("output_tokens"),
                         # a draft scored off the reasoning channel, or a
                         # reply cut at the cap, is not the same measurement
                         "answer_from_reasoning": reply.get("answer_from_reasoning"),
                         "finish_reason": reply.get("finish_reason")})
        if ok:
            break
        current = RETRY_TEMPLATE.format(task=first, code=code, error=error,
                                        component=task["component"])
    solved = bool(attempts) and attempts[-1]["ok"]
    return {
        "task": task["id"], "arm": arm, "rep": rep,
        "model": _model(args), "max_iters": args.max_iters,
        "solved": solved,
        "rounds": len(attempts) if solved else args.max_iters + 1,
        # None when any attempt's usage was not reported: a partial sum
        # would read as a cheaper cell
        "tokens_in": _total(attempts, "tokens_in"),
        "output_tokens": _total(attempts, "output_tokens"),
        # what this arm reads, by the pack's own proxy
        "reading_tokens": count_tokens(reading(arm, piece, pack)),
        "runner_error": any(a.get("runner_error") for a in attempts),
        "attempts": attempts,
    }


def _mean(xs):
    return statistics.mean(xs) if xs else float("nan")


def summarize(rows: list, max_iters: int) -> str:
    by = {}
    for r in rows:
        by.setdefault((r["task"], r["arm"]), []).append(r)
    tasks = sorted({r["task"] for r in rows})
    out = [f"# Rounds-to-green, raw vs pack (max_iters {max_iters}; "
           f"unsolved = {max_iters + 1})", "",
           "| task | raw rounds | pack rounds | raw solved | pack solved | "
           "reading raw / pack | raw tokens in | pack tokens in |",
           "|---|---|---|---|---|---|---|---|"]
    diffs, tok_raw, tok_pack, unknown = [], [], [], False
    for t in tasks:
        # a cell a runner error cut short measured the endpoint, not the arm
        raw = [r for r in by.get((t, "raw"), []) if not r["runner_error"]]
        pack = [r for r in by.get((t, "pack"), []) if not r["runner_error"]]
        rr, pr = _mean([r["rounds"] for r in raw]), _mean([r["rounds"] for r in pack])
        known = all(r["tokens_in"] is not None for r in raw + pack)
        unknown = unknown or not known
        ti_r = _mean([r["tokens_in"] for r in raw]) if known else float("nan")
        ti_p = _mean([r["tokens_in"] for r in pack]) if known else float("nan")
        if raw and pack:
            diffs.append(pr - rr)
            tok_raw.append(ti_r)
            tok_pack.append(ti_p)
        reading = "/".join(str(rs[0]["reading_tokens"]) if rs else "-"
                           for rs in (raw, pack))
        out.append(f"| {t} | {rr:.2f} | {pr:.2f} | "
                   f"{sum(r['solved'] for r in raw)}/{len(raw)} | "
                   f"{sum(r['solved'] for r in pack)}/{len(pack)} | "
                   f"{reading} | {ti_r:.0f} | {ti_p:.0f} |")
    errors = sum(r["runner_error"] for r in rows)
    out.append("")
    out.append("`reading` is the proxy token count of what the arm reads (the "
               "file, or the pack); `tokens in` is what the endpoint reported, "
               "system prompt and retries included.")
    out.append("")
    ratio = None
    if diffs:
        loses = _mean(diffs) > 0
        out.append(f"- pack minus raw, mean rounds per task: {_mean(diffs):+.2f} "
                   f"({'the pack LOSES rounds' if loses else 'the pack does not lose rounds'})")
        out.append(f"- tasks where the pack took more rounds: "
                   f"{sum(d > 0 for d in diffs)} of {len(diffs)}")
        if unknown:
            out.append("- input tokens, pack / raw: unknown (an attempt's usage "
                       "was not reported)")
        elif sum(tok_raw):
            ratio = sum(tok_pack) / sum(tok_raw)
            out.append(f"- input tokens, pack / raw: {ratio:.2f}")
        else:
            out.append("- input tokens, pack / raw: not reported (no endpoint "
                       "usage)")
    if errors:
        out.append(f"- cells cut short by a runner error: {errors}, left out "
                   f"of the means; rerun with --resume")
    # the rule in docs/context.md: promote only if the pack does not lose
    # rounds AND wins on tokens, judged on complete runs only
    if errors or not diffs or ratio is None:
        out.append("- verdict: none (the run is incomplete or reported no usage)")
    elif not loses and ratio < 1:
        out.append("- verdict: PROMOTE (no rounds lost, fewer input tokens)")
    else:
        out.append("- verdict: HOLD (" + ("loses rounds" if loses
                                          else "no token win") + ")")
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runner", choices=["mock", "local"], required=True)
    ap.add_argument("--tasks", default=None, help="comma-separated task ids")
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--max-iters", type=int, default=3)
    ap.add_argument("--model", default=None)
    ap.add_argument("--base-url", default="http://localhost:1234/v1")
    ap.add_argument("--api-key-env", default=None,
                    help="the NAME of the environment variable holding the "
                         "endpoint's key; the key is never printed or stored")
    ap.add_argument("--max-tokens", type=int, default=run.DEFAULT_LOCAL_MAX_TOKENS)
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--label", default=None)
    ap.add_argument("--out", default=str(BENCH / "results"),
                    help="the directory that holds the run's directory")
    ap.add_argument("--resume", action="store_true",
                    help="keep the rows already in the run's directory and run "
                         "only the cells that have none")
    args = ap.parse_args(argv)
    if args.runner == "local" and not args.model:
        raise SystemExit("--runner local needs --model")
    arms = args.arms.split(",")
    if set(arms) - set(ARMS):
        raise SystemExit(f"unknown arms: {sorted(set(arms) - set(ARMS))}")

    tasks = load_tasks(args.tasks.split(",") if args.tasks else None)
    system = run.load_variant_prompt("v2")
    run_dir = Path(args.out) / (args.label or f"context-arm-{args.runner}")
    run_dir.mkdir(parents=True, exist_ok=True)
    rows_path = run_dir / "results.jsonl"
    rows = []
    if args.resume and rows_path.exists():
        rows = [json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        # a row from another model or another iteration cap is another
        # measurement, and a runner error is no measurement
        rows = [r for r in rows if not r["runner_error"]
                and r.get("model") == _model(args)
                and r.get("max_iters") == args.max_iters]
    done = {(r["task"], r["arm"], r["rep"]) for r in rows}
    with rows_path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
        for rep in range(1, args.reps + 1):
            for task in tasks:
                for arm in arms:
                    if (task["id"], arm, rep) in done:
                        continue
                    row = run_cell(task, arm, rep, args, system)
                    rows.append(row)
                    fh.write(json.dumps(row) + "\n")
                    fh.flush()
                    print(f"{task['id']:20} {arm:5} rep {rep}: "
                          f"{'green' if row['solved'] else 'red'} in "
                          f"{row['rounds']} round(s), "
                          f"{row['tokens_in'] if row['tokens_in'] is not None else '?'} "
                          f"tokens in",
                          flush=True)
    summary = summarize(rows, args.max_iters)
    (run_dir / "summary.md").write_text(summary, encoding="utf-8")
    print(summary)
    return rows


if __name__ == "__main__":
    main()
