"""One-off generator for declared_compensate.ir.json. NOT part of the build.

The ts mirror of tests/test_declared_compensate_positions.py (issue #1511): an
emission extern that DECLARES its own compensation,

    extern emission fn put_let(k: Str) -> Int compensate undo_let() = @ts { .. }

crossed from a provide method in every position (statement, `let`, `return`,
argument, `if` arm, nested operand), from an activation body, and beside a
site-spelled `emit .. compensate ..`. Every host body records what it ran on
the runtime's `hostLog` (`put:<tag>`, `comp:<tag>`), so the order the
compensations ran in is observed, not inferred.

The whole source is compiled by `compile_source`, so the shapes are real
compiler output. Run once, by hand, to regenerate the checked-in fixture:

    python3 backends/typescript/tests/fixtures/_gen_declared_compensate.py
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

TAGS = ["statement", "let", "return", "argument", "ifarm", "nested", "act",
        "first", "boom"]


def _declared(tag: str) -> str:
    fail = "  throw new Error('put:boom failed')\n" if tag == "boom" else ""
    return (f"extern pure fn undo_{tag}() -> Unit = @ts {{\n"
            f"  record('comp:{tag}')\n}}\n"
            f"extern emission fn put_{tag}(k: Str) -> Int compensate undo_{tag}()"
            f" = @ts {{\n  record('put:{tag}')\n{fail}  return 1n\n}}\n")


_SOURCE = (
    "fn double(n: Int) -> Int { return n * 2 }\n"
    "extern emission fn plain(k: Str) -> Int = @ts {\n"
    "  record('put:site')\n  return 1n\n}\n"
    "extern pure fn undo_site() -> Unit = @ts {\n  record('comp:site')\n}\n"
    + "".join(_declared(tag) for tag in TAGS)
    + "service Positions {\n"
    "  emission fn statement() -> Int\n"
    "  emission fn binding() -> Int\n"
    "  emission fn returned() -> Int\n"
    "  emission fn argument() -> Int\n"
    "  emission fn ifarm() -> Int\n"
    "  emission fn nested() -> Int\n"
    "  emission fn boom() -> Int\n"
    "}\n"
    "service Every { emission fn run() -> Int }\n"
    # one method per position, each crossing `put_<tag>` once
    "component Positions provides positions: Positions {\n"
    "  provide positions {\n"
    '    fn statement() {\n      emit put_statement("k")\n      return 0\n    }\n'
    '    fn binding() {\n      let a = emit put_let("k")\n      return a\n    }\n'
    '    fn returned() {\n      return emit put_return("k")\n    }\n'
    '    fn argument() {\n      let s = double(emit put_argument("k"))\n      return s\n    }\n'
    '    fn ifarm() {\n      if (true) { emit put_ifarm("k") }\n      return 0\n    }\n'
    '    fn nested() {\n      let b = emit put_nested("k") + 1\n      return b\n    }\n'
    '    fn boom() {\n      emit put_first("k")\n      return emit put_boom("k")\n    }\n'
    "  }\n"
    "}\n"
    # every position in one method, a site-spelled compensation among them, and
    # a declared crossing in the activation body
    "component Everything provides all: Every {\n"
    '  emit put_act("k")\n'
    "  provide all {\n"
    "    fn run() {\n"
    '      emit put_statement("k")\n'
    '      let a = emit put_let("k")\n'
    '      let s = double(emit put_argument("k"))\n'
    '      if (true) { emit put_ifarm("k") }\n'
    '      emit plain("k") compensate undo_site()\n'
    '      let b = emit put_nested("k") + 1\n'
    '      return emit put_return("k")\n'
    "    }\n"
    "  }\n"
    "}\n"
)


def build() -> dict:
    return compile_source(_SOURCE, "declared_compensate.rvl")


if __name__ == "__main__":
    out = pathlib.Path(__file__).resolve().parent / "declared_compensate.ir.json"
    out.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
