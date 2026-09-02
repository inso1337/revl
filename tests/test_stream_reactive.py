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


def test_go_and_rust_lower_the_blocking_tier(tmp_path):
    """Slice 3's two implemented blocking tiers (§4.6). Both erase the async
    color: the fan-in opens inside the subscription's acquisition, and the
    bracket inverse is the subscription's `close`, which trips the cancel
    signal."""
    ir = compile_source(_FANIN, "s.rvl")
    go = _tier_emit("go").emit(ir)
    assert 'StreamSubscribe(StreamMerge(a, b), "error", 0)' in go
    assert "return func() error { sub.Close(); return nil }" in go
    rust = _tier_emit("rust").emit(ir)
    assert 'Stream::subscribe(&Stream::merge(&a, &b), "error", 0usize)' in rust
    assert "sub_undo.close(); Ok(())" in rust


@pytest.mark.parametrize("tier", ["java", "typescript"])
def test_unimplemented_tiers_refuse_honestly(tier):
    """A tier Slice 3 did NOT lower must refuse the stream IR kind by name, not
    fall through to the generic `unsupported expression kind` — a half-wired
    tier that emits something whose bracket inverse was never proven reachable
    is worse than an honest refusal."""
    emit = _tier_emit(tier)
    with pytest.raises(emit.EmitError) as excinfo:
        emit.emit(compile_source(_FANIN, "s.rvl"))
    msg = str(excinfo.value)
    assert "unsupported" not in msg
    assert "suspends a fiber" in msg
    assert "py, go and rust" in msg


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


@pytest.mark.parametrize("tier", ["java", "typescript"])
def test_source_only_program_is_refused_not_silently_emitted(tier):
    emit = _tier_emit(tier)
    with pytest.raises(emit.EmitError) as excinfo:
        emit.emit(compile_source(_SOURCE_ONLY, "s.rvl"))
    msg = str(excinfo.value)
    assert "unsupported" not in msg
    assert "Stream.source" in msg
    assert "py, go and rust" in msg


@pytest.mark.parametrize("tier", ["go", "rust"])
def test_source_only_program_still_emits_on_the_lowered_tiers(tier):
    """The refusal is scoped to the tiers with no runtime: go and rust carry a
    real `Stream` (Slice 3) and must keep emitting one."""
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


@pytest.mark.parametrize("tier", ["go", "rust"])
@pytest.mark.parametrize(("head", "want"), [
    ("subscribe a.map(x => x) undo sub.close()", "combinator chain"),
    ("subscribe a policy drop_oldest undo sub.close()", "drop_oldest"),
    ("subscribe a policy block drain 5s undo sub.close()", "block"),
])
def test_blocking_tiers_refuse_the_slice_2_surface_they_do_not_lower(tier, head, want):
    """Slice 2's combinator chain and its three non-default backpressure
    policies run on the py reference tier only. A blocking tier that emitted a
    subscription while SILENTLY dropping the chain, the lossy policy or the
    drain window would run and quietly disagree with the reference — the worst
    outcome available. Both refuse by name instead."""
    emit = _tier_emit(tier)
    ir = compile_source(
        "component C {\n"
        "  let a = effect Stream.source() undo a.close()\n"
        f"  let sub = {head}\n"
        "  await sub.next()\n"
        "}\n", "s.rvl")
    with pytest.raises(emit.EmitError) as excinfo:
        emit.emit(ir)
    msg = str(excinfo.value)
    assert want in msg
    assert "not lowered" in msg and "backend py" in msg


@pytest.mark.parametrize("tier", ["go", "rust"])
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


@pytest.mark.parametrize("tier", ["go", "rust"])
def test_the_blocking_tiers_refuse_the_iteration_form_by_name(tier):
    """go/rust lower the Slice 1/3 protocol but not the iteration form. A
    refusal that named nothing — or worse, a subscription emitted with its body
    silently dropped — is the outcome the honest EmitError exists to prevent."""
    emit = _tier_emit(tier)
    with pytest.raises(emit.EmitError) as excinfo:
        emit.emit(compile_source(_ITER, "s.rvl"))
    msg = str(excinfo.value)
    assert "unsupported component step" not in msg
    assert "`every … in`" in msg and "backend py" in msg


@pytest.mark.parametrize("tier", ["java", "typescript", "wasm"])
def test_the_unlowered_tiers_still_refuse_the_iteration_program(tier):
    """These three refuse the whole stream surface, and the subscription the
    loop needs is refused before the loop is reached — so an iteration program
    gets the same honest refusal a Slice 1 one does."""
    emit = _tier_emit(tier)
    with pytest.raises(emit.EmitError) as excinfo:
        emit.emit(compile_source(_ITER, "s.rvl"))
    assert "suspends a fiber" in str(excinfo.value)
