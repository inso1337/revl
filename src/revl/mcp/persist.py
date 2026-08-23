"""Snapshot and restore for the live MCP session (roadmap item 15).

The session (`session.py`) holds an *evolved composition*: components an
agent admitted at runtime through the gate (`revl_load`, then `revl_swap`).
That state is in-memory — an admitted generation does not survive a restart,
so self-evolution is durable only for the life of the process. This adds the
missing half.

What a snapshot is
------------------
A snapshot is **not** a pickle of live runtime objects. It is the *inputs*
needed to reproduce the composition: the **sources** of the currently-admitted
components (as text), plus the **manifest** and a little **meta**. Plain JSON.

The load-bearing rule
---------------------
Restore **replays admission** — it compiles the snapshotted sources through
the *same* gate a live `revl_load` runs (`compile_source`/`compile_files` ->
parse -> check -> lower, then the holes gate and the runtime boot inside
`Session.load`). It never rehydrates the runtime from the stored `manifest`
dict, because that would be a bypass: a snapshot taken under an *older*
checker could then smuggle a now-rejected component past a *newer* one. So a
component the current checker rejects fails the restore *loudly*, carrying the
diagnostic — it is never silently loaded.

The manifest travels in the snapshot as metadata and as a cross-check: after
recompiling, the resulting component set must match what the snapshot claimed,
or the restore is refused. The authority is always the freshly compiled IR,
never the stored manifest.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from ..compiler import compile_files, compile_source
from ..diagnostics import classify
from ..errors import RevlError

SNAPSHOT_VERSION = 1


class RestoreError(RuntimeError):
    """A snapshot could not be re-admitted through the gate.

    `diagnostic` is the structured compiler diagnostic (same shape the MCP
    tools return) when the failure was a rejected component, else None. The
    point of the type is that a rejected restore is a *result* an agent can
    read, not a crash — and that the rejection is surfaced, never swallowed.
    """

    def __init__(self, message: str, diagnostic: dict | None = None) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic

    @classmethod
    def from_revl(cls, error: RevlError) -> "RestoreError":
        diagnostic = classify(error)
        return cls(
            f"restore refused: a component the current checker rejects cannot be "
            f"re-admitted — {diagnostic.get('message', str(error))}",
            diagnostic=diagnostic,
        )


# ---------------------------------------------------------------- snapshot

def _materialize(origin: dict) -> dict:
    """Turn the recorded admission inputs into a self-contained text bundle.

    `files` are read into text at snapshot time so the snapshot is portable
    JSON that reproduces the *snapshotted* sources — not whatever the paths
    happen to hold at restore time.
    """
    sources: dict = {}
    if origin.get("source") is not None:
        sources["source"] = origin["source"]
    if origin.get("modules"):
        sources["modules"] = dict(origin["modules"])
    files = origin.get("files")
    if files:
        sources["files"] = list(files)
        sources["files_content"] = {path: _read_text(path) for path in files}
    return sources


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def snapshot(session) -> dict:
    """`{sources, manifest, meta}` — enough to re-admit the live composition.

    Raises `SessionError` (from the session) when nothing is loaded or when
    the live composition has no recorded sources to reproduce it from.
    """
    from .session import SessionError  # noqa: PLC0415 — avoid an import cycle

    if not session.loaded:
        raise SessionError("nothing is loaded — snapshot needs a live composition")
    if not getattr(session, "origin", None):
        raise SessionError(
            "this composition has no recorded sources, so it cannot be "
            "snapshotted for re-admission — it was loaded without its source "
            "inputs (snapshot captures the sources a live admission was given, "
            "not a dump of runtime objects)")

    manifest = (session.ir or {}).get("manifest") or {}
    return {
        "sources": _materialize(session.origin),
        "manifest": manifest,
        "meta": {
            "snapshotVersion": SNAPSHOT_VERSION,
            "irVersion": (session.ir or {}).get("ir_version"),
            "components": [entry.get("name")
                          for entry in manifest.get("components") or []],
            "loadOrder": manifest.get("loadOrder") or [],
            "record": session.recorder is not None,
            "config": dict(session.config or {}),
            "createdAt": datetime.now(timezone.utc).isoformat(),
        },
    }


# ---------------------------------------------------------------- restore

def _recompile(sources: dict) -> dict:
    """Compile the snapshotted sources through the very entry points a live
    `revl_load` uses. This *is* the gate: parse + check + lower run here, so a
    component the current checker rejects raises `RevlError` right here."""
    source = sources.get("source")
    modules = sources.get("modules")
    if source is not None:
        return compile_source(source, "<snapshot>.rvl", modules=modules)

    files = sources.get("files")
    if files:
        # reconstruct from the text captured at snapshot time, so restore
        # re-admits the snapshotted sources rather than trusting the disk
        virtual = {os.path.abspath(path): text
                   for path, text in (sources.get("files_content") or {}).items()}
        return compile_files(list(files), sources=virtual or None)

    from .session import SessionError  # noqa: PLC0415

    raise SessionError("snapshot has no `sources` to restore")


def _origin_from(sources: dict) -> dict:
    """The admission inputs to re-attach to the session, so the restored
    composition can itself be snapshotted again (a round-trip stays a
    round-trip)."""
    origin: dict = {}
    if sources.get("source") is not None:
        origin["source"] = sources["source"]
    if sources.get("modules"):
        origin["modules"] = dict(sources["modules"])
    if sources.get("files"):
        origin["files"] = list(sources["files"])
    return origin


def restore(session, snap: dict) -> dict:
    """Re-admit a snapshot into `session`, replaying admission.

    Refuses if a composition is already loaded. Compiles the snapshotted
    sources through the gate (a rejected component -> `RestoreError` carrying
    the diagnostic, and nothing is loaded), then boots them through
    `Session.load` — the same holes gate and runtime path a live load takes.
    Cross-checks that the re-admitted component set matches what the snapshot
    claimed.
    """
    from .session import SessionError  # noqa: PLC0415

    if session.loaded:
        raise SessionError("a composition is already loaded — unload before restore")
    if not isinstance(snap, dict):
        raise SessionError("snapshot must be a JSON object")

    sources = snap.get("sources") or {}
    meta = snap.get("meta") or {}
    config = meta.get("config") or {}
    record = bool(meta.get("record"))

    try:
        ir = _recompile(sources)
    except RevlError as error:
        # the load-bearing failure: a snapshot whose component the current
        # checker refuses does not load — it fails here, with the diagnostic
        raise RestoreError.from_revl(error) from None

    # defence in depth: the runtime IR is the freshly compiled one, never the
    # stored manifest. Verify re-admission produced the composition the
    # snapshot described, so a silent drift is a loud refusal, not a smuggle.
    claimed = set(meta.get("components") or [])
    got = {entry.get("name")
           for entry in (ir.get("manifest") or {}).get("components") or []}
    if claimed and claimed != got:
        raise RestoreError(
            f"restore refused: re-admission produced components {sorted(got)}, "
            f"but the snapshot claimed {sorted(claimed)} — the sources no "
            f"longer reproduce the snapshotted composition")

    state = session.load(ir, config, record=record, origin=_origin_from(sources))
    return {
        "restored": True,
        "components": meta.get("components") or [],
        "loadOrder": (ir.get("manifest") or {}).get("loadOrder") or [],
        "reAdmitted": True,
        **state,
    }
