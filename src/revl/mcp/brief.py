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
then the last refusal's repair, then its location, then the source name, then
the whole `last` block. Every trim is reported in `trimmed`, so a short answer
never silently hides that it is short. The one caller-supplied name this
records (a hatch's verb name) is clipped at the source, and a final floor
keeps the state and the next call if every trim is spent, so the cap is
enforced by construction rather than by enumeration.

An empty session - nothing loaded and no draft held - is a NAMED answer,
`"state": "empty"`, with a `why` line and a `next` call. No field is null: a
caller can read the answer without checking each value.
"""

from __future__ import annotations

import json
import weakref

from . import ambient
from . import draft as _draft
from . import edit as _edit
from . import proposal as _proposal
from . import remedy as _remedy
from . import undo_record as _undo_record
from ..errors import RevlError

#: The documented cap: the compact JSON of the brief block, in characters.
CAP = 1200

#: The named empty answer's state.
EMPTY = "empty"

#: The verbs whose success is a verdict about the caller's work, kept in
#: `last` as the thing that judged it.
_VERDICTS = {"revl_check": "checked", "revl_admit": "admitted"}

#: Verbs that, on success, move the unit of work or the working source, so a
#: refusal about the old one is retired rather than reported as current.
# The generation verbs are one closed vocabulary: `undo_record` owns the
# definition and this module is a consumer of it, so the two cannot drift.
_NEW_UNIT = _undo_record.GENERATION_VERBS

#: The recorded answer per session. It is deliberately NOT an attribute on the
#: session: a swap-residue check compares the session's own state before and
#: after a generation move, and a warning index the brief keeps must not look
#: like session state. A weak table keeps it beside the session without becoming
#: part of it, and a session that cannot be weakly referenced simply records
#: nothing (the brief then reports no `last`).
_STORES: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

#: The trims, in the order they are applied when the block is over `CAP`.
#: Names go before the action: `next` is the last thing an answer may lose.
_TRIM_ORDER = ("next.arguments", "components", "pending.replacing",
               "last.repair", "last.location", "source", "last")


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
    """The session's recorded answer, or a throwaway dict for a session that
    cannot be weakly referenced (a frozen or slotted stand-in): it still answers,
    it simply keeps no record, and the brief then reports no `last`."""
    try:
        return _STORES.setdefault(session, {})
    except TypeError:  # a stand-in that cannot be weakly referenced
        return {}


def _remember(session, entry: dict) -> None:
    """Replace the record with `entry`. It is one verdict, not a set of
    fields: merging would leave a refusal's code beside a later check's
    `verdict`, and `last` would report two answers at once."""
    store = _store(session)
    store.clear()
    store.update(entry)


def _forget(session) -> None:
    _store(session).clear()


def recorded(session) -> dict | None:
    """The recorded verdict, or None. The one piece of state the projection
    reads that is not a session model field, kept here so the test can compare
    it."""
    return _STORES.get(session) or None


def _refusal(verb: str, payload: dict) -> dict:
    """The refusal fields a caller acts on: the code, where it is, its
    mechanical repair and the call the refusal already offers."""
    diagnostic = next((d for d in payload.get("diagnostics") or []
                       if isinstance(d, dict)), {})
    entry = {"outcome": "refused", "verb": _clip(verb, 80),
             "code": _clip(diagnostic.get("code") or "REVL", 40)}
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
    """A green verdict: which verb judged the work, and the facts that can
    change the next step (a check's open holes, and whether it judged the
    source the session holds rather than a candidate the caller sent)."""
    entry = {"outcome": "ok", "verb": _clip(verb, 80),
             "verdict": _VERDICTS[verb]}
    if verb == "revl_check":
        if payload.get("checked") == "session":
            entry["held"] = True
        holes = len(payload.get("holes") or [])
        if holes:
            entry["holes"] = holes
    return entry


# ----------------------------------------------------------------- projection


def project(session) -> dict:
    """The brief, as a pure function of `session`'s state. Every field is
    recomputed here; nothing is cached and nothing is written."""
    footer = ambient.footer(session)
    held = _held_proposal(session)
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
        brief["pending"] = _draft_pending(draft)
    brief["next"] = _next(session, brief, footer, held, draft, last)
    return _bounded(brief)


def _held_proposal(session) -> dict | None:
    """The calling operator's proposal, or None.

    Read without the proposal module's lazily-creating accessor: a projection
    that CREATED the empty store on every read would be a write, which is the
    one thing this module may not do. The holder key is the leases module's own,
    so the read can never disagree with `revl_change` over whose proposal it
    is."""
    from .leases import holder_identity  # noqa: PLC0415 - no import cycle
    store = getattr(session, "proposals", None)
    if not isinstance(store, dict):
        return None
    return store.get(holder_identity(session))


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


def _draft_pending(draft: dict) -> dict:
    """The held draft, with its open hole count when the draft compiles (it
    always does: a patch that does not compile leaves the draft unchanged).
    The count is what tells the loop whether to fill or to boot."""
    pending = {"kind": "draft"}
    holes = _draft_holes(draft.get("vs"))
    if holes is not None:
        pending["holes"] = holes
    return pending


def _draft_holes(vs) -> int | None:
    """The draft's open holes, read the same way `revl_state` reads them. A
    draft that cannot be compiled reports `None` rather than a made-up zero.

    The compile records the dependency buffers it read on the working set it
    is handed, so it gets a copy: a projection may not write to the draft."""
    if not isinstance(vs, dict):
        return None
    try:
        return len(_draft.collect_holes(_edit.compile_virtual(dict(vs))))
    except RevlError:
        return None


def _clip(text, limit: int) -> str:
    """`text` as a string no longer than `limit` characters. Only a safety
    bound for a field whose value is a name handed in by a caller (a hatch's
    verb name), never a normal path."""
    value = str(text)
    return value if len(value) <= limit else value[:limit]


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
    """The one call to make next, in `remedy`'s call shape.

    A refusal's own remedy comes first, whatever the state: it names what the
    refused call needs, and a session with nothing loaded can only be told to
    load, so a check or a swap here would necessarily be refused too. After
    that the state decides, then the last verdict."""
    refused = last if last and last.get("outcome") == "refused" else None
    if refused is not None:
        action = refused.get("_next")
        if isinstance(action, dict) and "tool" in action:
            return dict(action)
        if brief["state"] == EMPTY:
            return _load_next(session)
        return _remedy.call("revl_check", {"session": True})
    if brief["state"] == EMPTY:
        return _load_next(session)
    if held is not None:
        if _proposal.stale(session) is not None:
            return _remedy.call("revl_change", {"discard": True})
        return _remedy.call("revl_change", {"commit": True})
    if draft is not None:
        holes = (brief.get("pending") or {}).get("holes")
        if holes == 0:
            return _remedy.call("revl_load", {})
        return _remedy.call(
            "revl_edit", {}, ready=False,
            needs="an edit that fills one of the draft's holes; each fillSpec "
                  "names the line and the expected type")
    verdict = last.get("verdict") if last else None
    if verdict in ("checked", "admitted"):
        if last.get("holes"):
            return _remedy.call(
                "revl_edit", {}, ready=False,
                needs="edits that fill the holes the last check listed")
        # only a verdict on the source the session HOLDS can be swapped with
        # `revl_swap {}`; a green verdict on a candidate the caller sent names
        # the swap step without pretending the brief carries that source
        if footer.get("dirty") and last.get("held"):
            return _remedy.call("revl_swap", {})
        return _remedy.call(
            "revl_swap", {}, ready=False,
            needs="the candidate that was checked or admitted; the brief does "
                  "not carry its source")
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
    if size(out) > CAP:
        # every named trim is spent and the block still does not fit: return
        # the smallest answer this verb may give rather than an unbounded one.
        # The next call stays, because it is the answer's whole point.
        out = _floor(out)
    return out


def _floor(brief: dict) -> dict:
    """The smallest answer `revl_brief` may return: the state, one next call
    with its strings clipped, and nothing else. It is a backstop for a value a
    caller supplied (a hatch's verb name) or an unusually long refusal field,
    never the normal shape, and it is small by construction."""
    action = brief.get("next") if isinstance(brief.get("next"), dict) else {}
    trimmed = list(brief.get("trimmed") or [])
    trimmed.append("all but the state and the next call")
    floor = {"state": _clip(brief.get("state") or EMPTY, 20),
             "next": {"tool": _clip(action.get("tool") or "", 80),
                      "arguments": {}, "ready": False},
             "trimmed": trimmed}
    if brief.get("why"):
        floor["why"] = _clip(brief["why"], 200)
    if brief.get("generation") is not None:
        floor["generation"] = brief["generation"]
    return floor


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
    if trim == "last":
        if "last" not in brief:
            return False
        del brief["last"]
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
