#!/usr/bin/env python3
"""Keep `selfhost/lower.rvl`'s in-file `test` blocks sorted by name (#1768).

WHY. Every self-host pull request that adds in-file tests used to append them
at the END of `selfhost/lower.rvl`, so any two such pull requests conflicted on
the file's tail, and the same tail conflicted again in the generated
`crates/revl-gate/src/selfhost.rs`, where the blocks become `#[test]` fns.
Sorted by name, a new test goes where its name falls. Two pull requests then
insert at different places and git merges them; they only meet when two new
names fall between the same two existing tests.

WHAT IS SORTED. The tests section is everything after the
`// ===... tests` marker line. Its top-level items are found with the
reference lexer (so a `}` inside a string or a triple-quoted program is never
read as the end of a block). Each item keeps the comment lines and blank lines
written above it. Items that are not `test` blocks (helper fns the tests call)
come first, in their written order; the `test` blocks follow, sorted by their
name, ties kept in written order. One blank line separates items. Nothing
inside an item changes, so the sort is a pure reorder: the module's
declarations, and therefore every non-test byte the self-host emits, are the
same before and after.

CONFLICTS. `--write` also resolves git conflict markers that fall inside the
tests section, by keeping both sides' items and sorting: two pull requests that
added different tests in the same gap is the only conflict this layout leaves.
It refuses (exit 2) when a marker sits outside the section, when the merged
section names one test twice (both sides edited the same test), or when the
section does not lex. `tools/regen_generated.py` runs it on a conflict in
`selfhost/lower.rvl`.

Usage:
    python3 tools/sort_lower_tests.py --check   # CI: exit 1 when unsorted
    python3 tools/sort_lower_tests.py --write   # sort in place (and resolve)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

LOWER = ROOT / "selfhost" / "lower.rvl"
MARKER = "// ================================================================ tests"
CONFLICT = ("<<<<<<< ", "=======", ">>>>>>> ", "||||||| ")


class SortError(Exception):
    """The file cannot be sorted without a person deciding something."""


def _is_marker(line: str) -> bool:
    return line.startswith(CONFLICT[0]) or line.startswith(CONFLICT[2]) \
        or line.startswith(CONFLICT[3]) or line.rstrip("\n") == CONFLICT[1]


def _split(text: str) -> tuple[str, str]:
    """`(head through the marker line, the tests section after it)`."""
    at = text.find(MARKER)
    if at == -1:
        raise SortError(f"no `{MARKER}` line: cannot find the tests section")
    end = text.index("\n", at) + 1
    return text[:end], text[end:]


def _resolve_markers(section: str) -> str:
    """Both sides of every conflict hunk, ours first. A diff3 base part is
    dropped: an item it held that neither side kept is gone on both sides."""
    out: list[str] = []
    mode = None
    for line in section.splitlines(keepends=True):
        if line.startswith(CONFLICT[0]):
            mode = "ours"
            continue
        if line.startswith(CONFLICT[3]) and mode == "ours":
            mode = "base"
            continue
        if line.rstrip("\n") == CONFLICT[1] and mode in ("ours", "base"):
            mode = "theirs"
            continue
        if line.startswith(CONFLICT[2]) and mode == "theirs":
            mode = None
            continue
        if mode != "base":
            out.append(line)
    if mode is not None:
        raise SortError("an unterminated conflict hunk in the tests section")
    return "".join(out)


def items(section: str) -> list[dict]:
    """The section's top-level items, each `{"kind", "name", "text"}`, in
    written order. `text` is the item's own lines plus the comment and blank
    lines above it, stripped of leading and trailing blank lines."""
    from revl import lexer  # noqa: PLC0415
    from revl.errors import RevlError  # noqa: PLC0415

    try:
        toks = lexer.lex(section, "selfhost/lower.rvl (tests section)")
    except RevlError as exc:
        raise SortError(f"the tests section does not lex: {exc}") from None
    lines = section.splitlines(keepends=True)
    spans: list[tuple[int, int, str, str]] = []
    depth = 0
    start = None
    for i, tok in enumerate(toks):
        if tok.kind == "eof":
            break
        if depth == 0 and start is None:
            start = (tok.line, tok.value,
                     toks[i + 1].value if i + 1 < len(toks) else "")
        if tok.kind == "{":
            depth += 1
        elif tok.kind == "}":
            depth -= 1
            if depth == 0 and start is not None:
                spans.append((start[0], tok.line, start[1], start[2]))
                start = None
    if depth != 0 or start is not None:
        raise SortError("the tests section has an unbalanced item")
    out = []
    prev_end = 0
    for first, last, kind, name in spans:
        chunk = "".join(lines[prev_end:last]).strip("\n")
        out.append({"kind": kind, "name": name, "text": chunk})
        prev_end = last
    tail = "".join(lines[prev_end:]).strip()
    if tail:
        raise SortError(f"text after the last item in the tests section: {tail[:80]!r}")
    return out


def sort_text(text: str) -> str:
    """`text` with its tests section sorted (and its conflicts resolved)."""
    head, section = _split(text)
    if any(_is_marker(line) for line in head.splitlines()):
        raise SortError("a conflict marker outside the tests section: resolve "
                        "it by hand, then run --write")
    section = _resolve_markers(section)
    found = items(section)
    tests = [it for it in found if it["kind"] == "test"]
    names = [it["name"] for it in tests]
    twice = sorted({n for n in names if names.count(n) > 1})
    if twice:
        raise SortError("a test is defined twice (both sides of a merge "
                        "edited it?): " + ", ".join(repr(n) for n in twice))
    others = [it for it in found if it["kind"] != "test"]
    ordered = others + sorted(tests, key=lambda it: it["name"])
    return head + "\n" + "\n\n".join(it["text"] for it in ordered) + "\n"


def problems(path: Path = LOWER) -> list[str]:
    """Why `path` is not in sorted form; empty when it is."""
    text = path.read_text(encoding="utf-8")
    try:
        fresh = sort_text(text)
    except SortError as exc:
        return [str(exc)]
    if fresh == text:
        return []
    return [f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}: "
            "the in-file test blocks are not sorted by name; run "
            "`python3 tools/sort_lower_tests.py --write`"]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--write", action="store_true")
    ap.add_argument("--path", type=Path, default=LOWER, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.check:
        found = problems(args.path)
        for line in found:
            print(line, file=sys.stderr)
        if not found:
            print(f"{args.path.name}: test blocks are sorted.")
        return 1 if found else 0
    text = args.path.read_text(encoding="utf-8")
    try:
        fresh = sort_text(text)
    except SortError as exc:
        print(f"sort_lower_tests: {exc}", file=sys.stderr)
        return 2
    if fresh != text:
        args.path.write_text(fresh, encoding="utf-8")
        print(f"sorted {args.path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
