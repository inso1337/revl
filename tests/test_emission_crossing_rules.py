"""The rules an emission crossing is held to in a component body.

Three rules, one owner, because they read the same functions in `lower.py`
(`_is_emission_call`, `_lower_emit_step`, `_lower_emit_approval`):

1. **The marker** (issue #1437). Every emission crossing carries `emit` at its
   call site, whatever carries it: a required service, a spawn handle, a host
   `emission` extern, or a module `fn` that reaches one. The extern carrier was
   held to it only inside another `emit`'s argument list.
2. **The declaration-owned approval floor** (item 246) on a capability-SCOPED
   extern, and on every spelling of a marked crossing. The floor was keyed by
   extern NAME while the crossing resolves to its capability TOKEN, so a scoped
   `requires approval` extern crossed with no edge. The `emit` value form and a
   marked call to a `fn` reaching the extern were never checked at all.
3. **The nested crossing.** `emit log_line(charge(1))` with `charge` requiring
   approval. Closed by the marker rule inside `emit` arguments (issue #1427);
   pinned here so the approval consequence has a test of its own.

docs/design/1437-emit-marks-every-crossing.md records the decisions.
"""

from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError

ROOT = Path(__file__).resolve().parents[1]


def _refusal(src: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "t.rvl")
    return excinfo.value


def _marker(name: str) -> str:
    return f"call to emission `{name}` must be marked `emit` (G4)"


def _approval(token: str) -> str:
    return (f"crossing capability `{token}` requires approval, but this `emit` "
            "carries no covering `with` edge")


# ------------------------------------------------------------ 1. the marker

_CHARGE = "extern emission fn charge(n: Int) -> Int = @py { return 1 }\n"
_TILL = "service Till { emission fn ring(n: Int) -> Int }\n"


def _method(body: str, prelude: str = "") -> str:
    return (_CHARGE + prelude + _TILL
            + "component R provides till: Till {\n"
            + "  provide till { " + body + " }\n}\n")


@pytest.mark.parametrize("body", [
    "fn ring(n) { let r = charge(n) return r }",
    "fn ring(n) { return charge(n) }",
    "fn ring(n) = charge(n)",
    "fn ring(n) { let r = 1 + charge(n) return r }",
    "fn ring(n) { if (n > 0) { let r = charge(n) } return 0 }",
], ids=["binding", "return", "expression-body", "operand", "if-arm"])
def test_an_unmarked_host_extern_call_is_refused_in_a_provide_method(body):
    err = _refusal(_method(body))
    assert err.code == "G4"
    assert err.message == _marker("charge")


@pytest.mark.parametrize("body", [
    "fn ring(n) { let r = emit charge(n) return r }",
    "fn ring(n) { return emit charge(n) }",
    "fn ring(n) = emit charge(n)",
    "fn ring(n) { let r = 1 + emit charge(n) return r }",
], ids=["binding", "return", "expression-body", "operand"])
def test_the_marked_spelling_is_admitted(body):
    assert compile_source(_method(body), "t.rvl")


def test_a_helper_reaching_an_emission_is_a_crossing_too():
    helper = "fn bill(n: Int) -> Int { return charge(n) }\n"
    err = _refusal(_method("fn ring(n) = bill(n)", prelude=helper))
    assert err.message == _marker("bill")
    assert compile_source(_method("fn ring(n) = emit bill(n)", prelude=helper),
                          "t.rvl")


def test_an_unmarked_acquisition_in_an_activation_body_is_refused():
    src = (_CHARGE
           + "extern pure fn close(n: Int) -> Int = @py { return 1 }\n"
           + "service S { fn go(n: Int) -> Int }\n"
           + "component C provides s: S {\n"
           + "  effect charge(1) undo close(1)\n"
           + "  provide s { fn go(n) = n }\n}\n")
    assert _refusal(src).message == _marker("charge")


def test_the_marker_refusal_precedes_the_provider_upper_bound():
    """The decided order: the marker refusal is raised during lowering, so it
    comes before the upper bound, as the `req` carrier's already did. Marked,
    the same plain-declared method still meets the upper bound."""
    plain = ("service Till { fn ring(n: Int) -> Int }\n"
             "component R provides till: Till {\n")
    unmarked = _CHARGE + plain + "  provide till { fn ring(n) = charge(n) }\n}\n"
    marked = _CHARGE + plain + "  provide till { fn ring(n) = emit charge(n) }\n}\n"
    assert _refusal(unmarked).message == _marker("charge")
    assert _refusal(marked).message == (
        "`Till.ring` is declared plain, but this implementation reaches `charge()`")


def test_a_teardown_slot_keeps_its_bare_emission_exception():
    src = ("extern emission fn notify(n: Int) -> Int = @py { return 1 }\n"
           + _CHARGE
           + "service S { fn go(n: Int) -> Int }\n"
           + "component C provides s: S {\n"
           + "  emit notify(1) compensate charge(2)\n"
           + "  provide s { fn go(n) = n }\n}\n")
    assert compile_source(src, "t.rvl")


def test_a_witnessed_extern_is_marked_by_effect():
    """`effect` is a witnessed extern's marker (docs/design/243). Holding it to
    `emit` refused every correct witnessed program in the corpus."""
    path = ROOT / "backends" / "go" / "scenarios" / "provide_method_witnessed.rvl"
    assert compile_source(path.read_text(encoding="utf-8"), str(path))


# --------------------------------------------------- 2. the approval floor

_SCOPED = ("extern emission[production.payment] fn charge(amount: Int) "
           "requires approval = @py { return }\n")
_UNSCOPED = "extern emission fn charge(amount: Int) requires approval = @py { return }\n"
_OPS = "service Ops { fn ping() -> Int }\n"


def _activation(extern: str, body: str, extra: str = "") -> str:
    return (extern + extra + _OPS
            + "component Biller provides ops: Ops {\n"
            + body
            + "  provide ops { fn ping() = 1 }\n}\n")


def test_the_unscoped_control_is_refused():
    """The case that always worked, pinned so the fix cannot regress it."""
    err = _refusal(_activation(_UNSCOPED, "  emit charge(1)\n"))
    assert err.code == "G4"
    assert err.message == _approval("charge")


def test_a_scoped_extern_requiring_approval_is_refused_without_an_edge():
    err = _refusal(_activation(_SCOPED, "  emit charge(1)\n"))
    assert err.code == "G4"
    assert err.message == _approval("production.payment")


def test_a_scoped_extern_is_admitted_with_a_covering_edge():
    body = ('  let a = await approval[production.payment] { reason: "pay" }\n'
            "  emit charge(1) with a\n")
    assert compile_source(_activation(_SCOPED, body), "t.rvl")


def test_a_scoped_extern_is_refused_with_an_edge_for_another_scope():
    body = ('  let a = await approval[staging.payment] { reason: "pay" }\n'
            "  emit charge(1) with a\n")
    assert _refusal(_activation(_SCOPED, body)).message == _approval(
        "production.payment")


def test_the_requirement_is_the_capabilitys_not_the_externs():
    """Keyed by token, as a policy `capability C requires approval` rule is: a
    sibling extern sharing the scope crosses the same capability. The
    unscoped spelling already behaved this way, because there the name IS the
    token."""
    sibling = ("extern emission[production.payment] fn refund(amount: Int) "
               "= @py { return }\n")
    err = _refusal(_activation(_SCOPED, "  emit refund(1)\n", extra=sibling))
    assert err.message == _approval("production.payment")


_RETURNING = ("extern emission fn charge(amount: Int) -> Int requires approval "
              "= @py { return 1 }\n")
_SCOPED_RETURNING = ("extern emission[production.payment] fn charge(amount: Int) "
                     "-> Int requires approval = @py { return 1 }\n")


def _provider(extern: str, body: str, activation: str = "") -> str:
    return (extern
            + "service Ops { emission fn ping() -> Int }\n"
            + "component Biller provides ops: Ops {\n"
            + activation
            + "  provide ops { " + body + " }\n}\n")


@pytest.mark.parametrize("body", [
    "fn ping() { let r = emit charge(1) return r }",
    "fn ping() { return emit charge(1) }",
    "fn ping() = emit charge(1)",
], ids=["binding", "return", "expression-body"])
@pytest.mark.parametrize("extern,token", [
    (_RETURNING, "charge"), (_SCOPED_RETURNING, "production.payment")],
    ids=["unscoped", "scoped"])
def test_the_value_form_in_a_provide_method_meets_the_floor(body, extern, token):
    """The value form has no `with` clause, so it can never carry the edge.
    Before, only the `emit` STEP was checked, and this crossed with none."""
    err = _refusal(_provider(extern, body))
    assert err.code == "G4"
    assert err.message == _approval(token)
    assert "has no `with` clause" in err.hint


def test_the_step_form_in_a_provide_method_meets_the_floor():
    err = _refusal(_provider(_SCOPED_RETURNING, "fn ping() { emit charge(1) return 1 }"))
    assert err.message == _approval("production.payment")


def test_the_step_form_in_a_provide_method_is_admitted_with_an_edge():
    """A provide method cannot mint an approval, but it can thread one minted
    in the activation body."""
    mint = '  let a = await approval[production.payment] { reason: "pay" }\n'
    assert compile_source(
        _provider(_SCOPED_RETURNING, "fn ping() { emit charge(1) with a return 1 }",
                  activation=mint), "t.rvl")


def test_a_marked_helper_crosses_what_it_reaches():
    helper = "fn bill(n: Int) -> Int { return charge(n) }\n"
    err = _refusal(_activation(_RETURNING, "  emit bill(1)\n", extra=helper))
    assert err.message == _approval("charge")
    scoped = _refusal(_activation(_SCOPED_RETURNING, "  emit bill(1)\n", extra=helper))
    assert scoped.message == _approval("production.payment")


def test_a_program_that_declares_no_approval_is_unchanged():
    """The floor is inert without a `requires approval` clause: the same
    helper and value-form spellings compile."""
    plain = "extern emission fn charge(amount: Int) -> Int = @py { return 1 }\n"
    helper = "fn bill(n: Int) -> Int { return charge(n) }\n"
    assert compile_source(_activation(plain, "  emit bill(1)\n", extra=helper), "t.rvl")
    assert compile_source(_provider(plain, "fn ping() = emit charge(1)"), "t.rvl")


# ------------------------------------------------- 3. the nested crossing

_NESTED_EXTERNS = (
    "extern emission fn charge(sink: Str, msg: Str) -> Int requires approval "
    "= @py { return 7 }\n"
    "extern emission fn log_line(x: Int) -> Int = @py { return x }\n")


def test_a_nested_approval_required_crossing_is_refused_in_an_activation_body():
    src = (_NESTED_EXTERNS + _OPS
           + "component Biller provides ops: Ops {\n"
           + '  emit log_line(charge("s", "m"))\n'
           + "  provide ops { fn ping() = 1 }\n}\n")
    assert _refusal(src).message == _marker("charge")


def test_a_nested_approval_required_crossing_is_refused_in_a_provide_method():
    src = (_NESTED_EXTERNS
           + "service Ops { emission fn ping() -> Int }\n"
           + "component Biller provides ops: Ops {\n"
           + '  provide ops { fn ping() { let r = emit log_line(charge("s", "m")) '
             "return r } }\n}\n")
    assert _refusal(src).message == _marker("charge")


def test_the_hoisted_spelling_meets_the_floor():
    """Hoisting the inner crossing into its own step is the fix the refusal
    names, and there the approval floor applies to it."""
    src = (_NESTED_EXTERNS + _OPS
           + "component Biller provides ops: Ops {\n"
           + '  emit charge("s", "m")\n'
           + "  provide ops { fn ping() = 1 }\n}\n")
    assert _refusal(src).message == _approval("charge")


# ------------------------------------------------------- the corpus twins

@pytest.mark.parametrize("name,message", [
    ("g4_unmarked_host_emission", _marker("charge")),
    ("g4_unmarked_host_emission_helper", _marker("bill")),
    ("g4_unmarked_host_emission_acquire", _marker("open_line")),
    ("g4_approval_scoped_extern", _approval("production.payment")),
    ("g4_approval_value_form_method", _approval("charge")),
    ("g4_approval_helper_reach", _approval("charge")),
    ("g4_nested_approval_emission", _marker("charge")),
])
def test_the_rejection_documents_state_their_verdict(name, message):
    path = ROOT / "examples" / "rejections" / f"{name}.rvl"
    text = path.read_text(encoding="utf-8")
    with pytest.raises(RevlError) as excinfo:
        compile_source(text, str(path))
    assert excinfo.value.code == "G4"
    assert excinfo.value.message == message
    # the header states the same sentence, wrapped as the header wraps it
    header = " ".join(line.lstrip("/ ").strip() for line in text.splitlines()
                      if line.startswith("//"))
    assert message in header
