"""Host body of the in-language `resolved_keys()` reflection query (roadmap item 403).

This is the ONE piece of host code behind the `resolved_keys()` language surface
(`stdlib/reflect.rvl`): the DECLARED, PURE `extern` body a running composition
reaches through to ask what keys it itself provides/resolves. It is the
un-privileged counterpart to the admit crossing (`revl.mcp.admit_bridge`): where
admit is a CLASSIFIED `emission` that changes what runs, reflection is a `pure`
read that changes nothing, so no 245 frame and no approval attach.

Before it, a composition that wanted its own resolved key set in-language had to
declare a classified host extern reaching sideways into the runtime — a
privileged crossing (that is the escape hatch the harness's "what am I running"
panel used). Here the same fact is a first-class pure query, and `revl audit`
names this host body on the G8 boundary surface as an ordinary pure read.
Landing it REMOVES a privileged crossing rather than adding one.

The body does nothing decisional: it binds the live session and returns the
driver's `resolved_keys()` as a deterministic, sorted list. Policy stays in
revl; this is the thin host seam the paradigm's rule ("host code only as a
declared extern body") allows.
"""

from __future__ import annotations

# The live session the query runs against. Bound by `Session.load` (so any
# composition that composes `stdlib/reflect.rvl` reflects the session it is
# running inside) and, for a test or an embedding host, settable directly via
# `bind`. One process drives one MCP session at a time, so a module global is
# the same single-session binding `admit_bridge` already relies on.
_SESSION = None


def bind(session) -> None:
    """Bind the live session the `resolved_keys()` query delegates to."""
    global _SESSION
    _SESSION = session


def current() -> object | None:
    return _SESSION


def resolved_keys() -> list:
    """The pure host body: the running composition's provision/resolution keys,
    deterministically sorted.

    A key appears IFF it has a live provider — `run._Driver.resolved_keys()`
    resolves each provided key in its placement realm — so the set is exactly the
    FIBERS/ROOT-consistent one `Session.state()`'s `providedKeys` derives from
    (roadmap item 372). Sorted so the query is deterministic across calls.

    With no live session/driver bound, the composition provides nothing
    observable through this seam yet, so the honest answer is the empty list
    rather than a raise — reading what you run must never abort the run.
    """
    session = _SESSION
    driver = getattr(session, "_driver", None) if session is not None else None
    if driver is None:
        return []
    return sorted(driver.resolved_keys())
