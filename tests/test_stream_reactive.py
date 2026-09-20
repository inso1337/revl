"""Item 130 — `Stream[T]` reactive types: admission + lowering + refusals.

The tier-agnostic half of the item (the runtime PROOF is the py suite at
backends/python/tests/test_stream_runtime.py). This suite pins:

* the surface parses and the subscription bracket lowers with the additive
  `subscribe`/`policy` IR keys and an awaited `next` (design §5);
* the six admission rules refuse the shapes the core guarantee forbids
  (§3, §9) — subscribe-needs-undo, single-consumer, no-silent-vanish provider,
  a non-suspending teardown, a non-source operand, and stream type-formation;
* the py emitter renders the cancellation-first bracket, and wasm REFUSES with
  the honest EmitError (§4.6, exit test §10.8).

Slice 2 adds the pure combinators (`map`/`filter`/`take`, lowered as
derived-stream STAGES rather than host method calls so the host-verb namespace
does not grow), the declared backpressure policies with their bounded buffer
(§4.4), and the `block`-policy drain window that fires on the deterministic test
clock (§8) — each with its refusal, and each still refused on wasm.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, compile_source  # noqa: E402
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


# ===========================================================================
# Slice 2 — pure combinators, the declared backpressure policies, the clock
# ===========================================================================

_CHAIN = """
component C {
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src.map(x => x * 2).filter(x => x > 2).take(3)
              policy drop_oldest buffer 4 undo sub.close()
  await sub.next()
}
"""


def _subscribe_step(src: str) -> dict:
    body = compile_source(src, "s.rvl")["components"][0]["body"]
    return next(s for s in body if s.get("subscribe"))


# ---------------------------------------------------------------------------
# Combinators lower as DERIVED-STREAM stages, not as host method calls (§1)
# ---------------------------------------------------------------------------

def test_combinator_chain_lowers_to_ordered_pure_stages():
    acquire = _subscribe_step(_CHAIN)["acquire"]
    stages = acquire["stages"]
    assert [s["stage"] for s in stages] == ["map", "filter", "take"], \
        "the chain lowers left to right, one derived stream per link"
    assert stages[0]["fn"]["kind"] == "arrow" and stages[1]["fn"]["kind"] == "arrow"
    assert stages[2]["count"] == 3
    # still ONE bracket: the whole chain rides the subscription's single inverse
    assert _subscribe_step(_CHAIN)["undo"]["method"] == "close"


def test_combinators_add_no_host_verbs():
    """The reason the chain is parsed as stages rather than as `src.map(…)`
    method calls: `map`/`filter`/`take` never enter the shared host-verb
    namespace, whose disjointness from the value-method table is the invariant
    pinned in tests/test_map_value_type.py."""
    from revl.typecheck import _HOST_ARG_SIG, _HOST_FAMILIES

    verbs = set()
    for methods in _HOST_FAMILIES.values():
        verbs |= set(methods)
    assert verbs.isdisjoint({"map", "filter", "take"})
    assert not any(key.split(".")[-1] in ("map", "filter", "take")
                   for key in _HOST_ARG_SIG)
    # and the stage carries no `call` node the host-verb checker would consult
    stages = _subscribe_step(_CHAIN)["acquire"]["stages"]
    assert all(stage.get("fn", {}).get("kind", "arrow") == "arrow"
               for stage in stages)


def test_a_non_combinator_after_the_stream_is_refused_naming_merge():
    msg = _refusal("""
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src.merge(x => x) undo sub.close()
      await sub.next()
    }
    """)
    assert "is not a stream combinator" in msg
    assert "map(f)" in msg and "merge" in msg


def test_rule_3_5_refuses_an_effectful_transform():
    """A `map`/`filter` transform types in PURE mode (G6): the chain is a pure
    derivation whose only effects are the source's bracket and the consumer's
    body (§3.5, §4.7)."""
    msg = _refusal("""
    component C {
      let pool = effect Pool.open("u", 1) undo pool.close()
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src.map(x => pool.query("q")) undo sub.close()
      await sub.next()
    }
    """)
    assert "pool.query" in msg and "combinator is pure" in msg
    assert "move the effect into the consumer body" in msg


def test_take_needs_a_positive_count():
    assert "positive whole count" in _refusal("""
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src.take(0) undo sub.close()
      await sub.next()
    }
    """)


# ---------------------------------------------------------------------------
# Backpressure: the declared policies and the bounded buffer (§4.4)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("policy", ["error", "drop_newest", "drop_oldest", "block"])
def test_every_declared_policy_lowers(policy):
    step = _subscribe_step(f"""
    component C {{
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src policy {policy} undo sub.close()
      await sub.next()
    }}
    """)
    assert step["policy"] == policy and step["acquire"]["policy"] == policy


def test_error_is_the_default_policy_and_the_defaults_stay_absent():
    """`error` is the default — deterministic, no silent loss (§4.4, judgment
    call 2) — and an undeclared buffer/drain/chain leaves the IR exactly as
    Slice 1 lowered it (the additive-keys promise, §5)."""
    step = _subscribe_step(_CONSUMER)
    assert step["policy"] == "error"
    assert "buffer" not in step and "drain" not in step
    assert step["acquire"] == {"kind": "subscribe",
                               "stream": {"kind": "name", "id": "src"},
                               "policy": "error"}


def test_bounded_buffer_capacity_lowers_and_zero_is_refused():
    step = _subscribe_step("""
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src policy drop_newest buffer 2 undo sub.close()
      await sub.next()
    }
    """)
    assert step["buffer"] == 2 and step["acquire"]["buffer"] == 2
    assert "no unbounded buffers" in _refusal("""
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src buffer 0 undo sub.close()
      await sub.next()
    }
    """)


def test_an_unknown_policy_is_refused_naming_the_four():
    msg = _refusal("""
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src policy retry undo sub.close()
      await sub.next()
    }
    """)
    assert "unknown backpressure policy" in msg
    for policy in ("error", "drop_newest", "drop_oldest", "block"):
        assert policy in msg


def test_a_qualifier_may_not_be_declared_twice():
    assert "duplicate `policy`" in _refusal("""
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src policy block policy error undo sub.close()
      await sub.next()
    }
    """)


# ---------------------------------------------------------------------------
# The deterministic test clock: the `block` drain window (§8)
# ---------------------------------------------------------------------------

def test_block_drain_window_lowers_in_milliseconds():
    step = _subscribe_step("""
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src policy block buffer 2 drain 10ms undo sub.close()
      await sub.next()
    }
    """)
    assert step["policy"] == "block" and step["drain"] == 10
    assert step["acquire"]["drain"] == 10


def test_the_drain_window_and_the_advance_statement_share_one_clock():
    """§8: stream timing is testable WITHOUT wall-clock sleeps because the drain
    window rides the existing test clock — the same `Clock` the `advance <n><unit>`
    lifecycle statement steps. Pinned on one emitted module so the two can never
    drift onto separate clocks."""
    code = _tier_emit("python").emit(compile_source("""
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src policy block buffer 2 drain 10ms undo sub.close()
      await sub.next()
    }
    lifecycle test "the window fires on a timeline step" {
      load C
      advance 10ms
      unload C
      assert no_residue
    }
    """, "s.rvl"))
    assert "Stream.subscribe(src, 'block', _revl_ctx, capacity=2, drain_ms=10)" in code
    assert "_revl_Clock.advance(10)" in code
    assert "Clock as _revl_Clock" in code


def test_drain_is_refused_without_the_block_policy():
    """The clock only drives the one time-windowed behavior the design names —
    a `block`-policy drain interval (§8). `drop_*`/`error` resolve an overflow
    immediately and have no window to fire."""
    msg = _refusal("""
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src policy drop_oldest drain 10ms undo sub.close()
      await sub.next()
    }
    """)
    assert "`drain` is the `block`-policy drain window" in msg


# ---------------------------------------------------------------------------
# Replay: provider-declared only, so every `replay(…)` at a `subscribe` is an
# undeclared argument and a compile error (§4.5)
# ---------------------------------------------------------------------------

def _replay(qual: str) -> str:
    return _refusal(f"""
    component C {{
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src {qual} undo sub.close()
      await sub.next()
    }}
    """)


@pytest.mark.parametrize("qual", ["replay(5)", "replay(from: cursor)"])
def test_a_well_formed_replay_is_refused_as_undeclared(qual):
    """§4.5: replay is a provider-side durability claim; no provider can declare
    it yet, so a well-formed `replay(n)` / `replay(from: <durable>)` at a
    `subscribe` is an UNDECLARED argument and a compile error, not a silently
    inert (vacuous) durability claim."""
    msg = _replay(qual)
    assert "`replay`" in msg and "requires a provider that declares it" in msg
    assert "§4.5" in msg


@pytest.mark.parametrize("qual", ["replay()", "replay(0)", "replay(-1)",
                                  "replay(foo)", "replay 5"])
def test_a_malformed_replay_argument_names_the_two_shapes(qual):
    """The other half of §4.5's compile error: a malformed argument is a syntax
    error distinct from the undeclared refusal, and it names the two accepted
    shapes so the fix is obvious."""
    msg = _replay(qual)
    assert "malformed `replay` argument" in msg
    assert "replay(<positive int>)" in msg and "replay(from: <durable>)" in msg


def test_replay_is_order_free_with_the_other_qualifiers():
    """`replay` is recognized in the order-free qualifier run, so it is refused
    (not mis-parsed) even after a `policy`/`buffer` qualifier."""
    msg = _replay("policy block buffer 2 replay(3)")
    assert "requires a provider that declares it" in msg


def test_a_replay_free_program_is_byte_identical():
    """The refusal threads no IR: a subscription with no `replay` lowers exactly
    as before, and the key never appears (§5 additive-key discipline)."""
    body = compile_source(_CONSUMER, "s.rvl")["components"][0]["body"]
    sub = next(s for s in body if s.get("subscribe"))
    assert "replay" not in sub and "replay" not in sub.get("acquire", {})


# ---------------------------------------------------------------------------
# Replay, DECLARED: the provider surface §4.5 gates the consumer request on
# ---------------------------------------------------------------------------

def _declared(decl: str, head: str) -> str:
    """A provider carrying the `decl` declaration and a consumer asking `head`."""
    return (
        "component C {\n"
        f"  let src = effect Stream.source() {decl} undo src.close()\n"
        f"  let sub = subscribe src {head} undo sub.close()\n"
        "  await sub.next()\n"
        "}\n"
    )


def _declared_ir(decl: str, head: str) -> tuple:
    body = compile_source(_declared(decl, head), "s.rvl")["components"][0]["body"]
    source = next(s for s in body if not s.get("subscribe")
                  and s.get("step") == "let-effect")
    sub = next(s for s in body if s.get("subscribe"))
    return source, sub


def test_a_declared_provider_admits_the_consumer_request_and_threads_both_ends():
    """§4.5: replay has ONE owner. The provider declares the backlog it holds,
    the consumer asks for a slice of it, and both ends land in the IR — the
    provider so the source can hold the items, the consumer so the subscription
    reads them before any live item."""
    source, sub = _declared_ir("replay(8)", "replay(3)")
    assert source["replay"] == {"count": 8}
    assert source["acquire"]["replay"] == {"count": 8}
    assert sub["replay"] == {"count": 3} == sub["acquire"]["replay"]


def test_a_durable_cursor_threads_its_name_on_both_ends():
    source, sub = _declared_ir('replay(from: "orders")', 'replay(from: "orders")')
    assert source["replay"] == {"cursor": "orders"}
    assert sub["replay"] == {"cursor": "orders"} == sub["acquire"]["replay"]


def test_a_consumer_cannot_ask_past_the_declared_backlog():
    """The provider holds the buffer, so the consumer cannot ask beyond it.
    Admitting `replay(3)` against a `replay(2)` provider would ship a claim
    the provider does not back — the vacuity §4.5 refuses."""
    msg = _refusal(_declared("replay(2)", "replay(3)"))
    assert "asks for more than stream source `src` declares" in msg
    assert "backlog holds 2" in msg


def test_a_last_n_provider_does_not_back_a_durable_cursor():
    msg = _refusal(_declared("replay(4)", 'replay(from: "orders")'))
    assert "declares a last-n backlog, not a durable cursor" in msg


def test_a_durable_cursor_provider_is_resumed_not_asked_for_a_count():
    msg = _refusal(_declared('replay(from: "orders")', "replay(2)"))
    assert "declares a durable cursor" in msg
    assert 'replay(from: "orders")' in msg


def test_a_consumer_may_only_resume_the_cursor_the_provider_declared():
    msg = _refusal(_declared('replay(from: "orders")', 'replay(from: "other")'))
    assert "declares the durable cursor `orders`, not `other`" in msg


def test_a_durable_cursor_must_be_a_literal_name():
    """The cursor IS the descriptor recovery re-issues the subscription from
    (§4.9), so it has to be writable into the WAL as it stands. A computed one
    would be a durability claim with nothing durable in it."""
    msg = _refusal(_declared("replay(from: 7)", "replay(2)"))
    assert "durable replay cursor must be a literal name" in msg


def test_replay_on_a_fan_in_is_refused_for_having_no_declared_order():
    msg = _refusal("""
    component C {
      let a = effect Stream.source() replay(4) undo a.close()
      let b = effect Stream.source() replay(4) undo b.close()
      let sub = subscribe merge(a, b) replay(2) undo sub.close()
      await sub.next()
    }
    """)
    assert "has no declared order" in msg


def test_a_durable_cursor_refuses_a_combinator_chain():
    """A cursor is a position in the PROVIDER's log. A derived stream drops and
    rewrites items, so it has no position in that log to resume from."""
    msg = _refusal("""
    component C {
      let src = effect Stream.source() replay(from: "orders") undo src.close()
      let sub = subscribe src.filter(x => x > 1) replay(from: "orders")
                  undo sub.close()
      await sub.next()
    }
    """)
    assert "durable replay cursor and a combinator chain do not compose" in msg


@pytest.mark.parametrize("policy", ["drop_newest", "drop_oldest"])
def test_a_durable_cursor_refuses_the_lossy_policies(policy):
    """A discarded item would advance the cursor past an item no consumer saw —
    a resumable position that silently lies."""
    msg = _refusal(_declared('replay(from: "orders")',
                             f"policy {policy} replay(from: \"orders\")"))
    assert "do not compose" in msg and policy in msg


def test_replay_on_a_non_stream_acquisition_is_refused():
    msg = _refusal("""
    component C {
      let p = effect Pool.open("u", 4) replay(3) undo p.close()
    }
    """)
    assert "not a stream source" in msg


def test_an_unbound_replay_declaration_is_refused():
    """Replay is consumed BY NAME (`subscribe <source> replay(n)`), so an
    unbound provider could never have its declaration honoured."""
    msg = _refusal("""
    component C {
      effect Stream.source() replay(3) undo Stream.source()
    }
    """)
    assert "needs a bound stream source" in msg


def test_replay_may_not_be_declared_twice_on_a_subscribe():
    msg = _refusal(_declared("replay(8)", "replay(2) replay(3)"))
    assert "duplicate `replay`" in msg


# ---------------------------------------------------------------------------
# Replay emission: py lowers it; every other tier REFUSES BY NAME (§4.5, §4.9)
# ---------------------------------------------------------------------------

def test_python_emits_the_declaration_the_request_and_the_durable_disposer():
    code = _tier_emit("python").emit(compile_source(
        _declared('replay(from: "orders")', 'replay(from: "orders")'), "s.rvl"))
    assert "Stream.source(replay={'cursor': 'orders'})" in code
    assert ("Stream.subscribe(src, 'error', _revl_ctx, "
            "replay={'cursor': 'orders'})") in code
    # §4.9: the bracket registers a disposer that DESCRIBES itself, so the WAL
    # records a re-issuable inverse instead of a closure it cannot run again.
    assert "yield sub.durable_undo()" in code
    # ... and the provider bracket is untouched: only the subscription is the
    # thing a durable cursor makes reconstructible.
    assert "yield lambda: src.close()" in code


def test_a_last_n_request_keeps_the_ordinary_closure_bracket():
    """Only the DURABLE cursor buys reconstructibility (§4.9). A last-n backlog
    dies with the provider, so its subscription is the closure-only bracket it
    always was — no tier ships a reconstructibility claim a count does not make."""
    code = _tier_emit("python").emit(compile_source(
        _declared("replay(8)", "replay(3)"), "s.rvl"))
    assert "Stream.source(replay={'count': 8})" in code
    assert "yield lambda: sub.close()" in code
    assert "durable_undo" not in code


@pytest.mark.parametrize("tier", ["go", "rust", "java", "typescript"])
@pytest.mark.parametrize("head", ["", "replay(2)"])
def test_every_other_tier_refuses_replay_by_name(tier, head):
    """Both ends refuse: the provider's declaration (a backlog this tier does
    not hold) and the consumer's request. A tier that emitted either while
    silently dropping it would deliver only live items and call it replay —
    exactly the run-and-quietly-disagree outcome item 130 refuses."""
    emit = _tier_emit(tier)
    ir = compile_source(_declared("replay(4)", head), "s.rvl")
    with pytest.raises(emit.EmitError) as excinfo:
        emit.emit(ir)
    msg = str(excinfo.value)
    assert "`replay(…)` is not lowered" in msg
    assert "§4.5" in msg and "§4.9" in msg and "backend py" in msg


@pytest.mark.parametrize("tier", ["go", "rust", "java", "typescript"])
@pytest.mark.parametrize("policy", ["drop_newest", "drop_oldest", "block"])
def test_replay_is_refused_beside_a_policy_the_tier_now_lowers(tier, policy):
    """The combination neither landing had. Since #1042 these tiers LOWER the
    three non-default §4.4 policies, so a `subscribe` carrying both a lossy
    policy and a `replay(…)` is the first shape where one half of a head is
    emittable and the other is not.

    The refusal must win. A tier that lowered the policy and let the backlog
    fall off the end would emit a program that runs, drops items by a rule the
    author declared, and never replays anything the author also declared — the
    run-and-quietly-disagree outcome, with a durability claim as the casualty.
    Asserted on the message, so a future landing that lowers replay has to
    delete this test rather than let it pass vacuously."""
    emit = _tier_emit(tier)
    head = f"policy {policy} buffer 2 replay(2)"
    with pytest.raises(emit.EmitError) as excinfo:
        emit.emit(compile_source(_declared("replay(4)", head), "s.rvl"))
    assert "`replay(…)` is not lowered" in str(excinfo.value)


@pytest.mark.parametrize("tier", ["go", "rust", "java", "typescript"])
@pytest.mark.parametrize("policy", ["drop_newest", "drop_oldest", "block"])
def test_the_same_head_without_replay_still_lowers_its_policy(tier, policy):
    """The control for the refusal above, and the thing that keeps it honest:
    drop the `replay` and the identical head EMITS, carrying the declared policy
    as the subscription's own argument. So the refusal is about replay, not a
    blanket refusal of the head it appears in, and this tier's #1042 policy
    lowering is untouched by this branch."""
    code = _tier_emit(tier).emit(compile_source(
        "component C {\n"
        "  let src = effect Stream.source() undo src.close()\n"
        f"  let sub = subscribe src policy {policy} buffer 2 undo sub.close()\n"
        "  await sub.next()\n"
        "}\n", "s.rvl"))
    # the emitted `subscribe` CALL carrying the policy, per tier's spelling —
    # not the bare policy word, which also appears in each runtime's own arms.
    assert (f'"{policy}", 2' in code                       # go / java / rust
            or f'"{policy}", ctx, {{ capacity: 2 }}' in code)  # ts


@pytest.mark.parametrize("tier", ["go", "rust", "java"])
def test_replay_outranks_the_drain_refusal_on_a_blocking_tier(tier):
    """A durable cursor (§4.5) and a `drain` window (§8) on one head.

    On rust and java both halves are unlowered and either message would be
    honest, so the point is that WHICH one is stable: the two refusals are about
    different things — the window about the clock, replay about the recovery
    surface — and a silent flip would send an author to fix the wrong half of
    their `subscribe`. On go only replay is left to refuse, and the same
    assertion holds for the plainer reason that the window lowers there; keeping
    go in the list is what would catch a regression that brought its window
    refusal back."""
    emit = _tier_emit(tier)
    head = 'policy block buffer 2 drain 10ms replay(from: "orders")'
    with pytest.raises(emit.EmitError) as excinfo:
        emit.emit(compile_source(
            _declared('replay(from: "orders")', head), "s.rvl"))
    message = str(excinfo.value)
    assert "`replay(…)` is not lowered" in message
    assert "`drain` window is not lowered" not in message


def test_wasm_still_refuses_a_replay_program_as_a_stream_program():
    emit = _tier_emit("wasm")
    with pytest.raises(emit.EmitError) as excinfo:
        emit.emit(compile_source(_declared("replay(4)", "replay(2)"), "s.rvl"))
    assert "suspends a fiber" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Emission: py renders the chain/policy/window; wasm still REFUSES (§4.6)
# ---------------------------------------------------------------------------

def test_python_emits_the_chain_the_capacity_and_the_drain_window():
    code = _tier_emit("python").emit(compile_source(_CHAIN, "s.rvl"))
    assert ("sub = Stream.subscribe(src, 'drop_oldest', _revl_ctx, "
            "stages=[('map', lambda x: (x * 2)), ('filter', lambda x: (x > 2)), "
            "('take', 3)], capacity=4)") in code
    assert "yield lambda: sub.close()" in code, "still ONE bracket for the chain"

    windowed = _tier_emit("python").emit(compile_source("""
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src policy block buffer 2 drain 10ms undo sub.close()
      await sub.next()
    }
    """, "s.rvl"))
    assert ("Stream.subscribe(src, 'block', _revl_ctx, capacity=2, drain_ms=10)"
            in windowed)


def test_a_slice_1_subscription_still_emits_the_exact_three_argument_call():
    """Byte-identity for the Slice 1 surface (§10.9): every Slice 2 argument is
    appended only when DECLARED."""
    code = _tier_emit("python").emit(compile_source(_CONSUMER, "s.rvl"))
    assert "sub = Stream.subscribe(src, 'error', _revl_ctx)\n" in code


def test_wasm_still_refuses_the_slice_2_surface():
    """The refusal fence holds over the new surface too: a combinator chain, a
    declared policy and a drain window are all still a fiber suspension this
    tier has no async host seam for (§4.6, exit test §10.8)."""
    emit = _tier_emit("wasm")
    for src in (_CHAIN, """
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src policy block buffer 2 drain 10ms undo sub.close()
      await sub.next()
    }
    """):
        with pytest.raises(emit.EmitError) as excinfo:
            emit.emit(compile_source(src, "s.rvl"))
        msg = str(excinfo.value)
        assert "suspends a fiber" in msg and "backend py" in msg


# ---------------------------------------------------------------------------
# Slice 3 — `merge` (design §1) and the blocking-tier lowerings (§4.6)
# ---------------------------------------------------------------------------

_FANIN = """
component C {
  let a = effect Stream.source() undo a.close()
  let b = effect Stream.source() undo b.close()
  let sub = subscribe merge(a, b) undo sub.close()
  await sub.next()
}
"""


def test_merge_lowers_inside_the_subscription_acquisition():
    """`merge(a, b)` is a DERIVED stream the subscription owns, not a bracket of
    its own: the fan-in lowers into the `subscribe` acquire, so the whole thing
    tears down on the ONE bracket the subscribe registers (design §1)."""
    body = compile_source(_FANIN, "s.rvl")["components"][0]["body"]
    sub = next(s for s in body if s.get("subscribe"))
    head = sub["acquire"]["stream"]
    assert head["kind"] == "stream-merge"
    assert head["sources"] == [{"kind": "name", "id": "a"},
                               {"kind": "name", "id": "b"}]
    # exactly three brackets: one per source, one for the subscription
    assert len([s for s in body if s.get("step") == "let-effect"]) == 3
    assert sub["undo"] == {"kind": "call",
                           "target": {"kind": "name", "id": "sub"},
                           "method": "close", "args": []}


def test_merge_adds_no_host_verb():
    """The fan-in is parsed in the `subscribe` head, the one position the
    surface already controls — the same call Slice 2 makes for the combinator
    chain. A `<src>.merge(..)` method spelling would grow the shared host-verb
    namespace, so the exact-set pin in tests/test_map_value_type.py stays
    untouched by this slice."""
    from revl.typecheck import _HOST_FAMILIES  # noqa: PLC0415
    verbs = {m for methods in _HOST_FAMILIES.values() for m in methods}
    assert "merge" not in verbs


def test_merge_refuses_a_source_that_can_vanish_without_a_terminal():
    """Rule 3.6 applied POINTWISE: a fan-in is only as sound as its weakest
    source, so one silent provider poisons the whole thing (§9 Part B)."""
    msg = _refusal("""
    component C {
      let other = effect Pool.open("u", 1) undo other.close()
      let a = effect Stream.source() undo a.close()
      let b = effect Stream.source() undo other.close()
      let sub = subscribe merge(a, b) undo sub.close()
      await sub.next()
    }
    """)
    assert "vanish without delivering a terminal" in msg and "`b`" in msg


def test_merge_refuses_an_already_consumed_source():
    """Rule 3.1: merging CONSUMES a source, so a stream cannot be both
    subscribed and merged."""
    msg = _refusal("""
    component C {
      let a = effect Stream.source() undo a.close()
      let b = effect Stream.source() undo b.close()
      let first = subscribe a undo first.close()
      let sub = subscribe merge(a, b) undo sub.close()
      await sub.next()
    }
    """)
    assert "already subscribed" in msg and "single-consumer" in msg


def test_a_merged_source_cannot_be_subscribed_again():
    """The mirror: a source consumed by a merge is not available to a later
    subscription either."""
    msg = _refusal("""
    component C {
      let a = effect Stream.source() undo a.close()
      let b = effect Stream.source() undo b.close()
      let sub = subscribe merge(a, b) undo sub.close()
      let second = subscribe a undo second.close()
      await sub.next()
    }
    """)
    assert "already subscribed" in msg


def test_merge_refuses_the_same_source_twice():
    msg = _refusal("""
    component C {
      let a = effect Stream.source() undo a.close()
      let sub = subscribe merge(a, a) undo sub.close()
      await sub.next()
    }
    """)
    assert "same stream source twice" in msg


def test_merge_is_binary():
    msg = _refusal("""
    component C {
      let a = effect Stream.source() undo a.close()
      let b = effect Stream.source() undo b.close()
      let c = effect Stream.source() undo c.close()
      let sub = subscribe merge(a, b, c) undo sub.close()
      await sub.next()
    }
    """)
    assert "exactly 2 streams" in msg


def test_merge_nests():
    """A merged stream is itself a stream, so a fan-in of three is a nested
    merge — with no new machinery and still one bracket."""
    body = compile_source("""
    component C {
      let a = effect Stream.source() undo a.close()
      let b = effect Stream.source() undo b.close()
      let c = effect Stream.source() undo c.close()
      let sub = subscribe merge(merge(a, b), c) undo sub.close()
      await sub.next()
    }
    """, "s.rvl")["components"][0]["body"]
    head = next(s for s in body if s.get("subscribe"))["acquire"]["stream"]
    assert head["kind"] == "stream-merge"
    assert head["sources"][0]["kind"] == "stream-merge"
    assert head["sources"][1] == {"kind": "name", "id": "c"}


def test_python_emits_the_fan_in_inside_the_subscription():
    code = _tier_emit("python").emit(compile_source(_FANIN, "s.rvl"))
    assert "sub = Stream.subscribe(Stream.merge(a, b), 'error', _revl_ctx)" in code
    assert "yield lambda: sub.close()" in code


def test_the_blocking_tiers_lower_the_fan_in(tmp_path):
    """Slice 3's three blocking tiers (§4.6). All three erase the async color:
    the fan-in opens inside the subscription's acquisition, and the bracket
    inverse is the subscription's `close`, which trips the cancel signal.

    item 130 (roadmap #81): `java` joined go and rust here — it carries a real
    `Stream` runtime and lowers the fan-in. wasm is the one tier that still
    refuses the whole surface, by design (it has no async at all); see
    `test_wasm_still_refuses_the_fan_in`."""
    ir = compile_source(_FANIN, "s.rvl")
    go = _tier_emit("go").emit(ir)
    assert 'StreamSubscribe(StreamMerge(a, b), "error", 0)' in go
    assert "return func() error { sub.Close(); return nil }" in go
    rust = _tier_emit("rust").emit(ir)
    assert 'Stream::subscribe(&Stream::merge(&a, &b), "error", 0usize)' in rust
    assert "sub_undo.close(); Ok(())" in rust
    java = _tier_emit("java").emit(ir)
    assert 'Stream.subscribe(Stream.merge(a, b), "error", 0)' in java
    assert "fx.track(Disposables.of(() -> sub.close()));" in java


# item 416a: `subscribe` was refused on every tier, but a `Stream.source()`-only
# program was not. It lowers to a plain `host` node, which java and typescript
# rendered verbatim against a runtime that has no `Stream` at all, so the
# failure surfaced as the CONSUMER's build error (`Stream` undeclared in the
# generated java, `host.Stream` undefined in the generated ts) instead of a
# refusal here. That is the silent emit the honest refusal exists to prevent.
_SOURCE_ONLY = """
component C {
  let src = effect Stream.source() undo src.close()
}
"""


@pytest.mark.parametrize("tier", ["go", "rust", "java"])
def test_source_only_program_still_emits_on_the_lowered_tiers(tier):
    """The refusal is scoped to the tiers with no runtime: go, rust and java
    carry a real `Stream` (Slice 3) and must keep emitting one.

    item 130 (roadmap #81): `typescript` and `java` both graduated off the
    source-only refusal — `runtime.ts` and the emitted `Stream` runtime class
    carry a real provider now, so the program EMITS. wasm is the only tier left
    refusing it (`test_wasm_refuses_the_source_only_program_too`)."""
    code = _tier_emit(tier).emit(compile_source(_SOURCE_ONLY, "s.rvl"))
    assert "Stream" in code


def test_wasm_refuses_the_source_only_program_too():
    emit = _tier_emit("wasm")
    with pytest.raises(emit.EmitError) as excinfo:
        emit.emit(compile_source(_SOURCE_ONLY, "s.rvl"))
    assert "suspends a fiber" in str(excinfo.value)


def test_wasm_still_refuses_the_fan_in():
    emit = _tier_emit("wasm")
    with pytest.raises(emit.EmitError) as excinfo:
        emit.emit(compile_source(_FANIN, "s.rvl"))
    assert "suspends a fiber" in str(excinfo.value)


_DRAIN_HEAD = "subscribe a policy block drain 5s undo sub.close()"


def _drain_program(head: str = _DRAIN_HEAD) -> str:
    return ("component C {\n"
            "  let a = effect Stream.source() undo a.close()\n"
            f"  let sub = {head}\n"
            "  await sub.next()\n"
            "}\n")


@pytest.mark.parametrize("tier", ["rust", "java"])
def test_the_two_clockless_tiers_refuse_the_drain_window_by_name(tier):
    """The `block`-policy drain window, on the two tiers that still cannot fire
    it — and this is the whole of what `_refuse_unlowered_stream_surface` stops
    for a non-replay program now.

    The list used to be all three blocking tiers, under one shared reason: "the
    deterministic test clock, which no blocking tier carries". That reason was
    false. go carries item 57's clock coeffect (`RevlClockAdvance`), process-wide
    and driven by the same `advance` statement the window shares on the
    reference, so the window is LOWERED there —
    `test_go_lowers_the_drain_window_against_its_own_clock` below, and the
    executable proof in backends/go/test_stream_exec_130.py.

    The two that remain refuse for two DIFFERENT reasons, which is why they no
    longer share a text:

      rust carries a clock, but `thread_local!` — deliberately, so parallel
        `cargo test` threads never share one. The window would be armed by the
        provider's thread and advanced by the consumer's (every scenario in
        backends/rust/scenarios/stream.rs drives its provider from a spawned
        thread), so it would never fire and the provider would stay suspended.
      java carries no clock at all: timers do not lower and an `advance` step is
        refused by name, so there is nothing to fire a window on.

    Either way the rule is the same one wasm follows for the whole surface: a
    tier that cannot lower something refuses it BY NAME. Compiling the program
    and answering differently from the reference is the worst outcome available."""
    emit = _tier_emit(tier)
    with pytest.raises(emit.EmitError) as excinfo:
        emit.emit(compile_source(_drain_program(), "s.rvl"))
    msg = str(excinfo.value)
    assert "`drain` window" in msg
    assert "not lowered" in msg and "backend py" in msg
    # the refusal names the tier's OWN reason, not the retired shared one
    assert "THREAD-LOCAL" in msg if tier == "rust" else "NO CLOCK AT ALL" in msg
    # and it points at the tier that does lower it, so an author has somewhere
    # to go that is not only the reference
    assert "backend go" in msg


def test_go_lowers_the_drain_window_against_its_own_clock():
    """go's refusal outlived its reason: this tier has carried item 57's clock
    coeffect — process-wide, advanced by the same `advance` statement §8 names —
    for as long as it has carried timers. The window rides it.

    The call stays the three-argument one for every subscription that declares
    no window (`test_a_declared_window_is_the_only_thing_that_changes_the_go_call`),
    so this is additive: a window-free stream program emits exactly what it did.
    """
    code = _tier_emit("go").emit(compile_source(_drain_program(), "s.rvl"))
    assert 'StreamSubscribe(a, "block", 0, 5000)' in code
    # the window is armed against the clock coeffect, not a wall clock
    assert "func RevlClockAdvance(ms int64) int {" in code
    assert "streamArmDrain = func(ms int64, body func()) func() bool {" in code
    assert "revlScheduleAfter(ms, body).Cancel" in code
    # no `time.Sleep`/wall-clock deadline anywhere in the window's path
    assert "time.Sleep" not in code


def test_a_declared_window_is_the_only_thing_that_changes_the_go_call():
    """A subscription with no §8 window emits the exact three-argument call it
    always has, and pulls in no scheduler: `StreamSubscribe` is variadic in the
    window, the same way the reference defaults `drain_ms` to None and the ts
    tier appends an options object only when something is declared."""
    code = _tier_emit("go").emit(compile_source(
        _drain_program("subscribe a policy block undo sub.close()"), "s.rvl"))
    assert 'StreamSubscribe(a, "block", 0)' in code
    assert "func RevlClockAdvance" not in code, \
        "a window-free stream program must not grow a timer scheduler"
    assert "streamArmDrain = func" not in code


def test_a_windowed_and_an_unwindowed_subscription_coexist_in_one_go_module():
    """One module carrying a windowed subscription, a typed-event handler and a
    second `block` subscription with NO window.

    The drain half of the go runtime is its own preamble, pulled in only by a
    declared window; the event contract is another. A module reaching all three
    is where a mis-gated preamble shows up — either as a missing arming hook or
    as a duplicated one — and neither would be visible in a single-feature
    program. `backends/go/test_stream_exec_130.py` compiles the emitted module;
    this pins the assembly everywhere, including where no go toolchain exists."""
    code = _tier_emit("go").emit(compile_source(
        "event E(key: k) { k: Str }\n"
        "service Sink { emission fn write(v: Str) }\n"
        "component H requires sink: Sink {\n"
        "  let src = effect Stream.source() undo src.close()\n"
        "  let sub = subscribe src policy block buffer 2 drain 10ms undo sub.close()\n"
        "  on E as e in sub { emit sink.write(e.k) }\n"
        "}\n"
        "component Plain {\n"
        "  let a = effect Stream.source() undo a.close()\n"
        "  let s2 = subscribe a policy block undo s2.close()\n"
        "  await s2.next()\n"
        "}\n", "s.rvl"))
    assert 'StreamSubscribe(src, "block", 2, 10)' in code   # windowed
    assert 'StreamSubscribe(a, "block", 0)' in code          # not windowed
    # each preamble lands exactly once, however many components reach it
    assert code.count("streamArmDrain = func") == 1
    assert code.count("func RevlClockAdvance(ms int64) int {") == 1
    assert code.count("func StreamSubscribe(") == 1
    assert "func StreamContract(" in code or "EventContract" in code


def test_the_go_window_and_the_advance_statement_share_one_clock():
    """§8 on the go tier, the same property the reference is pinned to by
    `test_the_drain_window_and_the_advance_statement_share_one_clock`: the window
    and the `advance <n><unit>` lifecycle statement step ONE clock, so stream
    timing is testable with no wall-clock sleeps. Pinned on one emitted module so
    the two can never drift onto separate clocks."""
    code = _tier_emit("go").emit(compile_source(
        "service Sink { emission fn write(v: Str) }\n"
        "component C provides sink: Sink {\n"
        "  let a = effect Stream.source() undo a.close()\n"
        "  let sub = subscribe a policy block buffer 2 drain 10ms undo sub.close()\n"
        "  provide sink { fn write(v: Str) { } }\n"
        "}\n"
        'lifecycle test "the window fires on a timeline step" {\n'
        "  load C\n"
        "  advance 10ms\n"
        "  unload C\n"
        "  assert no_residue\n"
        "}\n", "s.rvl"))
    assert 'StreamSubscribe(a, "block", 2, 10)' in code
    assert "RevlClockAdvance(10)" in code
    assert code.count("func RevlClockAdvance(ms int64) int {") == 1, \
        "one scheduler, so the window and `advance` cannot be on two clocks"


_CHAIN_HEAD = """
component C {
  let a = effect Stream.source() undo a.close()
  let sub = subscribe a.filter(x => x != "skip").map(x => x + "!").take(2)
              undo sub.close()
  await sub.next()
}
"""


@pytest.mark.parametrize(("tier", "want"), [
    ("go", 'StreamSubscribe(StreamTake(StreamMap(StreamFilter(a, '
           'func(x string) bool { return (x != "skip") }), '
           'func(x string) string { return (x + "!") }), 2), "error", 0)'),
    ("rust", 'Stream::subscribe(&Stream::take(&Stream::map(&Stream::filter(&a, '
             '|x: String| -> bool { (x != "skip") }), '
             '|x: String| -> String { format!("{}{}", x, String::from("!")) }), '
             '2usize), "error", 0usize)'),
    ("java", 'Stream.subscribe(Stream.take(Stream.map(Stream.filter(a, '
             '(x) -> !revlEq(x, "skip")), (x) -> (x + "!")), 2), "error", 0)'),
])
def test_the_blocking_tiers_lower_the_combinator_chain(tier, want):
    """Slice 2's derived-stream chain on the three blocking tiers (§1).

    Each link is a DERIVED stream nested INSIDE the subscription's acquisition,
    left to right, so the LAST link is the subscription's immediate upstream —
    the same nesting the py reference builds from its `stages=[…]` list, which
    is what makes the four tiers agree item for item. There is still exactly ONE
    bracket: the chain is owned by the subscription, never by a bracket of its
    own, so `close` unwinds the whole chain and each plain source is left to its
    own inverse."""
    assert want in _tier_emit(tier).emit(compile_source(_CHAIN_HEAD, "s.rvl"))


@pytest.mark.parametrize("tier", ["go", "rust", "java"])
def test_a_combinator_chain_is_still_one_bracket_on_the_blocking_tiers(tier):
    """The chain is a DERIVED stream owned by the subscription (§1), so a
    three-link chain registers the SAME single inverse a Slice 1 subscription
    does. A link with a bracket of its own would break the LIFO close-order
    proof the core guarantee rests on."""
    chained = _tier_emit(tier).emit(compile_source(_CHAIN_HEAD, "s.rvl"))
    plain = _tier_emit(tier).emit(compile_source(
        "component C {\n"
        "  let a = effect Stream.source() undo a.close()\n"
        "  let sub = subscribe a undo sub.close()\n"
        "  await sub.next()\n"
        "}\n", "s.rvl"))
    inverse = {"go": "return func() error { sub.Close(); return nil }",
               "rust": "sub_undo.close(); Ok(())",
               "java": "() -> sub.close()"}[tier]
    assert chained.count(inverse) == plain.count(inverse) == 1


def test_the_general_arrow_refusal_still_stands_on_the_blocking_tiers():
    """Lowering the combinator transform did NOT open arrow values in component
    position. The stage renderer is scoped to a one-parameter transform over the
    tier's stream item — a known interface and a known item type — so the
    general limit go and java declare is untouched, and
    `tests/test_expr_dispatcher_conformance.py` keeps pinning it as data.

    A bare arrow in a component body still refuses on both, which is what stops
    this slice from having quietly widened the tier surface."""
    assert "arrow" in _tier_emit("go").EXPR_REFUSED["component"]
    assert "arrow" in _tier_emit("java").EXPR_REFUSED


@pytest.mark.parametrize("tier", ["go", "rust", "java"])
@pytest.mark.parametrize("policy", ["drop_newest", "drop_oldest", "block"])
def test_blocking_tiers_lower_every_backpressure_policy(tier, policy):
    """item 130 Slice 2 on the blocking tiers: all four §4.4 policies lower.

    The declared policy reaches the emitted `subscribe` call as its own
    argument, so the runtime picks the arm rather than the emitter picking it —
    which is what keeps the four arms a single decision, mirrored from the py
    reference, instead of four emitter shapes that can drift apart."""
    code = _tier_emit(tier).emit(compile_source(
        "component C {\n"
        "  let a = effect Stream.source() undo a.close()\n"
        f"  let sub = subscribe a policy {policy} buffer 2 undo sub.close()\n"
        "  await sub.next()\n"
        "}\n", "s.rvl"))
    assert f'"{policy}", 2' in code


@pytest.mark.parametrize("tier", ["go", "rust", "java"])
def test_the_blocking_runtimes_carry_the_reference_policy_arms(tier):
    """The emitted runtime carries all four arms and the reserved `Paused` state
    index, and it no longer carries the "not lowered on this tier" panic that
    used to sit where the three non-default arms are now.

    The MARKS are the load-bearing half: a drop is recorded with the item it
    lost and a pause/resume is recorded as such, so backpressure is never a
    silent loss on any tier (§4.4), and the mark text matches the py reference
    and ts tiers so one trace assertion reads the same everywhere."""
    code = _tier_emit(tier).emit(compile_source(_CONSUMER, "s.rvl"))
    assert "stream.drop_newest " in code
    assert "stream.drop_oldest " in code
    assert "stream.paused" in code
    assert "stream.resume" in code
    assert "is not lowered on the" not in code


@pytest.mark.parametrize(("tier", "fragment"), [
    ("go", 'hostRecord("stream.emit " + item + " refused")'),
    ("rust", 'format!("stream.emit {} refused", item)'),
    ("java", '"stream.emit " + item + " refused"'),
])
def test_the_blocking_runtimes_report_a_refused_emit_like_the_reference(tier, fragment):
    """`block` is only observable if the provider LEARNS it was refused, so the
    emitted `emit` returns downstream acceptance and TRACES the refusal. The py
    reference writes `stream.emit <item> refused` and so does ts; a blocking
    tier that recorded an unconditional `stream.emit <item>` would read as a
    delivery that never happened.

    The assertion is the emitted RECORD CALL, not the word `refused` anywhere in
    the file: the combinator-chain landing put "refused" in a prose comment
    (`take(n)` refunds its slot when the delivery is refused), so a substring
    test on the bare word passes against a runtime that never writes the line."""
    code = _tier_emit(tier).emit(compile_source(_CONSUMER, "s.rvl"))
    assert fragment in code


@pytest.mark.parametrize("tier", ["go", "rust", "java"])
@pytest.mark.parametrize("policy", ["drop_newest", "drop_oldest", "block"])
def test_a_chain_and_a_lossy_policy_coexist_on_one_subscription(tier, policy):
    """The two Slice 2 landings on one `subscribe`. Neither emitter path knew
    about the other, and the refusal that used to stand between them is gone
    from both, so the combined form is the one that has never been emitted.

    It must emit with BOTH halves present: the chain as derived links in the
    acquisition, and the declared policy as the subscription's own argument.
    The runtime interaction (a `block` pause must not spend `take`'s budget,
    because both ride the same acceptance boolean) is proved by running, on each
    tier's own scenario and against the py reference."""
    code = _tier_emit(tier).emit(compile_source(
        "component C {\n"
        "  let a = effect Stream.source() undo a.close()\n"
        f"  let sub = subscribe a.map(x => x).take(2) policy {policy} buffer 1 "
        "undo sub.close()\n"
        "  await sub.next()\n"
        "}\n", "s.rvl"))
    assert f'"{policy}", 1' in code
    # the chain is still there, between the provider and the subscription
    assert ("Take(" in code) or ("take(" in code)
    assert ("Map(" in code) or ("map(" in code)


@pytest.mark.parametrize("tier", ["rust", "java"])
def test_the_drain_refusal_says_the_block_policy_itself_is_lowered(tier):
    """The refusal has to stay accurate or it sends an author to `--backend py`
    for something this tier does: the window is refused, `block` is not.

    "Accurate" is load-bearing twice over here. This same clause carried a
    reason that had quietly stopped being true — "the deterministic test clock,
    which no blocking tier carries" — while two of the three tiers it named did
    carry one. A refusal whose REASON is stale is a refusal nobody can act on,
    so each tier now states its own and is pinned to it."""
    emit = _tier_emit(tier)
    with pytest.raises(emit.EmitError) as excinfo:
        emit.emit(compile_source(_drain_program(), "s.rvl"))
    msg = str(excinfo.value)
    assert "`block` policy itself IS lowered" in msg
    assert "clock coeffect" in msg
    # the retired blanket reason must not come back
    assert "deterministic test clock, which lives on the" not in msg


@pytest.mark.parametrize("tier", ["go", "rust", "java"])
def test_blocking_tiers_honour_a_declared_buffer(tier):
    """`buffer n` IS lowered on both blocking tiers: every buffer is bounded
    either way (§4.4), so honouring the declared capacity costs nothing and
    refusing it would be a spurious limitation."""
    src = _tier_emit(tier).emit(compile_source(
        "component C {\n"
        "  let a = effect Stream.source() undo a.close()\n"
        "  let sub = subscribe a buffer 3 undo sub.close()\n"
        "  await sub.next()\n"
        "}\n", "s.rvl"))
    assert ('"error", 3)' in src) or ('"error", 3usize)' in src)


# ===========================================================================
# The ts REACTIVE tier (item 130, roadmap #81): a faithful async-generator
# mirror of the py reference (design §4.6), NOT a blocking erasure like go/rust.
# It lowers the core protocol (subscribe / next / close + the cancellation-first
# race), the Slice 2 derived-combinator chain + backpressure policies, the
# Slice 3 `merge` fan-in, the Slice 4 plain `every … in` iteration form, and the
# Slice 5 `on … as` typed-event handler (its schema-and-dedup contract gate, item
# 130 (roadmap #81)). The §4.5 durable `replay` is still the py reference tier's
# — a frontend refusal (a provider must declare it) that never reaches the
# emitter.
# ===========================================================================

def _ts():
    return _tier_emit("typescript")


def test_ts_lowers_the_source_only_program():
    """The item-416a honest refusal is gone for ts: `runtime.ts` carries a real
    `Stream`, so `Stream.source()` opens a provider through `host.Stream` rather
    than naming a host object that does not exist."""
    code = _ts().emit(compile_source(_SOURCE_ONLY, "s.rvl"))
    assert "host.Stream.source()" in code
    assert "src.close()" in code


def test_ts_emits_the_cancellation_first_subscription():
    """The core protocol: `subscribe` opens a single-consumer subscription
    through `host.Stream.subscribe`, passing `ctx` so a parked `next` observes
    owner withdrawal, and the bracket inverse is `sub.close()` — the
    cancellation-first close the guarantee rests on (design §4.6)."""
    code = _ts().emit(compile_source(_CONSUMER, "s.rvl"))
    assert 'host.Stream.subscribe(src, "error", ctx)' in code
    assert "() => src.close()" in code
    assert "() => sub.close()" in code
    # the `await sub.next()` lands, then the iteration boundary yields (A1)
    assert "await sub.next()" in code


def test_ts_emits_the_fan_in_inside_the_subscription():
    """Slice 3: the `merge(a, b)` fan-in is a derived stream opened INSIDE the
    subscription, so `sub.close()` unwinds it off the one bracket."""
    code = _ts().emit(compile_source(_FANIN, "s.rvl"))
    assert 'host.Stream.subscribe(host.Stream.merge(a, b), "error", ctx)' in code
    assert "() => sub.close()" in code


def test_ts_merge_nests():
    code = _ts().emit(compile_source("""
    component C {
      let a = effect Stream.source() undo a.close()
      let b = effect Stream.source() undo b.close()
      let c = effect Stream.source() undo c.close()
      let sub = subscribe merge(merge(a, b), c) undo sub.close()
      await sub.next()
    }
    """, "s.rvl"))
    assert "host.Stream.merge(host.Stream.merge(a, b), c)" in code


def test_ts_lowers_the_combinator_chain_and_backpressure():
    """Slice 2: unlike the blocking tiers (which refuse the chain and the lossy
    policies), the async mirror carries them — the derived chain is `stages`,
    the declared capacity is `capacity`, each appended only when present."""
    code = _ts().emit(compile_source("""
    component C {
      let a = effect Stream.source() undo a.close()
      let sub = subscribe a.map(x => x + 1).filter(x => x > 0).take(3) policy drop_oldest buffer 5 undo sub.close()
      await sub.next()
    }
    """, "s.rvl"))
    assert 'host.Stream.subscribe(a, "drop_oldest", ctx, {' in code
    assert '["map", ' in code and '["take", 3]' in code and '["filter", ' in code
    assert "capacity: 5" in code


def test_ts_lowers_the_block_drain_window():
    """The `block`-policy drain interval is `drainMs`, driven by the same
    deterministic clock the ts timer runtime already ships."""
    code = _ts().emit(compile_source("""
    component C {
      let a = effect Stream.source() undo a.close()
      let sub = subscribe a policy block drain 5s undo sub.close()
      await sub.next()
    }
    """, "s.rvl"))
    assert 'host.Stream.subscribe(a, "block", ctx, { drainMs: 5000 })' in code


def test_ts_a_slice_1_subscription_omits_the_options_object():
    """Byte discipline: a subscription with no chain / capacity / drain emits the
    plain three-argument call, never an empty `{}`."""
    code = _ts().emit(compile_source(_CONSUMER, "s.rvl"))
    assert 'host.Stream.subscribe(src, "error", ctx)' in code
    assert 'host.Stream.subscribe(src, "error", ctx, {' not in code


def test_ts_emits_the_iteration_form():
    """Slice 4: `every … in` is a `while (true)` over `await sub.next()`. Three
    load-bearing lines: the `yield` sits immediately after the await (a divert
    while parked abandons the loop, A1); a `Closed` terminal ENDS the loop before
    the body (`host.Stream.isClosed`); a `Faulted` is not tested — it THROWS out
    of `next`, failing the activation and reverting the subscription bracket."""
    code = _ts().emit(compile_source(_ITER, "s.rvl"))
    assert "while (true) {" in code
    assert "const o = await sub.next()" in code
    assert "yield () => {}  // iteration boundary (A1)" in code
    assert "if (host.Stream.isClosed(o)) break" in code
    # the item enters the body only after the terminal test
    assert code.index("host.Stream.isClosed(o)") < code.index("ctx.sink.write(o)")


def test_ts_the_iteration_body_forces_an_async_generator():
    """The await lives inside the loop, not at top level, so the async-generator
    detection must see the `stream-iter` step or the body would be a sync
    `function*` with an `await` in it (a tsc error)."""
    code = _ts().emit(compile_source(_ITER, "s.rvl"))
    assert "async function* ()" in code


# ===========================================================================
# Slice 4 — `every <x> in <sub> { … }`, the async-iteration form (§1, §4.7)
# ===========================================================================
#
# The form the roadmap's v1 spec names, built out of the three operations
# Slice 1 shipped: each turn awaits `<sub>.next()`, a `Closed` terminal ends the
# loop, a `Faulted` one raises out of it. Nothing about the lifecycle is new —
# the bracket is the `subscribe`'s, the teardown is the same LIFO stack — so
# what this suite pins is the parse (the `every` collision, §1 judgment call 1),
# the step IR, and the refusals that keep the loop from weakening the guarantee.
# The RUNTIME proof (an item per body turn, a terminal that is not an item, a
# handler failure closing the subscription) is the py suite at
# backends/python/tests/test_stream_runtime.py.

_ITER = """
service Sink { emission fn write(v: Str) }
component C requires sink: Sink {
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src undo sub.close()
  every o in sub {
    emit sink.write(o)
  }
}
"""


def _iter_step(src: str = _ITER) -> dict:
    body = compile_source(src, "s.rvl")["components"][0]["body"]
    return next(s for s in body if s.get("step") == "stream-iter")


# ---------------------------------------------------------------------------
# Parse + lowering: the `every` collision, and the step IR (§1, §5)
# ---------------------------------------------------------------------------

def test_stream_iteration_lowers_to_its_own_step_carrying_bind_subject_and_body():
    step = _iter_step()
    assert step["bind"] == "o"
    assert step["subject"] == {"kind": "name", "id": "sub"}
    assert [inner["step"] for inner in step["body"]] == ["emit"]
    assert step["body"][0]["expr"]["method"] == "write"
    # the item reaches the body as an ordinary local
    assert step["body"][0]["expr"]["args"] == [{"kind": "name", "id": "o"}]


def test_the_every_collision_is_one_token_of_lookahead():
    """§1, judgment call 1: `every <n><unit>` stays the timer, `every <x> in`
    is stream iteration. Both parse in one component, neither shadows the
    other."""
    ir = compile_source("""
    service Sink { emission fn write(v: Str) }
    component C requires sink: Sink {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      every 30s { emit sink.write("tick") }
      every o in sub { emit sink.write(o) }
    }
    """, "s.rvl")
    kinds = [s.get("step") for s in ir["components"][0]["body"]]
    assert "timer" in kinds and "stream-iter" in kinds


def test_a_non_stream_program_grows_no_stream_iter_step():
    """Byte-identity (§10.9): the additive step never appears in a program that
    does not iterate a stream."""
    ir = compile_source("""
    service L { emission fn note(v: Str) }
    component C requires log: L {
      let pool = effect Pool.open("u", 4) undo pool.close()
      every 30s { emit log.note("tick") }
    }
    """, "s.rvl")
    assert all(s.get("step") != "stream-iter"
               for s in ir["components"][0]["body"])


def test_the_body_emission_is_enumerated_on_the_component_boundary():
    """§4.7: the body is a setup-mode effect context — its emissions are
    capability-checked (G1) and reach the G8 audit as component reach, exactly
    as an activation-body `emit` does, because that is what they are."""
    step = _iter_step()
    emit_expr = step["body"][0]["expr"]
    assert emit_expr["target"] == {"kind": "req", "name": "sink"}


def test_an_undeclared_requirement_in_the_body_is_refused_g1():
    assert "not a declared requirement" in _refusal("""
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      every o in sub { emit sink.write(o) }
    }
    """)


# ---------------------------------------------------------------------------
# Admission: the rules the loop is its own owner of (§3.1, §4.7)
# ---------------------------------------------------------------------------

def test_iterating_something_that_is_not_a_subscription_is_refused():
    """The loop pulls the handle a `subscribe` bracket bound — not the SOURCE,
    whose own bracket is a different entry on the stack."""
    msg = _refusal("""
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      every o in src { emit sink.write(o) }
    }
    """)
    assert "needs a live subscription" in msg
    assert "let sub = subscribe" in msg


def test_a_second_iteration_of_one_subscription_is_refused_rule_3_1():
    msg = _refusal("""
    service Sink { emission fn write(v: Str) }
    component C requires sink: Sink {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      every o in sub { emit sink.write(o) }
      every p in sub { emit sink.write(p) }
    }
    """)
    assert "already iterated" in msg and "single-consumer" in msg
    assert "bridge" in msg


def test_an_acquisition_in_the_body_is_refused_naming_the_unbounded_stack():
    """§4.7: an `effect … undo …` per delivered item is one accumulator entry
    per item, unbounded in the length of the stream, and the per-iteration
    discharge that would bound it does not exist. Acquire before the loop."""
    msg = _refusal("""
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      every o in sub {
        let p = effect Pool.open("u", 1) undo p.close()
      }
    }
    """)
    assert "records emissions (and `fail`) only" in msg
    assert "unbounded in the length of the stream" in msg


def test_a_per_item_compensation_is_refused():
    msg = _refusal("""
    service Sink { emission fn write(v: Str) }
    component C requires sink: Sink {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      every o in sub { emit sink.write(o) compensate sink.write("undo") }
    }
    """)
    assert "cannot declare `compensate`" in msg
    assert "per delivered item" in msg


def test_a_nested_await_in_the_body_is_refused():
    """The loop IS the pull: a second `next` inside its own iteration would be a
    second consumer racing a single-consumer subscription (rule 3.1)."""
    msg = _refusal("""
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      every o in sub { await sub.next() }
    }
    """)
    assert "cannot `await`" in msg and "second consumer" in msg


def test_an_empty_iteration_body_is_refused():
    assert "is empty" in _refusal("""
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      every o in sub { }
    }
    """)


def test_iteration_after_provide_is_refused():
    """The loop runs to the stream's terminal, so a provision below it would be
    reached only once the stream ended (linker rule A2)."""
    msg = _refusal("""
    service Sink { emission fn write(v: Str) }
    service Q { fn v() -> Int }
    component C requires sink: Sink provides q: Q {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      provide q { fn v() -> Int { return 1 } }
      every o in sub { emit sink.write(o) }
    }
    """)
    assert "after `provide`" in msg


def test_iteration_is_refused_in_a_provide_method():
    """Rule 3.3: `next` is a suspension and lives only where a suspension is
    legal; a provide method runs while the component is ACTIVE."""
    msg = _refusal("""
    service Sink { emission fn write(v: Str) }
    service Q { fn v() -> Int }
    component C requires sink: Sink provides q: Q {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      provide q {
        fn v() -> Int {
          every o in sub { emit sink.write(o) }
          return 1
        }
      }
    }
    """)
    assert "activation body" in msg


def test_iterating_a_name_that_is_not_an_identifier_is_refused():
    assert "needs the name of a subscription" in _refusal("""
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      every o in 3 { emit sink.write(o) }
    }
    """)


# ---------------------------------------------------------------------------
# Emission: py renders the loop; every other tier REFUSES by name (§4.6)
# ---------------------------------------------------------------------------

def test_python_emits_the_cancellation_first_loop():
    emit = _tier_emit("python")
    code = emit.emit(compile_source(_ITER, "s.rvl"))
    assert "async def _body():" in code, "the loop is a suspension: async body"
    assert "while True:" in code
    assert "o = await sub.next()" in code
    # the await LANDS, then the yield closes the iteration (A1 inertia) — this
    # is what lets a divert abandon the loop instead of running one more turn
    assert "yield None  # iteration boundary (A1)" in code
    # a `Closed` terminal ends the loop and never enters the body
    assert "if Stream.is_closed(o):" in code
    assert "break" in code
    assert code.index("break") < code.index("_revl_ctx.sink.write(o)")
    # a `Faulted` terminal is not caught: it raises out of `next`, the
    # activation fails, and the prefix reverts LIFO with the bracket on it (A8)
    assert "StreamFaulted" not in code and "except" not in code


def test_go_emits_the_iteration_form_as_a_blocking_next_loop():
    """The go tier lowers `every … in` (Slice 4) as a plain for-loop over the
    cancel-channel `next` the Slice 1/3 protocol already ships — no new runtime.
    The three properties that carry the guarantee are each a line: a `Faulted`
    is the error return (it fails the activation, reverting the prefix with the
    subscription bracket on it — never caught); a `Closed` ENDS the loop before
    the body (it is a terminal, not an item); the item enters the body only
    after both, on the same LIFO-teardown-reachable bracket Slice 1 proved."""
    emit = _tier_emit("go")
    code = emit.emit(compile_source(_ITER, "s.rvl"))
    assert "for {" in code
    assert "_revlStreamItem1, _revlStreamErr1 := sub.Next()" in code
    # a Faulted terminal is the error return: uncaught, it fails the activation
    assert "if _revlStreamErr1 != nil {" in code
    assert "return nil, _revlStreamErr1" in code
    # a Closed terminal ends the loop and never enters the body
    assert "if IsStreamClosed(_revlStreamItem1) {" in code
    assert code.index("break") < code.index("sink.Write(o)")
    # the item is recovered and the body runs it
    assert "o := _revlStreamItem1.(string)" in code
    assert code.index("o := _revlStreamItem1.(string)") < code.index("sink.Write(o)")


def test_rust_emits_the_iteration_form_as_a_blocking_next_loop():
    """The rust tier lowers `every … in` (Slice 4) as a plain `loop` over the
    cancel-select `next` the Slice 1/3 protocol already ships — no new runtime,
    the SAME shape the go tier lowers. The three properties that carry the
    guarantee are each a line: a `Faulted` is the `Err` that `map_err(…)?`
    propagates (it fails the activation, reverting the prefix with the
    subscription bracket on it — never caught); a `Closed` terminal ENDS the
    loop before the body (it is a terminal, not an item); the item enters the
    body only after both, on the same LIFO-teardown-reachable bracket."""
    emit = _tier_emit("rust")
    code = emit.emit(compile_source(_ITER, "s.rvl"))
    assert "loop {" in code
    # `next` on the subscription, with the Faulted terminal propagated uncaught
    assert ("match sub.next().map_err(|e| cordis::CordisError::with_message("
            "cordis::ErrorCode::Plugin, e))? {") in code
    # a Closed terminal ends the loop and never enters the body
    assert "StreamNext::Closed => break," in code
    assert code.index("StreamNext::Closed => break,") < code.index("sink.write(o)")
    # the item binds and the body runs it
    assert "StreamNext::Item(o) => {" in code
    assert code.index("StreamNext::Item(o) => {") < code.index("sink.write(o)")


# item 130 (roadmap #81): this tier is no longer a refusal here — it lowers the
# handler. See `test_rust_lowers_the_typed_event_handler_with_an_additive_contract_gate`
# in the Slice 5 section for the emitted shape, and
# backends/rust/test_emit_rust.py::test_stream_runtime_on_real_cordis_rs for the
# runtime proof.


def test_java_emits_the_iteration_form_as_a_blocking_next_loop():
    """The java tier lowers `every … in` (Slice 4) as a plain `while (true)` loop
    over the cancellation-first `next` the Slice 1/3 protocol already ships — no
    new runtime, the SAME shape go and rust lower. The three properties that
    carry the guarantee are each a line: a `Faulted` is the `CordisException`
    `next` throws, uncaught, so the activation's own `catch` disposes the
    accumulated prefix (subscription bracket included) and rethrows; a `Closed`
    terminal ENDS the loop before the body (it is a terminal, not an item); the
    item enters the body only after both."""
    emit = _tier_emit("java")
    code = emit.emit(compile_source(_ITER, "s.rvl"))
    assert "while (true) {" in code
    assert "Object _revlStreamItem1 = sub.next();" in code
    # a Closed terminal ends the loop and never enters the body
    assert "if (Stream.isClosed(_revlStreamItem1)) {" in code
    assert code.index("break;") < code.index("sink.write(o)")
    # the item is recovered and the body runs it
    assert "String o = (String) _revlStreamItem1;" in code
    assert code.index("String o = (String) _revlStreamItem1;") < code.index(
        "sink.write(o)")
    # a Faulted terminal is NOT caught in the loop: nothing between the `next`
    # and the body catches anything, so the throw leaves the activation and the
    # A8 self-revert below disposes the accumulated prefix and rethrows.
    loop = code[code.rindex("while (true) {"):code.index("sink.write(o)")]
    assert "catch" not in loop and "try" not in loop
    assert "fx.dispose();" in code


def test_wasm_still_refuses_the_iteration_program():
    """wasm refuses the whole stream surface, and the subscription the loop needs
    is refused before the loop is reached — so an iteration program gets the same
    honest refusal a Slice 1 one does.

    item 130 (roadmap #81): `typescript` and then `java` left this set — both
    lower the plain `every … in` iteration form (see
    `test_ts_emits_the_iteration_form` and
    `test_java_emits_the_iteration_form_as_a_blocking_next_loop`)."""
    emit = _tier_emit("wasm")
    with pytest.raises(emit.EmitError) as excinfo:
        emit.emit(compile_source(_ITER, "s.rvl"))
    assert "suspends a fiber" in str(excinfo.value)


# ===========================================================================
# Slice 5 — typed EVENTS: `event E(key: f)` + `on E as e in sub { … }` (§6)
# ===========================================================================
#
# §6's call is that events are a `Stream[T]` SPECIALIZATION, not a second
# surface: `on … as` desugars to Slice 4's `every … in`, and everything the
# guarantee rests on — the subscription bracket, the cancellation-first `next`,
# the iteration boundary, the uncaught `Faulted` — is that slice's, unchanged.
# So this suite pins the two things events ADD and the one thing they must not
# move.
#
#   ADD (1): a SCHEMA. `event E { … }` is a record, so `e.<field>` is checked by
#   the ordinary record rules, and every delivered item is validated against the
#   derived schema at the boundary before the body runs.
#   ADD (2): an identity KEY and the bounded dedup window it enables, so a
#   redelivery is collapsed instead of running the handler twice.
#   MUST NOT MOVE: the emitted loop. The contract is built ONCE above it, and the
#   gate sits after the await, after its `yield`, and after the terminal test.
#
# The runtime proof (an item validated before the body, a duplicate collapsed, a
# schema violation closing the subscription) is the py suite at
# backends/python/tests/test_stream_runtime.py.

_EVENT = """
event OrderCreated(key: order_id) { order_id: Str, quantity: Int }
service Ship { emission fn dispatch(id: Str) }
component Fulfiller requires ship: Ship {
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src undo sub.close()
  on OrderCreated as e in sub {
    emit ship.dispatch(e.order_id)
  }
}
"""


def _event_step(src: str = _EVENT) -> dict:
    body = compile_source(src, "s.rvl")["components"][0]["body"]
    return next(s for s in body if s.get("step") == "stream-iter")


# ---------------------------------------------------------------------------
# The specialization: one node, one step, one loop (§6)
# ---------------------------------------------------------------------------

def test_a_handler_lowers_to_the_slice_4_step_with_an_additive_contract():
    """§6: `on … as` DESUGARS to `every … in`. The handler is the same
    `stream-iter` step — same bind, same subject, same body — plus the contract
    the event adds. If these ever became two steps, the two forms could drift
    apart on the properties that carry the guarantee."""
    step = _event_step()
    assert step["step"] == "stream-iter"
    assert step["bind"] == "e"
    assert step["subject"] == {"kind": "name", "id": "sub"}
    assert [inner["step"] for inner in step["body"]] == ["emit"]
    contract = step["event"]
    assert contract["name"] == "OrderCreated"
    assert contract["key"] == "order_id"
    assert contract["window"] == 64, "the documented default dedup window"
    assert contract["schema"] == {
        "type": "object",
        "properties": {"order_id": {"type": "string"},
                       "quantity": {"type": "integer"}},
        "required": ["order_id", "quantity"],
    }


def test_a_plain_iteration_carries_no_contract_key():
    """The contract is ADDITIVE: a Slice 4 `every … in` lowers exactly as it did
    before Slice 5 existed."""
    assert "event" not in _iter_step()


def test_an_event_is_an_ordinary_record_type():
    """§6, "schema compatibility": the event's record half is an ordinary record
    type-check on `T` — the declaration contributes a normal record to the type
    table, with no event-specific case anywhere in the type machinery."""
    ir = compile_source(_EVENT, "s.rvl")
    assert ir["types"]["OrderCreated"] == {
        "params": [], "kind": "record",
        "fields": {"order_id": "Str", "quantity": "Int"},
    }


def test_a_declared_window_overrides_the_default():
    step = _event_step("""
    event E(key: id, window: 8) { id: Str }
    service Sink { emission fn write(v: Str) }
    component C requires sink: Sink {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      on E as e in sub { emit sink.write(e.id) }
    }
    """)
    assert step["event"]["window"] == 8


def test_an_event_declaration_alone_changes_no_component():
    """An event that nothing handles is a record and nothing more: it adds no
    step, no capability and no runtime. The contract reaches the emitters on the
    ONE step that uses it, never as a global table."""
    ir = compile_source("""
    event E(key: id) { id: Str }
    component C {
      let pool = effect Pool.open("u", 4) undo pool.close()
    }
    """, "s.rvl")
    assert ir["types"]["E"]["kind"] == "record"
    assert all("event" not in step for step in ir["components"][0]["body"])


# ---------------------------------------------------------------------------
# `event` and `on` are CONTEXTUAL — no keyword, no lexer sync
# ---------------------------------------------------------------------------

def test_event_and_on_stay_ordinary_identifiers_elsewhere():
    """Both words are recognised only in their own shape (`event IDENT (`/`{`,
    `on IDENT as`), the `boot`/`fault`/`secret` discipline. The corpus really
    does use `event` as a parameter name, so promoting it to a lexer keyword
    would have broken shipped programs — and the self-hosted lexer's KEYWORDS
    table (and the gate crate's frontier derived from it) needs no sync."""
    ir = compile_source("""
    service Audit { emission fn log(event: Str) }
    component C requires audit: Audit {
      config { on: Str = "yes" }
      emit audit.log(config.on)
    }
    """, "s.rvl")
    assert ir["components"][0]["body"][0]["step"] == "emit"


def test_a_handler_and_a_timer_and_an_iteration_coexist():
    ir = compile_source("""
    event E(key: id) { id: Str }
    service Sink { emission fn write(v: Str) }
    component C requires sink: Sink {
      let a = effect Stream.source() undo a.close()
      let b = effect Stream.source() undo b.close()
      let sa = subscribe a undo sa.close()
      let sb = subscribe b undo sb.close()
      every 30s { emit sink.write("tick") }
      every o in sa { emit sink.write(o) }
      on E as e in sb { emit sink.write(e.id) }
    }
    """, "s.rvl")
    steps = ir["components"][0]["body"]
    kinds = [s.get("step") for s in steps]
    assert kinds.count("stream-iter") == 2 and "timer" in kinds
    contracts = [s.get("event") for s in steps if s.get("step") == "stream-iter"]
    assert [c is None for c in contracts] == [False, True] or \
           [c is None for c in contracts] == [True, False]


# ---------------------------------------------------------------------------
# The contract's refusals (§6)
# ---------------------------------------------------------------------------

def test_an_event_needs_a_key():
    """The key is REQUIRED: it is what makes the two obligations §6 assigns to
    events beyond a plain stream — handler idempotency and duplicate handling —
    checkable at all."""
    msg = _refusal("""
    event E { id: Str }
    component C { }
    """)
    assert "needs a key" in msg and "key: <field>" in msg


def test_the_key_must_name_a_field_of_the_event():
    msg = _refusal("""
    event E(key: nope) { id: Str }
    component C { }
    """)
    assert "is not one of its fields (id)" in msg


def test_the_key_must_be_scalar_serializable():
    """A duplicate is recognised by comparing key VALUES across deliveries, so a
    compound key has no stable identity to dedup on (item 309 §1b's rule, on the
    consumer side of the same idea)."""
    msg = _refusal("""
    type P = { a: Str }
    event E(key: p) { id: Str, p: P }
    component C { }
    """)
    assert "not scalar-serializable" in msg


def test_the_dedup_window_is_bounded_and_positive():
    msg = _refusal("""
    event E(key: id, window: 0) { id: Str }
    component C { }
    """)
    assert "positive whole count" in msg
    assert "never one entry per delivered item" in msg


def test_an_event_with_no_fields_is_refused():
    assert "declares no fields" in _refusal("""
    event E(key: id) { }
    component C { }
    """)


def test_a_handler_over_an_undeclared_event_is_refused():
    msg = _refusal("""
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      on Nope as e in sub { fail "x" }
    }
    """)
    assert "names no declared event" in msg


def test_a_plain_record_cannot_stand_in_for_an_event():
    """A `type` carries no identity key, so `on` over one would be `every` with
    extra syntax — and the two rows §6 assigns to events would be a claim
    nothing backs."""
    msg = _refusal("""
    type E = { id: Str }
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      on E as e in sub { fail "x" }
    }
    """)
    assert "is a `type`, not an `event`" in msg


def test_a_handler_with_no_clause_does_not_capture_a_local_subscription():
    """Dropping `in <sub>` resolves the handler's source from the component's
    REQUIRED `Stream[T]` coeffect (§6b) — never from whatever subscription
    happens to be in scope. A component that owns a local source and no stream
    requirement has nothing to resolve, and says so; silently capturing `sub`
    would make the clause's presence change which stream a handler reads."""
    msg = _refusal("""
    event E(key: id) { id: Str }
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      on E as e { fail "x" }
    }
    """)
    assert "has no stream to resolve" in msg
    assert "requires no `Stream[...]`" in msg


def test_a_handler_over_something_that_is_not_a_subscription_is_refused():
    msg = _refusal("""
    event E(key: id) { id: Str }
    component C {
      let src = effect Stream.source() undo src.close()
      on E as e in src { fail "x" }
    }
    """)
    assert "needs a live subscription" in msg


def test_a_second_handler_on_one_subscription_is_refused_rule_3_1():
    """Rule 3.1 on the handle is Slice 4's, inherited: an event handler consumes
    its subscription to the terminal exactly as a plain iteration does."""
    msg = _refusal("""
    event E(key: id) { id: Str }
    service Sink { emission fn write(v: Str) }
    component C requires sink: Sink {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      on E as e in sub { emit sink.write(e.id) }
      on E as f in sub { emit sink.write(f.id) }
    }
    """)
    assert "already iterated" in msg and "single-consumer" in msg


# ---------------------------------------------------------------------------
# The typed item: what an event buys the checker over a plain iteration
# ---------------------------------------------------------------------------

def test_the_item_is_typed_so_an_undeclared_field_is_a_compile_error():
    """The checked half of "schema compatibility": under `on`, the item has the
    event's record type for the length of the body, where a plain `every`'s item
    is untyped and admits any field read."""
    msg = _refusal("""
    event E(key: id) { id: Str }
    service Sink { emission fn write(v: Str) }
    component C requires sink: Sink {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      on E as e in sub { emit sink.write(e.nope) }
    }
    """)
    assert "`event E` has no field `nope`" in msg


def test_the_item_type_reaches_the_emission_signature():
    msg = _refusal("""
    event E(key: id) { id: Str, quantity: Int }
    service Sink { emission fn write(v: Str) }
    component C requires sink: Sink {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      on E as e in sub { emit sink.write(e.quantity) }
    }
    """)
    assert "expects `Str`" in msg and "got `Int`" in msg


def test_the_item_does_not_outlive_the_handler_body():
    """The bind is the body's, exactly as Slice 4's is: it names ONE delivered
    item and there is no last item once the terminal arrived."""
    msg = _refusal("""
    event E(key: id) { id: Str }
    service Sink { emission fn write(v: Str) }
    component C requires sink: Sink {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      on E as e in sub { emit sink.write(e.id) }
      emit sink.write(e.id)
    }
    """)
    assert "e" in msg


# ---------------------------------------------------------------------------
# The body is Slice 4's body — including the acquisition refusal, unchanged
# ---------------------------------------------------------------------------

def test_an_acquisition_in_a_handler_body_is_still_refused():
    """Slice 5 does not change the §4.7 calculus and does not lift the refusal.
    An event's dedup memory is a FIXED-SIZE window per handler, constant in the
    length of the stream; an `effect … undo …` per delivered item is still one
    accumulator entry per item, and the per-iteration discharge that would bound
    it still does not exist."""
    msg = _refusal("""
    event E(key: id) { id: Str }
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      on E as e in sub {
        let p = effect Pool.open("u", 1) undo p.close()
      }
    }
    """)
    assert "records emissions (and `fail`) only" in msg
    assert "unbounded in the length of the stream" in msg


def test_a_per_item_compensation_in_a_handler_is_refused():
    msg = _refusal("""
    event E(key: id) { id: Str }
    service Sink { emission fn write(v: Str) }
    component C requires sink: Sink {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      on E as e in sub { emit sink.write(e.id) compensate sink.write("undo") }
    }
    """)
    assert "cannot declare `compensate`" in msg


def test_a_nested_await_in_a_handler_is_refused():
    msg = _refusal("""
    event E(key: id) { id: Str }
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      on E as e in sub { await sub.next() }
    }
    """)
    assert "cannot `await`" in msg and "second consumer" in msg


def test_an_empty_handler_body_is_refused():
    assert "is empty" in _refusal("""
    event E(key: id) { id: Str }
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      on E as e in sub { }
    }
    """)


def test_a_handler_in_a_provide_method_is_refused():
    msg = _refusal("""
    event E(key: id) { id: Str }
    service Sink { emission fn write(v: Str) }
    service Q { fn v() -> Int }
    component C requires sink: Sink provides q: Q {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      provide q {
        fn v() -> Int {
          on E as e in sub { emit sink.write(e.id) }
          return 1
        }
      }
    }
    """)
    assert "activation body" in msg


def test_a_handler_after_provide_is_refused():
    msg = _refusal("""
    event E(key: id) { id: Str }
    service Sink { emission fn write(v: Str) }
    service Q { fn v() -> Int }
    component C requires sink: Sink provides q: Q {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      provide q { fn v() -> Int { return 1 } }
      on E as e in sub { emit sink.write(e.id) }
    }
    """)
    assert "after `provide`" in msg


def test_the_handler_body_emission_is_enumerated_on_the_boundary():
    """§4.7, inherited: the body is a setup-mode effect context, so its
    emissions are capability-checked (G1) and reach the G8 audit as component
    reach — because they lower through the same `_lower_emit_step` and the same
    `env` an activation-body `emit` does."""
    step = _event_step()
    assert step["body"][0]["expr"]["target"] == {"kind": "req", "name": "ship"}


def test_an_undeclared_requirement_in_a_handler_body_is_refused_g1():
    msg = _refusal("""
    event E(key: id) { id: Str }
    service Sink { emission fn write(v: Str) }
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      on E as e in sub { emit sink.write(e.id) }
    }
    """)
    assert "sink" in msg


# ---------------------------------------------------------------------------
# Emission: py renders the gate; every other tier REFUSES by name (§4.6)
# ---------------------------------------------------------------------------

def test_python_emits_the_contract_above_the_loop_and_the_gate_below_the_yield():
    """Where each line sits is the argument that the contract costs the
    guarantee nothing: the contract is built ONCE above the loop (constant dedup
    memory, not one entry per item), and the gate sits AFTER the await, after
    its `yield` (the iteration boundary a divert-while-parked depends on), and
    after the terminal test (a `Closed` is not an item to validate)."""
    emit = _tier_emit("python")
    code = emit.emit(compile_source(_EVENT, "s.rvl"))
    contract = next(line for line in code.splitlines()
                    if "Stream.contract(" in line)
    assert "'OrderCreated'" in contract and "'order_id'" in contract
    assert contract.rstrip().endswith(", 64)"), "the bounded window is emitted"
    gate = next(line for line in code.splitlines() if ".admit(e," in line)
    assert gate.strip().startswith("if not ")
    order = [code.index(contract), code.index("while True:"),
             code.index("e = await sub.next()"),
             code.index("yield None  # iteration boundary (A1)"),
             code.index("if Stream.is_closed(e):"),
             code.index(gate),
             code.index("_revl_ctx.ship.dispatch(")]
    assert order == sorted(order), "the contract is above the loop, the gate below the yield"
    # a duplicate skips THIS item and pulls the next one; it never leaves the loop
    assert "continue" in code
    # a `Faulted` — including a schema violation — is still not caught (A8)
    assert "StreamFaulted" not in code and "except" not in code


def test_the_emitted_gate_runs_before_the_body_not_after():
    emit = _tier_emit("python")
    code = emit.emit(compile_source(_EVENT, "s.rvl"))
    assert code.index(".admit(e,") < code.index("_revl_ctx.ship.dispatch(")


def test_go_lowers_a_stream_component_that_also_declares_a_record():
    """The routing hole the go refusal once named is now CLOSED by lowering: a
    stream-holding component in a document that also declares a record (the shape
    every typed event takes) is diverted to the live stc-go path — the component
    is kept and its record type materialized — rather than dropped by the pure
    typed-core path. The subscription's bracket survives."""
    emit = _tier_emit("go")
    code = emit.emit(compile_source("""
    type Foo = { a: Str }
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      await sub.next()
    }
    """, "s.rvl"))
    # the component is emitted (not dropped) with its subscription bracket, and
    # the record type is materialized alongside it.
    assert "func LoadC(" in code
    assert 'sub = StreamSubscribe(src, "error", 0)' in code
    assert "type Foo struct {" in code


def test_go_diverts_a_source_only_component_beside_a_top_level_declaration():
    """The same routing rule, on the stream surface it did not cover.

    `_document_holds_stream` recognised a `subscribe` bracket and a `stream-iter`
    body step, and nothing else. A component that only ACQUIRES a provider —
    `let src = effect Stream.source() undo src.close()`, the shape
    `test_source_only_program_still_emits_on_the_lowered_tiers` already requires
    this tier to emit — carries neither, so the moment the document also held a
    top-level declaration it routed to the pure typed-core path and the whole
    component vanished: no subscription, no `Close` inverse, no diagnostic. A
    live provider is a live host listener whether or not anything is reading it,
    so it is diverted like every other stream component."""
    emit = _tier_emit("go")
    code = emit.emit(compile_source("""
    type Foo = { a: Str }
    component C {
      let src = effect Stream.source() undo src.close()
    }
    """, "s.rvl"))
    assert "func LoadC(" in code
    assert "StreamSource()" in code


@pytest.mark.parametrize("tail, named", [
    ("fn tag(s: Str) -> Str { return s }", "functions"),
    ('test "t" { assert 1 == 1 }', "tests"),
])
def test_go_refuses_a_stream_document_whose_top_level_it_would_drop(tail, named):
    """The other direction of the same routing decision, and the same rule.

    A stream document is diverted to the live stc-go path so the component is not
    dropped. That path renders types, externs, services, components and lifecycle
    tests — it renders no top-level `fn` and no plain `test` block. So the
    diversion silently dropped whatever the pure path would have emitted, and the
    document compiled to a module missing a section the author wrote. Refused by
    name instead: the tier states which section it cannot carry beside a stream
    rather than answering with a module that is quietly short of one."""
    emit = _tier_emit("go")
    src = tail + """
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      await sub.next()
    }
    """
    with pytest.raises(emit.EmitError) as excinfo:
        emit.emit(compile_source(src, "s.rvl"))
    message = str(excinfo.value)
    assert named in message
    assert "item 130" in message


def test_go_still_emits_a_stream_document_whose_top_level_it_can_carry():
    """The control for the refusal above: a `type` (every typed event declares
    one) and an `extern` ARE rendered on the live path, so a stream document
    carrying them still emits."""
    emit = _tier_emit("go")
    code = emit.emit(compile_source("""
    type Foo = { a: Str }
    component C {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      await sub.next()
    }
    """, "s.rvl"))
    assert "type Foo struct {" in code
    assert 'sub = StreamSubscribe(src, "error", 0)' in code


@pytest.mark.parametrize("program", [_CONSUMER, _SOURCE_ONLY])
def test_the_wasm_refusal_names_every_tier_that_lowers_a_stream(program):
    """wasm's refusal is correct behaviour, not a gap (§4.6, §8: no async host
    seam, and it skips `advance`). That is precisely why the tier list inside it
    has to stay true: routing the author somewhere that works is the refusal's
    only remaining job, and a list that omits a working tier is a wrong answer
    delivered by a right refusal.

    It read "py, go, rust" long after ts and java had both graduated onto the
    surface. So the list is checked against the EMITTERS — every tier named must
    actually emit this program, and every tier that emits it must be named —
    rather than against a comment that can go stale the same way."""
    named = {"py": "python", "ts": "typescript", "go": "go",
             "java": "java", "rust": "rust"}
    wasm = _tier_emit("wasm")
    with pytest.raises(wasm.EmitError) as excinfo:
        wasm.emit(compile_source(program, "s.rvl"))
    message = str(excinfo.value)
    # Read the list out of the sentence rather than searching the whole message
    # for each short name: "awaits" contains "ts", so a substring test would
    # find the typescript tier in a message that never names it.
    found = re.search(r"lower the subscription protocol \(([^)]*)\)", message)
    assert found is not None, message
    listed = {t.strip() for t in found.group(1).split(",")}
    lowers = set()
    for short, tier in named.items():
        try:
            _tier_emit(tier).emit(compile_source(program, "s.rvl"))
        except Exception:  # noqa: BLE001 — any refusal means "does not lower"
            continue
        lowers.add(short)
    assert lowers == set(named), "a tier stopped lowering the stream surface"
    assert listed == lowers, (
        f"the wasm refusal names {sorted(listed)}; these tiers lower a stream: "
        f"{sorted(lowers)}")


def test_wasm_still_refuses_a_handler_program():
    emit = _tier_emit("wasm")
    with pytest.raises(emit.EmitError) as excinfo:
        emit.emit(compile_source(_EVENT, "s.rvl"))
    assert "suspends a fiber" in str(excinfo.value)


def test_ts_lowers_the_typed_event_handler_with_an_additive_contract_gate():
    """item 130 (roadmap #81): the ts tier graduated Slice 5 — it lowers the
    `on … as` typed-event handler, no longer refusing it. The handler is the
    SAME `while (true)` loop the plain `every … in` emits (the specialization,
    §6) plus one gate: the contract is built ONCE above the loop, and the
    `admit` gate sits AFTER the terminal test and its `continue` collapses a
    duplicate — so the iteration boundary the guarantee rests on does not move.
    The runtime proof (a conforming item reaches the body, a duplicate is
    collapsed, a schema violation faults and closes) is backends/typescript/
    test_stream_ts.py; this pins the emitted shape."""
    ts = _tier_emit("typescript").emit(compile_source(_EVENT, "s.rvl"))
    # the contract is built once, above the loop, from the derived schema
    assert 'host.Stream.contract("OrderCreated", ' in ts
    assert '"order_id", 64)' in ts, "the derived key and default window"
    # exactly one loop (one node, one lowering), with the gate after the terminal
    assert ts.count("while (true) {") == 1
    gate = ts.index(".admit(")
    closed = ts.index("host.Stream.isClosed(")
    assert closed < gate, "the gate sits after the `Closed` terminal test"
    assert "if (!_revlEvent1.admit(e, " in ts and ")) continue" in ts


def test_go_lowers_the_typed_event_handler_with_an_additive_contract_gate():
    """item 130 (roadmap #81): the go tier graduated Slice 5 — it lowers the
    `on … as` typed-event handler, no longer refusing it. The handler is the
    SAME blocking `for {}` `next` loop the plain `every … in` emits (the
    specialization, §6) plus one gate: the contract is built ONCE above the loop,
    the `admit` call sits AFTER the terminal test, a schema violation is the
    error return that faults the activation (go cannot raise), and a duplicate
    `continue`s — so the iteration boundary the guarantee rests on does not move.
    The runtime proof (a conforming item reaches the body, a duplicate is
    collapsed, a schema violation faults and leaves no residue) is
    backends/go/test_stream_exec_130.py; this pins the emitted shape."""
    go = _tier_emit("go").emit(compile_source(_EVENT, "s.rvl"))
    # the contract is built once, above the loop, from the derived schema
    assert 'StreamContract("OrderCreated", ' in go
    assert '"order_id", 64)' in go, "the derived key and default window"
    # exactly one loop (one node, one lowering), with the gate after the terminal
    assert go.count("for {") == 1
    gate = go.index(".admit(")
    closed = go.index("IsStreamClosed(")
    assert closed < gate, "the gate sits after the `Closed` terminal test"
    # a schema violation faults (uncaught error return); a duplicate continues
    assert "_revlEventOk1, _revlEventErr1 := _revlEvent1.admit(" in go
    assert go.index("return nil, _revlEventErr1") < go.index("if !_revlEventOk1")
    assert "continue" in go
    # the validated item decodes into the event's record struct for the body
    assert "var e OrderCreated" in go
    assert go.index("var e OrderCreated") < go.index("e.OrderId")


def test_rust_lowers_the_typed_event_handler_with_an_additive_contract_gate():
    """item 130 (roadmap #81): the rust tier graduated Slice 5 — it lowers the
    `on … as` typed-event handler, no longer refusing it. The handler is the
    SAME blocking `loop` the plain `every … in` emits (the specialization, §6)
    plus one gate, and the tier erases the async color, so there is no `yield`
    to move: the iteration boundary IS the return of the blocking `next`, and
    the gate sits between that boundary and the body.

    What must NOT move, and does not: `Closed => break` is still the FIRST arm
    (a terminal ends the loop without being validated or delivered as an item),
    and the gate's refusal is a `return Err(...)` — the same uncaught error the
    `Faulted` arm propagates, so the activation fails and the prefix reverts
    LIFO with the subscription bracket on it (go/ts express this as an error
    return / a throw; rust has `Result`). The runtime proof (an item reaches the
    typed body, a redelivery of its identity key collapses, a schema violation
    faults and leaves no residue, the dedup window is bounded) is
    backends/rust/scenarios/stream.rs, run by
    backends/rust/test_emit_rust.py::test_stream_runtime_on_real_cordis_rs."""
    code = _tier_emit("rust").emit(compile_source(_EVENT, "s.rvl"))
    # the contract is built ONCE above the loop, from the derived schema
    assert 'Stream::contract("OrderCreated", ' in code
    assert '"order_id", 64)' in code, "the derived key and default window"
    # exactly one iteration node (one lowering), with the gate after the terminal
    # (the host's own `next` wait loop is not a lowering and lives in the shim)
    assert code.count("StreamNext::Closed => break,") == 1
    closed = code.index("StreamNext::Closed => break,")
    gate = code.index(".admit(&e, ")
    assert closed < gate, "the gate sits after the `Closed` terminal test"
    # the three outcomes: run, collapse, FAULT (uncaught, so the prefix reverts)
    assert "Ok(true) => {}" in code
    assert "Ok(false) => continue," in code
    assert "Err(_revl_event1_err) => return Err(cordis::CordisError::with_message(" in code
    # the validated item decodes into the event's record for the typed body
    assert "let e: OrderCreated = match serde_json::from_value::<OrderCreated>(" in code
    assert code.index("let e: OrderCreated") < code.index("ship.dispatch(e.order_id)")
    assert "except" not in code and "catch" not in code


def test_java_lowers_the_typed_event_handler_with_an_additive_contract_gate():
    """item 130 (roadmap #81): the java tier graduated Slice 5 — it lowers the
    `on … as` typed-event handler. The handler is the SAME blocking `while (true)`
    loop the plain `every … in` emits (the specialization, §6) plus ONE gate, and
    the tier erases the async color, so there is no `yield` to move: the iteration
    boundary IS the return of the blocking `next`, and the gate sits between that
    boundary and the body.

    What must NOT move, and does not: the `Closed` test is still FIRST (a terminal
    ends the loop without being validated or delivered as an item), and the gate's
    refusal is the `CordisException` `admit` throws — uncaught, the same way a
    `Faulted` out of `next` is, so the activation fails and the prefix reverts
    LIFO with the subscription bracket on it (go/rust express this as an error
    return / an `Err`; java, like py and ts, raises). The runtime proof (an item
    reaches the typed body, a redelivery of its identity key collapses, a schema
    violation faults and leaves no residue) is
    backends/java/test_stream_exec_java_130.py."""
    code = _tier_emit("java").emit(compile_source(_EVENT, "s.rvl"))
    # the contract is built ONCE above the loop, from the derived schema
    assert 'new EventContract("OrderCreated", ' in code
    assert '"order_id", 64)' in code, "the derived key and default window"
    # exactly one iteration node (one lowering): the host's own `next` park loop
    # is not a lowering and lives in the emitted runtime, which is why the count
    # is taken on the terminal test rather than on `while (true)`.
    assert code.count("if (Stream.isClosed(_revlStreamItem1)) {") == 1
    closed = code.index("if (Stream.isClosed(_revlStreamItem1)) {")
    gate = code.index("_revlEvent1.admit(")
    assert closed < gate, "the gate sits after the `Closed` terminal test"
    # the three outcomes: run, collapse, FAULT (uncaught, so the prefix reverts)
    assert ("java.util.Map<String, Object> _revlEventObj1 = _revlEvent1.admit("
            in code)
    assert "if (_revlEventObj1 == null) {" in code
    assert "continue;" in code
    loop = code[code.rindex("while (true) {"):code.index("ship.dispatch(")]
    assert "catch" not in loop and "try" not in loop
    # the validated item is constructed into the event's record for the typed body
    assert ('OrderCreated e = new OrderCreated((String) _revlEventObj1.get('
            '"order_id"), ((Number) _revlEventObj1.get("quantity")).longValue());'
            in code)
    assert code.index("OrderCreated e = new OrderCreated(") < code.index(
        "ship.dispatch((e).order_id)")


def test_java_pulls_the_event_contract_runtime_only_for_a_handler_program():
    """The `EventContract` half of the java stream runtime — the derived-schema
    validator and the bounded dedup window — is gated on the document carrying an
    `on … as` handler, so a plain `every … in` program emits without it (§10.9's
    "a tier pays for what it uses"). It is also the only half that needs the
    hand-written JSON reader, the JDK shipping no binder."""
    handler = _tier_emit("java").emit(compile_source(_EVENT, "s.rvl"))
    assert "public static final class EventContract {" in handler
    assert "static final class RevlJson {" in handler
    plain = _tier_emit("java").emit(compile_source(_ITER, "s.rvl"))
    assert "public static final class Stream {" in plain
    assert "EventContract" not in plain
    assert "RevlJson" not in plain


def test_java_refuses_an_event_field_shape_it_cannot_read_back():
    """The one place the java handler is narrower than go's and rust's, refused by
    NAME rather than decoded approximately. go binds a validated item with
    `encoding/json` and rust with `serde`; the JDK ships no JSON binder, so this
    tier constructs the event record field by field and covers the scalars, a
    nested record and `List[T]` over those. A union-valued field has no such
    reading, and a handler that silently mis-bound one would run its body on
    invented data — the worst outcome available (§6)."""
    emit = _tier_emit("java")
    ir = compile_source("""
    type Channel = Web | Api
    event OrderPlaced(key: order_id) { order_id: Str, via: Channel }
    service Sink { emission fn write(v: Str) }
    component C requires sink: Sink {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src undo sub.close()
      on OrderPlaced as e in sub { emit sink.write(e.order_id) }
    }
    """, "s.rvl")
    with pytest.raises(emit.EmitError) as excinfo:
        emit.emit(ir)
    msg = str(excinfo.value)
    assert "unsupported" not in msg
    assert "event `OrderPlaced` field `via`" in msg
    assert "Channel" in msg
    assert "--backend py" in msg


def test_a_multifile_build_carries_event_declarations_into_the_merged_program(
        tmp_path):
    """The frontend prerequisite the rust graduation exposed.

    `compile_source` parses one in-memory document; the CLI compiles through
    `compile_files`, which merges each module into a synthetic program before
    lowering. That merge copied `type_decls`, `fn_decls`, `externs`, `tests`,
    `prop_tests`, `secrets`, `components` and `services` but NOT `event_decls`,
    and the module rewriter never visited `StreamIterStmt` or an event's name,
    so a build down the CLI path lost the contract table and refused `on … as`
    with "`OrderCreated` is a type, not an event" while the same source compiled
    in memory. Nothing caught it because every stream test in this suite uses
    `compile_source`; the rust scenario, which is compiled through
    `compile_files`, did. Pin the merged program carries the contract the
    handler lowers against."""
    source = tmp_path / "s.rvl"
    source.write_text(_EVENT)
    ir = compile_files([str(source)])
    steps = [s for c in ir["components"] for s in c.get("body", ())
             if isinstance(s, dict) and s.get("step") == "stream-iter"]
    assert len(steps) == 1, "the merged program kept the handler component"
    assert steps[0]["event"]["name"] == "OrderCreated"
    assert steps[0]["event"]["key"] == "order_id"
    assert steps[0]["event"]["window"] == 64
    assert steps[0]["event"]["schema"]["required"] == ["order_id", "quantity"]


# ---------------------------------------------------------------------------
# The required `Stream[T]` coeffect (§4.3, §6b): `requires <k>: Stream[T]`, and
# the handler that resolves its source from it instead of naming a subscription
# ---------------------------------------------------------------------------

_COEFFECT = """
event OrderCreated(key: order_id) { order_id: Str, quantity: Int }
service Ship { emission fn dispatch(id: Str) }

component Fulfiller requires feed: Stream[OrderCreated], ship: Ship {
  on OrderCreated as e { emit ship.dispatch(e.order_id) }
}
"""


def _coeffect_component(src: str = _COEFFECT) -> dict:
    return compile_source(src, "s.rvl")["components"][0]


def test_a_required_stream_is_an_ordinary_requirement_on_the_wiring_graph():
    """A coeffect is a DECLARED REQUIREMENT, so a required stream rides the same
    wiring the item's other requirements ride: it lands in `requires` under its
    declared type, which is what puts it in the emitted inject set and leaves an
    unmet one PENDING under R2 (§4.3, the coeffect-layer half)."""
    comp = _coeffect_component()
    assert comp["requires"] == {"feed": "Stream[OrderCreated]", "ship": "Ship"}


def test_the_handler_desugars_to_the_bracket_plus_the_iteration():
    """§6: `on <Event> as <x> { … }` desugars to `every <x> in subscribe(<the
    Event stream>) { … }`. Two steps, and the first is an ORDINARY subscription
    bracket — same `let-effect` step, same `subscribe: true`, same default
    `error` policy, same `close` inverse — so the core guarantee (§0) rides the
    machinery Slice 1 proved rather than a second lowering."""
    body = _coeffect_component()["body"]
    assert [step["step"] for step in body] == ["let-effect", "stream-iter"]
    bracket, loop = body
    assert bracket["subscribe"] is True and bracket["policy"] == "error"
    assert bracket["acquire"] == {
        "kind": "subscribe",
        "stream": {"kind": "req", "name": "feed"},
        "policy": "error",
    }
    assert bracket["undo"] == {"kind": "call",
                               "target": {"kind": "name", "id": bracket["bind"]},
                               "method": "close", "args": []}
    assert loop["subject"] == {"kind": "name", "id": bracket["bind"]}
    assert loop["event"]["name"] == "OrderCreated"


def test_the_implicit_and_explicit_handlers_lower_to_the_same_steps():
    """The non-vacuity control for the desugaring: writing the bracket by hand
    and letting the handler resolve it produce the SAME two steps, modulo the
    subscription's name. If they ever diverged, the clause's presence would
    change the program rather than only its spelling."""
    explicit = compile_source("""
    event OrderCreated(key: order_id) { order_id: Str, quantity: Int }
    service Ship { emission fn dispatch(id: Str) }
    component Fulfiller requires feed: Stream[OrderCreated], ship: Ship {
      let sub = subscribe feed undo sub.close()
      on OrderCreated as e in sub { emit ship.dispatch(e.order_id) }
    }
    """, "s.rvl")["components"][0]["body"]
    implicit = _coeffect_component()["body"]

    def _rename(steps, bind):
        dumped = json.dumps(steps)
        return json.loads(dumped.replace(f'"{bind}"', '"<sub>"'))

    assert _rename(explicit, explicit[0]["bind"]) == \
        _rename(implicit, implicit[0]["bind"])


def test_the_synthesized_subscription_cannot_be_named():
    """The desugared bracket is ANONYMOUS: it is never entered in the component's
    locals, so there is no half-spelled form where the author reaches the handle
    the handler owns — no second `every` over it, no hand-written `close`, and no
    collision with a local of the same spelling."""
    bind = _coeffect_component()["body"][0]["bind"]
    msg = _refusal("""
    event OrderCreated(key: order_id) { order_id: Str, quantity: Int }
    service Ship { emission fn dispatch(id: Str) }
    component Fulfiller requires feed: Stream[OrderCreated], ship: Ship {
      on OrderCreated as e { emit ship.dispatch(e.order_id) }
      every o in %s { emit ship.dispatch("x") }
    }
    """ % bind)
    assert "needs a live subscription" in msg


def test_a_handler_with_no_required_stream_is_refused_by_name():
    """The failure direction. A coeffect is a declared requirement, so a handler
    whose required `Stream[T]` cannot be resolved REFUSES — it never binds
    nothing and never picks an arbitrary subscription."""
    msg = _refusal("""
    event OrderCreated(key: order_id) { order_id: Str, quantity: Int }
    service Ship { emission fn dispatch(id: Str) }
    component Fulfiller requires ship: Ship {
      on OrderCreated as e { emit ship.dispatch(e.order_id) }
    }
    """)
    assert "has no stream to resolve" in msg
    assert "requires no `Stream[...]`" in msg
    assert "requires <key>: Stream[OrderCreated]" in msg


def test_a_handler_whose_event_is_not_the_required_element_is_refused_by_name():
    """Resolution is BY TYPE — §6's "the event source is the provided
    `Stream[T]`" — so a component holding a stream of some OTHER element has
    nothing to resolve, and the refusal says which stream it looked at."""
    msg = _refusal("""
    event OrderCreated(key: order_id) { order_id: Str, quantity: Int }
    event Shipped(key: id) { id: Str }
    service Ship { emission fn dispatch(id: Str) }
    component Fulfiller requires feed: Stream[Shipped], ship: Ship {
      on OrderCreated as e { emit ship.dispatch(e.order_id) }
    }
    """)
    assert "has no required `Stream[OrderCreated]` to resolve" in msg
    assert "`feed: Stream[Shipped]`" in msg


def test_two_required_streams_of_one_event_are_ambiguous_and_refused():
    """The other failure direction, and the one a "pick the first" resolution
    would hide: two candidates is a program whose author has not said which
    stream the handler pulls."""
    msg = _refusal("""
    event OrderCreated(key: order_id) { order_id: Str, quantity: Int }
    service Ship { emission fn dispatch(id: Str) }
    component Fulfiller requires a: Stream[OrderCreated],
                                 b: Stream[OrderCreated], ship: Ship {
      on OrderCreated as e { emit ship.dispatch(e.order_id) }
    }
    """)
    assert "is ambiguous" in msg
    assert "`a`, `b`" in msg


def test_two_events_over_two_required_streams_each_resolve():
    """Resolution by element type is what makes several handlers in one
    component unambiguous: two events are two element types."""
    body = compile_source("""
    event OrderCreated(key: order_id) { order_id: Str, quantity: Int }
    event Shipped(key: id) { id: Str }
    service Ship { emission fn dispatch(id: Str) }
    component Fulfiller requires orders: Stream[OrderCreated],
                                 ships: Stream[Shipped], ship: Ship {
      on OrderCreated as e { emit ship.dispatch(e.order_id) }
      on Shipped as s { emit ship.dispatch(s.id) }
    }
    """, "s.rvl")["components"][0]["body"]
    assert [step["step"] for step in body] == \
        ["let-effect", "stream-iter", "let-effect", "stream-iter"]
    assert body[0]["acquire"]["stream"] == {"kind": "req", "name": "orders"}
    assert body[2]["acquire"]["stream"] == {"kind": "req", "name": "ships"}


def test_rule_3_1_holds_across_the_requirement_key():
    """Single-consumer is unchanged by where the stream came from: two
    subscriptions to one required stream are refused exactly as two
    subscriptions to one local source are."""
    msg = _refusal("""
    component C requires feed: Stream[Int] {
      let a = subscribe feed undo a.close()
      let b = subscribe feed undo b.close()
    }
    """)
    assert "required stream `feed` is already subscribed" in msg
    assert "rule 3.1" in msg


def test_an_explicit_subscription_and_a_handler_contend_for_one_requirement():
    """Same rule, reached the other way: the handler's implicit `subscribe` is a
    real subscription, so it contends with a hand-written one on the same key
    instead of quietly sharing it."""
    msg = _refusal("""
    event OrderCreated(key: order_id) { order_id: Str, quantity: Int }
    service Ship { emission fn dispatch(id: Str) }
    component Fulfiller requires feed: Stream[OrderCreated], ship: Ship {
      let sub = subscribe feed undo sub.close()
      on OrderCreated as e { emit ship.dispatch(e.order_id) }
    }
    """)
    assert "required stream `feed` is already subscribed" in msg


def test_the_explicit_form_keeps_every_qualifier_on_a_required_stream():
    """Dropping the clause loses no expressiveness because the clause stays: a
    handler that needs a policy, a buffer or a combinator chain writes the
    `subscribe` itself, on the same required stream."""
    body = compile_source("""
    event OrderCreated(key: order_id) { order_id: Str, quantity: Int }
    service Ship { emission fn dispatch(id: Str) }
    component Fulfiller requires feed: Stream[OrderCreated], ship: Ship {
      let sub = subscribe feed.take(3) policy drop_oldest buffer 2
        undo sub.close()
      on OrderCreated as e in sub { emit ship.dispatch(e.order_id) }
    }
    """, "s.rvl")["components"][0]["body"]
    acquire = body[0]["acquire"]
    assert acquire["stream"] == {"kind": "req", "name": "feed"}
    assert acquire["stages"] == [{"stage": "take", "count": 3}]
    assert acquire["policy"] == "drop_oldest" and acquire["buffer"] == 2


def test_a_required_stream_merges_with_a_local_source():
    """A required stream is a stream: the Slice 3 fan-in takes one pointwise,
    with rules 3.1 and 3.6 applied to each operand on its own terms."""
    acquire = compile_source("""
    component C requires feed: Stream[Int] {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe merge(feed, src) undo sub.close()
      await sub.next()
    }
    """, "s.rvl")["components"][0]["body"][1]["acquire"]
    assert acquire["stream"] == {
        "kind": "stream-merge",
        "sources": [{"kind": "req", "name": "feed"},
                    {"kind": "name", "id": "src"}],
    }


def test_a_required_stream_is_not_a_service():
    """The one confusion the surface invites. A required stream is declared the
    way a required service is, so a method call on it is refused by name rather
    than resolved against a service table the requirement was never in."""
    msg = _refusal("""
    service Ship { emission fn dispatch(id: Str) }
    component C requires feed: Stream[Int], ship: Ship {
      emit feed.dispatch("x")
    }
    """)
    assert "`feed.dispatch` — `feed` is a required `Stream[Int]`, not a service" \
        in msg
    assert "the only operation on it is `subscribe`" in msg


@pytest.mark.parametrize("body", [
    'emit ship.dispatch(feed)',
    'let sub = subscribe feed.map(x => feed) undo sub.close()',
])
def test_a_required_stream_is_read_only_in_a_subscribe_head(body):
    """A `Stream[T]` is a capability to ACQUIRE a subscription, not a value: a
    stream that could be bound, passed or stored would be a second handle on a
    single-consumer resource with no bracket behind it."""
    msg = _refusal("""
    service Ship { emission fn dispatch(id: Str) }
    component C requires feed: Stream[Int], ship: Ship {
      %s
    }
    """ % body)
    assert "required stream `feed` is read outside a `subscribe`" in msg


def test_a_requirement_carries_no_state_index():
    """The index is what `subscribe` PRODUCES (`Stream[T]` -> `Stream[T,
    Active]`), so a requirement names the unsubscribed capability."""
    msg = _refusal("component C requires feed: Stream[Int, Active] { }")
    assert "a required stream carries no state index" in msg


def test_a_requirement_is_not_optional():
    msg = _refusal("component C requires feed: Stream[Int]? { }")
    assert "a requirement is not optional" in msg


def test_a_component_cannot_provide_a_stream():
    """Provider-side stream provision is not this slice, and is refused rather
    than admitted as a capability the wiring graph reports as satisfiable and no
    program can satisfy."""
    msg = _refusal("component C provides feed: Stream[Int] { }")
    assert "a component cannot provide a stream" in msg


def test_a_service_named_stream_is_untouched():
    """`Stream` is not a keyword: a service that happens to be called `Stream`
    still resolves as a service, because only `Stream[` — the type application —
    enters the coeffect path."""
    comp = compile_source("""
    service Stream { emission fn go(v: Str) }
    component C requires s: Stream { emit s.go("x") }
    """, "s.rvl")["components"][0]
    assert comp["requires"] == {"s": "Stream"}


def test_python_emits_the_required_stream_as_an_injected_key():
    """The reference tier resolves the requirement through the SAME injection
    that resolves a required service: the subscription opens on the committed
    view of the key, and the key is in the inject set, so an unmet requirement
    pends rather than subscribing to nothing."""
    code = _tier_emit("python").emit(compile_source(_COEFFECT, "s.rvl"))
    assert "Stream.subscribe(_revl_ctx.feed, 'error', _revl_ctx)" in code
    assert "'inject': ['feed', 'ship']," in code


@pytest.mark.parametrize("tier", ["go", "rust", "java", "typescript", "wasm"])
def test_the_other_tiers_refuse_a_required_stream_by_name(tier):
    """A stream form that compiled on a tier and behaved differently from the py
    reference would be worse than a refusal. These tiers resolve a requirement
    against a SERVICE; before this refusal they rendered the requirement's type
    as the literal text `Stream[T]`, which no compiler on those tiers has."""
    emit = _tier_emit(tier)
    with pytest.raises(emit.EmitError) as excinfo:
        emit.emit(compile_source(_COEFFECT, "s.rvl"))
    msg = str(excinfo.value)
    assert "requires `feed: Stream[OrderCreated]`" in msg
    assert "not lowered on the" in msg
    assert "--backend py" in msg


@pytest.mark.parametrize("tier", ["go", "rust", "java", "typescript", "wasm"])
def test_a_declared_but_unsubscribed_required_stream_is_refused_too(tier):
    """The refusal is at the DECLARATION, not at the subscription: a requirement
    nothing subscribes to still shapes the emitted requirement resolution, so
    admitting it would emit an unresolvable key."""
    emit = _tier_emit(tier)
    with pytest.raises(emit.EmitError) as excinfo:
        emit.emit(compile_source("""
        service Ship { emission fn dispatch(id: Str) }
        component C requires feed: Stream[Int], ship: Ship {
          emit ship.dispatch("x")
        }
        """, "s.rvl"))
    assert "requires `feed: Stream[Int]`" in str(excinfo.value)


def test_a_stream_free_program_is_byte_identical():
    """Byte-identity (§10.9): the coeffect adds nothing to a program that does
    not declare a required stream — requirement lowering is untouched."""
    comp = compile_source("""
    service Ship { emission fn dispatch(id: Str) }
    component C requires ship: Ship { emit ship.dispatch("x") }
    """, "s.rvl")["components"][0]
    assert comp["requires"] == {"ship": "Ship"}
    assert [step["step"] for step in comp["body"]] == ["emit"]


def test_a_multifile_build_carries_a_required_stream(tmp_path):
    """The CLI path merges each module into a synthetic program before lowering;
    pin that the merge carries the requirement's stream type, since the merge is
    where the event table was lost once already."""
    source = tmp_path / "s.rvl"
    source.write_text(_COEFFECT)
    ir = compile_files([str(source)])
    comp = ir["components"][0]
    assert comp["requires"]["feed"] == "Stream[OrderCreated]"
    assert [step["step"] for step in comp["body"]] == ["let-effect", "stream-iter"]


# ---------------------------------------------------------------------------
# §4.5 meets §6c: the replay declaration a stream REQUIREMENT carries, and the
# cross-restart program the two halves compose into
# ---------------------------------------------------------------------------

_CROSS_RESTART = """
event OrderCreated(key: order_id) { order_id: Str, quantity: Int }
service Ship { emission fn dispatch(id: Str) }

component Fulfiller requires feed: Stream[OrderCreated] replay(from: "fulfiller-cursor"),
                             ship: Ship {
  let sub = subscribe feed replay(from: "fulfiller-cursor") undo sub.close()
  on OrderCreated as e in sub { emit ship.dispatch(e.order_id) }
}
"""


def _coeffect_replay(requirement: str, request: str) -> str:
    return """
    event OrderCreated(key: order_id) { order_id: Str, quantity: Int }
    service Ship { emission fn dispatch(id: Str) }
    component C requires feed: Stream[OrderCreated]%s, ship: Ship {
      let sub = subscribe feed%s undo sub.close()
      on OrderCreated as e in sub { emit ship.dispatch(e.order_id) }
    }
    """ % (requirement, request)


def test_the_cross_restart_replay_program_compiles():
    """§4.9's case, as ONE program. It needs both halves and neither alone: the
    coeffect puts the provider in the wiring so a second activation can be wired
    to the stream the first was reading, and the durable cursor is what lets that
    activation resume where the first stopped rather than at the live edge."""
    comp = compile_source(_CROSS_RESTART, "s.rvl")["components"][0]
    assert comp["requires"]["feed"] == "Stream[OrderCreated]"
    assert comp["stream_replay"] == {"feed": {"cursor": "fulfiller-cursor"}}
    bracket = comp["body"][0]
    assert bracket["acquire"]["stream"] == {"kind": "req", "name": "feed"}
    assert bracket["acquire"]["replay"] == {"cursor": "fulfiller-cursor"}
    assert bracket["replay"] == {"cursor": "fulfiller-cursor"}


def test_a_request_against_an_undeclaring_requirement_is_refused():
    """§4.5's rule does not bend for a coeffect: a request is admitted only
    against a DECLARATION, and only where the declaration lives moves."""
    msg = _refusal(_coeffect_replay("", " replay(3)"))
    assert "requires a provider that declares it" in msg
    assert "requires feed: Stream[T] replay(<n>)" in msg


def test_a_last_n_backlog_may_be_declared_on_a_requirement_too():
    comp = compile_source(_coeffect_replay(" replay(8)", " replay(3)"),
                          "s.rvl")["components"][0]
    assert comp["stream_replay"] == {"feed": {"count": 8}}
    assert comp["body"][0]["acquire"]["replay"] == {"count": 3}


@pytest.mark.parametrize("requirement,request_,expected", [
    (' replay(from: "cur")', ' replay(from: "other")',
     "declares the durable cursor `cur`, not `other`"),
    (' replay(2)', ' replay(9)', "asks for more than required stream `feed`"),
    (' replay(from: "cur")', ' replay(3)',
     "declares a durable cursor, so a subscription resumes FROM it"),
    (' replay(4)', ' replay(from: "cur")',
     "declares a last-n backlog, not a durable cursor"),
])
def test_the_four_comparisons_name_the_requirement_not_a_missing_source(
        requirement, request_, expected):
    """Every §4.5 comparison runs unchanged against a requirement's declaration,
    and each names the requirement. Before this they resolved the declaration
    from a local bind, so a required stream reached them with `src_name` of
    `None` and the whole family collapsed into the undeclared refusal."""
    msg = _refusal(_coeffect_replay(requirement, request_))
    assert expected in msg


def test_replay_on_a_non_stream_requirement_is_refused_by_name():
    msg = _refusal("""
    service Ship { emission fn dispatch(id: Str) }
    component C requires ship: Ship replay(3) { emit ship.dispatch("x") }
    """)
    assert "only a required `Stream[T]` declares a replay backlog" in msg


def test_a_requirements_cursor_must_be_a_literal_name():
    """The cursor IS the descriptor a fresh process re-issues from, so it has to
    be writable into the WAL as it stands — the same rule the provider-side
    declaration follows, through the same routine."""
    msg = _refusal("""
    service Ship { emission fn dispatch(id: Str) }
    component C requires feed: Stream[Int] replay(from: 3), ship: Ship {
      emit ship.dispatch("x")
    }
    """)
    assert "a durable replay cursor must be a literal name" in msg


def test_a_declaring_requirement_nothing_asks_for_is_admitted():
    """A declaration the body never consumes is fine — exactly as a declaring
    local source that is subscribed without a `replay(…)` is. The declaration is
    an obligation on the WIRING, not on the body."""
    comp = compile_source("""
    event OrderCreated(key: order_id) { order_id: Str, quantity: Int }
    service Ship { emission fn dispatch(id: Str) }
    component C requires feed: Stream[OrderCreated] replay(from: "cur"),
                         ship: Ship {
      on OrderCreated as e { emit ship.dispatch(e.order_id) }
    }
    """, "s.rvl")["components"][0]
    assert comp["stream_replay"] == {"feed": {"cursor": "cur"}}
    assert "replay" not in comp["body"][0]["acquire"]


def test_a_local_source_declaration_is_unaffected_by_a_sibling_requirement():
    """The two declaration sites do not leak into each other: a component may
    hold both, and each `subscribe` is admitted against the one its own stream
    carries."""
    comp = compile_source("""
    event OrderCreated(key: order_id) { order_id: Str, quantity: Int }
    service Ship { emission fn dispatch(id: Str) }
    component C requires feed: Stream[OrderCreated] replay(3), ship: Ship {
      let src = effect Stream.source() replay(5) undo src.close()
      let sub = subscribe src replay(5) undo sub.close()
      on OrderCreated as e in sub { emit ship.dispatch(e.order_id) }
    }
    """, "s.rvl")["components"][0]
    assert comp["stream_replay"] == {"feed": {"count": 3}}
    assert comp["body"][1]["acquire"]["replay"] == {"count": 5}


def test_a_replay_free_component_carries_no_stream_replay_key():
    """Additive (§10.9): the key appears only when a requirement declares."""
    comp = compile_source(_COEFFECT, "s.rvl")["components"][0]
    assert "stream_replay" not in comp


@pytest.mark.parametrize("tier", ["go", "rust", "java", "typescript", "wasm"])
def test_the_other_tiers_refuse_the_cross_restart_program_at_the_requirement(tier):
    """Both halves are py-only, and the requirement is the earlier of the two —
    the tier cannot resolve the key at all, so that is the honest refusal rather
    than one about a backlog it would never reach."""
    emit = _tier_emit(tier)
    with pytest.raises(emit.EmitError) as excinfo:
        emit.emit(compile_source(_CROSS_RESTART, "s.rvl"))
    msg = str(excinfo.value)
    assert "requires `feed: Stream[OrderCreated]`" in msg
    assert "--backend py" in msg


def test_python_emits_the_resume_against_the_injected_stream():
    code = _tier_emit("python").emit(compile_source(_CROSS_RESTART, "s.rvl"))
    assert ("Stream.subscribe(_revl_ctx.feed, 'error', _revl_ctx, "
            "replay={'cursor': 'fulfiller-cursor'})") in code
    assert "'inject': ['feed', 'ship']," in code


def test_a_local_source_replay_program_is_unchanged_by_the_coeffect_path():
    """The non-vacuity control for the `_admit_replay` refactor, and it passes on
    main too. §4.5's existing shape — the declaration on the acquisition, the
    request on the `subscribe` — lowers to exactly what it lowered to before the
    requirement became a second place a declaration can live."""
    comp = compile_source("""
    event OrderCreated(key: order_id) { order_id: Str, quantity: Int }
    service Ship { emission fn dispatch(id: Str) }
    component C requires ship: Ship {
      let src = effect Stream.source() replay(from: "orders") undo src.close()
      let sub = subscribe src replay(from: "orders") undo sub.close()
      on OrderCreated as e in sub { emit ship.dispatch(e.order_id) }
    }
    """, "s.rvl")["components"][0]
    assert "stream_replay" not in comp
    assert comp["body"][0]["replay"] == {"cursor": "orders"}
    assert comp["body"][1]["acquire"] == {
        "kind": "subscribe",
        "stream": {"kind": "name", "id": "src"},
        "policy": "error",
        "replay": {"cursor": "orders"},
    }


def test_the_undeclared_refusal_still_names_the_source_for_a_local_stream():
    """The refusal family split by where the declaration lives, and the LOCAL
    arm must be untouched: its hint still points at the acquisition, not at a
    requirement the program does not have. Passes on main too."""
    msg = _refusal("""
    service Ship { emission fn dispatch(id: Str) }
    component C requires ship: Ship {
      let src = effect Stream.source() undo src.close()
      let sub = subscribe src replay(3) undo sub.close()
      await sub.next()
    }
    """)
    assert "requires a provider that declares it" in msg
    assert "let src = effect Stream.source() replay(<n>) undo src.close()" in msg


# ---------------------------------------------------------------------------
# The item-130 EXIT, as a table (roadmap item 130, issue #81)
#
# "For each of the nine named stream surfaces on each of the six tiers, the
# answer is one of exactly two things and a test proves which: the tier LOWERS
# it, or the tier REFUSES IT BY NAME. Nothing is silently dropped."
#
# Everything the surfaces below assert is already asserted one cell at a time
# somewhere above. What was missing is the CLOSURE: nothing said that those
# cells are all of them, so a tenth surface, or a seventh tier, or a surface
# that quietly stopped being refused, cost nothing. The table is the closure --
# it enumerates the nine surfaces and the six tiers and requires every one of
# the 54 cells to be one of the two admitted answers, with the refusals pinned
# to the word they refuse BY. A refusal that stops naming its surface reds here
# even though it is still a refusal, because "refuses by name" is the half of
# the exit that a bare `EmitError` does not deliver.
# ---------------------------------------------------------------------------

_POLICY = """
component C {
  let a = effect Stream.source() undo a.close()
  let sub = subscribe a policy drop_oldest buffer 4 undo sub.close()
  await sub.next()
}
"""

_REPLAY_DECLARED = """
component C {
  let src = effect Stream.source() replay(4) undo src.close()
  let sub = subscribe src replay(2) undo sub.close()
  await sub.next()
}
"""

#: surface -> the program that carries it. The nine of design §1 plus the two
#: durability surfaces §4.5/§6c, which the exit holds to the same rule.
_EXIT_SURFACES = {
    "subscription bracket": _CONSUMER,
    "map/filter/take chain": _CHAIN_HEAD,
    "merge fan-in": _FANIN,
    "backpressure policy": _POLICY,
    "drain window": None,          # built from _DRAIN_HEAD below
    "every..in iteration": _ITER,
    "on..as typed event": _EVENT,
    "replay declaration": _REPLAY_DECLARED,
    "required Stream[T] coeffect": _COEFFECT,
}

_EXIT_TIERS = ("python", "typescript", "go", "rust", "java", "wasm")

#: (surface, tier) -> the text the tier's refusal must carry. Every pair absent
#: from this map must LOWER. Measured 2026-09-20; each entry is asserted one at
#: a time by a test above, and this map is what makes the set of them closed.
_EXIT_REFUSALS = {
    # §4.6: wasm has no async, so it refuses the whole surface at the provider.
    **{("wasm", surface): "streams live on the tiers that lower the "
                          "subscription protocol"
       for surface in _EXIT_SURFACES if surface != "required Stream[T] coeffect"},
    # §8: rust's clock is thread-local, java has no `advance` lowering.
    ("rust", "drain window"): "a `drain` window is not lowered",
    ("java", "drain window"): "a `drain` window is not lowered",
    # §4.5/§4.9: the durability claim whose recovery surface is the WAL's.
    **{(tier, "replay declaration"): "a stream `replay(…)` is not lowered"
       for tier in ("typescript", "go", "rust", "java")},
    # §6b: a requirement resolves against a SERVICE on every non-reference tier.
    **{(tier, "required Stream[T] coeffect"):
       "a required `Stream[T]` coeffect is not lowered"
       for tier in ("typescript", "go", "rust", "java", "wasm")},
}


def _exit_source(surface: str) -> str:
    return _drain_program() if surface == "drain window" else _EXIT_SURFACES[surface]


@pytest.mark.parametrize("surface", sorted(_EXIT_SURFACES))
@pytest.mark.parametrize("tier", _EXIT_TIERS)
def test_every_stream_surface_lowers_or_refuses_by_name_on_every_tier(tier, surface):
    """One cell of the item-130 exit table: LOWER, or REFUSE naming the surface.

    A tier that cannot carry a stream surface does not fail this exit by
    refusing. It fails by being SILENT -- by answering a program it does not
    carry with a module that looks complete and never subscribes. So the
    assertion has two halves: the cell is one of the two admitted answers, and a
    refusing cell names what it refused.
    """
    emit = _tier_emit(tier)
    ir = compile_source(_exit_source(surface), "s.rvl")
    want = _EXIT_REFUSALS.get((tier, surface))
    if want is None:
        code = emit.emit(ir)
        assert "Stream" in code, (
            f"{tier} is recorded as LOWERING `{surface}` and emitted no stream"
        )
        return
    with pytest.raises(emit.EmitError) as excinfo:
        emit.emit(ir)
    assert want in str(excinfo.value), (
        f"{tier} still refuses `{surface}`, but no longer by name: a refusal "
        f"that does not say what it refused is the half of the exit a bare "
        f"EmitError does not deliver\n{excinfo.value}"
    )


def test_the_exit_table_covers_the_whole_named_surface_and_nothing_else():
    """The table's own closure. A surface added to design §1 without a row here
    would leave the exit asserting nine of ten cells per tier and calling it
    complete, which is how this item carried two wrong tier lists before."""
    assert len(_EXIT_SURFACES) == 9
    assert len(_EXIT_TIERS) == 6
    unknown = {cell for cell in _EXIT_REFUSALS
               if cell[0] not in _EXIT_TIERS or cell[1] not in _EXIT_SURFACES}
    assert not unknown, f"refusal recorded for a cell outside the table: {unknown}"
    assert not [s for s in _EXIT_SURFACES
                if ("python", s) in _EXIT_REFUSALS], (
        "py is the reference tier and lowers all nine (design §4.6)"
    )
