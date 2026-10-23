"""`revl_brief`: one compact next-step answer for the authoring loop (#2854).

An agent between loop steps needs a few lines, not a state dump: what is
loaded and at which generation, what the last refusal or check said, what is
pending, and one call to make next. `revl_state` answers the broad question
and costs context for a decision that is one step wide.

The brief is a PROJECTION, not a second state. Every field is read from state
the session already holds:

* `state`, `generation`, `components` and `dirty` come from `ambient.footer`,
  the same footer every answer carries;
* `pending` comes from the proposal store (`revl.mcp.proposal`) and the held
  draft (`revl.mcp.draft`), the same records `revl_change`, `revl_swap` and
  `revl_load` act on;
* `source` comes from the origin the composition was admitted from;
* `last` is the verdict of the last answer that judged work, recorded where
  every answer already passes (`server.handle`), in the same posture as the
  repeated-refusal guard (`revl.mcp.repeat`). It is an index of an answer the
  session already gave, never an input to admission, planning or swapping.

Nothing here is authoritative and nothing here is cached: `project` recomputes
the whole answer from the session on every call, so the brief cannot drift
from the state `revl_state` reports. `observe` writes the index and returns
nothing; the brief verb itself writes nothing.

The answer is bounded. `CAP` is the maximum length, in characters, of the
compact JSON of the brief block. A caller pays for the block, not for the
session footer and `ok` flag beside it. When a projection is over the cap the
trims are applied in a fixed order that drops NAMES before the action: the
next call's arguments first (the call and its `tool` stay), then the component
names (a `componentCount` replaces them), then the proposal's replaced names,
then the last refusal's repair, then its location, then the source name. Every
trim is reported in `trimmed`, so a short answer never silently hides that it
is short.

An empty session - nothing loaded, no draft held, no proposal pending - is a
NAMED answer, `"state": "empty"`, with a `why` line and a `next` call. No
field is null: a caller can read the answer without checking each value.
"""

from __future__ import annotations

import json

from . import ambient
from . import draft as _draft
from . import proposal as _proposal
from . import remedy as _remedy

#: The documented cap: the compact JSON of the brief block, in characters.
CAP = 1200

#: The named empty answer's state.
EMPTY = "empty"

#: The verbs whose success is a verdict about the caller's work, kept in
#: `last` as the thing that judged it.
_VERDICTS = {"revl_check": "checked", "revl_admit": "admitted"}

#: Verbs that, on success, move the unit of work or the working source, so a
#: refusal about the old one is retired rather than reported as current.
_NEW_UNIT = frozenset({
    "revl_load", "revl_swap", "revl_edit", "revl_change", "revl_rollback",
    "revl_undo", "revl_unload", "revl_restore", "revl_repair", "revl_ship",
})

#: The key that holds the recorded answer on the session. An underscore name,
#: because it is an index into answers the session already gave, not part of
#: the session's own model.
_STORE = "_brief_last"

#: The trims, in the order they are applied when the block is over `CAP`.
#: Names go before the action: `next` is the last thing an answer may lose.
_TRIM_ORDER = ("next.arguments", "components", "pending.replacing",
               "last.repair", "last.location", "source")


# ------------------------------------------------------------------ recording


def observe(session, verb: str, payload) -> None:
    """Record the verdict of one finished answer, so `last` can report it.

    Called from `server.handle` for every `tools/call`, success or refusal.
    The brief's own answer is skipped, so calling the brief cannot change what
    it says. A refusal is always kept (the repair is what a caller needs). A
    successful verdict verb is kept too. A successful change of the unit of
    work retires the old refusal, which no longer describes the session. Any
    other answer leaves the record alone."""
    if verb == "revl_brief" or not isinstance(payload, dict):
        return
    if payload.get("ok") is False or payload.get("admitted") is False:
        _remember(session, _refusal(verb, payload))
    elif verb in _VERDICTS:
        _remember(session, _verdict(verb, payload))
    elif verb in _NEW_UNIT:
        _forget(session)


def _store(session) -> dict:
    """The session's recorded answer. A frozen or slotted stand-in (a test
    double) that takes no attribute still answers; it simply keeps no record,
    and the brief then reports no `last`."""
    store = getattr(session, _STORE, None)
    if store is not None:
        return store
    store = {}
    try:
        setattr(session, _STORE, store)
    except (AttributeError, TypeError):  # a stand-in that takes no attribute
        return store
    return store


def _remember(session, entry: dict) -> None:
    _store(session).update(entry)


def _forget(session) -> None:
    _store(session).clear()


def recorded(session) -> dict | None:
    """The recorded verdict, or None. The one field the projection reads that
    is not a session model field, kept here so the test can compare it."""
    return getattr(session, _STORE, None) or None


def _refusal(verb: str, payload: dict) -> dict:
    """The refusal fields a caller acts on: the code, where it is, its
    mechanical repair and the call the refusal already offers."""
    diagnostic = next((d for d in payload.get("diagnostics") or []
                       if isinstance(d, dict)), {})
    entry = {"outcome": "refused", "verb": verb,
             "code": diagnostic.get("code") or "REVL"}
    location = _location(diagnostic)
    if location:
        entry["location"] = location
    repair = diagnostic.get("fix") or diagnostic.get("hint")
    if repair:
        entry["repair"] = repair
    action = payload.get("next")
    if action:
        # preference order: the first call the refusal offers is the one it
        # recommends, and the brief carries one next step. The key is private
        # so the projection moves it to `next` instead of repeating it in
        # `last`.
        entry["_next"] = action[0] if isinstance(action, list) else action
    return entry


def _location(diagnostic: dict) -> str | None:
    path = diagnostic.get("file")
    line = diagnostic.get("line")
    if isinstance(path, str) and path and line is not None:
        return f"{path}:{line}"
    if isinstance(path, str) and path:
        return path
    if line is not None:
        return f"line {line}"
    return None


def _verdict(verb: str, payload: dict) -> dict:
    """A green verdict: which verb judged the work, and the fact that can
    change the next step (a check's open holes)."""
    entry = {"outcome": "ok", "verb": verb, "verdict": _VERDICTS[verb]}
    if verb == "revl_check":
        holes = len(payload.get("holes") or [])
        if holes:
            entry["holes"] = holes
    return entry


# ----------------------------------------------------------------- projection


def project(session) -> dict:
    """The brief, as a pure function of `session`'s state. Every field is
    recomputed here; nothing is cached and nothing is written."""
    footer = ambient.footer(session)
    held = _proposal.held(session)
    draft = _draft.pending(session)
    last = recorded(session)
    brief = {"state": _state(footer, draft)}
    if brief["state"] == EMPTY:
        brief["why"] = "nothing is loaded and no draft is held"
    else:
        generation = footer.get("generation")
        if generation is not None:
            brief["generation"] = generation
        names = list(footer.get("components") or [])
        if names:
            brief["components"] = names
        source = _source(draft.get("vs") if draft else
                         getattr(session, "origin", None))
        if source:
            brief["source"] = source
    if last:
        brief["last"] = {k: v for k, v in last.items()
                         if not k.startswith("_")}
    if held is not None:
        brief["pending"] = _pending(session, held)
    elif draft is not None:
        brief["pending"] = {"kind": "draft"}
    brief["next"] = _next(session, brief, footer, held, draft, last)
    return _bounded(brief)


def _state(footer: dict, draft) -> str:
    if footer.get("loaded"):
        return "loaded"
    if draft is not None:
        return "draft"
    return EMPTY


def _pending(session, held: dict) -> dict:
    pending = {"kind": "proposal"}
    generation = held.get("generation")
    if generation is not None:
        pending["generation"] = generation
    replacing = list(held.get("replacing") or [])
    if replacing:
        pending["replacing"] = replacing
    if _proposal.stale(session) is not None:
        pending["stale"] = True
    return pending


def _source(vs) -> str | None:
    """The identity of a working set: the files it came from, or the fact that
    it is inline text. `None` when there is no source to name."""
    if not isinstance(vs, dict):
        return None
    files = vs.get("files")
    if files:
        names = [_basename(path) for path in files]
        if len(names) > 3:
            return f"{', '.join(names[:3])} and {len(names) - 3} more"
        return ", ".join(names)
    if vs.get("source") is not None or vs.get("modules"):
        return "inline source"
    return None


def _basename(path) -> str:
    text = str(path).replace("\\", "/")
    return text.rsplit("/", 1)[-1]


def _next(session, brief: dict, footer: dict, held, draft, last) -> dict:
    """The one call to make next, in `remedy`'s call shape. State first, then
    what the last verdict leaves to do."""
    if held is not None:
        if _proposal.stale(session) is not None:
            return _remedy.call("revl_change", {"discard": True})
        return _remedy.call("revl_change", {"commit": True})
    if draft is not None:
        return _remedy.call("revl_check", {"session": True})
    refused = last if last and last.get("outcome") == "refused" else None
    if refused is not None:
        action = refused.get("_next")
        if isinstance(action, dict) and "tool" in action:
            return dict(action)
        return _remedy.call("revl_check", {"session": True})
    verdict = last.get("verdict") if last else None
    if brief["state"] == "loaded" and footer.get("dirty"):
        if verdict in ("checked", "admitted"):
            return _remedy.call("revl_swap", {})
        return _remedy.call("revl_check", {"session": True})
    if verdict in ("checked", "admitted"):
        return _remedy.call(
            "revl_swap", {}, ready=False,
            needs="the candidate that was checked or admitted; the source the "
                  "session holds is what already runs")
    if brief["state"] == EMPTY:
        return _load_next(session)
    return _remedy.call("revl_check", {"session": True})


def _load_next(session) -> dict:
    """`revl_load`, with the origin this session still holds when it has one
    (a session that just unloaded keeps none, and `remedy` remembers the last
    origin for exactly that case)."""
    origin = getattr(session, "origin", None) or {}
    arguments = {key: origin[key] for key in ("source", "files", "modules")
                 if key in origin}
    if arguments:
        return _remedy.call("revl_load", arguments)
    return _remedy.load_next()


# -------------------------------------------------------------------- bounding


def size(brief: dict) -> int:
    """The brief's compact JSON length, the measure `CAP` bounds."""
    return len(json.dumps(brief, sort_keys=True, separators=(",", ":"),
                          default=str))


def _bounded(brief: dict) -> dict:
    """`brief` with the `_TRIM_ORDER` trims applied until it fits `CAP`, and
    `trimmed` naming what was dropped.

    The marker is part of the block from the first trim on, so the bound is
    measured over the answer the caller actually receives and every drop is
    reported: a trimmed answer that did not say it was trimmed is the one
    answer this may not return."""
    if size(brief) <= CAP:
        return brief
    out = {**brief, "trimmed": []}
    for trim in _TRIM_ORDER:
        if size(out) <= CAP:
            break
        if _trim(out, trim):
            out["trimmed"].append(trim)
    return out


def _trim(brief: dict, trim: str) -> bool:
    """Apply one trim in place. `False` when this brief has nothing to drop at
    that step, which is not an error: the trim is skipped."""
    if trim == "next.arguments":
        action = brief.get("next")
        if not isinstance(action, dict) or not action.get("arguments"):
            return False
        action = dict(action)
        action["arguments"] = {}
        action["ready"] = False
        action["needs"] = action.get("needs") or (
            "the arguments this step takes; they were too large for the brief")
        brief["next"] = action
        return True
    if trim == "components":
        names = brief.get("components")
        if not names:
            return False
        del brief["components"]
        brief["componentCount"] = len(names)
        return True
    if trim == "pending.replacing":
        pending = brief.get("pending")
        if not isinstance(pending, dict) or not pending.get("replacing"):
            return False
        pending = dict(pending)
        pending["replacingCount"] = len(pending.pop("replacing"))
        brief["pending"] = pending
        return True
    if trim.startswith("last."):
        field = trim.split(".", 1)[1]
        last = brief.get("last")
        if not isinstance(last, dict) or field not in last:
            return False
        last = dict(last)
        del last[field]
        brief["last"] = last
        return True
    if trim == "source":
        if "source" not in brief:
            return False
        del brief["source"]
        return True
    return False


__all__ = ["CAP", "EMPTY", "observe", "project", "recorded", "size"]
