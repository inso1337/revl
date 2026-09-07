"""Native write receipts + expected-before filesystem guards (issue #523).

The supported, opt-in Python surface for a *guarded native write*. `write`
overwrites (or creates) one file inside the configured session workspace root and
returns a `WriteReceipt` whose facts are bound to the ORIGINAL held target inode,
and its `expect=` keyword refuses the write — fail-closed, with no partial write —
when the current on-disk state does not match what the consumer recorded:

    from revl import fs

    r = fs.write("notes.txt", "hello\n")            # unconditional create/overwrite
    r2 = fs.write("notes.txt", "world\n", expect=r.new_digest)  # only if unchanged
    fs.write("fresh.txt", "x", expect=fs.ABSENT)    # only if it does not yet exist

`expect=` takes one of:

* ``fs.ABSENT`` — the target must not currently exist. If it exists, the write is
  refused (`FsOpError` code ``EEXPECT``) and the existing file is left untouched.
* a prior ``"sha256:..."`` digest (a previous receipt's ``new_digest``) — the
  target's *current* on-disk content must still hash to it. A drifted or absent
  target is refused (``EEXPECT``); the target is left byte-identical.
* ``None`` (the default) — no guard.

The check runs on the *held descriptor*, inside the workspace jail, BEFORE the
snapshot and before the truncate, which is the whole point of issue #523: a
host-side preflight is re-derived from the name and a same-UID writer can swap
the target between that check and the native open. Because the guard is evaluated
before `write_through` truncates, a refusal never mutates the target — there is
no partial write.

Why the receipt binds to the original inode: `WriteReceipt.prev_digest` and the
identity behind the `expect=` check are read through the descriptor
`open_confined_write` verified (its `(dev, ino)`), not by reopening the path. A
snapshot or output path replaced with identical bytes before the first host
observation therefore cannot forge the receipt: the replacement is a different
inode, so an `expect=` bound to the recorded identity detects the swap even when
the bytes are unchanged (see `tests/test_fs_write_receipts.py`).

This is a thin public shim over the confined-write machinery in
`backends/python/revl_fs_workspace.py` (the issue #529 slice:
`open_confined_write`, `original_receipt`, `expect_existing`, `write_through`,
`confirm_landed`, `discard_write`). It reuses that machinery rather than
reinventing it, so every mutation still routes through the workspace boundary:
confinement is retained, a write outside the root raises `ConfinementError`, and
a lost race is refused (`ERACE`) rather than silently reporting a false success.
Legacy `stdlib/fs.rvl` execution is unaffected — nothing here is wired into the
witnessed `@py` bodies, so a caller who does not use this surface runs exactly
the code that ran before (issue #523 requirement 7).

Versioning: `WRITE_RECEIPT_API_VERSION` tags this surface independently of the
package version (see docs/witnessed-fs.md), like the other narrow host APIs in
`revl.fs_workspace`.
"""

from __future__ import annotations

import hashlib
from importlib import import_module
from pathlib import Path
import sys
from typing import NamedTuple, Optional, Union

from ._paths import backends_root

# Import the confined-write runtime exactly as `revl.fs_workspace` does: add
# `backends/python` to the path, import the module, and refuse a runtime that
# does not belong to THIS installation, so the public surface cannot be pointed
# at a foreign guard module.
_backend = backends_root() / "python"
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_runtime = import_module("revl_fs_workspace")
if Path(_runtime.__file__).resolve() != (_backend / "revl_fs_workspace.py").resolve():
    raise ImportError("filesystem runtime belongs to a different Revl installation")

#: Semver-independent version of THIS surface (`write` / `WriteReceipt` /
#: `expect=`). An unsupported reader can refuse a receipt it does not understand.
WRITE_RECEIPT_API_VERSION = 1

#: Re-exported so a caller needs one `except` clause for every guarded-write
#: refusal. `ConfinementError` is the boundary-specific subclass.
FsOpError = _runtime.FsOpError
ConfinementError = _runtime.ConfinementError


class _Absent:
    """The type of `ABSENT`. A distinct sentinel (not `None`, not `False`) so an
    absent-required write is impossible to confuse with "no guard"."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "revl.fs.ABSENT"


#: `expect=ABSENT`: the target must not currently exist, or the write is refused.
ABSENT = _Absent()


class WriteReceipt(NamedTuple):
    """The record a successful `write` returns.

    * ``path`` — the resolved absolute path the bytes landed on, inside the
      session workspace root.
    * ``prev_digest`` — the ``"sha256:..."`` digest of the target's ORIGINAL
      content, read through the held descriptor before the truncate, or ``None``
      when the write created a previously-absent target.
    * ``new_digest`` — the ``"sha256:..."`` digest of the bytes just written.
      Feed it back as the next call's ``expect=`` to make that write conditional
      on nothing having changed in between.
    * ``replaced`` — ``True`` when an existing file was overwritten, ``False``
      when the write created the target.
    """

    path: str
    prev_digest: Optional[str]
    new_digest: str
    replaced: bool


Expectation = Union[str, _Absent, None]


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _validate_write_args(path, data, expect) -> None:
    if not isinstance(data, str):
        raise FsOpError(
            "EINVAL",
            "write() data must be a str written as UTF-8 text, not "
            f"{type(data).__name__}",
            str(path),
        )
    if not (expect is None or expect is ABSENT or isinstance(expect, str)):
        raise FsOpError(
            "EINVAL",
            "expect= must be revl.fs.ABSENT, a prior 'sha256:...' digest string, "
            f"or None, not {type(expect).__name__}",
            str(path),
        )


def _perform_write(real: str, data: str, expect: Expectation, *,
                   capture_witness: bool):
    """The one guarded-write body `write` and `write_witnessed` share (issue
    #623: the witnessed path must consume the SAME held-descriptor guard, never
    a reconstructed parallel one).

    Returns `(WriteReceipt, witness_or_None)`. When `capture_witness` is True the
    preimage is snapshotted BEFORE the truncate (so the witnessed inverse can
    restore the original bytes) and the returned witness carries the ORIGINAL
    receipt captured before any mutation. A refusal or a failure after the open
    leaves no residue and never returns a witness.
    """
    # A positive content expectation requires the target to ALREADY exist, so it
    # is opened existing-only (create=False). Opening create-capable and then
    # refusing on `handle.created` was a fail-open (issue #626): the target inode
    # was transiently created before the guard refused it, leaving observable
    # filesystem events and a process-death window in which an empty file could
    # survive. Existing-only means a missing target is refused before any inode
    # exists. `ABSENT` and `None` still open create-capable — an absent-required
    # or unconditional write legitimately creates the target.
    positive_digest = isinstance(expect, str)
    try:
        handle = _runtime.open_confined_write(real, create=not positive_digest)
    except FsOpError as exc:
        if positive_digest and exc.code == "ENOENT":
            # The target does not exist (missing leaf, or a missing parent), so
            # the recorded content cannot be present. Refuse with EEXPECT and
            # nothing created — the existing-only open never made an inode.
            raise FsOpError(
                "EEXPECT",
                "expect=<digest> requires the recorded content to still be "
                "present, but the target does not exist; the write is refused "
                "and nothing was created",
                real,
            ) from None
        raise

    committed = False
    try:
        # Facts from the ORIGINAL held descriptor, captured before any content
        # mutation (the open does not truncate). This is the evidence a reopened
        # path cannot supply (issue #523 requirement 4), and — for the witnessed
        # path — the receipt bound onto the witness (issue #623).
        receipt = _runtime.original_receipt(handle)

        # --- expected-before guard: fail-closed, before the truncate ---
        if expect is ABSENT:
            if not handle.created:
                raise FsOpError(
                    "EEXPECT",
                    "expect=ABSENT requires the target to not exist, but it is "
                    "present; the guarded write is refused and the target is "
                    "left unchanged",
                    handle.real,
                )
        elif positive_digest:
            # The target was opened existing-only, so it exists here; compare
            # the recorded content against the held descriptor before any
            # mutation. A drifted target is refused (EEXPECT), byte-unchanged.
            _runtime.expect_existing(handle, {"digest": expect})
        # expect is None: no guard.

        prev_digest = None if handle.created else receipt["digest"]

        # A witnessed write must be REVERSIBLE, so snapshot the preimage (of an
        # existing target) before diverging it — taken from the verified fd into
        # a guarded sidecar, exactly as the witnessed `stdlib/fs.rvl` write body
        # does. A created target needs no preimage (the inverse deletes it).
        if capture_witness and not handle.created:
            _runtime.snapshot_preimage(handle)

        # --- the mutation, THROUGH the verified fd (confinement retained) ---
        _runtime.write_through(handle, data)
        # The name must still resolve to the inode we wrote, or the receipt would
        # lie about where the bytes landed (roadmap 431(b)).
        _runtime.confirm_landed(handle)
        committed = True
        wr = WriteReceipt(
            path=handle.real,
            prev_digest=prev_digest,
            new_digest=_digest(data.encode("utf-8")),
            replaced=not handle.created,
        )
        witness = None
        if capture_witness:
            # Bind the ORIGINAL receipt (captured above, pre-truncate) onto the
            # WAL-serializable witness the effect entry will carry.
            witness = _runtime.witnessed_write_record(handle, data, receipt)
        return wr, witness
    finally:
        if not committed:
            # A guard refusal, or a write that failed after the open, leaves no
            # residue: drop the preimage sidecar (if one was taken) and, only if
            # THIS call created the target and the name still holds our inode,
            # remove it again. An existing target was never truncated, so it
            # keeps its original bytes.
            try:
                _runtime.discard_write(handle)
            except FsOpError:
                pass
        _runtime.close_handle(handle)


def write(path, data: str, *, expect: Expectation = None) -> WriteReceipt:
    """Write `data` to `path` inside the session workspace root, returning a
    `WriteReceipt`, and optionally guard the write on the target's current state.

    This is the LEGACY opt-out surface (issue #623): a direct guarded write that
    does NOT register a witnessed inverse and never touches the session's
    witnessed/WAL machinery. A caller who wants the write bound to the witnessed
    teardown/verdict machinery uses `write_witnessed` instead; a caller who does
    not is byte-for-byte on the pre-#623 path.

    `path` is resolved against the configured workspace root (relative paths are
    taken relative to it) and refused if it escapes the root. `data` is text,
    written as UTF-8.

    `expect=` guards the write and is checked on the held descriptor BEFORE any
    mutation; a mismatch is a fail-closed `FsOpError` and the target is left
    unchanged (no partial write):

    * `ABSENT`   — refuse (`EEXPECT`) if the target already exists.
    * a digest   — refuse (`EEXPECT`) unless the target currently exists and its
      content hashes to the given `"sha256:..."` string.
    * `None`     — no guard (unconditional create-or-overwrite).

    Raises `FsOpError` (its `ConfinementError` subclass for a boundary refusal)
    on any refusal; the exception carries a machine `code`, a message, and the
    offending `path`.
    """
    _validate_write_args(path, data, expect)
    real = _runtime.resolve_within(str(path))
    receipt, _ = _perform_write(real, data, expect, capture_witness=False)
    return receipt


# ---------------------------------------------------------------------------
# issue #623: the OPT-IN guarded witnessed write
# ---------------------------------------------------------------------------

#: Version of the witnessed-binding surface (`write_witnessed`/`write_all` and
#: the receipt the witness carries), independent of the package version and of
#: `WRITE_RECEIPT_API_VERSION`. Mirrors the guard module's
#: `WITNESS_RECEIPT_VERSION`, so a reader that refuses an unknown witness version
#: and a writer that stamps one agree on the same number.
WITNESSED_WRITE_API_VERSION = _runtime.WITNESS_RECEIPT_VERSION

#: Re-exported outcome tags (issue #623), so a consumer classifies a call the
#: same way the guard module and the runtime inventory do.
OUTCOME_SUCCESS = _runtime.OUTCOME_SUCCESS
OUTCOME_FAILED = _runtime.OUTCOME_FAILED
OUTCOME_UNKNOWN = _runtime.OUTCOME_UNKNOWN
OUTCOME_UNATTEMPTED = _runtime.OUTCOME_UNATTEMPTED


class WitnessedWriteReceipt(NamedTuple):
    """What `write_witnessed` returns: a `WriteReceipt` plus the identity of the
    witnessed effect the write was bound to.

    * ``path`` / ``prev_digest`` / ``new_digest`` / ``replaced`` — as
      :class:`WriteReceipt`.
    * ``effect_id`` — the stable id of the registered witnessed effect (the same
      id a `witness_snapshot` / verdict review reports), or ``None`` when the
      register hook returned no identity.
    * ``outcome`` — the recorded call outcome; ``"success"`` for a bound write.
    """

    path: str
    prev_digest: Optional[str]
    new_digest: str
    replaced: bool
    effect_id: Optional[str]
    outcome: str


def _cordis_runtime():
    """The cordis Python runtime module (`backends/python/runtime.py`), imported
    lazily so `revl.fs`'s legacy surface never depends on it. Returns None if it
    is not importable — a host that never set up a witnessed session."""
    try:
        return import_module("runtime")
    except Exception:  # pragma: no cover - defensive
        return None


def _witnessed_restore(witness) -> None:
    """The host inverse a `write_witnessed` effect replays on abort — the same
    contract as `stdlib/fs.rvl`'s `restore`: delete a created target, or put the
    preimage snapshot back over a replaced one. Idempotent and confined."""
    _runtime.refuse_unknown_receipt_version(witness)
    target = _runtime.resolve_within(witness["path"])
    if witness.get("created"):
        _runtime.remove_confined(target)
        return
    preimage = witness.get("preimage") or ""
    if preimage:
        preimage = _runtime.resolve_sidecar(preimage, "preimage")
        if _runtime.lexists_confined(preimage):
            _runtime.replace_confined(preimage, target)


def _ambient_bind(witness):
    """Register `witness` as a transactional effect on the ambient session's
    current frame, or return ``None`` when no witnessed session is active.

    The effect is bound to the live `SessionOwner`, its newest live registry
    `Frame`, the frame's effect entry/registration order and (under a recording
    run) the durable WAL seq — exactly what a bare host write cannot establish
    for itself (issue #623 consumer impact)."""
    rt = _cordis_runtime()
    if rt is None:
        return None
    owner = rt.session_owner()
    if owner is None or not owner._registry:
        return None
    frame = owner._registry[-1]
    # `_witnessed_restore` is idempotent on replay (a second run once the world
    # is restored is a no-op), exactly like `stdlib/fs.rvl`'s `restore`, so it is
    # registered declared-idempotent (item 309): it replays freely on abort and
    # needs no WAL fence.
    return frame.transactional(_witnessed_restore, witness, undo_idempotent=True)


def _effect_id_of(entry, rt) -> Optional[str]:
    if entry is None or rt is None:
        return None
    owner = rt.session_owner()
    if owner is None:
        return None
    try:
        return owner._entry_identity(entry)
    except Exception:  # pragma: no cover - defensive
        return None


def write_witnessed(path, data: str, *, expect: Expectation = None,
                    bind=None) -> WitnessedWriteReceipt:
    """A guarded native write BOUND to the witnessed teardown/verdict machinery
    (issue #623) — the opt-in counterpart to the legacy `write`.

    Performs the exact same held-descriptor guard as `write` (same `expect=`
    semantics, same fail-closed no-partial-write refusals), snapshots the
    preimage so the effect is reversible, and then registers the write as a
    transactional witnessed effect carrying the ORIGINAL receipt. The bound
    witness is the WAL-serializable record `witness_snapshot` / `prepare_verdict`
    read, so those APIs consume the original held-target facts rather than a
    reconstructed inventory, and the effect discharges on commit / replays its
    `restore` inverse on abort like any other witnessed mutation.

    `bind` is the register hook `(witness) -> effect_entry`; omit it to bind to
    the ambient session (its live `SessionOwner` + newest live `Frame`). If no
    register hook is given AND no witnessed session is active, the write is
    REFUSED (`EUNWITNESSED`) with nothing written — the host API alone cannot
    establish witnessed provenance, so it does not silently degrade to a bare
    `write` (issue #623 consumer impact). Use `write` for an unwitnessed write.
    """
    _validate_write_args(path, data, expect)
    rt = _cordis_runtime()
    binder = bind if bind is not None else _ambient_bind

    # Refuse BEFORE any mutation when there is provably nothing to bind to, so a
    # refused witnessed write never leaves a written-but-unbound target.
    if bind is None and (rt is None or rt.session_owner() is None
                         or not rt.session_owner()._registry):
        raise FsOpError(
            "EUNWITNESSED",
            "write_witnessed requires an active witnessed session (a registered "
            "SessionOwner with a live frame) or an explicit bind= hook; none is "
            "active, so the write is refused rather than silently degrading to "
            "an unbound bare write — use revl.fs.write for that",
            str(path),
        )

    real = _runtime.resolve_within(str(path))
    wr, witness = _perform_write(real, data, expect, capture_witness=True)

    # The mutation landed; now bind it. A binder that raises here is a genuine
    # problem — the bytes are on disk but nothing will ever revert them — so it
    # is NOT swallowed; it propagates to the caller with the target written.
    entry = binder(witness)
    return WitnessedWriteReceipt(
        path=wr.path,
        prev_digest=wr.prev_digest,
        new_digest=wr.new_digest,
        replaced=wr.replaced,
        effect_id=_effect_id_of(entry, rt),
        outcome=witness["outcome"],
    )


class WriteOutcome(NamedTuple):
    """One entry in a `write_all` inventory (issue #623): what happened to one
    requested write. `outcome` is one of `success` / `failed` / `unknown` /
    `unattempted`. `receipt` is the `WitnessedWriteReceipt` on success, else
    None; `error` is the `FsOpError` on a `failed` call, else None."""

    path: str
    outcome: str
    receipt: Optional[WitnessedWriteReceipt]
    error: Optional[BaseException]


def write_all(specs, *, bind=None) -> dict:
    """Attempt a batch of guarded witnessed writes IN ORDER and return an
    accurate partial-failure inventory (issue #623 requirement 5), without any
    automatic retry, recovery, or whole-batch rollback.

    `specs` is a sequence of `(path, data)` or `(path, data, expect)`. Each write
    that lands is bound like `write_witnessed` and recorded `success`. The FIRST
    write that is refused/fails stops the batch: it is recorded `failed` (with
    its `FsOpError`), an error that is not an `FsOpError` — one that leaves the
    landing genuinely undetermined — is recorded `unknown`, and every later spec
    the stop prevented from running is recorded `unattempted`. The four are kept
    distinct rather than collapsed, so a reviewer sees exactly which writes
    landed, which failed, and which never ran.

    Returns `{"results": [WriteOutcome, ...], "attempted", "succeeded",
    "failed", "unknown", "unattempted"}`. The already-bound successes are NOT
    unwound — the session verdict (commit/abort) owns their fate, per the
    witnessed contract; this call introduces no compensation of its own.
    """
    specs = list(specs)
    _runtime.check_witness_batch_bound(len(specs))
    results: list = []
    stopped = False
    for spec in specs:
        if len(spec) == 3:
            path, data, expect = spec
        else:
            path, data = spec
            expect = None
        if stopped:
            results.append(WriteOutcome(str(path), OUTCOME_UNATTEMPTED,
                                        None, None))
            continue
        try:
            r = write_witnessed(path, data, expect=expect, bind=bind)
            results.append(WriteOutcome(r.path, OUTCOME_SUCCESS, r, None))
        except FsOpError as exc:
            # A guarded FsOpError is fail-closed: the target was NOT mutated, so
            # this is a determinate `failed`. Stop the batch — no retry.
            results.append(WriteOutcome(str(path), OUTCOME_FAILED, None, exc))
            stopped = True
        except Exception as exc:  # noqa: BLE001 - landing genuinely undetermined
            # Anything else (e.g. the binder failed after the bytes landed)
            # leaves the outcome uncertain, distinct from a clean `failed`.
            results.append(WriteOutcome(str(path), OUTCOME_UNKNOWN, None, exc))
            stopped = True
    tally = {k: 0 for k in (OUTCOME_SUCCESS, OUTCOME_FAILED, OUTCOME_UNKNOWN,
                            OUTCOME_UNATTEMPTED)}
    for r in results:
        tally[r.outcome] += 1
    return {
        "results": results,
        "attempted": tally[OUTCOME_SUCCESS] + tally[OUTCOME_FAILED]
        + tally[OUTCOME_UNKNOWN],
        "succeeded": tally[OUTCOME_SUCCESS],
        "failed": tally[OUTCOME_FAILED],
        "unknown": tally[OUTCOME_UNKNOWN],
        "unattempted": tally[OUTCOME_UNATTEMPTED],
    }


__all__ = [
    "write",
    "write_witnessed",
    "write_all",
    "WriteReceipt",
    "WitnessedWriteReceipt",
    "WriteOutcome",
    "ABSENT",
    "WRITE_RECEIPT_API_VERSION",
    "WITNESSED_WRITE_API_VERSION",
    "OUTCOME_SUCCESS",
    "OUTCOME_FAILED",
    "OUTCOME_UNKNOWN",
    "OUTCOME_UNATTEMPTED",
    "FsOpError",
    "ConfinementError",
]
