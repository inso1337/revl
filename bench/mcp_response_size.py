#!/usr/bin/env python3
"""MCP response sizes: how much text a verb returns into an agent's context.

The token economy already measures two protocol costs: the arguments an agent
must serialize to drive the MCP surface (`bench/results/token-surface-audit.md`)
and the round-trips it must walk (`revl_ship`, docs/token-economy.md). Neither
notices the third: the text a verb's RESPONSE puts back into the agent's
context, which is paid as input tokens on every later turn of the session. A
verbose refusal, a full IR echo or a long diagnostic list costs more than the
turn that asked for it, and nothing would fail if one of them grew.

This module measures that half. It drives a fixed scripted session through the
real dispatcher (`revl.mcp.server.handle`, in process, no transport, no model,
no live runtime) and counts the text of each tool result with the same
deterministic BPE proxy as `bench/tokens.py` (`count_tokens`):

  * a passing `revl_ship` rehearsal,
  * a refused `revl_ship` (a candidate that does not compile),
  * a `revl_plan`.

The running manifest is compiled and passed explicitly, so the script seeds no
live composition and every byte is a function of this checkout. The report
gives one row per call and per-verb count/median/max.

`tests/test_mcp_response_size.py` pins one ceiling per scripted call at the
measured size, so a change that makes a response grow has to raise the ceiling
in the same diff instead of silently adding input tokens to every later turn.

Usage:
  python3 bench/mcp_response_size.py
  python3 bench/mcp_response_size.py --json sizes.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
for _path in (BENCH, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import rescore  # noqa: E402
from tokens import count_tokens  # noqa: E402

# The fixed scripted session. The running composition seeds a cache service;
# the candidate adds a component that requires it and provides a new one. The
# refused candidate is a plain `let` in an activation body (G6): a real refusal
# with diagnostics, not a usage error.
_RUNNING = """
service Cache { fn get(key: Str) -> Opt[Str] }
component C provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache { fn get(key) = store.get(key) }
}
"""

_CANDIDATE = """
service Cache { fn get(key: Str) -> Opt[Str] }
service Log { fn note(m: Str) -> Int }
component Watcher requires cache: Cache provides log: Log {
  provide log { fn note(m) = 1 }
}
"""

_REFUSED = "component Broken { let x = nope }\n"

SCRIPT = (
    {"step": "ship-pass", "verb": "revl_ship",
     "source": _CANDIDATE},
    {"step": "ship-refused", "verb": "revl_ship",
     "source": _REFUSED},
    {"step": "plan", "verb": "revl_plan",
     "source": _CANDIDATE},
)


def _calls() -> list[dict]:
    """The scripted calls with the compiled manifest attached.

    Rebuilt per measurement so no state is shared between runs. `compile_source`
    is deterministic, and the manifest is passed to every call, so nothing is
    loaded and no runtime is needed.
    """
    from revl import compile_source  # noqa: PLC0415 - only the report needs revl

    running = compile_source(_RUNNING)
    return [
        {"step": call["step"], "verb": call["verb"],
         "arguments": {"manifest": running, "source": call["source"]}}
        for call in SCRIPT
    ]


def measure() -> list[dict]:
    """Run the scripted session through the real dispatcher.

    One row per call: the step, the verb, whether it succeeded or refused, and
    the size of the text the agent receives, in chars and in proxy tokens. The
    session is swapped for a fresh one and the undo stack cleared for the run,
    then both are restored, so calling this twice gives the same rows and no
    caller's state is touched.
    """
    from revl.mcp import server  # noqa: PLC0415 - only the report needs revl
    from revl.mcp import repeat  # noqa: PLC0415
    from revl.mcp.session import Session  # noqa: PLC0415

    calls = _calls()
    previous = server.SESSION
    previous_undo = list(server.UNDO_STACK.entries)
    # the repeat layer remembers the last refusal in a module global and a
    # generation verb stamps the undo stack's depth, so the scripted run must
    # neither see nor leave either one: otherwise the same call could be
    # restated, or the ship row's chars could count a caller's undo depth.
    repeat.forget()
    server.UNDO_STACK.clear()
    server.SESSION = Session()
    try:
        rows = []
        for call in calls:
            response = server.handle({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": call["verb"], "arguments": call["arguments"]},
            })
            result = response["result"]
            # the payload an agent reads: every text part of the tool result.
            # `structuredContent` carries the same payload for clients that
            # consume it, so counting it too would double-count the answer.
            text = "".join(
                part.get("text") or ""
                for part in result.get("content") or ()
                if part.get("type") == "text"
            )
            refused = bool(result.get("isError"))
            rows.append({
                "step": call["step"],
                "verb": call["verb"],
                "outcome": "refused" if refused else "ok",
                "ok": not refused,
                "est_tokens": count_tokens(text),
                "chars": len(text),
            })
        return rows
    finally:
        server.SESSION = previous
        server.UNDO_STACK.entries[:] = previous_undo
        repeat.forget()


def _median(xs: list[int]) -> float | None:
    if not xs:
        return None
    ordered = sorted(xs)
    mid = len(ordered) // 2
    return float(ordered[mid]) if len(ordered) % 2 else (
        ordered[mid - 1] + ordered[mid]) / 2


def _number(value: float | None) -> str:
    """A size for the report: integral values without a decimal, halves kept."""
    if value is None:
        return "n/a"
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


def per_verb(rows: list[dict]) -> list[dict]:
    """The report's per-verb aggregate: count, median and max response size."""
    out = []
    for verb in sorted({row["verb"] for row in rows}):
        sizes = [row["est_tokens"] for row in rows if row["verb"] == verb]
        out.append({
            "verb": verb,
            "count": len(sizes),
            "median_est_tokens": _median(sizes),
            "max_est_tokens": max(sizes),
            "total_est_tokens": sum(sizes),
        })
    return out


def render(rows: list[dict], sha: str) -> list[str]:
    """The report as Markdown lines. Deterministic for a given run's rows."""
    lines = [
        "## response size: text the measured MCP verbs return into the agent's context",
        f"compiler: `{sha}`  ·  reproduce: `python3 bench/mcp_response_size.py`",
        "",
        "A fixed scripted session drives the real MCP dispatcher in process "
        "(no transport, no model, no runtime) and measures the text of the "
        "tool result the agent receives. `est. tokens` is the same "
        "deterministic BPE proxy as `bench/tokens.py`; the cost is paid as "
        "input tokens on every later turn of the session.",
        "",
        "| scripted call | verb | outcome | est. tokens | chars |",
        "|---|---|---|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| `{row['step']}` | `{row['verb']}` | {row['outcome']} | "
            f"{row['est_tokens']} | {row['chars']} |"
        )

    lines += [
        "",
        "### per verb",
        "",
        "| verb | calls | median est. tokens | max est. tokens | total |",
        "|---|---:|---:|---:|---:|",
    ]
    for summary in per_verb(rows):
        lines.append(
            f"| `{summary['verb']}` | {summary['count']} | "
            f"{_number(summary['median_est_tokens'])} | "
            f"{summary['max_est_tokens']} | {summary['total_est_tokens']} |"
        )

    lines += [
        "",
        "The ceilings live in `tests/test_mcp_response_size.py`: each scripted "
        "call is pinned at its measured size, so a response that grows must "
        "raise its ceiling in the same diff.",
    ]
    return lines


def to_json(rows: list[dict], sha: str) -> dict:
    return {
        "compiler": sha,
        "unit": "est_tokens (deterministic BPE-proxy; see count_tokens)",
        "calls": rows,
        "perVerb": per_verb(rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", default=None,
                        help="also write the sizes as JSON")
    args = parser.parse_args()

    rows = measure()
    sha = rescore.compiler_sha(ROOT)
    print("\n".join(render(rows, sha)))
    if args.json:
        Path(args.json).write_text(
            json.dumps(to_json(rows, sha), indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
