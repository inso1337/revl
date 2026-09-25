"""Roadmap item 470 / issue #822, slice 3: the checker-visible refinement rule
(docs/design/470-intent-refinement.md §4 stage 1).

Slices 1-2 landed the semantic kernel (`revl.intent`) and deliberately wired it
to nothing, so `tests/test_470_intent_refinement.py` exercises `refine` over
hand-built records. This file covers the SURFACE that finally holds a
declaration across time and compares a real crossing against it:

  * `within { … }` on a service operation is the intent it DECLARES;
  * `acting { … }` on an `emit` step is what one crossing actually DOES;
  * the object and the amount are read off the crossing's own capability
    spelling rather than restated in `acting`, so the two sides cannot drift.

The file is organised as the roadmap's exit criterion reads it. Every dimension
it names (a wider verb, a higher amount, a different tenant, an extra
capability) has a case that is refused BY NAME, with the declared value in the
message; the object-cone and set-valued-scope clauses the issue adds have one
each; and two controls prove the rule is not vacuous — a legitimate refinement
still compiles, and a program that writes neither clause lowers to byte-identical
IR.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.parser import EmitStmt, LetStmt, Parser  # noqa: E402

# One shape throughout: a provider whose operation declares an intent, and whose
# body performs exactly one crossing. `cap` is what the crossed operation is
# scoped to (the action's object and amount), `within` is the declaration, and
# `acting` is what the crossing states about itself.
PROGRAM = """
service Store {{ emission[{cap}] fn ingest(row: Str) -> Int }}
service Worker {{ emission fn run() -> Str{within} }}
component W requires fs: Store provides worker: Worker {{
  provide worker {{ fn run() {{ emit fs.ingest("row"){acting} return "k" }} }}
}}
"""

TMP_WRITE = 'fs.write(path="/tmp/out")'
DECLARE_TMP = ' within { object: fs.write(path="/tmp"), verbs: [ingest] }'
ACT_INGEST = ' acting { verb: ingest }'


def source(cap=TMP_WRITE, within=DECLARE_TMP, acting=ACT_INGEST):
    return PROGRAM.format(cap=cap, within=within, acting=acting)


def refused(**kwargs) -> str:
    with pytest.raises(RevlError) as exc:
        compile_source(source(**kwargs), "<test>")
    return str(exc.value)


# ------------------------------------------------------- the controls
#
# Both of these would pass against a build that never ran the check at all, so
# they are not evidence on their own — they are what makes the refusals below
# evidence, by pinning that the rule admits the legitimate program and costs the
# unannotated one nothing.


def test_a_legitimate_refinement_still_compiles():
    # `/tmp/out` is inside the declared `/tmp` cone, `ingest` is a declared
    # verb, no ceiling is stated and none is spent, no tenant is declared.
    assert compile_source(source(), "<test>")


def test_no_clause_is_byte_identical_ir():
    """The claim the two clauses are designed around: a method and an emit that
    write neither one lower exactly as they did before the clauses existed.

    Expressed as the equality that can actually be checked from inside the
    tree — the annotated program and the same program with both clauses removed
    produce the same IR — so the declaration provably contributes zero IR bytes
    and the check it arms lives entirely in lower.
    """
    plain = compile_source(source(within="", acting=""), "<test>")
    annotated = compile_source(source(), "<test>")
    assert json.dumps(plain, sort_keys=True) == json.dumps(annotated, sort_keys=True)


def test_absent_clauses_are_the_ast_default():
    program = Parser(source(within="", acting=""), "<test>").parse()
    assert program.services[1].methods["run"].within is None
    body = program.components[0].body[0].methods[0].body
    assert [stmt.acting for stmt in body if isinstance(stmt, EmitStmt)] == [None]


def test_positional_emitstmt_construction_is_unchanged():
    # `acting` is kept last, so the four-positional construction every existing
    # caller uses still builds a clause-free emit.
    assert EmitStmt("expr", 1, None, None).acting is None


# ------------------------------------- the four dimensions the roadmap names


def test_a_wider_verb_is_refused_with_the_declared_verbs():
    message = refused(
        within=' within { object: fs.write(path="/tmp"), verbs: [read] }')
    assert "exceeds the intent `Worker.run` declares" in message
    assert "performs `ingest`" in message
    assert "the declared intent does not permit there" in message


def test_a_higher_amount_is_refused_with_the_declared_ceiling():
    message = refused(
        cap='fs.write(path="/tmp/out", calls=5)',
        within=' within { object: fs.write(path="/tmp"), verbs: [ingest],'
               ' ceilings: { calls: 1 } }')
    assert "spends `calls=5`, above the declared ceiling `calls=1`" in message


def test_a_different_tenant_is_refused_with_the_declared_tenant():
    message = refused(
        within=' within { object: fs.write(path="/tmp"), verbs: [ingest],'
               ' tenant: "eu" }',
        acting=' acting { verb: ingest, tenant: "us" }')
    assert "runs in tenant `us`" in message
    assert "confined to tenant `eu`" in message


def test_an_extra_capability_is_refused_naming_the_declared_objects():
    message = refused(cap="db")
    assert "reaches `db`, a capability the declared intent does not name" in message
    assert 'it names `fs.write(path="/tmp")`' in message


# ------------------------------------- the two clauses the issue adds


def test_the_same_object_outside_the_declared_cone_is_refused():
    # the same token, a sibling path: `covers` refuses it, and the refusal says
    # which of the two object findings it is.
    message = refused(cap='fs.write(path="/etc/passwd")')
    assert 'reaches `fs.write(path="/etc/passwd")`' in message
    assert "outside the scope the declared intent authorizes on `fs.write`" in message


def test_an_explicitly_related_object_is_admitted():
    # relatedness is a declaration and never an inference: `db` is admitted only
    # because the intent named it in `related`.
    assert compile_source(source(
        cap="db",
        within=' within { object: fs.write(path="/tmp"), related: [db],'
               ' verbs: [ingest] }'), "<test>")


def test_a_broader_scope_is_refused_with_the_declared_members():
    message = refused(
        within=' within { object: fs.write(path="/tmp"), verbs: [ingest],'
               ' scopes: { recipients: [alice] } }',
        acting=' acting { verb: ingest,'
               ' scopes: { recipients: [alice, mallory] } }')
    assert "reaches `recipients={mallory}` outside the declared scope" in message
    assert "`recipients={alice}`" in message


# ------------------------------------- the two omissions, both directions


def test_a_crossing_under_a_declaration_must_state_what_it_does():
    """The dangerous direction: without this demand a body could declare an
    intent and then reach anything at all through an unannotated `emit`, which
    would make every declaration in the tree vacuous."""
    message = refused(acting="")
    assert "declares an intent (`within` at line 3)" in message
    assert "must state what it does with `acting { … }`" in message


def test_an_action_with_no_declaration_is_refused_not_inferred():
    """Item 470's own scope note: nothing infers an intent the caller never
    stated, so an `acting` clause with nothing to check against is refused
    rather than silently passing."""
    message = refused(within="")
    assert "declares no `within { … }` intent for it to refine" in message


def test_a_bare_emission_cannot_refine_a_declaration():
    """A bare `emission` operation names no capability, so no declared object
    can be shown to cover it. Fail closed rather than read the unnameable as the
    declared one."""
    bare = PROGRAM.format(cap=TMP_WRITE, within=DECLARE_TMP, acting=ACT_INGEST)
    bare = bare.replace("emission[" + TMP_WRITE + "]", "emission")
    with pytest.raises(RevlError) as exc:
        compile_source(bare, "<test>")
    assert "crosses an unnameable boundary" in str(exc.value)


def test_a_crossing_with_no_resolvable_capability_is_refused():
    """The same rule for the shapes whose boundary set the per-crossing
    resolution cannot pin down at all (here, a crossing through a service-typed
    parameter). An empty capability set is "nothing to compare", not "nothing to
    check": treating it as the latter is the silent hole."""
    src = (
        'service Store { emission[db] fn ingest(row: Str) -> Int }\n'
        'service Worker { emission fn run(s: Store) -> Str'
        ' within { object: db, verbs: [ingest] } }\n'
        'component W provides worker: Worker {\n'
        '  provide worker { fn run(s: Store) { emit s.ingest("row")'
        ' acting { verb: ingest } return "k" } }\n'
        '}\n'
    )
    with pytest.raises(RevlError) as exc:
        compile_source(src, "<test>")
    assert "crosses an unnameable boundary" in str(exc.value)
    assert "authorizes `db`" in str(exc.value)


# ------------------------------------- the clause grammar


def _within(fields: str):
    src = "service W { emission fn run() -> Str within { " + fields + " } }"
    return Parser(src, "<test>").parse().services[0].methods["run"].within


@pytest.mark.parametrize("fields,needle", [
    ("verbs: [a]", "must state `object`"),
    ("object: db", "must state `verbs`"),
    ("object: db, verbs: [a], bogus: 1", "`bogus` is not a `within` clause field"),
    ("object: db, object: bus, verbs: [a]", "duplicate `within` field `object`"),
    ("object: db, verbs: [a, a]", "duplicate verb `a` in `verbs`"),
    ("object: db, verbs: a", "expected `[` before a verb list"),
    # the ceiling is ONE bound: a resource parameter may not name one, and a
    # ceiling spelled on both the object and in `ceilings` is refused rather
    # than merged.
    ("object: db, verbs: [a], ceilings: { path: 3 }", "is not a ceiling parameter"),
    ("object: db(calls=3), verbs: [a], ceilings: { calls: 1 }",
     "stated twice"),
    # a scope may not restate a fact the object or ceiling dimension bounds.
    ("object: db, verbs: [a], scopes: { path: [x] }",
     "cannot name a scope"),
    # a per-object ceiling would be a second ceiling comparison.
    ("object: db, verbs: [a], related: [bus(calls=2)]",
     "an intent states one ceiling"),
])
def test_within_clause_refusals(fields, needle):
    with pytest.raises(RevlError) as exc:
        _within(fields)
    assert needle in str(exc.value)


def test_the_ceiling_may_be_written_either_way():
    """`object: db(calls=3)` and `ceilings: { calls: 3 }` are one declaration
    written two ways, and `Intent.from_cap`'s split is what makes them equal."""
    assert (_within("object: db(calls=3), verbs: [a]").intent
            == _within("object: db, verbs: [a], ceilings: { calls: 3 }").intent)


def test_related_and_scopes_and_tenant_reach_the_intent():
    declared = _within(
        'object: db, related: [bus], verbs: [a, b], tenant: prod.eu,'
        ' scopes: { recipients: [alice, "bob"] }').intent
    assert [cap.to_str() for cap in declared.objects()] == ["db", "bus"]
    assert declared.verbs == frozenset({"a", "b"})
    assert declared.tenant == "prod.eu"
    assert declared.scope_map() == {"recipients": frozenset({"alice", "bob"})}


def test_an_acting_clause_must_state_its_verb():
    with pytest.raises(RevlError) as exc:
        compile_source(source(acting=' acting { tenant: "eu" }'), "<test>")
    assert "must state `verb`" in str(exc.value)


def test_within_and_acting_are_not_reserved_words():
    """Both clauses are recognised only in the one slot they occupy, so the
    lexer's KEYWORDS set is untouched and a program using either name as an
    ordinary identifier is unaffected."""
    src = (
        "fn within(acting: Int) -> Int { let within = acting return within }\n"
        'test "t" { assert within(3) == 3 }\n'
    )
    assert compile_source(src, "<test>")


# --------------- the declaration bounds the OPERATION, not its `emit` steps
#
# `_check_intent_refinement` closes one hole per crossing: an `emit` step under
# a declaration must state what it does. That left the declaration vacuous
# through every OTHER spelling of a crossing, and the tree has three:
#
#   * a direct call to an `emission` extern, which needs no `emit` marker;
#   * a value-position emission (`let r = emit ...`), whose marker sits inside
#     an expression where no clause can trail it;
#   * a helper `fn` that reaches an emission one hop down.
#
# Each of them let a body declare `within { object: fs.write(path="/tmp") }`
# and then reach `/etc` with no refusal at all. The rule is therefore stated
# over the whole body: under a declaration, the only crossing spelling admitted
# is an `emit` step that states itself, and whatever the body still reaches once
# those are removed is a crossing that states nothing.

# A second shape, because these spellings need a host `emission` extern rather
# than a required service. Since issue #1437 a host emission carries the `emit`
# marker in every program too, declaration or not, so the unstated spellings
# below are the MARKED ones that still state nothing: a value-position `emit`
# and a marked call to a helper that reaches the extern. The bare call is
# refused for its missing marker before any declaration is consulted
# (`test_a_bare_call_is_refused_for_its_marker_with_or_without_a_declaration`).
EXTERN_PROGRAM = """
extern emission[fs.write(path="/tmp/out")] fn wr(msg: Str) -> Int = @py {{
    return 1
}}
fn helper(msg: Str) -> Int {{ return wr(msg) }}
service Worker {{ emission fn run() -> Str{within} }}
component W provides worker: Worker {{
  provide worker {{ fn run() {{ {body} return "k" }} }}
}}
"""

STATED = 'emit wr("row") acting { verb: ingest }'
UNSTATED = {
    "a value-position emission": 'let n = emit wr("row")',
    "a marked call to a helper that reaches an emission": 'let n = emit helper("row")',
}


def extern_source(body, within=DECLARE_TMP):
    return EXTERN_PROGRAM.format(within=within, body=body)


@pytest.mark.parametrize("body", list(UNSTATED.values()), ids=list(UNSTATED))
def test_an_unstated_crossing_under_a_declaration_is_refused(body):
    with pytest.raises(RevlError) as exc:
        compile_source(extern_source(body), "<test>")
    message = str(exc.value)
    assert "`Worker.run` declares an intent (`within` at line 6)" in message
    assert "through a crossing that states nothing about itself" in message
    # the refusal names the intent it violated, which is the roadmap's exit
    # criterion and the whole reason the kernel holds a declaration.
    assert 'authorizes `fs.write(path="/tmp")`' in message


def test_an_unstated_crossing_is_refused_even_inside_the_declared_cone():
    """The failure direction, stated exactly: the refusal is not "the object is
    outside the cone", it is "this crossing cannot be SHOWN to refine the
    declaration". `fs.write(path="/tmp/out")` is inside the declared `/tmp`
    cone and the same crossing spelled as an annotated `emit` compiles, so the
    only difference is that this spelling states nothing."""
    with pytest.raises(RevlError) as exc:
        compile_source(extern_source('let n = emit wr("row")'), "<test>")
    assert "states nothing about itself" in str(exc.value)
    assert compile_source(extern_source(STATED), "<test>")


@pytest.mark.parametrize("within", [DECLARE_TMP, ""], ids=["declared", "undeclared"])
def test_a_bare_call_is_refused_for_its_marker_with_or_without_a_declaration(within):
    """A bare call to an `emission` extern is an unmarked crossing, refused by
    the marker rule (issue #1437) whether or not an intent is declared. It is
    therefore no longer one of the spellings the completeness check has to
    catch: it never reaches it."""
    with pytest.raises(RevlError) as exc:
        compile_source(extern_source('let n = wr("row")', within=within), "<test>")
    assert "call to emission `wr` must be marked `emit` (G4)" in str(exc.value)


@pytest.mark.parametrize("body", list(UNSTATED.values()), ids=list(UNSTATED))
def test_every_crossing_spelling_still_compiles_with_no_declaration(body):
    """The non-vacuity control, and the one that keeps the rule honest: none of
    these spellings is refused on its own. Each is refused only because an
    operation declared an intent it cannot be checked against."""
    assert compile_source(extern_source(body, within=""), "<test>")


def test_the_annotated_emit_is_the_admitted_spelling():
    """The other half of the control: the declaration admits the crossing that
    states itself, so the rule bounds the spelling and not the boundary."""
    assert compile_source(extern_source(STATED), "<test>")


def test_the_completeness_check_contributes_no_ir():
    """The property #1007 pinned, re-proved for this shape: the declaration and
    the clause it arms lower to nothing, so a program that writes neither is
    byte-identical to the annotated one."""
    plain = compile_source(extern_source('emit wr("row")', within=""), "<test>")
    annotated = compile_source(extern_source(STATED), "<test>")
    assert json.dumps(plain, sort_keys=True) == json.dumps(annotated, sort_keys=True)


# ----------------- the stating slot for a VALUE-RETURNING crossing
#
# Stage 0 of the design note's section 4, and the hole the completeness half
# above opened. An `emit` STEP discards the value it produces, so an emission
# that RETURNS data is written `let r = emit svc.op(...)` — and there the
# marker sits inside an expression, where no clause can trail it. Under a
# declaration that spelling was refused with no rewrite available other than
# throwing the returned value away.
#
# The binding STATEMENT is the slot that can carry the clause. Once it does,
# the check is the per-crossing one the `emit` step already runs: the verb, the
# tenant and the scopes come off `acting`, while the object and the amount are
# read off the crossing's own capability spelling, so the two sides still
# cannot drift by being written twice.

BOUND = 'let n = emit wr("row") acting { verb: ingest }'


def test_a_value_returning_crossing_can_state_itself():
    """The hole, closed: the binding compiles AND keeps its value, which is the
    whole point — `n` is in scope afterwards."""
    assert compile_source(
        extern_source('let n = emit wr("row") acting { verb: ingest }'
                      ' let m = n + 1'), "<test>")


def test_the_same_binding_without_the_clause_is_still_refused():
    """The control that makes the case above evidence rather than a build with
    the check switched off: the only difference between the two programs is the
    clause, and the unstated one is refused by the completeness half."""
    with pytest.raises(RevlError) as exc:
        compile_source(extern_source('let n = emit wr("row")'), "<test>")
    assert "through a crossing that states nothing about itself" in str(exc.value)


def test_a_bound_crossing_carries_the_service_shape_too():
    """The design note's own spelling of the hole, `let r = emit svc.op(...)`:
    the slot is the binding statement, not the extern call, so a required
    service's returning emission takes it as well."""
    assert compile_source(source(
        acting="", within=DECLARE_TMP).replace(
            'emit fs.ingest("row")',
            'let n = emit fs.ingest("row") acting { verb: ingest }'), "<test>")


@pytest.mark.parametrize("acting,needle", [
    (' acting { verb: read }', "does not permit"),
    (' acting { verb: ingest, tenant: "us" }', "runs in tenant `us`"),
])
def test_a_bound_crossing_outside_the_declaration_is_refused(acting, needle):
    """Every dimension the step slot checks, the binding slot checks: the
    clause is the same record and the object and amount still come off the
    crossing's own spelling."""
    declare = (' within { object: fs.write(path="/tmp"), verbs: [ingest],'
               ' tenant: "eu" }')
    with pytest.raises(RevlError) as exc:
        compile_source(
            extern_source('let n = emit wr("row")' + acting, within=declare),
            "<test>")
    message = str(exc.value)
    assert "this `let` exceeds the intent `Worker.run` declares" in message
    assert needle in message


def test_a_bound_crossing_outside_the_declared_cone_is_refused():
    """The object dimension, read off the crossing's own spelling rather than
    restated in the clause: `/etc` is not under the declared `/tmp`."""
    src = EXTERN_PROGRAM.format(within=DECLARE_TMP, body=BOUND).replace(
        '/tmp/out', '/etc/passwd')
    with pytest.raises(RevlError) as exc:
        compile_source(src, "<test>")
    assert 'reaches `fs.write(path="/etc/passwd")`' in str(exc.value)


def test_a_bound_crossing_with_no_declaration_is_refused_not_inferred():
    """Item 470's scope note on the new slot: a clause with nothing to check it
    against is refused rather than read as its own permission."""
    with pytest.raises(RevlError) as exc:
        compile_source(extern_source(BOUND, within=""), "<test>")
    assert "this `let` states `acting { … }`" in str(exc.value)
    assert "declares no `within { … }` intent for it to refine" in str(exc.value)


def test_a_bound_crossing_that_names_no_boundary_is_refused():
    """Fail closed on the unnameable, on this slot too: a bare `emission`
    operation names no capability, so no declared object can be SHOWN to cover
    it."""
    src = (
        'service Store { emission fn ingest(row: Str) -> Int }\n'
        'service Worker { emission fn run() -> Str' + DECLARE_TMP + ' }\n'
        'component W requires fs: Store provides worker: Worker {\n'
        '  provide worker { fn run() { let n = emit fs.ingest("row")'
        ' acting { verb: ingest } return "k" } }\n'
        '}\n'
    )
    with pytest.raises(RevlError) as exc:
        compile_source(src, "<test>")
    assert "this `let` crosses an unnameable boundary" in str(exc.value)


def test_a_clause_on_an_unmarked_value_is_refused():
    """The marker stays load-bearing. A clause on a value that carries no
    `emit` would state something about a crossing that is not there while
    leaving the real one — a bare call to an `emission` extern — unstated, so
    the honest spellings stay `let n = emit wr(...) acting { ... }` and
    `emit wr(...) acting { ... }`. Since issue #1437 the bare call is refused
    for its own missing marker, before the clause is judged."""
    with pytest.raises(RevlError) as exc:
        compile_source(
            extern_source('let n = wr("row") acting { verb: ingest }'), "<test>")
    assert "call to emission `wr` must be marked `emit` (G4)" in str(exc.value)


def test_a_clause_on_a_helper_hop_is_refused():
    """The helper hop keeps its verdict. One clause cannot state a crossing the
    binding does not perform, so the transitive reach stays the completeness
    check's business. Unmarked, the call is refused for its marker (issue
    #1437); marked, the clause is refused because the hop names no boundary."""
    with pytest.raises(RevlError) as exc:
        compile_source(
            extern_source('let n = helper("row") acting { verb: ingest }'),
            "<test>")
    assert "call to emission `helper` must be marked `emit` (G4)" in str(exc.value)
    with pytest.raises(RevlError) as exc:
        compile_source(
            extern_source('let n = emit helper("row") acting { verb: ingest }'),
            "<test>")
    assert "this `let` crosses an unnameable boundary" in str(exc.value)


def test_a_stated_binding_is_admitted_inside_control_flow():
    """The clause rides the statement, so it works wherever a binding does —
    the control-flow interior shares one lowering path with the top level."""
    assert compile_source(
        extern_source('if (true) { ' + BOUND + ' }'), "<test>")


def test_the_binding_slot_contributes_no_ir():
    """The property #1007 pinned and #1031 re-proved, held for the third time
    on the slot that finally makes a returning crossing expressible."""
    plain = compile_source(
        extern_source('let n = emit wr("row")', within=""), "<test>")
    annotated = compile_source(extern_source(BOUND), "<test>")
    assert json.dumps(plain, sort_keys=True) == json.dumps(annotated,
                                                           sort_keys=True)


def test_the_binding_clause_is_the_ast_default():
    program = Parser(extern_source('let n = emit wr("row")', within=""),
                     "<test>").parse()
    body = program.components[0].body[0].methods[0].body
    assert [stmt.acting for stmt in body if isinstance(stmt, LetStmt)] == [None]


def test_positional_letstmt_construction_is_unchanged():
    # `acting` is kept last, so the five-positional construction every existing
    # caller uses still builds a clause-free binding.
    assert LetStmt("n", "value", False, 1, None).acting is None


def test_acting_is_still_an_ordinary_name_in_a_method_body():
    """The clause is CONTEXTUAL in the new slot as well, and told apart from a
    plain name by the `{` that must follow it — so a method that binds a value
    called `acting` and then assigns it still compiles."""
    src = (
        "service Worker { fn run() -> Int }\n"
        "component W provides worker: Worker {\n"
        "  provide worker { fn run() { var acting = 1 acting = 2"
        " return acting } }\n"
        "}\n"
    )
    assert compile_source(src, "<test>")
