"""Pipeline: source files -> parse -> check/lower -> link -> IR document."""

from __future__ import annotations

from .errors import RevlError
from .lower import check_and_lower
from .parser import Parser, Program, parse_file


def compile_source(source: str, filename: str = "<string>") -> dict:
    return check_and_lower(Parser(source, filename).parse())


def compile_files(paths: list[str]) -> dict:
    """Compile a composition: all services and components across the files
    are checked and linked together (the composition manifest, DESIGN §4)."""
    merged = Program(filename=paths[0] if paths else "<none>")
    seen_services: dict[str, str] = {}
    seen_components: dict[str, str] = {}
    for path in paths:
        program = parse_file(path)
        for svc in program.services:
            if svc.name in seen_services:
                raise RevlError(path, svc.line,
                                f"duplicate service `{svc.name}` (also declared in {seen_services[svc.name]})")
            seen_services[svc.name] = path
            merged.services.append(svc)
        for comp in program.components:
            if comp.name in seen_components:
                raise RevlError(path, comp.line,
                                f"duplicate component `{comp.name}` (also declared in {seen_components[comp.name]})")
            seen_components[comp.name] = path
            merged.components.append(comp)
        merged.filename = path  # diagnostics from lowering name the declaring file
    return check_and_lower(merged)
