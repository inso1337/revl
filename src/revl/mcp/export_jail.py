"""The export jail (issue #2524).

`revl_export` is the one MCP verb that writes to disk. The `files` exemption in
`server.compile_under_authoring` rested on the premise that bytes at a jailed
path were put there by a human, so a module the agent exported and then loaded
back by `files` compiled without the authoring profile. The rule that makes
the exemption sound again is one sentence: an export writes only under
`<root>/.revl/export/`, and never over a file revl did not write.

* WHERE: an inline composition is exported into the export directory. A
  files-loaded composition writes each changed file to its copy there, at the
  same path relative to the root (a file already inside the export directory
  is its own copy), never over the operator's file. Adopting an exported file
  is the operator's act: copying it out.
* WHAT ELSE: an existing destination is replaced only when its bytes are the
  bytes an export recorded for that file in `<root>/.revl/export/manifest.json`
  (path and SHA-256 of the exact bytes written). A missing or unreadable
  manifest records nothing, so it refuses the overwrite: it fails closed.
* TRUST: a file inside any `.revl/export/` directory is the agent's, by path,
  always. Named in `files` or reached by `use`, its text compiles under the
  authoring profile with no operator text to diff against, exactly as if it
  had been sent inline. The manifest never decides who wrote a file.

Under a closed authoring trust an inline export also writes only `.rvl` modules
the program reaches by `use`, a destination may not be a symbolic link, and a
rendered note may not carry a line break other than a newline.

Under every trust, an export never replaces a file the server opened as
operator configuration at serve time (the operator profile, the boundary
policy, provider modules, the restore snapshot, TLS files, the emergency-stop
latch), whichever spelling or directory names it.
"""
from __future__ import annotations

import hashlib
import json
import os

EXPORT_DIR = os.path.join(".revl", "export")
MANIFEST = "manifest.json"
_EXPORT_PARTS = tuple(EXPORT_DIR.split(os.sep))

# the files the server opened as operator configuration: (st_dev, st_ino) of
# those that exist, and the case-folded realpath of every one
_OPERATOR_IDS: set[tuple[int, int]] = set()
_OPERATOR_NAMES: set[str] = set()

# characters a reader may take as a line break although the revl lexer, which
# ends a `//` comment only at "\n", does not; and the byte-order mark
_LINE_BREAKS = frozenset("\r\x0b\x0c\x1c\x1d\x1e\x85  ﻿")


class ExportRefused(Exception):
    """An export the jail refuses; nothing has been written."""


def _real(path: str) -> str:
    return os.path.realpath(os.path.abspath(path))


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
        _OPERATOR_NAMES.add(_real(path).casefold())


def reset_operator_files() -> None:
    """Forget every protected file (test and embedder wiring)."""
    _OPERATOR_IDS.clear()
    _OPERATOR_NAMES.clear()


def _is_operator_file(path: str) -> bool:
    identity = _identity(path)
    if identity is not None and identity in _OPERATOR_IDS:
        return True
    return _real(path).casefold() in _OPERATOR_NAMES


def export_dirs(roots) -> tuple[str, ...]:
    """The export directory of each sanctioned root."""
    return tuple(os.path.join(root, EXPORT_DIR) for root in roots)


def in_export_area(path: str) -> bool:
    """Whether `path` resolves inside any `.revl/export` directory, whichever
    root it was written under. Compared case-insensitively: on a filesystem
    that folds case, `.REVL/Export` IS the export directory, and on one that
    does not, counting it as one only ever profiles more."""
    parts = [part.casefold() for part in _real(path).split(os.sep)]
    width = len(_EXPORT_PARTS)
    return any(tuple(parts[i:i + width]) == _EXPORT_PARTS
               for i in range(len(parts) - width + 1))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_exact(path: str) -> str | None:
    """The file's text with no newline translation: the bytes, decoded."""
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def exported_texts(paths) -> dict[str, str]:
    """`{abspath: text}` of every path in `paths` inside an export directory:
    the files whose text is the agent's."""
    out = {}
    for path in paths:
        if in_export_area(path):
            text = _read_exact(path)
            if text is not None:
                out[os.path.abspath(path)] = text
    return out


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


def export_copy(path: str, roots) -> str:
    """Where an export writes the held text of the loaded file `path`: the
    file itself when it is inside an export directory, else its copy at the
    same path under its root's export directory."""
    if in_export_area(path):
        return path
    root = _root_of(path, roots)
    if root is None:
        return path
    return os.path.join(root, EXPORT_DIR, os.path.relpath(_real(path), root))


# ------------------------------------------------------------------ manifest


def _manifest_entries(manifest: str) -> dict:
    text = _read_exact(manifest)
    if text is None:
        return {}
    try:
        files = json.loads(text).get("files")
    except (ValueError, AttributeError):
        return {}
    return files if isinstance(files, dict) else {}


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def revl_wrote(path: str) -> bool:
    """Whether the bytes at `path` are bytes an export recorded for that very
    file, in any ancestor's manifest. Matched by file identity, so a case or
    link alias is the same file; a missing manifest matches nothing."""
    real = _real(path)
    text = _read_exact(real)
    if text is None:
        return False
    digest = _sha256(text)
    directory = os.path.dirname(real)
    while True:
        entries = _manifest_entries(os.path.join(directory, EXPORT_DIR, MANIFEST))
        for key, entry in entries.items():
            if (isinstance(entry, dict) and entry.get("sha256") == digest
                    and _same_file(os.path.join(directory, key), real)):
                return True
        parent = os.path.dirname(directory)
        if parent == directory:
            return False
        directory = parent


def manifest_writes(targets, roots) -> list[tuple[str, str]]:
    """The manifest files to write with `targets`, as `(path, text)`: each
    target recorded by the SHA-256 of the exact text written, under the
    manifest of the root it lies in."""
    manifests: dict[str, dict] = {}
    for path, text in targets:
        root = _root_of(path, roots)
        if root is None:
            continue
        manifest = os.path.join(root, EXPORT_DIR, MANIFEST)
        entries = manifests.setdefault(manifest, dict(_manifest_entries(manifest)))
        entries[os.path.relpath(_real(path), root)] = {"sha256": _sha256(text)}
    return [(manifest, json.dumps({"files": entries}, indent=2, sort_keys=True) + "\n")
            for manifest, entries in manifests.items()]


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
            f"refused: {names} is not a .rvl module; under a closed authoring "
            "trust revl_export writes only revl modules; nothing was written")
    base_dir = os.path.dirname(os.path.abspath(path))
    targets = [os.path.abspath(path)] + [
        os.path.normpath(os.path.join(base_dir, key)) for key in modules]
    allowed = export_dirs(roots)
    outside = [p for p in targets if not _inside(p, allowed)]
    if outside:
        raise ExportRefused(
            f"refused: {', '.join(outside)} is outside the export directory "
            f"({', '.join(allowed)}); under a closed authoring trust an inline "
            "composition is exported there; nothing was written")


def check_write_back(paths) -> None:
    """Refuse writing a files-loaded composition back as a non-`.rvl` file."""
    not_rvl = [p for p in paths if not p.endswith(".rvl")]
    if not_rvl:
        raise ExportRefused(
            f"refused: {', '.join(not_rvl)} is not a .rvl file; under a closed "
            "authoring trust revl_export writes only revl modules; nothing was "
            "written")


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


def check_foreign(existing) -> None:
    """Refuse overwriting a file whose bytes revl did not write."""
    foreign = [p for p in existing if not revl_wrote(p)]
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
            "writes only regular files under a closed authoring trust; nothing "
            "was written")


def _is_latch(path: str, latches) -> bool:
    real = _real(path).casefold()
    return (real.endswith(".estop")
            or any(real == _real(latch).casefold() or _same_file(path, latch)
                   for latch in latches if latch))


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
