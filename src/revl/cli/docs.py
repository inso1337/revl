"""`revl docs gen` / `revl docs check` -- managed behavior comment blocks.

`gen` writes or refreshes the IR-derived block above every component in the
given sources and prints how many it wrote; `check` refuses, by name, a block
whose digest no longer matches the IR and passes when every block is current.
The block contract lives in `revl.behavior_blocks`; this module only loads the
composition, groups its components by the file they were declared in and
applies the edits.

Both verbs take the same file list `revl compile` takes, because a component
file alone may not name the services it requires; the composition is compiled
once and the components are written back to the roots they came from.
"""
from __future__ import annotations

import sys
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


def _load(files: list[str]) -> tuple[dict[str, list[dict]], set[str], dict[str, dict]]:
    from ..boundary import _boundary  # noqa: PLC0415 — one authoritative walk

    ir = compile_files(files)
    templates = set((ir.get("manifest") or {}).get("templates") or ())
    surfaces = _boundary(ir)
    return _components_by_source(ir), templates, surfaces


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
              templates: set[str], surfaces: dict[str, dict]) -> int:
    from .. import behavior_blocks  # noqa: PLC0415

    written = 0
    touched = 0
    for argument, components in _targets(files, grouped):
        path = Path(argument)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            print(f"error: cannot read {argument}: {error}", file=sys.stderr)
            return 1
        try:
            new_text, count = behavior_blocks.generate(
                text, str(path), components, templates, surfaces)
        except behavior_blocks.BehaviorBlockError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
            touched += 1
        written += count
    if written:
        print(f"wrote {written} behavior block(s) in {touched} file(s)")
    else:
        print("behavior blocks are up to date")
    return 0


def _check(files: list[str], grouped: dict[str, list[dict]],
           templates: set[str], surfaces: dict[str, dict]) -> int:
    from .. import behavior_blocks  # noqa: PLC0415

    failed = 0
    for argument, components in _targets(files, grouped):
        path = Path(argument)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            print(f"error: cannot read {argument}: {error}", file=sys.stderr)
            return 1
        problems = behavior_blocks.check(
            text, str(path), components, templates, surfaces)
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        failed += len(problems)
    return 1 if failed else 0


def _run_docs(args) -> int:
    try:
        grouped, templates, surfaces = _load(args.files)
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.docs_command == "gen":
        return _generate(args.files, grouped, templates, surfaces)
    return _check(args.files, grouped, templates, surfaces)
