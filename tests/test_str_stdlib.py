"""The stdlib STR module (roadmap item 193, docs/stdlib-str.md, stdlib/str.rvl).

The string-utility kit every self-host stage otherwise re-derives.
`selfhost/checker.rvl` and `selfhost/emit_py.rvl` (item 192) BOTH hand-roll the
same toolkit — `trim`/`trim_ws`/`lstrip`/`last_index_of`/`ident_tokens`/
`split_type`/`split_types` — directly over the base `Str` surface (item 11).
This module is that kit, once, so the next emitter stages `use` it instead.

Unlike stdlib/value.rvl (per-tier `@py` externs, five tiers deferred), EVERY
function here is PURE revl built on the base `Str` methods, so it lowers on
every tier the day it lands and adds no new primitive. This suite proves:

  * the module imports through `use` and carries NO externs (pure revl);
  * the py tier EXECUTES each function, including the empty / no-match /
    all-whitespace edge cases;
  * `dedent` is BYTE-EXACT to Python's `textwrap.dedent` — pinned against a
    table of hand-picked reference cases AND a randomized fuzz batch;
  * a pure-revl program that `use`s the kit compiles and runs on py.
"""

import importlib.util
import random
import re
import shutil
import sys
import tempfile
import textwrap
import types
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

STDLIB = ROOT / "stdlib" / "str.rvl"

#: a self-hosted-stage-shaped consumer: it `use`s the kit and re-exports each
#: function under a `t_` wrapper so the py tier can drive them directly. This is
#: also the PROOF that a pure-revl program using the kit compiles and runs.
CONSUMER = """\
use "stdlib/str.rvl" {
  is_space, trim, lstrip, rstrip, contains, index_of, last_index_of,
  ident_tokens, split_top, dedent, str_utf8_bytes,
  str_newline, str_tab, str_char, str_crlf,
  lower, upper, lines, slice_from
}

fn t_str_utf8_bytes(s: Str) -> List[Int] { return str_utf8_bytes(s) }
fn t_str_newline() -> Str { return str_newline() }
fn t_str_tab() -> Str { return str_tab() }
fn t_str_char(code: Int) -> Str { return str_char(code) }
fn t_str_crlf() -> Str { return str_crlf() }
// code point of a single-char string round-trips through str_char.
fn t_str_char_code(code: Int) -> Int { return str_char(code).charCodeAt(0) }
// end-to-end: a line join with the portable newline yields REAL newlines,
// exactly the line-joining shape item 181 was cut for.
fn t_join_newline(parts: List[Str]) -> Str { return parts.join(str_newline()) }
fn t_is_space(c: Str) -> Bool { return is_space(c) }
fn t_trim(s: Str) -> Str { return trim(s) }
fn t_is_space_builtin(c: Str) -> Bool { return c.is_space() }
fn t_lstrip(s: Str) -> Str { return lstrip(s) }
fn t_rstrip(s: Str) -> Str { return rstrip(s) }
fn t_contains(s: Str, sub: Str) -> Bool { return contains(s, sub) }
fn t_index_of(s: Str, sub: Str) -> Int { return index_of(s, sub) }
fn t_last_index_of(s: Str, sub: Str) -> Int { return last_index_of(s, sub) }
fn t_ident_tokens(s: Str) -> List[Str] { return ident_tokens(s) }
fn t_split_top(s: Str, sep: Str) -> List[Str] { return split_top(s, sep) }
fn t_dedent(s: Str) -> Str { return dedent(s) }
fn t_lower(s: Str) -> Str { return lower(s) }
fn t_upper(s: Str) -> Str { return upper(s) }
fn t_lines(s: Str) -> List[Str] { return lines(s) }
fn t_slice_from(s: Str, i: Int) -> Str { return slice_from(s, i) }

// a tiny end-to-end use of the kit: normalise a type spelling's argument list
// (exactly the checker/emit_py hand-rolled `split_type`/`split_types` shape).
fn type_args(spelling: Str) -> List[Str] {
  let open = index_of(spelling, "[")
  if (open < 0) { return [] }
  let close = last_index_of(spelling, "]")
  let inner = trim(spelling.slice(open + 1, close))
  if (inner == "") { return [] }
  return split_top(inner, ",")
}
"""


@pytest.fixture(scope="module")
def consumer_ir(tmp_path_factory):
    # the module resolves relative to the importing file, so the stdlib file
    # sits beside the consumer fixture (its repo content is pinned by
    # test_module_file_is_the_documented_surface).
    d = tmp_path_factory.mktemp("str_consumer")
    (d / "stdlib").mkdir()
    (d / "stdlib" / "str.rvl").write_text(STDLIB.read_text(encoding="utf-8"),
                                          encoding="utf-8")
    main = d / "main.rvl"
    main.write_text(CONSUMER, encoding="utf-8")
    return compile_files([str(main)])


def _exec_python(ir: dict):
    spec = importlib.util.spec_from_file_location(
        "pyemit_str", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "str.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@pytest.fixture(scope="module")
def ns(consumer_ir):
    return _exec_python(consumer_ir)


# ---------------------------------------------------------------- the module

def test_module_imports_and_is_pure_revl(consumer_ir):
    # PURE revl: the kit contributes NO externs — it composes the frozen base
    # Str surface only, which is what lets it lower on every tier at once.
    assert consumer_ir.get("externs", []) == []
    names = {f["name"] for f in consumer_ir["functions"]}
    for pub in ("is_space", "trim", "lstrip", "rstrip", "contains", "index_of",
                "last_index_of", "ident_tokens", "split_top", "dedent",
                "str_utf8_bytes", "str_newline", "str_tab", "str_char",
                "str_crlf", "lower", "upper", "lines", "slice_from"):
        assert pub in names, pub
    assert consumer_ir["ir_version"] == 3


def test_module_file_is_the_documented_surface():
    text = STDLIB.read_text(encoding="utf-8")
    for sig in (
        "pub fn is_space(c: Str) -> Bool",
        "pub fn trim(s: Str) -> Str",
        "pub fn lstrip(s: Str) -> Str",
        "pub fn rstrip(s: Str) -> Str",
        "pub fn contains(s: Str, sub: Str) -> Bool",
        "pub fn index_of(s: Str, sub: Str) -> Int",
        "pub fn last_index_of(s: Str, sub: Str) -> Int",
        "pub fn ident_tokens(s: Str) -> List[Str]",
        "pub fn split_top(s: Str, sep: Str) -> List[Str]",
        "pub fn dedent(text: Str) -> Str",
        "pub fn str_utf8_bytes(s: Str) -> List[Int]",
        "pub fn str_newline() -> Str",
        "pub fn str_tab() -> Str",
        "pub fn str_char(code: Int) -> Str",
        "pub fn str_crlf() -> Str",
        "pub fn lower(s: Str) -> Str",
        "pub fn upper(s: Str) -> Str",
        "pub fn lines(s: Str) -> List[Str]",
        "pub fn slice_from(s: Str, i: Int) -> Str",
    ):
        assert sig in text, sig
    # no @py / per-tier extern bodies in CODE — the pure-revl guarantee. (The
    # header comment discusses them, so scan only non-comment lines.)
    code = "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("//"))
    assert "@py" not in code
    assert "extern" not in code


# ---------------------------------------------------------------- trimming

@pytest.mark.parametrize("s,exp", [
    ("  hi  ", "hi"),
    ("hi", "hi"),
    ("", ""),
    ("   ", ""),                 # all whitespace
    ("\t\n hi \r\n", "hi"),      # mixed ASCII whitespace
    ("  a b  ", "a b"),          # interior space kept
    ("\x0bhi\x0c", "\x0bhi\x0c"),  # VT / FF kept, as the builtin keeps them
])
def test_trim(ns, s, exp):
    assert ns["t_trim"](s) == exp


@pytest.mark.parametrize("c", [" ", "\t", "\n", "\r", "\x0b", "\x0c", "x", ""])
def test_free_is_space_agrees_with_the_builtin(ns, c):
    # Issue #3466: the free helper and the method are one contract. VT/FF are
    # outside it, so neither spelling calls them whitespace.
    assert ns["t_is_space"](c) == ns["t_is_space_builtin"](c)


def test_free_and_builtin_is_space_agree_in_a_compiled_consumer():
    # Self-contained (own temp dir, no pytest tmp_path) so a receipt can run
    # this node against both commits without the module fixture.
    d = Path(tempfile.gettempdir()) / f"revl-str-3466-{uuid.uuid4().hex}"
    d.mkdir()  # explicit 0o777; tempfile.mkdtemp's 0o700 is unwritable confined
    try:
        shutil.copyfile(STDLIB, d / "str.rvl")
        main = d / "main.rvl"
        main.write_text(
            'use "str.rvl" { is_space }\n'
            "fn free(c: Str) -> Bool { return is_space(c) }\n"
            "fn method(c: Str) -> Bool { return c.is_space() }\n",
            encoding="utf-8")
        ns = _exec_python(compile_files([str(main)]))
        for c in (" ", "\t", "\n", "\r"):
            assert ns["free"](c) is ns["method"](c) is True, c
        for c in ("\x0b", "\x0c"):
            assert ns["free"](c) is ns["method"](c) is False, c
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.mark.parametrize("s", ["  hi  ", "", "   ", "\t x \n", "x"])
def test_lstrip_rstrip_match_python(ns, s):
    assert ns["t_lstrip"](s) == s.lstrip()
    assert ns["t_rstrip"](s) == s.rstrip()


# ---------------------------------------------------------------- search

@pytest.mark.parametrize("s,sub,exp", [
    ("hello", "ell", True),
    ("hello", "z", False),      # no match
    ("hello", "", True),        # empty sub is contained
    ("", "x", False),
    ("", "", True),
])
def test_contains(ns, s, sub, exp):
    assert ns["t_contains"](s, sub) is exp
    assert ns["t_contains"](s, sub) == (sub in s)


@pytest.mark.parametrize("s,sub", [
    ("hello", "l"), ("hello", "z"), ("hello", ""), ("a.b.c", "."), ("", "x"),
])
def test_index_of_matches_python_find(ns, s, sub):
    assert ns["t_index_of"](s, sub) == s.find(sub)


@pytest.mark.parametrize("s,sub", [
    ("hello", "l"),             # last of a repeated char
    ("a.b.c", "."),             # multi-occurrence separator
    ("hello", "z"),             # absent -> -1
    ("hello", ""),              # empty -> len(s)
    ("", "x"),
    ("abcabc", "bc"),           # multi-char substring
])
def test_last_index_of_matches_python_rfind(ns, s, sub):
    assert ns["t_last_index_of"](s, sub) == s.rfind(sub)


# ---------------------------------------------------------------- idents

@pytest.mark.parametrize("s", [
    "List[Row, Map[Str, Int]]",
    "(Int, Str) -> Bool",
    "3foo _bar 9 baz2",         # digit-led runs start at first letter/underscore
    "",                         # no match
    "   ",                      # all whitespace, no idents
    "___",                      # underscores are idents
    "café_x",                   # non-ASCII letter is NOT a word char (re \w with re.ASCII differs; see note)
])
def test_ident_tokens_matches_re_findall(ns, s):
    # the reference is `re.findall(r"[A-Za-z_]\w*")` with ASCII word semantics,
    # which is what the hand-rolled selfhost helper reproduces.
    assert ns["t_ident_tokens"](s) == re.findall(r"[A-Za-z_][A-Za-z0-9_]*", s)


# ---------------------------------------------------------------- split_top

@pytest.mark.parametrize("s,sep,exp", [
    ("Row, Map[Str, Int]", ",", ["Row", "Map[Str, Int]"]),   # nested comma protected
    ("(Int, Str), Bool", ",", ["(Int, Str)", "Bool"]),        # paren depth protected
    ("A", ",", ["A"]),
    ("", ",", []),                                             # empty -> []
    ("A, B, C", ",", ["A", "B", "C"]),
    ("  A ,  B  ", ",", ["A", "B"]),                           # parts trimmed
    ("Map[K, V], List[Row, Col]", ",", ["Map[K, V]", "List[Row, Col]"]),
])
def test_split_top(ns, s, sep, exp):
    assert ns["t_split_top"](s, sep) == exp


def test_type_args_end_to_end(ns):
    # the kit composed the way checker/emit_py hand-roll it
    assert ns["type_args"]("List[Row, Map[Str, Int]]") == ["Row", "Map[Str, Int]"]
    assert ns["type_args"]("Int") == []
    assert ns["type_args"]("List[Int]") == ["Int"]


# ---------------------------------------------------------------- is_space

@pytest.mark.parametrize("c,exp", [
    (" ", True), ("\t", True), ("\n", True), ("\r", True),
    ("\x0b", False), ("\x0c", False),   # VT / FF are not in the builtin set
    ("x", False), ("0", False), ("", False),   # empty is total -> False
])
def test_is_space(ns, c, exp):
    assert ns["t_is_space"](c) is exp


# ---------------------------------------------------------------- dedent

#: hand-picked cases that exercise every branch of textwrap.dedent's algorithm.
DEDENT_CASES = [
    "",                                              # empty
    "no indent\n",                                   # zero margin
    "    hello\n    world\n",                        # uniform space margin
    "  a\n    b\n  c\n",                             # margin = shortest common
    "\thello\n\tworld\n",                            # tab margin
    "line1\n  line2\n",                              # one line unindented -> no strip
    "    a\n\n    b\n",                              # empty middle line excluded
    "    a\n   \n    b\n",                           # whitespace-only line normalized
    "        x\n            y\n        z\n",         # deep, ragged
    "  \t mixed\n  \t other\n",                      # mixed space+tab margin
    "   spaced\n\t tabbed\n",                        # divergent margins -> common prefix ""
    "\t \tone\n\t \ttwo\n",                          # tab/space interleaved margin
    "   trailing\n",                                 # no final newline variant below
    "   trailing",                                   # no trailing newline
    "keep\n    indented\nflush\n",                   # margin "" because a flush line exists
]


@pytest.mark.parametrize("text", DEDENT_CASES)
def test_dedent_byte_exact(ns, text):
    assert ns["t_dedent"](text) == textwrap.dedent(text)


def test_dedent_fuzz_matches_textwrap(ns):
    # randomized proof: build lines from spaces/tabs/content/blank and compare
    # byte-for-byte against textwrap.dedent over many shapes.
    rng = random.Random(1993)
    alphabet = ["    ", "  ", "\t", "\t ", " \t", "x", "yy", "", "  z", "\tq"]
    for _ in range(400):
        n = rng.randint(0, 6)
        lines = ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 3)))
                 for _ in range(n)]
        text = "\n".join(lines)
        if rng.random() < 0.5:
            text += "\n"
        assert ns["t_dedent"](text) == textwrap.dedent(text), repr(text)


# ---------------------------------------------------------------- utf-8 bytes

#: (string, expected UTF-8 bytes) pinned against the known encodings — one per
#: byte-length tier plus the empty string and a mixed string spanning all four.
UTF8_CASES = [
    ("A", [0x41]),                              # 1 byte, ASCII
    ("é", [0xC3, 0xA9]),                   # 2 bytes, e-acute U+00E9
    ("€", [0xE2, 0x82, 0xAC]),             # 3 bytes, euro sign U+20AC
    ("\U0001F600", [0xF0, 0x9F, 0x98, 0x80]),   # 4 bytes, astral U+1F600
    ("", []),                                   # empty
    ("aé€\U0001F600z",                # mixed: 1+2+3+4+1 bytes
     [0x61, 0xC3, 0xA9, 0xE2, 0x82, 0xAC, 0xF0, 0x9F, 0x98, 0x80, 0x7A]),
]


@pytest.mark.parametrize("s,exp", UTF8_CASES)
def test_str_utf8_bytes_known_encodings(ns, s, exp):
    # byte-exact against the hand-written reference AND Python's own encoder.
    got = ns["t_str_utf8_bytes"](s)
    assert got == exp
    assert got == list(s.encode("utf-8"))
    assert all(0 <= b <= 255 for b in got)


def test_str_utf8_bytes_boundary_scalars(ns):
    # the exact code points at each tier boundary (last of one, first of next).
    for cp in (0x00, 0x7F, 0x80, 0x7FF, 0x800, 0xFFFF, 0x10000, 0x10FFFF):
        s = chr(cp)
        assert ns["t_str_utf8_bytes"](s) == list(s.encode("utf-8")), hex(cp)


def test_str_utf8_bytes_fuzz_matches_python(ns):
    # randomized scalars across the whole range (surrogates excluded, since a
    # Str is a sequence of scalar values) compared byte-for-byte with utf-8.
    rng = random.Random(2026)
    for _ in range(500):
        cps = []
        for _ in range(rng.randint(0, 5)):
            cp = rng.randint(0, 0x10FFFF)
            while 0xD800 <= cp <= 0xDFFF:
                cp = rng.randint(0, 0x10FFFF)
            cps.append(cp)
        s = "".join(chr(c) for c in cps)
        assert ns["t_str_utf8_bytes"](s) == list(s.encode("utf-8")), repr(s)


# ------------------------------------------------- control chars (item 181)

def test_str_newline_is_exactly_lf(ns):
    # the portable newline is a ONE-char string whose only code point is U+000A —
    # NOT the two-char backslash-n a plain `"\n"` literal would carry.
    nl = ns["t_str_newline"]()
    assert nl == "\n"
    assert len(nl) == 1
    assert ord(nl[0]) == 10


def test_str_tab_exact_code_point(ns):
    tab = ns["t_str_tab"]()
    assert tab == "\t" and len(tab) == 1 and ord(tab[0]) == 9


@pytest.mark.parametrize("code", [0, 1, 8, 9, 10, 11, 12, 13, 27, 31, 127])
def test_str_char_control_round_trips(ns, code):
    # every control character, CR (13) and NUL (0) included, is one code
    # point: the pure table covers 9/10 and `Int.to_char` the rest (#2285).
    ch = ns["t_str_char"](code)
    assert ch == chr(code)
    assert ns["t_str_char_code"](code) == code


def test_str_crlf_is_cr_then_lf(ns):
    assert ns["t_str_crlf"]() == "\r\n"


def test_str_char_10_equals_str_newline(ns):
    assert ns["t_str_char"](10) == ns["t_str_newline"]()


@pytest.mark.parametrize("code", list(range(32, 127)))
def test_str_char_printable_ascii_round_trips(ns, code):
    # str_char is the reverse of charCodeAt across all printable ASCII:
    # str_char(code).charCodeAt(0) == code, and equals Python chr(code).
    assert ns["t_str_char"](code) == chr(code)
    assert ns["t_str_char_code"](code) == code


@pytest.mark.parametrize("code", [128, 0xE9, 0x7FF, 0x800, 0xFFFD, 0x1F600,
                                  0x10FFFF])
def test_str_char_beyond_ascii_round_trips(ns, code):
    assert ns["t_str_char"](code) == chr(code)
    assert ns["t_str_char_code"](code) == code


# ------------------------------------------------- issue #3452: case and lines

@pytest.mark.parametrize("s,exp", [
    ("AbC", "abc"),
    ("", ""),
    ("aBc9-_", "abc9-_"),        # digits/punct pass through; ASCII only
    ("\u00c9A", "\u00c9a"),    # non-ASCII passes through unchanged (stated ASCII)
])
def test_lower(ns, s, exp):
    assert ns["t_lower"](s) == exp


@pytest.mark.parametrize("s,exp", [
    ("aBc9", "ABC9"),
    ("", ""),
    ("AbC-_", "ABC-_"),
])
def test_upper(ns, s, exp):
    assert ns["t_upper"](s) == exp


def _crlf_text(ns):
    # real CR and LF code points, built through str_char (a plain revl string
    # carries no escapes): "a" CR LF "b" CR "c".
    cr = ns["t_str_char"](13)
    lf = ns["t_str_char"](10)
    return "a" + cr + lf + "b" + cr + "c"


def test_lines_splits_lf_crlf_and_lone_cr(ns):
    ls = ns["t_lines"](_crlf_text(ns))
    assert ls == ["a", "b", "c"]          # the issue's acceptance: three elements


def test_lines_semantics_match_splitlines(ns):
    cr = ns["t_str_char"](13)
    lf = ns["t_str_char"](10)
    assert ns["t_lines"]("") == []
    assert ns["t_lines"]("x" + lf + lf) == ["x", ""]
    assert ns["t_lines"]("x" + cr) == ["x"]
    assert ns["t_lines"]("x" + cr + lf) == ["x"]


def test_str_char_13_is_a_real_cr(ns):
    ch = ns["t_str_char"](13)
    assert len(ch) == 1                   # the issue's acceptance
    assert ord(ch[0]) == 13


@pytest.mark.parametrize("s,i,exp", [
    ("hello", 2, "llo"),
    ("hello", 0, "hello"),
    ("hello", 5, ""),
    ("hello", 99, ""),                    # clamped, not a fault
    ("hello", -5, "hello"),               # clamped at 0
])
def test_slice_from(ns, s, i, exp):
    assert ns["t_slice_from"](s, i) == exp


@pytest.mark.parametrize("code", [-1, 0xD800, 0xDFFF, 0x110000])
def test_str_char_non_scalar_returns_empty(ns, code):
    # a value that is not a Unicode scalar (negative, a surrogate, above
    # U+10FFFF) returns "" rather than faulting; documented in
    # docs/stdlib-str.md and the same on every tier.
    assert ns["t_str_char"](code) == ""


def test_join_with_str_newline_produces_real_newlines(ns):
    # the exact `lines.join("\n")`-wanting-a-real-newline shape from the item.
    joined = ns["t_join_newline"](["a", "b", "c"])
    assert joined == "a\nb\nc"
    assert joined.split("\n") == ["a", "b", "c"]


# ---------------------------------------------------------------- e2e py run

def test_pure_revl_program_compiles_and_runs_on_py(ns):
    # the whole point: a pure-revl program that only `use`s the kit executes on
    # the py tier and produces the hand-rolled helpers' results.
    assert ns["t_trim"]("  spec  ") == "spec"
    assert ns["t_ident_tokens"]("Map[Str, Int]") == ["Map", "Str", "Int"]
    assert ns["t_dedent"]("    a\n      b\n") == "a\n  b\n"
    assert ns["type_args"]("Result[Row, Err]") == ["Row", "Err"]
