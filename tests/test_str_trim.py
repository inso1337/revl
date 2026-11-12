"""`Str.trim()` (issue #3361, docs/stdlib-2.0.md §Str.trim).

Strips both ends of a text over exactly the `is_space` set (space, tab, LF,
CR) and nothing else, on every tier that lowers `is_space` (py, ts, rust), so
a trimmed text is the same on each of them. The host trims are not used: JS
`trim()`, python `strip()` and rust `str::trim` each take a different, wider
set (#3265), which is the divergence this builtin closes.

The same revl test blocks run on each tier, and every assertion pins an exact
string, so a pass on two tiers is identical output on both. A character the
set does not hold (VT, FF, NBSP) stays put on every tier: that is the half a
host trim would get wrong.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.test import run_py, run_rust, run_ts  # noqa: E402

# revl plain strings carry no escapes, so each control character is built
# from its code point.
PROGRAM = """\
fn ch(n: Int) -> Str { return n.to_char() }
fn ws() -> Str { return " ".concat(ch(9)).concat(ch(10)).concat(ch(13)) }

test "trim drops spaces from both ends" {
  assert "   a b   ".trim() == "a b"
}

test "trim drops tabs from both ends" {
  assert ch(9).concat("x").concat(ch(9)).concat(ch(9)).trim() == "x"
}

test "trim drops newlines and carriage returns from both ends" {
  assert ch(10).concat(ch(13)).concat("line").concat(ch(13)).concat(ch(10)).trim() == "line"
}

test "trim drops the full is_space set from both ends and keeps the inside" {
  let inner = "a".concat(ws()).concat("b")
  assert ws().concat(ws()).concat(inner).concat(ws()).trim() == inner
}

test "trim of an all-space text is empty" {
  assert ws().concat(ws()).trim() == ""
  assert ws().concat(ws()).trim().length() == 0
}

test "trim of the empty text is empty" {
  assert "".trim() == ""
}

test "trim leaves a text with no edge space unchanged" {
  assert "CANNOT: x".trim() == "CANNOT: x"
}

test "trim keeps characters outside the is_space set" {
  assert ch(11).concat(" a ").concat(ch(11)).trim() == ch(11).concat(" a ").concat(ch(11))
  assert ch(12).concat("a").concat(ch(12)).trim() == ch(12).concat("a").concat(ch(12))
  assert ch(160).concat("a").concat(ch(160)).trim() == ch(160).concat("a").concat(ch(160))
  assert " ".concat(ch(160)).concat(" ").trim() == ch(160)
}

test "trim keeps a long interior run of spaces and is not quadratic on it" {
  let gap = " ".repeat(200000)
  let t = gap.concat("a").concat(gap).concat("b").concat(gap).trim()
  assert t.length() == 200002
  assert t.startsWith("a ") && t.endsWith(" b")
}

test "trim of a sliced reply line" {
  let line = "CANNOT:   the rules cannot read files  "
  assert line.slice(7, line.length()).trim() == "the rules cannot read files"
}
"""


@pytest.fixture(scope="module")
def ir():
    return compile_source(PROGRAM)


def test_trim_needs_a_str_receiver():
    with pytest.raises(RevlError) as excinfo:
        compile_source("fn bad(n: Int) -> Str { return n.trim() }\n")
    assert "Str receiver" in str(excinfo.value)


def test_trim_takes_no_argument():
    with pytest.raises(RevlError) as excinfo:
        compile_source('fn bad(s: Str) -> Str { return s.trim(" ") }\n')
    assert "takes 0 argument" in str(excinfo.value)


def test_trim_result_is_str():
    compile_source("fn f(s: Str) -> Int { return s.trim().length() }\n")


@pytest.mark.parametrize("run", [run_py, run_ts, run_rust],
                         ids=["py", "ts", "rust"])
def test_trim_on_tier(ir, run):
    verdict, detail = run(ir)
    if verdict == "skip":
        pytest.skip(str(detail))
    assert verdict == "pass", detail
