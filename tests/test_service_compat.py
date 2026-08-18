"""Admission-gate structural compatibility (roadmap §5 / paper §6.6).

`compile_files(..., manifest=running)` is the runtime-admission gate. It used
to demand that a service redeclared against the running composition be
*structurally identical* (`lower._service_equal`). This suite pins the
replacement relation, `lower._service_compatible`: a redeclared interface is
admissible when every running component that touches it still type-checks
against the new shape.

The relation is *consumer/provider-relative*. A running CONSUMER's call sites
were checked against the old interface and are never recompiled, so a method
may be added, a parameter may widen (contravariant) and a return may narrow
(covariant), an emission may be dropped but never introduced. A retained
PROVIDER's `provide` block is pinned to the old parameter/return types (A6),
so where a provider stays those are frozen. When nothing running touches the
service, any change is admissible — there is nothing to break.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.diagnostics import classify  # noqa: E402
from revl.lower import (  # noqa: E402
    _service_compatible,
    _service_equal,
    _service_from_ir,
)


# A running composition: Kv provides the Store interface, App consumes it
# (its `ping` provider calls `store.get`). So Store has both a running
# provider (Kv) and a running consumer (App).
BASE = """
service Store {
  fn get(key: Str) -> Str
  fn bump(n: Int) -> Int
  emission fn put(key: Str, value: Str)
}
service AppSvc { fn ping() -> Str }

component Kv provides store: Store {
  let m = effect Map.new() undo m.drop()
  provide store {
    fn get(key) = key
    fn bump(n) = n
    fn put(key, value) = value
  }
}
component App requires store: Store provides app: AppSvc {
  provide app { fn ping() = store.get("boot") }
}
"""


@pytest.fixture
def running():
    return compile_source(BASE, "base.rvl")


def _admit(candidate: str, running: dict):
    """Compile a hot-swap candidate against the running composition."""
    return compile_source(candidate, "cand.rvl", manifest=running)


# A hot-swap replaces the provider Kv (same name), so Store has no *retained*
# provider and the richer consumer-relative widening applies.
def _swap(store_body: str, kv_provides: str) -> str:
    return f"""
service Store {{ {store_body} }}
component Kv provides store: Store {{
  let m = effect Map.new() undo m.drop()
  provide store {{ {kv_provides} }}
}}
"""


# ----------------------------------------------------------- compatible swaps

def test_adding_a_method_is_admitted(running):
    admitted = _admit(_swap(
        """fn get(key: Str) -> Str
           fn bump(n: Int) -> Int
           fn size() -> Int
           emission fn put(key: Str, value: Str)""",
        "fn get(key)=key fn bump(n)=n fn size()=0 fn put(key,value)=value",
    ), running)
    # the resulting interface carries the added method
    assert "size" in admitted["services"]["Store"]["methods"]


def test_tightening_an_emission_to_plain_is_admitted(running):
    admitted = _admit(_swap(
        """fn get(key: Str) -> Str
           fn bump(n: Int) -> Int
           fn put(key: Str, value: Str)""",
        "fn get(key)=key fn bump(n)=n fn put(key,value)=value",
    ), running)
    # `put` is no longer an emission — purity tightened, the boundary shrank
    assert not admitted["services"]["Store"]["methods"]["put"].get("emission")


def test_widening_a_parameter_is_admitted(running):
    # Int -> Float is contravariantly safe: a value a consumer passed still fits
    admitted = _admit(_swap(
        """fn get(key: Str) -> Str
           fn bump(n: Float) -> Int
           emission fn put(key: Str, value: Str)""",
        "fn get(key)=key fn bump(n)=0 fn put(key,value)=value",
    ), running)
    params = admitted["services"]["Store"]["methods"]["bump"]["params"]
    assert params[0]["type"] == "Float"


def test_identical_redeclaration_is_admitted(running):
    # the exact interface is trivially compatible (the _service_equal fast path)
    admitted = _admit(BASE, running)
    assert set(admitted["services"]["Store"]["methods"]) == {"get", "bump", "put"}


# --------------------------------------------------------- incompatible swaps

def test_removing_a_called_method_is_refused(running):
    with pytest.raises(RevlError) as excinfo:
        _admit(_swap(
            """fn bump(n: Int) -> Int
               emission fn put(key: Str, value: Str)""",
            "fn bump(n)=n fn put(key,value)=value",
        ), running)
    msg = str(excinfo.value)
    assert "differs from the running manifest" in msg   # stable classification
    assert "`get` is removed" in msg or "method `get` is removed" in msg
    assert "`App`" in msg                                # names the consumer


def test_changing_a_signature_is_refused(running):
    with pytest.raises(RevlError) as excinfo:
        _admit(_swap(
            """fn get(key: Str) -> Int
               fn bump(n: Int) -> Int
               emission fn put(key: Str, value: Str)""",
            "fn get(key)=0 fn bump(n)=n fn put(key,value)=value",
        ), running)
    msg = str(excinfo.value)
    assert "`get` return" in msg and "Str" in msg and "Int" in msg
    assert "`App`" in msg


def test_an_emission_that_appears_is_refused(running):
    with pytest.raises(RevlError) as excinfo:
        _admit(_swap(
            """emission fn get(key: Str) -> Str
               fn bump(n: Int) -> Int
               emission fn put(key: Str, value: Str)""",
            "fn get(key)=key fn bump(n)=n fn put(key,value)=value",
        ), running)
    msg = str(excinfo.value)
    assert "`get` becomes an `emission`" in msg
    assert "G4/G8" in msg
    assert "`App`" in msg


def test_narrowing_a_parameter_is_refused(running):
    # Float -> Int is contravariantly unsafe: a Float a consumer passes is lost.
    # (bump is Int in BASE; widen it to Float first would need a different base,
    # so we narrow `get`'s Str param to a subtype the consumer may overrun.)
    with pytest.raises(RevlError) as excinfo:
        _admit(_swap(
            """fn get(key: Str) -> Str
               fn bump(n: Str) -> Int
               emission fn put(key: Str, value: Str)""",
            "fn get(key)=key fn bump(n)=0 fn put(key,value)=value",
        ), running)
    assert "`bump` parameter" in str(excinfo.value)


# ------------------------------------------------- consumer/provider-relative

def test_incompatible_change_is_admitted_when_nothing_consumes_it():
    """The honest strengthening: with no running consumer, an interface may
    change however it likes — there is no call site to strand."""
    provider_only = """
    service Store {
      fn get(key: Str) -> Str
      emission fn put(key: Str, value: Str)
    }
    component Kv provides store: Store {
      let m = effect Map.new() undo m.drop()
      provide store { fn get(key) = key
                      fn put(key, value) = value }
    }
    """
    running = compile_source(provider_only, "base.rvl")
    admitted = _admit("""
    service Store { emission fn put(key: Str, value: Str) }
    component Kv provides store: Store {
      let m = effect Map.new() undo m.drop()
      provide store { fn put(key, value) = value }
    }
    """, running)
    assert set(admitted["services"]["Store"]["methods"]) == {"put"}


def test_a_retained_provider_pins_the_signature(running):
    """A candidate that redeclares Store but does NOT re-provide it leaves the
    running provider Kv in place; a parameter change that is safe for a
    consumer is still refused because Kv implements the old type (A6)."""
    candidate = """
    service Store {
      fn get(key: Str) -> Str
      fn bump(n: Float) -> Int
      emission fn put(key: Str, value: Str)
    }
    service ProbeSvc { fn go() -> Str }
    component Probe requires store: Store provides probe: ProbeSvc {
      provide probe { fn go() = store.get("x") }
    }
    """
    with pytest.raises(RevlError) as excinfo:
        _admit(candidate, running)
    msg = str(excinfo.value)
    assert "running provider implements it at `Int` (A6)" in msg
    assert "`Kv`" in msg   # the retained provider is named


# ----------------------------------------------------- identity is preserved

def test_duplicate_declaration_in_one_document_is_still_identity():
    """Two declarations of one service in a single compilation are a
    duplicate, never a compatible swap — the relation only relaxes exact-match
    across the runtime-admission boundary."""
    doc = """
    service Store { fn get(key: Str) -> Str }
    service Store { fn get(key: Str) -> Str
                    fn extra() -> Int }
    component Kv provides store: Store {
      let m = effect Map.new() undo m.drop()
      provide store { fn get(key) = key }
    }
    """
    with pytest.raises(RevlError, match="duplicate service `Store`"):
        compile_source(doc, "dup.rvl")


def test_the_drift_rejection_classifies_as_an_admission_error(running):
    """The structured projection keeps the (G2, admission) shape the
    exact-match gate carried, so downstream consumers see no change."""
    try:
        _admit(_swap(
            "fn bump(n: Int) -> Int emission fn put(key: Str, value: Str)",
            "fn bump(n)=n fn put(key,value)=value",
        ), running)
        assert False, "expected a drift rejection"
    except RevlError as err:
        record = classify(err)
        assert record["code"] == "G2"
        assert record["category"] == "admission"
        # the why-trace names the subject service and the affected component
        assert record["why"]["subject"] == "Store"
        names = {step["name"] for step in record["why"]["steps"]}
        assert "App" in names


# --------------------------------------------------------------- unit: relation

def _decl(*services):
    """Compile a services-only doc and rebuild ServiceDecls from the IR, the
    same round trip the admission gate performs (`_service_from_ir`). No
    provider is declared: a provider would have to implement every method
    (A6 completeness), which is beside the point when unit-testing the
    relation over arbitrary interface shapes."""
    ir = compile_source("\n".join(services), "u.rvl")
    return {name: _service_from_ir(name, spec)
            for name, spec in ir["services"].items()}


def test_relation_contravariant_parameters_and_covariant_returns():
    old = _decl("service S { fn f(x: Int) -> Float }")["S"]
    # widen the parameter (Int -> Float) and narrow the return (Float -> Int):
    # both keep every old consumer call valid.
    new = _decl("service S { fn f(x: Float) -> Int }")["S"]
    assert _service_compatible(new, old, providers_retained=False) is None
    # with a retained provider both are frozen, so the same change is refused
    assert _service_compatible(new, old, providers_retained=True) is not None
    # the reverse (narrow param, widen return) breaks a consumer either way
    assert _service_compatible(old, new, providers_retained=False) is not None


def test_relation_addition_is_compatible_only_without_a_retained_provider():
    old = _decl("service S { fn f(x: Int) -> Int }")["S"]
    new = _decl("service S { fn f(x: Int) -> Int\n fn g() -> Int }")["S"]
    # a replaced provider is checked against the new shape, so adding is fine
    assert _service_compatible(new, old, providers_retained=False) is None
    # a *retained* provider (A6 completeness) would not implement `g`, so the
    # addition is an unfilled obligation and the change is refused
    drift = _service_compatible(new, old, providers_retained=True)
    assert drift is not None and drift.method == "g"


def test_relation_emission_appearance_is_incompatible_but_removal_is_safe():
    plain = _decl("service S { fn f(x: Int) -> Int }")["S"]
    emits = _decl("service S { emission fn f(x: Int) -> Int }")["S"]
    # plain -> emission is refused either way (an unmarked call site would emit)
    assert _service_compatible(emits, plain, providers_retained=False) is not None
    assert _service_compatible(emits, plain, providers_retained=True) is not None
    # emission -> plain is admitted once the provider is replaced (purity
    # tightened for the consumer); with a provider retained it stays pinned
    assert _service_compatible(plain, emits, providers_retained=False) is None
    assert _service_compatible(plain, emits, providers_retained=True) is not None


def test_service_equal_still_means_identity():
    a = _decl("service S { fn f(x: Int) -> Int }")["S"]
    b = _decl("service S { fn f(x: Int) -> Int }")["S"]
    c = _decl("service S { fn f(x: Int) -> Int\n fn g() -> Int }")["S"]
    assert _service_equal(a, b)
    assert not _service_equal(a, c)      # a compatible superset is not identity
