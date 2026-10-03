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
        _LAST_ORIGIN = dict(origin)


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


def swap_files_next(origin: dict | None, replacing: tuple = ()) -> dict | None:
    """`revl_swap` with the files a files-loaded composition came from: the
    re-admission a name-only swap cannot do, because the session holds no
    inline source to re-admit."""
    files = (origin or {}).get("files")
    if not files:
        return None
    return call("revl_swap", _with_replacing({"files": list(files)}, replacing))


def edit_as_swap(session, arguments: dict, admits) -> dict | None:
    """The patch `revl_edit` could not apply to a files-loaded composition,
    applied to the loaded file's text and offered as `revl_swap` with inline
    `source`. After that swap the session holds inline source, so the next
    `revl_edit` patches it directly.

    Offered for one root file only: re-sending a multi-file composition inline
    would need its imports re-keyed, which is exactly what `revl_edit` itself
    should learn to do. `admits(arguments)` is the admission probe the server
    runs (None, or the reason the call would be refused); it decides `ready`.
    A patch that does not apply raises the same `EditError` revl_edit would."""
    from . import edit as _edit  # noqa: PLC0415 - edit imports the session
    files = (session.origin or {}).get("files") or []
    if len(files) != 1 or arguments.get("target") not in (None, "source"):
        return None
    with open(files[0], encoding="utf-8") as handle:
        text = handle.read()
    patched, _applied = _edit._apply_edits(text, arguments.get("edits") or [])
    swap_args = _with_replacing({"source": patched},
                                tuple(arguments.get("replacing") or ()))
    problem = admits(swap_args)
    if problem is None:
        return call("revl_swap", swap_args)
    return call("revl_swap", swap_args, ready=False,
                needs=f"a patch that admits (this one is refused: {problem})")


def _with_replacing(arguments: dict, replacing: tuple) -> dict:
    if replacing:
        arguments["replacing"] = list(replacing)
    return arguments


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
