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

from . import a2a_boundary, tls_pin
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


#: The environment variable that names the LOCAL webhook base the push
#: receiver is served under (e.g. "https://agent.example"). Unset means push
#: is off and polling is the only feed; the canonical feed never depends on it.
PUSH_BASE_ENV = "REVL_A2A_PUSH_BASE"

#: The route the synthesized push receiver serves (item 439 T3). `{task}` is
#: the LOCALLY minted push correlation id, not the peer's task id: the peer
#: posts to the exact URL it was configured with, so the receiver maps a push
#: back to a token and an inbox without trusting anything in the body first.
PUSH_ROUTE = "/a2a/notify"

#: Bounded per-task inbound queue. Overflow is an explicit refusal (the peer
#: may retry, and the canonical poll still sees the task), never a silent drop.
PUSH_INBOX_LIMIT = 64


def _push_state_prelude() -> str:
    """Source that lazily creates the module-level push registry shared by the
    three bodies (`_start`, `_poll`, the receiver). The registry maps a peer
    task id to a push id and a push id to its token and bounded inbox; it is
    per emitted module, which is the per-row module the synthesizer writes."""
    return (
        "    global _A2A_PUSH_STATE\n"
        "    try:\n"
        "        _A2A_PUSH_STATE\n"
        "    except NameError:\n"
        "        _A2A_PUSH_STATE = {\"tasks\": {}, \"push\": {}}\n"
    )


def _push_start_setup(message_obj: str) -> str:
    """Source that mints the local push id and token, registers the entry, and
    builds the A2A `pushNotificationConfig` that rides the `message/send` with
    the task's creation. `REVL_A2A_PUSH_BASE` unset means no config, and
    polling is the only feed."""
    return (
        "    _A2A_PUSH_BASE = _os.environ.get(" + json.dumps(PUSH_BASE_ENV)
        + ", \"\").rstrip(\"/\")\n"
        "    _push_id = _push_token = _push_cfg = None\n"
        "    if _A2A_PUSH_BASE:\n"
        "        _push_id = _uuid.uuid4().hex\n"
        "        _push_token = _secrets.token_hex(16)\n"
        "        _push_cfg = {\"pushNotificationConfig\": {\n"
        "            \"url\": _A2A_PUSH_BASE + " + json.dumps(PUSH_ROUTE)
        + " + \"/\" + _push_id,\n"
        "            \"token\": _push_token,\n"
        "            \"authentication\": {\"schemes\": [\"Bearer\"],\n"
        "                               \"credentials\": _push_token},\n"
        "        }}\n"
        "        _A2A_PUSH_STATE[\"push\"][_push_id] = {\"token\": _push_token,\n"
        "            \"task\": None, \"inbox\": []}\n"
        "    _params = {\"message\": " + message_obj + "}\n"
        "    if _push_cfg:\n"
        "        _params[\"configuration\"] = _push_cfg\n"
    )


def _push_start_store() -> str:
    """After a successful `_start` the entry learns the peer's task id, which
    is the key `_poll` holds; before that reply a push can still arrive and is
    accepted against the push id alone."""
    return (
        "    if _push_id:\n"
        "        _A2A_PUSH_STATE[\"push\"][_push_id][\"task\"] = _tid\n"
        "        _A2A_PUSH_STATE[\"tasks\"][_tid] = _push_id\n"
    )


def _push_start_cleanup() -> str:
    """A `_start` that faulted never handed the consumer a handle, so its push
    entry is dropped rather than left holding an inbox nothing can drain."""
    return (
        "        if _push_id:\n"
        "            _A2A_PUSH_STATE[\"push\"].pop(_push_id, None)\n"
    )


def _push_poll_drain() -> str:
    """`_poll` drains the inbox BEFORE crossing. A non-terminal status, a
    message or an artifact is returned without a network round trip; a pushed
    terminal FALLS THROUGH to the `tasks/get` that confirms it, because a peer
    must not be able to end a Task by pushing a terminal."""
    return (
        "    _push_key = (_task.get(\"id\") if isinstance(_task, dict)\n"
        "                  else getattr(_task, \"id\", None))\n"
        "    _push_id = (_A2A_PUSH_STATE[\"tasks\"].get(_push_key)\n"
        "                if _push_key else None)\n"
        "    _entry = (_A2A_PUSH_STATE[\"push\"].get(_push_id)\n"
        "              if _push_id else None)\n"
        "    if _entry and _entry[\"inbox\"]:\n"
        "        _ev = _entry[\"inbox\"].pop(0)\n"
        "        if _ev[\"kind\"] == \"message\":\n"
        "            return Message(_ev[\"text\"])\n"
        "        if _ev[\"kind\"] == \"artifact\":\n"
        "            return Artifact(_ev[\"text\"])\n"
        "        if not is_terminal(task_state_from_wire(_ev[\"state\"])):\n"
        "            return Status(task_state_from_wire(_ev[\"state\"]))\n"
        "        # a pushed terminal is not trusted: the tasks/get below confirms\n"
        "        # it, and only its answer is returned.\n"
    )


def push_notify_body() -> str:
    """The receiver handler's `@py` body (item 439 T3): an exact token check, a
    shape check of the A2A `StreamResponse` push payload, and a deposit into
    the bounded per-task inbox. It returns a `Response` (stdlib/http.rvl) so
    the statuses are spelled in one place: 401 for an unknown task or a bad
    token, 400 for a malformed payload, 429 for a full inbox, 204 when the
    event is accepted. Nothing is deposited on any refusal."""
    return '''
    import json as _json
    global _A2A_PUSH_STATE
    try:
        _A2A_PUSH_STATE
    except NameError:
        _A2A_PUSH_STATE = {"tasks": {}, "push": {}}

    def _reply(status, message=None):
        if message is None:
            return {"status": status, "headers": [],
                    "body": {"$kind": "Empty"}}
        return {"status": status, "headers": [],
                "body": {"$kind": "Json", "$value": _json.dumps(
                    {"code": "a2a-push", "message": message})}}

    _pid, _auth, _req = _args[0], _args[1], _args[2]
    _tok = (_auth.get("token") if isinstance(_auth, dict)
            else getattr(_auth, "token", None))
    _entry = _A2A_PUSH_STATE["push"].get(_pid)
    if _entry is None or _tok != _entry["token"]:
        return _reply(401, "a2a push: unknown task or token mismatch")
    _carrier = (_req.get("body") if isinstance(_req, dict)
                else getattr(_req, "body", None))
    if isinstance(_carrier, dict):
        _raw = _carrier.get("$value") if _carrier.get("$kind") == "Text" else None
    else:
        _raw = (getattr(_carrier, "value", None)
                if type(_carrier).__name__ == "Text" else None)
    if not isinstance(_raw, str) or not _raw.strip():
        return _reply(400, "a2a push: missing or non-text body")
    try:
        _obj = _json.loads(_raw)
    except Exception:
        return _reply(400, "a2a push: body is not JSON")
    if not isinstance(_obj, dict):
        return _reply(400, "a2a push: body is not a JSON object")
    _keys = [k for k in ("task", "message", "statusUpdate", "artifactUpdate")
             if k in _obj]
    if len(_keys) != 1:
        return _reply(400, "a2a push: body must carry exactly one StreamResponse arm")
    _kind = _keys[0]
    _tid = None
    _event = None
    if _kind in ("task", "statusUpdate"):
        _holder = _obj[_kind]
        if not isinstance(_holder, dict):
            return _reply(400, "a2a push: %s is not an object" % _kind)
        _tid = _holder.get("id") if _kind == "task" else _holder.get("taskId")
        _status = _holder.get("status")
        if not isinstance(_status, dict) or not isinstance(_status.get("state"), str):
            return _reply(400, "a2a push: %s carries no string status.state" % _kind)
        _event = {"kind": "status", "state": _status["state"]}
    elif _kind == "message":
        _holder = _obj["message"]
        if not isinstance(_holder, dict):
            return _reply(400, "a2a push: message is not an object")
        _tid = _holder.get("taskId")
        _parts = _holder.get("parts")
        if not isinstance(_parts, list):
            return _reply(400, "a2a push: message carries no parts list")
        _text = "".join(p.get("text", "") for p in _parts
                        if isinstance(p, dict) and p.get("kind") == "text"
                        and isinstance(p.get("text"), str))
        _event = {"kind": "message", "text": _text}
    else:
        _holder = _obj["artifactUpdate"]
        if not isinstance(_holder, dict):
            return _reply(400, "a2a push: artifactUpdate is not an object")
        _tid = _holder.get("taskId")
        _artifact = _holder.get("artifact")
        _parts = _artifact.get("parts") if isinstance(_artifact, dict) else None
        if not isinstance(_parts, list):
            return _reply(400, "a2a push: artifactUpdate carries no parts list")
        _text = "".join(p.get("text", "") for p in _parts
                        if isinstance(p, dict) and p.get("kind") == "text"
                        and isinstance(p.get("text"), str))
        _event = {"kind": "artifact", "text": _text}
    if not isinstance(_tid, str) or not _tid:
        return _reply(400, "a2a push: payload carries no task id")
    if _entry["task"] is not None and _entry["task"] != _tid:
        return _reply(401, "a2a push: payload task id is not the configured task")
    if _entry["task"] is None:
        _entry["task"] = _tid
    if len(_entry["inbox"]) >= __LIMIT__:
        return _reply(429, "a2a push: inbox is full; polling remains canonical")
    _entry["inbox"].append(_event)
    return _reply(204)
'''.replace("__LIMIT__", str(PUSH_INBOX_LIMIT))


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
              rest: bool = False, pin: str | None = None,
              push: bool = True) -> str:
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
    pin_src = tls_pin.py_pin_handler(pin) if pin else ""
    pin_imports = f"    {tls_pin.PY_PIN_IMPORTS}\n" if pin else ""
    pin_catch = ("    except TransportFault:\n        raise\n" if pin else "")
    policy = py_policy(
        "a2a", follow=follow_redirects,
        extra_handlers=", _PinnedHTTPSHandler()" if pin else "")
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

    # -- push (item 439 T3): a locally minted token and a per-task inbox -----
    # `_start` sends the config on the SAME `message/send`; `_poll` drains the
    # inbox before crossing; the receiver (a separate route op) deposits. The
    # three bodies share one module-level registry, which is the per-row module
    # the synthesizer writes.
    push_imports = ""
    push_setup = ""
    push_cleanup = ""
    push_store = ""
    push_drain = ""
    if kind == "start" and push:
        push_imports = "    import os as _os, secrets as _secrets\n"
        push_setup = _push_state_prelude() + _push_start_setup(message_obj)
        push_cleanup = _push_start_cleanup()
        push_store = _push_start_store()
    elif kind == "poll" and push:
        push_setup = _push_state_prelude()
        push_drain = _push_poll_drain()

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
            if sends_message:
                if kind == "start" and push:
                    request = (
                        '    _payload = _json.dumps({"message": ' + message_obj
                        + ', **({"configuration": _push_cfg} if _push_cfg else {})'
                        '}).encode()\n' + post_headers)
                else:
                    request = ('    _payload = _json.dumps({"message": '
                               + message_obj + '}).encode()\n' + post_headers)
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
            method = "message/send"
            params = ('_params' if (kind == "start" and push)
                      else '{"message": ' + message_obj + '}')
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
            + push_store
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
{pin_imports}    import json as _json, urllib.request as _req, urllib.parse as _urlp
    import uuid as _uuid
{push_imports}{faults}{pin_src}{funnel}{correlation}{header}{bind}{push_setup}{push_drain}{url_src}{request}{policy}    try:
        # A crossing that never returns is not a crossing.
{unwrap}    except _RedirectRefused:
        # NOT a transport failure: the peer declining to be the declared
        # endpoint, re-raised so it is never flattened into a feed event.
        raise
{pin_catch}    except Exception as _exc:
{push_cleanup}{fault(8, '"a2a: transport failure"', cause="_exc")}{gates}{handle}    """
