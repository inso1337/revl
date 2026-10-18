"""The export jail (issue #2524).

`revl_export` is the one MCP verb that writes to disk. The `files` exemption in
`server.compile_under_authoring` rested on the premise that bytes at a jailed
path were put there by a human, so a module the agent exported and then loaded
back by `files` compiled without the authoring profile. Under a closed
authoring trust this module keeps the two facts that make the exemption sound
again:

* WHERE an agent may write: inline exports land only inside the export
  directory, `<root>/.revl/export/`, and only as `.rvl` modules the exported
  program actually `use`s. A files-loaded composition writes back only to the
  `.rvl` files it loaded.
* WHAT revl wrote: every export is recorded, path and SHA-256, in
  `<root>/.revl/export/manifest.json`, together with the operator's own text of
  a file it wrote over. A file inside an export directory, or one whose bytes
  still match its manifest entry, is the agent's and compiles under the
  profile; the operator's original text, when there was one, is what the
  profile diffs against, so the operator's own declarations stay trusted.

Both facts live on disk, beside the files they describe, so they survive a
server restart and a change of `--root`. Changing the bytes of a written file
outside the export directory is the operator adopting it.

Separately, and under every authoring trust, an export may never replace a
file the server opened as operator configuration at serve time (the operator
profile, the boundary policy, provider modules, the restore snapshot, the
emergency-stop latch), whatever directory it sits in.
"""
from __future__ import annotations

import hashlib
import json
import os

EXPORT_DIR = os.path.join(".revl", "export")
MANIFEST = "manifest.json"
_EXPORT_PARTS = tuple(EXPORT_DIR.split(os.sep))

# realpaths of the files the server opened as operator configuration
_OPERATOR_FILES: set[str] = set()


class ExportRefused(Exception):
    """An export the jail refuses; nothing has been written."""


def _real(path: str) -> str:
    return os.path.realpath(os.path.abspath(path))


def protect_operator_files(paths) -> None:
    """Record files the server opened as operator configuration, so no export
    can ever replace one (operator-side wiring; no verb reaches it)."""
    for path in paths:
        if path:
            _OPERATOR_FILES.add(_real(path))


def operator_files() -> frozenset[str]:
    return frozenset(_OPERATOR_FILES)


def export_dirs(roots) -> tuple[str, ...]:
    """The export directory of each sanctioned root."""
    return tuple(os.path.join(root, EXPORT_DIR) for root in roots)


def in_export_area(path: str) -> bool:
    """Whether `path` resolves inside any `.revl/export` directory, whichever
    root it was written under."""
    parts = _real(path).split(os.sep)
    width = len(_EXPORT_PARTS)
    return any(tuple(parts[i:i + width]) == _EXPORT_PARTS
               for i in range(len(parts) - width + 1))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def _manifest_entries(manifest: str) -> dict:
    text = _read_text(manifest)
    if text is None:
        return {}
    try:
        files = json.loads(text).get("files")
    except (ValueError, AttributeError):
        return {}
    return files if isinstance(files, dict) else {}


def _manifest_entry(real: str) -> dict | None:
    """The manifest entry for `real` in the nearest ancestor that records it."""
    directory = os.path.dirname(real)
    while True:
        entries = _manifest_entries(os.path.join(directory, EXPORT_DIR, MANIFEST))
        entry = entries.get(os.path.relpath(real, directory))
        if isinstance(entry, dict):
            return entry
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def _matches(entry: dict | None, text: str | None) -> bool:
    return (entry is not None and text is not None
            and entry.get("sha256") == _sha256(text))


def authorship(path: str) -> tuple[bool, str | None]:
    """`(written, operator_text)`: whether an export wrote the bytes now at
    `path`, and the operator's own text of that file when there was one."""
    real = _real(path)
    if in_export_area(real):
        return True, None
    entry = _manifest_entry(real)
    if not _matches(entry, _read_text(real)):
        return False, None
    operator = entry.get("operator")
    return True, operator if isinstance(operator, str) else None


def export_written(paths) -> dict[str, str]:
    """`{abspath: text}` of every path in `paths` an export wrote."""
    out = {}
    for path in paths:
        if authorship(path)[0]:
            text = _read_text(path)
            if text is not None:
                out[os.path.abspath(path)] = text
    return out


def revl_wrote(path: str) -> bool:
    """Whether the bytes at `path` are the bytes an export recorded there."""
    real = _real(path)
    return _matches(_manifest_entry(real), _read_text(real))


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


def _inside(path: str, directories) -> bool:
    real = _real(path)
    for directory in directories:
        base = _real(directory)
        if real == base or real.startswith(base.rstrip(os.sep) + os.sep):
            return True
    return False


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
    """Refuse writing a files-loaded composition back to a non-`.rvl` file."""
    not_rvl = [p for p in paths if not p.endswith(".rvl")]
    if not_rvl:
        raise ExportRefused(
            f"refused: {', '.join(not_rvl)} is not a .rvl file; under a closed "
            "authoring trust revl_export writes only revl modules; nothing was "
            "written")


def check_foreign(existing) -> None:
    """Refuse overwriting a file whose bytes revl did not write."""
    foreign = [p for p in existing if not revl_wrote(p)]
    if foreign:
        raise ExportRefused(
            f"refused: {', '.join(foreign)} was not written by revl_export (or "
            "has changed since), so it is not replaced, even with `overwrite: "
            "true`; nothing was written")


def check_operator_files(paths) -> None:
    """Refuse any destination that is a file the server opened as operator
    configuration, under every authoring trust."""
    hit = [p for p in paths if _real(p) in _OPERATOR_FILES]
    if hit:
        raise ExportRefused(
            f"refused: {', '.join(hit)} is an operator configuration file this "
            "server opened at start; revl_export never replaces one; nothing "
            "was written")


def _root_of(real: str, roots) -> str | None:
    for root in roots:
        if _inside(real, (root,)):
            return _real(root)
    return None


def manifest_writes(targets, roots) -> list[tuple[str, str]]:
    """The manifest files to write BEFORE `targets` land, as `(path, text)`.

    Each `.rvl` target is recorded by SHA-256 under its root's manifest, with
    the operator's own text of the file it replaces: kept from the earlier
    entry while the file still holds what revl wrote, else the bytes on disk
    now (`None` inside an export directory, or for a new file)."""
    manifests: dict[str, dict] = {}
    for path, text in targets:
        real = _real(path)
        root = _root_of(real, roots)
        if root is None or not path.endswith(".rvl"):
            continue
        manifest = os.path.join(root, EXPORT_DIR, MANIFEST)
        entries = manifests.setdefault(manifest, dict(_manifest_entries(manifest)))
        key = os.path.relpath(real, root)
        entries[key] = {"sha256": _sha256(text),
                        "operator": _operator_baseline(real, entries.get(key))}
    return [(manifest, json.dumps({"files": entries}, indent=2, sort_keys=True) + "\n")
            for manifest, entries in manifests.items()]


def _operator_baseline(real: str, prior: dict | None) -> str | None:
    if in_export_area(real):
        return None
    current = _read_text(real)
    if _matches(prior, current):
        operator = prior.get("operator")
        return operator if isinstance(operator, str) else None
    return current
