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


def build_snapshot(ir: dict | None, origin: dict | None,
                   config: dict | None, record: bool = False,
                   approval: dict | None = None) -> dict | None:
    """`{sources, manifest, meta}` from *explicit* admission inputs, not a live
    session — the shared core of :func:`snapshot` and of the generation-history
    entries (roadmap item 65, docs/generation-history.md).

    Returns None when there are no recorded sources to reproduce the
    composition from: a snapshot is the *inputs* to re-admit, so a generation
    admitted without its sources (a hand-built IR) has nothing to snapshot, and
    an undo to it will be refused rather than rehydrated past the gate.

    `approval` is the operator-flag approval posture that governed the original
    admission (the `--approval-policy` mode and the bound policy-file reference).
    Stamped into meta so a restore into a policy-less session can REFUSE rather
    than boot the recovered generation ungated (see :func:`restore`). A composition
    admitted with no operator-flag posture stamps nothing, so the meta stays
    byte-identical off-policy.
    """
    if not origin:
        return None
    manifest = (ir or {}).get("manifest") or {}
    meta = {
        "snapshotVersion": SNAPSHOT_VERSION,
        "irVersion": (ir or {}).get("ir_version"),
        "components": [entry.get("name")
                      for entry in manifest.get("components") or []],
        "loadOrder": manifest.get("loadOrder") or [],
        "record": bool(record),
        "config": dict(config or {}),
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    if approval:
        meta["approval"] = dict(approval)
    return {
        "sources": _materialize(origin),
        "manifest": manifest,
        "meta": meta,
    }


def _approval_posture(session) -> dict | None:
    """The operator-flag approval posture in force on `session`, or None.

    Two orthogonal operator-flag admissions gate a live composition: the
    `--approval-policy` MODE (item 246) and the `--policy` boundary-policy FILE
    whose `requires approval` rules enable the same gate (Decision 3). Neither is
    derivable from the IR — unlike the language-surface `with a` edges, which
    `_ir_has_approval_edges` recovers from the sources — so a restore that does
    not carry them forward boots policy-less. This captures both by reference so
    the operator can re-establish the identical posture on recovery."""
    posture: dict = {}
    mode = getattr(session, "approval_policy", None)
    if mode is not None:
        posture["policy"] = mode
    sandbox = getattr(session, "sandbox", None)
    if sandbox is not None:
        # a boundary policy enables the gate only when it names an
        # approval-required capability (Decision 3); record its file reference
        # so restore can demand it back.
        if getattr(sandbox, "requires_approval", None) is not None \
                and sandbox.requires_approval():
            posture["policyFile"] = getattr(sandbox, "source", None)
    return posture or None


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

    snap = build_snapshot(session.ir, session.origin, session.config,
                          session.recorder is not None,
                          approval=_approval_posture(session))
    # component leases (item 61): reflect the active workspace claims into the
    # persisted meta, so a snapshot records who was iterating on what. Leases
    # are wall-clock TTL claims, so only the still-live ones are carried and
    # restore drops any that have since expired (docs/component-leases.md).
    book = getattr(session, "leases", None)
    if snap is not None and book is not None:
        active = book.document()
        if active:
            snap["meta"]["leases"] = active
    return snap


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


def _restore_leases(session, docs) -> None:
    """Re-seat persisted component leases (item 61) at their absolute expiry,
    dropping any already elapsed. Best-effort: a malformed entry is skipped,
    never fatal to the restore."""
    book = getattr(session, "leases", None)
    if book is None:
        return
    for doc in docs:
        try:
            book.reinstate(doc["component"], doc["holder"],
                           float(doc.get("acquired") or doc["expiry"]),
                           float(doc["expiry"]))
        except (KeyError, TypeError, ValueError):
            continue


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


def _refuse_policy_downgrade(session, meta: dict) -> None:
    """Refuse to re-admit a snapshot taken under an operator-flag approval
    posture into a session that has none.

    The activation gate (`_enforce_activation_gate`) and the class map are off
    when `approval_policy`/`sandbox` are unset, so a generation whose activation
    body reaches a class-(c) emission — one that PROMPTED a human on first boot —
    would replay that crossing SILENTLY on restore, the original single-use
    approval long since consumed. Unlike the language-surface `with a` path,
    which re-derives its frame check from the IR and so fails closed on its own,
    the operator-flag posture is nowhere in the sources: it must be carried in
    meta and demanded back here. Same refuse-don't-degrade shape as the
    record-required rule in `Session.load` (item 246, Decision 2)."""
    posture = meta.get("approval") or {}
    if not posture:
        return
    mode = posture.get("policy")
    if mode is not None and getattr(session, "approval_policy", None) is None:
        raise RestoreError(
            "restore refused: this snapshot was admitted under approval policy "
            f"'{mode}', but the recovering session has none. Booting it policy-less "
            "would replay a class-(c) activation crossing UNPROMPTED, past an "
            "approval already spent. Re-establish the posture: pass "
            f"`--approval-policy {mode}` to `revl recover` (item 246, "
            "refuse-don't-degrade)")
    policy_file = posture.get("policyFile")
    if policy_file is not None and getattr(session, "sandbox", None) is None:
        raise RestoreError(
            "restore refused: this snapshot was admitted under a boundary policy "
            f"that names approval-required capabilities ({policy_file}), but the "
            "recovering session has no policy bound. Re-establish it: pass "
            f"`--policy {policy_file}` to `revl recover` (item 246, "
            "refuse-don't-degrade)")


def restore(session, snap: dict) -> dict:
    """Re-admit a snapshot into `session`, replaying admission.

    Refuses if a composition is already loaded. Refuses a snapshot taken under an
    operator-flag approval posture into a policy-less session (a silent re-fire of
    a once-approved class-(c) crossing). Compiles the snapshotted sources through
    the gate (a rejected component -> `RestoreError` carrying the diagnostic, and
    nothing is loaded), then boots them through `Session.load` — the same holes
    gate and runtime path a live load takes. Cross-checks that the re-admitted
    component set matches what the snapshot claimed.
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

    # refuse-don't-degrade: a policy-recorded snapshot into a policy-less session
    # would boot the recovered generation ungated. Demand the posture back before
    # any recompile or runtime touch, so the refusal is loud and side-effect-free.
    _refuse_policy_downgrade(session, meta)

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
    # rehydrate still-live component leases (item 61) onto the fresh book; ones
    # whose wall-clock TTL has elapsed since the snapshot are silently dropped.
    _restore_leases(session, meta.get("leases") or [])
    return {
        "restored": True,
        "components": meta.get("components") or [],
        "loadOrder": (ir.get("manifest") or {}).get("loadOrder") or [],
        "reAdmitted": True,
        **state,
    }


# ------------------------------------------------------ crash recovery (item 47)

def resume(session, snap: dict) -> dict:
    """Roll-forward's other half (roadmap item 47, docs/crash-recovery.md).

    When crash recovery reads a WAL whose activation *completed* before the
    crash, the composition's shape is durable and there is no in-flight boundary
    state to undo — recovery resumes by re-admitting the persisted generation.
    That re-admission is exactly item 15's :func:`restore`: it replays admission
    through the current gate, so a generation the current checker now rejects
    fails loudly rather than resuming on stale authority. This is the named
    composition seam `revl.recovery` calls; it adds nothing to `restore` but the
    intent, so the two halves stay honest about who owns what."""
    result = restore(session, snap)
    return {**result, "resumedForCrashRecovery": True}
