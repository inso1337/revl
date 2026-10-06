"""The diagnostic codes this package can emit, derived from the source that emits them.

`diagnostics.GUARANTEES`/`FIXES` are hand-maintained tables, and `explain` is only
useful when they are closed over the codes that actually reach a diagnostic record.
Nothing enforced that closure, so it drifted (issue #2028): the tables knew 25
guarantee codes while the emitters named 34 more, and over half of realistic
faults came back from `explain` as a roster instead of an answer.

So the roster is *derived* here, by reading this package's own source, and
`tests/test_explain_coverage_2028.py` asserts the catalogue covers it. A new
`code="X"` raise site therefore fails a test until `diagnostics` knows `X` — the
direction the drift was missing.

Three emission shapes are collected, because the package uses all three:

* a `code="X"` keyword on a `RevlError` (the common case), including a `code=NAME`
  that resolves to a module-level string constant;
* a `"code": "X"` entry in a verdict/handshake dict (`gate.ProposeResult`, the MCP
  payloads) — these reach an agent's `revl_explain` call just as a raise does;
* a prose tag such as ``(G4)`` inside a message string, which is how a raise site
  routed through a helper that sets no `code=` still names its code.

Non-string `code=` values are skipped: `dev.py`/`run.py` use `code=3` for a *process
exit status*, which is not a diagnostic code. Docstrings are skipped too — they
quote codes as documentation, which is not an emission.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

# The prose form of a code, matching `diagnostics._TAG`. Kept in step by
# `test_explain_coverage_2028.py`, which asserts the two regexes agree.
TAG = re.compile(r"\((G[1-9]|A[1-9]|R[1-5]|T[1-9])\)")

_SRC = Path(__file__).resolve().parent


def _module_string_constants(tree: ast.Module) -> dict:
    """`NAME = "<str>"` at module level — the code-constant idiom."""
    out = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value.value
    return out


def _docstring_nodes(tree: ast.Module) -> set:
    """The `id()` of every string constant that is a docstring."""
    out = set()
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if isinstance(node, holders):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    out.add(id(value))
    return out


def _package_constants(sources: dict) -> dict:
    """Names defined once across the package, so an imported code constant resolves.

    A name assigned the same string in several modules is included; a name with two
    different values is dropped rather than guessed at.
    """
    seen: dict = {}
    for tree in sources.values():
        for name, value in _module_string_constants(tree).items():
            seen.setdefault(name, set()).add(value)
    return {name: next(iter(values)) for name, values in seen.items()
            if len(values) == 1}


def _sources(root: Path) -> dict:
    return {path: ast.parse(path.read_text(encoding="utf-8"))
            for path in sorted(root.rglob("*.py"))}


def _resolve(node: ast.AST, local: dict, package: dict):
    """The string a `code=`/`"code":` value names, or None if it is not a literal."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        if node.id in local:
            return local[node.id]
        return package.get(node.id)
    return None


def emitted_codes(root: str | Path | None = None) -> dict:
    """Every code this package can emit, as `{code: (site, ...)}`.

    `site` is `"<file>:<line>"`, with `"(tag)"` appended when the code was found as
    a prose tag rather than a `code=` value. Sites are sorted, so the value is a
    stable, reviewable witness for the code's presence.
    """
    root = Path(root) if root is not None else _SRC
    sources = _sources(root)
    package = _package_constants(sources)
    found: dict = {}

    def add(code, site):
        if code:
            found.setdefault(code, set()).add(site)

    for path, tree in sources.items():
        local = _module_string_constants(tree)
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "code":
                add(_resolve(node.value, local, package), f"{path.name}:{node.value.lineno}")
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (isinstance(key, ast.Constant) and key.value == "code"
                            and isinstance(value, (ast.Constant, ast.Name))):
                        add(_resolve(value, local, package), f"{path.name}:{value.lineno}")
            elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings):
                for tag in TAG.findall(node.value):
                    add(tag, f"{path.name}:{node.lineno}(tag)")

    return {code: tuple(sorted(sites)) for code, sites in sorted(found.items())}


def dynamic_code_sites(root: str | Path | None = None) -> list:
    """`code=` sites whose value is neither a literal nor a bare name.

    These are *propagations* — `code=error.code`, `code=getattr(...)` — which carry a
    code chosen elsewhere and so are already covered by that site. A site that
    *builds* a code (an f-string, a `join`) would be a code the roster cannot
    enumerate; `test_explain_coverage_2028.py` asserts there are none, so the derived
    roster stays a superset of what can reach the wire.
    """
    root = Path(root) if root is not None else _SRC
    out = []
    for path, tree in _sources(root).items():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.keyword) and node.arg == "code"):
                continue
            value = node.value
            if isinstance(value, ast.Constant):
                continue          # a literal code, or `code=3` (a process exit status)
            if isinstance(value, (ast.Name, ast.Attribute, ast.BoolOp, ast.Subscript)):
                continue          # a code chosen at another site, propagated
            if isinstance(value, ast.Call) and getattr(value.func, "id", None) == "getattr":
                continue
            out.append((f"{path.name}:{value.lineno}", ast.unparse(value)))
    return sorted(out)
