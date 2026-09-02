"""Item 130 Slice 1 — `Stream[T]` reactive types: admission + lowering + refusals.

The tier-agnostic half of the item (the runtime PROOF is the py suite at
backends/python/tests/test_stream_runtime.py). This suite pins:

* the surface parses and the subscription bracket lowers with the additive
  `subscribe`/`policy` IR keys and an awaited `next` (design §5);
* the six admission rules refuse the shapes the core guarantee forbids
  (§3, §9) — subscribe-needs-undo, single-consumer, no-silent-vanish provider,
  a non-suspending teardown, a non-source operand, and stream type-formation;
* the py emitter renders the cancellation-first bracket, and wasm REFUSES with
  the honest EmitError (§4.6, exit test §10.8).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.errors import RevlError, RevlErrors  # noqa: E402


def _tier_emit(tier: str):
    path = ROOT / "backends" / tier / "emit.py"
    spec = importlib.util.spec_from_file_location(f"_emit_{tier}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(path.parent))
    return mod


_CONSUMER = """
component C {
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src undo sub.close()
  await sub.next()
}
"""


def _refusal(src: str) -> str:
    with pytest.raises((RevlError, RevlErrors)) as excinfo:
        compile_source(src, "s.rvl")
    return str(excinfo.value)


# ---------------------------------------------------------------------------
# Lowering: the subscription bracket IR (design §5)
# ---------------------------------------------------------------------------

def test_subscribe_lowers_to_a_bracket_step_with_additive_keys():
    ir = compile_source(_CONSUMER, "s.rvl")
    body = ir["components"][0]["body"]
    sub = next(s for s in body if s.get("subscribe"))
    assert sub["step"] == "let-effect", "a subscription is an ordinary bracket step"
    assert sub["subscribe"] is True and sub["policy"] == "error"
    assert sub["acquire"]["kind"] == "subscribe"
    assert sub["acquire"]["stream"] == {"kind": "name", "id": "src"}
    # the inverse is a synchronous `close` on the subscription handle
    assert sub["undo"] == {"kind": "call",
                           "target": {"kind": "name", "id": "sub"},
                           "method": "close", "args": []}
    # `next` is an awaited suspension step
    awaited = next(s for s in body if s.get("step") == "await")
    assert awaited["expr"]["method"] == "next"
    # a subscription carries no `async` acquisition flag (subscribe is sync)
    assert "async" not in sub


def test_non_stream_program_is_byte_identical():
    """Byte-identity (§10.9): a program that uses no streams lowers exactly as
    before — the additive keys never appear."""
    plain = """
    component C {
      let pool = effect Pool.open("u", 4) undo pool.close()
    }
    """
    body = compile_source(plain, "s.rvl")["components"][0]["body"]
    assert all("subscribe" not in step and step.get("policy") is None
               for step in body)


# ---------------------------------------------------------------------------
# Admission rules (§3, §9)
# ---------------------------------------------------------------------------

def test_rule_3_2_subscribe_needs_undo():
    msg = _refusal("""
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src
    }
    """)
    assert "subscribe" in msg and "undo" in msg


def test_rule_3_1_single_consumer_refuses_a_second_subscribe():
    msg = _refusal("""
    component C {
      let src = effect Stream.source() undo src.close()
      let a = subscribe src undo a.close()
      let b = subscribe src undo b.close()
      await a.next()
    }
    """)
    assert "already subscribed" in msg and "single-consumer" in msg


def test_rule_3_6_refuses_a_provider_that_can_vanish_without_a_terminal():
    """The §9 Part B rule the core guarantee rests on: a stream source whose
    inverse does not CLOSE it can leave an outstanding `next` with no terminal."""
    msg = _refusal("""
    component C {
      let other = effect Pool.open("u", 1) undo other.close()
      let src = effect Stream.source() undo other.close()
      let sub = subscribe src undo sub.close()
      await sub.next()
    }
    """)
    assert "vanish without delivering a terminal" in msg


def test_rule_3_4_teardown_may_not_suspend_on_next():
    """`close` is the synchronous bracket inverse — a `next` in the `undo` slot
    is a suspending teardown, refused (§3.4)."""
    msg = _refusal("""
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.next()
      await sub.next()
    }
    """)
    assert "next" in msg and "synchronous" in msg


def test_subscribe_refuses_a_non_source_operand():
    msg = _refusal("""
    component C {
      let pool = effect Pool.open("u", 1) undo pool.close()
      let sub = subscribe pool undo sub.close()
      await sub.next()
    }
    """)
    assert "stream source" in msg


def test_rule_3_6_subscription_undo_that_closes_nothing_is_refused():
    """The subscription half of rule 3.6, and the one the SOURCE half never
    covered: the source's inverse was shape-checked (it must `close` the
    source), the subscription's was not. A pure no-op `undo` left the listener
    attached to a live source after its owner reached DISPOSED — the core
    guarantee ("unloading its owner CLOSES the stream before the owner
    disappears") read backwards."""
    msg = _refusal("""
    extern pure fn nop(x: Str) -> Unit = @py { return None }
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo nop("x")
      await sub.next()
    }
    """)
    assert "must close THAT subscription" in msg
    assert "`sub`" in msg


def test_rule_3_6_subscription_undo_that_closes_the_source_is_refused():
    """Closing the SOURCE is not closing the subscription. It looks plausible
    (the trace even shows a `stream.source close`) and it is exactly wrong:
    `stream.close` never runs, the subscription stays `_closed = False`, and it
    is still attached to the source it just tore down under itself."""
    msg = _refusal("""
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo src.close()
      await sub.next()
    }
    """)
    assert "must close THAT subscription" in msg


def test_rule_3_6_subscription_undo_closing_a_SIBLING_is_refused():
    """The copy-paste shape: a second subscription whose `undo` still names the
    first one's handle. Both brackets typecheck, one subscription is closed
    twice and the other never."""
    msg = _refusal("""
    component C {
      let s1 = effect Stream.source() undo s1.close()
      let a = subscribe s1 undo a.close()
      let s2 = effect Stream.source() undo s2.close()
      let b = subscribe s2 undo a.close()
      await b.next()
    }
    """)
    assert "must close THAT subscription" in msg
    assert "`b`" in msg


def test_the_correct_subscription_inverse_still_admits():
    """No over-refusal: `undo <sub>.close()` — the one shape the design
    specifies — lowers exactly as before."""
    ir = compile_source(_CONSUMER, "s.rvl")
    sub = next(s for s in ir["components"][0]["body"] if s.get("subscribe"))
    assert sub["undo"] == {"kind": "call",
                           "target": {"kind": "name", "id": "sub"},
                           "method": "close", "args": []}


def test_bare_subscribe_must_be_bound():
    msg = _refusal("""
    component C {
      let src = effect Stream.source() undo src.close()
      subscribe src undo src.close()
    }
    """)
    assert "must be bound" in msg


# ---------------------------------------------------------------------------
# Stream type formation (§1)
# ---------------------------------------------------------------------------

def test_stream_type_rejects_bad_arity_and_state():
    assert "1 or 2 type arguments" in _refusal(
        "service S { fn f() -> Stream[A, Active, X] }")
    assert "must be a state" in _refusal(
        "service S { fn f() -> Stream[A, Bogus] }")


def test_stream_state_index_parses():
    # a well-formed state-indexed stream type is accepted in a signature
    ir = compile_source("service S { fn f() -> Stream[Order, Active] }", "s.rvl")
    assert "S" in ir["services"]


# ---------------------------------------------------------------------------
# Emission: py renders the bracket; wasm REFUSES (§4.6, exit test §10.8)
# ---------------------------------------------------------------------------

def test_python_emits_the_cancellation_first_bracket():
    code = _tier_emit("python").emit(compile_source(_CONSUMER, "s.rvl"))
    # the subscription opens with the owner ctx (so a parked `next` sees
    # withdrawal), and the bracket inverse yields `close` on the next line
    assert "sub = Stream.subscribe(src, 'error', _revl_ctx)" in code
    assert "yield lambda: sub.close()" in code
    # `next` is awaited inside the async body generator
    assert "async def _body()" in code
    assert "await sub.next()" in code


def test_wasm_refuses_a_stream_program_with_an_honest_emit_error():
    emit = _tier_emit("wasm")
    with pytest.raises(emit.EmitError) as excinfo:
        emit.emit(compile_source(_CONSUMER, "s.rvl"))
    msg = str(excinfo.value)
    assert "suspends a fiber" in msg
    assert "Job.run" in msg and "backend py" in msg
