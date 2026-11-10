"""Service replacement reads the declared types behind a signature.

A service signature spells a record by NAME, and the service-replacement gate
(`admission._service_compatible`) compared those spellings: `Rec` against `Rec`
is "equal" whatever `Rec` is declared to be. A running consumer's call sites
and a running provider were checked against the declaration the running
manifest carries, so a candidate that declares the same name over a changed
record (a field added, a field retyped, a `Secret` field made plain) was
admitted, with the service redeclared or, worst, without it, as a type-only
edit that never reached the service check.

The gate now resolves every name a running service signature reaches against
the running manifest's `types` and against the candidate's, and a name whose
declaration (or anything it reaches) differs is an interface drift, refused
with the existing refusal naming the running components it breaks and both
declarations. A service no running component touches, and a type no touched
service reaches, are not checked.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402

BREAKS = "service `Store` differs from the running manifest in a way that breaks"


def _prog(rec: str = "{ a: Int }", *, ret: str = "Rec", store: bool = True,
          decls: str = "", reader: bool = False, version: int = 1) -> str:
    """A cache provider behind `Store`, whose signature reaches `Rec`.
    `reader` adds the running consumer; `store=False` leaves the service
    undeclared, so the candidate is a type-only edit over a provider."""
    out = f"type Rec = {rec}\n{decls}\n"
    if store:
        out += (f"service Store {{\n  fn get(k: Str) -> {ret}\n"
                f"  fn put(k: Str, v: Rec)\n  fn version() -> Int\n}}\n")
    if reader:
        out += "service Look { fn read(k: Str) -> Int }\n"
    out += f"""component Cache provides cache: Store {{
  let m = effect Map.new() undo m.drop()
  provide cache {{
    fn get(k) = m.get(k)
    fn put(k, v) {{ effect m.insert(k, v) undo m.remove(k) }}
    fn version() = {version}
  }}
}}
"""
    if reader:
        out += ("component Reader requires cache: Store provides look: Look {\n"
                "  provide look { fn read(k) = cache.get(k).a }\n}\n")
    return out


def _running(rec: str = "{ a: Int }", **kw) -> dict:
    return compile_source(_prog(rec, reader=True, **kw), "c.rvl")


def _swap(old: dict, new_src: str, replacing=("Cache",)) -> dict:
    return compile_source(new_src, "c.rvl", manifest=old, replacing=replacing)


def _refused(old: dict, new_src: str, replacing=("Cache",)) -> RevlError:
    with pytest.raises(RevlError) as info:
        _swap(old, new_src, replacing)
    return info.value


def _assert_drift(exc: RevlError) -> None:
    assert BREAKS in str(exc)
    assert "`Reader`" in str(exc)
    assert exc.why is not None and exc.why.kind == "interface-drift"


# -- the rows that were admitted ----------------------------------------------

def test_record_gaining_a_field_is_refused():
    exc = _refused(_running(), _prog("{ a: Int, b: Int }", version=2))
    _assert_drift(exc)


def test_field_retyped_with_the_service_redeclared_is_refused():
    exc = _refused(_running(), _prog("{ a: Str }", version=2))
    _assert_drift(exc)


def test_field_retyped_with_the_service_not_redeclared_is_refused():
    # the worst case: the candidate never mentions `Store`, so only the
    # running service's signature can say a consumer is affected
    exc = _refused(_running(), _prog("{ a: Str }", store=False, version=2))
    _assert_drift(exc)


def test_type_only_edit_replacing_nothing_is_refused():
    exc = _refused(_running(), "type Rec = { a: Str }\n", replacing=())
    _assert_drift(exc)


def test_field_removed_is_refused():
    exc = _refused(_running("{ a: Int, b: Int }"), _prog("{ a: Int }", version=2))
    _assert_drift(exc)


def test_secret_field_made_plain_is_refused():
    exc = _refused(_running("{ a: Int, t: Secret[Str] }"),
                   _prog("{ a: Int, t: Str }", version=2))
    _assert_drift(exc)


def test_change_nested_under_another_record_is_refused():
    # `Rec` is spelled and declared as before; the `Inner` it reaches changed
    outer = "type Outer = { inner: Inner }\n"
    old = compile_source(
        _prog("{ a: Int, o: Outer }", reader=True,
              decls=outer + "type Inner = { x: Int }"), "c.rvl")
    exc = _refused(old, _prog("{ a: Int, o: Outer }", version=2,
                              decls=outer + "type Inner = { x: Str }"))
    _assert_drift(exc)
    assert "`Inner = {x: Int}`" in str(exc) and "`Inner = {x: Str}`" in str(exc)


def test_change_reached_through_a_map_in_the_signature_is_refused():
    def prog(field: str, version: int = 1, reader: bool = False) -> str:
        return (f"type Rec = {{ a: {field} }}\n"
                "service Store { fn all() -> Map[Str, Rec] }\n"
                "service Look { fn read() -> Int }\n"
                "component Cache provides cache: Store {\n"
                "  let m = effect Map.new() undo m.drop()\n"
                "  provide cache { fn all() = m }\n}\n"
                + ("component Reader requires cache: Store provides look: Look {\n"
                   "  provide look { fn read() = 1 }\n}\n" if reader else ""))

    old = compile_source(prog("Int", reader=True), "c.rvl")
    exc = _refused(old, prog("Str", version=2))
    _assert_drift(exc)


def test_refusal_names_both_declarations_and_the_method():
    exc = _refused(_running(), _prog("{ a: Int, b: Int }", version=2))
    text = str(exc)
    assert "`Rec = {a: Int}`" in text
    assert "`Rec = {a: Int, b: Int}`" in text
    assert "`get` uses `Rec`" in text
    step = exc.why.steps[0]
    assert "`Rec = {a: Int, b: Int}`" in step.detail


# -- controls -----------------------------------------------------------------

def test_signature_change_is_still_refused_control():
    exc = _refused(_running(), _prog(ret="Str", version=2))
    assert BREAKS in str(exc)
    assert "return widens" in str(exc)


def test_unchanged_record_is_admitted():
    _swap(_running(), _prog(version=2))


def test_unchanged_record_is_admitted_through_a_json_round_trip():
    import json

    old = json.loads(json.dumps(_running()))
    _swap(old, _prog(version=2))
    _swap(old, _prog(store=False, version=2))


def test_field_reorder_is_admitted():
    old = _running("{ a: Int, b: Str }")
    _swap(old, _prog("{ b: Str, a: Int }", version=2))


def test_an_unrelated_type_changing_is_admitted():
    old = _running(decls="type Other = { x: Int }")
    _swap(old, _prog(decls="type Other = { x: Str }", version=2))
    _swap(old, _prog(decls="type Other = { x: Str }", store=False, version=2))


def test_a_service_nothing_running_touches_is_not_checked():
    # `Aux` reaches `Spare`, which changes; no running component provides or
    # consumes `Aux`, so nothing can break
    decls = "service Aux { fn peek() -> Spare }"
    old = compile_source(_prog(reader=True, decls="type Spare = { x: Int }\n"
                               + decls), "c.rvl")
    _swap(old, _prog(decls="type Spare = { x: Str }\n" + decls, version=2))


def test_a_candidate_not_mentioning_the_type_is_admitted():
    # a new component beside the running ones, no `Rec` and no `Store`
    old = _running()
    extra = ("service Hello { fn hi() -> Int }\n"
             "component Hello provides hello: Hello { provide hello "
             "{ fn hi() = 1 } }\n")
    compile_source(extra, "c.rvl", manifest=old)


def test_a_running_manifest_without_types_refuses_a_declared_name():
    # fail closed, as the hand-off gate does: the running declaration cannot be
    # read, so a name the candidate declares cannot be proved the same
    old = _running()
    old.pop("types")
    exc = _refused(old, _prog(version=2))
    assert BREAKS in str(exc)


def test_widening_a_parameter_type_is_still_admitted_control():
    # the pre-existing compatible widening is unaffected by the type table
    base = """
service Store {{ fn bump(n: {t}) -> Int }}
service Look {{ fn read() -> Int }}
component Cache provides cache: Store {{ provide cache {{ fn bump(n) = 1 }} }}
{reader}"""
    reader = ("component Reader requires cache: Store provides look: Look "
              "{ provide look { fn read() = cache.bump(1) } }")
    old = compile_source(base.format(t="Int", reader=reader), "c.rvl")
    compile_source(base.format(t="Float", reader=""), "c.rvl",
                   manifest=old, replacing=("Cache",))


# -- a merged document carries the running declarations ------------------------
#
# The composition `revl apply` installs, and the document an admission against
# a manifest returns, both carry the running services. Each is the running
# manifest of a later admission, so each carries the declarations those
# signatures are spelled against: without them the next swap, changed or not,
# meets a running name it cannot read and is refused.

HELLO = ("service Hello { fn hi() -> Int }\n"
         "component Hello provides hello: Hello { provide hello "
         "{ fn hi() = 1 } }\n")


def _applied(old: dict, new_src: str, replacing=("Cache",)) -> dict:
    """The composition `revl apply` installs for this admission."""
    from revl.plan import plan

    out = plan(source=new_src, manifest=old, replacing=replacing,
               include_ir=True)
    return out["resultingIR"]


def test_the_applied_composition_keeps_the_running_declarations():
    old = _running()
    now = _applied(old, HELLO, replacing=())
    assert now["types"]["Rec"] == old["types"]["Rec"]


def test_a_swap_that_changes_nothing_is_admitted_after_an_apply():
    now = _applied(_running(), _prog(version=2))
    _swap(now, _prog(version=3))
    _swap(now, _prog(store=False, version=3))


def test_a_swap_that_changes_nothing_is_admitted_after_an_unrelated_apply():
    now = _applied(_running(), HELLO, replacing=())
    _swap(now, _prog(version=2))
    _swap(now, _prog(store=False, version=2))


def test_a_changed_record_is_refused_after_an_apply():
    now = _applied(_running(), _prog(version=2))
    exc = _refused(now, _prog("{ a: Int, b: Int }", version=3))
    _assert_drift(exc)
    assert "`Rec = {a: Int}`" in str(exc)
    assert "`Rec = {a: Int, b: Int}`" in str(exc)


def test_a_document_compiled_against_a_manifest_keeps_its_declarations():
    old = _running()
    now = compile_source(HELLO, "c.rvl", manifest=old)
    assert "Store" in now["services"]
    assert now["types"]["Rec"] == old["types"]["Rec"]
    # the candidate's own declaration of a running name is the one kept
    over = compile_source("type Other = { x: Str }\n" + HELLO, "c.rvl",
                          manifest=compile_source(
                              _prog(reader=True, decls="type Other = { x: Int }"),
                              "c.rvl"))
    assert over["types"]["Other"] == compile_source(
        "type Other = { x: Str }\n", "c.rvl")["types"]["Other"]


def test_a_program_with_no_manifest_and_no_types_carries_no_table():
    assert "types" not in compile_source(HELLO, "c.rvl")


# -- a turn admitted into a live session ---------------------------------------
#
# A session that admits a turn adopts its components and services. The
# declarations the turn's signatures are written against have to be adopted
# with them: otherwise the running manifest holds a service spelled over a name
# it does not declare, and the next turn that declares that name the same way
# cannot be proved unchanged.

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="a turn is admitted into a live cordis-py composition; install it "
           "with `sh backends/python/setup.sh`",
)

_T_BASE = ("service Ops { fn ping() -> Int }\n"
           "component Agent provides ops: Ops { provide ops { fn ping() = 1 } }\n")


def _turn(n: int, rec: str = "{ a: Int }", name: str = "Rec",
          body: str = "r.a") -> str:
    return (f"type {name} = {rec}\n"
            f"service Turn{n} {{ fn run(r: {name}) -> Int }}\n"
            f"component T{n} provides turn{n}: Turn{n} "
            f"{{ provide turn{n} {{ fn run(r) = {body} }} }}\n")


def _session(base: str = _T_BASE):
    import copy

    from revl.mcp.session import Session

    s = Session()
    s.load(copy.deepcopy(compile_source(base, "base.rvl")))
    return s


def _session_after_one_turn():
    s = _session()
    assert s.admit(_turn(1), granted=["Ops"], filename="t1.rvl").admitted
    return s


def _decl(source: str, name: str) -> dict:
    return compile_source(source, "c.rvl")["types"][name]


def _queue_turns(monkeypatch, s, *turns: str) -> None:
    """Admit `turns` the way a live call does: each decided against the
    composition as it stands, all of them wired when the call returns."""
    monkeypatch.setattr(s._loop, "is_running", lambda: True)
    for n, source in enumerate(turns, 1):
        verdict = s.admit(source, granted=["Ops"], filename=f"q{n}.rvl")
        assert verdict.admitted, verdict.message
    monkeypatch.undo()
    s._drain_pending_admits()


@needs_cordis
def test_a_session_adopts_the_declarations_of_an_admitted_turn():
    s = _session_after_one_turn()
    try:
        assert "Turn1" in s.ir["services"]
        assert s.ir["types"]["Rec"] == compile_source(
            "type Rec = { a: Int }\n", "c.rvl")["types"]["Rec"]
    finally:
        s.unload()


@needs_cordis
def test_a_second_turn_redeclaring_an_unchanged_record_is_admitted():
    s = _session_after_one_turn()
    try:
        verdict = s.admit(_turn(2), granted=["Ops"], filename="t2.rvl")
        assert verdict.admitted, verdict.message
    finally:
        s.unload()


@needs_cordis
def test_a_second_turn_changing_a_record_a_running_turn_uses_is_refused():
    s = _session_after_one_turn()
    try:
        verdict = s.admit(_turn(2, "{ a: Int, b: Int }"), granted=["Ops"],
                          filename="t2.rvl")
        assert not verdict.admitted
        assert "service `Turn1` differs from the running manifest" in verdict.message
        assert "the running manifest declares `Rec = {a: Int}`" in verdict.message
        assert "the candidate declares `Rec = {a: Int, b: Int}`" in verdict.message
    finally:
        s.unload()


@needs_cordis
def test_two_turns_queued_in_one_call_both_keep_their_declarations(monkeypatch):
    # the second turn's document never saw the first: wiring it adds its
    # declarations to the table and leaves the first turn's in it
    s = _session()
    try:
        _queue_turns(monkeypatch, s, _turn(1),
                     _turn(2, "{ x: Str }", name="Other", body="1"))
        assert {"Turn1", "Turn2"} <= set(s.ir["services"])
        assert s.ir["types"]["Rec"] == _decl("type Rec = { a: Int }\n", "Rec")
        assert s.ir["types"]["Other"] == _decl("type Other = { x: Str }\n",
                                               "Other")
    finally:
        s.unload()


@needs_cordis
def test_a_queued_turn_does_not_put_back_a_declaration_an_earlier_one_changed(
        monkeypatch):
    # nothing running reads `Cfg`, so the first turn may redeclare it; the
    # second was compiled before that and carries the old one in its document
    s = _session("type Cfg = { a: Int }\n" + _T_BASE)
    try:
        _queue_turns(monkeypatch, s,
                     _turn(1, "{ a: Int, b: Int }", name="Cfg"),
                     _turn(2, "{ x: Str }", name="Other", body="1"))
        assert s.ir["types"]["Cfg"] == _decl(
            "type Cfg = { a: Int, b: Int }\n", "Cfg")
        assert s.ir["types"]["Other"] == _decl("type Other = { x: Str }\n",
                                               "Other")
    finally:
        s.unload()


_CFG_BASE = "type Cfg = { a: Int, b: Int }\n" + _T_BASE
_CFG_NARROW = _turn(1, "{ a: Int }", name="Cfg")
_CFG_WIDE = _turn(2, "{ a: Int, b: Int }", name="Cfg", body="r.b")


@needs_cordis
def test_one_at_a_time_the_second_of_two_turns_that_disagree_is_refused():
    # nothing running reads `Cfg`, so the first turn may narrow it; the second
    # then spells the record `Turn1` no longer takes
    s = _session(_CFG_BASE)
    try:
        assert s.admit(_CFG_NARROW, granted=["Ops"], filename="t1.rvl").admitted
        verdict = s.admit(_CFG_WIDE, granted=["Ops"], filename="t2.rvl")
        assert not verdict.admitted
        assert "service `Turn1` differs from the running manifest" in verdict.message
    finally:
        s.unload()


@needs_cordis
@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "issue #3346: turns admitted during one call are each decided against the "
    "composition before any of them is wired, so they are not checked against "
    "each other"))
def test_two_queued_turns_that_disagree_on_a_declaration_do_not_both_run(
        monkeypatch):
    # the pair the test above refuses, admitted during one call: both are
    # wired, and `Turn2` then runs beside a `Cfg` it was not checked against
    s = _session(_CFG_BASE)
    try:
        monkeypatch.setattr(s._loop, "is_running", lambda: True)
        first = s.admit(_CFG_NARROW, granted=["Ops"], filename="q1.rvl")
        second = s.admit(_CFG_WIDE, granted=["Ops"], filename="q2.rvl")
        monkeypatch.undo()
        s._drain_pending_admits()
        assert first.admitted, first.message
        assert not (second.admitted and "Turn2" in s.ir["services"])
    finally:
        s.unload()


# -- `revl migrate` reports the same verdict ----------------------------------

def _migrate(old_src: str, new_src: str, against_src: str) -> dict:
    from revl.migrate import plan

    return plan(compile_source(old_src, "o.rvl"), compile_source(new_src, "n.rvl"),
                compile_source(against_src, "a.rvl"))


def test_migrate_plan_refuses_a_consumer_over_a_changed_record():
    result = _migrate(_prog(store=True), _prog("{ a: Int, b: Int }", version=2),
                      _prog(reader=True))
    assert result["surface"]["changed"], "the provision reads as reshaped"
    consumers = {c["component"]: c for c in result["consumers"]}
    assert consumers["Reader"]["verdict"] != "movable"
    assert consumers["Reader"]["difference"]["kind"] == "signature"
    assert "`Rec`" in consumers["Reader"]["difference"]["reason"]
    assert result["classification"] != "safe"


def test_migrate_plan_moves_a_consumer_over_an_unchanged_record():
    result = _migrate(_prog(), _prog(version=2), _prog(reader=True))
    consumers = {c["component"]: c for c in result["consumers"]}
    assert not result["surface"]["changed"]
    assert all(c["verdict"] == "movable" for c in consumers.values())


def test_migrate_plan_moves_a_consumer_over_a_reordered_record():
    result = _migrate(_prog("{ a: Int, b: Str }"),
                      _prog("{ b: Str, a: Int }", version=2),
                      _prog("{ a: Int, b: Str }", reader=True))
    assert not result["surface"]["changed"]
