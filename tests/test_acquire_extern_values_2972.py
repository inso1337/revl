"""An `acquire` extern reached through a value is refused where the direct
acquisition is refused (issue #2972).

A direct acquisition in a provide-method body is refused ("only `spawn` may be
acquired inside a provide-method body"), while the same acquisition reached
through a value binding was accepted: `let f = open_sock` then `f()`,
`apply(open_sock)`, `first(open_sock)`, a bare mention, and a reference taken in
a module `fn` all compiled. So the value position bought reach the direct
spelling is not allowed to have, which is the opposite of what a syntactic
detour should do.

Both compilers refuse it now, with one sentence, code G4 and category
`acquire`:

  `acquire` extern `open_sock` is used as a function value <where>; its declared
  `undo` teardown would be dropped, leaving the acquisition irreversible (G4)

`<where>` is the position clause: "inside a provide-method body, where only
`spawn` may be acquired" for a component, "in the body of fn `get`" for a module
function. The direct refusal carries the same code and category
(`_acquire_value_refusal` / the item-386 raise), so the two spellings classify
identically and a reader of `--json` cannot tell which one bought the reach.

Where the direct acquisition is ADMITTED, the value is not refused: the
activation body keeps its bracket, and `spawn` keeps its request-scoped
instance. A test body and a local binding named like the extern keep their own
rules (the reference's fn walk keeps no binding scope, and the self-host's
mirror reproduces that exactly).

The self-host difference this file pins is the DIRECT side: `selfhost/lower.rvl`
does not implement the item-386 "only `spawn`" rule, so it admits the direct
acquisition the value refusal inherits its category from. The value side is
ported (the five forms below agree message for message). The pin is named
`SELFHOST_GAPS` and it fails in BOTH directions: the day the gate learns the
direct rule it refuses here, the entry is DELETED and the two compilers compare
messages instead.
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


# ---------------------------------------------------------------- programs

_HOST = (
    "type Sock = { fd: Int }\n"
    "extern pure fn close_sock(h: Sock) = @py { return None }\n"
    "extern acquire fn open_sock() -> Sock undo close_sock(result)"
    ' = @py { return {"fd": 1} }\n'
)

_WORKER = (
    "service T { fn run() -> Int }\n"
    "component W provides task: T {\n"
    "  provide task { fn run() = 7 }\n"
    "}\n"
)

_FN_REF = "fn get() -> () -> Sock { return open_sock }\n"

# (id, provide-method body, module functions, the position clause the refusal
# names). The first four are the issue's forms 1-4, all inside a provide-method
# body; the last is form 5, the reference taken in a module `fn` body.
_METHOD_FORMS = [
    ("let-then-invoke",
     "let f = open_sock\n      let h = f()\n      return 0", "", None),
    ("pass-as-value",
     "return apply(open_sock)",
     "fn apply(f: () -> Sock) -> Int { return 0 }\n", None),
    ("two-hop",
     "return first(open_sock)",
     "fn first(f: () -> Sock) -> Int { return 0 }\n", None),
    ("bare-mention",
     "let f = open_sock\n      return 0", "", None),
]

_FN_FORM = (
    "fn-reference",
    "return get()",
    _FN_REF,
    "in the body of fn `get`",
)

_METHOD_WHERE = ("inside a provide-method body, where only `spawn` may be "
                 "acquired")


def _message(name: str, where: str) -> str:
    return (f"`acquire` extern `{name}` is used as a function value {where}; "
            f"its declared `undo` teardown would be dropped, leaving the "
            f"acquisition irreversible (G4)")


def _program(body: str, functions: str = "") -> str:
    return (
        _HOST + functions
        + "service Ops { fn invoke() -> Int }\n"
        + "component C provides ops: Ops {\n"
        + "  provide ops {\n    fn invoke() {\n"
        + f"      {body}\n"
        + "    }\n  }\n}\n"
    )


def _refuse(src: str) -> RevlError:
    with pytest.raises(RevlError) as caught:
        compile_source(src, "acquire-values-2972.rvl")
    return caught.value


# ---------------------------------------------------------------- reference

@pytest.mark.parametrize(
    ("_name", "body", "functions", "where"),
    _METHOD_FORMS + [_FN_FORM],
    ids=[case[0] for case in _METHOD_FORMS + [_FN_FORM]],
)
def test_every_laundered_form_is_refused_naming_the_extern(
        _name, body, functions, where):
    """All five forms the issue measured are refused, with one sentence that
    names the extern and the shape, in the acquire class."""
    err = _refuse(_program(body, functions))
    assert err.code == "G4"
    assert err.category == "acquire"
    assert err.message == _message("open_sock", where or _METHOD_WHERE)


def test_the_direct_acquisition_keeps_its_message_and_the_same_category():
    """The refusal the value form inherits: the item-386 rule, unchanged in
    words, now carrying the same code and category as its value twin."""
    err = _refuse(_program(
        "let h = effect open_sock() undo close_sock(h)\n      return 1"))
    assert err.message == ("only `spawn` may be acquired inside a "
                           "provide-method body")
    assert err.code == "G4"
    assert err.category == "acquire"


def test_the_value_refusal_fires_at_the_value_not_at_a_later_statement():
    """A line assertion, so the refusal is anchored where the author reads the
    extern rather than on the method header."""
    src = _program("let f =\n        open_sock\n      return 0")
    err = _refuse(src)
    assert err.message == _message("open_sock", _METHOD_WHERE)
    assert err.line == src.splitlines().index("        open_sock") + 1


def test_spawn_is_still_admitted_inside_a_provide_method():
    """The control the issue asks for: `spawn` acquired directly compiles."""
    compile_source(
        _WORKER + _program(
            "let w = effect spawn W with { } undo w.dispose()\n      return 1"),
        "acquire-values-spawn.rvl")


def test_the_activation_body_keeps_its_direct_acquisition():
    """No widening: the activation body is where the acquisition belongs, so
    the direct bracket there is still admitted."""
    compile_source(
        _HOST
        + "service Ops { fn invoke() -> Int }\n"
        + "component C provides ops: Ops {\n"
        + "  let h = effect open_sock() undo close_sock(h)\n"
        + "  provide ops { fn invoke() { return 1 } }\n"
        + "}\n",
        "acquire-activation.rvl")


def test_a_method_local_named_like_the_extern_is_the_local():
    """A binding in the body shadows the extern, so a value read of that name
    is the author's local, not the acquisition."""
    compile_source(_program("let open_sock = 1\n      return 0"),
                   "acquire-shadow.rvl")


def test_a_test_body_keeps_its_own_rules():
    """Out of scope by design: the issue names component method bodies and the
    module `fn`, and the fn walk (unlike a method) keeps no binding scope. A
    test body is neither, and revl's test rules are unchanged."""
    compile_source(
        _HOST
        + 'test "held" {\n  let f = open_sock\n  let h = f()\n}\n',
        "acquire-test-body.rvl")


# ---------------------------------------------------------------- self-host

def _exec_gate() -> dict:
    """`selfhost/lower.rvl` compiled by the reference, emitted to python, and
    executed: `admit_src` is the gate's verdict ("" admits, else
    `<tag>|<message>`). The same harness `tests/test_selfhost_lower.py` uses,
    over the session-cached IR."""
    ir = compiled_ir(ROOT / "selfhost" / "lower.rvl")
    assert ir["ir_version"] == 3
    spec = importlib.util.spec_from_file_location(
        "pyemit_acquire_values_2972", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace: dict = {}
        exec(compile(module.emit(ir), "selfhost_acquire_values_2972.py", "exec"),
             namespace)
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


@pytest.mark.parametrize(
    ("_name", "body", "functions", "where"),
    _METHOD_FORMS + [_FN_FORM],
    ids=[case[0] for case in _METHOD_FORMS + [_FN_FORM]],
)
def test_the_self_host_refuses_every_form_with_the_reference_message(
        admit, _name, body, functions, where):
    """The same five forms, the same sentence, the same tag. This is the parity
    record the issue's acceptance names: `admit` answers `G4|<message>` and the
    message is the reference's verbatim."""
    src = _program(body, functions)
    verdict = admit(src)
    assert verdict != "", f"{_name} is admitted by the self-host gate"
    assert _gate_message(verdict) == _message("open_sock", where or _METHOD_WHERE)
    # ... and the reference's own text for the same program, so a later edit to
    # either sentence fails here rather than drifting silently.
    assert _gate_message(verdict) == _refuse(src).message


def test_the_self_host_admits_the_shadowing_method_local(admit):
    """The scope rule the gate shares with the reference: a `let` of the same
    name in the body is the local on both sides."""
    src = _program("let open_sock = 1\n      return 0")
    assert admit(src) == ""
    compile_source(src, "acquire-shadow-gate.rvl")


def test_the_self_host_admits_the_spawn_control(admit):
    compile_source(
        _WORKER + _program(
            "let w = effect spawn W with { } undo w.dispose()\n      return 1"),
        "acquire-spawn-gate.rvl")
    assert admit(
        _WORKER + _program(
            "let w = effect spawn W with { } undo w.dispose()\n      return 1")
    ) == ""


# The self-host differences this slice does NOT close, named rather than left
# silent. Each entry is a program the reference refuses and the gate ADMITS,
# with the reason it cannot be closed here. The convention is
# `GENERIC_VARIANT_SILENT_ADMITS`'s in tests/test_selfhost_compile.py: keep the
# entry, keep the test, and delete the entry the day the gap closes, so the
# allowance only ever shrinks.
SELFHOST_GAPS = {
    "direct-method-acquisition":
        "the item-386 rule (only `spawn` may be acquired inside a provide-method "
        "body) is not ported to selfhost/lower.rvl, so the DIRECT spelling is "
        "admitted there. The value refusal above inherits its category from "
        "that rule and IS ported; the direct rule is a separate port (it also "
        "needs item 397's result-declared host-verb lift, `insert_if_absent`). "
        "Delete this entry when the direct rule lands.",
}


@pytest.mark.parametrize("name", sorted(SELFHOST_GAPS))
def test_a_known_selfhost_gap_is_still_open(admit, name):
    """The pin. It fails the day the gate refuses the direct acquisition: that
    is a fix, not a regression, and the entry is DELETED rather than reworded.
    The reference side is asserted first, so this can never pass by the
    reference having changed its mind."""
    assert name == "direct-method-acquisition"
    direct = _program("let h = effect open_sock() undo close_sock(h)\n"
                      "      return 1")
    err = _refuse(direct)
    assert err.message == ("only `spawn` may be acquired inside a "
                           "provide-method body")
    assert admit(direct) == "", (
        "the self-host gate now refuses the DIRECT acquisition in a "
        "provide-method body. That closes the remaining gap: DELETE "
        "`direct-method-acquisition` from SELFHOST_GAPS and compare the two "
        f"messages instead ({SELFHOST_GAPS[name]!r}).")
