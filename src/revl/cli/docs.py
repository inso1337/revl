"""`revl docs gen` / `revl docs check` -- managed behavior comment blocks.

`gen` writes or refreshes the IR-derived block above every component in the
given sources and prints how many it wrote; `check` refuses, by name, a block
whose digest no longer matches the IR and passes when every block is current.
The block contract lives in `revl.behavior_blocks`; this module only loads the
composition, groups its components by the file they were declared in and
applies the edits.

Both verbs take the same file list `revl compile` takes, because a component
file alone may not name the services it requires; the composition is compiled
once and the components are written back to the roots they came from. A
directory is not a file list: it is refused with exit 2 rather than walked,
because the `.rvl` files under one directory are not necessarily one
composition (two of them may declare the same component).

The files are the user's sources, so they are read and written without newline
translation (a CRLF file stays CRLF) and a rewrite goes through a temporary file
in the same directory and `os.replace`, never a truncate-then-write that a
crash could leave half done.
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

from ..compiler import compile_files
from ..errors import RevlError


def _components_by_source(ir: dict) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for component in ir.get("components") or ():
        source = component.get("source")
        if not source:
            continue
        grouped.setdefault(str(Path(source).resolve()), []).append(component)
    return grouped


def _load(files: list[str]):
    from .. import behavior_blocks  # noqa: PLC0415
    from ..boundary import _boundary  # noqa: PLC0415 — one authoritative walk

    ir = compile_files(files)
    templates = set((ir.get("manifest") or {}).get("templates") or ())
    surfaces = _boundary(ir)
    reaches = behavior_blocks.reached_declarations(ir)
    return _components_by_source(ir), templates, surfaces, reaches


def _read(path: Path) -> str:
    """The file's text exactly as stored: no newline translation."""
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def _write_atomically(path: Path, text: str) -> None:
    """Replace `path` with `text` through a temporary file and `os.replace`.

    The temporary file lives in the target's own directory (a rename across
    filesystems is not atomic) and takes the target's permission bits. A
    symlinked source is written through to what it points at, as an editor
    would, instead of being replaced by a regular file.
    """
    target = Path(os.path.realpath(path))
    mode = stat.S_IMODE(os.stat(target).st_mode)
    handle, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                         dir=target.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _targets(files: list[str], grouped: dict[str, list[dict]]):
    """Yield (file argument, components) for each distinct argument.

    A file that declares no component is still visited: it may carry an
    orphaned block (a component that was deleted, or a marker pair moved above
    a service), and `check` must refuse that rather than skip the file.
    """
    seen: set[str] = set()
    for argument in files:
        key = str(Path(argument).resolve())
        if key in seen:
            continue
        seen.add(key)
        yield argument, grouped.get(key, [])


def _generate(files: list[str], grouped: dict[str, list[dict]],
              templates: set[str], surfaces: dict[str, dict],
              reaches: dict[str, dict]) -> int:
    from .. import behavior_blocks  # noqa: PLC0415

    written = 0
    touched = 0
    for argument, components in _targets(files, grouped):
        path = Path(argument)
        try:
            text = _read(path)
        except (OSError, UnicodeDecodeError) as error:
            print(f"error: cannot read {argument}: {error}", file=sys.stderr)
            return 1
        try:
            new_text, count = behavior_blocks.generate(
                text, str(path), components, templates, surfaces, reaches)
        except behavior_blocks.BehaviorBlockError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        if new_text != text:
            try:
                _write_atomically(path, new_text)
            except OSError as error:
                print(f"error: cannot write {argument}: {error}", file=sys.stderr)
                return 1
            touched += 1
        written += count
    if written:
        print(f"wrote {written} behavior block(s) in {touched} file(s)")
    else:
        print("behavior blocks are up to date")
    return 0


def _check(files: list[str], grouped: dict[str, list[dict]],
           templates: set[str], surfaces: dict[str, dict],
           reaches: dict[str, dict]) -> int:
    from .. import behavior_blocks  # noqa: PLC0415

    failed = 0
    for argument, components in _targets(files, grouped):
        path = Path(argument)
        try:
            text = _read(path)
        except (OSError, UnicodeDecodeError) as error:
            print(f"error: cannot read {argument}: {error}", file=sys.stderr)
            return 1
        problems = behavior_blocks.check(
            text, str(path), components, templates, surfaces, reaches)
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        failed += len(problems)
    return 1 if failed else 0


def _run_docs(args) -> int:
    for argument in args.files:
        if Path(argument).is_dir():
            print(f"error: {argument} is a directory; name the .rvl files of the "
                  "composition", file=sys.stderr)
            return 2
    try:
        grouped, templates, surfaces, reaches = _load(args.files)
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.docs_command == "gen":
        return _generate(args.files, grouped, templates, surfaces, reaches)
    return _check(args.files, grouped, templates, surfaces, reaches)
