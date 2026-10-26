#!/usr/bin/env python3
"""The "do not say / say instead" table for claims about revl's proofs.

One table, `PHRASINGS`, is the single place the project records which ways of
describing its formal layer it has already got wrong, or could get wrong, and
what to write instead. Three readers use it and nothing else:

  * `tools/check_vision_claims.py` (rule `formal-wording`) refuses each
    phrasing in the front-door documents when the ledger
    (`formal/STATUS.md`, read through `revl.cert`) does not support it, and
    prints the "say instead" wording beside the finding;
  * `tools/docgen.py` renders the table into `docs/guarantees.md`
    (`formal-wording` block), so the reader-facing page and the gate cannot
    disagree about what is refused;
  * `tests/test_check_vision_claims.py` fires every entry on its own
    `example`, so an entry whose pattern stopped matching its own example is
    a red test and not a silent entry.

The complement is the `formal-status` rule, which reads the plain form
"G<n> is full / partial / unproved". This table is for the wordings that rule
cannot see: a synonym for proved, a universal over all nine guarantees, an
"only these are full" list, a coverage or row claim, and the claim that the
compiler itself is verified.

Some entries are conditional on the ledger (a claim that G6 is proved is fine
the day the ledger says G6 is full and a defect the day it says partial).
Others are never true (the Lean layer proves statements about a model of the
admission judgment, checked against the shipped checker by a differential
oracle; it does not prove the compiler).

This module is pure: it reads no file and imports nothing from revl, so the
generator and the checker can both import it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Sequence

# --------------------------------------------------------------------------
# Guarantee-code groups: `G1`, `G2, G3 and G7`, `G2 through G9`.
# --------------------------------------------------------------------------
CODE = r"`?G\d+`?"
CODE_GROUP = (
    CODE
    + r"(?:(?:\s*,\s*(?:(?:and|to|through)\s+)?|\s+(?:and|to|through)\s+|[-–—]\s*)"
    + CODE
    + r")*"
)
CODE_RANGE_RE = re.compile(
    r"`?G(\d+)`?\s*(?:to|through|[-–—])\s*`?G?(\d+)`?",
    re.IGNORECASE,
)


def codes_in(group: str) -> List[str]:
    """Every guarantee code a group names, ranges expanded, in order."""
    def expand(match: "re.Match[str]") -> str:
        first, last = (int(value) for value in match.groups())
        step = 1 if first <= last else -1
        return " ".join("G%d" % n for n in range(first, last + step, step))

    expanded = CODE_RANGE_RE.sub(expand, group)
    return list(dict.fromkeys(re.findall(r"G\d+", expanded, re.IGNORECASE)))


# --------------------------------------------------------------------------
# The ledger summary a "say instead" can quote.
# --------------------------------------------------------------------------
Statuses = Mapping[str, str]  # code -> "full" | "partial" | "unproved"


def ledger_summary(statuses: Statuses) -> str:
    """`G1 partial; G2 to G9 full`: the ledger's numbered rows, runs collapsed."""
    ordered = sorted(statuses, key=lambda code: int(code[1:]))
    runs: List[List[str]] = []
    for code in ordered:
        if (runs and statuses[runs[-1][0]] == statuses[code]
                and int(runs[-1][-1][1:]) + 1 == int(code[1:])):
            runs[-1].append(code)
        else:
            runs.append([code])
    parts = []
    for run in runs:
        label = run[0] if len(run) == 1 else "%s to %s" % (run[0], run[-1])
        parts.append("%s %s" % (label, statuses[run[0]]))
    return "; ".join(parts)


# --------------------------------------------------------------------------
# What makes a phrasing true. `codes` is what the match named (may be empty).
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Ledger:
    """What the phrasings are judged against: the numbered rows' normalized
    statuses, and whether any row at all (numbered or not) states UNPROVED.

    The second field exists because a ledger can carry an UNPROVED row that is
    not a numbered guarantee: before issue #2108 G9's path coverage was its own
    row, while the numbered G9 row read `partial`."""
    statuses: Statuses
    unproved_row: bool = False


_GUARANTEE_ROW_LABEL = re.compile(r"G\d+\b")


def unproved_row_in(ledger_text: str) -> bool:
    """True when a guarantee row states UNPROVED.

    A guarantee row is a table row whose first cell is a G-code label, so the
    numbered rows and their unnumbered sub-rows (`G9 path coverage`) count and
    an UNPROVED cell in an unrelated table (a `T` row, a registry) does not."""
    for line in ledger_text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip().strip("*").strip() for c in line.strip().strip("|").split("|")]
        if not cells or not _GUARANTEE_ROW_LABEL.match(cells[0]):
            continue
        if any(c.lower().startswith("unproved") for c in cells[1:]):
            return True
    return False


def _every_named_code_is_full(codes: Sequence[str], ledger: Ledger) -> bool:
    return bool(codes) and all(ledger.statuses.get(c) == "full" for c in codes)


def _all_numbered_rows_are_full(codes: Sequence[str], ledger: Ledger) -> bool:
    return bool(ledger.statuses) and all(
        v == "full" for v in ledger.statuses.values())


def _named_codes_are_exactly_the_full_rows(
    codes: Sequence[str], ledger: Ledger
) -> bool:
    return set(codes) == {c for c, v in ledger.statuses.items() if v == "full"}


def _every_named_code_is_not_full(codes: Sequence[str], ledger: Ledger) -> bool:
    """A coverage-unproved sentence is true while the guarantee is not `full`:
    its numbered row reads `partial` when the rule is proved and the coverage
    is not, so `unproved` coverage never shows as an `unproved` numbered row."""
    return bool(codes) and all(
        c in ledger.statuses and ledger.statuses[c] != "full" for c in codes)


def _some_row_is_unproved(codes: Sequence[str], ledger: Ledger) -> bool:
    return ledger.unproved_row or any(
        v == "unproved" for v in ledger.statuses.values())


def _never(codes: Sequence[str], ledger: Ledger) -> bool:
    return False


@dataclass(frozen=True)
class Phrasing:
    key: str
    pattern: "re.Pattern[str]"
    holds: Callable[[Sequence[str], Ledger], bool]
    example: str          # a sentence this entry refuses (the "do not say")
    say_instead: str      # the wording to use instead
    refused_when: str     # one line: when the phrasing is wrong


# "Proved" in the sense of a proof. Plain "verified" and "checked" are not
# here: they also say "verified at compile time" or "checked by the checker",
# which are claims about the compiler's own checks. "verified" counts only
# after formally / mechanically / machine.
_STRONG = r"(?:formally|mechanically|machine)[\s-]+"
_PROVED_WORD = (
    r"(?:(?:fully|completely|rigorously|entirely|now|also)\s+)*"
    r"(?:" + _STRONG + r"(?:proved|proven|verified|checked)"
    r"|proved|proven|machine-checked)"
)
# The words between a subject and its predicate.
_AUX = (
    r"(?:(?:are|is|was|were|be|been|has|have|had|remain|remains|stay|stays|"
    r"also|all|now|already|still|currently)\s+)*"
)

PHRASINGS: Sequence[Phrasing] = (
    Phrasing(
        key="proved-synonym",
        pattern=re.compile(
            r"(?P<codes>" + CODE_GROUP + r")"
            r"\s+" + _AUX + _PROVED_WORD + r"(?![A-Za-z0-9_-])",
            re.IGNORECASE),
        holds=_every_named_code_is_full,
        example="G1 is proved in Lean.",
        say_instead=(
            "Use the status word the ledger records for that code, with the "
            "granularity its row states, and point to formal/STATUS.md for the "
            "row's scope."),
        refused_when="a named guarantee is not `full` in formal/STATUS.md",
    ),
    Phrasing(
        key="all-guarantees-proved",
        pattern=re.compile(
            r"\b(?:all|every|each)(?:\s+one)?(?:\s+of)?\s+(?:the\s+)?"
            r"(?:(?:nine|9)(?:\s+(?:gate\s+)?guarantees?)?|(?:gate\s+)?guarantees?)"
            r"\s+" + _AUX + r"(?:`?full`?|" + _PROVED_WORD + r")(?![A-Za-z0-9_-])",
            re.IGNORECASE),
        holds=_all_numbered_rows_are_full,
        example="All nine guarantees are proved.",
        say_instead=(
            "Name the rows by status as the ledger records them (the Formal "
            "status column above), each at the granularity its row states."),
        refused_when="any numbered row (G1 to G9) is not `full`",
    ),
    Phrasing(
        key="only-these-are-full",
        pattern=re.compile(
            r"\bonly\s+(?P<codes>" + CODE_GROUP + r")\s+" + _AUX +
            r"(?:`?full`?|" + _PROVED_WORD + r")(?![A-Za-z0-9_-])",
            re.IGNORECASE),
        holds=_named_codes_are_exactly_the_full_rows,
        example="Only G2, G3 and G7 are full.",
        say_instead=(
            "Say which rows are `full` and which are not, from the ledger, "
            "instead of an \"only\" list that drifts when a row lands."),
        refused_when="the codes named are not exactly the `full` rows",
    ),
    Phrasing(
        key="coverage-unproved",
        pattern=re.compile(
            r"(?P<codes>G\d+)(?:['’]s)?\s+(?:path\s+)?coverage\s+"
            r"(?:is|remains|stays|was)\s+(?:still\s+)?(?:the\s+one\s+)?"
            r"`?(?:unproved|not\s+(?:yet\s+)?proved)`?(?![A-Za-z0-9_-])",
            re.IGNORECASE),
        holds=_every_named_code_is_not_full,
        example="G9's coverage is unproved.",
        say_instead=(
            "State the row as the ledger does, including what the row covers "
            "(the rule, the coverage of the walk)."),
        refused_when="the named guarantee is `full` in formal/STATUS.md",
    ),
    Phrasing(
        key="unproved-row",
        pattern=re.compile(
            r"\bthe\s+(?:one|only|last|remaining)\s+`?UNPROVED`?\s+row\b",
            re.IGNORECASE),
        holds=_some_row_is_unproved,
        example="Path coverage is the one UNPROVED row.",
        say_instead=(
            "Do not count unproved rows in prose; say which rows are `partial` "
            "(from the ledger) and point to formal/STATUS.md."),
        refused_when="no row of formal/STATUS.md, numbered or not, is UNPROVED",
    ),
    Phrasing(
        key="compiler-verified",
        pattern=re.compile(
            r"\b(?:(?:(?:the|revl['’]s|our|this)\s+(?:revl\s+)?compiler|revl\s+compiler)\s+"
            r"(?:is|was|is\s+now|has\s+been|have\s+been)\s+"
            + _PROVED_WORD + r"(?:\s+correct)?"
            r"|" + _STRONG + r"(?:proved|proven|verified)\s+compiler"
            r"|(?:proved|proven|verified)[-\s]correct\s+compiler)"
            r"(?![A-Za-z0-9_-])",
            re.IGNORECASE),
        holds=_never,
        example="The compiler is formally verified.",
        say_instead=(
            "The Lean layer proves statements about a model of the admission "
            "judgment, and a differential oracle compares that model with the "
            "shipped checker on a corpus. Say that, and link formal/STATUS.md."),
        refused_when="always: no proof covers the compiler itself",
    ),
    Phrasing(
        key="ledger-count-in-prose",
        pattern=re.compile(
            r"\b\d[\d,]*\s+(?:Lean\s+)?(?:theorems?|oracle\s+rows?|"
            r"differential\s+rows?|corpus\s+rows?)\b",
            re.IGNORECASE),
        holds=_never,
        example="The ledger records 182 oracle rows.",
        say_instead=(
            "Do not copy theorem or oracle-row totals into prose, they change "
            "with every formal landing. Point to formal/STATUS.md."),
        refused_when="always: the total belongs to the ledger, not to prose",
    ),
)


def by_key() -> Dict[str, Phrasing]:
    return {p.key: p for p in PHRASINGS}


def render_table() -> str:
    """The markdown the `formal-wording` docgen block holds."""
    out = [
        "| Do not say | Say instead | Refused when |",
        "| ---------- | ----------- | ------------ |",
    ]
    for p in PHRASINGS:
        out.append("| \"%s\" | %s | %s |" % (p.example, p.say_instead, p.refused_when))
    return "\n".join(out)
