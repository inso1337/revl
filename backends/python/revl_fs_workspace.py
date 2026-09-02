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

# The four path families (the choke point, stated as an enumeration)

An earlier revision of this file claimed `resolve_within` was "the single choke
point every forward op AND every inverse routes through". It was not: three
families of paths reached a syscall without passing through it, and an
adversarial audit turned each into an executed escape. The claim is now
maintained as an EXPLICIT enumeration — `PATH_FAMILIES` below — with one guard
entry point per family, and `tests/test_fs_confinement_families.py` scans the
`@py` bodies of `stdlib/fs.rvl` to prove no path reaches a mutating syscall by
any other route. A fifth family cannot be added silently: the scan refuses a
raw `os.*` mutation in a body, and the family table has to be edited (and the
test with it) to admit a new one.

1. `named-endpoint` (`resolve_within`) — a path an op was HANDED: `write`'s
   target, `rm`'s target, `move`'s two endpoints, `mkdir`'s target, and every
   inverse's target. Realpathed (must #1: symlinks resolved BEFORE the
   membership test) and refused unless it lands inside the root.
2. `sidecar-directory` (`garbage_dir`, `preimage_dir`, `fresh_sidecar`) — the
   reversal machinery's OWN directories and the files made inside them (must
   #3). Previously built with `os.path.join` + `os.makedirs(exist_ok=True)` and
   returned unresolved; `makedirs`' existence check follows symlinks, so a
   pre-existing `.revl-fs-garbage` / `.revl-fs-preimage` symlink inside the
   workspace silently redirected every preimage snapshot and every parked `rm`
   OUT of the root. Now the directory is created through a directory fd on the
   root, `lstat`-checked to be a real directory and not a link, and returned
   only after `resolve_within` confirms it.
3. `inverse-source` (`resolve_sidecar`) — an inverse's SOURCE endpoint:
   `restore`'s `preimage`, `unrm`'s `garbage`. Previously unchecked, and
   `os.replace` is a RENAME, so an unchecked source is a steal-and-destroy
   primitive: a witness naming a file outside the root removed it from where it
   lived and planted it inside the workspace. `resolve_sidecar` confines the
   source AND requires it to sit inside the matching sidecar directory, so an
   inverse can consume only a sidecar this workspace itself produced — never an
   arbitrary path, inside the root or out (see "why the inverses stay `pure`").
4. `syscall-time` (`open_confined_write`, `replace_confined`, `remove_confined`,
   `mkdir_confined`, `rmdir_confined`, `snapshot_preimage`, `write_through`) —
   the mutation itself. Resolving a path and then re-walking it BY NAME at the
   syscall leaves a check-to-syscall window; measured on the previous revision,
   a competing thread in the workspace won it on essentially every trial. Every
   mutation now runs through a directory fd walked down from the workspace root
   one component at a time with `O_NOFOLLOW`, so no component can be swapped
   for a symlink after the check: the syscall reaches the inode the check
   admitted, or it fails. This family is also where the HARDLINK control lives
   (below).

# Hardlinks

`realpath` cannot see through a hardlink: an inside name and an outside name
can be two directory entries for the SAME inode, and every path-based check
passes while a write mutates the file outside the root. Worse, the undo's
`os.replace(preimage, target)` replaces the NAME and breaks the link, so the
inside file reads as reverted while the outside file keeps the attacker's
bytes — residue no discharge descriptor enumerates.

Writing through an fd does not fix this (the fd names the same shared inode),
so the honest control is REFUSAL: `open_confined_write` `fstat`s the fd it
opened and returns `EMULTILINK` when `st_nlink > 1`. A refused write is an
`Err`, so Ok-conditional registration means nothing is registered and there is
no undo to mislead. The cost is real and stated plainly: a legitimately
hardlinked file inside the workspace cannot be witnessed-written. That is the
right trade here — the workspace jail's whole premise is that the workspace
writer is untrusted, unlike `hostfile.py`'s item-396 jail, where an accepted
hardlink residual is justified by the author already controlling the bytes.

# Why the inverses stay `pure` (and what makes that safe now)

The `restore`/`unrm`/`unmove`/`rmdir_if_empty` externs are `pure`, which means
they carry NO capability: they are callable from every pure position. That
classification is not a slip, and it cannot simply be changed:
`_check_witnessed_inverse` (src/revl/lower.py, item 243 rule 3, G5) requires a
witnessed extern's declared inverse to be non-emitting and non-witnessed — an
emitting inverse crosses a one-way boundary during teardown, a witnessed one is
infinite regress — which leaves `pure` and `acquire`; the parser refuses a
`[caps]` bracket on both (only `witnessed[...]`/`emission[...]` are
capability-scoped), and an `acquire` inverse would itself have to declare an
`undo`. There is no spelling of "capability-scoped inverse" in the surface, by
design.

So the fix is not to gate the primitive but to shrink it. After this change the
inverses' entire authority is: move a sidecar THIS WORKSPACE PRODUCED back over
a path inside the same workspace, or delete a path inside the workspace. Every
endpoint, source and target, is confined; the source is additionally restricted
to the sidecar directories. A capability-free inverse can no longer name an
outside file at all, so the WAL-replay vector (`revl recover` reconstructing an
inverse from a forged witness, src/revl/recovery.py) hits the same guard with
no revl source involved. What remains is bounded by the jail itself, which is
exactly the property the jail is supposed to provide.

# Configuring the root

The session workspace root is read from the `REVL_FS_WORKSPACE` environment
variable (py tier). Keeping it in the process environment — rather than
threading it through component config — matches the toy witnessed fixture
(`tests/test_witnessed_runtime.py`, `REVL_WIT_TARGET`) and keeps the `@py`
bodies a pure host-local read with no cordis config-resolution dependency.
A future tier / item 294 (parameterized capabilities) can promote this to a
typed session capability; for the py H1 slice it is one env var.

The root itself is opened by name once per operation; it is the trust anchor
the session configures, not attacker-supplied input, and everything below it is
reached only through fds. A root that is itself moved or replaced mid-session
is out of scope for this guard, as is the ts tier (`revl_fs_ts.ts`), which
still carries the pre-fix shape and gets its own pass with Slice 2b.
"""

from __future__ import annotations

import os
import stat
import uuid

#: The environment variable naming the session workspace root (py tier).
WORKSPACE_ENV = "REVL_FS_WORKSPACE"

#: Subdirectory names, inside the root, that hold the reversal machinery.
#: Both are inside the workspace root (must #3), so a target under them still
#: passes `resolve_within`.
GARBAGE_DIRNAME = ".revl-fs-garbage"
PREIMAGE_DIRNAME = ".revl-fs-preimage"

#: The sidecar `kind` tokens `fresh_sidecar`/`resolve_sidecar` speak, mapped to
#: their directory name. `rm` parks in `garbage`; `write` snapshots in
#: `preimage`.
SIDECAR_KINDS = {"garbage": GARBAGE_DIRNAME, "preimage": PREIMAGE_DIRNAME}

#: Every family of paths in this slice that can reach a filesystem syscall, and
#: the guard entry points that family must route through. This table IS the
#: single-choke-point claim, stated so it can be tested:
#: `tests/test_fs_confinement_families.py` asserts the family set, and scans the
#: `@py` bodies of `stdlib/fs.rvl` to prove every mutating call is one of the
#: `syscall-time` entry points and every path handed to one was bound from a
#: family 1-3 guard. Adding a family means editing this table and that test.
PATH_FAMILIES: dict[str, tuple[str, ...]] = {
    "named-endpoint": ("resolve_within",),
    "sidecar-directory": ("garbage_dir", "preimage_dir", "fresh_sidecar"),
    "inverse-source": ("resolve_sidecar",),
    "syscall-time": ("open_confined_write", "write_through", "snapshot_preimage",
                     "replace_confined", "remove_confined", "mkdir_confined",
                     "rmdir_confined", "close_handle", "discard_write"),
}

#: Read-only helpers an `@py` body may call. They observe and mutate nothing,
#: so they belong to no family; listing them here is what lets the family scan
#: refuse EVERYTHING else, instead of maintaining a hand-kept exception list in
#: the test.
READ_HELPERS: tuple[str, ...] = ("lexists_confined",)

#: Which positional arguments of a `syscall-time` entry point are PATHS (and so
#: must have come from a family 1-3 guard). The rest are handles or data.
SYSCALL_PATH_ARGS: dict[str, tuple[int, ...]] = {
    "open_confined_write": (0,),
    "write_through": (),
    "snapshot_preimage": (),
    "replace_confined": (0, 1),
    "remove_confined": (0,),
    "mkdir_confined": (0,),
    "rmdir_confined": (0,),
    "close_handle": (),
    "discard_write": (),
}

_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class FsOpError(Exception):
    """A witnessed fs op (or its inverse) could not run. Carries the same
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


class ConfinementError(FsOpError):
    """The boundary refusal specifically: a path (a target, a sidecar, or an
    inverse's source) did not resolve inside the session workspace root, or no
    root was configured. A subclass of `FsOpError` so a body needs one `except`
    clause, and a distinct type so a caller (and the test suite) can tell a
    confinement refusal from an ordinary `ENOENT`."""


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


# ---------------------------------------------------------------------------
# family 1: named endpoints
# ---------------------------------------------------------------------------

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

    This is the `named-endpoint` family's guard: every path an op is HANDED
    routes through it, forward ops and inverses alike (must #2). It is not the
    whole story — see `PATH_FAMILIES` for the other three families and their
    guards. A resolved path is a CHECKED path, not yet a safe syscall: the
    `syscall-time` helpers below are what actually reach the filesystem, and
    they re-establish containment through directory fds rather than trusting
    this string a second time."""
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


# ---------------------------------------------------------------------------
# the directory-fd walk: how a checked path becomes a safe syscall
# ---------------------------------------------------------------------------

def _open_dirfd(real_dir: str) -> int:
    """A directory fd for `real_dir`, obtained by opening the workspace root and
    then walking DOWN one component at a time with `O_NOFOLLOW`.

    This is what closes the check-to-syscall window. A path-based syscall
    re-traverses every component by name, so a concurrent writer that swaps a
    component for a symlink after `resolve_within` returns redirects the
    syscall. Walking with fds means each component is resolved exactly once,
    and `O_NOFOLLOW` refuses a symlink component outright (`ELOOP`), so the
    directory we end up holding is provably the one the membership check
    admitted — no component can be re-pointed underneath us.

    Raises `ConfinementError` if `real_dir` is not inside the root; propagates
    `OSError` (`ENOENT` for a missing component, `ELOOP` for a swapped one) for
    the caller to translate."""
    root = workspace_root()
    if not _is_within(root, real_dir):
        raise ConfinementError(
            "EOUTSIDE", "path escapes the session workspace root", real_dir)
    fd = os.open(root, os.O_RDONLY | _O_DIRECTORY)
    try:
        rel = os.path.relpath(real_dir, root)
        parts = [] if rel == os.curdir else rel.split(os.sep)
        for part in parts:
            nxt = os.open(part, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                          dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def _split(real: str) -> tuple[str, str]:
    """A resolved path as (parent directory, leaf name), refusing the root
    itself — no op may replace or remove the workspace root."""
    root = workspace_root()
    if real == root:
        raise ConfinementError(
            "EWORKSPACE", "the workspace root itself is not a valid target", real)
    return os.path.dirname(real), os.path.basename(real)


# ---------------------------------------------------------------------------
# family 2: the sidecar directories and the files made inside them
# ---------------------------------------------------------------------------

def _sidecar_dir_real(kind: str, create: bool) -> str:
    """The resolved path of a sidecar directory, refusing a symlink.

    `os.makedirs(d, exist_ok=True)` — what this used to do — succeeds on a
    PRE-EXISTING SYMLINK, because its existence check follows links, and the
    old callers then returned the unresolved `d`. One symlink named
    `.revl-fs-garbage` or `.revl-fs-preimage` inside the workspace therefore
    redirected the whole reversal machinery outside the root. Here the
    directory is created through a fd on the root, then `lstat`ed through that
    same fd: a symlink is refused by TYPE, not merely resolved and compared, so
    a dangling link (which realpath alone normalizes happily) is caught too.
    `resolve_within` then confirms containment."""
    name = SIDECAR_KINDS[kind]
    root = workspace_root()
    rootfd = os.open(root, os.O_RDONLY | _O_DIRECTORY)
    try:
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=rootfd)
            except FileExistsError:
                pass
        try:
            st = os.lstat(name, dir_fd=rootfd)
        except FileNotFoundError:
            # not created yet and we were told not to create it: nothing can
            # legitimately live inside it, so name the (absent) directory.
            return os.path.join(root, name)
        if stat.S_ISLNK(st.st_mode):
            raise ConfinementError(
                "EOUTSIDE",
                f"the `{name}` sidecar directory is a symlink; the reversal "
                "machinery must live inside the session workspace root",
                os.path.realpath(os.path.join(root, name)),
            )
        if not stat.S_ISDIR(st.st_mode):
            raise ConfinementError(
                "EWORKSPACE",
                f"the `{name}` sidecar path is not a directory",
                os.path.join(root, name),
            )
    finally:
        os.close(rootfd)
    return resolve_within(os.path.join(root, name))


def garbage_dir() -> str:
    """The session garbage directory, created inside the workspace root (must
    #3) and returned RESOLVED. `rm` renames its target in here; `unrm` renames
    it back out."""
    return _sidecar_dir_real("garbage", create=True)


def preimage_dir() -> str:
    """The preimage-snapshot directory, created inside the workspace root (must
    #3) and returned RESOLVED. `write` snapshots the target's preimage in here;
    `restore` reads it."""
    return _sidecar_dir_real("preimage", create=True)


def _sidecar_kind_of(real_dir: str) -> str:
    """The sidecar `kind` whose directory is `real_dir`, or a refusal. Keeps
    `fresh_sidecar` from being handed an arbitrary directory: the only places a
    sidecar may be made are the two the reversal machinery owns."""
    for kind in SIDECAR_KINDS:
        if real_dir == _sidecar_dir_real(kind, create=False):
            return kind
    raise ConfinementError(
        "EOUTSIDE",
        "a sidecar may only be created in the session garbage or preimage "
        "directory",
        real_dir,
    )


def fresh_sidecar(directory: str, tag: str) -> str:
    """A unique, non-colliding path inside `directory` for a snapshot or a
    parked file. The uuid keeps concurrent ops and repeated same-name removals
    from clobbering one another's sidecars (each rm/write gets its own).

    `directory` is re-resolved and required to BE one of the two sidecar
    directories, and the returned leaf is itself put through `resolve_within`,
    so the sidecar path is a guarded path and not merely a string joined onto a
    guarded one."""
    real_dir = resolve_within(directory)
    _sidecar_kind_of(real_dir)
    return resolve_within(os.path.join(real_dir, f"{tag}-{uuid.uuid4().hex}"))


# ---------------------------------------------------------------------------
# family 3: an inverse's source endpoint
# ---------------------------------------------------------------------------

def resolve_sidecar(path: str, kind: str) -> str:
    """The guard for an inverse's SOURCE endpoint: `restore`'s `preimage`,
    `unrm`'s `garbage`.

    Confinement to the root is necessary but not sufficient here. The inverses
    are `pure` (item 243 rule 3 leaves no capability-scoped spelling for them —
    see the module docstring), so this is a capability-free primitive callable
    from any pure position, and `os.replace` is a rename: whatever it names as a
    source is REMOVED from where it lives. Admitting any confined path would
    still make the inverses an arbitrary move-anything-inside-the-workspace
    primitive, and admitting an unconfined one made them a steal-and-destroy
    primitive for the whole filesystem.

    So the source is restricted to the matching sidecar directory: an inverse
    may consume only a sidecar this workspace itself produced. Returns the
    resolved path (which may not exist — the caller checks, because a replayed
    inverse whose sidecar is already consumed must no-op, item 243 rule 5);
    raises `ConfinementError` for anything else."""
    real = resolve_within(path)
    real_dir = _sidecar_dir_real(kind, create=False)
    if os.path.dirname(real) != real_dir or real == real_dir:
        raise ConfinementError(
            "EOUTSIDE",
            f"an inverse may only consume a sidecar from the session {kind} "
            "directory",
            real,
        )
    return real


# ---------------------------------------------------------------------------
# family 4: the mutations themselves
# ---------------------------------------------------------------------------

class WriteHandle:
    """An open, verified write fd plus what the caller needs to finish the op.

    Holding the fd across the snapshot and the write is the point: containment,
    file type and link count were established ON THIS FD, and every subsequent
    step uses the fd rather than re-walking the name."""

    __slots__ = ("fd", "real", "created", "mode", "atime_ns", "mtime_ns")

    def __init__(self, fd: int, real: str, created: bool, st) -> None:
        self.fd = fd
        self.real = real
        self.created = created
        self.mode = stat.S_IMODE(st.st_mode)
        self.atime_ns = st.st_atime_ns
        self.mtime_ns = st.st_mtime_ns


#: How many times `_open_leaf` retries the open/create pair before giving up.
#: Only a writer racing the very same leaf can consume an attempt, and each
#: attempt is one syscall, so a small bound is enough to keep an honest
#: concurrent creation from failing while refusing to spin against a hostile one.
_OPEN_ATTEMPTS = 8


def _open_leaf(dirfd: int, leaf: str, real: str) -> tuple[int, bool]:
    """Open `leaf` inside the already-verified directory fd, creating it if it
    does not exist, and report which happened.

    `O_NOFOLLOW` is the point: `resolve_within` saw a real file (or nothing),
    so a symlink here means the leaf was swapped after the check — refused as a
    confinement failure, never followed. The open/create pair races against a
    concurrent writer of the same name in both directions (the file can appear
    between the open and the create, or vanish between the create and the
    retry), so it retries a bounded number of times rather than surfacing the
    raw errno.

    The fd is `O_RDWR`, not `O_WRONLY`, because the preimage snapshot is read
    back out of this very fd (`snapshot_preimage`) rather than reopening the
    target by name — reading the name a second time is exactly the race the fd
    exists to avoid. A target the process may write but not read is therefore
    refused (`EACCES` from the open), which is the honest outcome: without read
    access there is no preimage, and without a preimage the write is not
    reversible."""
    for _ in range(_OPEN_ATTEMPTS):
        try:
            return os.open(leaf, os.O_RDWR | _O_NOFOLLOW | _O_NONBLOCK,
                           dir_fd=dirfd), False
        except FileNotFoundError:
            pass
        except IsADirectoryError:
            raise FsOpError(
                "ENOTFILE", "write target exists and is not a regular file",
                real) from None
        except OSError as exc:
            # ELOOP: the leaf is a symlink now, though `resolve_within` saw a
            # real file. That is exactly the swapped-component race, refused.
            raise ConfinementError(
                "EOUTSIDE",
                f"the write target changed under the confinement check "
                f"({exc.strerror})",
                real,
            ) from None
        try:
            return os.open(leaf, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o666,
                           dir_fd=dirfd), True
        except FileExistsError:
            continue
    raise FsOpError(
        "ERACE",
        "the write target kept appearing and vanishing under a concurrent "
        "writer, so no open could be verified",
        real,
    )


def open_confined_write(path: str) -> WriteHandle:
    """Open `path` for writing, inside the root, without a check-to-syscall
    window and without a hardlink write-through.

    Order matters: resolve (family 1), walk to the parent through directory fds
    with `O_NOFOLLOW` (so no component can be swapped after the check), open the
    leaf with `O_NOFOLLOW` (so the leaf itself cannot be swapped for a symlink
    in the window that remains), and only THEN `fstat` the fd to decide whether
    the file is writable at all. Nothing is truncated by the open, so a refusal
    leaves the target byte-identical.

    Refusals, all as `FsOpError` (an `Err` on the forward path, which registers
    no inverse):
      * `EOUTSIDE`   - outside the root, or a symlink appeared where the check
        saw a real file (a lost race, refused rather than followed);
      * `ENOENT`     - the parent directory does not exist (we create no
        directories, so the inverse leaves zero residue);
      * `ENOTFILE`   - the target exists and is not a regular file;
      * `EMULTILINK` - the target has more than one link. `realpath` cannot see
        through a hardlink and writing through the fd would still mutate the
        shared inode, so a multiply-linked target is refused outright;
      * `ERACE`      - a concurrent writer kept the leaf appearing and vanishing
        for more attempts than `_open_leaf` will spend."""
    real = resolve_within(path)
    parent, leaf = _split(real)
    try:
        dirfd = _open_dirfd(parent)
    except (FileNotFoundError, NotADirectoryError):
        raise FsOpError("ENOENT", "parent directory does not exist", real) from None
    except ConfinementError:
        raise
    except OSError as exc:
        raise ConfinementError(
            "EOUTSIDE",
            f"the path to the write target changed under the confinement check "
            f"({exc.strerror})",
            real,
        ) from None
    try:
        fd, created = _open_leaf(dirfd, leaf, real)
    finally:
        os.close(dirfd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise FsOpError(
                "ENOTFILE", "write target exists and is not a regular file", real)
        if st.st_nlink > 1:
            raise FsOpError(
                "EMULTILINK",
                "write target has more than one hard link, so the write would "
                "reach an inode the workspace boundary cannot confine",
                real,
            )
    except BaseException:
        os.close(fd)
        raise
    return WriteHandle(fd, real, created, st)


def close_handle(handle: WriteHandle) -> None:
    """Release a `WriteHandle`'s fd. Idempotent."""
    if handle.fd >= 0:
        os.close(handle.fd)
        handle.fd = -1


def discard_write(handle: WriteHandle) -> None:
    """Abandon a write that failed after the open: if the open CREATED the
    target, remove it again.

    A forward op that returns `Err` registers no inverse (Ok-conditional
    registration, item 243), so anything the failed attempt left behind would be
    residue nothing enumerates. An existing target is left alone — the open does
    not truncate, so it still holds its original bytes."""
    if handle.created:
        remove_confined(handle.real)


def write_through(handle: WriteHandle, contents: str) -> None:
    """Truncate and write `contents` THROUGH the verified fd — never by name, so
    the bytes reach the inode `open_confined_write` admitted and no other."""
    os.ftruncate(handle.fd, 0)
    os.lseek(handle.fd, 0, os.SEEK_SET)
    data = contents.encode("utf-8")
    while data:
        data = data[os.write(handle.fd, data):]


def _clone_from_fd(src_fd: int, dst_dirfd: int, dst_leaf: str) -> bool:
    """Try APFS `fclonefileat()`: a CoW clone of an OPEN fd into a directory fd.

    The fd-and-dirfd form is what keeps the cheap snapshot without reopening the
    source by name — the clone is taken from the very fd whose containment,
    type and link count were verified. Returns False on any failure (non-APFS
    volume, other platform, missing symbol) so correctness never depends on
    CoW."""
    if os.uname().sysname != "Darwin":
        return False
    try:
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        fclonefileat = libc.fclonefileat
        fclonefileat.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p,
                                 ctypes.c_uint32]
        fclonefileat.restype = ctypes.c_int
        return fclonefileat(src_fd, dst_dirfd, os.fsencode(dst_leaf), 0) == 0
    except Exception:
        return False


def snapshot_preimage(handle: WriteHandle) -> str:
    """Snapshot the open target into a fresh preimage sidecar, and return the
    sidecar's path (the witness's `preimage`).

    The source is the verified fd, not a name, so the snapshot cannot be raced
    into copying some other file; the destination is a guarded sidecar path
    (family 2) created through the preimage directory's own fd with
    `O_CREAT|O_EXCL`. The APFS clone is attempted first (`fclonefileat`, O(1)
    until the subsequent write diverges the two), with a portable `pread`/write
    copy as the fallback; the fallback restores the original mode and mtime so a
    restored preimage is not silently re-permissioned."""
    directory = preimage_dir()
    dst = fresh_sidecar(directory, "pre")
    dst_leaf = os.path.basename(dst)
    dirfd = _open_dirfd(directory)
    try:
        if _clone_from_fd(handle.fd, dirfd, dst_leaf):
            return dst
        out = os.open(dst_leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                      dir_fd=dirfd)
        try:
            offset = 0
            while True:
                chunk = os.pread(handle.fd, 1 << 20, offset)
                if not chunk:
                    break
                offset += len(chunk)
                while chunk:
                    chunk = chunk[os.write(out, chunk):]
            os.fchmod(out, handle.mode)
            os.utime(out, ns=(handle.atime_ns, handle.mtime_ns))
        finally:
            os.close(out)
    finally:
        os.close(dirfd)
    return dst


def replace_confined(src_real: str, dst_real: str) -> None:
    """`os.replace(src, dst)` performed through both parents' directory fds.

    Both endpoints must already have come from a family 1-3 guard; this
    re-establishes them as fds so the rename cannot be redirected by a
    component swapped after the check. A missing source is `ENOENT` (the caller
    decides whether that is an error or an idempotent replay)."""
    src_parent, src_leaf = _split(src_real)
    dst_parent, dst_leaf = _split(dst_real)
    try:
        src_dirfd = _open_dirfd(src_parent)
    except (FileNotFoundError, NotADirectoryError):
        raise FsOpError("ENOENT", "no such file", src_real) from None
    try:
        try:
            dst_dirfd = _open_dirfd(dst_parent)
        except (FileNotFoundError, NotADirectoryError):
            raise FsOpError(
                "ENOENT", "parent directory does not exist", dst_real) from None
        try:
            os.replace(src_leaf, dst_leaf,
                       src_dir_fd=src_dirfd, dst_dir_fd=dst_dirfd)
        except FileNotFoundError:
            raise FsOpError("ENOENT", "no such file", src_real) from None
        finally:
            os.close(dst_dirfd)
    finally:
        os.close(src_dirfd)


def remove_confined(real: str) -> None:
    """`os.remove` through the parent's directory fd. A missing target is a
    no-op: an inverse must be idempotent on replay (item 243 rule 5)."""
    parent, leaf = _split(real)
    try:
        dirfd = _open_dirfd(parent)
    except (FileNotFoundError, NotADirectoryError):
        return
    try:
        os.unlink(leaf, dir_fd=dirfd)
    except (FileNotFoundError, NotADirectoryError):
        pass
    finally:
        os.close(dirfd)


def mkdir_confined(real: str) -> None:
    """`os.mkdir` through the parent's directory fd."""
    parent, leaf = _split(real)
    try:
        dirfd = _open_dirfd(parent)
    except (FileNotFoundError, NotADirectoryError):
        raise FsOpError("ENOENT", "parent directory does not exist", real) from None
    try:
        os.mkdir(leaf, dir_fd=dirfd)
    except FileExistsError:
        raise FsOpError("EEXIST", "path already exists", real) from None
    finally:
        os.close(dirfd)


def rmdir_confined(real: str) -> None:
    """`os.rmdir` through the parent's directory fd, iff the directory is still
    empty. Total and idempotent: a missing or non-empty directory is left as-is,
    never a raise — `rmdir_if_empty` must never delete a directory the
    activation (or a concurrent writer) populated."""
    parent, leaf = _split(real)
    try:
        dirfd = _open_dirfd(parent)
    except (FileNotFoundError, NotADirectoryError):
        return
    try:
        os.rmdir(leaf, dir_fd=dirfd)
    except OSError:
        pass
    finally:
        os.close(dirfd)


def lexists_confined(real: str) -> bool:
    """Does `real` name something (a dangling symlink included)? A READ, used by
    the bodies for their `ENOENT`/`EEXIST` pre-checks and for an inverse's
    idempotent no-op. It decides nothing about confinement — the mutation that
    follows re-establishes containment through fds regardless of what this said,
    so a lost race here costs an error message, never an escape."""
    return os.path.lexists(real)
