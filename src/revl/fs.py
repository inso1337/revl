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


def write(path, data: str, *, expect: Expectation = None) -> WriteReceipt:
    """Write `data` to `path` inside the session workspace root, returning a
    `WriteReceipt`, and optionally guard the write on the target's current state.

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

    real = _runtime.resolve_within(str(path))
    handle = _runtime.open_confined_write(real)
    committed = False
    try:
        # Facts from the ORIGINAL held descriptor, captured before any content
        # mutation (the open does not truncate). This is the evidence a reopened
        # path cannot supply (issue #523 requirement 4).
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
        elif isinstance(expect, str):
            # A previously-absent target can never satisfy a positive content
            # expectation; refuse rather than create-then-check.
            if handle.created:
                raise FsOpError(
                    "EEXPECT",
                    "expect=<digest> requires the recorded content to still be "
                    "present, but the target did not exist; the write is refused "
                    "and nothing was created",
                    handle.real,
                )
            _runtime.expect_existing(handle, {"digest": expect})
        # expect is None: no guard.

        prev_digest = None if handle.created else receipt["digest"]

        # --- the mutation, THROUGH the verified fd (confinement retained) ---
        _runtime.write_through(handle, data)
        # The name must still resolve to the inode we wrote, or the receipt would
        # lie about where the bytes landed (roadmap 431(b)).
        _runtime.confirm_landed(handle)
        committed = True
        return WriteReceipt(
            path=handle.real,
            prev_digest=prev_digest,
            new_digest=_digest(data.encode("utf-8")),
            replaced=not handle.created,
        )
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


__all__ = [
    "write",
    "WriteReceipt",
    "ABSENT",
    "WRITE_RECEIPT_API_VERSION",
    "FsOpError",
    "ConfinementError",
]
