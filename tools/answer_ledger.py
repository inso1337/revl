#!/usr/bin/env python3
"""The MCP answer ledger: what one answer costs today (issue #2859).

The handles/profile work (#2852, #2853) has to be judged on numbers, not on a
claim that an answer "feels smaller". This tool pins the numbers for a small,
representative set of MCP calls against example components that already exist
in the tree. It is a BASELINE: it changes no verb and no answer shape.

What is measured, per call, from the answer a client actually receives:

  * **answer bytes** - the length of the `text` content block in the
    JSON-RPC result, which is what an agent pays context for.
  * **estimated tokens** - that same text through `bench/tokens.py`'s BPE proxy
    (`count_tokens`), imported rather than re-implemented, so the ledger and
    the token-economy metric cannot drift apart.
  * **echoed source/IR characters** - the part of the answer that repeats
    content the caller or the session already holds: a nested string value of
    at least 32 characters that appears verbatim in the submitted source, and a
    nested component IR document (a dict with `components` of named records and
    `loadOrder`, the shape `revl_audit` returns). Values are counted
    compactly, so the number is a lower bound on the indented answer bytes they
    occupy. These are the characters a content handle could replace with a
    name.

Everything runs IN PROCESS through `revl.mcp.server.handle`, the same dispatch
a transport uses: no network, no model, no provider, no cordis boot. Each run
starts from a fresh session, clears the repeated-refusal memory and pins the
build identity to fixed placeholders, so the table is byte-stable across runs
and across commits. The pinned identity is why a `revl_state` answer does not
churn with every commit; it says nothing about the answer shape.

Usage:
  python3 tools/answer_ledger.py            # print the table
  python3 tools/answer_ledger.py --write    # rewrite the committed table
  python3 tools/answer_ledger.py --compare [PATH]
                                            # reproduce the committed table;
                                            # exit 1 on any difference (CI)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _import_root in (ROOT / "src", ROOT / "bench"):
    if str(_import_root) not in sys.path:
        sys.path.insert(0, str(_import_root))

import tokens as token_proxy  # noqa: E402  (bench/tokens.py: the one tokenizer)
from revl.mcp import identity, repeat, server  # noqa: E402
from revl.mcp.server import Session, handle  # noqa: E402

#: The committed table, in the repository's results location.
DEFAULT_DOC = ROOT / "bench" / "results" / "answer-ledger.md"

#: The shortest answer string that can count as an echo: below this, a value is
#: a name or a word that may occur in any source by chance, not a copy of it.
MIN_ECHO_CHARS = 32

#: Fixed identity placeholders. `revision` changes with every commit, so the
#: real value would make the committed table stale on arrival; the ledger
#: measures answer content, not the build that produced it.
PINNED_REVISION = "0" * 40
PINNED_SOURCE_DIGEST = "0" * 64

#: Sentinel for "this root had no cached identity before the run".
_MISSING = object()


@dataclass(frozen=True)
class Call:
    """One measured call: a stable id, the verb, a component label and args."""

    id: str
    verb: str
    component: str
    arguments: dict


@dataclass(frozen=True)
class Row:
    """One answer's measured cost."""

    id: str
    verb: str
    component: str
    answer_bytes: int
    est_tokens: int
    echoed_chars: int


def _example(relative: str, **_extra) -> dict:
    """The call arguments for one example component, read from the tree."""
    arguments = {"source": (ROOT / relative).read_text(encoding="utf-8")}
    arguments.update(_extra)
    return arguments


def default_calls() -> list[Call]:
    """The representative set: query, state, check, compile-like, one refusal.

    The components are existing examples, picked for size and for what their
    answers carry: `pulse` and `counter_pair` are small, `beacon` has a
    two-component composition. Order is part of the measurement (the session
    footer and the attempt counter are stateful), so it is fixed here.
    """
    return [
        Call("state-idle", "revl_state", "-", {}),
        Call("check-pulse", "revl_check", "pulse.rvl",
             _example("examples/pulse.rvl")),
        Call("plan-counter-pair", "revl_plan", "counter_pair.rvl",
             _example("examples/counter_pair.rvl")),
        Call("audit-beacon", "revl_audit", "beacon.rvl",
             _example("examples/beacon.rvl")),
        Call("source-counter", "revl_source", "counter_pair.rvl",
             _example("examples/counter_pair.rvl", symbol="Counter")),
        Call("explain-g4", "revl_explain", "-", {"code": "G4"}),
        Call("fmt-pulse", "revl_fmt", "pulse.rvl",
             _example("examples/pulse.rvl")),
        Call("refusal-ship-no-manifest", "revl_ship", "pulse.rvl",
             _example("examples/pulse.rvl")),
    ]


# ------------------------------------------------------------------ measuring


def _is_ir_document(value) -> bool:
    """A component IR document: `components` of named records + `loadOrder`.

    This is the shape `revl_audit` returns under `manifest`. A summary block
    such as `revl_plan`'s `running` has the same key names but plain string
    lists, so it is not counted as an IR echo.
    """
    if not isinstance(value, dict):
        return False
    components = value.get("components")
    return (isinstance(components, list) and bool(components)
            and all(isinstance(c, dict) and "name" in c for c in components)
            and isinstance(value.get("loadOrder"), list))


def _submitted_sources(arguments: dict) -> list[str]:
    """The source text a call submitted, for the verbatim-echo test."""
    sources = []
    source = arguments.get("source")
    if isinstance(source, str) and source:
        sources.append(source)
    for path in arguments.get("files") or ():
        try:
            sources.append((ROOT / path).read_text(encoding="utf-8"))
        except OSError:
            pass
    return sources


def _serialised_size(value) -> int:
    """The characters a value occupies when the answer serialises it."""
    return len(json.dumps(value))


def echoed_chars(payload: dict, sources: list[str]) -> int:
    """Count the answer characters that repeat submitted source or compiled IR.

    A string leaf counts when it is at least `MIN_ECHO_CHARS` long and is a
    verbatim substring of a submitted source. A nested IR document counts
    whole, and its inner strings are not counted again. The answer root is not
    tested: `revl_check` carries `components` and `loadOrder` as a summary at
    the top level, and that is not an IR document. Each counted value is
    serialised compactly, so the figure is a lower bound on the indented
    characters the answer spends on it.
    """
    total = 0
    stack = list(payload.values())
    while stack:
        value = stack.pop()
        if isinstance(value, str):
            if len(value) >= MIN_ECHO_CHARS and any(value in s for s in sources):
                total += _serialised_size(value)
        elif isinstance(value, dict):
            if _is_ir_document(value):
                total += _serialised_size(value)
            else:
                stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return total


def measure(calls: list[Call]) -> list[Row]:
    """Run every call once from a fresh session and measure its answer."""
    root = identity.package_root()
    previous_session = server.SESSION
    previous_identity = identity._CACHE.get(root, _MISSING)
    previous_repeat = dict(repeat._LAST)
    server.SESSION = Session()
    repeat.forget()
    identity._CACHE[root] = {"revision": PINNED_REVISION,
                             "source_digest": PINNED_SOURCE_DIGEST}
    try:
        rows = []
        for call in calls:
            response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": call.verb,
                                          "arguments": call.arguments}})
            result = response["result"]
            text = result["content"][0]["text"]
            rows.append(Row(
                id=call.id,
                verb=call.verb,
                component=call.component,
                answer_bytes=len(text.encode("utf-8")),
                est_tokens=token_proxy.count_tokens(text),
                echoed_chars=echoed_chars(result["structuredContent"],
                                          _submitted_sources(call.arguments)),
            ))
        return rows
    finally:
        server.SESSION = previous_session
        if previous_identity is _MISSING:
            identity._CACHE.pop(root, None)
        else:
            identity._CACHE[root] = previous_identity
        with repeat._LOCK:
            repeat._LAST.update(previous_repeat)


# ------------------------------------------------------------------ rendering


def total(rows: list[Row]) -> Row:
    """The sum row. `id`/`verb`/`component` are labels, not measurements."""
    return Row(
        id="total",
        verb="",
        component="",
        answer_bytes=sum(r.answer_bytes for r in rows),
        est_tokens=sum(r.est_tokens for r in rows),
        echoed_chars=sum(r.echoed_chars for r in rows),
    )


def render(rows: list[Row]) -> str:
    """The committed markdown, byte-for-byte reproducible."""
    sums = total(rows)
    share = (100.0 * sums.echoed_chars / sums.answer_bytes
             if sums.answer_bytes else 0.0)
    lines = [
        "# Answer ledger",
        "",
        "What one MCP answer costs today: a deterministic, model-free baseline",
        "for the handles and profile work (#2852, #2853). This is a measurement,",
        "not a change: no verb and no answer shape moves here. Every number is",
        "produced by `tools/answer_ledger.py` from the answer a client actually",
        "receives, so it can be reviewed in a diff and re-run in CI.",
        "",
        "## Result",
        "",
        "| call | verb | component | answer bytes | est. tokens | echoed chars |",
        "|---|---|---|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(f"| `{row.id}` | `{row.verb}` | {row.component} | "
                     f"{row.answer_bytes} | {row.est_tokens} | "
                     f"{row.echoed_chars} |")
    lines += [
        f"| **total ({len(rows)} calls)** | | | **{sums.answer_bytes}** | "
        f"**{sums.est_tokens}** | **{sums.echoed_chars}** |",
        "",
        f"Echoed source and IR is {sums.echoed_chars} compact-serialised characters",
        f"against {sums.answer_bytes} answer bytes ({share:.1f}%): the part of the",
        "spend that repeats content the caller or the session already holds. The",
        "figure is a lower bound, because the answer is indented and the echoed",
        "values are counted without indentation. Everything else is a verdict, a",
        "summary or a diagnostic the caller did not have before.",
        "",
        "## Methodology",
        "",
        "- **In process, not over a transport:** each call goes through the same",
        "  `handle` dispatch a server uses, with a fresh session per run. No",
        "  network, no model, no provider, no cordis boot.",
        "- **Components:** `examples/pulse.rvl`, `examples/counter_pair.rvl` and",
        "  `examples/beacon.rvl`, the existing examples named in the table. The",
        "  set covers query (`revl_source`, `revl_explain`), state",
        "  (`revl_state`), check (`revl_check`), compile-like (`revl_plan`,",
        "  `revl_audit`, `revl_fmt`) and one refusal (`revl_ship` without the",
        "  running manifest).",
        "- **Answer bytes:** the length of the JSON-RPC `content[0].text` block,",
        "  exactly the string an agent receives.",
        "- **Estimated tokens:** `bench/tokens.py`'s `count_tokens` on that same",
        "  text. The ledger imports the tokenizer instead of copying it.",
        "- **Echoed characters:** a nested string value of at least 32",
        "  characters that appears verbatim in the submitted source, plus a",
        "  nested component IR document (a dict with `components` of named",
        "  records and `loadOrder`). The answer root is not counted: the",
        "  `components`/`loadOrder` summary `revl_check` carries at the top level",
        "  is not an IR document. Values are serialised compactly, without",
        "  indentation, so the count is a lower bound on the answer bytes they",
        "  occupy.",
        "- **Determinism:** fixed call order, a fresh session, the",
        "  repeated-refusal memory cleared for the run, and the build identity",
        "  pinned to fixed placeholders. Two runs in one process and two runs in",
        "  separate processes produce the same table.",
        "",
        "## Re-run",
        "",
        "```",
        "python3 tools/answer_ledger.py            # print the table",
        "python3 tools/answer_ledger.py --write    # rewrite this file",
        "python3 tools/answer_ledger.py --compare  # reproduce it; exit 1 on drift",
        "```",
        "",
        "`tests/test_answer_ledger_2859.py` measures a temporary fixture, checks",
        "that two runs agree, and checks that `--compare` is green against this",
        "file and red when a committed row is wrong.",
        "",
    ]
    return "\n".join(lines)


def compare(rows: list[Row], path: Path) -> int:
    """0 when `path` is byte-identical to a fresh render, 1 when it is not."""
    rendered = render(rows)
    try:
        committed = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"answer ledger: cannot read {path}: {exc}", file=sys.stderr)
        return 1
    if rendered == committed:
        print(f"answer ledger: reproduced {path} ({len(rows)} calls, "
              f"{total(rows).answer_bytes} answer bytes, "
              f"{total(rows).est_tokens} est. tokens)")
        return 0
    print(f"answer ledger: {path} does not match a fresh run", file=sys.stderr)
    expected = rendered.splitlines()
    actual = committed.splitlines()
    for line_no in range(max(len(expected), len(actual))):
        want = expected[line_no] if line_no < len(expected) else "<missing>"
        got = actual[line_no] if line_no < len(actual) else "<missing>"
        if want != got:
            print(f"  first difference at line {line_no + 1}:", file=sys.stderr)
            print(f"  committed: {got}", file=sys.stderr)
            print(f"  fresh:     {want}", file=sys.stderr)
            break
    print("  run `python3 tools/answer_ledger.py --write` and review the diff",
          file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true",
                      help=f"rewrite {DEFAULT_DOC.relative_to(ROOT)}")
    mode.add_argument("--compare", nargs="?", const=str(DEFAULT_DOC),
                      metavar="PATH",
                      help="reproduce the committed table; exit 1 on difference")
    args = ap.parse_args(argv)

    rows = measure(default_calls())
    print(render(rows), end="")

    if args.write:
        DEFAULT_DOC.write_text(render(rows), encoding="utf-8")
        print(f"\nanswer ledger: wrote {DEFAULT_DOC.relative_to(ROOT)}")
        return 0
    if args.compare is not None:
        return compare(rows, Path(args.compare))
    return 0


if __name__ == "__main__":
    sys.exit(main())
