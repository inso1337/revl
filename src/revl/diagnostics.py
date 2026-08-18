"""Structured diagnostics — the agent-facing projection of a RevlError.

Human-facing rendering stays `file:line: message` + hint (DESIGN §9). This
module adds the machine-facing view: a stable code, the guarantee the
rejection enforces, and the expected/actual pair where one exists, so an
agent can react to a rejection without parsing prose.

Codes are derived, not hand-maintained at ~100 raise sites: a rejection
that names its guarantee in the message (the `(G4)` convention) carries it
through, and the table below classifies the rest by shape.
"""

from __future__ import annotations

import re

from .errors import RevlError

# guarantee/amendment tag embedded in the message, e.g. "... (G4)"
_TAG = re.compile(r"\((G[1-8]|A[1-8]|R[1-5]|T[1-9])\)")

# what each guarantee is *about* — the one-line description an agent can
# surface without reading DESIGN.md
GUARANTEES = {
    "G1": "declared access: a component reads only what it requires",
    "G2": "provision disjointness: one provider per key (per realm)",
    "G3": "acyclic dependencies: a cycle can never activate",
    "G4": "every mutation carries an inverse, or admits irreversibility with `emit`",
    "G5": "teardown cannot register effects",
    "G6": "purity outside effect forms",
    "G7": "derived LIFO teardown",
    "G8": "the boundary surface is enumerable",
    "A1": "iteration boundaries exist only during activation",
    "A2": "no acquisition after a provision",
    "A3": "host-safe identifiers",
    "A5": "compensation accompanies an emission",
    "A6": "provide-methods match the service signature",
    "A8": "mid-body failure reverts and contains (L-Raise)",
    "T1": "declared types are checked",
    "T2": "absence is Opt[T]; `null` has no type",
    "T3": "a hole is an obligation: it checks, but it never runs (docs/holes.md)",
}

# message-shape -> (code, category) for rejections that carry no tag
_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"^`null` has no type"), "T2", "null-safety"),
    (re.compile(r"expects `.*`, got `"), "T1", "type-mismatch"),
    (re.compile(r"non-exhaustive match"), "T1", "exhaustiveness"),
    (re.compile(r"has no field |record literal for "), "T1", "type-mismatch"),
    (re.compile(r"takes \d+ (type )?argument|arity does not match"), "T1", "arity"),
    (re.compile(r"is not a method of service|is not declared by service"), "A6", "interface"),
    (re.compile(r"differs from the running manifest"), "G2", "admission"),
    (re.compile(r"provision conflict"), "G2", "linking"),
    (re.compile(r"dependency cycle|import cycle"), "G3", "linking"),
    (re.compile(r"cannot reassign|already (declared|bound)"), "G6", "binding"),
    (re.compile(r"is not declared in this (function|component)"), "G1", "binding"),
    (re.compile(r"no builtin method"), "T1", "stdlib"),
    (re.compile(r"verified fn .* is not total"), "G7", "totality"),
    (re.compile(r"expected .*, found "), "SYNTAX", "parse"),
    (re.compile(r"unexpected character|unterminated string"), "SYNTAX", "lex"),
]


def classify(error: RevlError) -> dict:
    """One rejection as a structured record."""
    code = getattr(error, "code", None)
    category = getattr(error, "category", None)
    if code is None:
        # the guarantee tag may sit in the message or in the fix hint
        tag = _TAG.search(error.message) or _TAG.search(error.hint or "")
        if tag:
            code = tag.group(1)
            category = category or "guarantee"
        else:
            for pattern, mapped_code, mapped_category in _PATTERNS:
                if pattern.search(error.message):
                    code, category = mapped_code, mapped_category
                    break
    record = {
        "severity": "error",
        "code": code or "REVL",
        "category": category or "check",
        "file": error.filename,
        "line": error.line,
        "message": error.message,
    }
    if error.hint:
        record["hint"] = error.hint
    expected = getattr(error, "expected", None)
    actual = getattr(error, "actual", None)
    if expected is not None or actual is not None:
        record["expected"] = expected
        record["actual"] = actual
    if code in GUARANTEES:
        record["guarantee"] = GUARANTEES[code]
    return record


def obligations(holes: list[dict]) -> dict:
    """Open typed holes as an agent-consumable document (docs/holes.md).

    Severity is `obligation`, not `error`: the draft compiled. It is the
    admission gate that says no while any of these is open, and an agent
    should treat the list as its remaining work, not as a rejection.
    """
    return {
        "ok": True,
        "holes": [
            {
                "severity": "obligation",
                "code": "T3",
                "category": "hole",
                "file": hole.get("file"),
                "line": hole.get("line"),
                "expected": hole.get("type"),
                "message": hole.get("message"),
                "guarantee": GUARANTEES["T3"],
            }
            for hole in holes
        ],
    }


def report(error: RevlError) -> dict:
    """A failed compile as an agent-consumable document."""
    return {"ok": False, "diagnostics": [classify(error)]}


def ok(**payload) -> dict:
    return {"ok": True, **payload}
