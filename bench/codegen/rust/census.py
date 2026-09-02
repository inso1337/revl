#!/usr/bin/env python3
"""Count wasteful constructs in the rust the emitter produces for REAL code.

`run.py` measures one emitter decision per benchmark under a counting
allocator. This does the complementary thing: it emits the self-host compiler
stages that build on the rust tier and counts how often each known-wasteful
shape actually appears. A benchmark says how bad a shape is; this says how
much of it there is.

Everything here is a property of the generated TEXT, so the numbers are
identical on an idle machine and on a loaded one. No timing is involved.

Run:
    PYTHONPATH=<repo>/src python3 bench/codegen/rust/census.py
    ... --files selfhost/lexer.rvl selfhost/parser.rvl
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "src"))

# The stages that currently emit to rust. `compile.rvl` and the `emit_*.rvl`
# stages carry externs with no `@rs` body, so the emitter refuses them; that is
# a portability gap, not a perf finding, and the tool says so rather than
# counting a partial module.
DEFAULT_FILES = [
    "selfhost/lexer.rvl",
    "selfhost/parser.rvl",
    "selfhost/checker.rvl",
    "selfhost/lower.rvl",
]

# Each shape is (label, regex, what a competent rust developer writes instead).
SHAPES = [
    (
        "literal into a &str slot",
        r"&String::from\(",
        "the literal itself; the helper trait's argument is already `&str`",
    ),
    (
        "literal compared to a Str",
        r"[!=]= String::from\(",
        "`x == \"lit\"`; `String: PartialEq<&str>` compares without allocating",
    ),
    (
        "index read cloned",
        r"\) as usize\]\.clone\(\)",
        "`xs[i]` borrowed, when the read lands in a read-only position",
    ),
    (
        "self-append via concat",
        r"^\s*(\w+) = \1\.revl_concat\(",
        "`v.extend(..)` / `s.push_str(..)`; the receiver is dead after the assign",
    ),
    (
        "self-append via +",
        r"^\s*(\w+) = format!\(\"\{\}\{\}\", \1,",
        "`s.push_str(..)`; same shape, spelled with `+`",
    ),
    (
        "iterable cloned for a for-loop",
        r"for \w+ in \w+\.clone\(\)",
        "`for x in &v` when the binding is dead after the loop",
    ),
]

TOTALS = [
    ("all `.clone()`", r"\.clone\(\)"),
    ("all `String::from(`", r"String::from\("),
    ("all `format!(`", r"format!\("),
]


def _load_emitter():
    path = ROOT / "backends" / "rust" / "emit.py"
    spec = importlib.util.spec_from_file_location("rustemit_census", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _module_body(src: str) -> str:
    """The emitted module WITHOUT the fixed stdlib helper-trait prelude, so a
    shape is counted where the program put it and not once per module."""
    marker = "trait RevlStrOps"
    return src[: src.index(marker)] if marker in src else src


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--files", nargs="*", default=DEFAULT_FILES)
    args = ap.parse_args(argv[1:])

    from revl import compile_files

    emitter = _load_emitter()
    bodies: dict[str, str] = {}
    for rel in args.files:
        try:
            bodies[rel] = _module_body(emitter.emit(compile_files([str(ROOT / rel)])))
        except Exception as exc:  # noqa: BLE001
            print(f"{rel}: NOT EMITTED — {str(exc)[:90]}")

    if not bodies:
        print("nothing emitted; nothing counted.")
        return 1

    lines = sum(b.count("\n") for b in bodies.values())
    print("=" * 78)
    print("wasteful shapes in the rust emitted for the self-host stages")
    print("=" * 78)
    print(f"modules  : {', '.join(bodies)}")
    print(f"emitted  : {lines:,} lines (helper-trait prelude excluded)")
    print()
    print("These are counts of generated TEXT. They do not move with machine load.")
    print()

    for label, pattern, instead in SHAPES:
        rx = re.compile(pattern, re.M)
        n = sum(len(rx.findall(b)) for b in bodies.values())
        print(f"{n:>7,}  {label}")
        print(f"{'':>7}  a rust developer writes: {instead}")
    print()
    for label, pattern in TOTALS:
        rx = re.compile(pattern, re.M)
        n = sum(len(rx.findall(b)) for b in bodies.values())
        print(f"{n:>7,}  {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
