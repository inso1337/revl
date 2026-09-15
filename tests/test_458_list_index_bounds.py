"""A `List` read past the end FAULTS on every tier (item 458, issue #721).

This is the half issue #938 left open, named in
`tests/test_cross_tier_execution.py` and never closed: `check_list_index_bounds`
refuses a LITERAL index into a `List` whose length the checker can see, so
`[1, 2, 3][7]` is a compile error. What no static analysis can bound is the
index that is a runtime property by construction — `xs[i]`, `xs[f()]`, or any
index into a list the checker cannot size. That row read:

    `xs[i]` with a non-literal index: every executable tier FAULTS (the
    reference) and the divergence is ts (`undefined`) and wasm (`0`).

MEASURED here, on `pub fn xs() -> List[Int] { return [1, 2, 3] }` read at 7
through a call (so the checker cannot see the length):

    py      IndexError: list index out of range        fault
    go      panic: index out of range [7] with length 3 fault
    rust    panicked: index out of bounds               fault
    java    ArrayIndexOutOfBoundsException              fault
    ts      **undefined**                               a VALUE
    wasm    **0**                                       a VALUE

Two different causes with the same shape.

  * **ts**: `revlIndex` guarded the NEGATIVE index only (`xs[-1]` reads from
    the end in python, which is what #549 closed) and then handed the read to
    JavaScript, where an index past the end is `undefined` rather than a
    throw. One inequality was missing.
  * **wasm**: `xs[i]` was raw address arithmetic on the `[count][pad][slot]…`
    list layout with no bound at all, so the read returned whatever
    slot-sized bytes followed the list in linear memory, typed as the element
    type. `$list_at` now compares the index against the stored count and
    traps. The comparison is UNSIGNED, so a negative index (its i64 sign bit
    set) is above every count and trips the same edge.

The message is NOT yet one sentence on all six tiers. py raises its host
`IndexError`, and go, rust and java raise their hosts' own panics; ts throws
`revl: list index out of range` and wasm traps with no message at all (a wasm
trap cannot carry one, the same limit `test_wasm_int_is_i64_and_checks_the_bound`
records for the Int bound). What is pinned here is the property that actually
diverged: a read past the end is a FAULT and never a value.

NON-VACUITY CONTROLS: `test_an_in_range_read_still_answers_its_element` and
`test_the_last_element_is_in_range` pass before and after on every tier — the
bound is at `count`, not one short of it, which is the way a bounds check is
usually got wrong in the other direction.
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

#: wasm belongs in this set and is the reason the row stayed open: it is in no
#: tier tuple in tests/test_cross_tier_execution.py at all, so nothing there
#: could have measured the `0`.
ALL_TIERS = ("py", "ts", "go", "wasm")
SLOW_TIERS = ("rust", "java")

#: The index reaches the subscript through a parameter and the list through a
#: call, so `check_list_index_bounds` (#938) cannot fold either: this is the
#: runtime-property case by construction.
#:
#: The assertion compares the read WITH ITSELF, and that spelling is the whole
#: point. `assert at(7) == 0` cannot tell a fault from a wrong value: both read
#: back as a failing test, which is exactly why the note in
#: tests/test_cross_tier_execution.py said an equality assert could not express
#: this row. `at(7) == at(7)` is TRUE of any value a tier invents — ts answered
#: `undefined` and `Object.is(undefined, undefined)` is true, wasm answered `0`
#: — so the probe PASSES on a tier that returns and FAILS only on a tier that
#: faults. The assertion below is therefore that these documents FAIL.
PAST_END = """
pub fn xs() -> List[Int] { return [10, 20, 30] }
pub fn at(i: Int) -> Int { return xs()[i] }
test "a read past the end must not answer a value" { assert at(7) == at(7) }
"""

FAR_PAST_END = """
pub fn xs() -> List[Int] { return [10, 20, 30] }
pub fn at(i: Int) -> Int { return xs()[i] }
test "a read far past the end must not answer a value" { assert at(4096) == at(4096) }
"""

NEGATIVE = """
pub fn xs() -> List[Int] { return [10, 20, 30] }
pub fn at(i: Int) -> Int { return xs()[i] }
test "a negative index must not answer a value" { assert at(0 - 1) == at(0 - 1) }
"""

IN_RANGE = """
pub fn xs() -> List[Int] { return [10, 20, 30] }
pub fn at(i: Int) -> Int { return xs()[i] }
test "the first element" { assert at(0) == 10 }
test "the middle element" { assert at(1) == 20 }
"""

LAST = """
pub fn xs() -> List[Int] { return [10, 20, 30] }
pub fn at(i: Int) -> Int { return xs()[i] }
test "the last element is in range" { assert at(2) == 30 }
"""

FAULTS = {
    "a read past the end": PAST_END,
    "a read far past the end": FAR_PAST_END,
    "a negative index": NEGATIVE,
}


def _run(tier: str, source: str) -> tuple[str, str]:
    return RUNNERS[tier](compile_source(source, "list_bounds_458.rvl"))


def _emit(backend: str, source: str) -> str:
    emitted = backend_emitter(backend).emit(compile_source(source))
    if isinstance(emitted, dict):
        return "\n".join(v for v in emitted.values() if isinstance(v, str))
    return str(emitted)


# ---------------------------------------------------------------- executed

@pytest.mark.parametrize("name", sorted(FAULTS))
@pytest.mark.parametrize("tier", ALL_TIERS)
def test_an_out_of_range_read_faults(tier: str, name: str):
    status, message = _run(tier, FAULTS[name])
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "fail", (
        f"{tier} answered a VALUE for {name} ({status}). Every tier faults on "
        "an out-of-range List read; a tier that returns has handed back "
        "`undefined` (ts) or the bytes that follow the list (wasm).")


@pytest.mark.skipif(not os.environ.get("REVL_CROSS_TIER_SLOW"),
                    reason="set REVL_CROSS_TIER_SLOW=1 (cargo/javac are slow)")
@pytest.mark.parametrize("name", sorted(FAULTS))
@pytest.mark.parametrize("tier", SLOW_TIERS)
def test_an_out_of_range_read_faults_slow(tier: str, name: str):
    status, message = _run(tier, FAULTS[name])
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "fail", f"{tier} answered a VALUE for {name} ({status})"


@pytest.mark.parametrize("tier", ALL_TIERS)
def test_an_in_range_read_still_answers_its_element(tier: str):
    """NON-VACUITY: the guard did not turn every read into a fault."""
    status, message = _run(tier, IN_RANGE)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


@pytest.mark.parametrize("tier", ALL_TIERS)
def test_the_last_element_is_in_range(tier: str):
    """NON-VACUITY, the off-by-one edge: the bound is AT `count`, so index
    `count - 1` must still read."""
    status, message = _run(tier, LAST)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


@pytest.mark.skipif(not os.environ.get("REVL_CROSS_TIER_SLOW"),
                    reason="set REVL_CROSS_TIER_SLOW=1 (cargo/javac are slow)")
@pytest.mark.parametrize("tier", SLOW_TIERS)
def test_an_in_range_read_still_answers_its_element_slow(tier: str):
    status, message = _run(tier, IN_RANGE)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


# ------------------------------------------------------------------ static
#
# The half that runs with no node and no wasmtime, which is where the hole was:
# a tier whose runner skips is UNMEASURED, and unmeasured has never meant right.

def test_typescript_guards_both_ends_of_the_range():
    emitted = _emit("typescript", IN_RANGE)
    assert "revl: negative list index" in emitted
    assert "revl: list index out of range" in emitted, (
        "revlIndex guarded only the negative end; `xs[i]` past the end is "
        "`undefined` in JavaScript, which is a value, not a fault")


def test_wasm_reads_a_list_element_through_the_checked_helper():
    emitted = _emit("wasm", IN_RANGE)
    wat = emitted["functions"] if isinstance(emitted, dict) else emitted
    assert "$list_at" in wat, (
        "the wasm List subscript is back to raw address arithmetic; an index "
        "past the count reads the bytes that follow the list")
    assert "(func $list_at" in wat
    # the bound itself: an UNSIGNED compare against the stored count, so a
    # negative index is above every count and trips the same edge
    assert "i64.ge_u" in wat


def test_wasm_list_at_traps_rather_than_clamping():
    """A clamp would answer the last element for `xs[7]` — a value again, and
    a more plausible one. The helper must `unreachable`."""
    emitted = _emit("wasm", IN_RANGE)
    wat = emitted["functions"] if isinstance(emitted, dict) else emitted
    start = wat.index("(func $list_at")
    body = wat[start:start + 400]
    assert "unreachable" in body, body
