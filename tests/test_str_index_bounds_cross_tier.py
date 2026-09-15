"""`charAt` / `charCodeAt` / `codepoint_at` outside the string, on all six tiers.

The cross-tier divergence hunt, string family. `docs/stdlib-2.0.md`
§Str.codepoint_at states the contract in one line — "the index is assumed **in
bounds** (`0 <= i < length()`)" — and nothing anywhere enforces it. An
assumption that no layer checks is not a contract, it is six behaviours, and
these three builtins had four of them at once.

The List side of this exact question was decided years ago and is written down:
a negative or out-of-range `xs[i]` FAULTS on every tier, python guards the read
that would otherwise wrap (`_revl_index`), and issue #938 refuses the index the
checker can bound (docs/contract-errata.md, "A negative list index diverged").
`Str` character access is the same question — an index into a sequence — and it
never got the same answer.

Measured on this branch's parent, one document per cell, six tiers each, with
`s = "a😀b"` (3 code points, so 3 is the first index past the end):

| probe                    | py       | ts     | go    | rust  | java   | wasm        |
|--------------------------|----------|--------|-------|-------|--------|-------------|
| `s.charAt(3)` (at end)   | fault    | **""** | fault | fault | **""** | **""**      |
| `s.charAt(9)` (past end) | fault    | **""** | fault | fault | **""** | **""**      |
| `"".charAt(0)`           | fault    | **""** | fault | fault | **""** | **""**      |
| `s.charAt(-1)`           | **"b"**  | **""** | fault | fault | **""** | **""**      |
| `s.charCodeAt(3)`        | fault    | fault  | fault | fault | fault  | **a byte**  |
| `s.charCodeAt(9)`        | fault    | fault  | fault | fault | fault  | **a byte**  |
| `"".charCodeAt(0)`       | fault    | fault  | fault | fault | fault  | **a byte**  |
| `s.charCodeAt(-1)`       | **98**   | fault  | fault | fault | fault  | **97**      |
| `s.codepoint_at(9)`      | fault    | fault  | fault | fault | fault  | **a byte**  |
| `s.codepoint_at(-1)`     | **98**   | fault  | fault | fault | fault  | **97**      |

Every bolded cell is one of four shapes:

  * **ts and java hand back the empty string.** Both `revlCharAt` bodies say so
    in as many words — TypeScript `return c === undefined ? "" : c`, java
    `if (i < 0 || i >= len) { return ""; }`. `""` is a perfectly good `Str`, so
    a program that walks one index too far reads a character that is not there,
    compares it, concatenates it, and finishes with a wrong answer and no
    diagnostic anywhere. The other four tiers fault on the same input.

  * **python reads from the END at a negative index.** `charAt`/`charCodeAt`/
    `codepoint_at` lower to `s[i]` / `ord(s[i])`, and python indexing is
    end-relative, so `s.charAt(-1)` is `"b"` on the REFERENCE tier and a fault
    on go, rust and java. This is verbatim the `xs[-1]` divergence #549 closed
    for `List` — closed there by making python fault, because "JS bracket
    indexing is not end-relative" and end-relative reads have no cross-tier
    reading. The `Str` twin was never closed.

  * **wasm reads one byte PAST the string's own bytes.** `$str_cp_offset`
    answers the byte LENGTH when the code-point index is at or after the end
    (it is written that way on purpose, because `$str_cp_slice` needs a
    clamping offset), and `$str_cp_char_code_at` then does `i32.load8_u` at
    that offset — a read of whatever sits after the string in linear memory.
    It is not a constant `0`: `test_wasm_past_the_end_read_saw_the_next_value`
    below builds the layout where the byte after the string is the next
    allocation's length prefix, and the pre-fix answer was that prefix.

  * **wasm answers the FIRST code point at a negative index.**
    `$str_cp_offset`'s walk stops on `seen >= cp`, which is already true at
    `seen == 0` for any negative `cp`, so the offset comes back `0` and the
    read lands on index 0. `s.charCodeAt(-1)` was `97` (`"a"`) on wasm and `98`
    (`"b"`) on python — two different wrong answers to the same question.

THE CLOSE is the one the `List` side already took: an index outside
`0 <= i < length()` FAULTS on every tier, for all three of `charAt`,
`charCodeAt` and `codepoint_at`. python guards the read it would otherwise wrap
(`_revl_str_at`, the shape `_revl_index` already has), ts and java throw
`revl: Str index out of range` where they returned `""`, wasm traps through a
strict offset helper, and go and rust already faulted natively and are
unchanged — the same "three fault natively, the rest are made to" split #549
settled on. For a position that may be past the end, `slice`-then-guard is
still the total form, exactly as docs/stdlib-2.0.md already says.

WHICH TIERS EXECUTE. py, ts, go and wasm run by default; rust (cargo) and java
(javac + JVM) sit behind `REVL_CROSS_TIER_SLOW`. A `*_slow` skip means
UNMEASURED and has never meant passing, so both slow tiers also get a cheap
STATIC reading of their emitted helper text, which runs everywhere.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.test import RUNNERS  # noqa: E402

FAST_TIERS = ("py", "ts", "go", "wasm")
SLOW_TIERS = ("rust", "java")

# `"a😀b"` is 3 code points and 6 UTF-8 bytes, so an index of 3 is the first
# one past the end under the documented unit (docs/strings.md) while 3, 4 and 5
# are all still inside the byte buffer — which is what made the wasm read land
# on real memory rather than trap. Both strings travel through a `pub fn` so no
# tier's constant folder can answer the access at compile time.
_PRELUDE = """
pub fn s() -> Str { return "a😀b" }
pub fn e() -> Str { return "" }
"""

# Each entry has NO in-bounds answer. The probe asserts only that the access
# produced SOME value (`d() == d()` is trivially true of any Str or Int), so a
# tier that PASSES has silently answered and a tier that faults fails it. That
# inversion is what `assert status == "fail"` is written for.
OUT_OF_RANGE = {
    "charAt at the end":            ("Str", "s().charAt(3)"),
    "charAt past the end":          ("Str", "s().charAt(9)"),
    "charAt on the empty string":   ("Str", "e().charAt(0)"),
    "charAt at a negative index":   ("Str", "s().charAt(0 - 1)"),
    "charCodeAt at the end":        ("Int", "s().charCodeAt(3)"),
    "charCodeAt past the end":      ("Int", "s().charCodeAt(9)"),
    "charCodeAt on the empty str":  ("Int", "e().charCodeAt(0)"),
    "charCodeAt negative index":    ("Int", "s().charCodeAt(0 - 1)"),
    "codepoint_at at the end":      ("Int", "s().codepoint_at(3)"),
    "codepoint_at past the end":    ("Int", "s().codepoint_at(9)"),
    "codepoint_at on the empty str":("Int", "e().codepoint_at(0)"),
    "codepoint_at negative index":  ("Int", "s().codepoint_at(0 - 1)"),
}

# The NON-VACUITY control. Every index is inside `0 <= i < length()`, including
# both ends and the astral char in the middle, so this passes on both sides of
# the fix. It is here to prove the probes above are measuring the BOUND and not
# "character access is broken", and to catch a fix that bought the edge by
# breaking the middle — a guard that is off by one on the last index, or one
# that measures in UTF-8 bytes instead of code points and so rejects index 2 of
# a 3-code-point / 6-byte string.
IN_RANGE_CONTROL = _PRELUDE + """
test "charAt reads the first code point"  { assert s().charAt(0) == "a" }
test "charAt reads the astral middle"     { assert s().charAt(1) == "😀" }
test "charAt reads the LAST code point"   { assert s().charAt(2) == "b" }
test "charCodeAt at the first index"      { assert s().charCodeAt(0) == 97 }
test "charCodeAt at the astral middle"    { assert s().charCodeAt(1) == 128512 }
test "charCodeAt at the LAST index"       { assert s().charCodeAt(2) == 98 }
test "codepoint_at at the first index"    { assert s().codepoint_at(0) == 97 }
test "codepoint_at at the astral middle"  { assert s().codepoint_at(1) == 128512 }
test "codepoint_at at the LAST index"     { assert s().codepoint_at(2) == 98 }
test "slice-then-guard stays TOTAL"       { assert s().slice(9, 10) == "" }
test "slice past the end is empty"        { assert e().slice(0, 1) == "" }
test "a walk over every index agrees"     { assert walk() == 3 }
pub fn walk() -> Int {
  var i = 0
  var n = 0
  while (i < s().length()) { n += 1
    if (s().charCodeAt(i) != s().codepoint_at(i)) { return 0 - 1 }
    i += 1 }
  return n
}
"""

# The wasm read that landed on the NEXT allocation. `mk()` is 4 bytes of heap
# Str; `nxt()` is allocated straight after it and is 10 bytes long, so the byte
# one past `mk()`'s own bytes is the low byte of `nxt()`'s i32 length prefix.
# Pre-fix, `mk().charCodeAt(4)` answered `10` on wasm — not a constant, the
# neighbour. Post-fix it traps, so this document faults.
WASM_ADJACENCY = """
pub fn mk() -> Str { return "ab".concat("cd") }
pub fn nxt() -> Str { return "QRSTUVWX".concat("YZ") }
pub fn d() -> Int { let a = mk()
  let b = nxt()
  return a.charCodeAt(4) + b.length() * 0 }
test "the access answered at all" { assert d() == d() }
"""

_SLOW = pytest.mark.skipif(
    not os.environ.get("REVL_CROSS_TIER_SLOW"),
    reason="set REVL_CROSS_TIER_SLOW=1 (cargo/javac are slow)")


def _run(tier: str, source: str):
    return RUNNERS[tier](compile_source(source, "str_index_bounds.rvl"))


def _emit(backend: str, source: str) -> str:
    spec = importlib.util.spec_from_file_location(
        f"str_index_bounds_{backend}", ROOT / "backends" / backend / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    emitted = module.emit(compile_source(source, "str_index_bounds.rvl"))
    return emitted if isinstance(emitted, str) else str(emitted)


def _probe_doc(rt: str, expression: str) -> str:
    return (_PRELUDE
            + f"pub fn d() -> {rt} {{ return {expression} }}\n"
            + 'test "the access answered at all" { assert d() == d() }\n')


def _assert_faults(tier: str, name: str, rt: str, expression: str) -> None:
    status, message = _run(tier, _probe_doc(rt, expression))
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "fail", (
        f"{tier}: {name} answered a value ({status}) — the index is outside "
        f"`0 <= i < length()`, where there is no character to read: {message}")


@pytest.mark.parametrize("name,spec", sorted(OUT_OF_RANGE.items()))
@pytest.mark.parametrize("tier", FAST_TIERS)
def test_no_tier_invents_a_character_outside_the_string(tier: str, name: str, spec):
    _assert_faults(tier, name, spec[0], spec[1])


@_SLOW
@pytest.mark.parametrize("name,spec", sorted(OUT_OF_RANGE.items()))
@pytest.mark.parametrize("tier", SLOW_TIERS)
def test_no_tier_invents_a_character_outside_the_string_slow(tier: str, name: str, spec):
    _assert_faults(tier, name, spec[0], spec[1])


@pytest.mark.parametrize("tier", FAST_TIERS)
def test_in_bounds_character_access_still_agrees(tier: str):
    """The control: every index inside the string, both ends included."""
    status, message = _run(tier, IN_RANGE_CONTROL)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


@_SLOW
@pytest.mark.parametrize("tier", SLOW_TIERS)
def test_in_bounds_character_access_still_agrees_slow(tier: str):
    status, message = _run(tier, IN_RANGE_CONTROL)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


def test_wasm_past_the_end_read_saw_the_next_value():
    """The read one byte past a Str's own bytes, with a known neighbour.

    `$str_cp_offset` answers the byte length for an index at or after the end,
    and `$str_cp_char_code_at` loaded from there — so this document answered
    `10`, the length prefix of the allocation that follows `mk()`. It traps
    now, which is the only answer available: there is no character at index 4
    of a 4-code-point string."""
    status, message = _run("wasm", WASM_ADJACENCY)
    if status == "skip":
        pytest.skip(f"wasm: {message}")
    assert status == "fail", (
        f"wasm read past the string's own bytes and answered: {message}")


# ------------------------------------------------------- cheap static guards
#
# A `*_slow` skip means UNMEASURED, so each executed claim a slow tier carries
# gets a static reading of the emitted text as well. These cost nothing and run
# everywhere.

def test_java_char_access_no_longer_hands_back_an_empty_string():
    """`revlCharAt` said `if (i < 0 || i >= len) { return ""; }` — a `Str` that
    is not in the string, for an index that is not in it either."""
    emitted = _emit("java", _probe_doc("Str", "s().charAt(9)"))
    assert 'private static String revlCharAt(String s, long i) {' in emitted
    body = emitted[emitted.index("private static String revlCharAt("):]
    body = body[:body.index("\n    }")]
    assert 'return "";' not in body, body
    assert _REASON in body, body


def test_java_char_code_access_names_the_bound():
    """java's `revlCharCodeAt` faulted already, through `offsetByCodePoints`'s
    own `IndexOutOfBoundsException`. It now names the revl rule instead, so one
    guarantee does not read as six different host errors."""
    emitted = _emit("java", _probe_doc("Int", "s().charCodeAt(9)"))
    assert "private static long revlCharCodeAt(String s, long i) {" in emitted
    body = emitted[emitted.index("private static long revlCharCodeAt("):]
    assert _REASON in body[:body.index("\n    }")], body[:600]


def test_rust_char_access_still_faults_on_its_own():
    """rust needs no guard and gets none: `chars().nth(i)` is `None` past the
    end and a negative `i64 as usize` is a huge index, so both arms reach the
    same `.unwrap()`. The assertion is that the `.unwrap()` is still there —
    a lowering that reached for `unwrap_or_default()` would reintroduce the
    ts/java empty string on the one tier that never had it."""
    emitted = _emit("rust", _probe_doc("Str", "s().charAt(9)"))
    assert ".chars().nth(" in emitted, emitted
    assert "unwrap_or" not in emitted, emitted


def test_every_tier_that_names_the_reason_spells_it_the_same_way():
    """One guarantee should not read as several bugs — the same rule
    `revl: negative list index` and `revl: Int overflow` already carry."""
    for backend in ("python", "typescript", "java"):
        emitted = _emit(backend, _probe_doc("Int", "s().charCodeAt(9)"))
        assert _REASON in emitted, backend


def test_wasm_offset_helper_has_a_strict_and_a_clamping_form():
    """`slice` must CLAMP (a bound past the end is an empty slice, never a
    fault) and character access must TRAP, and both read the same walk. The
    two uses are separate helpers now, so neither can be fixed into the
    other's semantics by accident."""
    wat = _emit("wasm", _probe_doc("Int", "s().charCodeAt(9)"))
    assert "(func $str_cp_offset_strict" in wat, wat[:400]
    strict = wat[wat.index("(func $str_cp_offset_strict"):]
    strict = strict[:strict.index("\n  (func ")]
    assert "unreachable" in strict, strict
    sliced = _emit("wasm", _probe_doc("Str", "s().slice(9, 10)"))
    assert "(func $str_cp_offset " in sliced
    clamping = sliced[sliced.index("(func $str_cp_offset "):]
    clamping = clamping[:clamping.index("\n  (func ")]
    assert "unreachable" not in clamping, clamping


def test_python_guards_the_read_it_would_otherwise_wrap():
    """python's `s[i]` is end-relative, so `charAt(-1)` read the LAST code
    point where go/rust/java/ts faulted — the `_revl_index` story, one type
    over. The guard rides the same preamble mechanism and only appears in a
    document that actually reads a character."""
    emitted = _emit("python", _probe_doc("Str", "s().charAt(9)"))
    assert "def _revl_str_at(s, i):" in emitted, emitted
    assert "_revl_str_at(" in emitted
    untouched = _emit("python", 'pub fn f() -> Int { return "ab".length() }\n'
                                'test "t" { assert f() == 2 }\n')
    assert "_revl_str_at" not in untouched, untouched


_REASON = "revl: Str index out of range"
