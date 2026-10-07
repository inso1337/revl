"""The gate/reference divergence census, held at its recorded baseline.

`crates/revl-gate` embeds `selfhost/lower.rvl`'s `admit_src`, and the two
implementations disagree in places. `tests/test_gate_crate_admit.py` pins the
hand-written oracle corpus and `tools/fuzz_frontend.py --stage gate` can stumble
on a bypass; neither ENUMERATES the disagreement. This file runs
`tools/gate_reference_census.py` over the whole census corpus and fails on any
change from `tools/gate_reference_census_baseline.json`, in either direction:

  * a NEW divergence is a regression — including, and especially, a new FALSE
    REJECTION. A change that "fixes" a bypass by refusing more programs lands
    in that bucket and nowhere else, which is the control this repo needs:
    an 85-file false-rejection regression once shipped with every unit test
    green, because no test compared verdicts over the whole corpus.
  * a divergence that is GONE means a fix landed and the baseline is stale.
    Re-record it (`python3 tools/gate_reference_census.py --record`) in the same
    commit, so the shrinking allowance is visible in the diff rather than
    silently generous.
  * a `false-admit` case — the reference refuses under a guarantee this gate
    claims to decide and the gate raises no objection — is a GATE BYPASS. The
    open ones are listed by name in `KNOWN_BYPASSES` below, with the family each
    belongs to, and `test_the_open_bypass_surface_is_exactly_the_named_list`
    fails on any case that is not on that list. A count would let the list churn
    silently; a named list has to be edited, in a diff somebody reads.

This runs in the `frontend` job's plain `pytest tests/ -q`: no cargo, no wasm
toolchain, ~30 seconds. The `--engine crate` half, which asks the real crate the
same questions, needs a rust toolchain and lives in
`tests/test_gate_crate_admit.py` with the rest of the crate's differential.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from _load_by_path import load_by_path  # noqa: E402


def _census():
    module = load_by_path(
        "gate_reference_census",
        ROOT / "tools" / "gate_reference_census.py")
    return module


@pytest.fixture(scope="module")
def census():
    return _census()


@pytest.fixture(scope="module")
def measured(census):
    """The census itself: one self-host build, one pass over the corpus."""
    reference, oracle = census._reference()
    cases = census.load_corpus(oracle)
    return cases, census.run(cases, census.SelfhostEngine(), reference)


def test_the_corpus_is_not_empty(measured):
    cases, _ = measured
    assert len(cases) > 300, \
        f"the census corpus collapsed to {len(cases)} programs; a directory " \
        f"moved out from under tools/gate_reference_census.py"


def test_no_bypass_and_no_new_divergence(census, measured):
    """The gate must not admit what the reference refuses under a guarantee it
    claims, and must not diverge anywhere the baseline does not already say."""
    _, (buckets, details) = measured
    baseline = json.loads(census.BASELINE.read_text())
    problems = census.compare(buckets, baseline)
    if problems:
        report = "\n".join(f"  {line}" for line in problems[:40])
        extra = "" if len(problems) <= 40 else \
            f"\n  ... and {len(problems) - 40} more"
        pytest.fail(
            f"the gate/reference census moved:\n{report}{extra}\n\n"
            f"A NEW entry is a regression — a new false rejection counts. A "
            f"MISSING entry means a fix landed: re-record with\n"
            f"  python3 tools/gate_reference_census.py --record\n\n"
            f"{census.report(buckets)}")
    # the details map is what makes a future regression readable; a baseline
    # that has drifted into recording nothing would pass the comparison above
    # while measuring nothing at all.
    assert details or not any(
        name.split("/", 1)[0] in census.TRACKED for name in buckets)


# The LINE ledger (issue #1965). The comparison above sees tag and message; a
# refusal both sides agree on can still be anchored at a different line, and
# `admit_src` orders a multi-defect program by line. Every such refusal is named
# in `tools/gate_reference_line_ledger.json` with its `[reference, gate]` pair,
# and this fails on a mismatch not listed there, a pair that moved, and an entry
# that no longer mismatches, so the list only shrinks.
def test_every_line_mismatch_is_named_on_the_ledger(census, measured):
    cases, (buckets, _) = measured
    pairs = census.line_pairs(cases, buckets, census.SelfhostEngine())
    ledger = json.loads(census.LINE_LEDGER.read_text())
    problems = census.compare_lines(pairs, ledger)
    assert not problems, "\n  ".join(
        ["the gate/reference line ledger moved:"] + problems[:40])


def test_the_line_ratchet_fails_in_all_three_directions(census):
    ledger = {"mismatches": {"a.rvl": [3, 2], "b.rvl": [9, 7]}}
    problems = census.compare_lines({"a.rvl": [3, 1], "c.rvl": [4, 2]}, ledger)
    assert any(p.startswith("new line mismatch: c.rvl") for p in problems)
    assert any(p.startswith("line pair moved: a.rvl") for p in problems)
    assert any(p.startswith("no longer a line mismatch: b.rvl") for p in problems)
    assert census.compare_lines({"a.rvl": [3, 2], "b.rvl": [9, 7]}, ledger) == []


# The OPEN BYPASS SURFACE, named case by case.
#
# Each of these is a program the reference refuses under a guarantee this gate
# claims to decide, and the gate raises no objection to it. They are listed —
# not merely counted — so the list can be read, worked down, and never grow by
# accident: `--record` would happily write a new one into the baseline, and this
# is what stops that from passing unnoticed.
#
# Item 131 §3's effect-composition `await`/async pairing over `effect` / `emit`
# steps (lower.py `_admit_effect_async` / `_admit_emit_async`, rules 1-3) is
# CLOSED: the statement now carries its own `await` marker (`Stmt.awaited`), so
# `stmt_a1_verdict` decides the exact pairing and the suspending-teardown rule
# per statement, and the name fence (`setup_async_verdict`) prunes the awaited
# steps it must not see. The two entries that remain are each in a layer this
# gate deliberately does not run:
#
# THE TYPE LAYER (docs/design/457). The bulk of this list is the type-layer
# gap named executably at slice T0: 43 fixtures over `examples/rejections/`
# that the reference refuses in its type checker and `selfhost/lower.rvl`'s
# `admit_src` admits, because the gate runs no type layer yet. (The
# self-declared async-colour arrow left this gap once the gate learned to parse
# an arrow's written return annotation and refuse a self-declared `Async[…]`
# colour — rule C1 — so it now agrees with the reference.) They used to sit
# in `no-objection-out-of-slice` (the classifier tagged every one "OUT:"); T0
# taught `tests/test_selfhost_lower.py::_classify` the type vocabulary, so they
# now surface here where they can be worked down. `TYPE_LAYER_GAP` in that same
# file pins the divergence per fixture; the two lists move together. Each later
# slice (T1..T4) refuses a family for real, at which point its fixtures leave
# BOTH lists and the census baseline is re-recorded.
# -- fn-body binding rules (G1/G6): CLOSED, no row left --
# The ASSIGNMENT half landed with item 391's binding-discipline slice (the
# `let`/`var`/parameter scope walk over a module `fn` body, plus the
# arrow-body write form): `v2_let_reassignment`,
# `v2_compound_assign_on_let`, `v2_duplicate_let_block_scope` and
# `g6_closure_mutates_capture` refused with the reference's message
# byte-for-byte and were struck from this list, and the callable-shadowing
# slice struck `shadowed_module_fn_call` the same way. The name-RESOLUTION
# rule (docs/design/457 §2.3) took the last two, `g1_template_undeclared`
# and `v2_undeclared_fn_var`: a name READ now resolves against the fn's
# scope and the callable universe, so the gate refuses both under G1 in the
# reference's own sentence and this family has no open bypass.
# -- expression typing (T1/T2) --
# The fn-body STATEMENT layer (docs/design/457 T3a) closed this family for
# the module-`fn` surface: `t2`, `t11`, `t12`, `t21`, `t22`, `t23`, `t26`,
# `t27`, `t28`, `t29`, `t36` and `dynamic_reserved_key` now refuse with the
# reference's own sentence and have been struck from this list, and the
# provide-method slice has since struck `t30` the same way: the walk over a
# component body now carries the environment that slice left empty — the
# method parameters at the service's declared types, the body's annotated
# and inferred locals, the activation locals at the operations they bind.
# The optional-chain rule (docs/design/457 T2d) closed the rest: `?.` now
# requires an optional on its left, so `t14_optional_chain_on_nonoptional`
# refuses with the reference's own sentence and is struck from this list.
# This family has no open bypass.
# -- calls and signatures --
# CLOSED WHOLE by docs/design/457 T2b: the signature table with its marked
# type parameters, the arity window, `unify`/`substitute` at a generic call
# site, the host stub surface, `_BUILTIN_SIG` with its receiver families and
# bottom learning, and the four refusals the reference makes while LOWERING
# a method call. All nine of this family's fixtures now refuse with the
# reference's own tag and sentence and are struck from this list.
# -- arrows and function values --
# CLOSED WHOLE by docs/design/457 T2c: an arrow types as a function value
# (parameters at their annotations or bottom, the result from the body only
# where no bottom parameter reaches it), its body is walked as an ordinary
# expression over the enclosing scope, a call through such a value is
# checked for arity and then per argument, and an annotation's type name
# resolves to the enclosing `fn`'s type parameter or an opaque nominal and
# never to a fresh one. All four remaining fixtures of this family now
# refuse with the reference's own tag and sentence and are struck from this
# list; `t34_arrow_self_declared_async` left it earlier with rule C1.
# -- return paths and match: CLOSED, no row left --
# The RETURN-PATH half landed first (docs/design/457 T3b): `fb_function`
# runs `_check_returns_on_every_path` over the statement tree the fn-body
# walk already builds, so `t8_missing_return` and
# `t9_return_path_incomplete` refuse with the reference's message AND its
# line. The MATCH half closed the rest: the declaration scan now records
# each variant's ordered case list, and `_check_match_exhaustiveness` runs
# at the position `_lower_pure_expr` runs it, so `t13_unknown_match_case`
# and `v2_match_nonexhaustive` refuse with the reference's own sentence and
# are struck from this list.
# -- declarations: CLOSED, no row left --
# `t6_bare_generic` LEFT this list with the type layer's slice T1:
# `selfhost/lower.rvl` now `use`s the shared type-spelling algebra in
# `selfhost/types.rvl` and runs `check_type_wellformed` over every module
# `fn`/`extern` signature and every config field, at the phase position
# `_validate_declared_types` gives it. The other two followed with T3b:
# `_resolve_type_aliases`' `expand` recursion is ported at the head of the
# declaration level (`t18_type_alias_cycle`), and
# `_lower_let_pattern_stmt`'s "requires a record" arms are read off a
# record destructuring pattern the fn-body walk used to step over
# (`t5_destructure_nonrecord`).
# -- provide-method and component bodies: NONE --
# The whole family closed with the provide-method slice (docs/design/457).
# `t1_service_arg_type`, `t4_field_arg_type`,
# `t7_provide_param_annotation_mismatch`,
# `t16_provide_method_missing_return`,
# `t31_index_non_int_provide_method` and `t3_config_default_type` now refuse
# with the reference's own sentence; `t30_field_read_on_any_provide_method`
# left the expression-typing group above in the same change.
# -- NOT the type layer: the parameterized rows are STRUCK --
# `_check_spawn_attenuation`'s two parameterized rows --
# `g4_spawn_widens_parameter` (a `path` cone) and `g4_spawn_widens_budget`
# (a `calls` ceiling) -- are STRUCK: `selfhost/lower.rvl` now carries the
# `cap_order` (T, P) order and `lower.py::_cap_keyed`'s key-to-token bridge,
# so both sides of the attenuation fold are spelled in the boundary's own
# namespace and both refuse with the reference's message byte-for-byte.
# `examples/rejections/g4_dotted_capability_key.rvl` is the corpus document
# for the shape that change caught and nothing spelled: a dotted item-343
# emission scope, which the old scope-list reader split into two
# capabilities so that the wiring key landed in the declared scope by
# accident. The other shape it caught -- a widening laundered through a key
# SPELLED the same on both sides -- is pinned by an in-file test in
# `selfhost/lower.rvl` instead; see the capability-order header there.
#
# -- the model role in the SPAWN product (item 519, issue #1193 slice 3) --
# `_check_spawn_attenuation` builds the surface its closure STARTS from out of
# each component's own crossings. A component whose effective ceiling is wider
# than its own crossings -- because a `model role` it routes through reaches
# further -- is therefore accounted as if the role were inert, and a spawner
# holding only the role's key admits a child that reaches past it. The
# reference now folds each component's role reach into that base
# (`lower.py::_spawn_base_with_model`), so the monotone-shrinkage refusal G4
# carries the case with no second rule and no second message. The gate's
# `check_spawn` now folds the same reach in (`selfhost/lower.rvl`'s
# `model_reach_spawn_base`, whose result is the surface the closure STARTS
# from), so both engines refuse the two widening documents with the reference's
# own sentence and no bypass is left for the corpus below.
#
# The fold is inert for every program that does not declare a `model role`
# (`model_roles_of` empty) and for a component this slice does not decide: an
# undecided role edge is left UNFOLDED, keeping the gate's previous answer
# rather than guessing at a reach the fold cannot justify. That direction is
# deliberate -- a false refusal over the corpus is the regression the census
# exists to catch, while leaving a widening unfolded only re-opens the gap this
# section records -- and `test_the_model_reach_spawn_corpus_is_decided_by_both`
# below pins all four documents, so the two directions cannot be confused.
KNOWN_BYPASSES: set[str] = set()


def test_the_open_bypass_surface_is_exactly_the_named_list(census, measured):
    """The bypass direction, capped by name.

    A bypass that is not on this list fails: the gate started admitting
    something the reference refuses under a guarantee it claims. A listed case
    that no longer bypasses also fails, so a fix has to delete its line here and
    say so in the diff."""
    _, (buckets, _) = measured
    found = set(census.bypasses(buckets))
    added = sorted(found - KNOWN_BYPASSES)
    fixed = sorted(KNOWN_BYPASSES - found)
    assert not added, (
        "NEW GATE BYPASS — the reference refuses these under a guarantee this "
        "gate decides and the gate raised no objection:\n  "
        + "\n  ".join(added))
    assert not fixed, (
        "these no longer bypass the gate; delete them from KNOWN_BYPASSES and "
        "re-record the census baseline:\n  " + "\n  ".join(fixed))


# --- authority rules after a block (tests/fixtures/gate_block_nesting/) -------
#
# The gate's statement reader lost track of block nesting. A `}` that began a
# line ended the enclosing body, so a multi-line activation guard hid every
# `provide` after it and a multi-line `if`/`while` hid every statement after it
# in a provide method. A block written on one line was skipped whole. The
# reference refused all of them under G1 or G4 and the gate raised no objection.
# The census stayed green because no corpus document put a violation after, or
# inside, a block.
#
# The census compares only the TRACKED buckets against its baseline, so a
# document here that drifted to a refusal the reference raises for some other
# reason would read as `no-objection-out-of-slice` and nobody would notice. Each
# document's bucket is therefore held by name: the file name's prefix is the
# verdict (`g1_`, `g4_` refused under that guarantee, `ok_` admitted).
BLOCK_NESTING = ROOT / "tests" / "fixtures" / "gate_block_nesting"


def _block_nesting_expected(stem: str) -> str:
    if stem.startswith("ok_"):
        return "agree-admit"
    return "agree-refuse/" + stem.split("_", 1)[0].upper()


def test_every_authority_rule_after_a_block_is_refused_by_both(measured):
    _, (buckets, _) = measured
    got = {case: name for name, cases in buckets.items() for case in cases}
    docs = sorted(BLOCK_NESTING.glob("*.rvl"))
    assert len(docs) >= 40, f"the block-nesting corpus shrank to {len(docs)}"
    wrong = []
    for doc in docs:
        case = str(doc.relative_to(ROOT))
        want = _block_nesting_expected(doc.stem)
        if got.get(case) != want:
            wrong.append(f"{case}: {got.get(case)}, expected {want}")
    assert not wrong, "\n  ".join(["block-nesting documents moved:"] + wrong)


# --- spawn attenuation over value-position crossings (issue #1562) ------------
#
# A child that crossed a boundary as a value (`let`, `return`, an expression
# body, an `if` arm, an argument, a compensation, a host extern, its own spawn
# handle) was spawned by a parent that does not hold it. On the base the
# reference admitted all eight and the gate refused four of them, a split no
# corpus document exposed. Held by name like the block-nesting corpus: `g4_`
# both refuse under G4, `ok_` both admit.
SPAWN_ATTENUATION_VALUE = ROOT / "tests" / "fixtures" / "spawn_attenuation_value"


def test_every_value_crossing_bounds_a_spawned_child_in_both(measured):
    _, (buckets, _) = measured
    got = {case: name for name, cases in buckets.items() for case in cases}
    docs = sorted(SPAWN_ATTENUATION_VALUE.glob("*.rvl"))
    assert len(docs) == 16, f"the value-crossing corpus has {len(docs)} documents"
    wrong = []
    for doc in docs:
        case = str(doc.relative_to(ROOT))
        want = _block_nesting_expected(doc.stem)
        if got.get(case) != want:
            wrong.append(f"{case}: {got.get(case)}, expected {want}")
    assert not wrong, "\n  ".join(["value-crossing documents moved:"] + wrong)


# --- effect statement rules (issue #1963) ------------------------------------
#
# Three statement rules the gate did not decide: a witnessed extern called with
# a site `undo`, an undo-less effect over a dotted call, and a
# teardown-registering step inside a provide-method `if`/`while`/`for`. All were
# false admissions. The arrow-method G1 document is here for the line the gate
# now anchors it at, which this census does not compare (the in-file
# `admit_all` test in selfhost/lower.rvl does). `g4_`/`t1_`/`g1_` both refuse
# under that tag, `ok_` both admit.
EFFECT_STATEMENT_RULES = ROOT / "tests" / "fixtures" / "effect_statement_rules"


def test_every_effect_statement_document_is_decided_alike_by_both(measured):
    _, (buckets, _) = measured
    got = {case: name for name, cases in buckets.items() for case in cases}
    docs = sorted(EFFECT_STATEMENT_RULES.glob("*.rvl"))
    assert len(docs) == 14, f"the effect-statement corpus has {len(docs)} documents"
    wrong = []
    for doc in docs:
        case = str(doc.relative_to(ROOT))
        want = _block_nesting_expected(doc.stem)
        if got.get(case) != want:
            wrong.append(f"{case}: {got.get(case)}, expected {want}")
    assert not wrong, "\n  ".join(["effect-statement documents moved:"] + wrong)


# --- the model reach fold (item 519, issue #1193 slice 2, issue #1451) --------
#
# A role a component's crossing is placed on was outside the product unless a
# `route model` block named it, and the gate did not decide the fold at all.
# The gate now folds it on the held set its spawn attenuation builds and spells
# the refusal byte for byte. `model_` documents both refuse under MODEL, `ok_`
# both admit.
MODEL_REACH_CROSSING = ROOT / "tests" / "fixtures" / "model_reach_crossing"


def test_every_model_reach_document_is_decided_alike_by_both(measured):
    _, (buckets, _) = measured
    got = {case: name for name, cases in buckets.items() for case in cases}
    docs = sorted(MODEL_REACH_CROSSING.glob("*.rvl"))
    assert len(docs) == 13, f"the model-reach corpus has {len(docs)} documents"
    wrong = []
    for doc in docs:
        case = str(doc.relative_to(ROOT))
        want = _block_nesting_expected(doc.stem)
        if got.get(case) != want:
            wrong.append(f"{case}: {got.get(case)}, expected {want}")
    assert not wrong, "\n  ".join(["model-reach documents moved:"] + wrong)


# --- the model role in the SPAWN product (item 519, issue #1193 slice 3) ------
#
# A component's effective ceiling is its own crossings UNION what the model role
# it routes through reaches, and a spawner must cover the CHILD's effective
# ceiling, not just the child's own crossings. Both engines fold the role reach
# into the surface the spawn closure STARTS from (the reference's
# `lower.py::_spawn_base_with_model`, the gate's `model_reach_spawn_base`), so
# the monotone-shrinkage refusal G4 carries the case and the existing message
# names the widening. The two `model_` documents are the widening; the two `ok_`
# documents are the controls -- a role that reaches nothing wider than the
# component holds, and a spawner that holds the role's reach.
MODEL_REACH_SPAWN = ROOT / "tests" / "fixtures" / "model_reach_spawn"

# The verdict each document must get, BY NAME. A rule read off the filenames --
# "anything called `model_*` is allowed to diverge" -- makes the expected
# verdict a function of the name, so neither a rename nor a third widening
# document could fail it. A list has to be edited: a third document means
# adding its name here in a diff somebody reads, and its bucket is then the one
# the census records for it.
MODEL_REACH_SPAWN_WIDENING = (
    "tests/fixtures/model_reach_spawn/model_child_role_reach.rvl",
    "tests/fixtures/model_reach_spawn/model_grandchild_role_reach.rvl",
)
MODEL_REACH_SPAWN_CONTROLS = (
    "tests/fixtures/model_reach_spawn/ok_role_within_the_spawner.rvl",
    "tests/fixtures/model_reach_spawn/ok_spawner_holds_the_role_reach.rvl",
)


def test_the_model_reach_spawn_corpus_is_decided_by_both(measured):
    _, (buckets, _) = measured
    got = {case: name for name, cases in buckets.items() for case in cases}
    docs = sorted(str(doc.relative_to(ROOT))
                  for doc in MODEL_REACH_SPAWN.glob("*.rvl"))
    assert docs == sorted(MODEL_REACH_SPAWN_WIDENING
                          + MODEL_REACH_SPAWN_CONTROLS), \
        f"the model-reach spawn corpus is {docs}"
    wrong = [f"{case}: {got.get(case)}, expected {want}"
             for case, want in (
                 [(c, "agree-refuse/G4") for c in MODEL_REACH_SPAWN_WIDENING]
                 + [(c, "agree-admit") for c in MODEL_REACH_SPAWN_CONTROLS])
             if got.get(case) != want]
    assert not wrong, "\n  ".join(["model-reach spawn documents moved:"] + wrong)


# --- crossings through service-typed locals (issue #1509) ----------------------
#
# A provision held by a local chosen by an `if`, a record field or a list
# element crossed past the approval floor, and through a field or an element
# past the marker rule too. Both engines read one resolver for it now. `g4_`
# both refuse under G4, `ok_` both admit.
APPROVAL_SERVICE_LOCALS = ROOT / "tests" / "fixtures" / "approval_service_locals"


def test_every_service_local_document_is_decided_alike_by_both(measured):
    _, (buckets, _) = measured
    got = {case: name for name, cases in buckets.items() for case in cases}
    docs = sorted(APPROVAL_SERVICE_LOCALS.glob("*.rvl"))
    assert len(docs) == 21, f"the service-local corpus has {len(docs)} documents"
    wrong = []
    for doc in docs:
        case = str(doc.relative_to(ROOT))
        want = _block_nesting_expected(doc.stem)
        if got.get(case) != want:
            wrong.append(f"{case}: {got.get(case)}, expected {want}")
    assert not wrong, "\n  ".join(["service-local documents moved:"] + wrong)


# --- crossings through a service-typed receiver expression (issue #1681) -------
#
# The same crossing as the service-typed local, with the receiver written in
# place: an `if`, a `match`, a record or list literal read in place. `g4_` both
# refuse under G4, `ok_` both admit.
SERVICE_RECEIVER_EXPRESSIONS = ROOT / "tests" / "fixtures" / "service_receiver_expressions"


def test_every_receiver_expression_document_is_decided_alike_by_both(measured):
    _, (buckets, _) = measured
    got = {case: name for name, cases in buckets.items() for case in cases}
    docs = sorted(SERVICE_RECEIVER_EXPRESSIONS.glob("*.rvl"))
    assert len(docs) == 22, f"the receiver-expression corpus has {len(docs)} documents"
    wrong = []
    for doc in docs:
        case = str(doc.relative_to(ROOT))
        want = _block_nesting_expected(doc.stem)
        if got.get(case) != want:
            wrong.append(f"{case}: {got.get(case)}, expected {want}")
    assert not wrong, "\n  ".join(["receiver-expression documents moved:"] + wrong)


# --- crossings through a service-typed method parameter (issue #1682) ---------
#
# A provide method's own parameter of a service type: a call through it is a
# crossing of the service's declared scopes, judged in the method (marker,
# approval floor, provider upper bound). `g4_` both refuse under G4, `ok_`
# both admit.
SERVICE_TYPED_PARAMS = ROOT / "tests" / "fixtures" / "service_typed_params"


def test_every_service_typed_param_document_is_decided_alike_by_both(measured):
    _, (buckets, _) = measured
    got = {case: name for name, cases in buckets.items() for case in cases}
    docs = sorted(SERVICE_TYPED_PARAMS.glob("*.rvl"))
    assert len(docs) == 10, f"the service-typed-param corpus has {len(docs)} documents"
    wrong = []
    for doc in docs:
        case = str(doc.relative_to(ROOT))
        want = _block_nesting_expected(doc.stem)
        if got.get(case) != want:
            wrong.append(f"{case}: {got.get(case)}, expected {want}")
    assert not wrong, "\n  ".join(["service-typed-param documents moved:"] + wrong)


# --- the provider bound reads a handle crossing at the op's scope (#1508) -----
#
# A spawn-handle crossing (direct, aliased, in value position) is read at the
# op's declared scope, a bare op stays `*`. `g4_` both refuse under G4, `ok_`
# both admit.
HANDLE_PROVIDER_BOUND = ROOT / "tests" / "fixtures" / "handle_provider_bound"


def test_every_handle_bound_document_is_decided_alike_by_both(measured):
    _, (buckets, _) = measured
    got = {case: name for name, cases in buckets.items() for case in cases}
    docs = sorted(HANDLE_PROVIDER_BOUND.glob("*.rvl"))
    assert len(docs) == 8, f"the handle-bound corpus has {len(docs)} documents"
    wrong = []
    for doc in docs:
        case = str(doc.relative_to(ROOT))
        want = _block_nesting_expected(doc.stem)
        if got.get(case) != want:
            wrong.append(f"{case}: {got.get(case)}, expected {want}")
    assert not wrong, "\n  ".join(["handle-bound documents moved:"] + wrong)


# --- a method call on a record in a component body (issue #1547) -------------
#
# Refused with the `fn` body's message ("no builtin method `f` on values"),
# which the census tags T1. `t1_` both refuse, `ok_` both admit.
RECORD_FIELD_CALL = ROOT / "tests" / "fixtures" / "record_field_call"


def test_every_record_field_call_document_is_decided_alike_by_both(measured):
    _, (buckets, _) = measured
    got = {case: name for name, cases in buckets.items() for case in cases}
    docs = sorted(RECORD_FIELD_CALL.glob("*.rvl"))
    assert len(docs) == 9, f"the record-field-call corpus has {len(docs)} documents"
    wrong = []
    for doc in docs:
        case = str(doc.relative_to(ROOT))
        want = _block_nesting_expected(doc.stem)
        if got.get(case) != want:
            wrong.append(f"{case}: {got.get(case)}, expected {want}")
    assert not wrong, "\n  ".join(["record-field-call documents moved:"] + wrong)


# --- a deferred extern reached outside an `emit` marker (item 400, #1688) -----
#
# The reference refused each `g4_` document under G4 and the gate raised no
# objection, a gap no corpus document showed until these. `g4_` both refuse,
# `ok_` both admit.
DEFERRED_REACH = ROOT / "tests" / "fixtures" / "deferred_reach"


def test_every_deferred_reach_document_is_decided_alike_by_both(measured):
    _, (buckets, _) = measured
    got = {case: name for name, cases in buckets.items() for case in cases}
    docs = sorted(DEFERRED_REACH.glob("*.rvl"))
    assert len(docs) == 9, f"the deferred-reach corpus has {len(docs)} documents"
    wrong = []
    for doc in docs:
        case = str(doc.relative_to(ROOT))
        want = _block_nesting_expected(doc.stem)
        if got.get(case) != want:
            wrong.append(f"{case}: {got.get(case)}, expected {want}")
    assert not wrong, "\n  ".join(["deferred-reach documents moved:"] + wrong)


# --- `verified` in a provide method (issue #1897) ----------------------------
#
# Only a witnessed effect may be verified in a provide method. The reference
# refused the other shapes (a site `undo`, a let-bound effect, `verified emit`)
# and the gate skipped the `verified` line whole, a false admission no corpus
# document showed. `t1_` both refuse, `ok_` both admit.
VERIFIED_METHOD_EFFECT = ROOT / "tests" / "fixtures" / "verified_method_effect"


def test_every_verified_method_document_is_decided_alike_by_both(measured):
    _, (buckets, _) = measured
    got = {case: name for name, cases in buckets.items() for case in cases}
    docs = sorted(VERIFIED_METHOD_EFFECT.glob("*.rvl"))
    assert len(docs) == 4, f"the verified-method corpus has {len(docs)} documents"
    wrong = []
    for doc in docs:
        case = str(doc.relative_to(ROOT))
        want = _block_nesting_expected(doc.stem)
        if got.get(case) != want:
            wrong.append(f"{case}: {got.get(case)}, expected {want}")
    assert not wrong, "\n  ".join(["verified-method documents moved:"] + wrong)


# --- `try e`, Result propagation in a `fn` body (issue #1900) ---------------
#
# `ok_` both admit; `t1_` both refuse T1 with the same message: an operand that
# is no `Result`, an enclosing return that is not `Result[_, E]` for its `E`, a
# `try` in any position but a whole `let` initializer or `return` operand, an
# `emit` operand, and a `try` in a provide method.
TRY_EXPR = ROOT / "tests" / "fixtures" / "try_expr"


def test_every_try_expr_document_is_decided_alike_by_both(measured):
    _, (buckets, _) = measured
    got = {case: name for name, cases in buckets.items() for case in cases}
    docs = sorted(TRY_EXPR.glob("*.rvl"))
    assert len(docs) == 17, f"the try-expr corpus has {len(docs)} documents"
    wrong = []
    for doc in docs:
        case = str(doc.relative_to(ROOT))
        want = _block_nesting_expected(doc.stem)
        if got.get(case) != want:
            wrong.append(f"{case}: {got.get(case)}, expected {want}")
    assert not wrong, "\n  ".join(["try-expr documents moved:"] + wrong)


# --- a non-builtin method on a stdlib value in a component (issues #1942,
# #1968) ---------------------------------------------------------------------
#
# Refused with the named receiver's message ("no builtin method `map` on
# `List[Int]`"), named or written in place; the census tags it T1. `t1_` both
# refuse, `ok_` both admit. Before the #1942 fix the reference admitted every
# in-place document and the gate admitted every one of them, the named ones
# included. #1968 added `Map` to the refused heads — the one value head a host
# handle shares its name with, told from the value by the binding — and the
# three `Map` fixtures and the handle guard beside them.
VALUE_METHOD_CALL = ROOT / "tests" / "fixtures" / "value_method_call"


def test_every_value_method_call_document_is_decided_alike_by_both(measured):
    _, (buckets, _) = measured
    got = {case: name for name, cases in buckets.items() for case in cases}
    docs = sorted(VALUE_METHOD_CALL.glob("*.rvl"))
    assert len(docs) == 15, f"the value-method-call corpus has {len(docs)} documents"
    wrong = []
    for doc in docs:
        case = str(doc.relative_to(ROOT))
        want = _block_nesting_expected(doc.stem)
        if got.get(case) != want:
            wrong.append(f"{case}: {got.get(case)}, expected {want}")
    assert not wrong, "\n  ".join(["value-method-call documents moved:"] + wrong)


# --- a block `match` arm in a component body (issue #1699) ------------------
#
# The gate's statement reader did not read a block arm: the reference admitted
# every `ok_` document and the gate refused it G1 on the arm's names, and each
# refusal came back on the wrong name. `ok_` both admit, `g1_`/`g4_` both refuse
# under that tag.
MATCH_BLOCK_ARMS = ROOT / "tests" / "fixtures" / "match_block_arms"


def test_every_match_block_arm_document_is_decided_alike_by_both(measured):
    _, (buckets, _) = measured
    got = {case: name for name, cases in buckets.items() for case in cases}
    docs = sorted(MATCH_BLOCK_ARMS.glob("*.rvl"))
    assert len(docs) == 16, f"the match-block-arm corpus has {len(docs)} documents"
    wrong = []
    for doc in docs:
        case = str(doc.relative_to(ROOT))
        want = _block_nesting_expected(doc.stem)
        if got.get(case) != want:
            wrong.append(f"{case}: {got.get(case)}, expected {want}")
    assert not wrong, "\n  ".join(["match-block-arm documents moved:"] + wrong)


def test_every_guarantee_this_census_names_is_in_the_construct_reach_row(measured):
    """The vocabulary `tools/oracle_construct_reach.py`'s `gate_census` row
    calls its reference set is read STATICALLY out of `_classify`, so that the
    report stays a `python3 tools/...` script with no pytest on the path. This
    holds that reading to a real census run: every guarantee the classifier
    actually produced over the corpus has to be a construct the row surveys, or
    the row has a blind family and the ratchet cannot see it go unreached.

    Here rather than beside the report because the run is already paid for."""
    reach = load_by_path(
        "oracle_construct_reach",
        ROOT / "tools" / "oracle_construct_reach.py")
    surveyed = reach._census_guarantees()

    _, (buckets, _) = measured
    named = set()
    for name in buckets:
        head, _, tail = name.partition("/")
        if head in ("agree-refuse", "false-admit", "msg-mismatch"):
            named.add(tail)
        elif head == "tag-mismatch":
            named.add(tail.split("->", 1)[0])
    assert named, "the census classified nothing; the corpus or the reference moved"
    assert named <= surveyed, (
        "the census named these guarantees over its corpus and the "
        "construct-reach row does not survey them:\n  "
        + "\n  ".join(sorted(named - surveyed)))


# --- the false-admission guard (docs/design/457, issue #346) -----------------
#
# The gate HAS an admission arm now (`revl_gate::issue_admission`): it upgrades a
# no-objection to an ISSUED admission where the source is inside the admission
# surface, and both census engines exercise it. So `false-admission` is a live
# release-blocking guard rather than a scaffold, and the tests below hold three
# separate things: the CLASSIFIER routes an issued admission correctly, the
# zero-tolerance handling cannot be baselined away, and the bucket is empty over
# a corpus that actually contains issued admissions.


def test_an_issued_admission_the_reference_refuses_is_a_false_admission(census):
    # in-slice refusal
    assert census.bucket(("G3", "held providers disagree"),
                         ("admitted", ("G3", "held providers disagree"))) \
        == census.ADMISSION
    # out-of-slice refusal (the type layer): still a false admission, because an
    # ISSUED admission claims the reference admits — unlike a no-objection, which
    # forgives an out-of-slice refusal since it never claimed a green.
    assert census.bucket(("OUT:type mismatch", "x is Int, not Str"),
                         ("admitted", ("", "x is Int, not Str"))) \
        == census.ADMISSION
    # a bare refusal with no code is still refused, so still a false admission
    assert census.bucket(("TYPE", "ternary branches disagree"),
                         ("admitted", ("", ""))) == census.ADMISSION


def test_an_admission_the_reference_admits_agrees(census):
    assert census.bucket(("", ""), ("admitted", ("", ""))) == "agree-admit"


def test_a_no_objection_out_of_slice_is_forgiven_but_an_admission_is_not(census):
    ref = ("OUT:type mismatch", "x is Int, not Str")
    # a no-objection to an out-of-slice refusal is the documented, tolerated
    # state; the SAME reference verdict against an issued admission is not.
    assert census.bucket(ref, ("no_objection", "")) == "no-objection-out-of-slice"
    assert census.bucket(ref, ("admitted", ("", ""))) == census.ADMISSION


def test_false_admission_is_never_baselined_and_always_fails_check(census):
    assert census.ADMISSION in census.NEVER_BASELINED
    assert census.ADMISSION in census.TRACKED
    buckets = {census.ADMISSION: ["oracle-reject:some_refused_program"]}
    # never in a baseline -> compare() reports it even against a full baseline
    # that happens to list it (a hand-edited baseline cannot buy tolerance).
    problems = census.compare(buckets, {"buckets": dict(buckets)})
    assert any("FALSE ADMISSION" in p for p in problems), problems
    assert census.false_admissions(buckets) == \
        ["oracle-reject:some_refused_program"]


def test_bypasses_does_not_swallow_the_false_admission_bucket(census):
    # `false-admission` shares `false-admit` as a string prefix; the named-list
    # bypass surface must key on the exact bucket, not startswith.
    buckets = {
        "false-admit/G3": ["a.rvl"],
        census.ADMISSION: ["b.rvl"],
    }
    assert census.bypasses(buckets) == ["a.rvl"]
    assert census.false_admissions(buckets) == ["b.rvl"]


def test_run_routes_an_admitted_gate_kind_to_false_admission(census):
    """End to end through run(): an engine that ISSUES an admission for a
    reference-refused program lands in `false-admission`, with a readable
    details entry."""
    cases = [("agree", "ok"), ("bad_inslice", "r1"), ("bad_outslice", "r2")]
    refs = {
        "ok": ("", ""),
        "r1": ("G3", "held providers disagree"),
        "r2": ("OUT:type mismatch", "x is Int, not Str"),
    }

    class _Engine:
        name = "fake"

        def verdicts(self, sources):
            table = {
                "ok": ("admitted", ("", "")),
                "r1": ("admitted", ("G3", "held providers disagree")),
                "r2": ("admitted", ("", "x is Int, not Str")),
            }
            for src in sources:
                yield table[src]

    buckets, details = census.run(cases, _Engine(), lambda s: refs[s])
    assert buckets.get("agree-admit") == ["agree"]
    assert sorted(buckets.get(census.ADMISSION, [])) == ["bad_inslice",
                                                         "bad_outslice"]
    # the details map records the issued admission's code/message like a refusal
    assert details["bad_inslice"]["gate"]["kind"] == "admitted"
    assert details["bad_inslice"]["gate"]["code"] == "G3"
    assert details["bad_inslice"]["reference"]["tag"] == "G3"


def test_no_issued_admission_is_one_the_reference_refuses(measured):
    """THE release-blocking direction, measured over the whole census corpus.

    Every admission the gate ISSUES must be one the reference admits. A single
    entry here means a host could read a rust admission as a green and run code
    the reference never admitted, which is the defect class the whole
    admission-gate arc exists to prevent. Never baselined, never allowed by
    name."""
    _, (buckets, _) = measured
    assert buckets.get("false-admission", []) == [], (
        "a gate ISSUED an admission the reference refuses — this is the "
        "release-blocking direction; see docs/design/457 / issue #346")


def test_the_admission_arm_actually_fires_over_the_corpus(census, measured):
    """The other half of the guard above, and the one that keeps it from being a
    vacuum.

    A bucket that is empty because NOTHING is ever admitted proves nothing: that
    was the state before the arm opened, and it read exactly the same. So the arm
    is measured for non-vacuity too — the engine must issue real admissions over
    the corpus, and every one of them must land in `agree-admit`.

    `ADMISSION_PROGRAMS` is why there are any: every real `.rvl` in the tree
    declares a component or an `fn` body and so sits outside the admission
    surface, which would leave the arm exercised on zero inputs."""
    cases, (buckets, _) = measured
    engine = census.SelfhostEngine()
    issued = [case_id for (case_id, _), verdict
              in zip(cases, engine.verdicts(src for _, src in cases))
              if verdict[0] == "admitted"]
    assert len(issued) >= 6, (
        "the admission arm issued nothing over the census corpus, so the "
        f"false-admission guard measured nothing: {issued}")
    agreed = set(buckets.get("agree-admit", []))
    assert set(issued) <= agreed, (
        "an issued admission did not land in `agree-admit`:\n  "
        + "\n  ".join(sorted(set(issued) - agreed)))


def test_the_admission_near_misses_are_withheld(census):
    """The near misses in `ADMISSION_PROGRAMS` sit one token outside the surface,
    and two of them are the certifier's OWN obligations: the reference refuses a
    duplicate service and a duplicate method, and the native gate raises no
    objection to either, so a certifier that skipped them would issue an
    admission the reference refuses. Held here directly, by name, rather than
    only through the bucket — a corpus edit that dropped them would otherwise
    make the guard quietly weaker."""
    certify = census.build_admission_certify()
    programs = dict(census.ADMISSION_PROGRAMS)
    near_misses = [name for name in programs if name.startswith("near_miss_")]
    assert len(near_misses) >= 8, near_misses
    for name in near_misses:
        assert not certify(programs[name]), (
            f"{name} is a NEAR MISS and must not be certified")
    inside = [name for name in programs if not name.startswith("near_miss_")]
    for name in inside:
        assert certify(programs[name]), (
            f"{name} is inside the surface and must be certified")


def test_the_admission_mirror_matches_the_rust(census):
    """The census's fast engine reads the crate's admission arm through a python
    mirror of `crates/revl-gate/src/admission.rs::certify`. Its TABLES are
    imported from the generator, so only the WALK can drift — held here against
    the cases `admission.rs`'s own unit tests state, plus the derived
    vocabularies the walk is written over."""
    admission_rs = (ROOT / "crates" / "revl-gate" / "src" / "admission.rs") \
        .read_text(encoding="utf-8")
    assert "fn certify(" in admission_rs and "fn certify_into(" in admission_rs, \
        "admission.rs no longer has the certifier this mirror was written against"

    certify = census.build_admission_certify()
    # `an_interface_only_source_is_certified`
    assert certify("service Store {\n  fn get(key: Str) -> Str\n"
                   "  fn put(key: Str, value: Str)\n}\n")
    # `the_empty_source_is_the_empty_composition_and_is_certified`
    for source in ("", "   \n", "// just a note\n"):
        assert certify(source)
    # `a_scalar_alias_is_certified_and_usable_in_a_signature`
    assert certify("type Key = Str\nservice S {\n  fn get(k: Key) -> Key\n}\n")
    # `an_alias_of_an_alias_is_not_certified`
    assert not certify("type A = Str\ntype B = A\n")
    # `a_term_of_any_kind_leaves_the_surface`
    for source in (
        "fn id(x: Int) -> Int { return x }",
        "component C provides s: S {\n  provide s {\n    fn f(x) = x\n  }\n}\n",
        "type R = { id: Int }",
        "service S {\n  fn f(x: List[Int]) -> Int\n}\n",
        'use "./other.rvl" { S }\n',
        "pub service S {\n  fn f(x: Int) -> Int\n}\n",
    ):
        assert not certify(source), source
    # `the_two_obligations_the_native_gate_does_not_carry`
    assert not certify("service A {\n  fn f(x: Int) -> Int\n}\n"
                       "service A {\n  fn g(x: Int) -> Int\n}\n")
    assert not certify("service A {\n  fn f(x: Int) -> Int\n"
                       "  fn f(y: Int) -> Int\n}\n")
    # `a_declaration_may_not_shadow_a_reference_builtin_type`
    for source in ("service Int {\n}\n", "type Opt = Str\n", "type Principal = Str\n"):
        assert not certify(source), source
    # `a_type_outside_the_scalar_vocabulary_leaves_the_surface`
    assert not certify("service S {\n  fn f(x: Unknown) -> Int\n}\n")
    assert not certify("service S {\n  fn f(x: Any) -> Int\n}\n")
    # `a_non_ascii_byte_leaves_the_surface`
    assert not certify("service \u00dcnicode {}")
    assert certify("service S {\n  fn f(x: Str) -> Str // caf\u00e9\n}\n")
    # `a_duplicate_parameter_name_leaves_the_surface`
    assert not certify("service S {\n  fn f(x: Int, x: Int) -> Int\n}\n")
    # `an_unterminated_declaration_leaves_the_surface`
    for source in ("service S {", "service S {\n  fn f(x: Int\n}", "type A =", "service"):
        assert not certify(source), source

    # --- the provide-method half (issue #346, docs/design/457 T6) ---
    # `a_self_contained_component_is_certified`
    assert certify(
        "service Store {\n  fn get(key: Str) -> Str\n}\n"
        "service Cache {\n  fn lookup(key: Str) -> Str\n}\n"
        "component Kv provides store: Store {\n"
        "  provide store { fn get(key) = key }\n}\n"
        "component CacheLayer requires store: Store provides cache: Cache {\n"
        "  provide cache { fn lookup(key) = store.get(key) }\n}\n")
    # The RETURN obligation. This one is NOT in `ADMISSION_PROGRAMS`: the
    # reference refuses it `T1` and `admit_src` raises no objection, so as a
    # corpus entry it would widen the fail-open `false-admit` baseline with a
    # program that is not in the tree. The certifier's duty to withhold it is
    # real either way, so it is held here by hand.
    assert not certify(
        "service S {\n  fn a(x: Int) -> Str\n}\n"
        "component C provides s: S {\n  provide s { fn a(x) = x }\n}\n")
    # ... and its argument, arity and member twins, which need a second service
    # to reach
    for body in ("fn a(x) = p.two(x, x)", "fn a(x) = p.two(x)",
                 "fn a(x) = p.missing(x)", "fn a(x) = q.one(x)"):
        assert not certify(
            "service P {\n  fn one(n: Int) -> Int\n  fn two(n: Int, m: Str) -> Int\n}\n"
            "service S {\n  fn a(x: Int) -> Int\n}\n"
            f"component C requires p: P provides s: S {{\n  provide s {{ {body} }}\n}}\n"
        ), body
    assert certify(
        "service P {\n  fn one(n: Int) -> Int\n  fn two(n: Int, m: Str) -> Int\n}\n"
        "service S {\n  fn a(x: Int) -> Int\n}\n"
        "component C requires p: P provides s: S {\n  provide s { fn a(x) = p.one(x) }\n}\n")
    # `a_provide_block_must_implement_exactly_the_declared_operations`
    for source in (
        "service S {\n  fn a(x: Int) -> Int\n  fn b(x: Int) -> Int\n}\n"
        "component C provides s: S {\n  provide s { fn a(x) = x }\n}\n",
        "service S {\n  fn a(x: Int) -> Int\n}\n"
        "component C provides s: S {\n  provide s { fn a(x) = x\n fn c(x) = x }\n}\n",
        "service S {\n  fn a(x: Int) -> Int\n}\n"
        "component C provides s: S {\n}\n",
    ):
        assert not certify(source), source
    # `a_body_form_the_surface_does_not_type_is_not_certified`
    for body in ("fn a(x) = 1", "fn a(x) { return x }", "fn a(x) -> Int = x",
                 "fn a(x: Int) = x", "fn a(y) = y", "fn a(x) = other",
                 "fn a(x) = { x }"):
        assert not certify(
            "service S {\n  fn a(x: Int) -> Int\n}\n"
            f"component C provides s: S {{\n  provide s {{ {body} }}\n}}\n"), body
    # `a_marked_service_operation_is_unspellable_in_a_certified_source`
    for source in ("service S {\n  emission fn put(key: Str, value: Str)\n}\n",
                   "service S {\n  async fn go(n: Int) -> Int\n}\n"):
        assert not certify(source), source
    # `a_name_repeated_across_two_namespaces_is_left_to_the_reference`
    for source in ("service A {\n  fn f(x: Int) -> Int\n}\ncomponent A {\n}\n",
                   "type A = Str\ncomponent A {\n}\n",
                   "component A {\n}\ncomponent A {\n}\n"):
        assert not certify(source), source
    # `an_alias_in_an_implemented_signature_is_left_to_the_reference`
    assert not certify(
        "type Key = Str\nservice Cache {\n  fn lookup(key: Key) -> Key\n}\n"
        "component C provides cache: Cache {\n  provide cache { fn lookup(key) = key }\n}\n")

    # and the tables the walk is written over are the generator's own, carried
    # into the rust verbatim
    spec = importlib.util.spec_from_file_location(
        "census_generator_admission", ROOT / "tools" / "build_gate_crate.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    tables = generator.admission_tables()
    assert "Int" in tables["scalars"] and "Opt" not in tables["scalars"]
    for name in tables["scalars"]:
        assert f'    "{name}",' in admission_rs
    # the surface id is computed by build.rs from the crate sources (#1768)
    assert ('concat!("admission-interface:", env!("REVL_GATE_IDENTITY"))'
            in admission_rs)


def test_the_frontier_mirror_matches_the_rust(census):
    """The census's fast engine reads the crate's frontier guard through a
    python mirror of `crates/revl-gate/src/frontier.rs::scan`. Its TABLES are
    imported from the generator, so only the scanning can drift — held here
    against the cases `frontier.rs`'s own unit tests state."""
    frontier = (ROOT / "crates" / "revl-gate" / "src" / "frontier.rs").read_text()
    assert "fn strip_literals" in frontier, \
        "frontier.rs no longer has the scan this mirror was written against"

    scan = census.build_frontier_scan()
    # a literal or a comment cannot trigger the scan (frontier.rs's
    # `a_literal_cannot_trigger_the_scan`)
    assert scan('fn f() -> Str { return ".is_digit" }') is None
    assert scan("// .is_digit()\nfn f(x: Int) -> Int { return x }") is None
    # an excluded builtin in member position is a gap
    # (`an_excluded_builtin_in_member_position_is_a_gap`)
    spec = importlib.util.spec_from_file_location(
        "census_generator", ROOT / "tools" / "build_gate_crate.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    tables = generator.frontier_tables()
    if tables["builtins"]:
        name = tables["builtins"][0]
        assert scan(f"fn f(x: Str) -> Bool {{ return x.{name}() }}") is not None
    # an oversized source is a gap (`an_oversized_source_is_a_gap`)
    assert scan("x" * (262145)) is not None
    # and so is one with too many items at a single bracket level
    # (`too_many_items_at_one_level_is_a_gap`)
    flat = "fn f() -> Int { return g(%s) }" % ", ".join(
        "1" for _ in range(generator.MAX_LEVEL_ITEMS + 1))
    assert len(flat) < generator.MAX_SOURCE_BYTES // 10
    assert scan(flat) is not None
    # a reserved capability namespace is a gap
    # (`a_reserved_capability_namespace_is_a_gap`). The mirror lacked this arm
    # until the corpus gained a computer-use document (issue #1369), when the
    # cheap engine said no-objection where the crate declined.
    if tables["capability_roots"]:
        root = tables["capability_roots"][0]
        assert scan(f"extern emission[{root}.click] fn c(t: Str) = @py {{ pass }}") \
            is not None
        # a root not followed by `.` is an ordinary word
        assert scan(f"fn {root}(x: Int) -> Int {{ return x }}") is None


def test_every_sibling_tool_is_loaded_relative_to_this_tool():
    """`ROOT` is the tree under measurement, and a caller may redirect it.

    The census loads several of its own sibling modules by path. Resolving
    one of those off `ROOT` asks the MEASURED tree for a module that belongs
    to the census itself, and a caller that points `ROOT` at a prepared tree
    then gets a `FileNotFoundError` from a tool it never asked about. That is
    what item 542 hit the moment it gave `main()` a provenance line to print:
    `--record` against a prepared tree raised on `tools/corpus_provenance.py`
    and took `tests/test_evolution_reward.py` red with it.

    Structural rather than behavioural, because the behavioural case only
    reaches the modules one code path happens to load, and the next sibling
    tool will be loaded from somewhere else. `backends/python/emit.py` stays
    on `ROOT` on purpose: the emitter is the SUBJECT of the census, part of
    the tree being measured, not part of the census.
    """
    import ast

    tree = ast.parse((ROOT / "tools" / "gate_reference_census.py").read_text(
        encoding="utf-8"))

    def spelled(node):
        """`("ROOT", ["tools", "x.py"])` for `ROOT / "tools" / "x.py"`."""
        parts = []
        while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            if not isinstance(node.right, ast.Constant) \
                    or not isinstance(node.right.value, str):
                return None, []
            parts.append(node.right.value)
            node = node.left
        if not isinstance(node, ast.Name):
            return None, []
        return node.id, list(reversed(parts))

    off_the_measured_tree, off_this_tool = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) \
                or not isinstance(node.func, ast.Attribute) \
                or node.func.attr != "spec_from_file_location":
            continue
        for arg in node.args:
            base, parts = spelled(arg)
            if base == "TOOLS":
                off_this_tool.append("/".join(parts))
            elif base == "ROOT" and parts[:1] == ["tools"]:
                off_the_measured_tree.append("/".join(parts))

    assert not off_the_measured_tree, (
        "these sibling tools are resolved off the tree under measurement "
        "rather than off this tool, so a redirected ROOT looks for them "
        "inside the measured tree: " + ", ".join(sorted(
            set(off_the_measured_tree))))
    # Not vacuous: a rename of the helper, or a census that stopped loading
    # siblings by path, would satisfy the assertion above without this file
    # having checked a single load.
    assert off_this_tool, (
        "no sibling tool is loaded off TOOLS; this test checked nothing")
