"""The A2A 1.0.0 Task-lifecycle FOUR-OP projection (item 439 T1, issue #118).

`docs/design/439-a2a-task-lifecycle.md` decides that a long-running A2A 1.0.0
Task is STREAM-SHAPED, and that its first implementable surface — T1, buildable
on today's language — is the four-op EXPLICIT-HANDLE projection:

  * `_start(message: Str) -> TaskRef`         — `message/send`, returns a handle
  * `_poll(task: TaskRef) -> TaskEvent`        — `tasks/get`, one lifecycle event
  * `_reply(task, message) -> TaskEvent`       — `message/send` + `taskId`
  * `_cancel(task: TaskRef) -> Unit`           — `tasks/cancel`

This module is the ONE wire those four crossings speak, shared by the two
compile entry points so their projections cannot drift — exactly as
`stdlib/a2a.rvl` is the one VOCABULARY they both name:

  * `synthesize.py`  — a `remote` row `through a2a long_running` over a service
    the composing engineer wrote in the four-op shape;
  * `import_a2a.py`  — `revl import a2a` for a card that declares
    `capabilities.streaming` (or `--long-running`), which OWNS the service and
    emits the four ops per skill.

Everything the terminal single-crossing wire (`_py_body_a2a`) decides, every op
here decides identically — one crossing, redirect refusal (`crossing_redirect`),
the deadline (`CROSSING_TIMEOUT`), the exact version claim, the `Untrusted[T]`
return, the peer-is-a-claim header. Three things are new, and only three:

  1. **The handle vocabulary crosses.** `_start` returns a `TaskRef` record
     built from the peer's `id`/`contextId`; `_poll`/`_reply` return a
     `TaskEvent` ADT built from the peer's task status. Those values are
     constructed by NAME (`Status`, `Done`, `Message`, `task_state_from_wire`,
     …), which resolve because the program `use`s `stdlib/a2a.rvl` and the
     single emitted module carries the type classes and the two pure gates.

  2. **`_start` carries a `tasks/cancel` compensation** (item 247). The caller
     builds the extern with `compensate <cancel>(result)`; this module only
     supplies the four bodies. On the owner's abort the runtime issues
     `tasks/cancel` best-effort and records `compensation-residue` if it does
     not land — it never pretends the task is undone (design §T1).

  3. **A deadline / transport error becomes a terminal.** Under the default
     `on_failure(withdraw)` a fault on any of the four crossings is a
     `TransportFault` the activation runtime maps to provider WITHDRAWAL (T0),
     satisfying item 130's "provider death is a terminal, never silence" AT THE
     ADAPTER because the peer is a claim and cannot be made to promise it.

SCOPE (T1): both JSON-body sub-transports A2A 1.0.0 defines, and only those.
`rest=False` speaks JSON-RPC 2.0 (`message/send` / `tasks/get` / `tasks/cancel`);
`rest=True` speaks HTTP+JSON/REST (`POST /v1/message:send`,
`GET /v1/tasks/{id}`, `POST /v1/tasks/{id}:cancel`) against the same endpoint
root. The two differ in the envelope and in nothing else: the four ops, the
handle vocabulary, the compensation, the funnel, the deadline, the redirect
refusal and the `Untrusted[T]` return are the same code on both. Two
consequences of the REST envelope are stated where they arise rather than
papered over — the correlation identity rides ONE WAY (there is no envelope
`id` to echo), and the peer-authored task id becomes URL path structure, so it
is percent-encoded whole (`a2a_boundary.py_rest_task_url`).

gRPC is not a sub-transport of either: it is binary framing over HTTP/2, not a
JSON POST, and it needs its own `through` name. The stream sugar (T2) is not
built here and is not waiting either: a `through a2a` row SYNTHESIZES a
provider, and item 130 refuses provider-side `provides <k>: Stream[T]` BY NAME
(`docs/design/130-stream-reactive-types.md` §6c, `src/revl/parser.py`), which is
the shape T2 would need. The ts async recolour waits on the async crossing.
"""

from __future__ import annotations

import json

from . import a2a_boundary
from .crossing_redirect import CROSSING_TIMEOUT, py_policy

#: The four suffixes the projection speaks, in the LIFO-sane order the design
#: names them. `start` first (its compensation is registered first); `cancel`
#: last (it IS that compensation and an explicit op the consumer may call).
SUFFIXES: tuple[str, ...] = ("start", "poll", "reply", "cancel")

#: The revl return type of each op, in `on_failure(withdraw)` mode (the only
#: settlement `long_running` binds — see `synthesize`/`import_a2a`). These are
#: the shapes of `stdlib/a2a.rvl`, so the vocabulary and the wire cannot drift.
RETURN_TYPE = {
    "start": "TaskRef",
    "poll": "TaskEvent",
    "reply": "TaskEvent",
    "cancel": "Unit",
}

#: The parameter list of each op, as `(name, revl type)` pairs.
PARAMS = {
    "start": [("message", "Str")],
    "poll": [("task", "TaskRef")],
    "reply": [("task", "TaskRef"), ("message", "Str")],
    "cancel": [("task", "TaskRef")],
}


def op_name(base: str, suffix: str) -> str:
    """`("research", "start") -> "research_start"`. The one place the naming
    convention lives, so the projector and the classifier cannot disagree."""
    return f"{base}_{suffix}"


def transport_fault_class(label: str, op: str, indent: int = 4) -> str:
    """Source for the inline `TransportFault` a synthesized four-op `@py` body
    raises under `on_failure(withdraw)` (item 439 T0/T1, issue #118).

    Byte-identical in shape to `synthesize._transport_fault_class`: the emitted
    module cannot import the runtime's `TransportFault`, so it is defined inline
    carrying the `_revl_transport_fault` MARKER the activation runtime keys on
    (never class identity, so the exec'd module and the runtime share one
    contract without an import), plus the row label and the crossing that
    failed. It subclasses `RuntimeError` so a caller catching `RuntimeError`
    still sees it.

    The two accounting attributes are spelled `_revl_row` / `_revl_crossing`
    because that is what reads them: `run.py`'s withdrawal record and the
    runtime's own declared `TransportFault` (`backends/python/runtime.py`). A
    four-op fault spelled them without the leading underscore, so its
    withdrawal was recorded with no row and no crossing."""
    pad = " " * indent
    rq, oq = json.dumps(label), json.dumps(op)
    return (
        f"{pad}class TransportFault(RuntimeError):\n"
        f"{pad}    # item 439 T0/T1: a crossing fault under `on_failure(withdraw)`\n"
        f"{pad}    # WITHDRAWS the provider (the runtime keys on the marker).\n"
        f"{pad}    _revl_transport_fault = True\n"
        f"{pad}    _revl_row = {rq}\n"
        f"{pad}    _revl_crossing = {oq}\n")


def _message_obj(skill_ref: str, *, with_task_id: bool) -> str:
    """The A2A 1.0.0 `Message` object literal a `message/send` (`_start`) or a
    `message/send` + `taskId` (`_reply`) carries. `_message` is the one text
    `Part`; the op name rides as `revl.skill`; `_reply` also carries the running
    task's id so the peer routes the answer to the same task."""
    task_id = '        "taskId": _task["id"],\n' if with_task_id else ""
    return (
        '{\n'
        '        "role": "user",\n'
        '        "messageId": str(_uuid.uuid4()),\n'
        '        "parts": [{"kind": "text", "text": _message}],\n'
        f'        "metadata": {a2a_boundary.py_metadata(skill_ref)},\n'
        f'{task_id}'
        '    }')


def _event_from_result(fault) -> str:
    """The lines that turn a JSON-RPC `result` (a `Task` or a `Message`) into a
    `TaskEvent`, shared by `_poll` and `_reply`.

    `completed` is the feed's `Done` and carries the terminal text; every other
    task state — live (`working`/`input-required`/…) or terminal-but-not-done
    (`failed`/`canceled`/`rejected`), and `unknown` — is a `Status` carrying the
    `TaskState` that `task_state_from_wire` (the one pure-revl gate, emitted into
    this module) maps the wire string to. A direct `Message` reply is a
    `Message` event. Both constructors and the gate resolve because the program
    `use`s `stdlib/a2a.rvl`."""
    return (
        '    _kind = _result.get("kind")\n'
        '    if _kind == "task":\n'
        + a2a_boundary.py_task_status_gate(fault, indent=8)
        + '        if _state == "completed":\n'
        + a2a_boundary.py_task_parts_gate(fault, indent=12)
        + '            _text = "".join(\n'
        '                p.get("text", "") for p in _parts\n'
        '                if p.get("kind") == "text" and isinstance(p.get("text"), str))\n'
        '            return Done(_text)\n'
        '        return Status(task_state_from_wire(_state))\n'
        '    if _kind == "message":\n'
        + a2a_boundary.py_message_parts_gate(fault, indent=8)
        + '        _text = "".join(p.get("text", "") for p in _parts\n'
        '                        if p.get("kind") == "text" and isinstance(p.get("text"), str))\n'
        '        return Message(_text)\n'
        + fault(4, '_scrub("a2a: unexpected result kind %r" % (_kind,))'))


def task_body(kind: str, endpoint: str, base_or_skill: str, *,
              follow_redirects: bool = False, label: str = "",
              rest: bool = False) -> str:
    """One four-op crossing, Python tier, over A2A 1.0.0.

    `kind` is one of `SUFFIXES`; `endpoint` is the agent's HTTPS endpoint (the
    `remote` row's `https://<host>` root, or the card's full `url`);
    `base_or_skill` is the skill reference a `message/send` rides as
    `revl.skill`. `rest` selects the sub-transport: JSON-RPC 2.0 by default,
    A2A 1.0.0's HTTP+JSON/REST method paths when true. The body is spliced
    after a `_args = [...]` line the caller writes, exactly as the terminal
    wire's `_py_body_a2a` is, so the argument marshalling is identical for
    every op arity.

    Withdraw-mode only: `long_running` binds no `on_failure(result)` (the
    compensation and the `TaskEvent` feed are the settlement), so a fault is
    always a `TransportFault`, never an in-band `Err`.
    """
    if kind not in SUFFIXES:
        raise ValueError(f"unknown task op kind {kind!r}")
    base = json.dumps(endpoint.rstrip("/"))
    url = json.dumps(endpoint)
    policy = py_policy("a2a", follow=follow_redirects)
    faults = transport_fault_class(label, op_name(base_or_skill, kind))

    def fault(indent: int, expr: str, cause: str = "") -> str:
        pad = " " * indent
        tail = f" from {cause}" if cause else ""
        return f"{pad}raise TransportFault({expr}){tail}\n"

    # -- what each op binds off `_args` --------------------------------------
    if kind == "start":
        bind = "    _message = _args[0]\n"
    elif kind == "reply":
        bind = "    _task = _args[0]\n    _message = _args[1]\n"
    else:  # poll / cancel
        bind = "    _task = _args[0]\n"

    # `_start` and `_reply` SEND a Message; `_poll` and `_cancel` name a task.
    # Only a Message can carry the correlation identity, because only a Message
    # has metadata to carry it in.
    sends_message = kind in ("start", "reply")
    message_obj = _message_obj(base_or_skill, with_task_id=(kind == "reply"))
    post_headers = ('    _r = _req.Request(_url, data=_payload,\n'
                    '                      headers={"content-type": '
                    '"application/json"})\n')

    # Item 439: the boundary's own gates are `a2a_boundary`'s, shared with the
    # terminal wire and the importer so a peer cannot be read differently
    # depending on which entry point generated the client. Which of them a wire
    # can run differs, and only that: an envelope gate needs an envelope.
    if rest:
        # A2A 1.0.0 HTTP+JSON/REST: the method is the PATH, the request body is
        # the bare object and the reply IS the `Task`/`Message` — an A2A error
        # arrives as a non-2xx status, which the transport branch already faults
        # on. There is no envelope, so there are no envelope gates: the shape
        # gate and the task-identity gate are what this wire can check, and
        # `a2a_boundary` says why the third one is absent rather than emitting a
        # check that could not fail.
        correlation = (a2a_boundary.py_correlation(rest=True)
                       if sends_message else "")
        if sends_message:
            wire = "HTTP+JSON/REST `POST " + a2a_boundary.HTTPJSON_SEND_PATH + "`"
            url_src = ("    _url = " + base + " + "
                       + json.dumps(a2a_boundary.HTTPJSON_SEND_PATH) + "\n")
            request = ('    _payload = _json.dumps({"message": ' + message_obj
                       + '}).encode()\n' + post_headers)
        elif kind == "poll":
            wire = "HTTP+JSON/REST `GET /v1/tasks/{id}`"
            url_src = a2a_boundary.py_rest_task_url(base, fault)
            request = ('    # `tasks/get` is a READ on this wire: a GET, no body,\n'
                       '    # and the same redirect refusal as every other op.\n'
                       '    _r = _req.Request(_url, method="GET")\n')
        else:  # cancel
            wire = "HTTP+JSON/REST `POST /v1/tasks/{id}:cancel`"
            url_src = a2a_boundary.py_rest_task_url(
                base, fault, verb=a2a_boundary.HTTPJSON_CANCEL_VERB)
            request = ('    _payload = b"{}"\n' + post_headers)
        unwrap = (f"        with _opener.open(_r, timeout={CROSSING_TIMEOUT}) "
                  "as _resp:\n            _result = _json.loads(_resp.read())\n")
        gates = (a2a_boundary.py_result_gate(fault)
                 + ("" if kind == "start"
                    else a2a_boundary.py_task_identity_gate(fault)))
    else:
        # A2A 1.0.0 JSON-RPC 2.0: one endpoint, the method in the envelope.
        correlation = a2a_boundary.py_correlation()
        if sends_message:
            method, params = "message/send", '{"message": ' + message_obj + '}'
        elif kind == "poll":
            method, params = "tasks/get", '{"id": _task["id"]}'
        else:  # cancel
            method, params = "tasks/cancel", '{"id": _task["id"]}'
        wire = "JSON-RPC 2.0"
        payload = ('{\n'
                   '        "jsonrpc": "2.0",\n'
                   '        "id": _corr,\n'
                   f'        "method": {json.dumps(method)},\n'
                   f'        "params": {params},\n'
                   '    }')
        url_src = "    _url = " + url + "\n"
        request = ("    _payload = _json.dumps(" + payload + ").encode()\n"
                   + post_headers)
        unwrap = (f"        with _opener.open(_r, timeout={CROSSING_TIMEOUT}) "
                  "as _resp:\n            _rpc = _json.loads(_resp.read())\n")
        gates = (a2a_boundary.py_envelope_gates(fault)
                 + '    if _rpc.get("error"):\n'
                 + fault(8, '_scrub("a2a: JSON-RPC error %s"'
                            ' % (_rpc["error"].get("code"),))')
                 + '    _result = _rpc.get("result")\n'
                 + a2a_boundary.py_result_gate(fault)
                 # `_start` mints the task identity; the other three name one
                 # they already hold, so their reply must describe THAT task.
                 + ("" if kind == "start"
                    else a2a_boundary.py_task_identity_gate(fault)))

    # -- the result each op reads back ---------------------------------------
    if kind == "start":
        handle = (
            '    if _result.get("kind") != "task":\n'
            + fault(8, '_scrub("a2a: _start expected a Task handle, got '
                       'kind %r" % (_result.get("kind"),))')
            + '    _tid = _result.get("id")\n'
            '    if not isinstance(_tid, str) or not _tid:\n'
            + fault(8, '"a2a: _start reply carried no task id"')
            + a2a_boundary.py_context_id_gate(fault)
            + '    return {"id": _tid, "context": _ctx}\n')
    elif kind == "cancel":
        # tasks/cancel is BEST-EFFORT (item 247): a 2xx with any result is
        # enough. A transport fault still withdraws (it is a crossing that did
        # not answer); a peer that answers is honoured whatever state it
        # reports, but NOT if it answered about another task (the identity gate
        # above already refused that).
        handle = "    return None\n"
    else:  # poll / reply
        handle = _event_from_result(fault)

    funnel = a2a_boundary.py_funnel("_args")
    header = f"    # A2A 1.0.0, {wire}, task op `{kind}`. ONE crossing.\n"
    return f"""
    import json as _json, urllib.request as _req, urllib.parse as _urlp
    import uuid as _uuid
{faults}{funnel}{correlation}{header}{bind}{url_src}{request}{policy}    try:
        # A crossing that never returns is not a crossing.
{unwrap}    except _RedirectRefused:
        # NOT a transport failure: the peer declining to be the declared
        # endpoint, re-raised so it is never flattened into a feed event.
        raise
    except Exception as _exc:
{fault(8, '"a2a: transport failure"', cause="_exc")}{gates}{handle}    """
