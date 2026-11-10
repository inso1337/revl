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


# -- after `revl apply` --------------------------------------------------------
#
# The composition `revl apply` installs is the running manifest of the next
# swap, so it keeps the declarations a running hand-off is spelled against.

def _applied(old: dict, new_src: str) -> dict:
    from revl.plan import plan

    out = plan(source=new_src, manifest=old, replacing=("Cache",),
               include_ir=True)
    return out["resultingIR"]


def test_nominal_record_unchanged_is_admitted_after_an_apply():
    decl = "type St = { a: Int }"
    old = compile_source(_with_decls(decl, "St"), "c.rvl")
    now = _applied(old, _with_decls(decl, "St", version=2))
    assert now["types"]["St"] == old["types"]["St"]
    compile_source(_with_decls(decl, "St", version=3), "c.rvl",
                   manifest=now, replacing=("Cache",))


def test_nominal_record_field_added_is_refused_after_an_apply():
    decl = "type St = { a: Int }"
    now = _applied(compile_source(_with_decls(decl, "St"), "c.rvl"),
                   _with_decls(decl, "St", version=2))
    with pytest.raises(RevlError) as info:
        compile_source(_with_decls("type St = { a: Int, b: Int }", "St",
                                   version=3), "c.rvl",
                       manifest=now, replacing=("Cache",))
    _assert_drift(info.value)
    assert "`St = {a: Int}`" in str(info.value)
    assert "`St = {a: Int, b: Int}`" in str(info.value)


# -- a hand-off the admission KEEPS -------------------------------------------
#
# The replacement check above reads a candidate that accepts the state. A
# candidate that leaves the stateful provider alone and declares the provider's
# type name over another record never reaches it, and the document it produces
# carries the candidate's declaration: the kept provider's `handoff` would then
# be spelled against a record it was never checked against, and a later swap to
# that record would read as unchanged. So a name a kept hand-off reaches must
# stay declared as the running manifest declares it.

_HELLO = ("service Hello { fn hi() -> Int }\n"
          "component Hello provides hello: Hello { provide hello "
          "{ fn hi() = 1 } }\n")
_KEPT = ("state hand-off on `cache` differs from the running manifest: "
         "`Cache` is kept and exports `St`")


def _kept_refusal(old: dict, new_src: str, replacing=()):
    with pytest.raises(RevlError) as info:
        compile_source(new_src, "c.rvl", manifest=old, replacing=replacing)
    return info.value


def test_kept_handoff_redeclared_differently_is_refused():
    old = compile_source(_with_decls("type St = { a: Int }", "St"), "c.rvl")
    exc = _kept_refusal(old, "type St = { a: Int, b: Int }\n" + _HELLO)
    assert _KEPT in str(exc), str(exc)
    assert "`St = {a: Int}`" in str(exc) and "`St = {a: Int, b: Int}`" in str(exc)
    assert exc.why is not None and exc.why.kind == "state-handoff-drift"
    assert {step.kind for step in exc.why.steps} == {"export", "redeclare"}
    redeclare = next(s for s in exc.why.steps if s.kind == "redeclare")
    assert (redeclare.name, redeclare.file, redeclare.line) == ("St", "c.rvl", 1)


def test_kept_handoff_redeclared_differently_is_refused_by_plan():
    from revl.plan import plan

    old = compile_source(_with_decls("type St = { a: Int }", "St"), "c.rvl")
    out = plan(source="type St = { a: Int, b: Int }\n" + _HELLO, manifest=old,
               include_ir=True)
    assert out["admissible"] is False and "resultingIR" not in out
    assert any(_KEPT in d["message"] for d in out["diagnostics"]), out


def test_kept_handoff_cannot_be_moved_under_a_later_swap():
    # the sequence the check closes: an unrelated admission may not change
    # what `St` means under the kept `Cache`, so the composition that stands
    # after it still declares `St` as before and the next swap of `Cache` is
    # compared against the record its state was built as
    from revl.plan import plan

    old = compile_source(_with_decls("type St = { a: Int }", "St"), "c.rvl")
    _kept_refusal(old, "type St = { a: Int, b: Int }\n" + _HELLO)
    now = plan(source=_HELLO, manifest=old, include_ir=True)["resultingIR"]
    assert {c["name"] for c in now["components"]} == {"Cache", "Hello"}
    assert now["types"]["St"] == old["types"]["St"]
    exc = _kept_refusal(
        now, _with_decls("type St = { a: Int, b: Int }", "St", version=2),
        replacing=("Cache",))
    _assert_drift(exc)


def test_kept_handoff_nested_change_is_refused():
    old = compile_source(
        _with_decls("type Inner = { x: Int }\ntype St = { i: Inner }",
                    "Map[Str, List[St]]"), "c.rvl")
    exc = _kept_refusal(old, "type Inner = { x: Str }\n" + _HELLO)
    assert ("`Cache` is kept and exports `Map[Str, List[St]]` "
            "(`Inner = {x: Int}`)") in str(exc), str(exc)
    assert "(`Inner = {x: Str}`)" in str(exc)


def test_kept_handoff_identical_redeclaration_is_admitted():
    decl = "type St = { a: Int }"
    old = compile_source(_with_decls(decl, "St"), "c.rvl")
    now = compile_source(decl + "\n" + _HELLO, "c.rvl", manifest=old)
    assert now["types"]["St"] == old["types"]["St"]


def test_kept_handoff_unrelated_declaration_is_admitted():
    old = compile_source(_with_decls("type St = { a: Int }\ntype Other = { x: Int }",
                                     "St"), "c.rvl")
    compile_source("type Other = { x: Str }\ntype New = { n: Int }\n" + _HELLO,
                   "c.rvl", manifest=old)


def test_withdrawn_handoff_is_not_held_to_the_running_declaration():
    # `Cache` leaves in this admission, replaced by a stateless successor or
    # withdrawn outright, so nothing kept is spelled against `St` any more
    old = compile_source(_with_decls("type St = { a: Int }", "St"), "c.rvl")
    stateless = _cache(version=2).replace("  handoff cache: Map[Str, Str]\n", "")
    compile_source("type St = { a: Int, b: Int }\n" + stateless, "c.rvl",
                   manifest=old)
    compile_source("type St = { a: Int, b: Int }\n" + _HELLO, "c.rvl",
                   manifest=old, replacing=("Cache",))


def test_a_provided_type_is_not_a_redeclaration():
    # `Answer[T]` is the language's own and no program can declare it. A
    # program that names it applied gets the declaration seeded into its table
    # and one that only hands it off does not: that is the same type on both
    # sides, kept or replaced.
    old = compile_source(_with_decls("", "Answer[Int]"), "c.rvl")
    assert "Answer" not in (old.get("types") or {})
    compile_source("type Q = { a: Answer[Int] }\n" + _HELLO, "c.rvl",
                   manifest=old)
    compile_source(_with_decls("type Q = { a: Answer[Int] }", "Answer[Int]",
                               version=2), "c.rvl",
                   manifest=old, replacing=("Cache",))


def test_a_record_under_a_provided_type_is_still_compared():
    old = compile_source(_with_decls("type St = { a: Int }", "Answer[St]"),
                         "c.rvl")
    exc = _kept_refusal(old, "type St = { a: Int, b: Int }\n" + _HELLO)
    assert "`Cache` is kept and exports `Answer[St]`" in str(exc), str(exc)
    assert "`St = {a: Int}`" in str(exc) and "`St = {a: Int, b: Int}`" in str(exc)
    assert "Answer[T]" not in str(exc)


def test_kept_handoff_without_running_types_fails_closed():
    import copy

    decl = "type St = { a: Int }"
    old = copy.deepcopy(compile_source(_with_decls(decl, "St"), "c.rvl"))
    old.pop("types")
    exc = _kept_refusal(old, decl + "\n" + _HELLO)
    assert _KEPT in str(exc) and "`St` is not declared" in str(exc)
    # a candidate that says nothing about `St` leaves its meaning alone
    compile_source(_HELLO, "c.rvl", manifest=old)


# -- the same declared-shape check on the agent-authored swap path -------------
#
# `Session._enforce_declared_disclosure` (before teardown) and
# `Session._restore_provider_state` (as the state is threaded) each carried a
# spelling-only `compatible(accepted, exported)` of their own, so a changed
# record under an unchanged name crossed there although admission refused it.

def _session_stub(**driver):
    from types import SimpleNamespace

    from revl.mcp import session as session_mod

    return session_mod, SimpleNamespace(
        _driver=SimpleNamespace(
            runtime=SimpleNamespace(live_instances=lambda name: [],
                                    StateIncompatible=RuntimeError),
            fibers={}, **driver),
        _frame_resources=lambda fiber: [],
        _declared_handoffs=session_mod.Session._declared_handoffs)


def test_session_disclosure_gate_refuses_a_changed_record_under_one_name():
    session_mod, stub = _session_stub()
    old = compile_source(_with_decls("type St = { t: Secret[Str] }", "St"), "c.rvl")
    new = compile_source(_with_decls("type St = { t: Str }", "St", version=2),
                         "c.rvl")
    with pytest.raises(session_mod.SessionError) as caught:
        session_mod.Session._enforce_declared_disclosure(stub, old, new)
    assert caught.value.code == "STATE_UNDISCLOSED"


def test_session_disclosure_gate_admits_an_unchanged_record():
    session_mod, stub = _session_stub()
    decl = "type St = { t: Secret[Str] }"
    old = compile_source(_with_decls(decl, "St"), "c.rvl")
    new = compile_source(_with_decls(decl, "St", version=2), "c.rvl")
    session_mod.Session._enforce_declared_disclosure(stub, old, new)


def test_session_restore_refuses_a_changed_record_under_one_name():
    session_mod, stub = _session_stub()
    old = compile_source(_with_decls("type St = { a: Int }", "St"), "c.rvl")
    new = compile_source(
        _with_decls("type St = { a: Int, b: Int }", "St", version=2), "c.rvl")
    pre = session_mod.Session._capture_provider_state(stub, old)
    assert pre
    with pytest.raises(RuntimeError, match="not §5-compatible"):
        session_mod.Session._restore_provider_state(stub, new, pre)


def test_session_restore_admits_an_unchanged_record():
    session_mod, stub = _session_stub()
    decl = "type St = { a: Int }"
    old = compile_source(_with_decls(decl, "St"), "c.rvl")
    new = compile_source(_with_decls(decl, "St", version=2), "c.rvl")
    pre = session_mod.Session._capture_provider_state(stub, old)
    assert session_mod.Session._restore_provider_state(stub, new, pre)


# -- what counts as the same declaration --------------------------------------

def test_renamed_type_parameter_is_the_same_declaration():
    old = compile_source(_with_decls("type Box[T] = { v: T }", "Box[Int]"), "c.rvl")
    compile_source(_with_decls("type Box[U] = { v: U }", "Box[Int]", version=2),
                   "c.rvl", manifest=old, replacing=("Cache",))


def test_type_parameter_swap_is_a_different_declaration():
    # positional, not by name: `{a: T, b: U}` read with its parameters swapped
    # is another record
    old = compile_source(
        _with_decls("type P[T, U] = { a: T, b: U }", "P[Int, Str]"), "c.rvl")
    new = _with_decls("type P[U, T] = { a: T, b: U }", "P[Int, Str]", version=2)
    _assert_drift(_refusal(_with_decls("type P[T, U] = { a: T, b: U }",
                                       "P[Int, Str]"), new))
    assert old


def test_variant_case_reorder_is_refused():
    # case order is observable: the WIT/canonical-ABI export numbers a variant's
    # cases by position, and the JSON schema lists them in order
    exc = _refusal(_with_decls("type Sh = Circle(Int) | Square", "Sh"),
                   _with_decls("type Sh = Square | Circle(Int)", "Sh", version=2))
    _assert_drift(exc)


def test_nominal_drift_hint_does_not_say_widen():
    exc = _refusal(_with_decls("type St = { a: Int }", "St"),
                   _with_decls("type St = { a: Int, b: Int }", "St", version=2))
    assert "widen" not in exc.hint and "declaration" in exc.hint


def test_missing_running_types_refuses_even_an_unchanged_declaration():
    # fail closed: a running document that carries no `types` cannot say what
    # `St` was, so the gate cannot prove it is the same
    import copy

    decl = "type St = { a: Int }"
    old = copy.deepcopy(compile_source(_with_decls(decl, "St"), "c.rvl"))
    old.pop("types")
    with pytest.raises(RevlError) as info:
        compile_source(_with_decls(decl, "St", version=2), "c.rvl",
                       manifest=old, replacing=("Cache",))
    _assert_drift(info.value)
