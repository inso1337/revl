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


# ---------------------------------------------------------------- the pairs
# (label, emitted spelling, hand-written spelling, argument sample)
def pairs():
    rec = {"x": 3, "y": 4}
    return [
        ("bounded Int `a + b`  (emit.py:2302)",
         lambda a, b: _revl_i64(a + b),
         lambda a, b: (v if I64_MIN <= (v := a + b) <= I64_MAX else _trap(v)),
         [(3, 4), (-9, 2), (1 << 40, 1 << 40)]),

        ("truncated `a % b`  (emit.py:2322)",
         lambda a, b: (lambda _a, _b: abs(_a) % abs(_b) if _a >= 0
                       else -(abs(_a) % abs(_b)))(a, b),
         lambda a, b: _rem(a, b),
         [(7, 3), (-7, 3), (7, -3)]),

        ("record field read `p.x`  (emit.py:2394/1173)",
         lambda p: _revl_field(p, 'x'),
         lambda p: p['x'],
         [(rec,)]),

        ("match Some/None  (emit.py:2256)",
         lambda o: (lambda match: (
             (lambda v: v)(match) if match is not None
             else (0 if match is None
                   else (_ for _ in ()).throw(TypeError('x')))))(o),
         lambda o: (match if (match := o) is not None else 0),
         [(5,), (None,)]),

        # `??` duplicates its LEFT OPERAND textually, so the cost (and the
        # observable double effect) only appears when that operand is a call.
        ("`f(x) ?? b`  (emit.py:2295/1182)",
         lambda x, b: (b if _side(x) is None else _side(x)),
         lambda x, b: (v if (v := _side(x)) is not None else b),
         [(5, 0), (None, 0)]),

        # `Opt[T]` is `T | None`, so `Some` IS the identity; the emitter still
        # builds a function object for it and enters a frame to apply it.
        ("`Some(x)`  (emit.py:2285/1155)",
         lambda x: (lambda _v: _v)(x),
         lambda x: x,
         [(5,)]),

        ("Map.remove  (emit.py:658)",
         lambda m, k: dict((kk, vv) for kk, vv in m.items() if kk != k),
         lambda m, k: {kk: vv for kk, vv in m.items() if kk != k},
         [({"a": 1, "b": 2, "c": 3}, "b")]),

        ("Str.indexOf  (emit.py:604)",
         lambda v, n: (lambda _v, _n: _v.find(_n) if isinstance(_v, str)
                       else (_v.index(_n) if _n in _v else -1))(v, n),
         lambda v, n: v.find(n) if isinstance(v, str) else (
             v.index(n) if n in v else -1),
         [("hello world", "wor"), ("abc", "z")]),

        ("Str.split  (emit.py:608)",
         lambda v, s: (lambda _v, _s: list(_v) if _s == "" else _v.split(_s))(v, s),
         lambda v, s: list(v) if s == "" else v.split(s),
         [("a,b,c", ","), ("abc", "")]),
    ]


def main() -> int:
    print("per-construct cost, ONE evaluation, deterministic counters")
    print("E = the cordis-py emitter's spelling, H = the hand-written one\n")
    print(f"{'construct':<44}{'ops E/H':>18}{'calls E/H':>16}")
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
        print(f"{label:<44}{f'{oe:.1f}/{oh:.1f}':>18}{f'{ce:.1f}/{ch:.1f}':>16}")
    SIDE_EFFECTS[0] = 0
    for _ in range(100):
        (0 if _side(7) is None else _side(7))
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
