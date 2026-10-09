"""The packaged stdlib, as something `revl_source` can enumerate (issue #2173).

`stdlib/` ships in the wheel (``pyproject.toml``, ``revl._paths.stdlib_root()``),
but no MCP verb could name a module of it: `revl_source` read the session's own
buffers, so a stdlib declaration was reachable only through a `use` that had
already pulled it in. The vocabulary an author starts from — the modules and
their exported names, plus the base type surface the checker admits — was
therefore unanswerable without already knowing the symbol that answers it.

Two things are built here, on demand, and neither is ever stored on the
session:

* every ``stdlib/*.rvl`` module as a ``("dependency", <path>)`` buffer — the
  same read-only shape a `use` puts in the working set — so the symbol model
  resolves ``str.rvl:trim`` and a bare ``list_sort`` with no new resolution
  code, and `revl_source` reaches the stdlib the way it already reaches a
  declaration pulled in by `use` (issue #1779);
* a ``builtin`` buffer holding the base type surface (``Str``, ``List``,
  ``Map``, ...) as ``typecheck._BUILTIN_SIG`` declares it. Those methods are
  built into the language and no file declares them, so the surface is
  synthesized from the table the checker admits a builtin call against — which
  is what makes ``Str.concat`` addressable and what keeps this answer from
  drifting from what compiles.
"""

from __future__ import annotations

from pathlib import Path

from .. import _paths
from ..typecheck import _BUILTIN_SIG, _SIZED_HEADS
from . import symbols as _symbols
from .persist import (ORIGIN_DEPENDENCIES, ORIGIN_FILES, ORIGIN_MODULES,
                      ORIGIN_SOURCE)

#: The buffer key the base type surface is read under. Not a path: the base
#: surface is not a file on disk, so it has no `<module>.rvl` to be named by.
BASE_BUFFER = "builtin"

#: How a builtin method's receiver type is written, per receiver head.
_SELF = {"Str": "Str", "Bytes": "Bytes", "List": "List[T]", "Map": "Map[Str, V]",
         "Int": "Int", "Int32": "Int32", "Float": "Float", "Value": "Value"}

#: How the `_BUILTIN_SIG` placeholders resolve on a receiver: `@self` is the
#: receiver itself, `@elem` the element a container holds, `@member` what
#: membership is asked of (`indexOf`).
_PLACEHOLDER = {
    "@self": _SELF,
    "@elem": {"List": "T", "Map": "V"},
    "@member": {"Str": "Str", "Bytes": "Int", "List": "T"},
}

#: The receiver heads, in the order the surface is written out.
_HEADS = ("Str", "Bytes", "List", "Map", "Int", "Int32", "Float", "Value")


def _rows(head: str):
    """The `_BUILTIN_SIG` rows a `head` receiver admits, as
    ``(name, (family, params, returns))``.

    A row is either one signature for a family — `"sized"` covering the three
    sized receivers, per `typecheck._SIZED_HEADS` — or a table keyed by the
    heads that take it (`to_int`, `keys`, `to_str`)."""
    for name, signature in _BUILTIN_SIG.items():
        if isinstance(signature, dict):
            if head in signature:
                yield name, signature[head]
            continue
        family, params, returns = signature
        if head in (_SIZED_HEADS if family == "sized" else {family}):
            yield name, signature


def _substitute(type_name: str, head: str) -> str:
    """A `_BUILTIN_SIG` type with `@self`/`@elem`/`@member` resolved on `head`."""
    for placeholder, table in _PLACEHOLDER.items():
        if placeholder in type_name:
            type_name = type_name.replace(placeholder, table[head])
    return type_name


def _signature(name: str, params, returns, head: str) -> str:
    """One builtin method as a declaration line. Parameters are positional and
    the table names none, so they are `a0`, `a1`, ... — the call is positional
    too."""
    args = ", ".join(f"a{index}: {_substitute(param, head)}"
                     for index, param in enumerate(params))
    return f"fn {name}({args}) -> {_substitute(returns, head)}"


def _head_surface(head: str) -> str:
    """One base type's method surface, as a `service` declaration writes it.

    `service` because it is the one declaration form with a method surface and
    no body, and because the nested-symbol reader (`revl.nested`) is what
    resolves `Str.concat` through it. The text is not source: no file declares
    it and nothing can `use` it, which the header says."""
    lines = [f"service {head} {{"]
    for name, (_family, params, returns) in _rows(head):
        lines.append("  " + _signature(name, params, returns, head))
    lines.append("}")
    return "\n".join(lines)


def base_surface() -> str:
    """The base type surface as one read-only buffer of text."""
    header = ("// The base type surface the checker admits — the methods built "
              "into the language\n"
              "// itself, not declared by any file (`docs/stdlib-2.0.md`, "
              "§The surface). Read it\n"
              "// as the `builtin` buffer; nothing can `use` it.\n")
    return header + "\n\n".join(_head_surface(head) for head in _HEADS) + "\n"


#: How the symbol model refuses a name nothing declares. `revl_source`'s own
#: tests pin this wording (`tests/test_mcp_source_2031.py`), so it is a
#: contract to match on rather than a message to guess at.
_NOT_DECLARED = "no top-level declaration matches"


def _module_paths() -> list[Path]:
    """Every ``stdlib/*.rvl`` module, sorted by module name."""
    root = _paths.stdlib_root()
    return sorted(root.glob("*.rvl")) if root.is_dir() else []


def _texts() -> dict[str, str]:
    """The stdlib as ``{buffer key: text}``, base surface last."""
    out = {str(path): path.read_text(encoding="utf-8") for path in _module_paths()}
    out[BASE_BUFFER] = base_surface()
    return out


def working_set() -> dict:
    """The stdlib as a read-only working set for `revl.symbols`.

    The modules are `dependencies` — the buffer kind a `use` fills — so a
    `<buffer>:Name` prefix resolves by module path or basename and a bare name
    resolves against all of them. Nothing here is writable: `_set_text` refuses
    a dependency buffer by name, and this working set never reaches the
    session.

    The four keys are the origin vocabulary itself, imported rather than
    re-spelled: `edit.py::compile_virtual` consumes this same mapping, so the
    names are declared once, in `persist` (issues #1690, #1285)."""
    return {ORIGIN_SOURCE: None, ORIGIN_FILES: [], ORIGIN_MODULES: {},
            ORIGIN_DEPENDENCIES: _texts()}


def _builtin_symbols() -> dict[str, str]:
    """The base surface as ``{name: what it is}``: each head, and its methods."""
    out: dict[str, str] = {}
    for head in _HEADS:
        out[head] = "builtin type"
        for name, (_family, params, returns) in _rows(head):
            out[f"{head}.{name}"] = _signature(name, params, returns, head)
    return out


def read(vs: dict, symbol: str, *, deps: bool = False,
         comments: bool = True) -> dict:
    """Read `symbol` out of the stdlib working set `vs`.

    A name the stdlib does not declare is refused with what the stdlib IS:
    with nothing loaded, the generic refusal ("nothing is loaded: load a
    composition, or pass `files` or `source`") sends the caller back to the
    search this branch exists to end (issue #2173). Every other refusal — an
    ambiguous name, a `<buffer>` that is no module, a member that does not
    exist — is the symbol model's own, unchanged."""
    try:
        return _symbols.read(vs, symbol, deps=deps, comments=comments)
    except _symbols.SymbolError as error:
        if not str(error).startswith(_NOT_DECLARED):
            raise
        raise _symbols.SymbolError(unknown_symbol(symbol)) from None


def catalogue() -> dict:
    """What the packaged stdlib declares: the modules and their symbols, and
    the base type surface (issue #2173).

    The symbols come from `revl.symbols.declarations` — the same function
    `revl_source` resolves a name with — so every name listed here is a name
    the reader can be given, and the two cannot drift."""
    modules = []
    for path in _module_paths():
        name = path.stem
        text = path.read_text(encoding="utf-8")
        modules.append({
            "module": name,
            "path": f"stdlib/{name}.rvl",
            "packaged": True,
            "symbols": {decl.name: decl.kind
                        for decl in _symbols.declarations(text, str(path))},
        })
    return {
        "kind": "stdlib",
        "root": str(_paths.stdlib_root()),
        "packaged": True,
        "modules": modules,
        "builtin": {"buffer": BASE_BUFFER, "symbols": _builtin_symbols()},
        "hint": ("read one with `symbol`: a bare name when it is unique "
                 "(`list_sort`), else qualified by module or base type "
                 "(`str.rvl:trim`, `builtin:Str.concat`)."),
    }


def unknown_symbol(symbol: str) -> str:
    """The refusal for a `symbol` the stdlib does not declare: what the stdlib
    is, and how to see all of it. Nothing is loaded, so the old answer
    (`nothing is loaded: ... pass files or source`) would leave the caller
    where it started."""
    root = _paths.stdlib_root()
    return (f"no top-level declaration matches `{symbol}` in the packaged "
            f"stdlib ({root}): {len(_module_paths())} modules, and the base "
            f"type surface as `{BASE_BUFFER}`. Call `revl_source` with no "
            f"`symbol` to list what they declare, or pass `files`/`source` to "
            f"read a composition's own.")
