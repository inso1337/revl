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
    # -- the type layer past the expression slice: a config-field default, and
    # the PARAMETER twin of this very issue. `t7` checks a provide method's
    # parameter annotation against the service declaration; the checker now
    # parses the annotation and does not yet check it, which is the next step
    # on this axis and not a regression — it was refused for being
    # unparseable, never for being wrong --
    "examples/rejections/t3_config_default_type.rvl",
    "examples/rejections/t7_provide_param_annotation_mismatch.rvl",

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
}


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
