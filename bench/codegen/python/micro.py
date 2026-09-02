#!/usr/bin/env python3
"""Per-construct cost: what ONE evaluation of an emitted spelling costs, next
to the spelling a competent Python developer uses for the same semantics.

`run.py` measures whole programs, which is what matters, but it cannot tell you
how much of a gap a single emitter decision is worth. This file can: each row
is one construct the cordis-py backend emits, paired with a drop-in replacement
that preserves the semantics exactly, and both are measured with the same
deterministic counters (`ops` = executed bytecode instructions, `calls` =
Python-level calls). No clock is involved, so the numbers are identical on
every machine at any load.

Each pair also asserts that the two spellings AGREE on a sample of inputs, so a
row cannot claim a saving by quietly changing behaviour.

    python bench/codegen/python/micro.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run import count_calls, count_ops  # noqa: E402

I64_MIN = -(2 ** 63)
I64_MAX = 2 ** 63 - 1
REPS = 200


def _trap(v):
    raise OverflowError('revl: Int overflow')


def _revl_i64(v):
    if v < I64_MIN or v > I64_MAX:
        raise OverflowError('revl: Int overflow')
    return v


def _revl_field(v, name):
    return v[name] if isinstance(v, dict) else getattr(v, name)


SIDE_EFFECTS = [0]


def _side(x):
    """Stands in for any call on the left of `??`: it counts its own
    invocations, which is what makes the duplication observable."""
    SIDE_EFFECTS[0] += 1
    return x


def _rem(a, b):
    return abs(a) % abs(b) if a >= 0 else -(abs(a) % abs(b))


# The preamble helpers the emitter now hoists each multi-expression builtin
# into (item 436 F6), copied verbatim from `backends/python/emit.py`.
def _revl_rem(a, b):
    return abs(a) % abs(b) if a >= 0 else -(abs(a) % abs(b))


def _revl_index_of(v, n):
    if isinstance(v, str):
        return v.find(n)
    return v.index(n) if n in v else -1


def _revl_split(v, s):
    return list(v) if s == "" else v.split(s)


# ---------------------------------------------------------------- the pairs
# (label, emitted spelling, hand-written spelling, argument sample)
#
# The E arm is the CURRENT emitted spelling, kept in step with
# `backends/python/emit.py` by hand. A row that reads 1.0/1.0 is a finding that
# has been closed: the emitter now writes what the developer writes.
def pairs():
    rec = {"x": 3, "y": 4}
    return [
        # item 436 F5: the bound is imposed inline, so the in-range answer —
        # every answer a correct program produces — costs no Python frame.
        ("bounded Int `a + b`  (emit.py::_bounded)",
         lambda a, b: (_bi if I64_MIN <= (_bi := a + b) <= I64_MAX
                       else _revl_i64(_bi)),
         lambda a, b: (v if I64_MIN <= (v := a + b) <= I64_MAX else _trap(v)),
         [(3, 4), (-9, 2), (1 << 40, 1 << 40)]),

        # item 436 F6: a preamble `def`, not a lambda built and applied here.
        ("truncated `a % b`  (emit.py::_render_builtin)",
         lambda a, b: _revl_rem(a, b),
         lambda a, b: _rem(a, b),
         [(7, 3), (-7, 3), (7, -3)]),

        # item 436 F4: the shape dispatch is inline, so the frame is gone; the
        # `isinstance` that is left is what only a frontend marker can remove.
        ("record field read `p.x`  (emit.py::_field_read)",
         lambda p: (_fv['x'] if isinstance((_fv := p), dict)
                    else getattr(_fv, 'x')),
         lambda p: p['x'],
         [(rec,)]),

        # item 436 F3: the scrutinee bind rides the first arm's test. The
        # PAYLOAD bind still rides a lambda — see the item for why.
        ("match Some/None  (emit.py::_match_expr)",
         lambda o: ((lambda v: v)(match) if (match := o) is not None
                    else (0 if match is None
                          else (_ for _ in ()).throw(TypeError('x')))),
         lambda o: (match if (match := o) is not None else 0),
         [(5,), (None,)]),

        # item 436 C1/F7: the left operand is bound once by a walrus, so it is
        # evaluated once — `_side` counts its own invocations to prove it.
        ("`f(x) ?? b`  (emit.py::_opt_bind)",
         lambda x, b: (_ov1 if (_ov1 := _side(x)) is not None else b),
         lambda x, b: (v if (v := _side(x)) is not None else b),
         [(5, 0), (None, 0)]),

        # item 436 F8: `Opt[T]` is `T | None`, so the argument IS the answer.
        ("`Some(x)`  (emit.py::_expr call arm)",
         lambda x: x,
         lambda x: x,
         [(5,)]),

        # item 436 F2: a dict comprehension, not `dict(<generator>)`.
        ("Map.remove  (emit.py::_render_builtin)",
         lambda m, k: {kk: vv for kk, vv in m.items() if kk != k},
         lambda m, k: {kk: vv for kk, vv in m.items() if kk != k},
         [({"a": 1, "b": 2, "c": 3}, "b")]),

        # item 436 F6: a preamble `def`. The frame that is LEFT is the receiver
        # dispatch (Str or List), which the IR node does not carry a type for.
        ("Str.indexOf  (emit.py::_render_builtin)",
         lambda v, n: _revl_index_of(v, n),
         lambda v, n: v.find(n) if isinstance(v, str) else (
             v.index(n) if n in v else -1),
         [("hello world", "wor"), ("abc", "z")]),

        ("Str.split  (emit.py::_render_builtin)",
         lambda v, s: _revl_split(v, s),
         lambda v, s: list(v) if s == "" else v.split(s),
         [("a,b,c", ","), ("abc", "")]),
    ]


def main() -> int:
    print("per-construct cost, ONE evaluation, deterministic counters")
    print("E = the cordis-py emitter's spelling, H = the hand-written one\n")
    print(f"{'construct':<48}{'ops E/H':>14}{'calls E/H':>16}")
    for label, emitted, hand, samples in pairs():
        for args in samples:
            if emitted(*args) != hand(*args):
                raise AssertionError(f"{label}: the two spellings disagree")
        args = samples[0]

        def drive(f, args=args):
            return lambda: [f(*args) for _ in range(REPS)]

        oe = (count_ops(drive(emitted)) - count_ops(drive(lambda *a: None))) / REPS
        oh = (count_ops(drive(hand)) - count_ops(drive(lambda *a: None))) / REPS
        ce = count_calls(drive(emitted)) / REPS
        ch = count_calls(drive(hand)) / REPS
        print(f"{label:<48}{f'{oe:.1f}/{oh:.1f}':>14}{f'{ce:.1f}/{ch:.1f}':>16}")
    # item 436 C1/F7 stays pinned: the emitted `??` binds its left operand
    # ONCE. This ran 200 for 100 uses before the walrus landed.
    SIDE_EFFECTS[0] = 0
    for _ in range(100):
        (_ov1 if (_ov1 := _side(7)) is not None else 0)
    emitted_evals = SIDE_EFFECTS[0]
    SIDE_EFFECTS[0] = 0
    for _ in range(100):
        (v if (v := _side(7)) is not None else 0)
    print(f"\n`f(x) ?? b` over 100 non-None results: the emitted spelling "
          f"evaluates the left operand {emitted_evals} times, "
          f"the hand-written one {SIDE_EFFECTS[0]}.")
    print("`ops` is net of the driver loop; `calls` counts Python frames"
          " entered per evaluation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
