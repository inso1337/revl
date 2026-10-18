"""A refusal's next call (issue #1691).

A refusal that has a known remedy returns it as data, beside the prose:

    "next": {"tool": "revl_load", "arguments": {...}, "ready": true}

`tool` is the verb to call and `arguments` its arguments, pre-filled from
session state. A remedy no MCP call can perform is an operator step instead,
`{"operator": "<what to do>", "ready": false}` (`operator_step`). `ready` says whether `arguments`, sent as-is, are expected to
succeed. When they are not (nothing this session holds says what the caller
meant), `needs` names what the caller must add. Where several remedies exist,
`next` is a list of calls in preference order.

A concrete affordance is followed more reliably than a remedy inferred from
text, and prose that names a formality ("call revl_load first", to an agent
that already loaded from files) gets the same call retried. So each refusal's
message also names the remedy that works, and `describe` renders it from the
same `next` the payload carries, so the two cannot disagree.
"""

from __future__ import annotations

import os

from .session import NothingLoaded

#: The session's last loaded origin (`{source|files|modules}`), kept after an
#: unload, so a "nothing is loaded" refusal can offer the reload that brings
#: back what was running. Updated after every tool call by `remember`.
_LAST_ORIGIN: dict | None = None


def call(tool: str, arguments: dict, *, ready: bool = True,
         needs: str | None = None) -> dict:
    """One next call, in the shape the module docstring pins."""
    entry = {"tool": tool, "arguments": arguments, "ready": ready}
    if needs is not None:
        entry["needs"] = needs
    return entry


def operator_step(action: str) -> dict:
    """A remedy no MCP call can perform: what an operator must do outside the
    session (install a runtime, restart the server). Never `ready`, because
    the caller cannot send it."""
    return {"operator": action, "ready": False}


def remember(session) -> None:
    """Record what is loaded now, so a later refusal can offer to reload it."""
    global _LAST_ORIGIN
    origin = getattr(session, "origin", None)
    if origin:
        # only what `revl_load` takes: a files origin also holds each file's
        # text (`files_content`, issue #1842), which is not a load argument
        _LAST_ORIGIN = {key: origin[key] for key in ("source", "files", "modules")
                        if key in origin}


def forget() -> None:
    """Drop the remembered origin (tests start each case from a clean slate)."""
    global _LAST_ORIGIN
    _LAST_ORIGIN = None


def load_next() -> dict:
    """`revl_load`, with the arguments that reload the last composition this
    session ran, or with what the caller must supply when it ran none."""
    if _LAST_ORIGIN:
        return call("revl_load", dict(_LAST_ORIGIN))
    return call("revl_load", {}, ready=False,
                needs="`source` (inline .rvl text) or `files` (.rvl paths)")


def for_error(error: BaseException) -> dict | list | None:
    """The `next` an exception implies: its own `next` when it carries one,
    the reload for a `NothingLoaded`, otherwise none."""
    carried = getattr(error, "next", None)
    if carried is not None:
        return carried
    if isinstance(error, NothingLoaded):
        return load_next()
    return None


def resolve(message, extra: dict) -> tuple:
    """`(message text, next or None)` for a refusal. `message` is prose or the
    exception that refused; a caller may also pass `next=` in `extra`, which
    is taken out of `extra` here. The remedy is named at the message's end."""
    nxt = extra.pop("next", None)
    if isinstance(message, BaseException):
        if nxt is None:
            nxt = for_error(message)
        message = str(message)
    if nxt is not None:
        message = f"{message}. {describe(nxt)}"
    return message, nxt


def attach(payload: dict, nxt) -> dict:
    """`payload` with its `next` call, when it has one."""
    if nxt is not None:
        payload["next"] = nxt
    return payload


def describe(nxt: dict | list) -> str:
    """One sentence naming the remedy, for the refusal's message."""
    first = nxt[0] if isinstance(nxt, list) else nxt
    if "operator" in first:
        return f"Next, for an operator: {first['operator']}."
    tool = first["tool"]
    if not first.get("ready", True):
        return f"Next: {tool} with {first['needs']}; `next` holds the call."
    origin = _origin_phrase(first["arguments"]) if tool == "revl_load" else ""
    tail = f" ({origin})" if origin else ""
    return f"Next: {tool}{tail}; `next` holds the call, ready to send as-is."


def _origin_phrase(arguments: dict) -> str:
    files = arguments.get("files")
    if files:
        names = ", ".join(os.path.basename(str(f)) for f in files)
        return f"from {names}"
    if arguments.get("source") is not None:
        return "with the inline source this session last ran"
    return ""
