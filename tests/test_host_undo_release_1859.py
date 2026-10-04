"""A host acquisition's `undo` is its family's release on THAT handle.

Issue #1859 (slice 1), with issue #1847. For `Map.new`, `Pool.open` and
`Stream.source` revl owns the stubs, so the inverse is provable: the family's
release (`_HOST_ACQUIRE_VERBS`: `drop`, `close`, `close`) applied to the handle
the bracket bound. Before this only a missing `undo` was refused: `undo
store.get("x")`, a sibling handle, a helper fn, a literal and the unbound
`effect Map.new() undo noop()` all compiled, the handle was never released,
and the teardown report still read clean.

#1847 is the narrow `List` case of the same hole: a builtin method written in
an `undo` drew "`List` is not a declared requirement" with an `add requires
List` hint that, followed, made `List.drop` resolve to a service and compile.
The builtin type now draws a diagnostic that names the type rule, and a
requirement key may not spell a builtin type or a host root.

Each refusal is mirrored in selfhost/lower.rvl, byte for byte; the census
holds the gate to the fixture examples/rejections/g4_undo_not_release.rvl.
"""

import pytest

from revl.compiler import compile_source
from revl.diagnostics import classify
from revl.errors import RevlError

_HEAD = "service S { fn go(n: Int) -> Int }\ncomponent C provides s: S {\n"
_TAIL = "  provide s { fn go(n) = n }\n}\n"


def _src(body: str, prelude: str = "") -> str:
    return prelude + _HEAD + body + "\n" + _TAIL


def _refusal(source: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(source, "t.rvl")
    return excinfo.value


def _release(bind: str, fn: str, release: str) -> str:
    return (f"the `undo` of `let {bind} = effect {fn}(...)` must release THAT "
            f"handle: write `undo {bind}.{release}()`")


def _unbound(fn: str, release: str) -> str:
    return (f"`effect {fn}(...)` must bind its handle so its `undo` can "
            f"release it: write `let <name> = effect {fn}(...) undo "
            f"<name>.{release}()`")


_NOOP = "fn noop() -> Int { return 0 }\n"

# the issue's pairings of a host acquire with an `undo`: only the release on
# the bound handle compiles
PAIRINGS = [
    ('  let store = effect Map.new() undo store.drop()', None),
    ('  let store = effect Map.new() undo store.get("x")', _release("store", "Map.new", "drop")),
    ('  let store = effect Map.new() undo store.remove("x")', _release("store", "Map.new", "drop")),
    ('  let store = effect Map.new() undo store.insert("k", "v")', _release("store", "Map.new", "drop")),
    ('  let store = effect Map.new() undo store.size()', _release("store", "Map.new", "drop")),
    ('  let other = effect Map.new() undo other.drop()\n'
     '  let store = effect Map.new() undo other.drop()', _release("store", "Map.new", "drop")),
    ('  let store = effect Map.new() undo noop()', _release("store", "Map.new", "drop")),
    ('  let store = effect Map.new() undo 1', _release("store", "Map.new", "drop")),
    ('  effect Map.new() undo noop()', _unbound("Map.new", "drop")),
    ('  effect Map.new() undo 1', _unbound("Map.new", "drop")),
]


@pytest.mark.parametrize("body,refused", PAIRINGS)
def test_only_the_release_on_the_bound_handle_compiles(body, refused):
    source = _src(body, _NOOP)
    if refused is None:
        assert compile_source(source, "t.rvl")
        return
    err = _refusal(source)
    assert (err.code, err.message) == ("G4", refused)


@pytest.mark.parametrize("acquire,release", [
    ('Pool.open("pg://x", 4)', "close"),
    ("Stream.source()", "close"),
])
def test_every_acquire_family_is_held_to_its_release(acquire, release):
    fn = acquire.split("(")[0]
    assert compile_source(_src(
        f"  let h = effect {acquire} undo h.{release}()"), "t.rvl")
    err = _refusal(_src(f"  let h = effect {acquire} undo 0"))
    assert (err.code, err.message) == ("G4", _release("h", fn, release))


def test_an_unbound_host_acquisition_in_a_provide_method_is_refused():
    source = ("service S { fn go(n: Int) -> Int }\n"
              "component C provides s: S {\n"
              "  provide s { fn go(n) {\n"
              "    effect Map.new() undo 1\n"
              "    return n\n"
              "  } }\n}\n")
    err = _refusal(source)
    assert (err.code, err.message) == ("G4", _unbound("Map.new", "drop"))


def test_the_g5_classification_of_the_undo_keeps_its_message():
    """The release rule runs after G5, so an emitting `undo` is still refused
    as the boundary crossing it is."""
    source = _src(
        '  let p = effect Pool.open("u", 1) undo send("x")',
        'extern emission fn send(x: Str) -> Int = @py { return 1 }\n')
    err = _refusal(source)
    assert err.code == "G5"
    assert "calls `send`, which is an emission" in err.message


# ---- issue #1847: the three-step chain --------------------------------------

_BUILTIN = "`List` is a builtin type, not a value"


def test_a_builtin_in_an_undo_names_the_rule_not_a_requirement():
    """Step 1: the diagnostic names the type rule, and its hint names what an
    `undo` must be. Never "add requires"."""
    err = _refusal(_src('  let xs = effect Map.new() undo List.drop(xs)'))
    assert classify(err).get("code") == "T1"
    assert err.message == _BUILTIN
    assert "requires" not in err.hint
    assert "undo h.drop()" in err.hint


def test_a_builtin_type_read_as_a_value_elsewhere_draws_the_same_rule():
    source = ("service S { fn go(n: Int) -> List[Str] }\n"
              "component C provides s: S {\n"
              '  provide s { fn go(n) = List.reverse(["a"]) }\n}\n')
    err = _refusal(source)
    assert err.message == _BUILTIN
    assert "requires" not in err.hint


@pytest.mark.parametrize("key,what", [
    ("List", "builtin type"), ("Map", "builtin type"), ("Str", "builtin type"),
    ("Pool", "host root"), ("Job", "host root"),
])
def test_a_requirement_key_may_not_shadow_a_builtin(key, what):
    """Step 2: following the old hint is refused by name, so `List.<op>` can
    never resolve to a required service."""
    source = ("service S { fn go(n: Int) -> Int }\n"
              "service Lst { fn drop(xs: List[Str]) }\n"
              f"component C requires {key}: Lst provides s: S {{\n"
              "  provide s { fn go(n) = n }\n}\n")
    err = _refusal(source)
    assert classify(err).get("code") == "G1"
    assert err.message == (f"requirement key `{key}` of C shadows the {what} "
                           f"`{key}`")


def test_nothing_in_the_chain_is_admitted():
    """Step 3: the document the chain converged on before is refused."""
    source = ("service S { fn go(n: Int) -> Int }\n"
              "service Lst { fn drop(xs: List[Str]) }\n"
              "component C requires List: Lst provides s: S {\n"
              "  let xs = effect Map.new() undo List.drop(xs)\n"
              "  provide s { fn go(n) = n }\n}\n")
    _refusal(source)
