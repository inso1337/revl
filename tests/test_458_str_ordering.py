"""`Str` ordering is CODE-POINT order on every tier (item 458, issue #721).

Item 458's defect class is a construct that COMPILES on all six tiers and
BEHAVES DIFFERENTLY on one of them — worse than a refusal, because nothing
tells the author. `tests/test_458_logical_short_circuit.py` pinned the `&&`
face of it. This file pins the ordering face, found the same way: by EXECUTING
the same source everywhere and comparing, not by reading the emitters.

docs/strings.md fixes ONE unit for every string operation: the Unicode code
point. `length`, `charAt`, `slice`, `indexOf` and `split("")` were brought to
it by item 51. Comparison was not, and three separate things were wrong.

MEASURED on this machine before the fix (py is the reference; go and rust
compare UTF-8 bytes, which is the same order as code points):

  `U+FFFF < U+10000`              py true · go true · rust true ·
                                  **ts FALSE** · **java DOES NOT COMPILE** ·
                                  wasm refused by name
  `"a" < "b"` (plain ASCII)       py/go/ts/rust true · **java DOES NOT
                                  COMPILE** · wasm refused by name
  `Map.keys()` with the two keys  py/go/rust/java [U+FFFF, U+10000] ·
                                  **ts [U+10000, U+FFFF]**
  `Map.keys()` of "ab","a","b"    py/go/ts/rust ["a", "ab", "b"] ·
                                  **java ["ab", "a", "b"]**

Three distinct causes, one family:

  - **ts `<` on a string is UTF-16 CODE UNIT order.** It agrees with code-point
    order for every pair below U+FFFF and disagrees at exactly the
    BMP/supplementary boundary: an astral scalar's first UTF-16 unit is a high
    surrogate in D800..DBFF, which sorts BELOW U+E000..U+FFFF. A silent wrong
    answer, at the boundary and nowhere else, which is why ASCII probes never
    saw it.
  - **java has no relational operator on `String` at all.** `(a < b)` reached
    javac as "bad operand types for binary operator '<'". The frontend accepted
    the document, five tiers emitted it, and the sixth would not build — for
    `"a" < "b"`, not only above the BMP. `tests/test_cross_tier_execution.py::
    test_str_ordering_agrees_everywhere` walks FAST_TIERS (py/ts/go) only, so
    java's column was never measured.
  - **both `Map.keys()` comparators had their own bug.** ts split each key into
    code points with `Array.from` and then compared the one-scalar STRINGS with
    `<` — the same code-unit comparison, one level down, so the comparator
    still misordered the exact boundary its comment said it fixed. java's
    tie-break was `Boolean.compare(i >= a.length(), j >= b.length())`, which
    answers `1` for ("a", "ab") and put a key AFTER its own extension.

The close: the frontend marks a relational `bin` whose operands are both `Str`
with `operands: "Str"` — the same additive annotation, on the same key, that
`/` and `+` already carry because a backend cannot tell `Int / Int` from
`Float / Float` from the node alone. ts and java then route through a
`revlStrCmp` that compares SCALARS AS NUMBERS, and both `Map.keys()` sorts
reuse it. py, go and rust are already in code-point order and are untouched.
wasm still refuses the form BY NAME and should keep doing so: a refusal is
honest where a wrong answer is not.

NON-VACUITY CONTROLS, and they carry weight here:
`test_an_int_comparison_still_uses_the_host_operator` proves the routing
discriminates on the annotation rather than rewriting every `<`, and
`test_wasm_still_refuses_str_ordering_by_name` proves the close did not turn
the honest refusal into a silent lowering. Both pass before and after.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from _backend_import import backend_emitter  # noqa: E402
from revl import compile_source  # noqa: E402
from revl.test import RUNNERS  # noqa: E402

FAST_TIERS = ("py", "ts", "go")
SLOW_TIERS = ("rust", "java")

#: U+FFFF is the last BMP scalar and U+10000 the first supplementary one: the
#: single pair where UTF-16 code-unit order and code-point order disagree.
HI_BMP = "￿"
ASTRAL = "\U00010000"

ASTRAL_ORDER = (
    f'pub fn lt() -> Bool {{ return "{HI_BMP}" < "{ASTRAL}" }}\n'
    f'pub fn gt() -> Bool {{ return "{ASTRAL}" > "{HI_BMP}" }}\n'
    'test "U+FFFF sorts before U+10000" { assert lt() }\n'
    'test "and the mirror" { assert gt() }\n'
)

ASCII_ORDER = (
    'pub fn lt() -> Bool { return "a" < "b" }\n'
    'pub fn le() -> Bool { return "a" <= "a" }\n'
    'pub fn gt() -> Bool { return "b" > "a" }\n'
    'pub fn ge() -> Bool { return "Z" >= "Z" }\n'
    'test "a < b" { assert lt() }\n'
    'test "a <= a" { assert le() }\n'
    'test "b > a" { assert gt() }\n'
    'test "Z >= Z" { assert ge() }\n'
)

BOUNDARY_KEYS = (
    'pub fn built() -> Map[Str, Int] {\n'
    '  let m: Map[Str, Int] = Map.empty()\n'
    f'  return m.set("{ASTRAL}", 1).set("{HI_BMP}", 2).set("a", 3)\n'
    '}\n'
    'pub fn k0() -> Str { return built().keys()[0] }\n'
    'pub fn k1() -> Str { return built().keys()[1] }\n'
    'pub fn k2() -> Str { return built().keys()[2] }\n'
    'test "ascii first" { assert k0() == "a" }\n'
    f'test "U+FFFF second" {{ assert k1() == "{HI_BMP}" }}\n'
    f'test "astral last" {{ assert k2() == "{ASTRAL}" }}\n'
)

#: a key and its own extension: the tie-break half of a lexicographic order,
#: which is where java's comparator answered backwards
PREFIX_KEYS = (
    'pub fn built() -> Map[Str, Int] {\n'
    '  let m: Map[Str, Int] = Map.empty()\n'
    '  return m.set("ab", 1).set("a", 2).set("b", 3)\n'
    '}\n'
    'pub fn k0() -> Str { return built().keys()[0] }\n'
    'pub fn k1() -> Str { return built().keys()[1] }\n'
    'pub fn k2() -> Str { return built().keys()[2] }\n'
    'test "a first" { assert k0() == "a" }\n'
    'test "ab second" { assert k1() == "ab" }\n'
    'test "b last" { assert k2() == "b" }\n'
)

STR_LT = 'pub fn lt(a: Str, b: Str) -> Bool { return a < b }\n'
INT_LT = 'pub fn lt(a: Int, b: Int) -> Bool { return a < b }\n'

#: the same comparison in a provide-method body — a SECOND renderer on every
#: tier, and the place item 458's `&&` fix had to be repeated
COMPONENT_LT = """
service Ord { fn lt(a: Str, b: Str) -> Bool }
component O provides ord: Ord {
  provide ord {
    fn lt(a, b) { return a < b }
  }
}
"""

PROBES = {
    "ordering above the BMP": ASTRAL_ORDER,
    "ordering in ASCII": ASCII_ORDER,
    "Map.keys() across the BMP boundary": BOUNDARY_KEYS,
    "Map.keys() puts a prefix before its extension": PREFIX_KEYS,
}


def _run(tier: str, source: str) -> tuple[str, str]:
    return RUNNERS[tier](compile_source(source, "str_ordering_458.rvl"))


def _emit(backend: str, source: str) -> str:
    emitted = backend_emitter(backend).emit(compile_source(source))
    if isinstance(emitted, dict):
        return "\n".join(v for v in emitted.values() if isinstance(v, str))
    return str(emitted)


# ---------------------------------------------------------------- executed
#
# The tiers that can RUN the source, which is the only way this class of defect
# is found: every one of these probes emitted cleanly on the tier that got it
# wrong.

@pytest.mark.parametrize("name", sorted(PROBES))
@pytest.mark.parametrize("tier", FAST_TIERS)
def test_str_ordering_agrees_on_the_fast_tiers(tier: str, name: str):
    status, message = _run(tier, PROBES[name])
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} disagrees about {name!r}: {message}"


@pytest.mark.skipif(not os.environ.get("REVL_CROSS_TIER_SLOW"),
                    reason="set REVL_CROSS_TIER_SLOW=1 (cargo/javac are slow)")
@pytest.mark.parametrize("name", sorted(PROBES))
@pytest.mark.parametrize("tier", SLOW_TIERS)
def test_str_ordering_agrees_on_the_slow_tiers(tier: str, name: str):
    status, message = _run(tier, PROBES[name])
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} disagrees about {name!r}: {message}"


# ------------------------------------------------------- the IR annotation
#
# Static, so it runs with no toolchain at all.

def _relational_operands(source: str) -> list:
    ir = compile_source(source, "str_ordering_458.rvl")
    found: list = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("kind") == "bin" and node.get("op") in ("<", ">", "<=", ">="):
                found.append(node.get("operands"))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(ir)
    return found


def test_a_str_comparison_carries_its_operand_type():
    assert _relational_operands(STR_LT) == ["Str"]


def test_a_str_comparison_in_a_provide_method_carries_it_too():
    # a provide-method body lowers through its own path, and item 458's `&&`
    # fix had to be applied there separately for the same reason
    assert _relational_operands(COMPONENT_LT) == ["Str"]


def test_an_int_comparison_carries_no_str_marking():
    """NON-VACUITY. The annotation names one family, not every comparison."""
    assert _relational_operands(INT_LT) == [None]


# ---------------------------------------------------- the two routed tiers

@pytest.mark.parametrize("backend", ["typescript", "java"])
@pytest.mark.parametrize("source", [STR_LT, COMPONENT_LT],
                         ids=["module-fn", "provide-method"])
def test_str_ordering_routes_through_the_code_point_comparator(backend, source):
    emitted = _emit(backend, source)
    assert "revlStrCmp(a, b) < 0" in emitted, (
        f"{backend} must order two Str values by code point; a bare `a < b` is "
        f"UTF-16 code-unit order on typescript and does not compile at all on "
        f"java:\n{emitted}")


@pytest.mark.parametrize("backend", ["typescript", "java"])
def test_an_int_comparison_still_uses_the_host_operator(backend: str):
    """NON-VACUITY, and the rule that earns the routing: only a `Str`
    comparison needs a helper. Every other relational operand is a scalar its
    host already orders the way revl does, and routing those through a call
    would cost the hot path for nothing."""
    emitted = _emit(backend, INT_LT)
    assert "revlStrCmp" not in emitted, emitted
    assert "(a < b)" in emitted, emitted


@pytest.mark.parametrize("backend", ["python", "go", "rust"])
def test_the_already_correct_tiers_are_untouched(backend: str):
    """NON-VACUITY. python compares code points; go and rust compare UTF-8
    bytes, which is the same order. Nothing to route, and the annotation must
    not have changed what they emit."""
    emitted = _emit(backend, STR_LT)
    assert "revlStrCmp" not in emitted, emitted
    assert "a < b" in emitted, emitted


def test_wasm_still_refuses_str_ordering_by_name():
    """NON-VACUITY, and the point of the whole item: this tier has no string
    comparison to lower, and it says so at emit time. A refusal is honest; the
    close must not have turned it into a silent lowering."""
    with pytest.raises(Exception) as excinfo:
        _emit("wasm", STR_LT)
    assert "relational operator" in str(excinfo.value)


# ------------------------------------------- the comparators, read directly
#
# Both `Map.keys()` comparators were independently wrong, in ways the
# executed probes above pin. These read the emitted helper so a future edit
# that reintroduces either shape is caught without a toolchain.

def test_typescript_compares_scalars_as_numbers_not_as_strings():
    emitted = _emit("typescript", BOUNDARY_KEYS)
    assert "codePointAt(0)" in emitted, emitted
    assert "A[i] < B[i]" not in emitted, (
        "comparing the one-scalar strings with `<` is the same UTF-16 "
        f"code-unit comparison one level down:\n{emitted}")


def test_typescript_map_keys_sorts_with_the_shared_comparator():
    emitted = _emit("typescript", BOUNDARY_KEYS)
    assert ".sort(revlStrCmp)" in emitted, emitted


def test_java_map_keys_sorts_with_the_shared_comparator():
    emitted = _emit("java", BOUNDARY_KEYS)
    assert "revlStrCmp(a, b)" in emitted, emitted
    assert "Boolean.compare(i >= a.length()" not in emitted, (
        "that tie-break answers 1 for (\"a\", \"ab\"), which sorts a key after "
        f"its own extension:\n{emitted}")
