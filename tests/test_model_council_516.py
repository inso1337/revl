"""A model council with role-bound members (roadmap item 516, issue #1190).

The executable spec for slice 1 of `docs/design/543-model-council.md`: the
`model council` declaration, its members, its `aggregate` clause, and every
refusal the three carry.

Slice 1's programs are written INLINE rather than dropped in `examples/` or
`tests/fixtures/`, which was the right call when they were written. Both
directories are census corpus roots (`tools/gate_reference_census.py`
CORPUS_DIRS), and while the gate could not read a council an admitting fixture
in either would have been a `false-reject` entry the moment it landed.

The rule that generalises from it, and that the two sections at the foot of
this file follow in opposite directions, is: **a fixture goes on disk when the
corpora that read that directory can decide it.** The self-host port is slice
5 and it LANDED (`docs/design/556-model-council-selfhost.md`), so the gate
decides a council declaration and, since slice 2, an arm that names one -
which is why the SLICE 2 section reads its two programs off `examples/`.
Slice 3's rule fires on a `match` in a function body, a flow position the gate
has no walk for, and its admitting program carries a generic ADT the go tier
cannot emit; so the SLICE 3 section is inline, and its own header says which
corpus each of those two facts would have mislead.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.diagnostics import GUARANTEES, classify  # noqa: E402

# Spelled here rather than imported from `revl.model_council`, so this module
# still COLLECTS against a tree that has no such module. That is what makes the
# non-vacuity run readable: on the tree without this change the controls pass
# and every council test fails, instead of the whole file erroring at import
# time. `test_the_module_constants_match` is the tie-back.
# Item 1190 / `docs/design/557-council-disagreement.md` split the council's
# refusals across two codes. `CODE` is the disagreement family, which is almost
# all of them; `PLACE_CODE` is the two whose subject is a `model role` and whose
# rewrite is therefore item 512's.
CODE = "G-COUNCIL-SPLIT"
PLACE_CODE = "G-MODEL-PLACE"
FUNCTIONS = ("proposer", "adversary", "verifier")
AGGREGATIONS = ("unanimous", "majority", "veto")

# Three distinct roles, two on the device and one off it. Every council below
# is built over these, so a member's residence is never in doubt.
ROLES = """
model role edge on_device
model role aux  on_device
model role vast off_device
"""

TAIL = """
service Plan { fn review(plan: Str) -> Str }

component Reviewer provides out: Plan {
  provide out { fn review(plan) = plan }
}
"""


def council(body: str) -> str:
    """A program whose only council is `body`, over the three roles above."""
    return f"{ROLES}\nmodel council Release {{\n{body}\n}}\n{TAIL}"


# The flagship: three members, each placed separately, and a rule written down.
# It compiles, and every refusal below is one edit away from it.
ADMITTED = """
  proposer  -> vast,
  adversary -> edge,
  verifier  -> aux,
  aggregate unanimous
"""

# The CONTROL: the same program with no council at all. It compiles on this
# tree and on the tree without this change, so a red here is the harness and
# not the feature.
NO_COUNCIL = ROLES + TAIL


def refusal(source: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(source, "council.revl")
    return excinfo.value


# ---------------------------------------------------------------------------
# the control, and the admitted shape
# ---------------------------------------------------------------------------

def test_a_program_with_no_council_compiles():
    """The control. Nothing here depends on the feature."""
    compile_source(NO_COUNCIL, "council.revl")


def test_the_flagship_council_compiles():
    compile_source(council(ADMITTED), "council.revl")


def test_a_two_member_council_compiles():
    compile_source(
        council("  proposer -> vast,\n  adversary -> edge,\n"
                "  aggregate unanimous"),
        "council.revl")


def test_the_optional_clauses_may_be_written_explicitly():
    """`quorum declared` and `on_tie split` are the defaults, spelled out."""
    compile_source(
        council("  proposer -> vast,\n  adversary -> edge,\n"
                "  aggregate majority quorum declared on_tie split"),
        "council.revl")


def test_on_tie_deny_is_a_permitted_narrowing():
    """A council may decide that inconclusive means refuse."""
    compile_source(
        council("  proposer -> vast,\n  adversary -> edge,\n"
                "  aggregate unanimous on_tie deny"),
        "council.revl")


@pytest.mark.parametrize("rule", AGGREGATIONS)
def test_every_rule_in_the_vocabulary_is_admitted(rule):
    compile_source(
        council(f"  proposer -> vast,\n  adversary -> edge,\n"
                f"  aggregate {rule}"),
        "council.revl")


def test_a_trailing_comma_after_the_aggregate_is_allowed():
    compile_source(
        council("  proposer -> vast,\n  adversary -> edge,\n"
                "  aggregate unanimous,"),
        "council.revl")


def test_model_is_still_an_ordinary_identifier():
    """`model` heads a declaration only in the two shapes it heads.

    `requires model: Model` is the common spelling for the key this whole item
    is about, so a program using `model` as a name has to keep parsing.
    """
    compile_source("""
service Completions { fn complete(p: Str) -> Str }
service Plan { fn review(plan: Str) -> Str }

component Reviewer requires model: Completions provides out: Plan {
  provide out { fn review(plan) = plan }
}
""", "council.revl")


def test_council_is_still_an_ordinary_identifier():
    compile_source("""
service Plan { fn review(plan: Str) -> Str }

component Reviewer provides out: Plan {
  provide out { fn review(council) = council }
}
""", "council.revl")


# ---------------------------------------------------------------------------
# the roadmap's exit test for the declaration half
# ---------------------------------------------------------------------------

def test_an_aggregation_that_admits_on_a_tie_is_refused():
    """The roadmap's own exit clause, verbatim."""
    err = refusal(council("  proposer -> vast,\n  adversary -> edge,\n"
                          "  aggregate majority on_tie allow"))
    assert "on_tie allow" in str(err)
    assert "admits when the members disagree" in str(err)
    assert classify(err)["code"] == CODE


@pytest.mark.parametrize("outcome", ["allow", "admit", "proceed", "accept",
                                     "first", "any"])
def test_every_admitting_tie_outcome_is_refused_by_name(outcome):
    """Refused BY NAME, not as a syntax error: the author gets the reason."""
    err = refusal(council(f"  proposer -> vast,\n  adversary -> edge,\n"
                          f"  aggregate majority on_tie {outcome}"))
    assert classify(err)["code"] == CODE
    assert "admits when the members disagree" in str(err)
    # the hint names both admitted outcomes, so the fix needs no design doc
    assert "`split`" in str(err) and "`deny`" in str(err)


def test_an_unknown_tie_outcome_is_a_closed_vocabulary_refusal():
    err = refusal(council("  proposer -> vast,\n  adversary -> edge,\n"
                          "  aggregate majority on_tie maybe"))
    assert "unknown tie outcome `maybe`" in str(err)
    assert classify(err)["code"] == CODE


# ---------------------------------------------------------------------------
# the aggregation rule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rule", ["first", "any", "fastest", "cheapest",
                                  "best", "random"])
def test_a_rule_that_picks_one_member_is_refused_by_name(rule):
    err = refusal(council(f"  proposer -> vast,\n  adversary -> edge,\n"
                          f"  aggregate {rule}"))
    assert f"`aggregate {rule}`" in str(err)
    assert "resolves disagreement toward one member's answer" in str(err)
    assert classify(err)["code"] == CODE


def test_an_unknown_rule_is_a_closed_vocabulary_refusal():
    err = refusal(council("  proposer -> vast,\n  adversary -> edge,\n"
                          "  aggregate averaged"))
    assert "unknown aggregation rule `averaged`" in str(err)
    assert classify(err)["code"] == CODE


def test_a_council_with_no_aggregate_is_refused():
    """"The aggregation function is total and must be written down"."""
    err = refusal(council("  proposer -> vast,\n  adversary -> edge"))
    assert "declares no `aggregate` rule" in str(err)
    assert classify(err)["code"] == CODE


def test_a_council_with_two_aggregates_is_refused():
    err = refusal(council("  proposer -> vast,\n  adversary -> edge,\n"
                          "  aggregate unanimous,\n  aggregate majority"))
    assert "declares two `aggregate` rules" in str(err)
    assert "`unanimous`" in str(err) and "`majority`" in str(err)
    assert classify(err)["code"] == CODE


def test_veto_without_an_adversary_is_refused():
    err = refusal(council("  proposer -> vast,\n  verifier -> edge,\n"
                          "  aggregate veto"))
    assert "`aggregate veto`" in str(err)
    assert "no `adversary`" in str(err)
    assert classify(err)["code"] == CODE


# ---------------------------------------------------------------------------
# the quorum basis: abstention must not shrink the council
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("basis", ["answered", "reachable", "available",
                                   "responding"])
def test_a_floor_over_the_answering_set_is_refused(basis):
    """The roadmap's second refusal: an aggregation that ignores an
    unreachable member."""
    err = refusal(council(f"  proposer -> vast,\n  adversary -> edge,\n"
                          f"  aggregate majority quorum {basis}"))
    assert f"`quorum {basis}`" in str(err)
    assert "over the members that answered" in str(err)
    assert classify(err)["code"] == CODE


def test_an_unknown_quorum_basis_is_a_closed_vocabulary_refusal():
    err = refusal(council("  proposer -> vast,\n  adversary -> edge,\n"
                          "  aggregate majority quorum most"))
    assert "unknown quorum basis `most`" in str(err)
    assert classify(err)["code"] == CODE


# ---------------------------------------------------------------------------
# the members
# ---------------------------------------------------------------------------

def test_a_member_with_no_declared_role_is_refused():
    """The council whose members cannot all be placed."""
    err = refusal(council("  proposer -> nowhere,\n  adversary -> edge,\n"
                          "  aggregate unanimous"))
    assert "names no action" not in str(err)
    assert "model role `nowhere`, which is not declared" in str(err)
    assert "edge" in str(err) and "vast" in str(err)  # the known roles
    # A `model role` is the subject, so the rewrite is item 512's: declare it.
    assert classify(err)["code"] == PLACE_CODE


def test_an_unknown_council_function_is_refused():
    """The member with no role in the aggregation."""
    err = refusal(council("  proposer -> vast,\n  auditor -> edge,\n"
                          "  aggregate unanimous"))
    assert "unknown council function `auditor`" in str(err)
    for function in FUNCTIONS:
        assert function in str(err)
    assert classify(err)["code"] == CODE


def test_a_repeated_function_is_refused():
    err = refusal(council("  proposer -> vast,\n  proposer -> edge,\n"
                          "  aggregate unanimous"))
    assert "council function `proposer` is declared twice" in str(err)
    assert classify(err)["code"] == CODE


def test_two_members_on_one_role_is_refused():
    """One model answering twice under two names: correlated errors, and no
    separate placement. It is also what keeps item 517's
    `(component, step_index, role)` unique."""
    err = refusal(council("  proposer -> vast,\n  adversary -> edge,\n"
                          "  verifier -> edge,\n  aggregate unanimous"))
    assert "are both placed on model role `edge`" in str(err)
    assert classify(err)["code"] == CODE


def test_a_one_member_council_is_refused():
    err = refusal(council("  proposer -> vast,\n  aggregate unanimous"))
    assert "declares 1 member" in str(err)
    assert classify(err)["code"] == CODE


def test_a_council_with_no_members_is_refused():
    err = refusal(council("  aggregate unanimous"))
    assert "declares 0 members" in str(err)
    assert classify(err)["code"] == CODE


def test_a_council_with_no_proposer_is_refused():
    err = refusal(council("  adversary -> edge,\n  verifier -> aux,\n"
                          "  aggregate unanimous"))
    assert "declares no `proposer`" in str(err)
    assert classify(err)["code"] == CODE


# ---------------------------------------------------------------------------
# the council's name
# ---------------------------------------------------------------------------

def test_a_council_declared_twice_is_refused():
    source = (ROLES
              + "\nmodel council Release {\n  proposer -> vast,\n"
                "  adversary -> edge,\n  aggregate unanimous\n}\n"
              + "\nmodel council Release {\n  proposer -> vast,\n"
                "  adversary -> aux,\n  aggregate majority\n}\n"
              + TAIL)
    err = refusal(source)
    assert "is declared twice" in str(err)
    assert classify(err)["code"] == CODE


def test_a_council_named_after_a_role_is_refused():
    source = (ROLES
              + "\nmodel council edge {\n  proposer -> vast,\n"
                "  adversary -> edge,\n  aggregate unanimous\n}\n"
              + TAIL)
    err = refusal(source)
    assert "has the name of the model role" in str(err)
    # Two placement tables would both answer the word, so this one is 512's too.
    assert classify(err)["code"] == PLACE_CODE


# ---------------------------------------------------------------------------
# what the declaration does NOT do
# ---------------------------------------------------------------------------

def test_an_admitted_council_writes_no_ir():
    """Slice 1 is a declaration checked at admission, not a runtime selection.

    An admitted program is byte-identical to the same program with the council
    deleted, which is what keeps every emitter, every golden and the manifest
    out of this item.
    """
    with_council = compile_source(council(ADMITTED), "council.revl")
    without = compile_source(NO_COUNCIL, "council.revl")
    assert with_council == without


def test_the_guarantee_codes_are_registered():
    """Slice 1 filed every council refusal under `G-MODEL-PLACE`, on the
    reading that the council is the placement family's second construct.

    Item 1190 reversed that for the disagreement rules and
    `docs/design/557-council-disagreement.md` section 2 gives the reason:
    `classify()` hands an agent a `fix` line, item 512's says to route the
    origin to an `on_device` role, and that is not the rewrite for `on_tie
    allow`. Both codes are registered; which refusal carries which is
    `tests/test_council_disagreement_1190.py`.
    """
    assert CODE in GUARANTEES
    assert PLACE_CODE in GUARANTEES


# ---------------------------------------------------------------------------
# the tie-back: the constants above are the module's own
# ---------------------------------------------------------------------------

def test_the_module_constants_match():
    from revl import model_council

    assert model_council.CODE == CODE
    assert model_council.PLACE_CODE == PLACE_CODE
    assert model_council.FUNCTIONS == FUNCTIONS
    assert model_council.AGGREGATIONS == AGGREGATIONS
    assert model_council.QUORUM_BASES == ("declared",)
    assert model_council.TIE_OUTCOMES == ("split", "deny")


def test_the_council_ceiling_is_the_most_permissive_member():
    """A council's residence is the JOIN of its members', not the meet.

    Giving an input to a council gives it to every member, so a council with
    one `off_device` member is `off_device`. This is the one number slice 2
    reads, and computing it the other way round is the fail-open reading.
    """
    from revl import model_council
    from revl.parser import Parser

    mixed = model_council.check(Parser(council(ADMITTED), "council.revl").parse())
    assert mixed["Release"].residence == "off_device"

    local_only = model_council.check(Parser(
        council("  proposer -> edge,\n  adversary -> aux,\n"
                "  aggregate unanimous"), "council.revl").parse())
    assert local_only["Release"].residence == "on_device"


def test_the_floor_is_counted_over_the_declared_members():
    """A member that abstains counts against the floor, never toward it."""
    from revl import model_council
    from revl.parser import Parser

    three = model_council.check(Parser(council(ADMITTED), "council.revl").parse())
    assert three["Release"].floor == 3  # unanimous over three declared

    majority = model_council.check(Parser(
        council("  proposer -> vast,\n  adversary -> edge,\n"
                "  verifier -> aux,\n  aggregate majority"), "council.revl").parse())
    assert majority["Release"].floor == 2  # 3 // 2 + 1, over the DECLARED set


def test_the_defaults_are_the_fail_closed_readings():
    from revl import model_council
    from revl.parser import Parser

    table = model_council.check(Parser(council(ADMITTED), "council.revl").parse())
    assert table["Release"].quorum == "declared"
    assert table["Release"].on_tie == "split"


def test_a_program_with_no_council_reads_an_empty_table():
    from revl import model_council
    from revl.parser import Parser

    assert model_council.check(Parser(NO_COUNCIL, "council.revl").parse()) == {}


# ===========================================================================
# SLICE 2: binding a council to an action (issue #1366)
#
# Slice 1 checked a declaration bound to nothing. A `route model` arm may now
# name a COUNCIL where it names a role, so the council's residence is what item
# 514's ceiling reads and the members' separate placement bites on a value.
#
# The two fixtures below are in `examples/`, unlike the inline programs above,
# and the module docstring's reason no longer holds: the self-host port landed
# (slice 5, `docs/design/556-model-council-selfhost.md`) and this slice extends
# it, so the gate decides both files and neither is a census divergence.
# ===========================================================================

BINDING_CONTROL = ROOT / "examples" / "model_council_binding.rvl"
BINDING_REFUSED = (ROOT / "examples" / "rejections"
                   / "gmodelplace_council_member_off_device.rvl")

# The two fixtures differ in ONE word. Spelled here so the non-vacuity claim is
# checked rather than asserted: if an edit ever made them differ in more, the
# pair would stop being a controlled comparison and this is where that shows.
BINDING_EDIT = ("local2", "vast")


def _binding(member_role: str) -> str:
    """The control program with the proposer placed on `member_role`.

    The three roles above are declared in both files whichever one the proposer
    names, so the pair differs in the proposer's ROLE and in nothing else.
    """
    return f"""
model role edge   on_device
model role local2 on_device
model role vast   off_device

service Answer {{ fn classify(text: Str) -> Str }}

model council Release {{
  proposer  -> {member_role},
  adversary -> edge,
  aggregate unanimous
}}

component Classifier provides out: Answer {{
  route model on classify {{
    confidential -> Release
  }}
  provide out {{ fn classify(text) = text }}
}}
"""


def test_a_council_bound_to_an_action_with_an_off_device_member_is_refused():
    """The slice's exit test. A two-member council whose members are placed
    differently, bound to an action by a `confidential` arm, does not admit -
    and the diagnostic names the MEMBER, not the council."""
    err = refusal(_binding("vast"))
    assert err.code == PLACE_CODE
    assert "model council `Release`" in err.message
    assert "whose member `proposer` runs on model role `vast`" in err.message
    assert "may not leave the device" in err.message


def test_the_honest_control_with_both_members_on_device_admits():
    """The other half of the same measurement. One word apart from the program
    above, and it compiles: the rule is about the member's placement and not
    about the word `council`."""
    assert compile_source(_binding("local2"), "council.revl")


def test_the_two_checked_in_fixtures_are_the_same_one_word_apart():
    """The corpus pair, read off disk. `examples/model_council_binding.rvl`
    admits and its rejection twin does not, and the ONLY difference between the
    two programs is which role the proposer names."""
    control = BINDING_CONTROL.read_text()
    refused = BINDING_REFUSED.read_text()
    assert compile_source(control, "binding.rvl")
    err = refusal(refused)
    assert err.code == PLACE_CODE
    assert "whose member `proposer` runs on model role `vast`" in err.message

    def _code(src):
        return [line.strip() for line in src.splitlines()
                if line.strip() and not line.strip().startswith("//")]

    a, b = _code(control), _code(refused)
    assert len(a) == len(b), (a, b)
    differ = [(x, y) for x, y in zip(a, b) if x != y]
    assert differ == [(f"proposer  -> {BINDING_EDIT[0]},",
                       f"proposer  -> {BINDING_EDIT[1]},")], differ


def test_the_refusal_names_every_off_device_member_in_its_hint():
    """The message names the first off-device member; the hint names them all.
    An author who moved one of two and recompiled would otherwise learn the
    rule one member at a time."""
    err = refusal("""
model role edge on_device
model role vast off_device
model role vast2 off_device

service Answer { fn classify(text: Str) -> Str }

model council Release {
  proposer  -> vast,
  adversary -> vast2,
  verifier  -> edge,
  aggregate unanimous
}

component Classifier provides out: Answer {
  route model on classify { confidential -> Release }
  provide out { fn classify(text) = text }
}
""")
    assert "whose member `proposer` runs on model role `vast`" in err.message
    assert "`proposer` on `vast`, `adversary` on `vast2`" in err.hint


def test_the_ceiling_is_a_confidentiality_ceiling_and_nothing_more():
    """An origin outside `CEILING_ORIGINS` routed to the same split council
    admits. The rule is item 514's ceiling, not a general statement about which
    roles an action may reach."""
    assert compile_source(_binding("vast").replace(
        "confidential -> Release", "web -> Release"), "council.revl")


def test_the_catch_all_never_covers_a_confidentiality_origin_for_a_council():
    """`* -> <council>` is admitted as a written arm for the same reason
    `* -> cloud` is: `*` is DEFINED not to cover a confidentiality origin, so
    it is not the sentence that sends a confidential input to the council."""
    assert compile_source(_binding("vast").replace(
        "confidential -> Release", "* -> Release"), "council.revl")


def test_a_bound_secret_reaches_no_council_at_any_placement():
    """Item 256's rule does not weaken because the arm names a council. The
    noun in the sentence moves and nothing else does."""
    err = refusal(_binding("local2").replace(
        "confidential -> Release", "secret -> Release"))
    assert err.code == "G-SECRET-FLOW"
    assert "routes the `secret` origin to model council `Release`" in err.message


def test_an_arm_naming_neither_a_role_nor_a_council_is_still_refused():
    """The council table WIDENS what an arm may name and narrows nothing. A
    name that is neither gets item 512's sentence, byte for byte."""
    err = refusal(_binding("local2").replace(
        "confidential -> Release", "confidential -> ghost"))
    assert err.code == PLACE_CODE
    assert err.message.endswith(") names no declared model role")


def test_the_binding_reads_the_council_table_and_re_derives_nothing():
    """`model_route.check` records the council's own residence and its members
    verbatim from `model_council.check`'s table, which is the slice's stated
    contract (design note 543 section 14)."""
    from revl import model_council, model_route
    from revl.parser import Parser

    program = Parser(_binding("local2"), "council.revl").parse()
    councils = model_council.check(program)
    placed = model_route.check(program, councils=councils)
    arm = placed["Classifier"]["classify"]["confidential"]

    assert arm["council"] == "Release"
    assert arm["role"] == "Release"
    assert arm["residence"] == councils["Release"].residence == "on_device"
    assert arm["member_roles"] == tuple(
        m.role for m in councils["Release"].members)
    assert [m["function"] for m in arm["members"]] == ["proposer", "adversary"]
    assert model_route.off_device_members(arm) == ()


def test_an_action_routed_to_a_council_reaches_every_members_role():
    """A council is one question asked of every member, so a crossing placed on
    a member's role is inside the placement the block names. Refusing it would
    widen nothing and protect nothing."""
    from revl import model_council, model_route
    from revl.parser import Parser

    program = Parser(_binding("local2"), "council.revl").parse()
    placed = model_route.check(program,
                               councils=model_council.check(program))
    reach = model_route.reach_of(placed["Classifier"]["classify"])
    assert reach == frozenset({"Release", "local2", "edge"})


def test_the_binding_costs_the_ir_nothing():
    """A council still writes no IR, and neither does the arm that names it
    (item 512 writes none either). The bound program emits byte for byte what
    the same program emits with the council and the route deleted."""
    bound = _binding("local2")
    plain = """
model role edge   on_device
model role local2 on_device
model role vast   off_device

service Answer { fn classify(text: Str) -> Str }

component Classifier provides out: Answer {
  provide out { fn classify(text) = text }
}
"""
    assert compile_source(bound, "council.revl") == \
        compile_source(plain, "council.revl")


def test_the_value_side_says_council_where_the_arm_named_one():
    """Item 514's ceiling fires on a VALUE, and its sentence has to name the
    thing the arm named. `* -> <council>` places no confidential value, the
    same way `* -> cloud` places none, and an author sent to a `model role
    Release` declaration that does not exist would be sent nowhere."""
    src = """
model role edge   on_device
model role local2 on_device

extern emission[model.complete] fn prompt(p: Secret[Str]) -> Int = @py { return 0 }

service Answer { emission fn summarize(d: Str) -> Int }

model council Release {
  proposer  -> local2,
  adversary -> edge,
  aggregate unanimous
}

component Summarizer provides out: Answer {
  config { doc: Secret[Str] }
  route model on summarize { * -> Release }
  provide out {
    fn summarize(d) {
      let r = prompt(config.doc)
      return 0
    }
  }
}
"""
    err = refusal(src)
    assert err.code == PLACE_CODE
    assert "the catch-all `*` routes to model council `Release`" in err.message
    assert "model role `Release`" not in err.message


# ---------------------------------------------------------------------------
# SLICE 3: `Aggregate[T]` and the exhaustiveness rule (issue #1367)
#
# Slice 1 checked the declaration and slice 2 bound a council to an action.
# Neither produced a value, so the roadmap's first exit clause - "a two-member
# council that disagrees does not admit" - had no call site to be true at:
# `Aggregate[` occurred once in all of `src/revl/`, in a comment. These tests
# are that clause, at the call site.
#
# The pair below is INLINE, the way slice 1's programs are and unlike slice
# 2's, and the reason is the one this file's header already gives for slice 1:
# a fixture on disk is a claim about tiers that have to be able to decide it,
# and this rule's tiers cannot.
#
#   * `examples/rejections/` is the TIER REPRODUCER set
#     (`tools/tier_guarantees.py` globs exactly it). A fixture there asserts
#     that every tier's verdict on it is measured. This refusal fires on a
#     `match` inside a function body, which is a flow position the self-host
#     gate has no walk for, so it would turn the whole `G-COUNCIL-SPLIT` row's
#     `revl` column into a divergence for a reason that has nothing to do with
#     the declaration refusal that row is about. Item 514's value-side ceiling
#     is in the same position and is kept out of that directory for the same
#     reason, which is why `G-MODEL-PLACE` reads `proved`: both
#     `gmodelplace_*` fixtures there are declaration-half refusals.
#   * `examples/` is carried by the GO tier
#     (`tests/test_go_carried_set_builds.py` walks the whole tree for any
#     document with a top-level `types` block beside an observable component).
#     A generic ADT emits Go that does not build - `type BoxHeld struct
#     { Value T }` with `T` undefined - and that is PRE-EXISTING and general,
#     reproduced with a plain `type Box[T] = Held(T) | Empty`. Carrying a
#     fixture through it would add a ratchet entry to
#     `tests/fixtures/go_carried_build_known_bad.json` for a defect this slice
#     did not cause.
#
# The measurement is not weakened by being inline: the twin is DERIVED from
# the control by the one documented substitution, so "the same program one arm
# apart" is a property of the construction rather than of two files that were
# edited to look alike.
# ---------------------------------------------------------------------------

#: The one arm that differs between the control and its twin.
ANSWER_EDIT = ('      Split(d)     => "held: the members did not agree",',
               '      _            => "held: the members did not agree",')

ANSWER_CONTROL_SRC = """
model role edge   on_device
model role local2 on_device

service Review {
  fn propose(plan: Str) -> Str
  fn settle(answer: Aggregate[Str]) -> Str
}

model council Release {
  proposer  -> local2,
  adversary -> edge,
  aggregate unanimous quorum declared on_tie split
}

component Reviewer provides out: Review {
  route model on propose {
    confidential -> Release
  }
  provide out {
    fn propose(plan) = plan
    fn settle(answer) = match answer {
      Agreed(v)    => v,
      Split(d)     => "held: the members did not agree",
      Inquorate(d) => "held: the council did not answer",
    }
  }
}
"""

#: The twin, DERIVED rather than written: one arm that named `Split` now names
#: `_`. Everything else is the same bytes by construction.
ANSWER_REFUSED_SRC = ANSWER_CONTROL_SRC.replace(*ANSWER_EDIT)

#: The three constructors of `Aggregate[T]`, and the only three.
AGGREGATE_CASES = ("Agreed", "Split", "Inquorate")

#: The two a consumer may not leave unnamed.
DISSENT_ARMS = ("Split", "Inquorate")


def _consumer(arms: str, returns: str = "Str",
              scrutinee: str = "Aggregate[Str]") -> str:
    """A two-member council bound to an action, plus a consumer of its answer.

    The council is real and bound: `propose` is routed to it, both members are
    placed separately and on the device, so nothing here is refused for a
    slice-1 or slice-2 reason and a refusal is about the ARMS.
    """
    return f"""
model role edge   on_device
model role local2 on_device

service Review {{
  fn propose(plan: Str) -> Str
  fn settle(answer: {scrutinee}) -> {returns}
}}

model council Release {{
  proposer  -> local2,
  adversary -> edge,
  aggregate unanimous quorum declared on_tie split
}}

component Reviewer provides out: Review {{
  route model on propose {{
    confidential -> Release
  }}
  provide out {{
    fn propose(plan) = plan
    fn settle(answer) = match answer {{
{arms}
    }}
  }}
}}
"""


ALL_ARMS = """      Agreed(v)    => v,
      Split(d)     => "held",
      Inquorate(d) => "held","""


def test_a_consumer_that_handles_every_arm_compiles():
    """The control for this whole section. A council bound to an action and a
    consumer that names all three constructors admits, so every refusal below
    is one edit away from a program that compiles."""
    assert compile_source(_consumer(ALL_ARMS), "answer.revl")


def test_a_consumer_that_handles_only_agreed_is_refused():
    """The roadmap's first exit clause, at the call site. A caller that reads
    the agreed value and writes nothing for the disagreement does not compile,
    and the refusal NAMES the arms it left out rather than saying the match is
    incomplete."""
    err = refusal(_consumer("      Agreed(v) => v,"))
    assert err.code == CODE
    for arm in DISSENT_ARMS:
        assert f"`{arm}`" in err.message, err.message
    assert "Aggregate[Str]" in err.message


def test_a_wildcard_does_not_stand_in_for_the_dissent_arms():
    """The half that makes the rule bite rather than be spelled.

    Everywhere else in revl a `_` arm satisfies exhaustiveness, and for an
    ordinary ADT it should. Here the case a wildcard would swallow is the one
    the construct exists to report: `_ => <proceed>` is `on_tie allow` written
    at the call site, where the declaration checker that refuses `on_tie
    allow` by name was not looking. If this test ever passes by admitting, the
    answer type has stopped carrying the guarantee."""
    err = refusal(_consumer('      Agreed(v) => v,\n      _ => "held",'))
    assert err.code == CODE
    for arm in DISSENT_ARMS:
        assert f"`{arm}`" in err.message, err.message
    assert "`_` arm does not answer for" in err.hint


def test_the_refusal_names_only_the_arm_that_is_missing():
    """A consumer that handled `Split` and forgot `Inquorate` is told about
    `Inquorate`, not about both. The two are separate constructors because
    `Split` is information about the QUESTION and `Inquorate` is information
    about the DEPLOYMENT (design note 543 section 5), and a refusal that
    named both would be telling the author to rewrite an arm that is right."""
    err = refusal(_consumer(
        '      Agreed(v) => v,\n      Split(d) => "held",'))
    assert err.code == CODE
    assert "`Inquorate`" in err.message
    assert "`Split`" not in err.message.split("leaves")[1]


@pytest.mark.parametrize("arm", DISSENT_ARMS)
def test_a_dissent_path_cannot_reach_a_value(arm):
    """"`Split` carries no `T`" is the type-level statement of the whole item
    (design note 543 section 3), and this is where it is a property rather
    than prose. There is no total projection `Aggregate[T] -> T`: the arm
    binds the per-member dissent record, so returning the payload from a
    function that must produce a `Str` does not typecheck.

    Without this the rule would be satisfiable by writing the arm and reading
    the value straight back out of it, which is `aggregate first` spelled at
    the call site."""
    arms = "\n".join(
        f"      {case}(x) => x," if case == arm
        else (f"      {case}(x) => x," if case == "Agreed"
              else f'      {case}(x) => "held",')
        for case in AGGREGATE_CASES)
    err = refusal(_consumer(arms))
    assert "List[DissentEntry]" in err.message, err.message
    assert "`Str`" in err.message


def test_agreed_binds_the_boundary_type():
    """The other side of the same coin: `Agreed` is the one constructor that
    DOES carry the value, and it carries the item-257 boundary type the
    council was asked for. A council over `Aggregate[Int]` hands its caller an
    `Int`, so the payload is usable without a cast and the type argument is
    not decoration."""
    assert compile_source(
        _consumer("""      Agreed(v)    => v + 1,
      Split(d)     => 0,
      Inquorate(d) => 0,""", returns="Int", scrutinee="Aggregate[Int]"),
        "answer.revl")
    # and the SAME program over `Aggregate[Str]` does not, because `v` is then
    # a `Str`: the argument is what decides, not the arm.
    err = refusal(_consumer("""      Agreed(v)    => v + 1,
      Split(d)     => 0,
      Inquorate(d) => 0,""", returns="Int"))
    assert "expects `Str`" in err.message or "`Int`" in err.message


def test_the_dissent_record_is_per_member_and_readable():
    """`Dissent` is a list of per-member rows, not an opaque blob: a caller
    that wants to report WHICH member said what can. Design note 543 section 3
    is the shape, and the answers in it are digests, which is why reading one
    does not give the caller a `T`.

    Written as a module `fn` rather than inside a `provide` body because a
    match-arm payload does not pin a method receiver there - a PRE-EXISTING
    limit that has nothing to do with this slice, pinned as parity by
    `test_a_dissent_payload_dispatches_like_any_other_adt_payload` below so a
    later reader does not mistake it for a property of the answer type."""
    assert compile_source("""
fn settle(a: Aggregate[Str]) -> Int {
  return match a {
    Agreed(v)    => 0,
    Split(d)     => d.length(),
    Inquorate(d) => d.length(),
  }
}
""", "answer.revl")


def test_a_dissent_payload_dispatches_like_any_other_adt_payload():
    """The provided type is not special where it has no business being.

    A match-arm payload does not pin a stdlib method receiver inside a
    `provide` body, for a user ADT and for `Aggregate[T]` alike. That is a
    pre-existing gap in method dispatch, not something this slice introduced
    and not something it should paper over: the two behave the same way, and
    this test is what says so."""
    adt = """
type Row = { id: Int }
type Outcome = Ok(List[Row]) | Missing

service S { fn f(o: Outcome) -> Int }
component C provides out: S {
  provide out { fn f(o) = match o { Ok(d) => d.length(), Missing => 0, } }
}
"""
    adt_err = refusal(adt)
    provided_err = refusal(_consumer("""      Agreed(v)    => 0,
      Split(d)     => d.length(),
      Inquorate(d) => d.length(),""", returns="Int"))
    assert "on a value of unknown type" in adt_err.message
    assert "on a value of unknown type" in provided_err.message


@pytest.mark.parametrize("name", ("Aggregate", "Answer", "DissentEntry"))
def test_a_program_may_not_declare_its_own_answer_type(name):
    """Reserved, for the reason `Principal` is. A program allowed to declare
    its own `Aggregate[T]` could declare one whose only case is `Agreed`,
    match it exhaustively with a single arm, and satisfy every rule in this
    section while having removed the constructor the rules are about."""
    err = refusal(f"{ROLES}\ntype {name}[T] = OnlyMine(T)\n{TAIL}")
    assert err.code == CODE
    assert f"`type {name}`" in err.message


def test_an_ordinary_adt_still_admits_a_wildcard():
    """The negative control, and the one that keeps this slice honest. The
    wildcard rule above is about the PROVIDED answer type and must not have
    become a new opinion about matching in general: a user ADT still takes a
    `_` arm exactly as it did before."""
    assert compile_source("""
type Outcome = Ok(Int) | NotFound | Invalid(Str)

fn describe(o: Outcome) -> Int {
  return match o {
    Ok(v) => v,
    _     => 0,
  }
}
""", "adt.revl")


def test_the_answer_type_costs_a_program_that_does_not_name_it_nothing():
    """Design note 543 section 9's byte-identity, one level further out. A
    council declaration writes no IR; a PROVIDED type that no signature names
    must not write one either, or every program in the tree would have gained
    three type entries it never asked for."""
    plain = """
model role edge   on_device
model role local2 on_device

service Answer { fn classify(text: Str) -> Str }

component Classifier provides out: Answer {
  provide out { fn classify(text) = text }
}
"""
    doc = compile_source(plain, "council.revl")
    assert "types" not in doc
    # and a program that DOES name it gets exactly the three, and no more.
    named = compile_source(_consumer(ALL_ARMS), "answer.revl")
    assert sorted(named["types"]) == ["Aggregate", "Answer", "DissentEntry"]


def test_the_answer_type_is_closed():
    """Three constructors, and the aggregation names one value or none. A
    fourth would be a way for the council to answer that is neither agreement
    nor disagreement, which is the shape design note 543 exists to remove."""
    from revl import model_answer

    doc = compile_source(_consumer(ALL_ARMS), "answer.revl")
    assert [c["name"] for c in doc["types"]["Aggregate"]["cases"]] \
        == list(AGGREGATE_CASES)
    assert [c["name"] for c in doc["types"]["Answer"]["cases"]] \
        == ["Says", "Abstains", "Unreachable"]
    assert model_answer.DISSENT_ARMS == DISSENT_ARMS
    # `Agreed` is the ONLY constructor carrying the boundary type.
    carries = [c["name"] for c in doc["types"]["Aggregate"]["cases"]
               if c["payload"] == "T"]
    assert carries == ["Agreed"]


def test_the_answer_type_carries_the_same_code():
    """One guarantee, spent in two places. The declaration rules and this one
    are the same promise - disagreement is never resolved toward allow - so
    they carry the same code, and an agent reading `classify()` gets the same
    guarantee line whichever half refused."""
    from revl import model_answer, model_council

    assert model_answer.CODE == model_council.CODE == CODE
    assert classify(refusal(_consumer("      Agreed(v) => v,")))["guarantee"] \
        == GUARANTEES[CODE]


def test_the_answer_refusal_never_reads_as_a_withheld_approval():
    """The slice-1 vocabulary pin, extended to the call site. A council member
    is a model with no identity and its disagreement is not a withheld
    consent; item 471's words must be absent here too, or `Split` starts
    reading as "an operator declined"."""
    err = refusal(_consumer('      Agreed(v) => v,\n      _ => "held",'))
    prose = f"{err.message} {err.hint}".lower()
    for word in ("operator", "approval", "approve", "consent", "signer"):
        assert word not in prose, word


def test_the_answer_module_imports_none_of_item_471s_machinery():
    """The slice-1 import pin, extended to the module this slice adds.

    `revl.model_council` is pinned to an exact import set so it cannot reach
    item 471's operator quorum. That pin is worth nothing if the answer type
    beside it reaches the same machinery, so this module is held to the same
    rule - and to a tighter set, because it needs only the error type."""
    import ast

    source = (ROOT / "src" / "revl" / "model_answer.py").read_text()
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            imported.add(base)
            imported.update(f"{base}.{alias.name}" for alias in node.names)
    for name in sorted(imported):
        assert "mcp" not in name and "quorum" not in name.lower() \
            and "polic" not in name.lower(), name
    assert imported <= {
        "__future__", "__future__.annotations",
        ".errors", ".errors.RevlError",
    }, sorted(imported)


def test_the_two_slice_three_fixtures_are_the_same_one_arm_apart():
    """The non-vacuity measurement. The control admits, the twin does not, and
    the ONLY difference between the two programs is that one arm which named
    `Split` now names `_`.

    A rule whose refusing fixture differs from its admitting one in some other
    way has not been shown to be the thing doing the refusing. Here the twin is
    DERIVED from the control by that substitution, so the property is
    structural: the assertion below would have to be edited, not merely
    re-run, for the two to drift apart."""
    assert compile_source(ANSWER_CONTROL_SRC, "answer.rvl")
    err = refusal(ANSWER_REFUSED_SRC)
    assert err.code == CODE
    assert "`Split`" in err.message

    def _code(src):
        return [line.strip() for line in src.splitlines()
                if line.strip() and not line.strip().startswith("//")]

    a, b = _code(ANSWER_CONTROL_SRC), _code(ANSWER_REFUSED_SRC)
    assert len(a) == len(b), (len(a), len(b))
    differ = [(x, y) for x, y in zip(a, b) if x != y]
    assert differ == [tuple(e.strip() for e in ANSWER_EDIT)], differ
    # and the substitution really did fire: a no-op replace would make every
    # assertion above vacuous.
    assert ANSWER_REFUSED_SRC != ANSWER_CONTROL_SRC


def test_the_admitting_fixture_binds_a_two_member_council():
    """The control is not a program that merely mentions the type. It declares
    a council with two separately placed members and ROUTES an action to it,
    so the answer the consumer must handle is an answer this program actually
    asks for."""
    from revl import model_council, model_route
    from revl.parser import Parser

    program = Parser(ANSWER_CONTROL_SRC, "answer.rvl").parse()
    councils = model_council.check(program)
    assert sorted(councils) == ["Release"]
    assert len(councils["Release"].members) == 2
    assert {m.function for m in councils["Release"].members} \
        == {"proposer", "adversary"}
    # placed separately, and both on the device: the control admits for a
    # reason rather than by omission.
    assert {m.role for m in councils["Release"].members} == {"local2", "edge"}
    assert councils["Release"].residence == "on_device"
    placed = model_route.check(program, councils=councils)
    assert placed["Reviewer"]["propose"]["confidential"]["council"] == "Release"
