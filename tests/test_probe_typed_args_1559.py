"""A placement probe's arguments are typed values, not strings (issue #1559).

WHAT WAS WRONG. The rust and go placement runners take their probes
structured, from `placement._parse_probe`, and that sent every argument as a
STRING. With `ops.run(n) = n * 10`, a go or rust consumer's probe
`ops.run(41)` answered 10: the runner decoded `"41"` into an `Int` parameter,
failed silently, and ran with 0. py, node and java parse the probe text
themselves and were not on this path.

WHAT IT DOES NOW. Each argument is a typed JSON value: the literal's own type,
converted to the parameter type the operation declares in the IR. A literal
that cannot be that type is refused by name. The runtime proofs are
backends/go/test_probe_typed_args_go.py and
backends/rust/test_probe_typed_args_rust.py; these are the toolchain-free half.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.placement import _parse_probe  # noqa: E402

SOURCE = """
service S { fn put(k: Str, n: Int, f: Float, on: Bool) -> Int }
component P provides s: S { provide s { fn put(k, n, f, on) { return n } } }
service Ops { fn run(x: Int) -> Int }
component C requires s: S provides ops: Ops {
  provide ops { fn run(x) { return s.put("a", x, 1.5, true) * 10 } }
}
"""
IR = compile_source(SOURCE, "probe.rvl")


def test_an_int_argument_is_an_int():
    assert _parse_probe("ops.run(41)", IR) == {"key": "ops", "method": "run", "args": [41]}


def test_every_scalar_takes_its_declared_type():
    probe = _parse_probe("s.put('a, b', 7, 2, true)", IR)
    assert probe["args"] == ["a, b", 7, 2.0, True]
    assert [type(a) for a in probe["args"]] == [str, int, float, bool]


def test_a_required_key_is_typed_from_the_service_it_requires():
    """`s` is provided by P and required by C: either side names the service."""
    assert _parse_probe("s.put(\"k\", \"41\", 0.5, false)", IR)["args"] == ["k", 41, 0.5, False]


def test_a_str_parameter_keeps_a_number_as_text():
    assert _parse_probe("s.put(42, 1, 1, true)", IR)["args"][0] == "42"


def test_a_literal_the_declared_type_cannot_hold_is_refused_by_name():
    with pytest.raises(RuntimeError, match="argument 'many' is not a Int"):
        _parse_probe("ops.run(many)", IR)


def test_without_the_ir_the_literal_keeps_its_own_type():
    assert _parse_probe("k.m('x', 3, 2.5, false)")["args"] == ["x", 3, 2.5, False]
