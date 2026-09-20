"""A model council with role-bound members (roadmap item 516, issue #1190).

The executable spec for slice 1 of `docs/design/543-model-council.md`: the
`model council` declaration, its members, its `aggregate` clause, and every
refusal the three carry.

These programs are written INLINE rather than dropped in `examples/` or
`tests/fixtures/`, on purpose, and for the reason item 512's suite records.
Both directories are census corpus roots (`tools/gate_reference_census.py`
CORPUS_DIRS) and `selfhost/parser.rvl` does not parse `model council`, so an
admitting fixture in either place would be a `false-reject` entry in the census
the moment it landed. The self-host port is slice 5; see the design doc's
section 13.
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
CODE = "G-MODEL-PLACE"
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
    assert classify(err)["code"] == CODE


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
    assert classify(err)["code"] == CODE


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


def test_no_new_guarantee_code_is_registered():
    """The council is the placement family's second construct, not a second
    family. A new code would need a reproducer under `examples/rejections/` or
    an `ACKNOWLEDGED` entry for item 523's generated tier matrix, and this
    slice adds no new class of refusal."""
    assert CODE in GUARANTEES
    assert "G-MODEL-COUNCIL" not in GUARANTEES


# ---------------------------------------------------------------------------
# the tie-back: the constants above are the module's own
# ---------------------------------------------------------------------------

def test_the_module_constants_match():
    from revl import model_council

    assert model_council.CODE == CODE
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
