"""Host body of the classified in-language `admit` crossing (roadmap item 330).

This is the ONE piece of host code behind the `admit(source, granted)` language
surface (`stdlib/admit.rvl`): the DECLARED, CLASSIFIED `extern emission` body a
running composition reaches through, not undeclared Python reaching sideways
into the compiler. That distinction is the whole of item 330. Before it, the
compile→admit→run→commit loop was buildable only through internal host APIs
(`compile_source` → `Session.load`/`.call` → `Frame.abort`), and the
admit-decision — the type judgment that IS the permission decision — lived in
un-declared host py (e.g. `demo/evolve_bridge.propose`). Here the same decision
is a first-class classified crossing an agent harness can invoke in-language to
admit its own per-turn actions, and `revl audit` names it on the G8 boundary
surface like any other host body.

The body itself does nothing decisional: it binds the live session and delegates
to `Session.admit`, which applies the item-329 untrusted-author profile (no
smuggled host code, granted-only reach) and wires an admitted turn into the
enclosing 245 frame. Policy stays in revl; this is the thin host seam the
paradigm's rule ("host code only as a classified extern body") explicitly
allows.
"""

from __future__ import annotations

import json

# The live session the crossing runs against. Bound by `Session.load` (so any
# composition that `use`s `stdlib/admit.rvl` reaches the session it is running
# inside) and, for a test or an embedding host, settable directly via `bind`.
# One process drives one MCP session at a time, so a module global is the same
# single-session binding `demo/evolve_bridge` already relies on.
_SESSION = None


def bind(session) -> None:
    """Bind the live session the `admit` crossing delegates to."""
    global _SESSION
    _SESSION = session


def current() -> object | None:
    return _SESSION


def admit(source: str, granted) -> str:
    """The classified crossing's host body: admit a per-turn `source` reaching
    only the `granted` service names, and return a JSON verdict string.

    `granted` arrives as the revl `List[Str]` the crossing was called with (a
    Python list). The verdict is JSON so the revl side can carry it as a plain
    `Str` result: `{"admitted": bool, "message": str|null, "keys": [...],
    "code": str|null}`. On admission the turn's handle stays on the session, its
    provided `keys` callable through the enclosing composition; on refusal the
    `message` is the repair signal and the running system is untouched.
    """
    session = _SESSION
    if session is None:
        return json.dumps({
            "admitted": False,
            "message": "no live session is bound to the admit crossing — the "
                       "host must `revl.mcp.admit_bridge.bind(session)` (Session."
                       "load does this automatically)",
            "keys": [], "code": None,
        })
    granted_list = list(granted) if granted is not None else []
    verdict = session.admit(source, granted_list)
    return json.dumps(verdict.as_dict())
