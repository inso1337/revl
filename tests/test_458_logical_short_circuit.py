"""`&&` and `||` SHORT-CIRCUIT on every tier (roadmap item 458, issue #721).

Item 458 asks for straightforward branching to carry *consistent semantics on
every tier*.  The defect this file pins is the bad half of that: a control-flow
form that COMPILES on all six tiers and ANSWERS DIFFERENTLY on one of them,
which is worse than a refusal because nothing tells the author.

`a && b` evaluates `b` only when `a` did not already decide the answer.  Five
tiers inherit that from their host operator — python `and`, typescript/go/rust/
java `&&`.  wasm has no short-circuiting instruction, and the emitter lowered
the operator to `i32.and` / `i32.or`, which are STRICT: both operands run.  So
the three ordinary guard idioms below

    d != 0 && n % d == 0            # `i64.rem_s` traps on a zero divisor
    i < xs.length() && xs[i] > 0    # a slot load past the end of the list
    big && n * n > 0                # the checked `$int_mul` at the Int edge

compiled on all six tiers, returned an answer on five, and TRAPPED on wasm —
the one tier where the guard was the whole point.  The same three shapes are
pinned here in a module `fn` and in a provide-method body, because the wasm
tier lowers those through two different renderers.

The lowering is now a branch, the folded `(if (result i32) …)` skeleton this
tier already uses for the ternary and for `??`, so the three control-flow forms
agree by construction.

NON-VACUITY CONTROL, and it is load-bearing twice over:
`test_the_strict_form_is_kept_when_the_right_operand_cannot_trap` asserts that
`a < b && a <= b` still emits the single `i32.and`.  It proves the assertions
above discriminate (they are not "every `&&` is a branch now"), and it pins the
rule that earns it: the strict form survives exactly where the right operand
provably cannot trap, allocate or touch memory, which is observationally
identical and is what the common comparison chain is.

Execution lives beside this file in `backends/wasm/test_short_circuit_458_exec.py`
(real `wasm-tools` + `wasmtime`).  Everything here is static and needs no
toolchain, so it runs in the `frontend` job and on any laptop.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "tests", ROOT / "backends" / "python"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from _backend_import import backend_emitter  # noqa: E402
from revl import compile_source  # noqa: E402

BACKENDS = ["python", "typescript", "go", "java", "rust", "wasm"]

# ---------------------------------------------------------------------------
# the three guards, each one a right operand that traps when it runs
# ---------------------------------------------------------------------------

#: `(source, fn name, the wasm instruction that traps when the guard fails)`
GUARDS = {
    "divisor": (
        "fn bucket(n: Int, d: Int) -> Int {\n"
        "  if (d != 0 && n % d == 0) { return 1 }\n"
        "  return 0\n"
        "}\n", "bucket", "i64.rem_s"),
    "index": (
        "fn at(xs: List[Int], i: Int) -> Int {\n"
        "  if (i < xs.length() && xs[i] > 0) { return 1 }\n"
        "  return 0\n"
        "}\n", "at", "i64.load"),
    "overflow": (
        "fn ovf(n: Int, big: Bool) -> Int {\n"
        "  if (big && n * n > 0) { return 1 }\n"
        "  return 0\n"
        "}\n", "ovf", "call $int_mul"),
    # the `||` face: the right operand runs only when the left is FALSE
    "divisor-or": (
        "fn either(n: Int, d: Int) -> Int {\n"
        "  if (d == 0 || n % d == 0) { return 1 }\n"
        "  return 0\n"
        "}\n", "either", "i64.rem_s"),
}

#: the same divisor guard in a provide-method body — a different wasm renderer
COMPONENT_GUARD = """
service Bucket { fn pick(n: Int, d: Int) -> Int }
component B provides bucket: Bucket {
  provide bucket {
    fn pick(n, d) {
      if (d != 0 && n % d == 0) { return 1 }
      return 0
    }
  }
}
"""

#: the control: both operands are comparisons of parameters, so neither can
#: trap and the strict single instruction stays correct
TOTAL_GUARD = "fn cmps(a: Int, b: Int) -> Bool { return a < b && a <= b }\n"


def _emit(source: str, backend: str):
    return backend_emitter(backend).emit(compile_source(source))


def _text(emitted) -> str:
    if isinstance(emitted, dict):
        return "\n".join(v for v in emitted.values() if isinstance(v, str))
    return str(emitted)


def _wasm_body(source: str, marker: str) -> str:
    """The WAT of the one emitted function whose header carries `marker`.

    Scoped deliberately: `i32.and` is all over this tier's string/UTF-8
    preamble, so an assertion about the operator lowering must not read the
    whole module.
    """
    lines = _text(_emit(source, "wasm")).splitlines()
    start = next(i for i, line in enumerate(lines)
                 if "(func" in line and marker in line)
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("  (func")), len(lines))
    return "\n".join(lines[start:end])


# ---------------------------------------------------------------------------
# the form compiles everywhere — which is what makes a silent answer gap bad
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("guard", sorted(GUARDS))
def test_every_tier_compiles_the_guard(guard, backend):
    source, _, _ = GUARDS[guard]
    assert _text(_emit(source, backend)).strip()


@pytest.mark.parametrize("backend", BACKENDS)
def test_every_tier_compiles_the_guard_in_a_provide_method(backend):
    assert _text(_emit(COMPONENT_GUARD, backend)).strip()


# ---------------------------------------------------------------------------
# wasm: the trapping operand is reached only through a branch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("guard", sorted(GUARDS))
def test_wasm_reaches_the_trapping_operand_only_under_a_branch(guard):
    source, name, trapping = GUARDS[guard]
    body = _wasm_body(source, name)
    assert trapping in body, f"the {guard} guard should still emit {trapping}"
    assert "(if (result i32)" in body, (
        f"the {guard} guard's right operand traps, so `&&`/`||` must lower to "
        f"a branch on this tier, not to a strict i32.and/i32.or:\n{body}")
    assert "(i32.and)" not in body and "(i32.or)" not in body, (
        f"the strict instruction still joins the {guard} guard:\n{body}")


def test_wasm_branches_in_a_provide_method_body_too():
    # the component path has its own renderer (`_scalar_operator`), in the
    # folded spelling — a guard must not depend on which body it is written in
    body = _wasm_body(COMPONENT_GUARD, "provide:bucket.pick")
    assert "i64.rem_s" in body
    assert "(if (result i32)" in body, body
    assert "(i32.and " not in body, body


# ---------------------------------------------------------------------------
# the control
# ---------------------------------------------------------------------------

def test_the_strict_form_is_kept_when_the_right_operand_cannot_trap():
    body = _wasm_body(TOTAL_GUARD, "cmps")
    assert "(i32.and)" in body, (
        "`a < b && a <= b` has a right operand that provably cannot trap, "
        f"allocate or read memory: evaluating it is observationally identical, "
        f"so the single instruction stays.\n{body}")
    assert "(if (result i32)" not in body, body


# ---------------------------------------------------------------------------
# the other five tiers emit their host's short-circuiting operator
# ---------------------------------------------------------------------------

# `remainder` is the SPELLING of `n % d` on that tier, which is not always the
# operator: python's native `%` floors, so it builds the truncated remainder in
# `_revl_rem`, and rust's `%` panics at `Int.MIN % -1` (whose remainder is 0),
# so it emits `wrapping_rem` — the same operation on every other input, and
# still a panic on a zero divisor.
@pytest.mark.parametrize("backend,spelling,remainder", [
    ("python", " and ", "_revl_rem("),
    ("typescript", " && ", "n % d"),
    ("go", " && ", "n % d"),
    ("rust", " && ", ".wrapping_rem(d)"),
    ("java", " && ", "n % d"),
])
def test_the_hosted_tiers_emit_a_short_circuiting_operator(backend, spelling,
                                                           remainder):
    source, _, _ = GUARDS["divisor"]
    text = _text(_emit(source, backend))
    guard = [line for line in text.splitlines()
             if remainder in line and "if" in line]
    assert len(guard) == 1, f"{backend}: {guard!r}"
    assert spelling in guard[0], f"{backend}: {guard[0]!r}"


def test_the_reference_tier_answers_zero_for_a_zero_divisor():
    """The python tier is the reference: EXECUTE it, so the number the wasm
    execution companion compares against is measured here and not asserted from
    memory."""
    source, _, _ = GUARDS["divisor"]
    namespace: dict = {}
    exec(compile(_text(_emit(source, "python")), "<py-tier>", "exec"), namespace)
    assert namespace["bucket"](7, 0) == 0
    assert namespace["bucket"](6, 3) == 1
    source, _, _ = GUARDS["divisor-or"]
    namespace = {}
    exec(compile(_text(_emit(source, "python")), "<py-tier>", "exec"), namespace)
    assert namespace["either"](7, 0) == 1
    assert namespace["either"](7, 3) == 0
