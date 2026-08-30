"""item 396 option A: resolve and splice an extern's external host-body file.

`= @backend file "path"` reads the file at COMPILE time and splices its
contents as the extern body, exactly as an inline `= @backend { ... }` body is
spliced today. This module owns the JAIL that makes that read safe: an unjailed
splice is an arbitrary-file-read primitive, so containment is enforced against
the REALPATH of the OPENED handle (never a string prefix, which conflates
`/foo` with `/foobar`), the read is open-then-fstat-then-validate (the checked
file and the spliced file cannot be swapped between check and use), and a
virtual (in-memory) compile resolves only through the supplied sources map with
no disk fallback.

The one accepted residual, stated rather than claimed away: a HARDLINK inside
the module tree to a file outside it realpaths to the inside name and passes.
Creating that hardlink needs write access inside the tree being compiled, at
which point the same author could paste the same bytes into an inline body; the
jail's threat model is reference REACH, not an author who already writes the
tree (design §"Resolution, and the jail").
"""

from __future__ import annotations

import hashlib
import os
import stat as _stat
import sys
import textwrap

from .errors import RevlError


def normalize_body(text: str) -> str:
    """The canonical pre-splice normalization (design §Emit).

    Applied ONCE here so a file-sourced body reaches the emitters already
    dedented and end-stripped, matching the `textwrap.dedent(text.strip("\\n"))`
    the py and ts emitters apply (idempotently) to an inline body. On py/ts this
    makes `= @py { X }` and `= @py file` (file containing exactly X)
    byte-identical on the emitted artifact; on the strip-only tiers
    (go/rust/java) the normalized bytes land verbatim, deterministic but equal
    to an inline twin only when that twin was authored flush-left.
    """
    return textwrap.dedent(text.strip("\n"))


def _normcase(path: str) -> str:
    # Case-fold on case-insensitive filesystems (macOS/APFS default, Windows)
    # so the containment check is not defeated by a case variant of the jail
    # root. `os.path.normcase` only folds on Windows, so fold explicitly.
    if sys.platform in ("darwin", "win32"):
        return path.lower()
    return path


def _contained(child_real: str, root_real: str) -> bool:
    """True iff `child_real` is `root_real` or a descendant of it, via
    `os.path.commonpath` over canonicalized operands — never `str.startswith`,
    which would conflate `/foo` with `/foobar`."""
    child = _normcase(child_real)
    root = _normcase(root_real)
    try:
        return os.path.commonpath([root, child]) == root
    except ValueError:
        # different drives, or a mix of absolute and relative — not contained.
        return False


def _reject_bad_written_path(written: str, backend: str, decl_name: str,
                             filename: str, line: int) -> None:
    """Refuse the textual escapes (absolute path, `..` segment) BEFORE any
    filesystem access, so the message never depends on what is on disk."""
    if not written:
        raise RevlError(
            filename, line,
            f"empty @{backend} host-body file path for extern `{decl_name}`")
    if os.path.isabs(written):
        raise RevlError(
            filename, line,
            f"@{backend} host-body file path {written!r} for extern "
            f"`{decl_name}` is absolute",
            hint="a body file is resolved strictly relative to the declaring "
                 ".rvl file's directory; absolute paths are refused (item 396 "
                 "jail)")
    parts = written.replace("\\", "/").split("/")
    if ".." in parts:
        raise RevlError(
            filename, line,
            f"@{backend} host-body file path {written!r} for extern "
            f"`{decl_name}` escapes its module directory with `..`",
            hint="a body file must sit inside the declaring module's own "
                 "directory; `..` segments are refused as written (item 396 "
                 "jail)")


def _real_path_of_fd(fd: int, fallback: str) -> str:
    """The real path of the ACTUAL opened handle, so containment is validated
    against what will be read rather than a name that could be swapped after
    the check (TOCTOU). macOS: `F_GETPATH`; Linux: `/proc/self/fd`. Falls back
    to `realpath` of the candidate where neither is available."""
    try:
        if sys.platform == "darwin":
            import fcntl
            _F_GETPATH = 50
            raw = fcntl.fcntl(fd, _F_GETPATH, b"\x00" * 1024)
            return os.fsdecode(raw.split(b"\x00", 1)[0])
        if sys.platform.startswith("linux"):
            return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        pass
    return os.path.realpath(fallback)


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        block = os.read(fd, 65536)
        if not block:
            break
        chunks.append(block)
    return b"".join(chunks)


def read_body_file_disk(module_dir: str, written: str, backend: str,
                        decl_name: str, filename: str,
                        line: int) -> tuple[str, str]:
    """Read a body file from disk under the jail. Returns
    `(normalized_text, sha256_of_raw_bytes)`.

    open-then-fstat-then-validate: open once, confirm it is a regular file,
    validate containment against the OPENED handle's real path, and read from
    that same fd, so the checked file and the spliced file are identical.
    """
    _reject_bad_written_path(written, backend, decl_name, filename, line)
    candidate = os.path.join(module_dir, written)
    root_real = os.path.realpath(module_dir)
    try:
        fd = os.open(candidate, os.O_RDONLY)
    except FileNotFoundError:
        raise RevlError(
            filename, line,
            f"@{backend} host-body file {written!r} for extern `{decl_name}` "
            f"not found (resolved to {candidate})",
            hint="a body file is resolved relative to the declaring .rvl file's "
                 "directory (item 396)")
    except OSError as exc:
        raise RevlError(
            filename, line,
            f"cannot open @{backend} host-body file {written!r} for extern "
            f"`{decl_name}` (resolved to {candidate}): {exc}")
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            raise RevlError(
                filename, line,
                f"@{backend} host-body file {written!r} for extern "
                f"`{decl_name}` is not a regular file (resolved to {candidate})")
        real_file = _real_path_of_fd(fd, candidate)
        if not _contained(real_file, root_real):
            raise RevlError(
                filename, line,
                f"@{backend} host-body file {written!r} for extern "
                f"`{decl_name}` resolves OUTSIDE the declaring module's "
                f"directory: {real_file} is not inside {root_real}",
                hint="a body file must sit inside the declaring .rvl file's own "
                     "directory tree; containment is checked on the real path "
                     "of the opened file, so a symlink escape is refused (item "
                     "396 jail). The one accepted residual is a hardlink inside "
                     "the tree to a file outside it, which needs write access "
                     "inside the tree to create.")
        data = _read_all(fd)
    finally:
        os.close(fd)
    text = data.decode("utf-8")
    return normalize_body(text), hashlib.sha256(data).hexdigest()


def read_body_file_memory(sources: dict[str, str], module_dir: str,
                          written: str, backend: str, decl_name: str,
                          filename: str, line: int) -> tuple[str, str]:
    """Resolve a body file for a VIRTUAL (in-memory) module through the
    `sources` map ONLY, with no disk fallback — the `compile_source` contract
    is that nothing is read from disk (design re-review F5). Same textual jail
    plus an abspath containment check (nothing is on disk to realpath)."""
    _reject_bad_written_path(written, backend, decl_name, filename, line)
    root_abs = os.path.abspath(module_dir)
    key = os.path.abspath(os.path.join(module_dir, written))
    if not _contained(key, root_abs):
        raise RevlError(
            filename, line,
            f"@{backend} host-body file {written!r} for extern `{decl_name}` "
            f"escapes the declaring module's directory ({key} is not inside "
            f"{root_abs})",
            hint="item 396 jail")
    if key not in sources:
        raise RevlError(
            filename, line,
            f"@{backend} host-body file {written!r} for extern `{decl_name}` is "
            f"not in the in-memory sources map (resolved key {key})",
            hint="an in-memory compile reads nothing from disk, so a body file "
                 "must be supplied through the same `modules=`/`sources=` map as "
                 "the module that references it (item 396 re-review F5)")
    data = sources[key].encode("utf-8")
    return normalize_body(data.decode("utf-8")), hashlib.sha256(data).hexdigest()


def resolve_body_files(program, module_dir: str, sources: dict[str, str],
                       is_virtual: bool) -> None:
    """Replace every `HostBodyFile` node in `program`'s externs with a resolved
    `HostBody` carrying the spliced (normalized) text plus provenance (the
    written path and the sha256 of the raw file bytes). A virtual module
    resolves through `sources` only; a real module reads from disk under the
    jail. After this runs, nothing downstream ever sees a `HostBodyFile`."""
    from .parser import HostBody, HostBodyFile

    for ext in program.externs:
        for index, body in enumerate(ext.bodies):
            if not isinstance(body, HostBodyFile):
                continue
            if is_virtual:
                text, digest = read_body_file_memory(
                    sources, module_dir, body.path, body.backend, ext.name,
                    program.filename, body.line)
            else:
                text, digest = read_body_file_disk(
                    module_dir, body.path, body.backend, ext.name,
                    program.filename, body.line)
            ext.bodies[index] = HostBody(
                body.backend, text, body.line,
                source_path=body.path, sha256=digest)


def program_has_body_file(program) -> bool:
    """True if any extern in `program` still carries an unresolved
    `HostBodyFile` (used by the loaderless `compile_source` refusal)."""
    from .parser import HostBodyFile
    return any(isinstance(body, HostBodyFile)
               for ext in program.externs for body in ext.bodies)
