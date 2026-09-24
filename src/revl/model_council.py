"""A model council with role-bound members and typed disagreement (item 516).

`docs/design/543-model-council.md` is the design; this module is slice 1 of it,
the DECLARATION half, kept in one file so each refusal sits beside the rule it
enforces (the `revl.model_route` discipline: the parser reads the shape, this
reads the meaning).

The checked property is registered as `G-COUNCIL-SPLIT`
(`docs/design/557-council-disagreement.md`, issue #1190): **disagreement can
never be silently resolved toward allow.** Item 516 landed the rules below and
filed them under item 512's `G-MODEL-PLACE`; 557 gives them their own code,
because the code is what an agent reads instead of the prose and item 512's
one-line fix is not the rewrite for any of them.

The surface is one declaration, at the program level because it binds several
program-level `model role` declarations in one aggregation:

    model role edge   on_device
    model role local2 on_device
    model role vast   off_device

    model council Release {
      proposer  -> vast,
      adversary -> edge,
      verifier  -> local2,
      aggregate unanimous
    }

A member is a pair: what it is FOR (`proposer`, `adversary`, `verifier`) and
WHERE it runs (an item-512 role). The two words are separate so the members can
be placed separately, which is the whole point of the construct: a local
adversary may read an origin the cloud proposer may not, and item 514's ceiling
decides that per member rather than per council.

WHICH WAY EVERY DECISION FAILS
------------------------------
A council that degrades to "whichever member answered" is STRICTLY WORSE than
one model, because the caller believes several agreed. So every rule below
refuses when it is unsure, and the two spellings that would resolve
disagreement toward an answer - `aggregate first` and `on_tie allow` - parse
and are refused BY NAME rather than by a syntax error, so the author gets the
reason.

The aggregation is total on the DECLARED member set, never on the set that
answered. A member that fails or times out ABSTAINS, and an abstention counts
against the rule's floor rather than toward agreement, so a council cannot
shrink itself into a quorum. `quorum answered` is the spelling for the other
reading and it is refused: an aggregation that ignores an unreachable member is
exactly the roadmap's second refusal.

WHAT THIS MODULE DOES NOT DO
----------------------------
It checks the DECLARATION. It does not bind a council to an action - a
`route model` arm naming a council instead of a role is slice 2 - and there is
no runtime aggregator here: the answer type `Aggregate[T]` of the design's
section 3 is described and not written. See the design doc's slice plan for
what each of those adds and in which order.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import RevlError
from .model_route import CATEGORY as PLACE_CATEGORY
from .model_route import CODE as PLACE_CODE
from .model_route import CONFIDENTIALITY_ORIGINS
from .model_route import roles
from .taint import ORIGIN_CLASSES

# The guarantee this module's refusals enforce (issue #1190,
# `docs/design/557-council-disagreement.md`).
#
# It is a SECOND code rather than item 512's, and the reason is the one
# `revl.diagnostics` exists for: `classify()` hands an agent a `guarantee` line
# and a `fix` line so it can react without parsing prose, and item 512's pair
# says "route the origin to a role declared `on_device`, declare the role the
# arm names, or drop the arm". None of that is the rewrite for `on_tie allow`.
# A refusal whose machine-readable fix does not fix it is worse than no code,
# so the disagreement rules carry their own.
#
# The line BETWEEN the two codes is one sentence, and section 3 of the design
# note is the table:
#
#   a council refusal carries `G-MODEL-PLACE` exactly when it is about a
#   `model role` - one this program does not declare, or one whose name the
#   council also claims. Every other council refusal carries
#   `G-COUNCIL-SPLIT`, because what it refuses would let the council report
#   agreement it does not have.
CODE = "G-COUNCIL-SPLIT"
CATEGORY = "model-council"

# What a member is FOR. A CLOSED vocabulary, for the same reason `RESIDENCES`
# is closed: a typo is a refusal rather than a member with no job.
#
# `proposer`  - answers the question. A council with none produces no value to
#               aggregate, so every result would be inconclusive.
# `adversary` - answers against the proposal. This is the member a council is
#               usually built to place differently, because it is the one that
#               may need to read an origin the proposer may not.
# `verifier`  - answers about the proposal on its own terms.
#
# These are functions, NOT identities. A 471 vote comes from a bound operator
# and is a statement about consent; a council member is a model and carries no
# identity at all. The two must not be conflated, and nothing here may be read
# as an approval.
FUNCTIONS = ("proposer", "adversary", "verifier")

# The rules that turn the members' answers into one outcome.
#
# `unanimous` - every declared member answered and every answer is the same.
# `majority`  - strictly more than half of the DECLARED members gave one same
#               answer. Anything short of that names no value.
# `veto`      - the adversary's `deny` decides alone; otherwise `unanimous`
#               over the rest. It can only ever move the outcome toward
#               refusing, which is why it needs an adversary to exist.
#
# None of the three resolves a disagreement: each either names one value or
# names none, and naming none is the outcome `split`.
AGGREGATIONS = ("unanimous", "majority", "veto")

# Spellings that resolve disagreement toward whichever member answered. They
# parse so that the refusal can give the reason. This is the `*`-on-the-right
# discipline item 515 uses in `_model_route_candidate`.
PICK_ONE_AGGREGATIONS = ("first", "any", "fastest", "cheapest", "best",
                         "random")

# What the rule's floor is counted over. `declared` is the only admitted
# reading and the default; `answered` parses so the refusal can say why a
# floor over the answering set lets an unreachable member disappear.
QUORUM_BASES = ("declared",)
REFUSED_QUORUM_BASES = ("answered", "reachable", "available", "responding")

# What happens when the rule names no value. `split` is the default and the
# named outcome; `deny` is a permitted narrowing, because a council may decide
# that inconclusive means refuse. There is no admitting outcome, and the
# spellings for one parse so the refusal can be the roadmap's exit test.
TIE_OUTCOMES = ("split", "deny")
ADMITTING_TIE_OUTCOMES = ("allow", "admit", "proceed", "accept", "first",
                          "any")

# What a `reads <origin>` clause on a member may name (item 516 slice 4).
#
# The clause is the per-member INPUT: it says this member is given an origin
# the members without the clause are not. Only a CONFIDENTIALITY origin can be
# withheld from a member, because the ordinary question and its ordinary input
# go to every member - that is what a council IS, and design note 543's
# section 6.1 says so. So the admitted vocabulary is exactly the origins item
# 514's ceiling judges.
#
# `secret` parses and is refused BY NAME, the `on_tie allow` discipline: a
# capability-bound secret never reaches a model prompt at any residence
# (`G-SECRET-FLOW`), so there is no member this clause could name and the
# author should be told why rather than told the word is not in a list.
MEMBER_INPUT_ORIGINS = ("confidential",)
REFUSED_MEMBER_INPUT_ORIGINS = ("secret",)

_INPUT_VOCABULARY = ", ".join(MEMBER_INPUT_ORIGINS)

_FUNCTION_VOCABULARY = ", ".join(FUNCTIONS)
_AGGREGATION_VOCABULARY = ", ".join(AGGREGATIONS)
_TIE_VOCABULARY = ", ".join(TIE_OUTCOMES)
_QUORUM_VOCABULARY = ", ".join(QUORUM_BASES)

_DESIGN = "docs/design/543-model-council.md"


@dataclass(frozen=True)
class Member:
    """A validated council member: a function, the role that places it, and
    the origins it is GIVEN.

    `inputs` is item 516 slice 4. It is empty for a member declared without a
    `reads` clause, which is every member in every program that predates the
    slice."""
    function: str
    role: str
    residence: str
    line: int
    inputs: tuple = ()

    @property
    def off_device(self) -> bool:
        return self.residence == "off_device"

    def reads(self, origin: str) -> bool:
        return origin in self.inputs


@dataclass(frozen=True)
class Council:
    """A validated `model council` declaration.

    `residence` is the council's own admission ceiling and it is the MOST
    PERMISSIVE of its members', not the least. Giving an input to a council
    gives it to every member, so a council with one `off_device` member is
    `off_device` for the purpose of item 514's ceiling. Computing it the other
    way round would be the fail-open reading, and it is the single number slice
    2 reads from here.
    """
    name: str
    members: tuple
    rule: str
    quorum: str
    on_tie: str
    line: int

    @property
    def residence(self) -> str:
        return ("off_device" if any(m.off_device for m in self.members)
                else "on_device")

    def member(self, function: str):
        for m in self.members:
            if m.function == function:
                return m
        return None

    @property
    def floor(self) -> int:
        """How many agreeing answers the rule needs, over the DECLARED set."""
        n = len(self.members)
        if self.rule == "majority":
            return n // 2 + 1
        return n

    @property
    def scoped(self) -> bool:
        """True when any member declares a `reads` clause (item 516 slice 4).

        A council with none is asked ONE question with ONE input, which is
        section 6.1's stated slice-1 limit, and `residence` above is its
        ceiling. A council with one is asked one question whose CONFIDENTIAL
        part reaches only the members that declared it, and the ceiling is
        `ceiling(origin)` below.

        Held as a property of the whole council rather than of each member so
        that the two readings can never be mixed: a council either withholds
        by declaration or withholds from nobody. A per-member default would
        have made an undeclared member's input depend on what a SIBLING member
        declared, which is the silent pick this construct exists to remove.
        """
        return any(m.inputs for m in self.members)

    def receivers(self, origin: str) -> tuple:
        """The members this council gives `origin` to.

        Every member when the council declares no per-member input, which is
        the conservative join slice 1 and slice 2 enforce and the reading
        every program that predates slice 4 gets. Otherwise exactly the
        members whose `reads` clause names it - and that set may be EMPTY,
        which is a council that is given the origin and hands it to nobody.
        """
        if not self.scoped:
            return self.members
        return tuple(m for m in self.members if m.reads(origin))

    def ceiling(self, origin: str) -> str:
        """The residence item 514's rule compares for `origin`.

        The most permissive residence among the members that RECEIVE it. A
        council that gives the origin to nobody has no ceiling to compare, and
        `on_device` would read as permission; `receivers()` being empty is the
        case `revl.model_route` refuses by name rather than admitting here.
        """
        return ("off_device"
                if any(m.off_device for m in self.receivers(origin))
                else "on_device")


def _err(filename, line, message, hint, code=CODE, category=CATEGORY):
    return RevlError(filename, line, message, hint=hint, code=code,
                     category=category)


def _place_err(filename, line, message, hint):
    """A council refusal whose subject is a `model role`, not the aggregation.

    Two of them exist (design note 557 section 3): a member naming a role this
    program does not declare, and a council claiming a declared role's name.
    Both are answered by editing a `model role`, which is what item 512's fix
    line tells an agent to do, so both keep item 512's code."""
    return _err(filename, line, message, hint, code=PLACE_CODE,
                category=PLACE_CATEGORY)


def check(program, filename: str | None = None) -> dict[str, Council]:
    """Check every `model council` in the program against its role table.

    Returns `{name: Council}` for the councils that passed, which is what slice
    2's route arm and item 517's evidence record read. A program that declares
    no council walks an empty list and gets `{}`, so every program on the tree
    today is byte-identical through here.
    """
    filename = filename or program.filename
    decls = getattr(program, "model_councils", ()) or ()
    if not decls:
        return {}
    role_table = roles(program, filename)
    table: dict[str, Council] = {}
    for decl in decls:
        where = getattr(decl, "source", None) or filename
        council = _check_one(decl, role_table, table, where)
        table[council.name] = council
    return table


def _check_one(decl, role_table, seen, where) -> Council:
    # 1. one declaration per name. Two councils under one name would make which
    #    members answer depend on declaration order.
    if decl.name in seen:
        first = seen[decl.name]
        raise _err(
            where, decl.line,
            f"model council `{decl.name}` is declared twice (first on line "
            f"{first.line})",
            "a council is one declared member set and one aggregation, so a "
            "reference to it reads one of each and not two; give the second "
            "council its own name",
        )
    # 2. a council and a role share one namespace, because slice 2's route arm
    #    names either, and an arm naming an ambiguous word would be placed by
    #    whichever table was consulted first.
    if decl.name in role_table:
        role = role_table[decl.name]
        raise _place_err(
            where, decl.line,
            f"model council `{decl.name}` has the name of the model role "
            f"declared on line {role.line}",
            "a council and a role are both named by a route arm, so one word "
            "may not be both: a placement that depended on which table was "
            "read first is exactly the match-order failure `route model` "
            "already refuses",
        )

    # 3. members.
    members: list[Member] = []
    by_function: dict[str, Member] = {}
    by_role: dict[str, Member] = {}
    for raw in decl.members:
        if raw.function not in FUNCTIONS:
            raise _err(
                where, raw.line,
                f"unknown council function `{raw.function}` in model council "
                f"`{decl.name}`",
                f"a member declares what it is FOR, from a closed vocabulary, "
                f"so a typo is a refusal rather than a member with no job in "
                f"the aggregation; the vocabulary is: {_FUNCTION_VOCABULARY} "
                f"({_DESIGN})",
            )
        if raw.function in by_function:
            first = by_function[raw.function]
            raise _err(
                where, raw.line,
                f"council function `{raw.function}` is declared twice in model "
                f"council `{decl.name}` (first on line {first.line}, as "
                f"`{first.role}`)",
                "two members with one function make the aggregation depend on "
                "declaration order, and a rule counted over the declared set "
                "cannot say which of the two it counted; give the second "
                "member a different function",
            )
        placed = role_table.get(raw.role)
        if placed is None:
            known = ", ".join(sorted(role_table)) or "none"
            raise _place_err(
                where, raw.line,
                f"member `{raw.function}` of model council `{decl.name}` names "
                f"model role `{raw.role}`, which is not declared",
                f"every member of a council is PLACED, because placing them "
                f"separately is what the construct is for: a local adversary "
                f"may read an origin the cloud proposer may not, and item 514 "
                f"decides that per member. An undeclared name has no residence, "
                f"so the member cannot be placed and the council is refused "
                f"rather than assumed. Declare it: `model role {raw.role} "
                f"on_device` or `model role {raw.role} off_device`. Declared "
                f"roles: {known}",
            )
        if raw.role in by_role:
            first = by_role[raw.role]
            raise _err(
                where, raw.line,
                f"members `{first.function}` and `{raw.function}` of model "
                f"council `{decl.name}` are both placed on model role "
                f"`{raw.role}`",
                "two members on one placement is one model answering twice "
                "under two names: same weights, correlated errors, and no "
                "separate placement, which is the shape a council exists "
                "instead of. Give each member its own role",
            )
        # 3a. the member's own input (item 516 slice 4). The clause is what
        #     makes design note 543 section 6.1's sentence literally true, so
        #     every rule about it is about withholding: which origin may be
        #     withheld, that a member declares it once, and that a member
        #     GIVEN a confidentiality origin is not placed off the device.
        inputs: list[str] = []
        for clause in getattr(raw, "inputs", ()) or ():
            if clause.origin in REFUSED_MEMBER_INPUT_ORIGINS:
                raise _err(
                    where, clause.line,
                    f"member `{raw.function}` of model council `{decl.name}` "
                    f"is declared `reads {clause.origin}`",
                    f"a capability-bound {clause.origin} is a host-scope local "
                    f"handed to its capability's own provider call, and an LLM "
                    f"prompt is a disclosure sink for it at every residence, "
                    f"on the device or off it. There is no role this member "
                    f"could be placed on that would make the clause safe, so "
                    f"it is refused rather than placed. Drop it "
                    f"(G-SECRET-FLOW, {_DESIGN})",
                )
            if clause.origin not in ORIGIN_CLASSES:
                known = ", ".join(sorted(ORIGIN_CLASSES))
                raise _err(
                    where, clause.line,
                    f"unknown origin class `{clause.origin}` in member "
                    f"`{raw.function}` of model council `{decl.name}`",
                    f"the origin is validated at compile time so a typo is not "
                    f"a member silently given nothing; the origin classes are: "
                    f"{known}",
                )
            if clause.origin not in MEMBER_INPUT_ORIGINS:
                raise _err(
                    where, clause.line,
                    f"member `{raw.function}` of model council `{decl.name}` "
                    f"is declared `reads {clause.origin}`, which is not a "
                    f"confidentiality origin",
                    f"every member of a council is asked the same question "
                    f"with the same ordinary input - that is what a council IS "
                    f"- so the only thing a `reads` clause can say is which "
                    f"member is given an origin the others are WITHHELD from, "
                    f"and only a confidentiality origin is withheld from "
                    f"anyone. Naming `{clause.origin}` here would read as a "
                    f"restriction the council does not have. The vocabulary "
                    f"is: {_INPUT_VOCABULARY} ({_DESIGN})",
                )
            if clause.origin in inputs:
                raise _err(
                    where, clause.line,
                    f"member `{raw.function}` of model council `{decl.name}` "
                    f"reads `{clause.origin}` twice",
                    "a member is given an origin or it is not; writing the "
                    "clause twice says nothing the first one does not, and a "
                    "reader counting inputs would count two. Drop the second",
                )
            if clause.origin in CONFIDENTIALITY_ORIGINS and placed.off_device:
                raise _place_err(
                    where, clause.line,
                    f"member `{raw.function}` of model council `{decl.name}` "
                    f"is declared `reads {clause.origin}` and runs on model "
                    f"role `{raw.role}`, declared `{placed.residence}` on line "
                    f"{placed.line}: a {clause.origin} input may not leave the "
                    f"device",
                    f"the per-member input is what lets a LOCAL adversary read "
                    f"an origin the CLOUD proposer is not given; handing it to "
                    f"a member placed off the device is the same disclosure "
                    f"the council was built to avoid, written one level down. "
                    f"Place `{raw.function}` on a role declared `on_device`, "
                    f"or drop its `reads {clause.origin}` clause ({_DESIGN})",
                )
            inputs.append(clause.origin)

        member = Member(raw.function, raw.role, placed.residence, raw.line,
                        tuple(inputs))
        members.append(member)
        by_function[raw.function] = member
        by_role[raw.role] = member

    # 4. two members at least. One member wearing a council's name is the
    #    misrepresentation the item exists to remove: the caller believes
    #    several agreed.
    if len(members) < 2:
        have = len(members)
        raise _err(
            where, decl.line,
            f"model council `{decl.name}` declares {have} "
            f"member{'' if have == 1 else 's'}",
            "a council is several models with declared roles; with fewer than "
            "two there is nothing to aggregate, and a caller reading the word "
            "`council` would believe several agreed when one answered. Declare "
            f"a second member, or call the role directly ({_DESIGN})",
        )

    # 5. a proposer. Without one nothing produces a value, so every result
    #    would be inconclusive and the council could only ever refuse.
    if "proposer" not in by_function:
        raise _err(
            where, decl.line,
            f"model council `{decl.name}` declares no `proposer`",
            "an adversary and a verifier answer ABOUT a proposal; with no "
            "member to make one, the aggregation has nothing to name and every "
            "result is inconclusive. Declare `proposer -> <role>`",
        )

    # 6. exactly one aggregation, written down. This is the roadmap's
    #    "the aggregation function is total and must be written down".
    if not decl.aggregates:
        raise _err(
            where, decl.line,
            f"model council `{decl.name}` declares no `aggregate` rule",
            f"the rule that turns several answers into one outcome is the "
            f"council; with none, disagreement would be resolved by whatever "
            f"the caller did with the members, which is the silent pick this "
            f"construct removes. Write one: {_AGGREGATION_VOCABULARY} "
            f"({_DESIGN})",
        )
    if len(decl.aggregates) > 1:
        first, second = decl.aggregates[0], decl.aggregates[1]
        raise _err(
            where, second.line,
            f"model council `{decl.name}` declares two `aggregate` rules "
            f"(`{first.rule}` on line {first.line}, `{second.rule}` here)",
            "which rule aggregates would be declaration order, and the two can "
            "disagree about the same answers; a council has one rule",
        )
    clause = decl.aggregates[0]

    # 7. the rule itself.
    if clause.rule in PICK_ONE_AGGREGATIONS:
        raise _err(
            where, clause.line,
            f"`aggregate {clause.rule}` in model council `{decl.name}` "
            f"resolves disagreement toward one member's answer",
            f"a council that answers with whichever member answered is worse "
            f"than one model, because the caller believes several agreed. Every "
            f"admitted rule either names one value or names none, and naming "
            f"none is `split`: {_AGGREGATION_VOCABULARY} ({_DESIGN})",
        )
    if clause.rule not in AGGREGATIONS:
        raise _err(
            where, clause.line,
            f"unknown aggregation rule `{clause.rule}` in model council "
            f"`{decl.name}`",
            f"the rule is validated at compile time so a typo is not a silent "
            f"aggregation; the vocabulary is: {_AGGREGATION_VOCABULARY}",
        )
    # 8. `veto` needs an adversary to be the veto of.
    if clause.rule == "veto" and "adversary" not in by_function:
        declared = ", ".join(m.function for m in members)
        raise _err(
            where, clause.line,
            f"`aggregate veto` in model council `{decl.name}`, which declares "
            f"no `adversary`",
            f"`veto` is the adversary's `deny` deciding alone; with no "
            f"adversary the clause names a member that does not exist and "
            f"reads as a protection the council does not have. Declared "
            f"members: {declared}",
        )

    # 9. the quorum basis. The floor is counted over the declared set, so an
    #    abstention counts against it; counting over the answering set is how
    #    an unreachable member disappears.
    quorum = clause.quorum
    if quorum is None:
        quorum = "declared"
    elif quorum in REFUSED_QUORUM_BASES:
        raise _err(
            where, clause.line,
            f"`quorum {clause.quorum}` in model council `{decl.name}` counts "
            f"the rule's floor over the members that answered",
            "a member that fails or times out abstains, and a floor counted "
            "over the answering set lets the council shrink until the "
            "survivors agree: two members of three agreeing while the third is "
            "unreachable would read as agreement. The floor is counted over "
            f"the declared members, which is `quorum declared` and is the "
            f"default; the outcome when it is not met is `inquorate`, which "
            f"names the silent member ({_DESIGN})",
        )
    elif quorum not in QUORUM_BASES:
        raise _err(
            where, clause.line,
            f"unknown quorum basis `{clause.quorum}` in model council "
            f"`{decl.name}`",
            f"the basis is validated at compile time so a typo is not a silent "
            f"floor; the vocabulary is: {_QUORUM_VOCABULARY}",
        )

    # 10. what happens when the rule names no value. This is the roadmap's own
    #     exit test: an aggregation written to admit on a tie is refused.
    on_tie = clause.on_tie
    if on_tie is None:
        on_tie = "split"
    elif on_tie in ADMITTING_TIE_OUTCOMES:
        raise _err(
            where, clause.line,
            f"`on_tie {clause.on_tie}` in model council `{decl.name}` admits "
            f"when the members disagree",
            f"disagreement is never resolved toward allow: a tie is the case "
            f"where `{clause.rule}` names no value, and a council that answers "
            f"anyway has told the caller that several models agreed when they "
            f"did not. The outcomes are `split`, which is the default and "
            f"carries the members' differing answers as evidence, and `deny` "
            f"({_DESIGN})",
        )
    elif on_tie not in TIE_OUTCOMES:
        raise _err(
            where, clause.line,
            f"unknown tie outcome `{clause.on_tie}` in model council "
            f"`{decl.name}`",
            f"the outcome is validated at compile time so a typo is not a "
            f"silent resolution; the vocabulary is: {_TIE_VOCABULARY}",
        )

    return Council(decl.name, tuple(members), clause.rule, quorum, on_tie,
                   decl.line)
