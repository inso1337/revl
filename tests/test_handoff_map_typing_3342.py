"""Issue #3342 slice 1: a `handoff <key>: Map[K, V]` declaration types the
component's host Map.

Before this slice the checker did not know what a host Map holds, so the
program below compiled: `put("text")` stored a Str into a Map declared
`Map[Str, Int]`, `get()` (declared `-> Int`) returned it, and `plus()` raised
TypeError at run time. The declaration now types the component's one
`Map.new()` acquisition: `insert`, `insert_if_absent`, `get` and `remove` are
held to K and V, and a `get` is `Opt[V]`.

A Map with no handoff declaration, and a component that acquires more than one
Map (which one the declaration names is not decided), behave exactly as before.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402


def _program(*, put_ty: str = "Str", state: str | None = "Map[Str, Int]",
             get_ret: str = "Int", get_body: str = 'm.get("k") ?? 0',
             extra: str = "") -> str:
    handoff = f"  handoff k: {state}\n" if state else ""
    return (
        f"service K {{ emission fn put(v: {put_ty})\n"
        f"  fn get() -> {get_ret}\n"
        "  fn plus() -> Int }\n"
        "component C provides k: K {\n"
        f"{handoff}"
        "  let m = effect Map.new() undo m.drop()\n"
        f"{extra}"
        "  provide k {\n"
        '    fn put(v) { effect m.insert("k", v) undo m.remove("k") }\n'
        f"    fn get() = {get_body}\n"
        '    fn plus() = (m.get("k") ?? 0) + 1\n'
        "  }\n"
        "}\n")


def _refusal(src: str) -> RevlError:
    with pytest.raises(RevlError) as info:
        compile_source(src, "t.rvl")
    return info.value


def test_the_issue_program_is_refused_at_the_insert_line():
    err = _refusal(_program())
    assert err.line == 8
    assert err.code == "T1"
    assert err.message == ("`insert` value of the Map typed by "
                           "`handoff k: Map[Str, Int]` expects `Int`, got `Str`")


def test_a_read_used_as_another_type_is_refused_at_the_get_line():
    # the store is right (Int into Map[Str, Int]); the read is used as a Str
    err = _refusal(_program(put_ty="Int", get_ret="Str",
                            get_body='m.get("k") ?? "none"'))
    assert err.line == 9
    assert err.code == "T1"
    assert err.message == "`get` returns expects `Str`, got `Int`"


def test_a_key_of_the_wrong_type_is_refused():
    err = _refusal(_program(put_ty="Int", state="Map[Int, Int]",
                            get_body="m.get(1) ?? 0"))
    assert err.line == 8
    assert err.message == ("`insert` key of the Map typed by "
                           "`handoff k: Map[Int, Int]` expects `Int`, got `Str`")


def test_a_get_key_of_the_wrong_type_is_refused():
    err = _refusal(_program(put_ty="Int", get_body="m.get(1) ?? 0"))
    assert err.line == 9
    assert err.message == ("`get` key of the Map typed by "
                           "`handoff k: Map[Str, Int]` expects `Str`, got `Int`")


def test_an_activation_body_insert_is_held_to_the_declaration_too():
    err = _refusal(_program(
        put_ty="Int",
        extra='  effect m.insert("seed", "x") undo m.remove("seed")\n'))
    assert err.line == 7
    assert "typed by `handoff k: Map[Str, Int]`" in err.message


def test_the_declared_program_compiles():
    compile_source(_program(put_ty="Int"), "t.rvl")


def test_a_map_with_no_handoff_declaration_is_unchanged():
    # the issue's own program without the `handoff` line compiles, as on main
    compile_source(_program(state=None), "t.rvl")


def test_two_maps_are_not_typed_by_one_declaration():
    # which of two Maps the declaration names is not decided: both stay on the
    # audit surface, as on main
    compile_source(_program(extra="  let n = effect Map.new() undo n.drop()\n"),
                   "t.rvl")


def test_a_non_map_handoff_types_nothing():
    compile_source(_program(state="Str"), "t.rvl")


@pytest.mark.skipif(importlib.util.find_spec("cordis") is None,
                    reason="cordis-py runtime not installed "
                           "(run `sh backends/python/setup.sh`)")
def test_the_declared_program_runs_and_plus_adds_one():
    sys.path.insert(0, str(ROOT / "backends" / "python"))
    from revl.mcp.session import Session

    src = _program(put_ty="Int")
    session = Session()
    session.load(compile_source(src, "t.rvl"), origin={"source": src})
    session.call("k", "put", [41])
    assert session.call("k", "get", [])["result"] == 41
    assert session.call("k", "plus", [])["result"] == 42
