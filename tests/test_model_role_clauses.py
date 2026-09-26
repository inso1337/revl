"""The two optional clauses after a `model role` residence (items 515, 519).

Item 515 (issue #1189) adds a `device` clause and item 519 (issue #1193) adds
a `reaches` clause, and both land in the same position: the slot that follows
the residence. This file is the executable spec for how they share it.

    model role fast on_device device gpu memory 6144 quant q4_k_m
                              reaches [model.complete]

The order is FIXED, `device` first and `reaches` second, and each clause is
independently omittable, so a role may write neither, either or both. The
parser refuses the other order rather than admitting two spellings of one
declaration.

The programs are inline for the reason both source files state: `examples/`
and `tests/fixtures/` are census corpus roots and `selfhost/parser.rvl` parses
neither `model role` nor `route model`, so an admitting fixture in either
place would be a `false-reject` census entry on the day it landed.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402

CODE = "G-MODEL-PLACE"

# One role written four ways: with neither clause (item 512's own shape), with
# each alone, and with both. `%s` is the whole clause run under test.
ROLE = """
model role local on_device %s

service Answer { fn classify(text: Str) -> Str }

component Classifier provides out: Answer {
  route model on classify { * -> local }
  provide out { fn classify(text) = text }
}
"""

NEITHER = ""
DEVICE_ONLY = "device gpu memory 6144 quant q4_k_m"
REACHES_ONLY = "reaches [model.complete]"
BOTH = DEVICE_ONLY + " " + REACHES_ONLY

# A component that really consults a model, so the item-519 fold runs: it
# wires a service whose emission method declares a `model.*` token and crosses
# that boundary in its body. Both candidates are profiled, which is what an
# ordered candidate set requires of every member.
PORTFOLIO_THAT_CONSULTS = """
model role fast on_device device gpu memory 6144 quant q4_k_m reaches [%s]
model role small on_device device cpu memory 512 quant int8 reaches [%s]

service Completions { emission[model.complete] fn complete(p: Str) -> Str }
service Answer { emission[llm] fn classify(text: Str) -> Str }

component Classifier requires llm: Completions provides out: Answer {
  route model on classify { * -> fast | small }
  provide out { fn classify(text) = emit llm.complete(text) }
}
"""


def _refuses(src: str, name: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, name)
    return excinfo.value


def _table(src: str, name: str = "role.rvl"):
    from revl.model_route import roles
    from revl.parser import Parser

    return roles(Parser(src, name).parse(), name)


# --------------------------------------------------------------------------
# Each clause is independently omittable
# --------------------------------------------------------------------------

@pytest.mark.parametrize("clauses", [NEITHER, DEVICE_ONLY, REACHES_ONLY, BOTH])
def test_every_combination_of_the_two_clauses_parses(clauses):
    """Four programs, one grammar. The point of the reconcile: neither clause
    is reachable only when the other is absent."""
    table = _table(ROLE % clauses)
    assert set(table) == {"local"}


def test_both_clauses_on_one_role_reach_the_role_table():
    role = _table(ROLE % BOTH)["local"]
    assert role.profile.device == "gpu"
    assert role.profile.memory_mib == 6144
    assert role.profile.quant == "q4_k_m"
    assert role.reach == ("model.complete",)
    assert role.reach_declared is True


def test_a_device_clause_alone_leaves_the_reach_undeclared():
    """The two clauses do not stand in for each other. Writing the device
    profile says nothing about reach, and an unsaid reach is the unnameable
    `*` and not an empty set."""
    role = _table(ROLE % DEVICE_ONLY)["local"]
    assert role.profile is not None
    assert role.reach is None
    assert role.reach_declared is False
    assert role.reach_tokens == ("*",)


def test_a_reach_clause_alone_leaves_the_profile_absent():
    """And the other way: a role that declares its reach declares no resource
    demand, which is not a demand of zero."""
    role = _table(ROLE % REACHES_ONLY)["local"]
    assert role.profile is None
    assert role.reach == ("model.complete",)


# --------------------------------------------------------------------------
# The order is fixed, and the parser says so
# --------------------------------------------------------------------------

def test_the_clauses_are_written_device_then_reaches():
    assert compile_source(ROLE % BOTH, "both.rvl")["components"][0]["name"] \
        == "Classifier"


def test_the_other_order_is_refused_and_names_the_one_that_is_right():
    """A free order would give one declaration two spellings and make every
    later reader of this slot carry the permutation. The refusal is a syntax
    refusal, so it registers no guarantee code."""
    err = _refuses(ROLE % (REACHES_ONLY + " " + DEVICE_ONLY), "order.rvl")
    text = str(err)
    assert "device" in text and "reaches" in text
    assert "quant <tag>" in text


def test_a_second_device_clause_is_refused():
    err = _refuses(ROLE % (DEVICE_ONLY + " " + DEVICE_ONLY), "twodev.rvl")
    assert "second `device` clause" in str(err)


def test_a_second_reaches_clause_is_refused():
    err = _refuses(ROLE % (REACHES_ONLY + " " + REACHES_ONLY), "tworeach.rvl")
    assert "second `reaches` clause" in str(err)


def test_neither_clause_word_is_reserved():
    """Both clauses are read as CONTEXTUAL identifiers in one slot, so a
    program using `device`, `memory`, `quant` or `reaches` as an ordinary name
    keeps compiling. `KEYWORDS` is untouched, and so is the self-hosted lexer
    that mirrors it."""
    src = """
service Answer { fn pick(device: Str, memory: Str, quant: Str,
                         reaches: Str) -> Str }

component Picker provides out: Answer {
  provide out { fn pick(device, memory, quant, reaches) = device }
}
"""
    assert compile_source(src, "names.rvl")["components"][0]["name"] == "Picker"


# --------------------------------------------------------------------------
# Neither field owns the fourth positional slot
# --------------------------------------------------------------------------

def test_a_role_is_still_built_from_name_residence_and_line():
    """The shape item 516's council reads through `model_route.roles()`. Both
    clauses are keyword-defaulted fields, so a three-argument construction is
    still the whole declaration for a role that writes neither."""
    from revl.model_route import Role

    role = Role("r", "on_device", 1)
    assert (role.name, role.residence, role.line) == ("r", "on_device", 1)
    assert role.profile is None
    assert role.reach is None


def test_the_two_fields_are_keyword_addressable_and_independent():
    from revl.model_route import Role
    from revl.model_profile import DeviceProfile

    profiled = Role("r", "on_device", 1, DeviceProfile("cpu", 512, "int8", 1))
    assert profiled.reach is None and profiled.reach_tokens == ("*",)
    reaching = Role("r", "on_device", 1, reach=())
    assert reaching.profile is None and reaching.reach_tokens == ()


def test_the_declaration_node_carries_both_clauses():
    from revl.parser import ModelRoleDecl, Parser

    decl = Parser(ROLE % BOTH, "both.rvl").parse().model_roles[0]
    assert isinstance(decl, ModelRoleDecl)
    assert decl.profile.device == "gpu"
    assert decl.reach == ("model.complete",)
    assert ModelRoleDecl("r", "on_device", 1).profile is None
    assert ModelRoleDecl("r", "on_device", 1).reach is None


# --------------------------------------------------------------------------
# The two items meet again on an ordered candidate set
# --------------------------------------------------------------------------

def test_a_candidate_set_of_reaching_roles_compiles():
    src = PORTFOLIO_THAT_CONSULTS % ("model.complete", "model.complete")
    rows = (compile_source(src, "set.rvl").get("manifest") or {})["model_reach"]
    # One record per candidate, in candidate-name order: the fallback is a
    # placement of its own and is accounted as one.
    assert [r["role"] for r in rows] == ["fast", "small"]
    assert all(r["reaches"] == ["model.complete"] for r in rows)


def test_a_fallback_reaching_past_the_component_is_refused():
    """The item-515 candidate set and the item-519 fold meet here. A reach
    checked only on the arm's HEAD would let the first fallback widen a
    ceiling the head respects, which is the fail-open shape a scheduler
    introduces. Every candidate is folded."""
    src = PORTFOLIO_THAT_CONSULTS % ("model.complete", "shell.exec")
    err = _refuses(src, "fallback.rvl")
    text = str(err)
    assert err.code == CODE
    assert "small" in text
    assert "shell.exec" in text


def test_the_route_table_carries_the_candidates_and_the_arm_line():
    """Both items write into the same route record: item 515's `candidates`
    and item 519's `line`, beside the head `role`/`residence` item 514 reads."""
    from revl.model_route import check
    from revl.parser import Parser

    src = PORTFOLIO_THAT_CONSULTS % ("model.complete", "model.complete")
    placed = check(Parser(src, "set.rvl").parse(), "set.rvl")
    arm = placed["Classifier"]["classify"]["*"]
    assert arm["role"] == "fast"
    assert arm["residence"] == "on_device"
    assert arm["candidates"] == ("fast", "small")
    assert arm["line"] > 0


# --------------------------------------------------------------------------
# The third construct in the same slot: a `model council` (item 516)
# --------------------------------------------------------------------------
#
# Item 516 landed between these two items and put a second thing on the right
# of a `route model` arrow. A council is a PLACEMENT and not a CANDIDATE, and
# the two rules below are what that sentence means in code.

COUNCIL_THAT_CONSULTS = """
model role edge on_device reaches [%s]
model role near on_device reaches [%s]

model council Board {
  proposer  -> edge,
  adversary -> near,
  aggregate unanimous
}

service Completions { emission[model.complete] fn complete(p: Str) -> Str }
service Answer { emission[llm] fn classify(text: Str) -> Str }

component Classifier requires llm: Completions provides out: Answer {
  route model on classify { * -> Board }
  provide out { fn classify(text) = emit llm.complete(text) }
}
"""


def test_a_council_alone_on_an_arm_still_places():
    """Item 516's own shape is unchanged by the candidate set: a council may
    stand alone on the right of an arrow, and the placement it records gains
    the same two additive keys every other placement gained."""
    from revl.model_council import check as council_check
    from revl.model_route import check
    from revl.parser import Parser

    src = COUNCIL_THAT_CONSULTS % ("model.complete", "model.complete")
    program = Parser(src, "council.rvl").parse()
    placed = check(program, "council.rvl",
                   councils=council_check(program, "council.rvl"))
    arm = placed["Classifier"]["classify"]["*"]
    assert arm["role"] == "Board"
    assert arm["council"] == "Board"
    # The MEMBER roles, not the council's own name. A council asks every
    # member, so those are the roles the arm actually reaches, and they are
    # what item 519's fold has to read: `Board` is not a `model role` and has
    # no reach of its own.
    assert arm["candidates"] == ("edge", "near")
    assert arm["line"] > 0


def test_a_council_may_not_be_one_candidate_among_several():
    """A council already aggregates its members by a declared, total rule that
    names no value when they disagree (G-COUNCIL-SPLIT). A scheduler choosing
    among candidates is a SECOND aggregation over the same call, written
    nowhere. Nesting one in the other leaves two rules and no written answer
    for which applies, so it is refused by name rather than resolved."""
    src = (COUNCIL_THAT_CONSULTS % ("model.complete", "model.complete")).replace(
        "* -> Board", "* -> Board | edge")
    err = _refuses(src, "nested.rvl")
    text = str(err)
    assert err.code == CODE
    assert "model council `Board`" in text
    assert "candidates" in text


def test_a_council_member_reaching_past_the_component_is_refused():
    """Item 519's fold reads every member of a council placement, for the same
    reason it reads every candidate of an ordered set: the component routes
    through all of them. Folding only the council's own name would fold
    nothing, because a council is not a role and declares no reach."""
    src = COUNCIL_THAT_CONSULTS % ("model.complete", "shell.exec")
    err = _refuses(src, "member_reach.rvl")
    text = str(err)
    assert err.code == CODE
    assert "near" in text
    assert "shell.exec" in text
