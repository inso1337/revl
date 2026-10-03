"""The session's ambient state, on every MCP response (issue #1693).

A person reads what is loaded and whether it is saved from a title bar. An
agent gets only what each response says, and acting on a wrong belief about
session state was the most common error measured in an agent benchmark. So
every `tools/call` result carries the same small footer, on success and on
refusal alike:

    "sessionState": {"loaded": true, "generation": 2,
                     "components": ["MemCache"], "dirty": false, "draft": false}

* `loaded`: a composition is running.
* `generation`: the running generation (1 after a load, +1 per swap, edit
  swap, rollback or undo); null when nothing is loaded.
* `components`: the running components' names, in load order.
* `dirty`: the server-side working source (what `revl_edit` patches) differs
  from the source the running generation was admitted from.
* `draft`: an edit with open holes is pending, so the working source is a
  draft that cannot swap until the holes are filled.

The key is `sessionState`, not `state`, because several verbs already return a
top-level `state` (fiber states).
"""

from __future__ import annotations

KEY = "sessionState"


def footer(session) -> dict:
    """The footer for `session` as it is now."""
    if not session.loaded:
        return {"loaded": False, "generation": None, "components": [],
                "dirty": False, "draft": False}
    draft = getattr(session, "draft", None)
    return {"loaded": True,
            "generation": getattr(session, "_generation", None),
            "components": _components(session.ir),
            "dirty": draft is not None and _buffers(draft) != _running(session),
            "draft": draft is not None}


def stamp(payload: dict, session) -> dict:
    """`payload` with the footer, as a new dict (a handler's payload may be a
    value it keeps)."""
    return {**payload, KEY: footer(session)}


def _components(ir: dict | None) -> list:
    """The running components in load order: the same reader `revl run` and
    the session's own report use."""
    from ..run import _load_order  # noqa: PLC0415 - run imports the runtime lazily
    return list(_load_order(ir or {}))


def _running(session) -> tuple:
    origin = getattr(session, "origin", None) or {}
    return _buffers(origin)


def _buffers(source_set: dict) -> tuple:
    """The inline buffers of a working or admitted source set."""
    return (source_set.get("source"), dict(source_set.get("modules") or {}))
