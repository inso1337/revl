"""Session workspace-root confinement for the witnessed `stdlib/fs.rvl` externs
(roadmap item 244, the confinement slice; docs/witnessed-fs.md).

This is the py-tier support module the `@py` extern bodies in `stdlib/fs.rvl`
import. The emitted module runs with `backends/python` on `sys.path`
(`src/revl/mcp/session.py`, `backends/python/loader.py`), so an `@py` body can
`import revl_fs_workspace` and reach these helpers. Other tiers get their own
confinement support when Slice 2b lands their fs bodies; this file is py-only.

# Why confinement lives here, not in the language

A witnessed fs effect is auto-approved (item 246) and discharged silently at
commit (item 245). Without a workspace boundary, an agent holding `fs`
authority could witnessed-mutate anywhere the process can reach, permanently,
invisible to the single commit prompt. The guard makes the reversible-effect
story honest: every witnessed write/rm/move/mkdir is refused unless its target
resolves inside one configured session root, and the reversal machinery itself
(the garbage dir, the preimage snapshots) also lives inside that root, so
undoing a mutation can never escape confinement either.

# The three musts (Fable, non-negotiable — a decorative guard is worse than none)

1. Resolve symlinks BEFORE the membership check. `resolve_within` realpaths the
   target (every existing path component is symlink-resolved) and only then
   tests root membership. A symlink placed inside the root that points outside
   is therefore caught, not followed blindly. The residual TOCTOU (a component
   swapped to a symlink between this check and the syscall) is bounded, not
   eliminated — see docs/witnessed-fs.md; the check narrows the window, it does
   not close it.
2. The guard applies to the INVERSE path too. `restore`/`unrm`/`unmove`/
   `rmdir_if_empty` re-run `resolve_within` on the path they are about to
   write, so a witness that (through tampering or a moved root) now points
   outside the root cannot make the reversal escape. A confinement failure on
   an inverse raises, which the teardown loop records as `restore-residue`
   rather than silently writing outside.
3. The garbage dir and preimage snapshots live INSIDE the root. `garbage_dir`
   and `preimage_dir` are subdirectories of the workspace root, so an `rm`
   parks the removed file inside the boundary and a `write` snapshots the
   preimage inside the boundary. Reversal reads only from inside the root.

# Configuring the root

The session workspace root is read from the `REVL_FS_WORKSPACE` environment
variable (py tier). Keeping it in the process environment — rather than
threading it through component config — matches the toy witnessed fixture
(`tests/test_witnessed_runtime.py`, `REVL_WIT_TARGET`) and keeps the `@py`
bodies a pure host-local read with no cordis config-resolution dependency.
A future tier / item 294 (parameterized capabilities) can promote this to a
typed session capability; for the py H1 slice it is one env var.
"""

from __future__ import annotations

import os
import uuid

#: The environment variable naming the session workspace root (py tier).
WORKSPACE_ENV = "REVL_FS_WORKSPACE"

#: Subdirectory names, inside the root, that hold the reversal machinery.
#: Both are inside the workspace root (must #3), so a target under them still
#: passes `resolve_within`.
GARBAGE_DIRNAME = ".revl-fs-garbage"
PREIMAGE_DIRNAME = ".revl-fs-preimage"


class ConfinementError(Exception):
    """A witnessed fs op (or its inverse) targeted a path outside the session
    workspace root, or no root was configured. Carries the same
    (code, message, path) shape the `FsError` witness record uses, so an `@py`
    body can turn it straight into an `Err(...)` on the forward path."""

    def __init__(self, code: str, message: str, path: str = "") -> None:
        super().__init__(f"{code}: {message} ({path})")
        self.code = code
        self.message = message
        self.path = path

    def as_error(self) -> dict:
        """The `FsError` record shape, for a forward op's `Err(...)` branch."""
        return {"code": self.code, "message": self.message, "path": self.path}


def workspace_root() -> str:
    """The configured session workspace root, realpath-resolved (so the root
    itself is symlink-canonical and membership tests compare like with like).

    Raises `ConfinementError` when unset: an fs op with no configured root is
    refused, never silently allowed to touch the whole filesystem."""
    root = os.environ.get(WORKSPACE_ENV)
    if not root:
        raise ConfinementError(
            "EWORKSPACE",
            f"no session workspace root configured (set {WORKSPACE_ENV})",
            "",
        )
    real = os.path.realpath(root)
    if not os.path.isdir(real):
        raise ConfinementError(
            "EWORKSPACE",
            "configured session workspace root is not a directory",
            real,
        )
    return real


def _is_within(root: str, real: str) -> bool:
    """True iff `real` is the root itself or a descendant of it. Compares
    realpath'd, normalized absolute paths; the `+ os.sep` guards against a
    sibling whose name merely shares the root as a prefix (`/ws` vs `/ws-evil`)."""
    return real == root or real.startswith(root + os.sep)


def resolve_within(path: str) -> str:
    """Realpath `path` (must #1: symlinks resolved BEFORE the check) and refuse
    it unless it lands inside the session workspace root. Returns the resolved
    absolute path on success; raises `ConfinementError` otherwise.

    A relative `path` is taken relative to the workspace root. `os.path.realpath`
    resolves symlinks in every existing component and normalizes a
    non-existent leaf (a `write`/`mkdir` target that does not exist yet), so a
    symlinked parent directory is fully canonicalized before membership is
    tested — a link inside the root pointing outside is caught here, not
    followed.

    This is the single choke point every forward op AND every inverse (must #2)
    routes through: confinement is enforced in exactly one place."""
    root = workspace_root()
    target = path if os.path.isabs(path) else os.path.join(root, path)
    real = os.path.realpath(target)
    if not _is_within(root, real):
        raise ConfinementError(
            "EOUTSIDE",
            "path escapes the session workspace root",
            real,
        )
    return real


def garbage_dir() -> str:
    """The session garbage directory, created inside the workspace root (must
    #3). `rm` renames its target in here; `unrm` renames it back out."""
    d = os.path.join(workspace_root(), GARBAGE_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d


def preimage_dir() -> str:
    """The preimage-snapshot directory, created inside the workspace root (must
    #3). `write` snapshots the target's preimage in here; `restore` reads it."""
    d = os.path.join(workspace_root(), PREIMAGE_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d


def fresh_sidecar(directory: str, tag: str) -> str:
    """A unique, non-colliding path inside `directory` for a snapshot or a
    parked file. The uuid keeps concurrent ops and repeated same-name removals
    from clobbering one another's sidecars (each rm/write gets its own)."""
    return os.path.join(directory, f"{tag}-{uuid.uuid4().hex}")


def snapshot(src: str, dst: str) -> str:
    """Copy `src` to a fresh `dst`, preferring an APFS `clonefile()` CoW clone
    (O(1), no data copied until one side diverges) and falling back to a
    byte copy on any other filesystem or platform. `dst` must not already
    exist (clonefile requires it). Returns the mechanism used, for the caller's
    audit note.

    The CoW clone is why `write`'s preimage is cheap even for a large file: the
    snapshot shares storage with the original until the subsequent write
    diverges it."""
    if os.uname().sysname == "Darwin":
        try:
            import ctypes
            import ctypes.util

            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            clonefile = libc.clonefile
            clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
            clonefile.restype = ctypes.c_int
            rc = clonefile(os.fsencode(src), os.fsencode(dst), 0)
            if rc == 0:
                return "clonefile"
        except Exception:
            # any failure (non-APFS volume, missing symbol, cross-device) falls
            # through to the portable copy — correctness never depends on CoW.
            pass
    import shutil

    shutil.copy2(src, dst)
    return "copy"
