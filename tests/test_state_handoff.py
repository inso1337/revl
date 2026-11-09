"""Verified state hand-off on hot-swap — the static half (roadmap item 53).

The `code_change` gap: `revl swap` (item 23) drains in-flight calls but tears
the old provider's *state* down LIFO, so a stateful successor starts cold. Item
53 closes it revl-style — an optional `handoff` names the shape of the state a
provider **exports** when replaced and **accepts** when replacing, and the
admission gate proves the two sides are §5-compatible before any swap threads
the value across (docs/state-handoff.md).

This file is the frontend contract — grammar, lowering, and the admission
gate — all checkable without a runtime. The warm-start threading itself runs
on cordis-py in `test_state_handoff_exec.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402


def _cache(state_type: str = "Map[Str, Str]", *, version: int = 1,
           name: str = "Cache") -> str:
    return f"""
service Store {{
  fn get(k: Str) -> Opt[Str]
  fn put(k: Str, v: Str)
  fn version() -> Int
}}
component {name} provides cache: Store {{
  handoff cache: {state_type}
  let m = effect Map.new() undo m.drop()
  provide cache {{
    fn get(k) = m.get(k)
    fn put(k, v) {{ effect m.insert(k, v) undo m.remove(k) }}
    fn version() = {version}
  }}
}}
"""


# -- grammar + lowering -------------------------------------------------------

def test_handoff_parses_and_lowers_additively():
    ir = compile_source(_cache(), "c.rvl")
    comp = next(c for c in ir["components"] if c["name"] == "Cache")
    assert comp["handoff"] == {"key": "cache", "type": "Map[Str, Str]"}


def test_handoff_is_absent_when_undeclared():
    src = _cache().replace("  handoff cache: Map[Str, Str]\n", "")
    ir = compile_source(src, "c.rvl")
    comp = next(c for c in ir["components"] if c["name"] == "Cache")
    assert "handoff" not in comp  # additive: stateless IR is byte-identical


def test_handoff_must_target_a_provided_key():
    src = _cache().replace("handoff cache:", "handoff nope:")
    with pytest.raises(RevlError, match="not a declared provision"):
        compile_source(src, "c.rvl")


def test_handoff_must_precede_effects():
    src = _cache().replace(
        "  handoff cache: Map[Str, Str]\n  let m = effect Map.new() undo m.drop()",
        "  let m = effect Map.new() undo m.drop()\n  handoff cache: Map[Str, Str]")
    with pytest.raises(RevlError, match="must precede"):
        compile_source(src, "c.rvl")


def test_at_most_one_handoff_per_component():
    src = _cache().replace(
        "  handoff cache: Map[Str, Str]\n",
        "  handoff cache: Map[Str, Str]\n  handoff cache: Map[Str, Int]\n")
    with pytest.raises(RevlError, match="more than one `handoff`"):
        compile_source(src, "c.rvl")


# -- the admission gate (the §5 relation, pointed at state) -------------------

def test_compatible_successor_is_admitted():
    old = compile_source(_cache(version=1), "c.rvl")
    # same declared shape, changed code — admitted
    compile_source(_cache(version=2), "c.rvl", manifest=old, replacing=("Cache",))


def test_widened_acceptor_is_admitted():
    # predecessor exports a bare `Str`; a successor that accepts the wider
    # `Opt[Str]` still admits everything the predecessor produces (the T ->
    # Opt[T] injection the §5 relation already models)
    old = compile_source(_cache("Str"), "c.rvl")
    compile_source(_cache("Opt[Str]"), "c.rvl", manifest=old, replacing=("Cache",))


def test_narrowed_acceptor_is_refused():
    # the reverse — predecessor exports `Opt[Str]`, successor accepts only
    # `Str` — drops the `None` case, so it is refused
    old = compile_source(_cache("Opt[Str]"), "c.rvl")
    with pytest.raises(RevlError, match="state hand-off on `cache` differs"):
        compile_source(_cache("Str"), "c.rvl", manifest=old, replacing=("Cache",))


def test_incompatible_successor_is_refused_at_admission():
    old = compile_source(_cache("Map[Str, Str]"), "c.rvl")
    with pytest.raises(RevlError, match="state hand-off on `cache` differs"):
        compile_source(_cache("Map[Str, Int]"), "c.rvl",
                       manifest=old, replacing=("Cache",))


def test_refusal_names_both_shapes_and_carries_a_why_trace():
    old = compile_source(_cache("Map[Str, Str]"), "c.rvl")
    try:
        compile_source(_cache("Map[Str, Int]"), "c.rvl",
                       manifest=old, replacing=("Cache",))
        assert False, "expected refusal"
    except RevlError as exc:
        assert "Map[Str, Int]" in str(exc) and "Map[Str, Str]" in str(exc)
        assert exc.why is not None and exc.why.kind == "state-handoff-drift"
        roles = {step.kind for step in exc.why.steps}
        assert {"accept", "export"} <= roles


def test_renamed_successor_correlates_by_provided_key():
    # the successor is a differently-named component that re-provides `cache`;
    # the gate correlates by the *key*, not the component name
    old = compile_source(_cache("Map[Str, Str]", name="Cache"), "c.rvl")
    with pytest.raises(RevlError, match="state hand-off on `cache` differs"):
        compile_source(_cache("Map[Str, Int]", name="CacheV2"), "c.rvl",
                       manifest=old, replacing=("Cache",))


def test_successor_without_handoff_opts_out_no_refusal():
    # a stateless successor of a stateful provider is a valid (lossy) choice —
    # it declares no `handoff`, so the gate does not force the old state on it
    old = compile_source(_cache("Map[Str, Str]"), "c.rvl")
    src = _cache(version=2).replace("  handoff cache: Map[Str, Str]\n", "")
    compile_source(src, "c.rvl", manifest=old, replacing=("Cache",))


def test_cold_key_no_running_export_is_admitted():
    # the running provider declared no hand-off; a successor that *accepts* one
    # simply starts cold — nothing to be incompatible with
    src_old = _cache().replace("  handoff cache: Map[Str, Str]\n", "")
    old = compile_source(src_old, "c.rvl")
    compile_source(_cache("Map[Str, Str]", version=2), "c.rvl",
                   manifest=old, replacing=("Cache",))


# -- the declared shape behind a nominal name ---------------------------------
#
# A `handoff` names its state by a written spelling, and a nominal type is only
# a NAME: `St` in the running version and `St` in the replacement can be two
# different records. The gate resolves each spelling against the declarations
# of ITS OWN version, so a name that points at a changed declaration is a
# changed shape, and the swap is refused instead of threading the old value
# into a record that cannot hold it.

def _with_decls(decls: str, state_type: str, *, version: int = 1) -> str:
    return decls + "\n" + _cache(state_type, version=version)


def _refusal(old_src: str, new_src: str):
    old = compile_source(old_src, "c.rvl")
    with pytest.raises(RevlError) as info:
        compile_source(new_src, "c.rvl", manifest=old, replacing=("Cache",))
    return info.value


def _assert_drift(exc) -> None:
    assert "state hand-off on `cache` differs from the running manifest" in str(exc)
    assert exc.why is not None and exc.why.kind == "state-handoff-drift"
    assert {step.kind for step in exc.why.steps} == {"accept", "export"}


def test_nominal_record_unchanged_is_admitted():
    decl = "type St = { a: Int }"
    old = compile_source(_with_decls(decl, "St", version=1), "c.rvl")
    compile_source(_with_decls(decl, "St", version=2), "c.rvl",
                   manifest=old, replacing=("Cache",))


def test_nominal_record_field_added_is_refused():
    exc = _refusal(_with_decls("type St = { a: Int }", "St"),
                   _with_decls("type St = { a: Int, b: Int }", "St", version=2))
    _assert_drift(exc)


def test_nominal_record_field_type_changed_is_refused():
    exc = _refusal(_with_decls("type St = { a: Int }", "St"),
                   _with_decls("type St = { a: Str }", "St", version=2))
    _assert_drift(exc)


def test_nominal_record_field_removed_is_refused():
    exc = _refusal(_with_decls("type St = { a: Int, b: Int }", "St"),
                   _with_decls("type St = { a: Int }", "St", version=2))
    _assert_drift(exc)


def test_nominal_record_secret_field_declassified_is_refused():
    exc = _refusal(_with_decls("type St = { t: Secret[Str] }", "St"),
                   _with_decls("type St = { t: Str }", "St", version=2))
    _assert_drift(exc)


def test_nested_nominal_record_changed_is_refused():
    state = "Map[Str, List[St]]"
    exc = _refusal(_with_decls("type St = { a: Int }", state),
                   _with_decls("type St = { a: Str }", state, version=2))
    _assert_drift(exc)


def test_nominal_record_reached_through_another_record_is_refused():
    # `Outer` is spelled identically and declared identically; the record it
    # holds is what changed
    outer = "type Outer = { inner: Inner }\n"
    exc = _refusal(
        _with_decls(outer + "type Inner = { a: Int }", "Outer"),
        _with_decls(outer + "type Inner = { a: Str }", "Outer", version=2))
    _assert_drift(exc)


def test_nominal_variant_payload_changed_is_refused():
    exc = _refusal(_with_decls("type Sh = Circle(Int) | Square", "Sh"),
                   _with_decls("type Sh = Circle(Str) | Square", "Sh", version=2))
    _assert_drift(exc)


def test_nominal_variant_case_added_is_refused():
    exc = _refusal(_with_decls("type Sh = Circle(Int) | Square", "Sh"),
                   _with_decls("type Sh = Circle(Int) | Square | Dot", "Sh",
                               version=2))
    _assert_drift(exc)


def test_generic_nominal_record_changed_is_refused():
    exc = _refusal(_with_decls("type Box[T] = { v: T }", "Box[Int]"),
                   _with_decls("type Box[T] = { v: T, n: Int }", "Box[Int]",
                               version=2))
    _assert_drift(exc)


def test_nominal_drift_refusal_names_both_declarations():
    exc = _refusal(_with_decls("type St = { a: Int }", "St"),
                   _with_decls("type St = { a: Int, b: Int }", "St", version=2))
    text = str(exc)
    assert "{a: Int, b: Int}" in text and "{a: Int}" in text
    accept = next(s for s in exc.why.steps if s.kind == "accept")
    export = next(s for s in exc.why.steps if s.kind == "export")
    assert "{a: Int, b: Int}" in accept.detail and "{a: Int}" in export.detail


def test_secret_in_a_written_map_is_refused_control():
    # already refused before the declared shapes were consulted
    exc = _refusal(_cache("Map[Str, Secret[Str]]"),
                   _cache("Map[Str, Str]", version=2))
    _assert_drift(exc)


def test_unrelated_changed_declaration_does_not_refuse():
    # `Other` changes but the hand-off never reaches it
    old = compile_source(_with_decls("type St = { a: Int }\ntype Other = { x: Int }",
                                     "St"), "c.rvl")
    new = _with_decls("type St = { a: Int }\ntype Other = { x: Str }", "St",
                      version=2)
    compile_source(new, "c.rvl", manifest=old, replacing=("Cache",))


def test_wildcard_acceptor_still_admits_a_changed_record():
    # `Any` accepts whatever is exported, whatever the record became
    old = compile_source(_with_decls("type St = { a: Int }", "St"), "c.rvl")
    compile_source(_cache("Any", version=2), "c.rvl",
                   manifest=old, replacing=("Cache",))


def test_migrate_plan_agrees_with_the_gate_on_a_changed_record():
    from revl.migrate import handoffs

    old = compile_source(_with_decls("type St = { a: Int }", "St"), "c.rvl")
    changed = compile_source(
        _with_decls("type St = { a: Int, b: Int }", "St", version=2), "c.rvl")
    same = compile_source(_with_decls("type St = { a: Int }", "St", version=2),
                          "c.rvl")
    assert [h["verdict"] for h in handoffs(old, changed)] == ["incompatible"]
    assert [h["verdict"] for h in handoffs(old, same)] == ["compatible"]


def test_nested_nominal_record_unchanged_is_admitted_through_a_json_round_trip():
    # the running manifest usually arrives as a parsed file, not the live dict;
    # the harness shape `Map[Str, List[Msg]]` is the real user of this path
    import json

    decl = "type Msg = { role: Str, text: Str }"
    state = "Map[Str, List[Msg]]"
    old = json.loads(json.dumps(compile_source(_with_decls(decl, state), "c.rvl")))
    compile_source(_with_decls(decl, state, version=2), "c.rvl",
                   manifest=old, replacing=("Cache",))
