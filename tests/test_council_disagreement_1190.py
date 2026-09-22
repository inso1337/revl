"""Disagreement is never resolved toward allow (roadmap item 516, issue #1190).

`docs/design/557-council-disagreement.md` is the note. Item 516 landed the
`model council` declaration and its rules (`tests/test_model_council_516.py` is
that slice's spec); this file is the executable spec for the CHECKED PROPERTY
the issue names, and for the guarantee code that carries it:

    disagreement can never be silently resolved toward allow - an aggregation
    that admits on a tie, or that admits while a member is unreachable, is
    refused.

Four adversarial shapes, written before the code that answers them:

  1. a tie that admits           `aggregate unanimous on_tie allow`
  2. a member that is unreachable `aggregate majority quorum answered`
  3. an aggregation that is not total  no `aggregate`, two of them, or a rule
                                       that picks one member's answer
  4. members that share a placement    two members on one `model role`

Each is refused BY NAME with a message, and the message is half the contract:
this repository's agreement is on tag AND message (`tools/gate_reference_
census.py` buckets a `msg-mismatch` separately from an `agree-refuse`), so the
assertions below pin message fragments and not only codes.

WHY THESE PROGRAMS ARE INLINE
-----------------------------
`examples/rejections/` and `tests/fixtures/` are census corpus roots
(`tools/gate_reference_census.py` CORPUS_DIRS, walked with rglob) and
`selfhost/parser.rvl` answers any council-declaring program with
`BAD|unexpected token at top level`. A fixture in either place would enter the
census as a tracked divergence against a self-host that cannot read it, for a
port that is somebody else's slice (issue #1291). Item 512's suite records the
same decision and `tools/tier_guarantees.py`'s ACKNOWLEDGED entry is where it
is declared, so this file is the reproducer set the matrix points at.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.diagnostics import FIXES, GUARANTEES, classify, explain  # noqa: E402

#: The code this file is about. Spelled rather than imported so the module
#: still collects against a tree that predates it.
CODE = "G-COUNCIL-SPLIT"

#: Item 512's code, which two council refusals keep (design note 557 §3).
PLACE_CODE = "G-MODEL-PLACE"

# The motivating shape, from the issue: a cloud proposer and a LOCAL adversary,
# so the adversary may read an origin the proposer may not. The two members are
# placed separately, which is the point of the construct and not a detail.
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
    return f"{ROLES}\nmodel council Release {{\n{body}\n}}\n{TAIL}"


#: Cloud proposer, local adversary, local verifier, one rule written down.
ADMITTED = """
  proposer  -> vast,
  adversary -> edge,
  verifier  -> aux,
  aggregate unanimous
"""

#: The control: the same roles and the same component, no council. It compiles
#: here and on the tree before this change, so a red here is the harness.
NO_COUNCIL = ROLES + TAIL


def refusal(source: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(source, "council.revl")
    return excinfo.value


def record(source: str) -> dict:
    return classify(refusal(source))


# ---------------------------------------------------------------------------
# the controls
# ---------------------------------------------------------------------------

def test_the_control_compiles():
    assert compile_source(NO_COUNCIL, "council.revl")


def test_the_motivating_council_compiles():
    """A cloud proposer beside a local adversary is the admitted case. If this
    red-lined, every refusal below would be measuring a broken harness."""
    assert compile_source(council(ADMITTED), "council.revl")


# ---------------------------------------------------------------------------
# 1. a tie that admits
# ---------------------------------------------------------------------------

def test_an_aggregation_that_admits_on_a_tie_is_refused():
    """The roadmap's own exit test, and the one direction that must not exist."""
    got = record(council("  proposer -> vast,\n  adversary -> edge,\n"
                         "  aggregate unanimous on_tie allow"))
    assert got["code"] == CODE
    assert got["message"] == (
        "`on_tie allow` in model council `Release` admits when the members "
        "disagree")
    assert "disagreement is never resolved toward allow" in got["hint"]


@pytest.mark.parametrize(
    "outcome", ["allow", "admit", "proceed", "accept", "first", "any"])
def test_every_admitting_tie_outcome_is_refused_by_name(outcome):
    """Each parses, so the author gets the reason and not a syntax complaint."""
    got = record(council(f"  proposer -> vast,\n  adversary -> edge,\n"
                         f"  aggregate unanimous on_tie {outcome}"))
    assert got["code"] == CODE
    assert f"`on_tie {outcome}`" in got["message"]
    assert "admits when the members disagree" in got["message"]


@pytest.mark.parametrize("outcome", ["split", "deny"])
def test_the_two_non_admitting_tie_outcomes_are_allowed(outcome):
    """`split` names no value and `deny` narrows toward refusing. Neither can
    move the outcome toward allow, which is the whole admission rule."""
    assert compile_source(
        council(f"  proposer -> vast,\n  adversary -> edge,\n"
                f"  aggregate unanimous on_tie {outcome}"), "council.revl")


def test_an_unrecognised_tie_outcome_is_refused_rather_than_ignored():
    """A typo must not fall through to the default. `Allow` is the exact shape
    that would, if the vocabulary were matched case-insensitively or the
    unknown value were dropped."""
    got = record(council("  proposer -> vast,\n  adversary -> edge,\n"
                         "  aggregate unanimous on_tie Allow"))
    assert got["code"] == CODE
    assert "unknown tie outcome `Allow`" in got["message"]


# ---------------------------------------------------------------------------
# 2. a member that is unreachable
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "basis", ["answered", "reachable", "available", "responding"])
def test_a_floor_counted_over_the_answering_set_is_refused(basis):
    """The issue's second half: an aggregation that admits while a member is
    unreachable. A floor over the members that ANSWERED lets a council shrink
    until the survivors agree, so two of three agreeing while the third is
    silent would read as agreement."""
    got = record(council(f"  proposer -> vast,\n  adversary -> edge,\n"
                         f"  verifier -> aux,\n"
                         f"  aggregate majority quorum {basis}"))
    assert got["code"] == CODE
    assert f"`quorum {basis}`" in got["message"]
    assert "counts the rule's floor over the members that answered" in \
        got["message"]
    assert "abstains" in got["hint"]


def test_the_only_admitted_basis_is_the_declared_member_set():
    from revl import model_council

    assert model_council.QUORUM_BASES == ("declared",)
    assert compile_source(
        council("  proposer -> vast,\n  adversary -> edge,\n"
                "  aggregate majority quorum declared"), "council.revl")


def test_the_default_basis_is_the_declared_member_set():
    """Omitting `quorum` is the fail-closed reading, not an unspecified one.

    The parser records the OMISSION rather than filling in a default, so this
    is the checker choosing the safe reading and not the grammar hiding it."""
    from revl.parser import Parser

    parsed = Parser(council(ADMITTED), "council.revl").parse()
    assert parsed.model_councils[0].aggregate.quorum is None
    assert parsed.model_councils[0].aggregate.on_tie is None
    release = _table(council(ADMITTED))["Release"]
    assert release.quorum == "declared"
    assert release.on_tie == "split"


def test_the_floor_is_counted_over_every_declared_member():
    """The arithmetic behind the refusal: `majority` over three declared
    members needs two AGREEING answers out of three DECLARED, so one silent
    member cannot be arranged into agreement by removing it from the count."""
    table = _table(council(ADMITTED))
    release = table["Release"]
    assert len(release.members) == 3
    assert release.floor == 3          # `unanimous`
    assert _floor("majority", 3) == 2
    assert _floor("majority", 2) == 2
    assert _floor("majority", 4) == 3


# ---------------------------------------------------------------------------
# 3. an aggregation that is not total
# ---------------------------------------------------------------------------

def test_a_council_with_no_aggregation_is_refused():
    """An implicit aggregation is the defect: with none written down,
    disagreement is resolved by whatever the caller does with the members."""
    got = record(council("  proposer -> vast,\n  adversary -> edge"))
    assert got["code"] == CODE
    assert got["message"] == (
        "model council `Release` declares no `aggregate` rule")


def test_a_council_with_two_aggregations_is_refused():
    """Two rules can disagree about the same answers, so which one aggregated
    would be declaration order."""
    got = record(council("  proposer -> vast,\n  adversary -> edge,\n"
                         "  aggregate unanimous,\n"
                         "  aggregate majority on_tie allow"))
    assert got["code"] == CODE
    assert "declares two `aggregate` rules" in got["message"]


@pytest.mark.parametrize(
    "rule", ["first", "any", "fastest", "cheapest", "best", "random"])
def test_a_rule_that_picks_one_members_answer_is_refused_by_name(rule):
    """Not an aggregation: a council that answers with whichever member
    answered is strictly worse than one model, because the caller believes
    several agreed."""
    got = record(council(f"  proposer -> vast,\n  adversary -> edge,\n"
                         f"  aggregate {rule}"))
    assert got["code"] == CODE
    assert f"`aggregate {rule}`" in got["message"]
    assert "resolves disagreement toward one member's answer" in got["message"]


@pytest.mark.parametrize("rule", ["unanimous", "majority", "veto"])
def test_every_admitted_rule_is_total_over_the_declared_set(rule):
    """Totality, checked rather than asserted: for every admitted rule and
    every council size the checker accepts, the rule has a floor, that floor is
    a strict majority of the DECLARED members, and falling short of it names
    `split` rather than a value."""
    body = ("  proposer -> vast,\n  adversary -> edge,\n  verifier -> aux,\n"
            f"  aggregate {rule}")
    table = _table(council(body))
    release = table["Release"]
    assert release.rule == rule
    assert release.floor * 2 > len(release.members)
    assert release.on_tie == "split"


def test_an_unknown_rule_is_refused_rather_than_defaulted():
    got = record(council("  proposer -> vast,\n  adversary -> edge,\n"
                         "  aggregate consensus"))
    assert got["code"] == CODE
    assert "unknown aggregation rule `consensus`" in got["message"]


def test_veto_without_an_adversary_is_refused():
    """A veto clause naming a member that does not exist reads as a protection
    the council does not have."""
    got = record(council("  proposer -> vast,\n  verifier -> aux,\n"
                         "  aggregate veto"))
    assert got["code"] == CODE
    assert got["message"] == (
        "`aggregate veto` in model council `Release`, which declares no "
        "`adversary`")


def test_a_council_that_cannot_disagree_is_refused():
    """One member wearing the word `council` always agrees with itself."""
    got = record(council("  proposer -> vast,\n  aggregate unanimous"))
    assert got["code"] == CODE
    assert "declares 1 member" in got["message"]


def test_a_council_with_nothing_to_propose_is_refused():
    """With no proposer the rule has no value to name and every result is
    inconclusive, so the council could only ever refuse."""
    got = record(council("  adversary -> edge,\n  verifier -> aux,\n"
                         "  aggregate unanimous"))
    assert got["code"] == CODE
    assert "declares no `proposer`" in got["message"]


# ---------------------------------------------------------------------------
# 4. members that share a placement
# ---------------------------------------------------------------------------

def test_two_members_on_one_role_is_refused():
    """The council must not flatten its members into one placement: one model
    answering twice under two names is correlated error, and two agreeing
    answers from one placement is agreement the council does not have."""
    got = record(council("  proposer -> vast,\n  adversary -> edge,\n"
                         "  verifier -> edge,\n  aggregate unanimous"))
    assert got["code"] == CODE
    assert "are both placed on model role `edge`" in got["message"]


def test_each_member_keeps_its_own_admission_ceiling():
    """The motivating case, measured rather than described: the local adversary
    is `on_device` while the cloud proposer is `off_device`, in ONE admitted
    council. A construct that flattened its members would report one residence
    for both."""
    release = _table(council(ADMITTED))["Release"]
    assert release.member("proposer").residence == "off_device"
    assert release.member("adversary").residence == "on_device"
    assert release.member("verifier").residence == "on_device"
    assert release.member("proposer").off_device is True
    assert release.member("adversary").off_device is False


def test_the_councils_own_ceiling_is_the_most_permissive_member():
    """Giving an input to a council gives it to every member, so the council's
    own ceiling is the LEAST restrictive of its members'. The other way round
    is the fail-open reading."""
    mixed = _table(council(ADMITTED))["Release"]
    assert mixed.residence == "off_device"

    local = _table(council("  proposer  -> edge,\n  adversary -> aux,\n"
                           "  aggregate unanimous"))["Release"]
    assert local.residence == "on_device"


def test_a_member_naming_an_undeclared_role_keeps_item_512s_code():
    """The line between the two codes (design note 557 §3): this refusal's
    subject is a `model role`, and item 512's fix line - declare the role - is
    the rewrite, so it is not reclassified."""
    got = record(council("  proposer -> nowhere,\n  adversary -> edge,\n"
                         "  aggregate unanimous"))
    assert got["code"] == PLACE_CODE
    assert "which is not declared" in got["message"]


# ---------------------------------------------------------------------------
# the code itself: why it is a second one
# ---------------------------------------------------------------------------

def test_the_code_is_registered_with_a_guarantee_and_a_fix():
    assert CODE in GUARANTEES
    assert CODE in FIXES
    answer = explain(CODE)
    assert answer["ok"] is True
    assert answer["guarantee"] == GUARANTEES[CODE]
    assert answer["fix"] == FIXES[CODE]


def test_the_guarantee_line_states_the_checked_property():
    text = GUARANTEES[CODE]
    assert "never resolves disagreement toward allow" in text
    assert "DECLARED members" in text


def test_the_refusal_carries_a_fix_an_agent_can_apply():
    """The defect this split repairs. Before issue #1190 a tie refusal was
    classified `G-MODEL-PLACE`, so the structured record's `fix` told the
    author to route the origin to an `on_device` role - advice that does not
    touch the clause that was refused. `revl.diagnostics` exists so an agent
    can react WITHOUT parsing prose, so the pair has to be about the refusal.
    """
    got = record(council("  proposer -> vast,\n  adversary -> edge,\n"
                         "  aggregate unanimous on_tie allow"))
    assert got["guarantee"] == GUARANTEES[CODE]
    assert "on_tie split" in got["fix"]
    assert "on_device" not in got["fix"]
    assert got["fix"] != FIXES[PLACE_CODE]


def test_the_two_codes_are_distinct_and_both_registered():
    assert CODE != PLACE_CODE
    assert GUARANTEES[CODE] != GUARANTEES[PLACE_CODE]
    assert FIXES[CODE] != FIXES[PLACE_CODE]


def test_the_category_is_the_councils_own():
    got = record(council("  proposer -> vast,\n  adversary -> edge,\n"
                         "  aggregate unanimous on_tie allow"))
    assert got["category"] == "model-council"


# ---------------------------------------------------------------------------
# NOT multi-party human approval (items 471 / 509)
# ---------------------------------------------------------------------------

def test_the_council_checker_imports_none_of_item_471s_machinery():
    """A council vote comes from a model and carries no identity; a 471 vote
    comes from a bound operator and is a statement about consent. The two
    vocabularies must not merge, and the cheapest way to keep them apart is
    that the council checker cannot reach the approval machinery at all.

    Read off the IMPORTS with `ast`, not off the text: the module's prose says
    the word `operator` several times, saying exactly that the two are
    different, and a grep would refuse the sentence that keeps them apart.
    """
    import ast

    source = (ROOT / "src" / "revl" / "model_council.py").read_text()
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
            and "polic" not in name.lower(), (
            f"`model_council` imports `{name}`: item 471's operator quorum "
            f"lives behind those names, and a council member is a model with "
            f"no identity")
    assert imported <= {
        "__future__", "__future__.annotations",
        "dataclasses", "dataclasses.dataclass",
        ".errors", ".errors.RevlError",
        ".model_route", ".model_route.CATEGORY", ".model_route.CODE",
        ".model_route.roles",
    }, sorted(imported)


def test_a_council_refusal_never_reads_as_a_withheld_approval():
    """The words a 471 refusal uses are absent here on purpose. A caller must
    not read `split` as "an operator declined"."""
    got = record(council("  proposer -> vast,\n  adversary -> edge,\n"
                         "  aggregate unanimous on_tie allow"))
    prose = f"{got['message']} {got['hint']}".lower()
    for word in ("operator", "approval", "approve", "consent", "signer"):
        assert word not in prose


def test_the_two_families_do_not_share_a_guarantee_code():
    """If a later change files an operator-approval refusal under this code, or
    a council refusal under an approval code, this is where it shows up."""
    assert "operator" not in GUARANTEES[CODE].lower()
    assert "approval" not in GUARANTEES[CODE].lower()
    assert "model" in GUARANTEES[CODE].lower()


# ---------------------------------------------------------------------------
# the declaration still writes no IR
# ---------------------------------------------------------------------------

def test_the_refusals_cost_the_ir_nothing():
    """Every rule above points toward refusing, so an admitted council is still
    byte-identical to the same program without it. That is what keeps this
    item out of every emitter and every golden."""
    assert compile_source(council(ADMITTED), "council.revl") == \
        compile_source(NO_COUNCIL, "council.revl")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _table(source: str) -> dict:
    """The validated council table for a program that compiles."""
    from revl import model_council
    from revl.parser import Parser

    return model_council.check(Parser(source, "council.revl").parse())


def _floor(rule: str, members: int) -> int:
    """The floor `revl.model_council.Council.floor` computes, for a size the
    fixtures above cannot all reach (`majority` over four declared members)."""
    return members // 2 + 1 if rule == "majority" else members
