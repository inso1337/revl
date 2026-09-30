"""Writing a private pool's state files: one writer at a time, and never a torn
file (issue #1198).

A pool directory holds the charter, the roster, the key directory, the
delivery ledger and the health record. Several operator processes change them:
`revl pool join` and `withdraw`, `revl run --pool private`, `revl pool probe`,
`register`, `rotate` and `revoke-key`. Every one of those is a
read-modify-write of a JSON file, and before this module nothing serialised
them. Two of them at once lost an update, and the worst case lost an
AUTHORITY update: a dispatch that finished after a withdrawal wrote back the
roster it had read before, and the withdrawn peer was a member again.

Two mechanisms, for two different faults:

* :func:`locked` serialises writers. It is an exclusive advisory lock on
  ``pool.lock`` in the pool directory, held for one read-modify-write and
  never across the network. Every writer takes it around its whole cycle,
  from the read it decides on to the last write, so no writer decides on
  state another writer is about to replace.
* :func:`write_json` makes each file replacement atomic: write a sibling temp
  file, fsync it, ``os.replace`` it over the target, fsync the directory.
  That is the convention `revl._deploy_participant` already uses for its world
  file (issues #538 and #627). A reader that takes no lock, like
  `pool status`, then sees a whole old file or a whole new one, never half of
  one. It does NOT make a set of files consistent with each other; a reader
  may see a roster from one transaction and a ledger from the next.

The lock is advisory: it binds `revl` processes, which all go through this
module. It is not a defence against another program editing the files, and
nothing here claims it is. The lock is not re-entrant, and taking it twice in
one thread raises rather than deadlocking, so a nested acquisition is a bug
reported at the call site rather than a hang.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

LOCK_FILE = "pool.lock"

#: The lock files this thread holds, so a nested acquisition fails loudly. An
#: flock taken twice by one process on two descriptors would wait on itself.
_held = threading.local()


class PoolStateError(RuntimeError):
    """The pool state could not be locked or written."""


def _held_paths() -> set:
    if not hasattr(_held, "paths"):
        _held.paths = set()
    return _held.paths


def _acquire(fd: int) -> None:
    try:
        import fcntl  # noqa: PLC0415 (POSIX)
    except ImportError:  # pragma: no cover - not exercised in CI (Linux only)
        import msvcrt  # noqa: PLC0415

        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                return
            except OSError:
                continue   # LK_LOCK gives up after ten seconds; keep waiting
    fcntl.flock(fd, fcntl.LOCK_EX)


def _release(fd: int) -> None:
    try:
        import fcntl  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        import msvcrt  # noqa: PLC0415

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def locked(pool_dir) -> Iterator[None]:
    """Hold the pool directory's writer lock for the body of the block.

    Blocks until the lock is free. Keep the body to one read-modify-write of
    local files: a writer that held this across a network round trip would
    stall every other operator command for as long as the peer took."""
    path = Path(pool_dir) / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    held = _held_paths()
    if key in held:
        raise PoolStateError(
            f"{path} is already held by this thread; the pool lock is not "
            f"re-entrant, so the caller must not take it twice")
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        _acquire(fd)
        held.add(key)
        try:
            yield
        finally:
            held.discard(key)
            _release(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    if os.name != "posix":  # pragma: no cover - no directory fsync there
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, payload: Any) -> None:
    """Replace ``path`` with ``payload`` as sorted, indented JSON, atomically.

    The bytes are the ones the pool modules always wrote (two-space indent,
    sorted keys, trailing newline), so a file written here compares equal to
    one written before. The mode of an existing file is kept; a new one is
    0644, since nothing in a pool's state files is secret and `pool status`
    is meant to be readable without a key."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    mode = (target.stat().st_mode & 0o777) if target.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=str(target.parent),
                               prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(target.parent)
