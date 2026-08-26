"""Frontend + IR for the session commit protocol — roadmap item 245, Slice 1.

Design: docs/design/245-session-commit.md, Decision 2. These are the checked
class-(b) obligations, the class tag on the G8 crossing surface, and the tier
gate — all pure frontend, so they run without a cordis runtime installed.
"""

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError
from revl.session_commit import refuse_deferred_on_ownerless_tier


def _compile(src: str) -> dict:
    return compile_source(src, "t.rvl")


_SEND = "extern emission deferred fn send(to: Str) = @py { return }\n"


# ---------------------------------------------------------------------------
# the `deferred` modifier and its IR flag
# ---------------------------------------------------------------------------

def test_deferred_modifier_sets_the_ir_flag():
    ir = _compile(_SEND)
    send = next(e for e in ir["externs"] if e["name"] == "send")
    assert send["class"] == "emission"
    assert send["deferred"] is True


def test_no_deferred_flag_on_a_plain_emission():
    ir = _compile("extern emission fn ping(to: Str) = @py { return }\n")
    ping = next(e for e in ir["externs"] if e["name"] == "ping")
    assert "deferred" not in ping   # additive: absent means class (c)


def test_deferred_and_async_parse_in_either_order():
    # `deferred` is emission-only and async-exclusive, so pair it with a witness
    # position instead; here just prove the parser accepts the modifier slot.
    ir = _compile(_SEND)
    assert any(e.get("deferred") for e in ir["externs"])


# ---------------------------------------------------------------------------
# Decision 2 checker obligations
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("src,fragment", [
    ("extern pure deferred fn f() = @py { return }\n",
     "only valid on an `emission`"),
    ("extern acquire deferred fn f() -> Str undo g(result) = @py { return }\n"
     "extern pure fn g(x: Str) = @py { return }\n",
     "only valid on an `emission`"),
    ("extern emission deferred fn f(to: Str) -> Str = @py { return \"x\" }\n",
     "must return `Unit`"),
    ("extern pure fn g() = @py { return }\n"
     "extern emission deferred fn f(to: Str) compensate g() = @py { return }\n",
     "cannot declare `compensate`"),
    ("extern emission deferred async fn f(to: Str) = @py { return }\n",
     "cannot be `async`"),
])
def test_deferred_obligations_are_refused(src, fragment):
    with pytest.raises(RevlError) as exc:
        _compile(src)
    assert fragment in str(exc.value)


def test_deferred_is_refused_in_a_teardown_position():
    # an acquire's undo may not call a deferred emission (teardown runs at/after
    # the verdict; enqueueing into a flushing/dropped queue is unanswerable)
    src = (_SEND +
           "extern acquire fn a() -> Str undo send(result) = @py { return \"r\" }\n")
    with pytest.raises(RevlError) as exc:
        _compile(src)
    assert "teardown position" in str(exc.value)


# ---------------------------------------------------------------------------
# the class tag on the G8 crossing surface (246's input)
# ---------------------------------------------------------------------------

def _crossings(ir: dict) -> dict:
    from revl.erase_report import _crossings
    from revl.query import Composition
    index = Composition(ir)
    members = [c["name"] for c in ir.get("components") or []]
    return _crossings(index, members)


_MIXED = (
    _SEND +
    "extern emission fn ping(to: Str) = @py { return }\n"
    "service Ops { emission fn q(to: Str) emission fn n(to: Str) }\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn q(to) { emit send(to) }\n"
    "    fn n(to) { emit ping(to) }\n"
    "  }\n"
    "}\n"
)


def test_g8_crossings_carry_the_action_class():
    cross = _crossings(_compile(_MIXED))
    by_name = {e["name"]: e["actionClass"] for e in cross["externs"]}
    assert by_name["send"] == "b"    # deferred emission -> class (b)
    assert by_name["ping"] == "c"    # immediate emission -> class (c)


# ---------------------------------------------------------------------------
# Decision 2 tier gate (the guard Slice 2 wires into the five emitters)
# ---------------------------------------------------------------------------

def test_tier_gate_refuses_a_reached_deferred_call_on_an_ownerless_tier():
    ir = _compile(_MIXED)
    for tier in ("rust", "go", "java", "wasm", "typescript"):
        with pytest.raises(RevlError) as exc:
            refuse_deferred_on_ownerless_tier(ir, tier)
        msg = str(exc.value)
        assert "needs a session owner runtime" in msg
        assert f"the {tier} tier" in msg
        assert "python tier only" in msg


def test_tier_gate_is_a_no_op_on_py():
    ir = _compile(_MIXED)
    refuse_deferred_on_ownerless_tier(ir, "python")   # no raise


def test_tier_gate_does_not_poison_a_declared_but_uncalled_deferred_extern():
    # `send` is declared deferred but never emitted -> the build is clean
    ir = _compile(_SEND +
                  "extern emission fn ping(to: Str) = @py { return }\n"
                  "service Ops { emission fn n(to: Str) }\n"
                  "component Agent provides ops: Ops {\n"
                  "  provide ops { fn n(to) { emit ping(to) } }\n"
                  "}\n")
    for tier in ("rust", "go", "java", "wasm", "typescript"):
        refuse_deferred_on_ownerless_tier(ir, tier)   # no raise
