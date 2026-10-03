"""The open-descriptor budget each test file has to stay within (issue #1720).

The root suite runs in one process, so a descriptor a test file leaves open
stays open for every file after it. By the time `frontend-cordis` reached
`tests/test_mcp_http_streams.py` the process held more than 1024, which is how
#1716 surfaced: `select()` refused a descriptor above FD_SETSIZE. The leaks
themselves were silent, because nothing counted.

`tests/conftest.py` counts. It reads the open descriptors when a test file
starts and again when it ends. If the file left more than `BUDGET` new ones
behind, even after a garbage collection, the file's last test errors at
teardown and names the file, the count and what the descriptors are (socket,
pipe, or the file's path).

The budget is small and not zero. A module may open something once and keep
it on purpose (a lazily opened log, a cache), and that is not a leak. A leak
grows with the number of tests, and the measured offenders grew by 3 per test.
A file that legitimately holds more says so in `ALLOWANCES`, with the reason.
"""

from __future__ import annotations

import gc
import os
import stat

#: New descriptors a test file may leave open without a reason on record.
BUDGET = 4

#: test file (relative to the repository root) -> (descriptors it may keep,
#: why). Empty: no file needs one today.
ALLOWANCES: dict[str, tuple[int, str]] = {}

_FD_DIR = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"


def available() -> bool:
    """Descriptors can be listed here (Linux and macOS; not Windows)."""
    return os.path.isdir(_FD_DIR)


def open_fds() -> set:
    fds = set()
    for name in os.listdir(_FD_DIR):
        try:
            fd = int(name)
            os.fstat(fd)
        except (ValueError, OSError):
            continue  # the listing's own descriptor, already gone
        fds.add(fd)
    return fds


def describe(fd: int) -> str:
    try:
        mode = os.fstat(fd).st_mode
    except OSError:
        return "closed"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISFIFO(mode):
        return "pipe"
    if stat.S_ISREG(mode):
        try:
            return os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            pass
        try:
            import fcntl  # noqa: PLC0415
            raw = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024)
            return raw.split(b"\0", 1)[0].decode("utf-8", "replace")
        except (AttributeError, OSError, ImportError):
            return "file"
    return "other"


def leaked(before: set) -> set:
    """The descriptors opened since *before* that are still open. Collected
    garbage is given its chance first, but only when the count is over budget,
    so a clean file pays nothing for the check."""
    new = open_fds() - before
    if len(new) > BUDGET:
        gc.collect()
        new = open_fds() - before
    return new


def verdict(module: str, new: set) -> str | None:
    """None when *module* is within its budget, else the failure message."""
    allowed, why = ALLOWANCES.get(module, (BUDGET, ""))
    if len(new) <= allowed:
        return None
    kinds: dict[str, int] = {}
    for fd in new:
        kind = describe(fd)
        kinds[kind] = kinds.get(kind, 0) + 1
    shown = ", ".join(f"{kind} x{count}" for kind, count in
                      sorted(kinds.items(), key=lambda item: -item[1])[:8])
    return (f"{module} left {len(new)} file descriptors open (budget {allowed}): "
            f"{shown}. Close what the tests open (sockets, servers, subprocess "
            f"pipes, event loops, files) in teardown, or record a reason in "
            f"tests/_fd_budget.py ALLOWANCES (issue #1720).")
