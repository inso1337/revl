"""What `${...}` renders, on every tier (item 458, issue #721).

Item 458's defect class is a construct that COMPILES on all six tiers and
MEANS something different on one of them. Interpolation had four faces of it
at once, and every one was found by EXECUTING the same document everywhere
rather than by reading the emitters.

MEASURED on this machine before the fix (py is the reference tier):

  `${b}` for a `Bool` b                 ts/go/rust/java `true` ·
                                        **py `True`** · wasm refused by name
  `${f}` for a `Float` PARAMETER at 3.0 ts/go `3` · **py `3.0`** ·
                                        **rust `3`** ok, **java `3.0`**
  `${f}` for a `Float` PARAMETER at     ts/go `1e-7` · **py `1e-07`** ·
  1e-7                                  **rust `0.0000001`** · **java `1.0E-7`**
  `${f}` at 1e21                        ts/go `1e+21` · **rust
                                        `1000000000000000000000`** ·
                                        **java `1.0E21`**
  `(5e-324).to_str()`                   py/ts/go/rust `5e-324` ·
                                        **java `4.9e-324`**
  `${xs}` for a `List[Int]` [1, 2]      py `[1, 2]` · ts `1,2` · go `[1 2]` ·
                                        java `[1, 2]` · **rust DOES NOT
                                        COMPILE** (E0277, `Vec<i64>` has no
                                        `Display`) · wasm refused by name
  `${p}` for a record {x: 1, y: 2}      py `{'x': 1, 'y': 2}` ·
                                        ts `[object Object]` · rust E0277
  `${o}` for `Opt[Int]`                 py `1`/`None` · ts `1`/`undefined` ·
                                        rust E0277

THREE CAUSES, and they are one cause seen three ways.

  1. **The backends GUESSED the operand's type from the node's own shape.**
     `_is_float_expr` (python, java) and `_v3_is_float` (rust) answer `True`
     for a float literal, a `/`, a Float-annotated `bin` and a unary minus of
     one. That proof cannot see a `Float` that arrives through a parameter, a
     local, a field or a call — and the fallback it drops to is the HOST's
     default rendering, which is not revl's. Its own docstring said a false
     answer "only costs the default `str()`, which is already correct for a
     non-Float"; for a Float the proof could not see, that was wrong.
     The frontend knows the type, so `lower.py` now WRITES IT DOWN:
     a `${...}` operand carries `interp_type` for `Float` and `Bool`, the two
     whose host default rendering is not revl's. `Str` and `Int` stay tag-less
     (the same discipline `to_str`'s `recv` follows), so the IR for the common
     template is byte-identical to what it was.
  2. **python rendered a `Bool` with `str()`**, which is `True`/`False`.
     revl spells its Bool literals `true` and `false`, and the other four
     executing tiers already printed them that way.
  3. **revl defines no rendering for a compound value at all**, so each host
     supplied its own and rust supplied a compile error. That is refused at
     the frontend now: one diagnostic instead of six answers downstream. The
     wasm tier had been refusing every one of them by name all along.

Separately, `revlFtoa` on java reformatted `Double.toString`'s digits, and
`Double.toString` never emits fewer than two significant digits — so
`Double.MIN_VALUE` came out `4.9E-324` where the shortest decimal that parses
back to it is `5e-324`. The helper now tries the one-digit form first, which
is the only length `Double.toString` can be too long for.

NON-VACUITY CONTROLS, which carry the weight here:
`test_an_int_template_carries_no_annotation` and
`test_a_str_template_carries_no_annotation` prove the annotation names two
types rather than being stamped on everything;
`test_a_syntactically_provable_float_is_still_proved_by_shape` proves the
node-local proof still answers on its own, so hand-written IR in the
backend-ir dialect keeps working; and
`test_wasm_still_refuses_a_bool_template_by_name` proves the close did not
turn an honest refusal into a silent lowering. All pass before and after.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from revl import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.test import RUNNERS  # noqa: E402

FAST_TIERS = ("py", "ts", "go")
SLOW_TIERS = ("rust", "java")

#: Every scalar template, rendered through a call so no tier can constant-fold
#: the operand back into a literal the shape proof would recognise.
SCALARS = """
pub fn idf(f: Float) -> Float { return f }
pub fn idb(b: Bool) -> Bool { return b }
pub fn idi(n: Int) -> Int { return n }
pub fn ids(s: Str) -> Str { return s }

pub fn tf(f: Float) -> Str { return `${f}` }
pub fn tb(b: Bool) -> Str { return `${b}` }
pub fn ti(n: Int) -> Str { return `${n}` }
pub fn ts(s: Str) -> Str { return `${s}` }

test "true is the revl literal" { assert tb(idb(true)) == "true" }
test "false is the revl literal" { assert tb(idb(false)) == "false" }
test "an integral Float drops its point" { assert tf(idf(3.0)) == "3" }
test "a small Float keeps the canonical exponent" { assert tf(idf(1.0e-7)) == "1e-7" }
test "a large Float keeps the canonical exponent" { assert tf(idf(1.0e21)) == "1e+21" }
test "a fractional Float is shortest round-trip" { assert tf(idf(0.1)) == "0.1" }
test "an Int is its digits" { assert ti(idi(0 - 7)) == "-7" }
test "a Str is itself" { assert ts(ids("hi")) == "hi" }
"""

#: `${f}` and `f.to_str()` are the same renderer reached two ways, so they must
#: agree at the ends of the double range as well as in the middle.
FLOAT_AGREEMENT = """
pub fn idf(f: Float) -> Float { return f }
pub fn tf(f: Float) -> Str { return `${f}` }
pub fn sf(f: Float) -> Str { return f.to_str() }

test "the smallest subnormal is one digit" { assert sf(idf(5.0e-324)) == "5e-324" }
test "and the template agrees with it" { assert tf(idf(5.0e-324)) == sf(idf(5.0e-324)) }
test "the largest double is shortest round-trip" {
  assert sf(idf(1.7976931348623157e308)) == "1.7976931348623157e+308"
}
test "and the template agrees there too" {
  assert tf(idf(1.7976931348623157e308)) == sf(idf(1.7976931348623157e308))
}
test "a third agrees both ways" { assert tf(idf(1.0 / 3.0)) == sf(idf(1.0 / 3.0)) }
"""

PROBES = {
    "every scalar renders the same": SCALARS,
    "Float agrees at both ends of the range": FLOAT_AGREEMENT,
}

#: Each compound operand, and the type name the refusal must contain.
REFUSED = {
    "List[Int]": 'pub fn f(xs: List[Int]) -> Str { return `${xs}` }\n',
    "Map[Str, Int]": 'pub fn f(m: Map[Str, Int]) -> Str { return `${m}` }\n',
    "Opt[Int]": 'pub fn f(o: Opt[Int]) -> Str { return `${o}` }\n',
    "Result[Int, Str]": 'pub fn f(r: Result[Int, Str]) -> Str { return `${r}` }\n',
    "P": 'type P = { x: Int }\npub fn f(p: P) -> Str { return `${p}` }\n',
    "S": 'type S = A(Int) | B\npub fn f(s: S) -> Str { return `${s}` }\n',
}


def _run(tier: str, source: str) -> tuple[str, str]:
    return RUNNERS[tier](compile_source(source, "interp_458.rvl"))


def _interp_types(source: str) -> list:
    """The `interp_type` each `${...}` operand carries, in document order."""
    ir = compile_source(source, "interp_458.rvl")
    found: list = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("kind") == "interp":
                for part in node.get("parts") or []:
                    if isinstance(part, (list, tuple)) and part[0] == "expr":
                        operand = part[1]
                        found.append(operand.get("interp_type")
                                     if isinstance(operand, dict) else None)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(ir)
    return found


# ---------------------------------------------------------------- executed
#
# The only way this class is found: every probe below emitted cleanly on the
# tier that got it wrong.

@pytest.mark.parametrize("name", sorted(PROBES))
@pytest.mark.parametrize("tier", FAST_TIERS)
def test_templates_render_the_same_on_the_fast_tiers(tier: str, name: str):
    status, message = _run(tier, PROBES[name])
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} disagrees about {name!r}: {message}"


@pytest.mark.skipif(not os.environ.get("REVL_CROSS_TIER_SLOW"),
                    reason="set REVL_CROSS_TIER_SLOW=1 (cargo/javac are slow)")
@pytest.mark.parametrize("name", sorted(PROBES))
@pytest.mark.parametrize("tier", SLOW_TIERS)
def test_templates_render_the_same_on_the_slow_tiers(tier: str, name: str):
    status, message = _run(tier, PROBES[name])
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} disagrees about {name!r}: {message}"


# ------------------------------------------------------- the IR annotation
#
# Static, so it runs with no toolchain at all — which is the half that would
# have caught this on a machine with no node, no cargo and no JDK.

def test_a_float_operand_carries_its_type():
    source = 'pub fn f(x: Float) -> Str { return `${x}` }\n'
    assert _interp_types(source) == ["Float"]


def test_a_bool_operand_carries_its_type():
    source = 'pub fn f(b: Bool) -> Str { return `${b}` }\n'
    assert _interp_types(source) == ["Bool"]


def test_an_int_template_carries_no_annotation():
    """NON-VACUITY. `str()`/`{}`/`String.valueOf` are already right for an Int
    on every tier, so the tag is not attached and the IR is unchanged."""
    source = 'pub fn f(n: Int) -> Str { return `${n}` }\n'
    assert _interp_types(source) == [None]


def test_a_str_template_carries_no_annotation():
    """NON-VACUITY, the second half: two types are named, not all of them."""
    source = 'pub fn f(s: Str) -> Str { return `${s}` }\n'
    assert _interp_types(source) == [None]


def test_a_float_through_a_field_and_a_call_is_seen_too():
    """The positions the node-local proof is blind to, which is the point."""
    source = (
        'type P = { r: Float }\n'
        'pub fn half() -> Float { return 0.5 }\n'
        'pub fn f(p: P) -> Str { return `${p.r} ${half()}` }\n'
    )
    assert _interp_types(source) == ["Float", "Float"]


# ---------------------------------------------------- the node-local proof

def test_a_syntactically_provable_float_is_still_proved_by_shape():
    """NON-VACUITY. The backends keep their own proof and consult it FIRST, so
    an IR document written by hand in the backend-ir dialect — which carries no
    annotation — still renders a Float through the canonical helper.
    """
    from _backend_import import backend_emitter

    ir = compile_source('pub fn f(a: Float, b: Float) -> Str { return `${a / b}` }',
                        "interp_458.rvl")

    def strip(node):
        if isinstance(node, dict):
            node.pop("interp_type", None)
            for value in node.values():
                strip(value)
        elif isinstance(node, list):
            for value in node:
                strip(value)

    strip(ir)
    for backend, needle in (("python", "_revl_ftoa("),
                            ("rust", "revl_ftoa("),
                            ("java", "revlFtoa(")):
        emitted = backend_emitter(backend).emit(ir)
        text = ("\n".join(v for v in emitted.values() if isinstance(v, str))
                if isinstance(emitted, dict) else str(emitted))
        assert needle in text, (
            f"{backend} lost the canonical Float renderer once the annotation "
            "was stripped; the node-local proof must still answer on its own")


# ------------------------------------------------------ the compound refusal

@pytest.mark.parametrize("type_name", sorted(REFUSED))
def test_a_compound_operand_is_refused_by_name(type_name: str):
    with pytest.raises(RevlError) as excinfo:
        compile_source(REFUSED[type_name], "interp_458.rvl")
    message = str(excinfo.value)
    assert "cannot interpolate" in message, message
    assert type_name in message, message


def test_a_structural_record_operand_is_refused_too():
    """An anonymous literal has no nominal name, so it is caught by its shape
    (item 71's structural record type) rather than by a declaration lookup."""
    source = 'pub fn f() -> Str { let a = { h: 1 }  return `${a}` }\n'
    with pytest.raises(RevlError) as excinfo:
        compile_source(source, "interp_458.rvl")
    assert "cannot interpolate a record" in str(excinfo.value)


def test_a_scalar_operand_is_not_refused():
    """NON-VACUITY. The refusal names what it can prove compound; it does not
    reject every template."""
    source = ('pub fn f(s: Str, n: Int, x: Float, b: Bool) -> Str '
              '{ return `${s}${n}${x}${b}` }\n')
    compile_source(source, "interp_458.rvl")


# ------------------------------------------------------------ the wasm tier

def test_wasm_still_refuses_a_bool_template_by_name():
    """NON-VACUITY, and the one that matters most: this tier answered nothing
    wrong because it answered nothing at all. Pinned so a later Bool lowering
    cannot land without revisiting the rendering above."""
    from _backend_import import backend_emitter

    ir = compile_source('pub fn f(b: Bool) -> Str { return `${b}` }', "interp_458.rvl")
    with pytest.raises(Exception) as excinfo:
        backend_emitter("wasm").emit(ir)
    assert "template interpolates" in str(excinfo.value)
