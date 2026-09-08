"""Issue #71 / roadmap item 436 — the python codegen-performance findings, as a
STANDING gate rather than a number in a fix note.

The audit at `bench/codegen/python/` measured what `backends/python/emit.py`
emits against the python a developer writes by hand, and its wins landed across
several branches (F1, F2, F5, F6, F8, F9, F3's scrutinee and binder-free arms,
F4's helper frame). Those wins are pinned only where a spelling happens to sit
in a golden; the PROPERTY each finding was about — a per-evaluation frame, a
per-evaluation allocation, an accumulation that grows quadratically — is
checked by nothing on CI. A refactor can preserve every golden line and still
reintroduce a frame or a copy, and the bench is run by hand.

This test makes the audit executable. It drives the REAL emitter through
`bench/codegen/python/run.py` (not `micro.py`, whose emitted arms are hand-kept
and have gone stale before) and asserts, for every program, the finding-shape
counts, the element-copy growth, and the Python frames entered. Every counter
here is deterministic and load-independent (bytecode-, frame- and
copy-counting, no clock), and every assertion is written so it reads the same
on 3.11 and on 3.14: it turns on the presence of a `lambda`/helper call and on
copy COUNTS, never on whether a comprehension is inlined (which is a version
difference, PEP 709).

Two findings are ACCEPTED as permanent micro-perf residuals — issue #71 was
CLOSED on that basis, not because they were fixed — and are asserted at their
accepted value so a change that removes one flips this test on purpose rather
than by accident:

  * F3, the match PAYLOAD bind read from inside a larger body, keeps its
    one-shot lambda. The gain is one interpreter frame per such evaluation, and
    the walrus that would remove it (writing the bind through to an enclosing
    local of the same name, which revl lets an arm bind shadow) is safe ONLY
    when that name is bound nowhere else in the function. The reference emitter
    can decide that from a single module-level tally set at each function entry
    — a clean, small change (verified: it emits the walrus for a unique bind
    and keeps the lambda for the shadow / nested / arrow shapes item 163 pins,
    and the walrus executes to the same value). But `selfhost/emit_py.rvl` has
    NO module state, so byte-agreement (`tests/test_selfhost_emit_py.py`) forces
    the same tally to be THREADED through its `expr` (112 call sites) and
    `cexpr` (52 call sites) recursions plus their helpers, and mirrored as a
    `collect_bound_names` walk over the value_* IR API. That ~180-edit
    deep-recursion signature refactor of the self-host emitter, graded
    byte-exact over the whole corpus (including the arbitrary whole-tree survey
    fixtures), is disproportionate to removing one frame from a micro-benchmark.
    Accepted; the arm-local scope contract stays pinned in
    `tests/test_163_match_payload_bind_scope.py`.
  * F4, the `isinstance(p, dict)` dispatch on every record field read. The
    helper FRAME is gone; the dynamic dispatch stays, because a field read off
    a record-typed value must still tolerate a host object an extern handed
    back under that type (`src/revl/typecheck.py`: an extern's return is
    deliberately opaque). Removing it needs a value-PROVENANCE fact (the value
    was built here, as a dict), not just its declared type — a FRONTEND change
    that is its own tracked follow-up (roadmap item 436 F4), out of scope for a
    codegen-only close. Accepted/deferred.
"""

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "bench" / "codegen" / "python"
if str(BENCH) not in sys.path:
    sys.path.insert(0, str(BENCH))

run = importlib.import_module("run")
copycount = importlib.import_module("copycount")


# One emitted module per program, compiled through the real backend once and
# shared across the tests in this file (the compile + emit is the slow part).
_EMITTED: dict = {}


def _emitted(program: str):
    if program not in _EMITTED:
        _EMITTED[program] = run._load_emitted(program)
    return _EMITTED[program]


def _source(program: str) -> str:
    return _emitted(program).__dict__["__source__"]


def _shape(program: str) -> dict:
    return run.shape_metrics(_source(program))


# --------------------------------------------------------------------------
# F1 / F2 — accumulation is LINEAR, not quadratic.
#
# `xs.push(v)` / `m.set(k, v)` / a record update in a loop used to copy the
# whole container per step (the audit's headline: 500x more elements copied
# than hand-written at n=1000). Every accumulation probe must now copy no more
# than the hand-written arm, and grow at the same exponent it does — the one
# property no golden captures and the whole point of `copycount`.

@pytest.mark.parametrize("label,fn,base,mkargs",
                         [(p[0], p[1], p[2], p[3]) for p in run.COPY_PROBES])
def test_accumulation_never_copies_more_than_handwritten(label, fn, base, mkargs):
    program = run.COPY_PROGRAM[label]
    emitted = _emitted(program)
    hw = run._load_handwritten()
    e_rc = run._instrument_emitted(program, emitted)
    h_rc = run._instrument_handwritten()
    e_fn, h_fn = getattr(e_rc, fn), getattr(h_rc, fn)

    # copycount must not have bought a count with a behaviour change.
    copycount.verify(e_fn, getattr(emitted, fn), *mkargs(base))
    copycount.verify(h_fn, getattr(hw, fn), *mkargs(base))

    e1 = copycount.measure(e_fn, *mkargs(base))
    e2 = copycount.measure(e_fn, *mkargs(2 * base))
    h1 = copycount.measure(h_fn, *mkargs(base))
    h2 = copycount.measure(h_fn, *mkargs(2 * base))

    # The emitter introduces no copy the hand-written arm does not make.
    assert e1 <= h1, f"{label}: emitted copies {e1} > hand-written {h1} at n={base}"
    assert e2 <= h2, f"{label}: emitted copies {e2} > hand-written {h2} at n={2 * base}"
    # And it grows at the SAME complexity class: a quadratic emitter arm next to
    # a linear hand-written one is exactly the defect F1 was, and it survives an
    # `e <= h` check only until the constant is paid off. Compare exponents.
    e_exp = e2 / max(e1, 1)
    h_exp = h2 / max(h1, 1)
    assert e_exp <= h_exp + 0.25, (
        f"{label}: emitted grows x{e_exp:.1f} when n doubles, "
        f"hand-written x{h_exp:.1f} — a complexity class the emitter introduced")


def test_no_persistent_push_or_map_spread_survives_in_a_loop():
    """The concrete F1 spellings — `(xs + [v])` for a push and `{**m, k: v}` for
    a `Map.set` — must not be what an accumulation loop emits. `list_build` and
    `maps` are pure push / set loops; neither may carry the persistent form."""
    assert _shape("list_build")["persistent-push `+ [`"] == 0, _source("list_build")
    assert _shape("maps")["dict-spread `{**`"] == 0, _source("maps")


# --------------------------------------------------------------------------
# F3 / F6 / F8 — no function object is built and immediately applied.
#
# `shape_metrics` counts `(lambda ` in the emitted source. F6 (eight builtins),
# F8 (`Some(x)`) and F3's scrutinee bind each used to add one; all are gone. The
# ONLY `(lambda ` left in the whole bench is F3's payload bind read inside a
# body, and it lives in exactly one program.

_LAMBDA_FREE = tuple(p for p in run.PROGRAM_ORDER if p != "matching")


@pytest.mark.parametrize("program", _LAMBDA_FREE)
def test_no_lambda_built_and_applied_in_an_expression(program):
    assert _shape(program)["lambda-in-expression"] == 0, _source(program)


def test_str_builtins_are_preamble_helpers_not_per_eval_lambdas():
    """F6 for the Str side: `indexOf` and `split` lower to a gated preamble
    `def` (one frame, no per-evaluation allocation), never a lambda built and
    applied at every call."""
    src = _source("strings")
    assert "(lambda " not in src, src
    assert "_revl_index_of(" in src
    assert "_revl_split(" in src


# --------------------------------------------------------------------------
# F5 — bounded arithmetic's in-range answer costs no Python frame.
#
# `a + b` on `Int` emits the bound inline as a walrus in a conditional's
# condition; `_revl_i64(` stays only as the trapping tail. So the arithmetic
# program enters no more frames than the hand-written arm (which also traps).

def test_bounded_arithmetic_enters_no_extra_frame():
    em, hw = _emitted("arith"), run._load_handwritten()
    e = run.count_calls(lambda: em.sum_squares(2000))
    h = run.count_calls(lambda: hw.sum_squares(2000))
    assert e == h, f"arith.sum_squares: emitted {e} frames vs hand-written {h}"
    # the inline form is present, and it is what carries the in-range answer.
    assert _shape("arith")["inline bound `_bi :=`"] > 0, _source("arith")


# --------------------------------------------------------------------------
# F4 (closed half) — a record field read is INLINE, not a `_revl_field` call.

def test_record_field_read_costs_no_helper_frame():
    src = _source("records")
    assert "_revl_field(" not in src, src
    em, hw = _emitted("records"), run._load_handwritten()
    pts = [{"x": i, "y": i * 2} for i in range(400)]
    e = run.count_calls(lambda: em.total(list(pts)))
    h = run.count_calls(lambda: hw.total(list(pts)))
    assert e == h, f"records.total: emitted {e} frames vs hand-written {h}"


# --------------------------------------------------------------------------
# F9 — a declared record type emits no `@dataclass` nothing constructs, so the
# module LOAD (exec with no fn called) is cheap. `records` declares `Point`.

def test_declared_record_type_emits_no_dataclass():
    assert "@dataclass" not in _source("records"), _source("records")
    assert "import dataclass" not in _source("records")


def test_module_load_enters_only_a_handful_of_frames():
    """F9's cost was paid at import: a `@dataclass` ran generated `__init__` /
    `__repr__` / `__eq__` for a class never instantiated. With it gone the
    LOAD of every bench module enters only a few frames (the preamble helper
    `def`s themselves), never dozens."""
    import types

    for program in run.PROGRAM_ORDER:
        src = _source(program)
        code = compile(src, f"<load {program}>", "exec")

        def go(code=code, program=program):
            module = types.ModuleType(f"load71_{program}")
            module.__dict__["CALLS"] = 0
            sys.modules[module.__name__] = module
            exec(code, module.__dict__)  # noqa: S102

        frames = run.count_calls(go)
        assert frames < 12, f"{program}: module load entered {frames} frames"


# --------------------------------------------------------------------------
# The two ACCEPTED residuals (see the module docstring). Issue #71 was closed
# with these two taxes accepted, not removed, so they are pinned at their
# accepted value: a change that removes one must flip THIS test deliberately,
# and doing so is the signal that the accepted decision has been revisited.

def test_accepted_residual_match_payload_bind_keeps_its_lambda():
    """F3, ACCEPTED. `matching` carries exactly one `(lambda ` — the payload
    bind of `Some(v) => v + 1`, read from inside a larger body — and it costs
    the one extra frame the hand-written arm does not. Removing it is a clean
    change in the reference emitter but a ~180-site threading refactor in the
    stateless `selfhost/emit_py.rvl` to keep byte-agreement, disproportionate to
    one frame per match; accepted per the module docstring. The arm-local scope
    contract the walrus would have to respect stays pinned in test_163."""
    assert _shape("matching")["lambda-in-expression"] == 1, _source("matching")
    em, hw = _emitted("matching"), run._load_handwritten()
    e = run.count_calls(lambda: em.classify(7))
    h = run.count_calls(lambda: hw.classify(7))
    assert e == h + 1, f"matching.classify: emitted {e} frames vs hand-written {h}"


def test_accepted_residual_record_field_read_dispatches_dynamically():
    """F4, ACCEPTED/DEFERRED. The helper frame is gone, but the field read still
    spells the `isinstance(p, dict)` dispatch that only a value-provenance fact
    could remove — a frontend change tracked as its own follow-up (roadmap item
    436 F4), out of scope for a codegen-only close. Pinned so its removal is a
    conscious flip of this test."""
    assert "isinstance(p, dict)" in _source("records"), _source("records")
