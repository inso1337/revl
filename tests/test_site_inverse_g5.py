"""G5 for a SITE-spelled bracket inverse (docs/design/teardown-contract.md).

The contract's entry-kind table says a `bracket` inverse "may emit in teardown:
no (G5)", and the runtime tags a FAILED bracket inverse contract-grade for
exactly that reason: the inverse claimed to be host-local, non-emitting, and
infallible. `_check_witnessed_inverse` enforced that claim for a witnessed
extern's DECLARED inverse only, and said so in its own docstring. The inverse an
author writes at the acquisition site had no such walk, so all three routes to a
boundary ran during teardown:

* a direct call to an `emission` extern;
* a call to a plain `fn` that transitively reaches one (the fn-wrapped escape
  the witnessed check already had to close);
* a call to an `emission` service operation off a required binding.

Teardown replays a bracket inverse on clean unload AND on abort, at or after the
session verdict, so a crossing there is unanswerable, unrollbackable, invisible
to the emission fold, and past the 246 approval gate. `compensate` (item 247) is
the teardown slot that MAY emit; the bracket inverse is not.

The bound is POSITIONAL, not a ban on the extern: the same emission on the
forward path still compiles and still lands on the G8 audit surface. The
false-positive tests below pin that, and pin that an honest host-local inverse
(`p.close()`, a pure fn) is untouched.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.errors import RevlError, RevlErrors  # noqa: E402

_EMIT = 'extern emission fn send_email(to: Str) -> Unit = @py { return None }\n'

_BUS = """
service Bus { emission fn publish(msg: Str) -> Int }
component Wire provides bus: Bus {
  let p = effect Pool.open("u", 1) undo p.close()
  provide bus { fn publish(msg) = p.execute(msg) }
}
"""


def _refusal(src: str) -> RevlError:
    with pytest.raises((RevlError, RevlErrors)) as excinfo:
        compile_source(src, "t.rvl")
    err = excinfo.value
    return err.errors[0] if isinstance(err, RevlErrors) else err


# -- refused: the three routes out of a site `undo` -------------------------

def test_site_undo_direct_emission_is_refused():
    err = _refusal(_EMIT + """
component C {
  let p = effect Pool.open("u", 1) undo send_email("teardown@x")
}
""")
    assert err.code == "G5"
    assert "`send_email`, which is an emission" in str(err)
    assert "may not cross a boundary (G5)" in str(err)


def test_site_undo_fn_wrapped_emission_is_refused():
    """The transitive escape: the inverse calls a plain top-level `fn` whose
    body reaches the emission. The refusal names the derivation, the same way
    the witnessed check does, because both read one emission-reach fixed
    point."""
    err = _refusal(_EMIT + """
fn wrapper(to: Str) -> Unit { return send_email(to) }
component C {
  let p = effect Pool.open("u", 1) undo wrapper("teardown@x")
}
""")
    assert err.code == "G5"
    assert "a fn that reaches an emission `send_email`" in str(err)
    assert "wrapper -> send_email" in str(err)


def test_site_undo_two_hop_fn_chain_is_refused():
    err = _refusal(_EMIT + """
fn inner(to: Str) -> Unit { return send_email(to) }
fn outer(to: Str) -> Unit { return inner(to) }
component C {
  let p = effect Pool.open("u", 1) undo outer("teardown@x")
}
""")
    assert "outer -> inner -> send_email" in str(err)


def test_site_undo_service_operation_emission_is_refused():
    """A service crossing is spelled `db.publish(...)` — an `ExprCall` whose
    callee is a FIELD, not a name — so the extern-name walk alone cannot see
    it. The walk reads the service method's own `emission` bit instead."""
    err = _refusal(_BUS + """
component C requires bus: Bus {
  let p = effect Pool.open("u", 1) undo bus.publish("x")
}
""")
    assert err.code == "G5"
    assert "`bus.publish`, which is an emission" in str(err)


def test_unbound_effect_site_undo_emission_is_refused():
    """An unbound `effect … undo …` registers the same bracket entry, so it is
    held to the same bound."""
    err = _refusal(_EMIT + """
component C {
  effect Pool.open("u", 1) undo send_email("teardown@x")
}
""")
    assert err.code == "G5"


def test_emission_nested_in_an_argument_of_the_inverse_is_refused():
    """The walk descends into arguments: hiding the crossing one level down
    still crosses it."""
    err = _refusal(_EMIT + """
component C {
  let p = effect Pool.open("u", 1) undo p.insert("k", send_email("teardown@x"))
}
""")
    assert err.code == "G5"


# -- refused: the subscription bracket's `undo` -----------------------------

def test_subscribe_undo_emission_is_refused():
    """The stream case is the worst of the three: the same line both crosses a
    boundary in teardown AND leaves the subscription open (item 130's core
    guarantee). G5 is reported first, naming the crossing."""
    err = _refusal(_EMIT + """
component C {
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src undo send_email("stream@x")
  await sub.next()
}
""")
    assert err.code == "G5"
    assert "`send_email`, which is an emission" in str(err)


def test_subscribe_undo_service_operation_emission_is_refused():
    err = _refusal(_BUS + """
component C requires bus: Bus {
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src undo bus.publish("x")
  await sub.next()
}
""")
    assert err.code == "G5"


# -- admitted: the bound is positional, not a ban ---------------------------

def test_a_host_local_inverse_still_admits():
    ir = compile_source(_EMIT + """
component C {
  let p = effect Pool.open("u", 1) undo p.close()
}
""", "t.rvl")
    step = ir["components"][0]["body"][0]
    assert step["undo"]["method"] == "close"


def test_a_pure_fn_inverse_still_admits():
    """No over-refusal: a `fn` that reaches no boundary is in neither the
    extern-class table nor the emission-reach fixed point."""
    ir = compile_source("""
extern pure fn note(x: Str) -> Unit = @py { return None }
fn local_restore(x: Str) -> Unit { return note(x) }
component C {
  let p = effect Pool.open("u", 1) undo local_restore("x")
}
""", "t.rvl")
    assert ir["components"][0]["body"][0]["undo"]["name"] == "local_restore"


def test_the_same_emission_on_the_forward_path_still_compiles():
    ir = compile_source(_EMIT + """
service S { emission fn note(x: Str) -> Unit }
component C provides s: S {
  let p = effect Pool.open("u", 1) undo p.close()
  provide s { fn note(x) = send_email(x) }
}
""", "t.rvl")
    assert ir["components"][0]["name"] == "C"


def test_a_program_with_no_emission_anywhere_is_untouched():
    """Byte-identity floor: the walk is inert when nothing is classified as a
    crossing."""
    src = """
component C {
  let m = effect Map.new() undo m.drop()
  let p = effect Pool.open("u", 1) undo p.close()
}
"""
    assert compile_source(src, "t.rvl") == compile_source(src, "t.rvl")
