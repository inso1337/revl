"""Typed delegation, slice 1: the `Delegate[S]` typed, borrow-confined handle
(roadmap item 442, issue #121; docs/design/442-typed-delegation.md §10 S1).

This slice adds the typed handle and confines it, reusing shipped machinery:

  * `Delegate[S]` is a POSITION-RESTRICTED type constructor (like `Async[T]`):
    v1 admits it only as a service-method parameter type, so a delegated
    reference is received for one call and cannot be written as a value
    binding, a return, a field, a config value, or a module `fn` parameter
    (design §4.1);
  * `S` must name a declared service — the interface the reference exposes;
  * a received `Delegate[S]` value is a BORROW: item 308's B1 walk confines it
    exactly as it confines an acquire handle, so it cannot escape the scope
    that received it (design §3.3 D2, §4.3 C3/C4).

Checker-only, no new grammar. Verdict-invariant for every existing program:
nothing in the shipped corpus names `Delegate`.

Not in this slice (see the PR body / roadmap remainder): the mint (`effect
lease` naming a wiring key, yielding the typed handle), the D3 augmented-graph
acyclicity check, the D4 depth bound at a call argument, the ledger `subject` /
`chain` fields, and the `revl audit` surface.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402

# A declared service `Fs`, the surface a delegated reference would expose.
_FS = "service Fs { emission fn write(p: Str) }\n"


def _refuse(src: str) -> str:
    with pytest.raises(RevlError) as ei:
        compile_source(src, "t.rvl")
    return str(ei.value)


def _admit(src: str) -> None:
    compile_source(src, "t.rvl")


# ---------------------------------------------------------------------------
# admits: the one legal position
# ---------------------------------------------------------------------------

def test_delegate_param_in_service_method_admits():
    _admit(
        _FS
        + "service Sink { fn run(d: Delegate[Fs]) -> Int }\n"
        "component P provides sink: Sink {\n"
        "  provide sink { fn run(d) { return 1 } }\n"
        "}\n")


def test_delegate_param_reannotated_in_provide_admits():
    # A provide method may re-spell the parameter; it mirrors the signature.
    _admit(
        _FS
        + "service Sink { fn run(d: Delegate[Fs]) -> Int }\n"
        "component P provides sink: Sink {\n"
        "  provide sink { fn run(d: Delegate[Fs]) { return 1 } }\n"
        "}\n")


# ---------------------------------------------------------------------------
# position restriction: refused everywhere but a service-method parameter
# ---------------------------------------------------------------------------

def test_delegate_as_service_return_refused():
    msg = _refuse(_FS + "service Sink { fn run() -> Delegate[Fs] }\n")
    assert "not a value type" in msg and "service-method parameter" in msg


def test_delegate_as_record_field_refused():
    msg = _refuse(_FS + "type Box = { d: Delegate[Fs] }\n")
    assert "not a value type" in msg


def test_delegate_as_module_fn_param_refused():
    msg = _refuse(_FS + "fn g(d: Delegate[Fs]) -> Int { return 1 }\n")
    assert "not a value type" in msg


def test_delegate_as_config_field_refused():
    msg = _refuse(
        _FS + "component C { config { d: Delegate[Fs] } }\n")
    assert "not a value type" in msg


# ---------------------------------------------------------------------------
# `S` must name a declared service, and arity is exactly one
# ---------------------------------------------------------------------------

def test_delegate_of_primitive_refused():
    msg = _refuse(_FS + "service Sink { fn run(d: Delegate[Int]) -> Int }\n")
    assert "must name a declared service" in msg


def test_delegate_of_undeclared_name_refused():
    msg = _refuse(_FS + "service Sink { fn run(d: Delegate[Nope]) -> Int }\n")
    assert "must name a declared service" in msg


def test_delegate_wrong_arity_refused():
    msg = _refuse(_FS + "service Sink { fn run(d: Delegate[Fs, Fs]) -> Int }\n")
    assert "1 type argument" in msg


def test_delegate_bare_head_refused():
    msg = _refuse(_FS + "service Sink { fn run(d: Delegate) -> Int }\n")
    assert "1 type argument" in msg


def test_delegate_nested_refused():
    msg = _refuse(
        _FS + "service Sink { fn run(d: Delegate[Delegate[Fs]]) -> Int }\n")
    # the inner Delegate is in a non-parameter position
    assert "not a value type" in msg


# ---------------------------------------------------------------------------
# confinement: a received delegated reference is a borrow and does not escape
# (item 308 B1, reused unchanged)
# ---------------------------------------------------------------------------

def test_delegate_borrow_escape_in_carrier_refused():
    msg = _refuse(
        _FS
        + "service Sink { fn run(d: Delegate[Fs]) -> Int }\n"
        "component P provides sink: Sink {\n"
        "  provide sink { fn run(d) { let leak = [d]  return 1 } }\n"
        "}\n")
    assert "borrowed resource `Delegate`" in msg
    assert "308" in msg and "B1" in msg


def test_delegate_borrow_stored_into_state_refused():
    msg = _refuse(
        _FS
        + "service Sink { emission fn run(d: Delegate[Fs]) }\n"
        "component P provides sink: Sink {\n"
        "  let slots = effect Map.new() undo slots.drop()\n"
        "  provide sink { fn run(d) { effect slots.insert(\"k\", d)"
        " undo slots.remove(\"k\") } }\n"
        "}\n")
    # B1 clause 1: a borrow seated into activation-level state is refused, and
    # the delegated reference is the borrow.
    assert "borrowed resource `Delegate`" in msg
    assert "activation-level state" in msg


# ---------------------------------------------------------------------------
# verdict invariance: a composition that names no `Delegate` is untouched
# ---------------------------------------------------------------------------

def test_non_delegate_service_still_admits():
    _admit(
        "service Sink { fn run(p: Str) -> Int }\n"
        "component P provides sink: Sink {\n"
        "  provide sink { fn run(p) { return 1 } }\n"
        "}\n")
