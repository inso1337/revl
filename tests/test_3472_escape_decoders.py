"""stdlib/escape.rvl decodes what it encodes: unescape_uri, parse_query, query_of (#3472).

The module had three encoders and no decoder, so anything receiving a request
had to hand-roll a percent-decoder that could disagree with `escape_uri` on
`+`, `%2B`, invalid escapes and non-ASCII bytes. The decoders are pure revl and
total (every failure is an `Err`), so they lower on every tier; the py tier is
executed here. Round-trip law: `unescape_uri(escape_uri(s)) == Ok(s)`.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from revl import compile_source  # noqa: E402
from revl.test import RUNNERS  # noqa: E402

STDLIB = (ROOT / "stdlib" / "escape.rvl").read_text(encoding="utf-8")

DEC = '''\
use "stdlib/escape.rvl" { unescape_uri }

fn dec(s: Str) -> Str {
  return match unescape_uri(s) { Ok(v) => v, Err(e) => "" }
}

fn dec_err(s: Str) -> Str {
  return match unescape_uri(s) { Ok(v) => "", Err(e) => e }
}
'''

PAIRS = '''\
use "stdlib/escape.rvl" { parse_query, QueryPair }

fn pairs(s: Str) -> List[QueryPair] {
  return match parse_query(s) { Ok(ps) => ps, Err(e) => [] }
}

fn qerr(s: Str) -> Str {
  return match parse_query(s) { Ok(ps) => "", Err(e) => e }
}
'''

CASES = {
    "round_trip": DEC + '''\
use "stdlib/escape.rvl" { escape_uri }

test "round trip law" {
  let s = "a b+c/d?e&f=1é☕"
  assert dec(escape_uri(s)) == s
}
''',
    "plus_and_percent20": DEC + '''\
test "plus is literal and %20 is space in unescape_uri" {
  assert dec("a+b%20c") == "a+b c"
}
''',
    "percent2b": DEC + '''\
test "%2B decodes to plus" {
  assert dec("%2B") == "+"
}
''',
    "utf8_multibyte": DEC + '''\
test "%C3%A9 decodes to é" {
  assert dec("%C3%A9") == "é"
}
''',
    "truncated_escape": DEC + '''\
test "a truncated % escape is Err" {
  assert dec_err("a%2").indexOf("truncated") >= 0
}
''',
    "bad_hex_escape": DEC + '''\
test "a non-hex % escape is Err" {
  assert dec_err("%zz").indexOf("hex") >= 0
}
''',
    "invalid_utf8": DEC + '''\
test "%ff is invalid UTF-8, Err not raise" {
  assert dec_err("%ff").length() > 0
}
''',
    "query_of": '''\
use "stdlib/escape.rvl" { query_of }

test "query_of reads between ? and #" {
  assert query_of("https://x.test/p?a=1&b=2#frag") == "a=1&b=2"
  assert query_of("https://x.test/p#frag") == ""
  assert query_of("https://x.test/p") == ""
  assert query_of("https://x.test/p?a=1#f") == "a=1"
}
''',
    "parse_query_basics": PAIRS + '''\
test "parse_query splits on & and first =, keeps order and repeats" {
  let ps = pairs("a=1&b=x+y&b=%C3%A9&flag&empty=")
  assert ps.length() == 5
  assert ps[0].key == "a" && ps[0].value == "1"
  assert ps[1].key == "b" && ps[1].value == "x y"
  assert ps[2].key == "b" && ps[2].value == "é"
  assert ps[3].key == "flag" && ps[3].value == ""
  assert ps[4].key == "empty" && ps[4].value == ""
}
''',
    "parse_query_plus_vs_2b": PAIRS + '''\
test "+ is space, %2B is plus, value may contain =" {
  let ps = pairs("k=a+b%2Bc&eq=1=2")
  assert ps[0].value == "a b+c"
  assert ps[1].value == "1=2"
}
''',
    "parse_query_bad_escape": PAIRS + '''\
test "a bad escape in the query is Err, not a raise" {
  assert qerr("a=%zz").length() > 0
}
''',
    "parse_query_empty": PAIRS + '''\
test "empty query is Ok([]); empty segments are skipped" {
  assert pairs("").length() == 0
  let ps = pairs("&&a=1&&")
  assert ps.length() == 1
  assert ps[0].key == "a" && ps[0].value == "1"
}
''',
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_decoder_case(name):
    src = CASES[name]
    ir = compile_source(src, f"escape3472_{name}.rvl",
                        modules={"stdlib/escape.rvl": STDLIB})
    status, message = RUNNERS["py"](ir)
    assert status == "pass", f"{name}: {message}"


def test_the_module_still_introduces_no_externs():
    ir = compile_source('use "stdlib/escape.rvl" { unescape_uri, parse_query, query_of }\n'
                        'pub fn f(s: Str) -> Str { return query_of(s) }\n',
                        "probe.rvl", modules={"stdlib/escape.rvl": STDLIB})
    assert ir.get("externs", []) == []
