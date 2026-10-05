"""The session's knowledge index, and the responses it rides on (issue #1745).

`revl.knowledge` builds index entries from comments. This module keeps one
index per session for the running composition, rebuilt whenever the
composition's generation changes (a load, a swap, an edit that landed, an
undo) and merged with the previous one so an entry whose anchor's code changed
is marked `stale`. It also attaches the entries that concern a call's symbols
to that call's response, bounded:

    "knowledge": {"touched": [...], "live": [...], "stale": [...],
                  "refuted": [...]}

and builds a throwaway index for a `revl_check` candidate, so drift is visible
before anything loads.
"""

from __future__ import annotations

from .. import knowledge as _k
from ..diagnostics import GUARANTEES
from . import edit as _edit

RIDE_LIMIT = 8


def _buffers(vs: dict) -> list[tuple[str, str]]:
    """`(name, text)` of every buffer of a working set, as the symbol model
    enumerates them (one definition of the working-set keys)."""
    from . import symbols  # noqa: PLC0415

    return [(buffer[1], text) for buffer, text in symbols.buffers(vs)]


def index_of(vs: dict, report: dict | None) -> list[dict]:
    """Index every buffer of a working set. `report` is the compile verdict
    (None: it compiled clean)."""
    entries = []
    for name, text in _buffers(vs):
        entries += _k.index_text(name, text, report, set(GUARANTEES))
    return entries


def refresh(session) -> list[dict]:
    """The running composition's index, rebuilt if its generation moved."""
    if not session.loaded:
        session.knowledge_index = None
        return []
    generation = getattr(session, "_generation", None)
    held = getattr(session, "knowledge_index", None)
    if held is not None and held["generation"] == generation:
        return held["entries"]
    current = index_of(_edit.running_source(session), None)
    entries = _k.merge(held["entries"], current) if held is not None else current
    session.knowledge_index = {"generation": generation, "entries": entries}
    return entries


def _concerns(anchor: str | None, symbols: list[str]) -> bool:
    """An entry concerns a symbol when it is anchored to it, to one of its
    members, or to the declaration that contains it."""
    if anchor is None:
        return False
    return any(anchor == s or anchor.startswith(s + ".") or s.startswith(anchor + ".")
               for s in symbols)


def ride(entries: list[dict], symbols: list[str]) -> dict:
    """The bounded `knowledge` field for these symbols."""
    out: dict = {"touched": symbols}
    for status in ("live", "stale", "refuted"):
        hits = [_k.public(e) for e in entries
                if e["status"] == status and _concerns(e["anchor"]["symbol"], symbols)]
        out[status] = hits[:RIDE_LIMIT]
        if len(hits) > RIDE_LIMIT:
            out[f"{status}More"] = len(hits) - RIDE_LIMIT
    return out


def touched_symbols(payload: dict) -> list[str]:
    return [t["symbol"] for t in payload.get("touched") or [] if t.get("symbol")]


def for_candidate(vs: dict, report: dict | None) -> dict:
    """A `revl_check` candidate's index summary: the counts, and every refuted
    entry (bounded), so a comment the compiler no longer agrees with is visible
    before anything loads."""
    entries = index_of(vs, report)
    refuted = [_k.public(e) for e in entries if e["status"] == "refuted"]
    return {**_k.counts(entries), "refutedEntries": refuted[:RIDE_LIMIT]}


__all__ = ["index_of", "refresh", "ride", "touched_symbols", "for_candidate"]
