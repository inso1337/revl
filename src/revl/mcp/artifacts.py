"""Content handles: a session-scoped name for bytes the server already holds.

An MCP answer that echoes text the caller or the session already holds spends
output tokens on a copy (issue #1700, measured for #2852 in
`bench/results/answer-ledger.md`). A producer verb can mint an `artifactId` for
that text instead of returning it, and a consumer verb can accept the id in
place of the text: the bytes stay in this session and are never re-sent.

The id is `canonical.digest` of the text, so the name is the content's own and
the same text always mints the same id; an id this session never minted cannot
name a different text. An entry is held in memory only, bound to the session's
unit of work (`loop_axes.attempt_unit`): a load, a swap, a rollback or an undo
moves the unit, and a handle from the old one is refused by name
(`STALE_ARTIFACT`) instead of being resolved against a composition that has
moved on. An id no entry names is refused as `UNKNOWN_ARTIFACT`. Nothing here
reads or writes a file, a database or a transport.
"""

from __future__ import annotations

from .canonical import digest
from .loop_axes import attempt_unit
from .session import SessionError

#: The argument a consumer reads a handle from, and the field a producer
#: answers with. One spelling both sides share.
ARTIFACT_KEY = "artifactId"

#: How many handles one session retains. A handle names source text the session
#: was sent, so the cap bounds the memory a long-lived session spends on
#: copies; the oldest entry is dropped first. The id is the text's digest, so
#: re-running the producer puts the same id back: a dropped handle costs one
#: call.
LIMIT = 64


class ArtifactError(SessionError):
    """A handle this session cannot resolve. `code` is `UNKNOWN_ARTIFACT` (never
    minted here, or dropped from the store) or `STALE_ARTIFACT` (minted in an
    earlier unit of work), so a caller can branch on the reason without
    reading the prose."""


def _store(session) -> dict:
    """The session's handle store, created on first use so a session built
    before this module existed (a stand-in in a test) still answers."""
    entries = getattr(session, "artifacts", None)
    if not isinstance(entries, dict):
        entries = {}
        session.artifacts = entries
    return entries


def mint(session, text: str) -> str:
    """Hold `text` under its content id and return the id.

    Minting the same text again replaces the entry, so a re-mint after the
    unit moved renews the handle for the current unit."""
    if not isinstance(text, str):
        raise TypeError("a content handle names text, not "
                        f"{type(text).__name__}")
    artifact_id = digest(text)
    entries = _store(session)
    entries.pop(artifact_id, None)
    entries[artifact_id] = {"text": text, "unit": attempt_unit(session)}
    while len(entries) > LIMIT:
        # pop with a default: two transports served in one session can mint
        # concurrently, and the oldest key may already be gone by the time
        # this thread reads it
        entries.pop(next(iter(entries)), None)
    return artifact_id


def resolve(session, artifact_id) -> str:
    """The text `artifact_id` names, or an `ArtifactError` saying why not.

    A handle is bound to the unit of work it was minted in. That unit is the
    same one `revl_state` counts check attempts against (the loaded
    composition, the load epoch and the generation), so a handle follows a
    format and check inside one unit and stops at the change that starts the
    next one."""
    if not isinstance(artifact_id, str):
        raise ArtifactError(
            f"`{ARTIFACT_KEY}` must be the string a producing verb returned, "
            f"not {type(artifact_id).__name__}",
            code="UNKNOWN_ARTIFACT")
    if not artifact_id:
        raise ArtifactError(
            f"`{ARTIFACT_KEY}` is empty: pass the id a producing verb "
            "returned",
            code="UNKNOWN_ARTIFACT")
    entry = _store(session).get(artifact_id)
    if entry is None:
        raise ArtifactError(
            f"`{ARTIFACT_KEY}` {artifact_id} is not a handle this session "
            "holds: no producing verb minted it here, or it has been dropped "
            "to keep the store bounded. Re-run the producer (`revl_fmt` with "
            "`handle: true`) to mint one for the current unit of work",
            code="UNKNOWN_ARTIFACT")
    unit = attempt_unit(session)
    if entry["unit"] != unit:
        raise ArtifactError(
            f"`{ARTIFACT_KEY}` {artifact_id} is stale: it was minted in an "
            "earlier unit of work (a load, swap, rollback or undo moved the "
            "session on), so the text it names no longer belongs to this "
            "unit. Re-run the producer (`revl_fmt` with `handle: true`) to "
            "mint a handle for the current unit, or pass the text as "
            "`source`",
            code="STALE_ARTIFACT")
    return entry["text"]
