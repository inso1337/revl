"""A `deferred` emission is enqueued wherever its `emit` marker appears, and
refused wherever it is reached without one. Issue #1457.

A `deferred` emission extern is queued and fires only at the session commit
(item 245). Measured before this change, on the py tier (the only tier with a
session owner), each of these fired AT THE CALL:

  * the tail form `fn enqueue(s, m) = emit deliver(s, m)`, emitted as a plain
    `return deliver(s, m)`;
  * the value forms `let r = emit deliver(..)` and `return emit deliver(..)`;
  * a marked call inside an arrow, `(m: Str) => emit deliver(s, m)`, called;
  * a deferred extern passed as a function VALUE, `apply(deliver, s, m)`, in
    a provide method or in a module `fn` the method calls.

And on the five ownerless tiers, whose emitters refuse a deferred call because
there is no queue to put it on, only the `emit` STEP was refused: every value
form above emitted code that fires at once.

Decided (issue #1457): an `emit`-marked crossing of a deferred extern is
enqueued in every position the marker appears; a deferred extern reached with
no marker (as a value, inside an arrow in a fn body, through a helper) does not
compile, and the refusal names the route.

The py rows RUN the emitted code through a live session and observe the sink
before and after the commit, not the IR.
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError

ROOT = Path(__file__).resolve().parents[1]
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the deferral queue is observed on a live cordis-py session")

OWNERLESS_TIERS = ("rust", "go", "java", "wasm", "typescript")

# A body for every tier, so the only thing an ownerless tier can refuse is the
# deferral itself.
_DELIVER = (
    "extern emission deferred fn deliver(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('deliver:' + msg + '\\n')\n"
    "    return\n"
    "} = @ts { } = @rs { } = @go { } = @java { }\n"
)
_SVC = "service Ops {\n  emission fn enqueue(sink: Str, msg: Str)\n}\n"

# every position the marker can appear in a provide method
MARKED = {
    "step": "    fn enqueue(sink, msg) { emit deliver(sink, msg) }\n",
    "tail": "    fn enqueue(sink, msg) = emit deliver(sink, msg)\n",
    "let_value": ("    fn enqueue(sink, msg) {\n"
                  "      let r = emit deliver(sink, msg)\n      return r\n    }\n"),
    "return_value": ("    fn enqueue(sink, msg) {\n"
                     "      return emit deliver(sink, msg)\n    }\n"),
    "arrow_body": ("    fn enqueue(sink, msg) {\n"
                   "      let f = (m: Str) => emit deliver(sink, m)\n"
                   "      let r = f(msg)\n      return r\n    }\n"),
}
VALUE_FORMS = ("tail", "let_value", "return_value", "arrow_body")


def _program(body: str, prelude: str = "") -> str:
    return (_DELIVER + prelude + _SVC + "component Agent provides ops: Ops {\n"
            "  provide ops {\n" + body + "  }\n}\n")


def _emitter(tier: str):
    path = ROOT / "backends" / tier / "emit.py"
    spec = importlib.util.spec_from_file_location(f"revl_1457_{tier}_emit", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _lines(sink: str) -> list:
    return Path(sink).read_text().splitlines() if os.path.exists(sink) else []


# ------------------------------------------------------- py: held until commit

@needs_cordis
@pytest.mark.parametrize("shape", sorted(MARKED))
def test_a_marked_deferred_crossing_is_held_until_the_commit(shape):
    from revl.mcp.session import Session
    sink = tempfile.mktemp(suffix=".log")
    session = Session()
    session.load(compile_source(_program(MARKED[shape]), f"{shape}.rvl"))
    session.call("ops", "enqueue", [sink, "q"])
    assert _lines(sink) == [], f"{shape}: fired at the call, before the commit"
    assert len(session._owner._queue) == 1
    manifest = session.commit()
    assert session.commit_confirm(manifest["hash"])["committed"]
    assert _lines(sink) == ["deliver:q"]


@needs_cordis
@pytest.mark.parametrize("shape", VALUE_FORMS)
def test_an_abort_drops_a_value_form_deferred_crossing(shape):
    from revl.mcp.session import Session
    sink = tempfile.mktemp(suffix=".log")
    session = Session()
    session.load(compile_source(_program(MARKED[shape]), f"{shape}.rvl"))
    session.call("ops", "enqueue", [sink, "q"])
    assert session.abort()["droppedDeferred"] == 1
    assert _lines(sink) == []


# ------------------------------------------- ownerless tiers: refused, not fired

@pytest.mark.parametrize("shape", VALUE_FORMS)
@pytest.mark.parametrize("tier", OWNERLESS_TIERS)
def test_an_ownerless_tier_refuses_a_value_form_deferred_call(tier, shape):
    ir = compile_source(_program(MARKED[shape]), f"{shape}.rvl")
    module = _emitter(tier)
    with pytest.raises(module.EmitError) as excinfo:
        module.emit(ir)
    assert "needs a session owner runtime" in str(excinfo.value)
    assert "`deliver`" in str(excinfo.value)


# ------------------------------------------------- unmarked routes: refused

_APPLY = ("fn apply(f: (Str, Str) -> Unit, a: Str, b: Str) -> Unit "
          "{ return f(a, b) }\n")


def _refusal(src: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "t.rvl")
    return excinfo.value


def test_a_deferred_extern_passed_as_a_value_in_a_method_is_refused():
    err = _refusal(_program(
        "    fn enqueue(sink, msg) {\n"
        "      let r = apply(deliver, sink, msg)\n      return r\n    }\n",
        _APPLY))
    assert err.code == "G4"
    assert err.message == (
        "`deferred` emission extern `deliver` is passed as a function value in "
        "component `Agent`; whoever calls the value fires it at once, with no "
        "session commit (G4)")


def test_a_deferred_extern_passed_as_a_value_in_a_fn_body_is_refused():
    err = _refusal(_program(
        "    fn enqueue(sink, msg) { emit run(sink, msg) }\n",
        _APPLY + "fn run(a: Str, b: Str) -> Unit { return apply(deliver, a, b) }\n"))
    assert err.message == (
        "`deferred` emission extern `deliver` is passed as a function value in "
        "the body of fn `run`; whoever calls the value fires it at once, with no "
        "session commit (G4)")


def test_a_deferred_call_inside_an_arrow_in_a_fn_body_names_the_route():
    err = _refusal(_program(
        "    fn enqueue(sink, msg) { emit run(sink, msg) }\n",
        _APPLY + "fn run(a: Str, b: Str) -> Unit "
                 "{ return apply((x: Str, y: Str) => deliver(x, y), a, b) }\n"))
    assert err.message == (
        "`deferred` emission extern `deliver` cannot be called inside an arrow "
        "in the body of fn `run`; a fn/test body has no session commit for the "
        "deferral to fire at (G4)")


def test_an_unmarked_call_inside_an_arrow_in_a_method_is_refused():
    """The component spelling of the arrow route: the marker rule (issue
    #1437) refuses the unmarked call, so the extern is never reached
    unmarked."""
    err = _refusal(_program(
        "    fn enqueue(sink, msg) {\n"
        "      let r = apply((x: Str, y: Str) => deliver(x, y), sink, msg)\n"
        "      return r\n    }\n", _APPLY))
    assert err.message == "call to emission `deliver` must be marked `emit` (G4)"


def test_a_plain_value_reference_to_an_ordinary_extern_is_unchanged():
    """Only a `deferred` extern is refused as a value; the rule is not a ban
    on function values."""
    src = _program(
        "    fn enqueue(sink, msg) {\n"
        "      let r = apply(note, sink, msg)\n      return r\n    }\n",
        _APPLY + "extern pure fn note(a: Str, b: Str) -> Unit = @py { return }\n")
    assert compile_source(src, "t.rvl")
