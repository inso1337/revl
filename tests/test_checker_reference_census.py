"""The checker/reference divergence census, held where it matters.

`selfhost/checker.rvl` is the second self-host verdict surface — service
boundaries, the checkable core of the G4 upper bound, call-site argument types
— and `tests/test_selfhost_checker.py` is its differential oracle against the
reference compiler. That oracle spells what somebody thought to spell.

Issue #1065 is what that costs. `p_prov_methods` read the method body straight
off the end of the parameter list, so a provide method that RESTATED its
declared return type (`fn go() -> Int { ... }`, which the reference parses and
hands to the type layer) failed the WHOLE component with `(bad) bad provide
block in component <C>`. It had been that way for as long as the function
existed, and nothing noticed, because no test had ever asked this checker about
a document in the tree. `tools/checker_reference_census.py` asks about all of
them and buckets the answers, the way `tools/gate_reference_census.py` does for
`selfhost/lower.rvl`'s `admit_src`.

Two things are held here, and neither is a snapshot. The gate census pins a
recorded baseline because the gate is a SHIPPED wire that consumers act on; the
checker is an oracle, and a baseline over the tree would red on every unrelated
`.rvl` a sibling change adds.

  * AN INVARIANT — no document the reference admits may fail at the
    provide-block parse. One-directional, so a document added tomorrow passes
    it by being parseable, and the only way to red it is to reintroduce the
    defect.
  * A NAMED LIST — the documents whose real verdict that parse refusal was
    hiding. A parse refusal that stands where a guarantee verdict belongs reads
    as agreement, which is why this list is by name with the reason rather than
    a count: a count would let it churn silently.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _census():
    spec = importlib.util.spec_from_file_location(
        "checker_reference_census",
        ROOT / "tools" / "checker_reference_census.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def census():
    return _census()


@pytest.fixture(scope="module")
def verdicts(census):
    """`{relative path: (reference message, checker message)}` over the census
    corpus — one pass of `check_service_src` and one of the reference, shared
    by everything below."""
    check = census.build_check_service_src()
    reference = census.build_reference()
    out = {}
    for name, src in census.load_corpus():
        try:
            out[name] = (reference(src), check(src))
        except RecursionError:
            # the recursive-descent slice limit (the checker's own sources are
            # the deepest documents in the tree), not a verdict; the census
            # buckets it the same way
            continue
    return out


def test_the_corpus_is_the_one_the_gate_census_measures(census, verdicts):
    """A guard on the harness rather than on the checker: an empty or tiny
    corpus would make every assertion below pass by measuring nothing."""
    assert len(verdicts) > 400, f"only {len(verdicts)} documents censused"


def test_no_admitted_document_fails_at_the_provide_block_parse(census, verdicts):
    """The invariant issue #1065 exists to establish, over the tree rather than
    over a chosen document.

    A provide block the reference parses and the checker does not is the worst
    shape a defect takes on this surface. It refuses legal first-party code,
    and on a program the reference refuses it puts a parse failure where the
    real verdict belongs. Measured over the 523 `.rvl` documents in the census
    directories, 97 were refused this way before the return annotation and the
    `=` shorthand body were ported."""
    failed = sorted(
        f"{name}: {got}" for name, (want, got) in verdicts.items()
        if want == "" and got.startswith(census.PROVIDE_BLOCK_REFUSAL))
    assert not failed, (
        "the reference admits these and the checker fails their provide block "
        "at the parse:\n  " + "\n  ".join(failed))


def test_no_admitted_document_fails_at_the_top_level_parse(census, verdicts):
    """The second one-directional invariant on this surface.

    `p_top` refuses a head it cannot read, and a head it cannot read is a whole
    document it never checked. It had no case for the `pub` visibility prefix,
    for a named `test` block (it skipped the header LINE, then walked the body's
    statements as top-level declarations), for the `lifecycle`/`prop`/`fault`
    qualifiers on `test`, for a `boot component`, or for a typed `event`
    declaration — every one of which the reference's `_parse_program` accepts.

    Measured before those heads were ported: 75 of the documents the reference
    admits were refused here, 44 of them by `pub` alone. Like the provide-block
    invariant above, this is one-directional, needs no re-recording, and the
    only way to red it is to reintroduce the gap."""
    failed = sorted(
        f"{name}: {got}" for name, (want, got) in verdicts.items()
        if want == "" and got.startswith(census.TOP_LEVEL_REFUSALS))
    assert not failed, (
        "the reference admits these and the checker fails their top-level "
        "parse:\n  " + "\n  ".join(failed))


def test_no_admitted_document_fails_at_a_declaration_signature(census, verdicts):
    """The third one-directional invariant on this surface.

    The two above cover heads `p_top` could not ENTER. This one covers
    declarations it entered and could not READ: an extern classification other
    than a bare `pure`/`acquire`/`emission` (a capability scope, `witnessed`,
    `async`, and the rest of the modifier slot), a type-parameter list between
    a `fn`'s name and its parameters, the `cache` trailing clause, a record
    field named after one of the eight grammar nouns the reference admits
    there, and the service-operation modifier slot.

    Measured before those were ported: 53 of the documents the reference admits
    were refused here — 29 at `expected fn after extern`, 16 at `bad method
    signature in service <S>`, 6 at `expected { after fn signature` and 2 at
    `bad record field in type <T>`. Like its two siblings this is
    one-directional and carries no baseline, so a document added tomorrow
    passes it by being parseable and the only way to red it is to reintroduce
    the gap."""
    failed = sorted(
        f"{name}: {got}" for name, (want, got) in verdicts.items()
        if want == "" and got.startswith(census.SIGNATURE_REFUSALS))
    assert not failed, (
        "the reference admits these and the checker fails their declaration "
        "signature:\n  " + "\n  ".join(failed))


# The documents whose real verdict the provide-block parse refusal was HIDING.
#
# Every one is refused by the reference and drew a `(bad) bad provide block in
# component <C>` from the checker, which a verdict-direction comparison reads
# as agreement. With the parse fixed they reach the checker's verdict and it
# has none to give: 20 documents moved from a masked refusal to a plain
# no-objection. None is newly admitted — the checker never decided any of them
# — but until now nobody could say so. The same unmasking happened on the other
# surface in PR #1063, where `false-admit/G4` went 2 to 4 for the same reason.
#
# A no-objection is not an admission: `check_service_src` issues none, `""`
# means "this slice found nothing", and `tests/test_gate_crate_admit.py` is
# where an ISSUED admission is held. It is still the direction to watch.
#
# Grouped by what the checker would need in order to decide them.
UNMASKED_UNDECIDED = {
    # -- link stage: the service is declared in a sibling file, and the checker
    # is handed one document's text --
    "demo/components/pg_database.rvl",
    "demo/variants/pg_database.hotswap.rvl",
    "examples/ecosystem-consumer/candidates/leaky_tool.rvl",
    # -- the composition graph: conflicts, cycles, acquisition ordering and
    # provision keys are decided across components, and the checker walks one
    # component at a time --
    "examples/rejections/a2_acquire_after_provide.rvl",
    "examples/rejections/a9_provide_key_not_declared.rvl",
    # issue #1172: the converse of A9 (a declared key no block installs) is
    # decided by the lowering gate, not this slice, for the same reason as the
    # direct half above.
    "examples/rejections/a9_provides_without_block.rvl",
    "examples/rejections/g2_provision_conflict.rvl",
    "examples/rejections/g3_dependency_cycle.rvl",
    "examples/rejections/service_compat_duplicate.rvl",
    "examples/rejections/v2_intercept_on_provision.rvl",
    "examples/rejections/v2_same_realm_conflict.rvl",
    # spawn attenuation, which needs the `cap_order` cone/ceiling algebra the
    # gate does not have either — the same document sits in `KNOWN_BYPASSES` in
    # tests/test_gate_reference_census.py, unmasked by the same parse fix on
    # the other surface
    "examples/rejections/g4_spawn_widens_budget.rvl",
    # -- effect and teardown discipline: host-acquisition provenance, bracket
    # inverses and the `undo` obligation are not in this slice --
    "demo/variants/pg_database.rejected.rvl",
    "examples/rejections/g4_fn_body_host_acquire.rvl",
    "examples/rejections/g4_undo_host_acquire.rvl",
    "examples/rejections/g5_undo_handle_emission.rvl",
    # -- an emission reached through a VALUE (an arrow parameter, an alias
    # binding, a handle) rather than through a callee name, which is the only
    # thing `emit_caps` / `calls_in` follows --
    "examples/rejections/g4_arrow_param_emission.rvl",
    "examples/rejections/g4_unmarked_alias_emission.rvl",
    "examples/rejections/g4_unmarked_handle_emission.rvl",
    # -- one `emit` marker per crossing (issue #1175): an emission evaluated
    # to build an emit head's argument, unmarked or marked in place. The
    # checker's marker rule reads a statement's head call and never its
    # argument list --
    "examples/rejections/g4_nested_unmarked_emission.rvl",
    "examples/rejections/g4_nested_emit_expression.rvl",
    # -- the type layer past the expression slice: a config-field default.
    # `t7_provide_param_annotation_mismatch.rvl` stood here too — the PARAMETER
    # twin of the return annotation #1063 taught the parser to read — until
    # item 391 added A6's signature-agreement block (`a6_signature` in
    # selfhost/checker.rvl) and the checker started deciding it. It is now a
    # pinned refusal in tests/test_selfhost_checker.py instead --
    "examples/rejections/t3_config_default_type.rvl",

    # ======================================================================
    # The second unmasking, by the TOP-LEVEL declaration heads (item 391).
    # 22 more documents moved out of a masked `msg-mismatch/parse` and into a
    # plain no-objection. Same shape as above: the checker never decided any of
    # them, and a parse `(bad)` had been standing where its verdict belongs.
    # ======================================================================

    # -- a cross-file `use`: the reference needs real source paths (or
    # `modules=`) and the census hands it one document's text. Out of this
    # slice by construction, and out of any single-document slice --
    "backends/go/scenarios/emitted/jsonwire/jsonwire.rvl",
    "backends/rust/scenarios/jsonwire.rvl",
    "examples/rejections/v2_use_private.rvl",
    "selfhost/compile.rvl",
    "selfhost/parser.rvl",
    "selfhost/types.rvl",
    "stdlib/auth.rvl",
    "stdlib/framing.rvl",
    "stdlib/template.rvl",
    "tests/fixtures/v2_math_main.rvl",

    # -- a diagnostic raised INSIDE a test body: which components are loaded,
    # what a `call` names, which assertions exist, and which statements a pure
    # `test` may hold. The checker steps a test block over WHOLE (a runner
    # concern it reads nothing out of), so it has no verdict for any of them --
    "dogfood/scratch-plain-test.rvl",
    "examples/rejections/lifecycle_config_unknown_field.rvl",
    "examples/rejections/lifecycle_double_load.rvl",
    "examples/rejections/lifecycle_no_swap.rvl",
    "examples/rejections/lifecycle_stmt_in_pure_test.rvl",
    "examples/rejections/lifecycle_unknown_assertion.rvl",
    "examples/rejections/lifecycle_unknown_component.rvl",
    "examples/rejections/lifecycle_unknown_operation.rvl",

    # -- a field read on an erased `Any`: the type layer past this slice's
    # expression checker, the same family `t3`/`t7` above sit in --
    "backends/typescript/tests/fixtures/dynamic_reserved_key.rvl",
    "examples/rejections/t29_field_read_on_any.rvl",
    "examples/rejections/t30_field_read_on_any_provide_method.rvl",

    # -- extern host-import provenance: a user-origin `@py` body importing a
    # backend module. Nothing in this slice reads extern bodies --
    "stdlib/shell.rvl",

    # ======================================================================
    # The third unmasking, by the DECLARATION SIGNATURES (item 391). 16 more
    # documents moved out of a masked `msg-mismatch/parse` and into a plain
    # no-objection, and one of the 16 — `v2_async_signature_mismatch.rvl` —
    # is not on this list because the same change DECIDED it (A6's async
    # agreement rule, `a6_signature` in selfhost/checker.rvl). The other 15
    # are here. Same shape as the two groups above: the checker never decided
    # any of them, and a parse `(bad)` had been standing where its verdict
    # belongs.
    # ======================================================================

    # -- A1, the sync/async colour rules (item 117,
    # docs/design/async-extern.md). Every one of these needs the async
    # REACHABILITY fixed point `selfhost/lower.rvl` carries and this slice does
    # not: the checker now reads the `async` modifier on a declaration and on a
    # provide method (it has to, to decide A6's agreement rule) and colours
    # nothing with it --
    "examples/rejections/a1_async_arrow_sync_type.rvl",
    "examples/rejections/a1_async_compensate_suspends.rvl",
    "examples/rejections/a1_async_effect_not_awaited.rvl",
    "examples/rejections/a1_async_emit_step_not_awaited.rvl",
    "examples/rejections/a1_async_extern_sync_method.rvl",
    "examples/rejections/a1_async_op_sync_ternary.rvl",
    "examples/rejections/a1_async_undo_suspends.rvl",
    "examples/rejections/a1_effect_await_block.rvl",
    "examples/rejections/t34_arrow_self_declared_async.rvl",

    # -- taint flow: G9's untrusted-to-authority rule and G-SECRET-FLOW's
    # disclosure sinks. Both are a value-provenance analysis across a whole
    # program; this slice follows callee NAMES and nothing else --
    "examples/rejections/g9_closure_capture_launders_taint.rvl",
    "examples/rejections/g9_service_return_launders_taint.rvl",
    "examples/rejections/g9_spawn_config_launders_taint.rvl",
    "examples/rejections/gsecret_service_return_discloses.rvl",

    # -- generic INFERENCE: the type-parameter list is now stepped over
    # (parameters erase), which is what the reference's IR does too, but
    # deciding `List[U]` against `List[Int]` needs the unifier this slice's
    # `compatible` is not --
    "examples/rejections/t25_explicit_tparam_heuristic_off.rvl",

    # -- extern host-import provenance, the same family as `stdlib/shell.rvl`
    # above: nothing in this slice reads extern BODIES --
    "stdlib/fs.rvl",

    # ======================================================================
    # The fourth unmasking, by `asset "<path>"` (item 459 F1) in the shared
    # expression parser. These two did not come out of a parse `(bad)` like
    # the three groups above: they came out of a WRONG SEMANTIC VERDICT,
    # which is the worse shape. `selfhost/parser.rvl` read the juxtaposed
    # string literal as a second expression, and `p_seq` needs no comma
    # between elements, so every argument list holding an asset counted one
    # argument too many and the checker refused a call the reference does
    # not object to. `examples/app/notes.rvl` was pinned by exact text in
    # the message-mismatch ledger below for that reason; with the form read
    # as one expression both documents reach the checker's real verdict,
    # and it has none. They are the live regression guard on the fix: if
    # the arity comes back, both red here rather than churning a count.
    # ======================================================================
    "examples/app/notes.rvl",
    "examples/webui-entry/console.rvl",
}


# The documents whose real verdict a parse refusal was hiding, and where the
# verdict underneath turned out to DISAGREE with the reference's text.
#
# This ledger is EMPTY, and it is empty because its one entry was closed rather
# than re-recorded. `examples/app/notes.rvl` stood here with
# "`webui.add_entry` takes 4 argument(s), 5 given": `asset "<path>"` (item 459
# F1) was one argument to the reference's expression parser and two to this
# one's, so a four-argument call read as five. `selfhost/parser.rvl` now reads
# the form as a single expression, the count agrees, and the document moved
# into `UNMASKED_UNDECIDED` above together with
# `examples/webui-entry/console.rvl`, which carried the same wrong verdict
# without ever having been pinned. Measured over the census corpus:
# `msg-mismatch/semantic` 3 to 1, `no-objection` 156 to 158.
#
# A ledger in this repo only shrinks, so the entry is gone rather than
# restated. What guards the fix is the pair of names in `UNMASKED_UNDECIDED`
# (each asserted to be UNDECIDED, so a returning arity reds them) and the
# `asset` corpus in `tests/test_selfhost_parser.py`, whose nine documents
# render differently against the unported parser and whose nine negative
# controls render the same.


@pytest.mark.parametrize("rel", sorted(UNMASKED_UNDECIDED))
def test_the_unmasked_documents_are_refused_and_undecided(verdicts, rel):
    """Each named document, held in the state the parse fix left it.

    Two ways to red, both of which want a human in the diff: the reference
    stops refusing it (the document changed, and the entry is meaningless), or
    the checker starts deciding it (a slice grew — delete the line and say
    which). Neither can be made to pass by re-recording anything."""
    assert rel in verdicts, f"{rel} is not in the census corpus"
    want, got = verdicts[rel]
    assert want != "", f"the reference now admits {rel}; drop it from the list"
    assert got == "", (
        f"the checker now decides {rel} ({got!r}); delete it from "
        f"UNMASKED_UNDECIDED and say which slice grew")
