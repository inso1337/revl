"""A parameter, `let` or `for` variable named like an emission extern shadows
it (issue #3354, duplicate #3378).

The G4 value rule of #2842 refuses an emission extern read as a function
value. It applied to the bare name, so a local of the same spelling was refused
as if it were the extern, also when the extern sat in another file of the same
compile, and the diagnostic then named the compile's first file. A local in
scope shadows the extern; a read that resolves to the extern is still refused,
and names the file it is in.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files, compile_source  # noqa: E402

EXT = 'extern emission fn now() -> Int = @py {\n  return 1\n}\n'

LOCALS = {
    "parameter": 'fn later(now: Int) -> Int { return now + 1 }\n',
    "let": 'fn later(n: Int) -> Int {\n  let now = n + 1\n  return now\n}\n',
    "for": ('fn later(ns: List[Int]) -> Int {\n  var s = 0\n'
            '  for (now of ns) { s = s + now }\n  return s\n}\n'),
    "match-binding": ('fn later(o: Opt[Int]) -> Int {\n'
                      '  return match o { Some(now) => now, None => 0, }\n}\n'),
    "lambda-parameter": ('fn later() -> Int {\n'
                         '  let g = (now: Int) => now + 1\n  return g(1)\n}\n'),
}

REAL_READ = 'fn later() -> Int {\n  let f = now\n  return 1\n}\n'


def _refusal(run) -> str:
    try:
        run()
    except RevlError as err:
        return str(err).splitlines()[0]
    return ""


@pytest.mark.parametrize("kind", sorted(LOCALS))
def test_a_local_shadows_the_extern_in_one_file(kind):
    assert _refusal(lambda: compile_source(EXT + LOCALS[kind], "a.rvl")) == ""


@pytest.mark.parametrize("order", [("a.rvl", "b.rvl"), ("b.rvl", "a.rvl")])
@pytest.mark.parametrize("kind", ["parameter", "let", "for"])
def test_a_local_shadows_the_extern_from_another_file(kind, order):
    sources = {"a.rvl": LOCALS[kind], "b.rvl": EXT}
    assert _refusal(lambda: compile_files(list(order), sources=sources)) == ""


def test_a_real_read_of_the_extern_is_still_refused():
    msg = _refusal(lambda: compile_source(EXT + REAL_READ, "a.rvl"))
    assert msg == ("a.rvl:5: emission `now` cannot be used as a function value; "
                   "call it by name under `emit` (G4)")


@pytest.mark.parametrize("order", [("a.rvl", "b.rvl"), ("b.rvl", "a.rvl")])
def test_a_real_read_in_another_file_names_that_file(order):
    sources = {"a.rvl": REAL_READ, "b.rvl": EXT}
    msg = _refusal(lambda: compile_files(list(order), sources=sources))
    assert msg.startswith("a.rvl:2: emission `now` cannot be used as a function value")


def test_a_local_shadows_only_inside_its_block():
    closed_block = ('fn later(b: Bool) -> Int {\n  if (b) {\n    let now = 1\n  }\n'
                    '  let f = now\n  return 1\n}\n')
    let_after_use = ('fn later() -> Int {\n  let f = now\n  let now = 2\n'
                     '  return now\n}\n')
    assert "(G4)" in _refusal(lambda: compile_source(EXT + closed_block, "a.rvl"))
    assert "(G4)" in _refusal(lambda: compile_source(EXT + let_after_use, "a.rvl"))
