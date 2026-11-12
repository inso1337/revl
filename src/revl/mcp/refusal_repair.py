"""Applying a refusal's verified repair (issue #2855).

A refusal carries the repair the compiler knows (issue #2845), and the fixgen
engine turns the unambiguous ones into an edit it has already verified
(issue #2684, `diagnostics._attach_repair_fixes`). This module is the action
half: it keeps the repair a check minted as a session handle, and turns that
handle plus the candidate the check compiled into the repaired candidate.

The handle is `repairId`, minted beside the refusal and bound to the unit of
work (`loop_axes.attempt_unit`), like the content handles of issue #2852: an id
this session never minted is `UNKNOWN_REPAIR`, and one minted before a load,
swap, rollback or undo moved the unit is `STALE_REPAIR`. Applying is read-only:
it returns the patched candidate and its re-check and installs nothing, so a
caller decides whether the candidate is worth loading.

Nothing here synthesizes a repair. The edits are the ones
`diagnostics.report` verified through fixgen, carried on the refusal the caller
was already given; this module stores them, applies them with
`fixgen.apply_edits`, and substitutes the patched text back into the candidate
the check read.
"""

from __future__ import annotations

import json
import os

from ..lsp import fixgen
from .canonical import digest
from .loop_axes import attempt_unit
from .session import SessionError

#: The argument the apply verb reads a repair from. One spelling both sides
#: share, like `artifacts.ARTIFACT_KEY`.
REPAIR_KEY = "repairId"

#: How many minted repairs one session retains. A repair names a candidate the
#: session was sent, so the cap bounds the memory a long-lived session spends
#: on copies; the oldest entry is dropped first. Minting the same repair again
#: puts the same id back, so a dropped id costs one `revl_check`.
LIMIT = 64

#: The compile-level filename an inline `source` candidate takes. The check
#: compiles it under this name, so the repair's edits are ranges into it.
INLINE = "<candidate>.rvl"


class RepairError(SessionError):
    """A repair this session cannot apply. `code` is `UNKNOWN_REPAIR` (never
    minted here, or dropped from the store) or `STALE_REPAIR` (minted in an
    earlier unit of work), so a caller can branch on it without the prose."""


def _store(session) -> dict:
    """The session's repair store, created on first use so a session built
    before this module existed (a stand-in in a test) still answers."""
    entries = getattr(session, "repairs", None)
    if not isinstance(entries, dict):
        entries = {}
        session.repairs = entries
    return entries


def forget(session) -> None:
    """Drop every minted repair. The tests use it between the shared server
    session's cases; nothing in the verb path calls it."""
    _store(session).clear()


def mint(session, payload: dict, *, sources: dict[str, str],
         source: str | None = None, files: list[str] | None = None,
         modules: dict | None = None) -> str | None:
    """Attach `repairId` to a refusal payload, when its first record carries a
    verified edit. Returns the id, or None when nothing is applyable.

    `sources` is the map the caller's compile read (`_compiled_sources`): the
    repair's ranges are into the text of the record's file, and the store keeps
    that exact text so applying never reads the disk. `source`/`files`/
    `modules` are the candidate the compile took, kept so the repaired
    candidate can be handed back in the same shape."""
    if not isinstance(payload, dict):
        return None
    for record in payload.get("diagnostics") or []:
        if not isinstance(record, dict):
            continue
        fix = _fix_of(record)
        if fix is None:
            continue
        filename = record.get("file")
        if not isinstance(filename, str):
            continue
        text = _compiled_text(filename, sources)
        if text is None:
            continue
        edits = fix["edits"]
        repair_id = _repair_id(filename, edits, text)
        entries = _store(session)
        entries.pop(repair_id, None)
        entries[repair_id] = {
            "code": record.get("code"),
            "title": fix.get("title") or "",
            "target": _target(filename, record.get("line")),
            "edits": edits,
            "filename": filename,
            "text": text,
            "source": source,
            "files": list(files) if files else None,
            "modules": dict(modules or {}),
            "unit": attempt_unit(session),
        }
        while len(entries) > LIMIT:
            entries.pop(next(iter(entries)), None)
        payload[REPAIR_KEY] = repair_id
        return repair_id
    return None


def resolve(session, repair_id) -> dict:
    """The entry `repair_id` names, or a `RepairError` saying why not."""
    if not isinstance(repair_id, str):
        raise RepairError(
            f"`{REPAIR_KEY}` must be the string a refusing verb returned, not "
            f"{type(repair_id).__name__}", code="UNKNOWN_REPAIR")
    if not repair_id:
        raise RepairError(
            f"`{REPAIR_KEY}` is empty: pass the id the refusal returned",
            code="UNKNOWN_REPAIR")
    entry = _store(session).get(repair_id)
    if entry is None:
        raise RepairError(
            f"`{REPAIR_KEY}` {repair_id} is not a repair this session holds: "
            "no `revl_check` refusal minted it here, or it has been dropped to "
            "keep the store bounded. Re-run the check to mint one for the "
            "current unit of work", code="UNKNOWN_REPAIR")
    if entry["unit"] != attempt_unit(session):
        raise RepairError(
            f"`{REPAIR_KEY}` {repair_id} is stale: it was minted in an earlier "
            "unit of work (a load, swap, rollback or undo moved the session "
            "on), so the candidate it repairs no longer belongs to this unit. "
            "Re-run the check to mint a repair for the current unit",
            code="STALE_REPAIR")
    return entry


def patched_candidate(entry: dict) -> dict:
    """The repaired candidate, in the argument shape the check took.

    The edits are ranges into the refusal's own file, so only that file is
    substituted: an inline `source` becomes the patched text, and a file rides
    as an in-memory `modules` overlay over the same `files` set, exactly the
    way an edited buffer reaches a check. Nothing is written."""
    text = fixgen.apply_edits(entry["text"], entry["edits"])
    filename = entry["filename"]
    candidate: dict = {}
    modules = dict(entry.get("modules") or {})
    if filename == INLINE:
        candidate["source"] = text
        if modules:
            candidate["modules"] = modules
        return candidate
    modules[filename] = text
    modules[os.path.abspath(filename)] = text
    if entry.get("source") is not None:
        candidate["source"] = entry["source"]
    else:
        candidate["files"] = list(entry.get("files") or [])
    candidate["modules"] = modules
    return candidate


def applied(entry: dict) -> dict:
    """What the verb changed, in the caller's terms: the code and title of the
    repair, where it lands, and the verified edits it applied."""
    return {"repairId": _repair_id(entry["filename"], entry["edits"],
                                   entry["text"]),
            "code": entry["code"], "title": entry["title"],
            "target": entry["target"], "edits": entry["edits"]}


def _fix_of(record: dict) -> dict | None:
    """The verified `fix_code` a record carries, wherever its class puts it: a
    plain code (T2, A9, REVL) has it on the record, and a G4 keeps it on the
    `site-undo` repair entry it verified (issue #2684).

    Only those are applyable. The G1/G2/R2 candidates of issue #2759 carry a
    `fix_code` on their repair entries too, but they are a ranked list for a
    caller to choose from, not the one repair the refusal names, so they mint
    no id. A repair entry marked `widens_authority` is never taken: applying
    one in a single call would enlarge the declared authority unasked."""
    fix = record.get("fix_code")
    if isinstance(fix, dict) and fix.get("edits"):
        return fix
    for repair in record.get("repairs") or []:
        if not isinstance(repair, dict):
            continue
        if repair.get("kind") != "site-undo" or repair.get("widens_authority"):
            continue
        fix = repair.get("fix_code")
        if isinstance(fix, dict) and fix.get("edits"):
            return fix
    return None


def _compiled_text(filename: str, sources: dict[str, str]) -> str | None:
    """The text the compile read for `filename`, spelled the way the record
    names it or as its absolute form (`diagnostics._compiled_text`)."""
    if filename in sources:
        return sources[filename]
    return sources.get(os.path.abspath(filename))


def _repair_id(filename: str, edits: list[dict], text: str) -> str:
    """The name of one repair: its file, its verified edits and the text they
    apply to. The same refusal on the same candidate mints the same id, so a
    re-check renews the handle instead of growing the store."""
    return digest(json.dumps({"file": filename, "edits": edits, "text": text},
                             sort_keys=True, separators=(",", ":")))


def _target(filename: str, line) -> str:
    return f"{filename}:{line}" if line is not None else filename
