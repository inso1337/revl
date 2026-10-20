"""Enumerate every compile call site under `src/revl` and what it names.

Issue #2563. The export-area trust rule (`export_area.CliRule`) decides
authorship from the NAME a compile is given: a real file under
`.revl/export/` is author-written, and a label that names no file is not
looked at at all. So the audit of the rule is an audit of the compile call
sites: which ones hand the compiler a real on-disk path, and which ones hand
it a synthetic label, a relative name, or text with no name.

This module is the mechanical pass that makes the audit complete rather than
remembered. It parses the tree (no imports, no execution), finds every call
to `compile_source` / `compile_files` (and the registry's `_compile_source`
wrapper), and prints one row per call: file, line, enclosing function, the
argument that names the text, and a first-pass classification:

  real      the argument's source text names an on-disk file (a path
            variable, a `str(...)`/`Path(...)`/`resolve()` result, an
            `os.path.join`, a `[str(p) for p in ...]` list)
  label     a string literal that cannot be a real path: `<...>` placeholder,
            a bare filename such as `component.rvl`, a `/__...` virtual label
  mixed     a list whose elements disagree (one literal and one expression)
  unknown   anything the pass cannot decide by reading the expression
            (a `compile_files` call with no paths argument)

The classification is a FIRST PASS to be reviewed by a person: `real` still
has to be a file at run time, and a `label` can be correct when the text is a
module constant or an editor buffer. The audit table in
`docs/label-compile-audit-2563.md` is the reviewed version.

Usage:
    python tools/label_compile_sites.py            # readable table
    python tools/label_compile_sites.py --json     # machine rows
    python tools/label_compile_sites.py --files ... # only these paths
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src", "revl")

# the compile entry points `compiler.py` defines, plus the registry wrapper
COMPILE = {"compile_source", "compile_files"}
WRAPPERS = {"_compile_source"}
LABEL_MARKERS = ("<", ">")


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _arg(node: ast.Call, position: int, keyword: str) -> ast.expr | None:
    if len(node.args) > position:
        return node.args[position]
    for kw in node.keywords:
        if kw.arg == keyword:
            return kw.value
    return None


def _literal_text(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _classify_label(text: str) -> str:
    if text.startswith("<") and text.endswith(">"):
        return "label"
    if "/__" in text or text.startswith("/__"):
        return "label"
    if os.sep in text:
        return "real" if os.path.isabs(text) else "label"
    return "label"


def _classify(node: ast.expr | None) -> tuple[str, str]:
    """`(class, rendered)` for the argument expression."""
    if node is None:
        # `compile_source(source)` names the text by the default `<string>`,
        # which is a label like any other; `compile_files()` with no paths
        # cannot be classified at all.
        return "label", "(default <string>)"
    if isinstance(node, (ast.List, ast.Tuple)):
        parts = [_classify(item)[0] for item in node.elts]
        rendered = "[" + ", ".join(_render(i) for i in node.elts) + "]"
        if not parts:
            return "unknown", rendered
        kinds = set(parts)
        if len(kinds) == 1:
            return parts[0], rendered
        return "mixed", rendered
    text = _literal_text(node)
    if text is not None:
        return _classify_label(text), _render(node)
    return "real", _render(node)


def _render(node: ast.expr | None) -> str:
    if node is None:
        return ""
    text = ast.unparse(node)
    return text if len(text) <= 90 else text[:87] + "..."


def _enclosing(tree: ast.AST) -> dict[int, str]:
    """The innermost function or class name for each node's line, in one walk.

    A walk per function is quadratic on a large module (`ast.walk` re-visits
    every descendant), so this carries the innermost name down the tree and
    overwrites an outer name with an inner one."""
    out: dict[int, str] = {}
    stack: list[tuple[ast.AST, str]] = [(tree, "")]
    while stack:
        node, name = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            name = node.name
        line = getattr(node, "lineno", None)
        if line is not None:
            out[line] = name
        for child in ast.iter_child_nodes(node):
            stack.append((child, name))
    return out


def collect(paths=None) -> list[dict]:
    rows: list[dict] = []
    for base, dirs, files in os.walk(SRC):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fname in sorted(files):
            if not fname.endswith(".py"):
                continue
            path = os.path.join(base, fname)
            if paths and not any(path.endswith(suffix) for suffix in paths):
                continue
            with open(path, encoding="utf-8") as handle:
                try:
                    tree = ast.parse(handle.read(), filename=path)
                except SyntaxError as error:  # pragma: no cover
                    print(f"cannot parse {path}: {error}", file=sys.stderr)
                    continue
            enclosing = _enclosing(tree)
            rel = os.path.relpath(path, ROOT)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = _call_name(node)
                if name not in COMPILE and name not in WRAPPERS:
                    continue
                if name == "compile_source":
                    subject = _arg(node, 1, "filename")
                    argname = "filename"
                elif name == "compile_files":
                    subject = _arg(node, 0, "paths")
                    argname = "paths"
                else:  # the registry wrapper: (name, source, source_path)
                    subject = _arg(node, 2, "source_path")
                    argname = "source_path"
                kind, rendered = _classify(subject)
                profile = _arg(node, 0, "profile") if name != "compile_files" else None
                rows.append({
                    "file": rel,
                    "line": node.lineno,
                    "function": enclosing.get(node.lineno, ""),
                    "call": name,
                    "argument": argname,
                    "names": rendered,
                    "class": kind,
                    "profile": _render(profile) if profile is not None else "",
                })
    rows.sort(key=lambda row: (row["file"], row["line"]))
    return rows


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="one JSON object per row")
    parser.add_argument("--files", nargs="*", default=None,
                        help="only files whose path ends in one of these")
    args = parser.parse_args(argv)
    rows = collect(args.files)
    if args.json:
        for row in rows:
            print(json.dumps(row, sort_keys=True))
    else:
        print(f"{'file:line':<44} {'function':<26} {'names':<46} class")
        for row in rows:
            where = f"{row['file']}:{row['line']}"
            print(f"{where:<44} {row['function']:<26} {row['names']:<46} "
                  f"{row['class']}")
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["class"]] = counts.get(row["class"], 0) + 1
    print(f"# {len(rows)} compile call sites: "
          + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
