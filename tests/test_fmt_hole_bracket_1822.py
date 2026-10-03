"""Issue #1822: `revl fmt` keeps a typed hole's bracket tight, `hole[T]`.

`hole` is a keyword, and the spacing rule gave a keyword followed by `[` a
space (right for `return [1, 2]`), with `emission[db]` as its only exception.
So `revl fmt` wrote `hole [Str] "..."`, a spelling the docs, the scaffolder
and the fillSpecs never use. A keyword whose `[` is its own argument binds it
tight; one that is followed by a list keeps its space.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.formatter import format_admitted, format_source  # noqa: E402

HOLED = ('service Greeter { fn greet() -> Str }\n'
         'component G provides greeter: Greeter {\n'
         '  provide greeter {\n'
         '    fn greet() = hole[Str] "produce a greeting"\n'
         '  }\n'
         '}\n')


def test_a_typed_hole_keeps_its_bracket_tight():
    assert format_source(HOLED) == HOLED


def test_a_spaced_hole_is_written_tight():
    spaced = HOLED.replace("hole[Str]", "hole [Str]")
    assert format_source(spaced) == HOLED


@pytest.mark.parametrize("text,expected", [
    ("fn f() -> List[Int] { return [1, 2] }", "fn f() -> List[Int] { return [1, 2] }\n"),
    ("service S { emission[db] fn put(k: Str) }", "service S { emission[db] fn put(k: Str) }\n"),
    ("fn g() -> Int = hole [Int]  \"n\"", 'fn g() -> Int = hole[Int] "n"\n'),
])
def test_only_a_keyword_that_takes_a_bracket_binds_it(text, expected):
    assert format_source(text) == expected


def test_the_gate_admits_the_tight_hole_and_it_is_idempotent():
    spaced = HOLED.replace("hole[Str]", "hole [Str]")
    text, gate = format_admitted(spaced)
    assert gate.admitted and text == HOLED
    assert format_admitted(text)[0] == text


SCAFFOLDS = sorted([*(ROOT / "examples").rglob("*.rvl"),
                    *(ROOT / "tests" / "fixtures").rglob("*.rvl")])


@pytest.mark.parametrize("path", [p for p in SCAFFOLDS
                                  if "hole[" in p.read_text(encoding="utf-8")],
                         ids=lambda p: p.name)
def test_every_holed_example_keeps_hole_tight(path):
    source = path.read_text(encoding="utf-8")
    text, gate = format_admitted(source, str(path))
    assert gate.admitted
    assert "hole [" not in text
