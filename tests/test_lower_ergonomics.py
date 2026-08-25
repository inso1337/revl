"""Lowering-stage ergonomics broadenings (roadmap 154 + 155).

Both changes are pure *acceptance broadenings*: a program that compiled before
still compiles and emits byte-identically; only previously-rejected shapes
become accepted. The rejection guards these invert live in
`tests/test_frontend.py` (the `v2_var_in_record.rvl` fixture was removed; its
shape now lives here) and `tests/test_v2_emit.py`
(`test_var_by_value_in_record_literal_accepted`).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from _backend_import import backend_emitter  # noqa: E402
from revl import RevlError, compile_source  # noqa: E402

emit = backend_emitter("python")


def _emit(source):
    return emit.emit(compile_source(source))


def _run(source):
    ns = {}
    exec(compile(_emit(source), "emitted.py", "exec"), ns)
    return ns


# ---------------------------------------------------------------------------
# Item 154 — a bare `var` read is allowed by value in a record literal.
# ---------------------------------------------------------------------------

def test_bare_var_in_record_literal_copies_by_value():
    # The record captures n's value at construction; mutating n afterwards does
    # not change what the record holds — proving it is a value copy, not a live
    # reference to the mutable cell (so the `var` still never escapes, §3.5).
    ns = _run(
        """
        fn snapshot() -> Int {
          var n = 1
          let row = { value: n }
          n = 99
          return row.value
        }
        """
    )
    assert ns["snapshot"]() == 1


def test_var_field_read_in_record_literal_still_works():
    # The field-read form (`{ x: v.field }`) was always allowed and is unchanged.
    ns = _run(
        """
        type Pt = { x: Int, y: Int }
        fn origin_x() -> Int {
          var p = { x: 7, y: 8 }
          let row = { first: p.x }
          return row.first
        }
        """
    )
    assert ns["origin_x"]() == 7


def test_bare_var_record_emits_identically_to_let_record():
    # Byte-identity guard for 154: a bare `var` in a record literal lowers with
    # NO special handling — its emission is byte-for-byte the emission of the
    # same program written with a `let` (the only pre-154 sanctioned form). The
    # relaxation added acceptance, not a new code path.
    var_form = _emit(
        """
        fn f() -> Int {
          var n = 1
          let row = { value: n }
          return row.value
        }
        """
    )
    let_form = _emit(
        """
        fn f() -> Int {
          let n = 1
          let row = { value: n }
          return row.value
        }
        """
    )
    assert var_form == let_form


# ---------------------------------------------------------------------------
# Item 155 — `let` is block-scoped, so disjoint sibling blocks may reuse a name.
# ---------------------------------------------------------------------------

def test_sibling_if_else_arms_may_reuse_a_name():
    ns = _run(
        """
        fn pick(x: Int) -> Int {
          if (x > 0) {
            let y = x + 1
            return y
          } else {
            let y = 0 - x
            return y
          }
        }
        """
    )
    assert ns["pick"](5) == 6
    assert ns["pick"](-3) == 3


def test_let_in_arm_does_not_leak_past_the_if():
    # A `let` inside an arm no longer leaks into the enclosing scope, so a
    # same-named `let` after the `if` is fine. (This is the exact shape the old
    # `v2_duplicate_let_block_scope.rvl` fixture rejected before item 155.)
    ns = _run(
        """
        fn pick(x: Int) -> Int {
          if (x > 0) {
            let y = x
            return y
          }
          let y = 0
          return y
        }
        """
    )
    assert ns["pick"](5) == 5
    assert ns["pick"](-1) == 0


def test_match_arms_may_reuse_a_bind_name():
    ns = _run(
        """
        type R = Ok(Int) | Err(Int)
        fn h(r: R) -> Int {
          return match r {
            Ok(v) => v + 1,
            Err(v) => v - 1,
          }
        }
        """
    )
    assert ns["h"](ns["Ok"](10)) == 11
    assert ns["h"](ns["Err"](10)) == 9


def test_same_linear_scope_redeclaration_still_refused():
    # The broadening does NOT touch redeclaration in one straight-line scope.
    with pytest.raises(RevlError, match="`y` is already declared in this function"):
        compile_source(
            """
            fn f() -> Int {
              let y = 1
              let y = 2
              return y
            }
            """
        )


def test_redeclaration_within_a_single_arm_still_refused():
    with pytest.raises(RevlError, match="`y` is already declared in this function"):
        compile_source(
            """
            fn f(x: Int) -> Int {
              if (x > 0) {
                let y = 1
                let y = 2
                return y
              }
              return 0
            }
            """
        )


# an ordinary if/else with unique names — captured from the pre-155 emitter.
_FROZEN_IF_ELSE = (
    '"""Generated by the revl cordis-py backend (ir_version 3) — do not edit.\n'
    "\n"
    "Components: \n"
    '"""\n'
    "\n"
    "_REVL_I64_MIN = -(2 ** 63)\n"
    "_REVL_I64_MAX = 2 ** 63 - 1\n"
    "\n"
    "def _revl_i64(v):\n"
    '    """Int is bounded 64-bit and overflow traps. python is arbitrary"""\n'
    '    """precision, so the bound is imposed here."""\n'
    "    if v < _REVL_I64_MIN or v > _REVL_I64_MAX:\n"
    "        raise OverflowError('revl: Int overflow')\n"
    "    return v\n"
    "\n"
    "def _revl_field(v, name):\n"
    '    """Record literals are dicts, ADT payloads are objects."""\n'
    "    return v[name] if isinstance(v, dict) else getattr(v, name)\n"
    "\n"
    "def classify(x):\n"
    "    if (x > 0):\n"
    "        hi = _revl_i64(x + 1)\n"
    "        return hi\n"
    "    else:\n"
    "        lo = _revl_i64(0 - x)\n"
    "        return lo\n"
    "\n"
    "SERVICES = {\n"
    "}\n"
    "\n"
    "\n"
    "COMPONENTS = {}\n"
)


def test_ordinary_if_else_emits_byte_identically():
    # Byte-identity guard for 155: an if/else with unique names — a program that
    # compiled before the block-scoping change — still emits exactly the same
    # bytes. Per-arm scope snapshots gate acceptance only; they do not perturb
    # emission of programs that never reused a name.
    out = _emit(
        """
        fn classify(x: Int) -> Int {
          if (x > 0) {
            let hi = x + 1
            return hi
          } else {
            let lo = 0 - x
            return lo
          }
        }
        """
    )
    assert out == _FROZEN_IF_ELSE
