"""The export jail (issue #2524).

`revl_export` is the one MCP verb that writes to disk. The `files` exemption in
`server.compile_under_authoring` rested on the premise that bytes at a jailed
path were put there by a human, so a module the agent exported and then loaded
back by `files` compiled without the authoring profile. The rule that makes
the exemption sound again is one sentence: an export writes source only
under `<root>/.revl/export/`, and never over a file revl did not write. (The
knowledge sidecars `with_knowledge` adds are new JSON files under
`.revl/knowledge/`, created exclusively and never compiled; issue #2534.)

The rule holds under every authoring trust. Trust decides only how a file
under the export directory compiles, never where an export writes or what it
may replace: an author over the transport is an author whatever the trust
flag, and `revl_export` is reached only over the transport.

* WHERE: an inline composition is exported into the export directory. A
  files-loaded composition writes each changed file to its copy there, at the
  same path relative to the root (a file already inside the export directory
  is its own copy), never over the operator's file. Adopting an exported file
  is the operator's act: copying it out.
* WHAT ELSE: an existing destination is replaced only when its bytes are the
  bytes an export recorded for that file in its root's own manifest,
  `<root>/.revl/export/manifest.json` (path and SHA-256 of the exact bytes
  written); no other manifest is read. A missing, unreadable or malformed
  manifest records nothing, so it refuses the overwrite: it fails closed. A
  manifest is never an export target, under any spelling.
* TRUST: a file inside any `.revl/export/` directory is the agent's, by path,
  always. Named in `files` or reached by `use`, its text compiles under the
  authoring profile with no operator text to diff against, exactly as if it
  had been sent inline. The manifest never decides who wrote a file. Every
  other compile applies the same rule to a file on disk there, and every
  `revl` command does unless it is given `--trust-exported` (`CliRule`, issue
  #2547).

An export also writes only `.rvl` modules (an inline one, only those the
program reaches by `use`), a destination may not be a symbolic link, and a
rendered note may not carry a line break other than a newline.

An export never creates or replaces a file the server opened as operator
configuration at serve time (the operator profile, the boundary policy,
provider modules, the restore snapshot, TLS files) or an emergency-stop latch,
whichever spelling or directory names it. Names are compared NFC-normalised
and case-folded.
"""
from __future__ import annotations

import hashlib
import json
import os

# the path rule lives outside `revl.mcp`, so compiling never imports the
# server (issue #1780); the jail and the compiler ask the same functions
from ..export_area import EXPORT_DIR, in_export_area
# re-exported: the server and its callers name them through this module
from ..export_area import CliRule  # noqa: F401
from ..export_area import cli_rule  # noqa: F401
from ..export_area import exported_texts  # noqa: F401
from ..export_area import server_door  # noqa: F401
from ..export_area import fold as _fold
from ..export_area import read_exact as _read_exact
from ..export_area import real as _real
from ..export_area import under_export_dir as _under_export_dir

MANIFEST = "manifest.json"

# the files the server opened as operator configuration: (st_dev, st_ino) of
# those that exist, and the case-folded realpath of every one
_OPERATOR_IDS: set[tuple[int, int]] = set()
_OPERATOR_NAMES: set[str] = set()

# characters a reader may take as a line break although the revl lexer, which
# ends a `//` comment only at "\n", does not; and the byte-order mark
_LINE_BREAKS = frozenset("\r\x0b\x0c\x1c\x1d\x1e\x85  ﻿")


class ExportRefused(Exception):
    """An export the jail refuses; nothing has been written."""


class ExportIncomplete(OSError):
    """An export that stopped part way (issue #2534): a write failed after
    others had landed. The message names the path it stopped at and every
    file written before it, saying which of them its root's manifest
    records."""

    def __init__(self, path: str, error: BaseException, written,
                 recorded=()) -> None:
        kept = set(recorded)
        recorded = [p for p in written if p in kept]
        unrecorded = [p for p in written if p not in kept]
        parts = []
        if recorded:
            parts.append("written and recorded in the export manifest: "
                         + ", ".join(recorded))
        if unrecorded:
            parts.append("written, not recorded: " + ", ".join(unrecorded))
        landed = "; ".join(parts) or "nothing was written before it"
        super().__init__(f"export stopped at {path}: {error}; {landed}")
        self.path = path
        self.written = list(written)
        self.recorded = recorded


def recorded_paths(paths, roots) -> list[str]:
    """The `paths` a manifest records once written: those inside a root."""
    return [p for p in paths if _root_of(p, roots) is not None]


def _identity(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def protect_operator_files(paths) -> None:
    """Record files the server opened as operator configuration, so no export
    can ever replace one (operator-side wiring; no verb reaches it)."""
    for path in paths:
        if not path:
            continue
        identity = _identity(path)
        if identity is not None:
            _OPERATOR_IDS.add(identity)
        _OPERATOR_NAMES.add(_fold(_real(path)))


def reset_operator_files() -> None:
    """Forget every protected file (test and embedder wiring)."""
    _OPERATOR_IDS.clear()
    _OPERATOR_NAMES.clear()


def _is_operator_file(path: str) -> bool:
    identity = _identity(path)
    if identity is not None and identity in _OPERATOR_IDS:
        return True
    return _fold(_real(path)) in _OPERATOR_NAMES


def export_dirs(roots) -> tuple[str, ...]:
    """The export directory of each sanctioned root."""
    return tuple(os.path.join(root, EXPORT_DIR) for root in roots)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _inside(path: str, directories) -> bool:
    real = _real(path)
    for directory in directories:
        base = _real(directory)
        if real == base or real.startswith(base.rstrip(os.sep) + os.sep):
            return True
    return False


def _root_of(path: str, roots) -> str | None:
    for root in roots:
        if _inside(path, (root,)):
            return _real(root)
    return None


def _rel_in_root(path: str, root: str) -> str:
    """`path` relative to `root`, for a path `_root_of` has already placed
    inside `root`. Works on the resolved spelling only: no `..`, no other
    drive, and no fallback to an absolute path, which joined under the export
    directory would leave it."""
    real = _real(path)
    base = _real(root)
    if real == base:
        return os.curdir
    prefix = base.rstrip(os.sep) + os.sep
    if not real.startswith(prefix):
        raise ValueError(f"{path!r} is not inside {root!r}")
    return real[len(prefix):]


def export_copy(path: str, roots) -> str:
    """Where an export writes the held text of the loaded file `path`: the
    file itself when it is inside an export directory, else its copy at the
    same path under its root's export directory. Decided by where `path`
    resolves only, never by its spelling: a link inside the export directory
    to an operator file is not a place an export writes through."""
    if _under_export_dir(_real(path)):
        return path
    root = _root_of(path, roots)
    if root is None:
        return path
    return os.path.join(root, EXPORT_DIR, _rel_in_root(path, root))


# ------------------------------------------------------------------ manifest


def _manifest_entries(manifest: str) -> dict:
    """`{relpath: sha256}` from a manifest, or nothing at all unless it is
    exactly `{"files": {str: {"sha256": str}}}`: whatever else it holds
    records nothing, so every overwrite it would vouch for is refused."""
    text = _read_exact(manifest)
    if text is None:
        return {}
    try:
        document = json.loads(text)
    except (ValueError, RecursionError):
        return {}
    files = document.get("files") if isinstance(document, dict) else None
    if not isinstance(files, dict) or set(document) != {"files"}:
        return {}
    entries = {}
    for key, entry in files.items():
        if not (isinstance(entry, dict) and set(entry) == {"sha256"}
                and isinstance(entry["sha256"], str)):
            return {}
        entries[key] = entry["sha256"]
    return entries


def _manifest_of(root: str) -> str:
    return os.path.join(root, EXPORT_DIR, MANIFEST)


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def revl_wrote(path: str, roots) -> bool:
    """Whether the bytes at `path` are bytes an export recorded for that very
    file in the manifest of the root it lies in, and no other: a manifest
    anywhere else is never read. Matched by file identity, so a case or link
    alias is the same file; a missing manifest matches nothing."""
    real = _real(path)
    root = _root_of(real, roots)
    text = _read_exact(real)
    if root is None or text is None:
        return False
    digest = _sha256(text)
    return any(sha == digest and _same_file(os.path.join(root, key), real)
               for key, sha in _manifest_entries(_manifest_of(root)).items())


def manifest_writes(targets, roots) -> list[tuple[str, str]]:
    """The manifest files to write with `targets`, as `(path, text)`: each
    target recorded by the SHA-256 of the exact text written, under the
    manifest of the root it lies in."""
    manifests: dict[str, dict] = {}
    for path, text in targets:
        root = _root_of(path, roots)
        if root is None:
            continue
        manifest = _manifest_of(root)
        entries = manifests.setdefault(manifest, dict(_manifest_entries(manifest)))
        entries[_rel_in_root(path, root)] = _sha256(text)
    return [(manifest, json.dumps(
        {"files": {key: {"sha256": sha} for key, sha in entries.items()}},
        indent=2, sort_keys=True) + "\n") for manifest, entries in manifests.items()]


# ------------------------------------------------------------------ checks


def reached_modules(source: str, modules: dict) -> set[str]:
    """The `modules` keys the inline `source` reaches through `use`, resolved
    the way the compiler resolves an in-memory module (relative to the
    importing file, the candidate sitting in the working directory)."""
    from ..parser import Parser  # noqa: PLC0415

    by_path = {os.path.abspath(key): key for key in modules}
    reached: set[str] = set()
    pending = [(os.path.abspath("<candidate>.rvl"), source)]
    while pending:
        importer, text = pending.pop()
        try:
            program = Parser(text, importer).parse()
        except Exception:  # noqa: BLE001: unparseable text reaches nothing
            continue
        for use in program.uses:
            target = os.path.abspath(os.path.join(os.path.dirname(importer),
                                                  use.path))
            key = by_path.get(target)
            if key is not None and key not in reached:
                reached.add(key)
                pending.append((target, modules[key]))
    return reached


def check_inline(path: str, modules: dict, roots) -> None:
    """Refuse an inline export whose file or module would land outside an
    export directory, or that names a module that is not a `.rvl` file."""
    not_rvl = sorted(key for key in modules if not key.endswith(".rvl"))
    if not path.endswith(".rvl") or not_rvl:
        names = ", ".join(([path] if not path.endswith(".rvl") else []) + not_rvl)
        raise ExportRefused(
            f"refused: {names} is not a .rvl module; revl_export writes only "
            "revl modules; nothing was written")
    base_dir = os.path.dirname(os.path.abspath(path))
    targets = [os.path.abspath(path)] + [
        os.path.normpath(os.path.join(base_dir, key)) for key in modules]
    allowed = export_dirs(roots)
    outside = [p for p in targets if not _inside(p, allowed)]
    if outside:
        raise ExportRefused(
            f"refused: {', '.join(outside)} is outside the export directory "
            f"({', '.join(allowed)}); an inline composition is exported "
            "there; nothing was written")


def check_write_back(paths) -> None:
    """Refuse writing a files-loaded composition back as a non-`.rvl` file."""
    not_rvl = [p for p in paths if not p.endswith(".rvl")]
    if not_rvl:
        raise ExportRefused(
            f"refused: {', '.join(not_rvl)} is not a .rvl file; revl_export "
            "writes only revl modules; nothing was written")


def check_note_bodies(notes) -> None:
    """Refuse rendering a note whose body holds a line break other than a
    newline, or a byte-order mark: rendered as a `//` comment it would end,
    for a reader, where the lexer that admitted it saw none."""
    bad = sorted({n["id"] for n in notes
                  if _LINE_BREAKS.intersection(str(n.get("body", "")))})
    if bad:
        raise ExportRefused(
            f"refused: note {', '.join(bad)} holds a carriage return, another "
            "line separator or a byte-order mark, which a rendered comment "
            "cannot carry; nothing was written")


def check_foreign(existing, roots) -> None:
    """Refuse overwriting a file whose bytes revl did not write."""
    foreign = [p for p in existing if not revl_wrote(p, roots)]
    if foreign:
        raise ExportRefused(
            f"refused: {', '.join(foreign)} was not written by revl_export (or "
            "has changed since), so it is not replaced, even with `overwrite: "
            "true`; nothing was written")


def check_not_linked(paths) -> None:
    """Refuse a destination that is a symbolic link: the write would replace
    the link with a file nothing recorded."""
    linked = [p for p in paths if os.path.islink(p)]
    if linked:
        raise ExportRefused(
            f"refused: {', '.join(linked)} is a symbolic link; revl_export "
            "writes only regular files; nothing was written")


def _is_latch(path: str, latches) -> bool:
    real = _fold(_real(path))
    return (real.endswith(".estop") or _fold(path).endswith(".estop")
            or any(real == _fold(_real(latch)) or _same_file(path, latch)
                   for latch in latches if latch))


def _is_manifest(path: str, roots) -> bool:
    """Whether `path` names an export manifest: any `manifest.json` inside an
    export directory, under any spelling, or a link to a root's manifest."""
    named = any(_fold(os.path.basename(p)) == MANIFEST and in_export_area(p)
                for p in (os.path.abspath(path), _real(path)))
    return named or any(_same_file(path, _manifest_of(root)) for root in roots)


def check_not_manifest(paths, roots) -> None:
    """Refuse any destination that is an export manifest: only `revl_export`
    itself records what it wrote, never an author's text."""
    hit = [p for p in paths if _is_manifest(p, roots)]
    if hit:
        raise ExportRefused(
            f"refused: {', '.join(hit)} is the export manifest, which only "
            "revl_export writes; nothing was written")


def check_operator_files(paths, latches=()) -> None:
    """Refuse any destination that is a file the server opened as operator
    configuration, or an emergency-stop latch (issue #2535: `latches` are the
    ones the live session acts on, and any `*.estop` file is one), under every
    authoring trust. A latch need not exist yet: creating one halts a session."""
    hit = [p for p in paths if _is_operator_file(p) or _is_latch(p, latches)]
    if hit:
        raise ExportRefused(
            f"refused: {', '.join(hit)} is an operator configuration file or an "
            "emergency-stop latch; revl_export never creates or replaces one; "
            "nothing was written")
