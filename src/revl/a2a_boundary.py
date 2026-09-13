"""The A2A 1.0.0 boundary gates, as EMITTED SOURCE (item 439, issue #118).

Three A2A `@py` bodies cross a peer's seam in this tree, and every one of them
is generated: `revl import a2a`'s single crossing (`import_a2a._py_a2a_body`),
the `remote ... through a2a[_rest]` row's single crossing
(`synthesize._py_body_a2a`), and the four-op Task-lifecycle projection
(`a2a_task.task_body`). This module is the ONE place the three share what they
do with a reply, so an external peer cannot be read differently depending on
which entry point generated the client.

It emits SOURCE rather than applying behaviour, for the reason
`revl.crossing_redirect` states about the redirect policy: the rule is compiled
into the generated body so it is readable in the file an operator reviews,
rather than applied from a runtime import the generated file would have to
trust. `backends/typescript/emit_temporal.py` emits the same F5 funnel into a
workflow module for the same reason.

Two rules live here, and the second is the first one's consequence.

**1. Every crossing carries a correlation identity, and a reply that does not
carry it back is not a verdict.** One `_corr` per crossing (a uuid4) is the
JSON-RPC 2.0 envelope `id` AND the `revl.correlation` member of the A2A
message metadata, so the identity the peer logs is the identity the reply is
checked against. A reply is read only after it clears three gates, in order:
it is a JSON object, it claims JSON-RPC `2.0`, and its `id` is exactly this
crossing's `_corr`. A reply that fails any of them is a FAULT naming the
crossing, never a value: an A2A peer is a claim (`docs/design/
439-a2a-transport-binding.md`, question (2)), so an unparseable or
unattributable reply is the one case where guessing would turn a peer's noise
into this composition's answer. JSON-RPC 2.0 requires the response `id` to
equal the request's, so the gate refuses only a peer that is already off
protocol.

The gates are fail-closed in the ordinary sense and in one more: the reply
shape is checked with `isinstance` before any member is read, so a peer that
answers a JSON array, a string or `null` raises the crossing's own declared
fault (which `on_failure(withdraw)` turns into provider withdrawal and
`on_failure(result)` turns into an `Err`) instead of an `AttributeError` that
neither settlement classifies.

The correlation gate is an ENVELOPE gate, so it exists only where there is an
envelope. A2A 1.0.0's HTTP+JSON/REST sub-transport replies with the bare
`Task`/`Message` and echoes nothing a client could check, so `through
a2a_rest` carries the identity one-way (on the wire, for the peer's log and
ours) and gets the shape gates alone. That is stated rather than papered over:
a check the wire cannot make is not emitted as if it could.

**2. Peer-authored text is funnelled before it reaches the consumer.** A reply
renders four peer-authored fields into this composition's own fault text: a
JSON-RPC `error.code`, a task `state`, and a reply `kind`. A peer that reflects
what we sent into any of them puts the caller's own bytes back on our error
channel and on the operator console, and no `Untrusted[T]` qualifier covers
that, because a taint is a property of a value the checker can see and not of
text a boundary renders. That is the fourth layer
`docs/design/439-a2a-transport-binding.md` question (2) names, and the
mechanism is not a new language feature: it is item 421 F5's call-argument
funnel (`backends/python/confidential.py:redact_call_text`, joined at the
placement seam by `backends/python/bridge.py`) joined at THIS boundary.

The funnel keeps F5's contract exactly, including its limits: EXACT match
against this call's own arguments (never a pattern), longest needle first so a
needle containing another leaves no tail, a minimum length below which a match
would shred an ordinary diagnostic for no gain, and both faces of a `Bytes`
argument because an encoder renders one of them. The failure's SHAPE survives,
which is the point: the sentence that makes a fault worth reading is ours, and
only the caller's bytes are removed.

One message deliberately renders NOTHING of the peer's: the correlation
refusal. Reporting the id a peer answered with would put peer-chosen text on
the error channel in the very gate that exists because peer text cannot be
trusted.
"""

from __future__ import annotations

import json

#: The A2A message-metadata member that carries a crossing's correlation
#: identity. It rides beside `revl.skill` (the op name the peer may route on)
#: so a peer's own log can be joined to the crossing that made the call.
#: Namespaced like `revl.skill`, because A2A metadata is a shared map and a
#: bare `correlation` key would collide with a peer's own.
CORRELATION_KEY = "revl.correlation"

#: The placeholder an argument value is replaced by in fault text. MUST equal
#: `backends/python/confidential.REDACTED_ARG` (and `bridge.ts`'s
#: `REDACTED_ARG`, and the go bridge's `RedactedArg`): an operator reading a
#: scrubbed A2A fault beside a scrubbed seam fault must not have to learn two
#: markers. `tests/test_439_a2a_boundary.py` pins the equality.
REDACTED_ARG = "<redacted:arg>"

#: The length an argument value must reach before it is matched as a substring
#: of host text. Mirrors `confidential._MIN_MATCHABLE_ARG` for the reason that
#: constant gives: below it a match is a coin flip against ordinary English and
#: blanket-replacing it would shred the diagnostic for no confidentiality gain.
MIN_MATCHABLE_ARG = 3


def py_correlation(indent: int = 4) -> str:
    """Bind this crossing's correlation identity.

    One value, used twice (the envelope `id` and the metadata member), so the
    identity a peer logs is the identity its reply is checked against.
    """
    pad = " " * indent
    return (
        f"{pad}# item 439: ONE correlation identity per crossing. It is the\n"
        f"{pad}# JSON-RPC envelope `id` and the `{CORRELATION_KEY}` metadata\n"
        f"{pad}# member, so a reply is attributable and a peer's log joins to\n"
        f"{pad}# this crossing. A reply that does not carry it back is refused\n"
        f"{pad}# below, never read as a verdict.\n"
        f"{pad}_corr = str(_uuid.uuid4())\n")


def py_metadata(skill_ref: str) -> str:
    """The A2A `Message.metadata` map source: the routed skill reference and
    this crossing's correlation identity."""
    return (f'{{"revl.skill": {json.dumps(skill_ref)}, '
            f'{json.dumps(CORRELATION_KEY)}: _corr}}')


def py_funnel(args_expr: str, indent: int = 4) -> str:
    """The F5 call-argument funnel, as source, closing over this crossing's own
    arguments (`args_expr`, `_args` at both synthesized call sites and
    `(message,)` in the importer's single-crossing body).

    No cycle guard, unlike `confidential._needles`: a crossing's arguments are
    the declared modality subset (`Str`, `Bytes`) or a `TaskRef` record, so
    there is no back edge for a walk to fall into.
    """
    pad = " " * indent
    lines = [
        "# -- failure-channel funnel (item 421 F5 at the A2A boundary) -------",
        "# A reply's peer-authored text (a JSON-RPC `error.code`, a task",
        "# `state`, a reply `kind`) is rendered into THIS composition's fault",
        "# text, so a peer that reflects what we sent would put the caller's",
        "# own bytes back on our error channel. Mirror of",
        "# `backends/python/confidential.py:redact_call_text`, the funnel the",
        "# placement seam joins in `backends/python/bridge.py`: EXACT match",
        "# against this call's arguments, longest needle first, so the",
        "# sentence and the shape of the failure survive and only the",
        "# caller's bytes are gone.",
        f"_REDACTED_ARG = {json.dumps(REDACTED_ARG)}",
        f"_MIN_MATCHABLE_ARG = {MIN_MATCHABLE_ARG}",
        "",
        "def _arg_needles(_value, _into):",
        "    if _value is None or isinstance(_value, bool):",
        "        return",
        "    if isinstance(_value, str):",
        "        if len(_value) >= _MIN_MATCHABLE_ARG:",
        "            _into.add(_value)",
        "        return",
        "    if isinstance(_value, bytes):",
        "        # Both faces: an encoder renders one of them, and `repr`",
        "        # escapes every non-ASCII byte (confidential._needles).",
        '        for _face in (_value.decode("utf-8", "replace"), repr(_value)):',
        "            if len(_face) >= _MIN_MATCHABLE_ARG:",
        "                _into.add(_face)",
        "        return",
        "    if isinstance(_value, dict):",
        "        for _item in _value.values():",
        "            _arg_needles(_item, _into)",
        "        return",
        "    if isinstance(_value, (list, tuple, set, frozenset)):",
        "        for _item in _value:",
        "            _arg_needles(_item, _into)",
        "        return",
        "    _face = str(_value)",
        "    if len(_face) >= _MIN_MATCHABLE_ARG:",
        "        _into.add(_face)",
        "",
        "def _scrub(_text):",
        "    _needles = set()",
        f"    _arg_needles({args_expr}, _needles)",
        "    for _needle in sorted(_needles, key=len, reverse=True):",
        "        if _needle and _needle in _text:",
        "            _text = _text.replace(_needle, _REDACTED_ARG)",
        "    return _text",
        "",
    ]
    return "".join(f"{pad}{line}\n" if line else "\n" for line in lines)


def py_envelope_gates(fault, indent: int = 4) -> str:
    """The three JSON-RPC 2.0 reply gates, in order: a JSON object, the version
    claim, then this crossing's correlation identity.

    `fault(indent, expr)` is the caller's own settlement (a `TransportFault`
    under `on_failure(withdraw)`, an `Err` under `on_failure(result)`, a
    `RuntimeError` in the importer), so a gate refuses the way every other
    refusal on that wire refuses.

    None of the three messages is funnelled, because none of them renders one
    character of the peer's: the funnel goes exactly where peer-authored text is
    interpolated (`py_funnel`), and running it over a sentence that is wholly
    ours could only damage our own diagnostic, which is F5's documented limit
    turned into a decision here.
    """
    pad = " " * indent
    inner = indent + 4
    return (
        f"{pad}# The reply is checked BEFORE any member of it is read: a peer\n"
        f"{pad}# that answers a JSON array, a string or `null` is a crossing\n"
        f"{pad}# that failed, not an `AttributeError` no settlement classifies.\n"
        f"{pad}if not isinstance(_rpc, dict):\n"
        + fault(inner, '"a2a: reply was not a JSON-RPC object"')
        + f'{pad}if _rpc.get("jsonrpc") != "2.0":\n'
        + fault(inner, '"a2a: reply did not claim JSON-RPC 2.0"')
        + f'{pad}if _rpc.get("id") != _corr:\n'
        + f"{pad}    # The id the peer DID answer with is not reported: it is\n"
        f"{pad}    # peer-chosen text, and this is the gate that exists\n"
        f"{pad}    # because peer text is a claim.\n"
        + fault(inner, '"a2a: reply did not carry this crossing\'s '
                       'correlation id"'))


def py_result_gate(fault, indent: int = 4) -> str:
    """The `result` shape gate. A truthy non-object (`"result": "ok"`) read
    member by member is a peer's noise promoted to an answer."""
    pad = " " * indent
    return (f"{pad}if not isinstance(_result, dict) or not _result:\n"
            + fault(indent + 4, '"a2a: response carried no result object"'))


def py_task_identity_gate(fault, indent: int = 4) -> str:
    """The Task-level correlation gate for an op that names a task it already
    holds (`_poll`, `_reply`, `_cancel`).

    The envelope gate says the reply answers THIS crossing; this one says it
    describes THIS task. A `tasks/get` answered with another task\'s status
    would otherwise be read as a lifecycle event for ours, which is the same
    failure as an uncorrelated reply one layer in. A reply that names no task
    (a direct `Message`, or a `tasks/cancel` acknowledgement with no id) is not
    refused here: `_cancel` is best-effort by design (item 247) and a `Message`
    carries no task identity to check.
    """
    pad = " " * indent
    return (f'{pad}if _result.get("kind") == "task" and _result.get("id") '
            f'not in (None, _task["id"]):\n'
            + fault(indent + 4,
                    '"a2a: reply described a different task than this '
                    'crossing asked about"'))


#: The ts counterpart of `py_correlation` + `py_envelope_gates`, for the
#: importer's coloured `@ts` body (the only A2A body on another tier). The F5
#: funnel is NOT mirrored here: it is the py tier first, exactly as the file
#: `Part` and the async recolour are, and
#: `docs/design/439-a2a-transport-binding.md` records it as remaining.
TS_CORRELATION = "      const a2aCorr = crypto.randomUUID();\n"

TS_ENVELOPE_GATES = """      // item 439: the reply is checked BEFORE any member is read, and the
      // envelope `id` must be the correlation identity this crossing sent.
      // An unparseable or unattributable reply is a FAULT, never a verdict.
      if (!rpc || typeof rpc !== "object") {
        throw new Error("a2a: reply was not a JSON-RPC object");
      }
      if (rpc.jsonrpc !== "2.0") {
        throw new Error("a2a: reply did not claim JSON-RPC 2.0");
      }
      if (rpc.id !== a2aCorr) {
        // The id the peer DID answer with is not reported: it is peer-chosen
        // text, and this is the gate that exists because peer text is a claim.
        throw new Error("a2a: reply did not carry this crossing's correlation id");
      }
"""
