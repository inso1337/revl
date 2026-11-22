"""`Session.call` checks its arguments against the declared parameter types
(issue #3345).

WHAT WAS WRONG. `call(key, method, args)` handed `args` to the provided method
as they came. A record missing a field, a string where a record was declared or
a fractional number for an `Int` was accepted: either a host error surfaced later
from inside the method (`KeyError: 'h'`), or nothing surfaced and a value that
did not have its declared type kept flowing (`keep` returned a `Box` with no
`h`).

WHAT HAPPENS NOW. The call is refused before the method runs, with a message
naming the method, the parameter and the path of the first mismatch. `check=False`
skips the check for a caller whose arguments came from the session, and
`Session.check_args` checks without calling.

The pure checker is tested without a runtime; the end-to-end tests drive a live
cordis-py composition and SKIP without the pinned fork, and a skip is not a pass.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.mcp.arg_check import args_error
from revl.mcp.session import Session, SessionError

ROOT = Path(__file__).resolve().parents[1]
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the calls run against a live cordis-py composition; install the "
           "pinned fork with `sh backends/python/setup.sh`",
)

SRC = """
type Box = { w: Int, h: Int, tag: Str }
type Shape = Circle(Int) | Square(Box) | Dot
type Holder = { items: List[Int], note: Opt[Str], by: Map[Str, Int], shape: Shape }

service Shapes {
  fn area(b: Box) -> Int
  fn keep(b: Box) -> Box
  fn tag_of(b: Box) -> Str
  fn twice(n: Int) -> Int
  fn half(x: Float) -> Float
  fn name(s: Shape) -> Str
  fn hold(h: Holder) -> Holder
  fn maybe(n: Opt[Int]) -> Int
  fn pair(a: Int, b: Str) -> Str
}

component Flat provides shapes: Shapes {
  provide shapes {
    fn area(b) = b.w * b.h
    fn keep(b) = b
    fn tag_of(b) = b.tag
    fn twice(n) = n + n
    fn half(x) = x
    fn name(s) = match s { Circle(r) => "circle", Square(b) => b.tag, Dot => "dot" }
    fn hold(h) = h
    fn maybe(n) = 1
    fn pair(a, b) = b
  }
}
"""

BOX = {"w": 2, "h": 3, "tag": "a"}


@pytest.fixture(scope="module")
def ir():
    return compile_source(SRC, "shapes.rvl")


def _err(ir, method, *args):
    return args_error(ir, "shapes", method, list(args))


# -- the checker, without a runtime ------------------------------------------

def test_conforming_arguments_pass(ir):
    assert _err(ir, "keep", BOX) is None
    assert _err(ir, "twice", 4) is None
    assert _err(ir, "half", 1) is None          # Float takes an Int
    assert _err(ir, "half", 1.5) is None
    assert _err(ir, "maybe", None) is None
    assert _err(ir, "maybe", 3) is None
    assert _err(ir, "pair", 1, "x") is None
    assert _err(ir, "hold", {"items": [1], "note": None, "by": {"k": 1},
                             "shape": {"$kind": "Dot"}}) is None


def test_missing_field_is_named_with_method_param_and_path(ir):
    assert _err(ir, "keep", {"w": 2, "tag": "a"}) == "shapes.keep: b.h is missing"
    assert _err(ir, "area", {"w": 2, "tag": "a"}) == "shapes.area: b.h is missing"
    assert _err(ir, "tag_of", {"w": 1, "h": 1, "zzz": 1}) \
        == "shapes.tag_of: b.tag is missing"


def test_wrong_kind_is_refused(ir):
    assert "b.w expected Int" in _err(ir, "keep", {**BOX, "w": "two"})
    assert "b expected Box" in _err(ir, "keep", "hello")
    assert "n expected Int" in _err(ir, "twice", 1.5)
    assert "n expected Int" in _err(ir, "twice", "ab")
    assert "n expected Int" in _err(ir, "twice", True)
    assert "x expected Float" in _err(ir, "half", "x")


def test_nested_path_is_reported(ir):
    ok = {"items": [1, 2], "note": "n", "by": {"k": 1}, "shape": {"$kind": "Dot"}}
    assert "h.items[1] expected Int" in _err(ir, "hold", {**ok, "items": [1, "z"]})
    assert "h.by[<key 0>] expected Int" in _err(ir, "hold", {**ok, "by": {"k": "z"}})
    assert "h.note expected Str" in _err(ir, "hold", {**ok, "note": 5})
    assert _err(ir, "hold", {k: v for k, v in ok.items() if k != "note"}) \
        == "shapes.hold: h.note is missing"


def test_variant_cases(ir):
    class Circle:
        __slots__ = ("value",)

        def __init__(self, value):
            self.value = value

    class Mystery:
        __slots__ = ()

    assert _err(ir, "name", Circle(3)) is None
    assert _err(ir, "name", "Dot") is None                 # an enum-style bare name
    assert _err(ir, "name", {"tag": "Circle", "value": 3}) is None
    assert _err(ir, "name", {"$kind": "Square", "$value": BOX}) is None
    assert "got Mystery" in _err(ir, "name", Mystery())
    assert "one of Circle, Dot, Square" in _err(ir, "name", "Triangle")
    assert "one of" in _err(ir, "name", "Circle")          # a payload case needs its payload
    assert "one of" in _err(ir, "name", 7)
    assert "s.Circle expected Int" in _err(ir, "name", Circle("r"))
    assert "s.Square.h is missing" in _err(
        ir, "name", {"$kind": "Square", "$value": {"w": 1, "tag": "t"}})


def test_a_map_key_is_never_echoed(ir):
    ok = {"items": [1], "note": None, "by": {}, "shape": {"$kind": "Dot"}}
    err = _err(ir, "hold", {**ok, "by": {"sk-live-SECRET": "x"}})
    assert "SECRET" not in err
    assert "h.by[<key 0>] expected Int" in err


GENERIC = """
type Pair[A, B] = { a: A, b: B }
type Tree[A] = { v: A, kids: List[Tree[A]] }
type Maybe[A] = Nope | Yes(A)
service G {
  fn takepair(p: Pair[Int, Str]) -> Int
  fn tree(t: Tree[Int]) -> Int
  fn maybe(m: Maybe[Pair[Int, Str]]) -> Int
}
component C provides g: G {
  provide g {
    fn takepair(p) = p.a
    fn tree(t) = t.v
    fn maybe(m) = 1
  }
}
"""


def test_an_instantiated_generic_is_checked_with_its_arguments():
    gir = compile_source(GENERIC, "generic.rvl")

    def err(method, arg):
        return args_error(gir, "g", method, [arg])

    assert err("takepair", {"a": 1, "b": "x"}) is None
    assert "p expected Pair[Int, Str], got str" in err("takepair", "hello")
    assert err("takepair", {"b": "x"}) == "g.takepair: p.a is missing"
    assert err("takepair", {"a": "one", "b": "x"}) == \
        "g.takepair: p.a expected Int, got str"
    assert "p.b expected Str" in err("takepair", {"a": 1, "b": 2})
    # a recursive generic terminates and checks every level
    assert err("tree", {"v": 1, "kids": [{"v": 2, "kids": []}]}) is None
    assert "t.kids[0].v expected Int" in err(
        "tree", {"v": 1, "kids": [{"v": "x", "kids": []}]})
    # a value that contains itself terminates
    cyclic = {"v": 1, "kids": []}
    cyclic["kids"].append(cyclic)
    assert err("tree", cyclic) is None
    # a generic variant payload is bound too
    assert err("maybe", {"$kind": "Nope"}) is None
    assert err("maybe", {"$kind": "Yes", "$value": {"a": 1, "b": "x"}}) is None
    assert "m.Yes.a expected Int" in err(
        "maybe", {"$kind": "Yes", "$value": {"a": "z", "b": "x"}})


def test_value_is_never_echoed(ir):
    assert "hunter2" not in _err(ir, "twice", "hunter2")
    assert "hunter2" not in _err(ir, "name", "hunter2")
    assert "hunter2" not in _err(ir, "keep", {**BOX, "w": "hunter2"})


def test_mangled_case_spelling_is_accepted():
    # the python emitter spells a case named like a keyword or an emitted
    # builtin with a trailing `_` (`None` -> `None_`), and `$kind` carries it
    ir2 = compile_source("""
type Pick = None | Exception(Int)
service Picks { fn take(p: Pick) -> Int }
component Q provides picks: Picks { provide picks { fn take(p) = 1 } }
""", "picks.rvl")

    class None_:
        __slots__ = ()

    class Exception_:
        __slots__ = ("value",)

        def __init__(self, value):
            self.value = value

    def err(arg):
        return args_error(ir2, "picks", "take", [arg])

    assert err(None_()) is None
    assert err(Exception_(3)) is None
    assert err({"$kind": "None_"}) is None
    assert err({"$kind": "Exception_", "$value": 3}) is None
    assert err({"$kind": "Exception", "$value": 3}) is None
    assert "p.Exception expected Int" in err(Exception_("x"))
    assert "got Other" in err(type("Other", (), {"__slots__": ()})())


def test_arity(ir):
    assert _err(ir, "twice") == "shapes.twice: argument n is missing"
    assert _err(ir, "twice", 1, 2) == "shapes.twice: takes 1 argument(s), got 2"
    # an omitted trailing Opt is refused too: the method would otherwise fail
    # with a host TypeError for the missing positional argument
    assert _err(ir, "maybe") == "shapes.maybe: argument n is missing"


def test_unknown_operation_or_key_is_left_to_the_call(ir):
    assert args_error(ir, "nope", "keep", [1]) is None
    assert args_error(ir, "shapes", "nope", [1]) is None
    assert args_error(None, "shapes", "keep", [1]) is None


# -- Session.call, live ------------------------------------------------------

@pytest.fixture
def session(ir):
    s = Session()
    s.load(ir)
    return s


@needs_cordis
def test_call_refuses_an_ill_shaped_record_at_the_call(session):
    for method, arg, message in [
        ("keep", {"w": 2, "tag": "a"}, "shapes.keep: b.h is missing"),
        ("area", {"w": 2, "tag": "a"}, "shapes.area: b.h is missing"),
        ("tag_of", {"w": 1, "h": 1, "zzz": 1}, "shapes.tag_of: b.tag is missing"),
    ]:
        with pytest.raises(SessionError) as info:
            session.call("shapes", method, [arg])
        assert str(info.value) == message
        assert info.value.code == "ARGS"
    for method, arg in [("keep", {**BOX, "w": "two"}), ("keep", "hello"),
                        ("twice", 1.5), ("twice", "ab")]:
        with pytest.raises(SessionError):
            session.call("shapes", method, [arg])
    with pytest.raises(SessionError):
        session.call("shapes", "keep", [{**BOX, "w": "two"}], raw=True)


@needs_cordis
def test_call_accepts_a_conforming_record(session):
    assert session.call("shapes", "keep", [BOX])["result"] == BOX
    assert session.call("shapes", "area", [BOX])["result"] == 6
    assert session.call("shapes", "twice", [4])["result"] == 8


@needs_cordis
def test_check_false_hands_the_arguments_over_unchecked(session):
    out = session.call("shapes", "keep", [{"w": 2, "tag": "a"}], check=False)
    assert out["result"] == {"w": 2, "tag": "a"}


@needs_cordis
def test_check_args_checks_without_calling(session):
    session.check_args("shapes", "keep", [BOX])
    with pytest.raises(SessionError, match=r"shapes\.keep: b\.h is missing"):
        session.check_args("shapes", "keep", [{"w": 2, "tag": "a"}])


@needs_cordis
def test_call_refuses_an_ill_shaped_generic_record_and_an_omitted_opt(session):
    gsession = Session()
    gsession.load(compile_source(GENERIC, "generic.rvl"))
    try:
        assert gsession.call("g", "takepair", [{"a": 1, "b": "x"}])["result"] == 1
        for bad in ("hello", {"b": "x"}):
            with pytest.raises(SessionError) as info:
                gsession.call("g", "takepair", [bad])
            assert info.value.code == "ARGS"
    finally:
        gsession.unload()
    with pytest.raises(SessionError, match=r"shapes\.maybe: argument n is missing"):
        session.call("shapes", "maybe", [])
