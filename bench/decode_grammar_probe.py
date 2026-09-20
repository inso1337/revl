#!/usr/bin/env python3
"""Item 513 slice 2: measure whether a real provider honours a stated constraint.

`docs/design/542-grammar-constrained-decoding.md` §3 says revl states a decoding
grammar and the provider performs the constraint, and §8 admitted that nothing
in slice 1 observed a decoder. This probe is what closes that gap for one real
provider. It is a bench tool, not a test: it needs a running local model server
and it is never run by CI.

What it measures, per response type, against one endpoint:

  * **control** - the prompt alone, no constraint attached. This is the status
    quo item 257 validates after the fact.
  * **gbnf** - the same prompt with the derived GBNF grammar attached under the
    field name a llama.cpp-shaped server reads. An OpenAI-shaped or Ollama-shaped
    server has no such field and ignores it, which is itself the result: an
    ignored constraint is indistinguishable from an absent one on the wire.
  * **json_schema** - the same prompt with the derived JSON Schema attached under
    the server's structured-output field. This is the second dialect
    (`revl.decode_grammar.JSON_SCHEMA_FORMAT`).

Each sample is scored three ways, and the three are deliberately different
questions:

  * `valid` - does item 257's validator accept it (the guarantee that already
    exists);
  * `in_grammar` - is the raw completion text inside the language of the GBNF
    revl derived (the guarantee the grammar states);
  * `ordered` - are the members in the order the grammar pins, which is the one
    part of `in_grammar` that survives into the decoded value and is therefore
    the part the runtime seam can check (`runtime.grammar_honoured_error`).

A run writes one JSON document with every raw completion, so the numbers in the
design note can be re-derived rather than trusted.

    python3 bench/decode_grammar_probe.py --base-url http://127.0.0.1:11434 \
        --model <tag> --arms control,json_schema --out results.json

`--arms` exists because a call against a 30B-class local model can take minutes:
the arms are independent and a run can be split across several invocations.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from revl.decode_grammar import decode_grammar_for, json_schema_grammar_for  # noqa: E402
from revl.mcp.schema import json_schema_for  # noqa: E402

from runtime import (  # noqa: E402
    ResponseValidationError,
    grammar_honoured_error,
    validate_response,
)


# --------------------------------------------------------------------------
# The response types under test. The flagship is item 257's own agent turn; the
# record is the simplest shape a structured-output mode can be expected to
# handle, so a failure on the flagship can be told apart from a failure of the
# endpoint's schema support in general.
# --------------------------------------------------------------------------

CALL = {"kind": "record", "fields": {"tool": "Str", "args": "Str"}}
AGENT_TURN = {
    "kind": "variant",
    "cases": [
        {"name": "Final", "payload": "Str"},
        {"name": "ToolCalls", "payload": "List[Call]"},
    ],
}
FLAGSHIP = {"Call": CALL, "AgentTurn": AGENT_TURN}

REVIEW = {"Review": {"kind": "record", "fields": {
    "verdict": "Str", "score": "Int", "note": "Str"}}}

CASES = {
    "AgentTurn": {
        "types": FLAGSHIP,
        "surface": "AgentTurn",
        "system": (
            "You answer as an agent turn. Either finish, or call tools. "
            'A finished turn is {"tag": "Final", "value": "<your answer>"}. '
            'A tool-calling turn is {"tag": "ToolCalls", "value": '
            '[{"tool": "<name>", "args": "<json string>"}]}. '
            "The tools are `search` and `read_file`. Reply with the object."
        ),
        "prompts": [
            "Who wrote Hamlet?",
            "Find today's tin price on the web, then tell me.",
            "Read /etc/hosts and tell me what is in it.",
            "What is 17 * 23?",
            "Summarise the plot of Moby-Dick in one sentence.",
            "Look up the population of Lisbon and report it.",
        ],
    },
    "Review": {
        "types": REVIEW,
        "surface": "Review",
        "system": (
            "You review a short text. Reply with an object holding a verdict "
            "(a short phrase), a score (an integer 0 to 10) and a note "
            "(one sentence)."
        ),
        "prompts": [
            "Review: `let x = 1` in a language with no let binding.",
            "Review: a function named `do_thing` that sorts a list.",
            "Review: a commit message that says `fix`.",
            "Review: a test that asserts True.",
            "Review: a README with no install instructions.",
            "Review: a 400-line function.",
        ],
    },
}


# --------------------------------------------------------------------------
# A GBNF recogniser, so `in_grammar` is checked rather than assumed. This is the
# same construct set `tests/test_decode_grammar_513.py` recognises, kept here so
# the bench tool has no test-module import.
# --------------------------------------------------------------------------

_ESC = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "/": "/",
        "b": "\b", "f": "\f", "]": "]", "[": "[", "^": "^", "x": "x"}


def _tokenize(text):
    toks, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\n":
            i += 1
        elif text.startswith("::=", i):
            toks.append(("def", "::="))
            i += 3
        elif ch in "()|*+?":
            toks.append(("op", ch))
            i += 1
        elif ch == '"':
            j, out = i + 1, []
            while text[j] != '"':
                if text[j] == "\\":
                    out.append(_ESC.get(text[j + 1], text[j + 1]))
                    j += 2
                else:
                    out.append(text[j])
                    j += 1
            toks.append(("lit", "".join(out)))
            i = j + 1
        elif ch == "[":
            j, out = i + 1, []
            while text[j] != "]":
                if text[j] == "\\":
                    if text[j + 1] == "x":
                        out.append(chr(int(text[j + 2:j + 4], 16)))
                        j += 4
                        continue
                    out.append(_ESC.get(text[j + 1], text[j + 1]))
                    j += 2
                else:
                    out.append(text[j])
                    j += 1
            toks.append(("cls", "".join(out)))
            i = j + 1
        elif ch.isalnum() or ch in "_-":
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_-"):
                j += 1
            toks.append(("id", text[i:j]))
            i = j
        else:
            raise AssertionError(f"unexpected character {ch!r} in grammar")
    return toks


class _Parser:
    def __init__(self, toks):
        self.toks, self.i = toks, 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def expr(self):
        alts = [self.seq()]
        while self.peek() == ("op", "|"):
            self.i += 1
            alts.append(self.seq())
        return alts[0] if len(alts) == 1 else ("alt", alts)

    def seq(self):
        items = []
        while True:
            kind, val = self.peek()
            if kind in (None, "def") or (kind, val) in (("op", "|"), ("op", ")")):
                break
            if kind == "id" and self.i + 1 < len(self.toks) \
                    and self.toks[self.i + 1] == ("def", "::="):
                break
            items.append(self.postfix())
        return items[0] if len(items) == 1 else ("seq", items)

    def postfix(self):
        node = self.atom()
        while self.peek()[0] == "op" and self.peek()[1] in "*+?":
            node = (self.peek()[1], node)
            self.i += 1
        return node

    def atom(self):
        kind, val = self.peek()
        self.i += 1
        if kind == "lit":
            return ("lit", val)
        if kind == "cls":
            return ("cls", val)
        if kind == "id":
            return ("ref", val)
        if (kind, val) == ("op", "("):
            inner = self.expr()
            assert self.peek() == ("op", ")"), "unbalanced group"
            self.i += 1
            return inner
        raise AssertionError(f"unexpected token {(kind, val)!r}")


def _parse_grammar(text):
    toks = _tokenize(text)
    rules, parser = {}, _Parser(_tokenize(text))
    del toks
    while parser.i < len(parser.toks):
        kind, name = parser.peek()
        assert kind == "id", f"expected a rule head, got {(kind, name)!r}"
        parser.i += 1
        assert parser.peek() == ("def", "::="), f"rule {name} has no `::=`"
        parser.i += 1
        rules[name] = parser.expr()
    return rules


def _class_match(spec, ch):
    negate = spec.startswith("^")
    body = spec[1:] if negate else spec
    hit, k = False, 0
    while k < len(body):
        if k + 2 < len(body) and body[k + 1] == "-":
            if body[k] <= ch <= body[k + 2]:
                hit = True
            k += 3
        else:
            if body[k] == ch:
                hit = True
            k += 1
    return hit != negate


def _ends(node, rules, s, starts):
    if not starts:
        return frozenset()
    kind = node[0]
    if kind == "lit":
        lit = node[1]
        return frozenset(i + len(lit) for i in starts if s.startswith(lit, i))
    if kind == "cls":
        return frozenset(i + 1 for i in starts
                         if i < len(s) and _class_match(node[1], s[i]))
    if kind == "ref":
        return _ends(rules[node[1]], rules, s, starts)
    if kind == "seq":
        cur = frozenset(starts)
        for item in node[1]:
            cur = _ends(item, rules, s, cur)
        return cur
    if kind == "alt":
        out = frozenset()
        for branch in node[1]:
            out |= _ends(branch, rules, s, starts)
        return out
    if kind == "?":
        return frozenset(starts) | _ends(node[1], rules, s, starts)
    if kind in ("*", "+"):
        seen = frozenset(starts) if kind == "*" else frozenset()
        frontier = frozenset(starts)
        while frontier:
            frontier = _ends(node[1], rules, s, frontier) - seen
            seen |= frontier
        return seen
    raise AssertionError(f"unknown node {kind!r}")


def accepts(grammar_text, candidate):
    """Does the grammar derive exactly `candidate`?"""
    rules = _parse_grammar(grammar_text)
    return len(candidate) in _ends(rules["root"], rules, candidate,
                                   frozenset({0}))


# --------------------------------------------------------------------------
# the endpoint
# --------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A probe that silently measured a different server is not a probe. Same
    policy `bench/run.py` applies to its own runner."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError(f"probe: HTTP {code} redirect refused")


def call(base_url, model, system, prompt, arm, grammar, schema, timeout):
    """One completion. Returns `(text, meta)`; `text` is the content, which for
    a reasoning model is the part after the reasoning trace, never the trace."""
    payload = {
        "model": model,
        "stream": False,
        "think": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "options": {"temperature": 0, "num_predict": 2048},
    }
    if arm == "gbnf":
        # The field name a llama.cpp-shaped server reads. A server without it
        # ignores the key, which is the measurement.
        payload["grammar"] = grammar["text"]
        payload["options"]["grammar"] = grammar["text"]
    elif arm == "json_schema":
        payload["format"] = schema

    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(_NoRedirect)
    started = time.monotonic()
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:400].decode("utf-8", "replace")
        return None, {"error": f"HTTP {exc.code}: {detail}",
                      "secs": round(time.monotonic() - started, 1)}
    except (urllib.error.URLError, TimeoutError) as exc:
        return None, {"error": str(exc),
                      "secs": round(time.monotonic() - started, 1)}
    body = json.loads(raw.decode("utf-8"))
    message = body.get("message") or {}
    return message.get("content"), {
        "secs": round(time.monotonic() - started, 1),
        "eval_count": body.get("eval_count"),
        "thinking_chars": len(message.get("thinking") or ""),
    }


def score(text, schema, grammar):
    """The three verdicts. `text` is the completion exactly as it arrived: no
    fence stripping and no trimming, because a decoder that honours the grammar
    cannot emit a fence and one that does not should not be helped to look as
    if it had."""
    out = {"valid": False, "in_grammar": False, "ordered": False,
           "parse_error": None, "validator_error": None, "order_error": None}
    if text is None:
        return out
    try:
        out["in_grammar"] = accepts(grammar["text"], text)
    except AssertionError as exc:                       # pragma: no cover
        out["in_grammar"] = False
        out["parse_error"] = f"grammar: {exc}"
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        out["parse_error"] = str(exc)
        return out
    try:
        validate_response(value, schema, where="probe")
        out["valid"] = True
    except ResponseValidationError as exc:
        out["validator_error"] = str(exc)[:200]
        return out
    err = grammar_honoured_error(value, schema)
    out["ordered"] = err is None
    out["order_error"] = err
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://127.0.0.1:11434")
    ap.add_argument("--model", required=True)
    ap.add_argument("--case", default="AgentTurn", choices=sorted(CASES))
    ap.add_argument("--arms", default="control,json_schema",
                    help="comma-separated: control, gbnf, json_schema")
    ap.add_argument("--n", type=int, default=4, help="prompts per arm")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--out", default=None, help="write the run as JSON here")
    args = ap.parse_args()

    case = CASES[args.case]
    schema = json_schema_for(case["surface"], case["types"], validated=True)
    grammar = decode_grammar_for(schema)
    wire_schema = json_schema_grammar_for(schema)["schema"]
    prompts = case["prompts"][:args.n]

    run = {"model": args.model, "case": args.case,
           "grammar_digest": grammar["digest"], "samples": []}
    for arm in args.arms.split(","):
        arm = arm.strip()
        for prompt in prompts:
            text, meta = call(args.base_url, args.model, case["system"],
                              prompt, arm, grammar, wire_schema, args.timeout)
            row = {"arm": arm, "prompt": prompt, "text": text, **meta,
                   **score(text, schema, grammar)}
            run["samples"].append(row)
            print(f"[{arm:11s}] valid={row['valid']:d} "
                  f"in_grammar={row['in_grammar']:d} ordered={row['ordered']:d} "
                  f"{meta.get('secs')}s  {(text or meta.get('error'))!r:.90}",
                  flush=True)

    print()
    for arm in dict.fromkeys(s["arm"] for s in run["samples"]):
        rows = [s for s in run["samples"] if s["arm"] == arm]
        print(f"{arm:11s} n={len(rows)} "
              f"valid={sum(r['valid'] for r in rows)} "
              f"in_grammar={sum(r['in_grammar'] for r in rows)} "
              f"ordered={sum(r['ordered'] for r in rows)}")
    if args.out:
        Path(args.out).write_text(json.dumps(run, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
