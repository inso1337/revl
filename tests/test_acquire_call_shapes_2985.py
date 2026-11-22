"""The call shapes beside the value form of an `acquire` extern (issue #2985,
follow-up to #2972).

An `acquire` extern read as a VALUE in a provide-method body is refused (#2972).
Two neighbouring shapes were still admitted there, with the same loss: the
acquisition is not the root of an `effect` bracket, so no step is built and the
`undo` the declaration asserts is never registered.

  1. a plain call: `let h = open_sock()`, `return open_sock().fd`
  2. an arrow whose body makes the call: `let f = () => open_sock()`

Both are refused now, code G4, category `acquire`, with one sentence:

  `acquire` extern `open_sock` cannot be called inside a provide-method body,
  where only `spawn` may be acquired; its declared `undo` teardown would be
  dropped, leaving the acquisition irreversible (G4)

The self-host gate agrees on both, and on the DIRECT acquisition they inherit
the category from (item 386: only `spawn` may be acquired in a method body, with
item 397's lift for a result-declared verb on a host `Map`), whether or not the
program declares an `acquire` extern. One under-refusal is left and pinned in
`SELFHOST_GAPS`.

The same issue measured one over-refusal that #2982 introduced in a module
`fn`: a parameter or local that shadows an acquire extern and is then read as a
value was refused where it had been admitted. The `fn` walk now keeps the
lexical scope the reference's component walk keeps, so a name bound in scope at
the read is the binding, and a read where no binding is in scope is still the
extern.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from _compiled_ir import compiled_ir  # noqa: E402


_HOST = (
    "type Sock = { fd: Int }\n"
    "extern pure fn close_sock(h: Sock) = @py { return None }\n"
    "extern acquire fn open_sock() -> Sock undo close_sock(result)"
    ' = @py { return {"fd": 1} }\n'
)

_APPLY = "fn apply(f: () -> Sock) -> Int { return 0 }\n"

_CALL_MESSAGE = (
    "`acquire` extern `open_sock` cannot be called inside a provide-method "
    "body, where only `spawn` may be acquired; its declared `undo` teardown "
    "would be dropped, leaving the acquisition irreversible (G4)")


def _value_message(where: str) -> str:
    return (f"`acquire` extern `open_sock` is used as a function value {where}; "
            f"its declared `undo` teardown would be dropped, leaving the "
            f"acquisition irreversible (G4)")


def _method(body: str, functions: str = "") -> str:
    return (
        _HOST + functions
        + "service Ops { fn invoke() -> Int }\n"
        + "component C provides ops: Ops {\n"
        + "  provide ops {\n    fn invoke() {\n"
        + f"      {body}\n"
        + "    }\n  }\n}\n"
    )


def _fn(source: str) -> str:
    return _HOST + source


def _refuse(src: str) -> RevlError:
    with pytest.raises(RevlError) as caught:
        compile_source(src, "acquire-call-shapes-2985.rvl")
    return caught.value


# (id, provide-method body, module functions)
_CALL_FORMS = [
    ("let-call", "let h = open_sock()\n      return 1", ""),
    ("call-then-field", "return open_sock().fd", ""),
    ("arrow-bound", "let f = () => open_sock()\n      return 0", ""),
    ("arrow-passed", "return apply(() => open_sock())", _APPLY),
]

# a fn that binds `open_sock` and reads it in scope: the binding, not the extern
_SHADOWED_FNS = [
    ("local", "fn g() -> Int {\n  let open_sock = 1\n  let f = open_sock\n"
              "  return f\n}\n"),
    ("param", "fn g(open_sock: Int) -> Int {\n  let f = open_sock\n"
              "  return f\n}\n"),
    ("for-element", "fn g(xs: List[Int]) -> Int {\n  var t = 0\n"
                    "  for (open_sock of xs) {\n    t = t + open_sock\n  }\n"
                    "  return t\n}\n"),
    ("arrow-param", "fn g() -> Int {\n  let f = (open_sock) => open_sock + 1\n"
                    "  return f(1)\n}\n"),
    ("match-payload", "fn g(o: Opt[Int]) -> Int {\n"
                      "  return match o { Some(open_sock) => open_sock, "
                      "None => 0 }\n}\n"),
    ("block-arm-let", "type R = A(Int) | B\nfn g(o: R) -> Int {\n"
                      "  return match o { A(v) => { let open_sock = v\n"
                      " open_sock }, B => 0 }\n}\n"),
    ("list-destructure", "fn g(r: List[Int]) -> Int {\n"
                         "  let [open_sock, b] = r\n  let f = open_sock\n"
                         "  return f\n}\n"),
    ("list-rest", "fn g(r: List[Int]) -> Int {\n"
                  "  let [h, ...open_sock] = r\n  let f = open_sock\n"
                  "  return h\n}\n"),
    ("record-destructure", "type P = { open_sock: Int }\n"
                           "fn g(r: P) -> Int {\n  let { open_sock } = r\n"
                           "  let f = open_sock\n  return f\n}\n"),
]

# a fn that reads the EXTERN, whatever else it binds elsewhere
_UNSHADOWED_FNS = [
    ("plain", "fn g() -> Int {\n  let f = open_sock\n  return 0\n}\n"),
    ("bound-after-the-read",
     "fn g() -> Int {\n  let f = open_sock\n  let open_sock = 1\n"
     "  return 0\n}\n"),
    ("bound-in-another-block",
     "fn g(c: Bool) -> Int {\n  if (c) {\n    let open_sock = 1\n  }\n"
     "  let f = open_sock\n  return 0\n}\n"),
    ("for-element-after-the-loop",
     "fn g(xs: List[Int]) -> Int {\n  for (open_sock of xs) {\n"
     "    let q = 1\n  }\n  let f = open_sock\n  return 0\n}\n"),
    ("initializer-of-the-same-name",
     "fn g() -> Int {\n  let open_sock = open_sock\n  return 0\n}\n"),
    ("match-payload-of-another-arm",
     "type R = A(Int) | B\nfn g(o: R) -> Int {\n"
     "  let f = match o { A(open_sock) => 1, B => 2 }\n"
     "  let h = open_sock\n  return f\n}\n"),
    ("block-arm-let-after-the-match",
     "type R = A(Int) | B\nfn g(o: R) -> Int {\n"
     "  let f = match o { A(v) => { let open_sock = v\n 1 }, B => 2 }\n"
     "  let h = open_sock\n  return f\n}\n"),
    ("destructure-after-the-read",
     "fn g(r: List[Int]) -> Int {\n  let f = open_sock\n"
     "  let [open_sock, b] = r\n  return b\n}\n"),
]

# A read in ANOTHER arm of the `match` whose block arm binds the name. The
# reference refuses it; the gate sees the arm's `let` hoisted ahead of the
# statement and cannot tell which arm a read sits in. One-way: the gate may
# under-refuse, never over-refuse. Delete the entry when the gate scopes arms.
_ARM_LEAK = (
    "type R = A(Int) | B\nfn apply(f: () -> Sock) -> Int { return 0 }\n"
    "fn g(o: R) -> Int {\n"
    "  return match o { A(v) => { let open_sock = v\n 1 },"
    " B => apply(open_sock) }\n}\n")
SELFHOST_GAPS = {"block-arm-let-read-in-another-arm": _ARM_LEAK}


# ---------------------------------------------------------------- reference

@pytest.mark.parametrize(("_name", "body", "functions"), _CALL_FORMS,
                         ids=[c[0] for c in _CALL_FORMS])
def test_a_call_in_a_provide_method_is_refused(_name, body, functions):
    err = _refuse(_method(body, functions))
    assert err.code == "G4"
    assert err.category == "acquire"
    assert err.message == _CALL_MESSAGE


def test_the_call_refusal_fires_at_the_call():
    src = _method("let h =\n        open_sock()\n      return 1")
    err = _refuse(src)
    assert err.message == _CALL_MESSAGE
    assert err.line == src.splitlines().index("        open_sock()") + 1


def test_the_bracketed_acquisition_is_still_the_bracket_refusal():
    """The root of an `effect` is the acquisition, not a stray call: it keeps
    the item-386 sentence instead of the new one."""
    err = _refuse(_method(
        "let h = effect open_sock() undo close_sock(h)\n      return 1"))
    assert err.message == ("only `spawn` may be acquired inside a "
                           "provide-method body")


def test_the_activation_body_keeps_its_direct_acquisition():
    compile_source(
        _HOST
        + "service Ops { fn invoke() -> Int }\n"
        + "component C provides ops: Ops {\n"
        + "  let h = effect open_sock() undo close_sock(h)\n"
        + "  provide ops { fn invoke() { return 1 } }\n"
        + "}\n",
        "acquire-activation-2985.rvl")


def test_a_pure_extern_call_in_a_provide_method_is_untouched():
    compile_source(
        _HOST + "extern pure fn probe() -> Int = @py { return 1 }\n"
        + "service Ops { fn invoke() -> Int }\n"
        + "component C provides ops: Ops {\n"
        + "  provide ops { fn invoke() { return probe() } }\n}\n",
        "acquire-pure-call-2985.rvl")


@pytest.mark.parametrize(("_name", "source"), _SHADOWED_FNS,
                         ids=[c[0] for c in _SHADOWED_FNS])
def test_a_binding_in_scope_shadows_the_extern_in_a_fn(_name, source):
    compile_source(_fn(source), "acquire-fn-shadow-2985.rvl")


@pytest.mark.parametrize(("_name", "source"), _UNSHADOWED_FNS,
                         ids=[c[0] for c in _UNSHADOWED_FNS])
def test_a_read_with_no_binding_in_scope_is_still_the_extern(_name, source):
    err = _refuse(_fn(source))
    assert err.code == "G4"
    assert err.category == "acquire"
    assert err.message == _value_message("in the body of fn `g`")


# ---------------------------------------------------------------- self-host

def _exec_gate() -> dict:
    """`selfhost/lower.rvl` compiled by the reference, emitted to python, and
    executed: `admit_src` answers "" (admits) or `<tag>|<message>`."""
    ir = compiled_ir(ROOT / "selfhost" / "lower.rvl")
    spec = importlib.util.spec_from_file_location(
        "pyemit_acquire_call_shapes_2985",
        ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace: dict = {}
        exec(compile(module.emit(ir), "selfhost_acquire_call_shapes_2985.py",
                     "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@pytest.fixture(scope="module")
def admit():
    return _exec_gate()["admit_src"]


def _gate_message(verdict: str) -> str:
    tag, _, message = verdict.partition("|")
    assert tag == "G4", verdict
    return message


@pytest.mark.parametrize(("_name", "body", "functions"), _CALL_FORMS,
                         ids=[c[0] for c in _CALL_FORMS])
def test_the_self_host_refuses_every_call_form_like_the_reference(
        admit, _name, body, functions):
    src = _method(body, functions)
    verdict = admit(src)
    assert verdict != "", f"{_name} is admitted by the self-host gate"
    assert _gate_message(verdict) == _CALL_MESSAGE
    assert _gate_message(verdict) == _refuse(src).message


def test_the_self_host_refuses_the_direct_acquisition(admit):
    src = _method("let h = effect open_sock() undo close_sock(h)\n"
                  "      return 1")
    assert _gate_message(admit(src)) == _refuse(src).message == (
        "only `spawn` may be acquired inside a provide-method body")


def test_the_self_host_admits_what_the_reference_admits(admit):
    """The activation body, the `spawn` control and a result-declared verb on a
    host `Map` (item 397) are not touched on either side."""
    activation = (
        _HOST
        + "service Ops { fn invoke() -> Int }\n"
        + "component C provides ops: Ops {\n"
        + "  let h = effect open_sock() undo close_sock(h)\n"
        + "  provide ops { fn invoke() { return 1 } }\n}\n")
    lift = (
        "service Gate { fn claim(t: Str) -> Bool }\n"
        "component G provides gate: Gate {\n"
        "  let ledger = effect Map.new() undo ledger.drop()\n"
        "  provide gate {\n"
        "    fn claim(t) {\n"
        "      let fresh = effect ledger.insert_if_absent(t, \"x\")\n"
        "                  undo ledger.remove(t)\n"
        "      return fresh\n"
        "    }\n  }\n}\n")
    for name, src in (("activation", activation), ("insert-if-absent", lift)):
        compile_source(src, f"acquire-admits-{name}.rvl")
        assert admit(src) == "", name


@pytest.mark.parametrize(("_name", "source"), _SHADOWED_FNS,
                         ids=[c[0] for c in _SHADOWED_FNS])
def test_the_self_host_admits_a_shadowed_fn_read(admit, _name, source):
    assert admit(_fn(source)) == ""


@pytest.mark.parametrize(("_name", "source"), _UNSHADOWED_FNS,
                         ids=[c[0] for c in _UNSHADOWED_FNS])
def test_the_self_host_refuses_an_unshadowed_fn_read(admit, _name, source):
    src = _fn(source)
    verdict = admit(src)
    assert verdict != "", f"{_name} is admitted by the self-host gate"
    assert _gate_message(verdict) == _value_message("in the body of fn `g`")
    assert _gate_message(verdict) == _refuse(src).message


def test_a_direct_acquisition_is_refused_with_no_acquire_extern_declared(admit):
    """The item-386 rule does not depend on the program declaring an `acquire`
    extern: a host `Map` acquired in a method body is refused on both sides."""
    src = ("service Ops { fn invoke() -> Int }\n"
           "component C provides ops: Ops {\n  provide ops {\n"
           "    fn invoke() {\n      let m = effect Map.new() undo m.drop()\n"
           "      return 1\n    }\n  }\n}\n")
    msg = ("only `spawn` may be acquired inside a provide-method body")
    assert _refuse(src).message == msg
    assert _gate_message(admit(src)) == msg


@pytest.mark.parametrize("name", sorted(SELFHOST_GAPS))
def test_a_known_selfhost_gap_is_still_open(admit, name):
    """The reference refuses, the gate admits. Fails the day the gate refuses
    it: that is a fix, and the entry is deleted rather than reworded."""
    src = _fn(SELFHOST_GAPS[name])
    err = _refuse(src)
    assert err.category == "acquire"
    assert err.message == _value_message("in the body of fn `g`")
    assert admit(src) == "", (
        f"the self-host gate now refuses {name}: delete it from SELFHOST_GAPS "
        f"and compare the two messages instead")
