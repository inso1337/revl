"""The drafting state: a composition with open holes, held but not booted (#1727).

`revl_scaffold` returns a draft with typed holes, and the documented loop is
scaffold -> load -> fill -> boot. But booting is admission, and admission
refuses a hole, so `revl_load` of a scaffold was refused and `revl_edit` had
nothing loaded to patch: the loop was a dead end.

So a load of holed source, with nothing running, now opens a DRAFT instead of
failing. The draft is compiled and checked (the same compile a load runs, under
the same authoring trust) and its holes come back with their fillSpecs, but
nothing boots: there is no running composition, no driver, no effect. Edits of
every form (hole fills, ranges, anchors, `{symbol, replacement}`) apply to the
draft and recompile it. While a hole remains the draft advances and reports the
holes left. Once it is hole-free, the same call boots it through every gate a
`revl_load` runs: the load half of the operator gate, a lease on a cold load,
the session's own admission checks, and the approval ticket two-step. If a gate
refuses, the reason comes back and the draft stays, now hole-free, so the next
call can fix what was refused or retry: `revl_load` with no source boots the
held draft (with `config`/`record` if given).

A draft never boots while a hole remains: booting goes through `Session.load`,
which refuses one, and this module only hands it a compile with no holes. The
disk is never written; a draft of files is held as their text, like any edit.
"""

from __future__ import annotations

import copy

from ..diagnostics import report
from ..errors import RevlError
from ..holes import collect as collect_holes
from . import edit as _edit
from . import fillspec


def pending(session) -> dict | None:
    """The held draft, or None."""
    return getattr(session, "pending_draft", None)


def discard(session) -> bool:
    held = pending(session) is not None
    session.pending_draft = None
    return held


def has_holes(ir: dict) -> bool:
    return bool(collect_holes(ir))


def _holes(ir: dict) -> list[dict]:
    from .server import _untrusted_author  # noqa: PLC0415 — cycle

    return fillspec.enrich(ir, untrusted=_untrusted_author())


def _working_set(arguments: dict) -> dict:
    if arguments.get("source") is not None:
        return {"source": arguments["source"],
                "modules": dict(arguments.get("modules") or {})}
    return _edit._files_source({"files": list(arguments["files"]),
                                "modules": arguments.get("modules")})


def _drafted(ir: dict, **extra) -> dict:
    from .server import _summary  # noqa: PLC0415 — cycle

    holes = _holes(ir)
    return {"ok": True, "draft": True, "booted": False, "loaded": False,
            "holes": holes, "holeCount": len(holes), **_summary(ir),
            "note": f"a draft with {len(holes)} open hole(s): compiled and "
                    "checked, nothing booted. Fill the holes with revl_edit "
                    "(each fillSpec gives the line and expected type); once "
                    "none remain the draft boots through the load gates",
            **extra}


def open_draft(session, arguments: dict, ir: dict) -> dict:
    """Hold a holed candidate a `revl_load` compiled, instead of booting it."""
    session.pending_draft = {"vs": _working_set(arguments),
                             "config": arguments.get("config"),
                             "record": bool(arguments.get("record"))}
    return _drafted(ir)


def edit_draft(session, arguments: dict, boot) -> dict:
    """Apply `revl_edit`'s edits to the held draft. `boot(vs, config, record)`
    is the server's load path for a hole-free working set."""
    draft = pending(session)
    edits = arguments.get("edits")
    if not isinstance(edits, list) or not edits:
        raise _edit.EditError("`edits` must be a non-empty array of patch operations")
    before = draft["vs"]
    vs = copy.deepcopy(before)
    applied, touched_buffers = _edit._apply_to_buffers(
        vs, edits, arguments.get("target") or arguments.get("component"))
    try:
        _edit.check_imports(vs, touched_buffers)
    except _edit._JailError as error:
        return {**_edit._jail_refused(str(error)), "draft": True}
    touched = _edit._touched(before, vs)
    try:
        ir = _edit.compile_virtual(vs)
    except RevlError as error:
        refused = report(error)
        return {**refused, "edited": False, "draft": True, "booted": False,
                "note": "the patch does not compile; the draft is unchanged"}
    draft["vs"] = vs
    if has_holes(ir):
        return _drafted(ir, edited=True, applied=applied, touched=touched)
    booted = boot(vs, draft.get("config"), draft.get("record"))
    return {**booted, "edited": True, "applied": applied, "touched": touched}


def boot_held(session, arguments: dict, boot) -> dict:
    """`revl_load` with no source and a held draft: boot it if it has no holes."""
    draft = pending(session)
    try:
        ir = _edit.compile_virtual(draft["vs"])
    except RevlError as error:
        return {**report(error), "draft": True, "booted": False}
    if has_holes(ir):
        return {**_drafted(ir), "ok": False,
                "note": "the draft still has open holes; it cannot boot until "
                        "every one is filled"}
    config = arguments.get("config", draft.get("config"))
    record = arguments.get("record", draft.get("record"))
    return boot(draft["vs"], config, record)


def still_a_draft(refusal: dict) -> dict:
    """A boot that a gate refused: the reason, and the draft is still held."""
    return {**refusal, "draft": True, "booted": False, "loaded": False,
            "note": (refusal.get("note") or "the load gates refused it")
            + "; the hole-free draft is still held. Fix what was refused and "
              "edit again, or revl_load with no source to retry"}


__all__ = ["pending", "discard", "open_draft", "edit_draft", "boot_held",
           "still_a_draft", "has_holes"]
