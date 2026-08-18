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
}

# how to satisfy each guarantee — the one-line rewrite `revl explain <code>`
# prints, kept beside GUARANTEES so the pair stays in step. A code with no
# entry still explains itself through GUARANTEES.
FIXES = {
    "G1": "add the key to the component's `requires` clause, or drop the access",
    "G2": "one provider per key per realm — withdraw one component, or `isolate` "
          "them into different realms",
    "G3": "break the cycle: split the interface, or move the shared state into a "
          "third component both depend on",
    "G4": "give the mutation an `undo`, or admit it is irreversible — `emit` at the "
          "call site and `emission fn` on the service operation",
    "G5": "teardown may not register new effects; acquire during activation instead",
    "G6": "outside an effect form every statement is pure — bind the value with "
          "`let`, or wrap the call in `effect ... undo ...`",
    "G7": "teardown is derived and LIFO; a `verified fn` must be total, so make "
          "every recursive call structurally smaller",
    "G8": "keep the boundary enumerable — declare host code as an `extern` with a "
          "`pure`/`acquire`/`emission` classification",
    "A1": "`await` is an iteration boundary and exists only during activation — "
          "move it into the component body",
    "A2": "acquire everything before the first `provide`",
    "A3": "identifiers are rewritten to host-safe names automatically; rename the "
          "source binding if the collision was deliberate",
    "A5": "an emission takes `compensate <expr>` — best-effort cleanup for what "
          "cannot be undone",
    "A6": "a provide-method must match the service declaration: name, arity, "
          "parameter types, `async`",
    "A8": "a mid-body failure reverts and contains; `fail` belongs in a component "
          "activation body",
    "T1": "make the types agree at the call site, or change the declaration",
    "T2": "revl has no `null` — model absence as `Opt[T]` and unwrap with `??`, "
          "`?.` or `match`",
}


def explain(code: str) -> dict:
    """What a diagnostic code means and how to fix it — the `revl explain`
    payload. Unknown codes answer with the roster rather than nothing, so a
    typo is one command from the right code."""
    normalized = (code or "").strip().upper()
    if normalized not in GUARANTEES:
        return {"ok": False, "code": normalized,
                "message": f"no diagnostic code `{normalized}`",
                "known": sorted(GUARANTEES)}
    record = {"ok": True, "code": normalized, "guarantee": GUARANTEES[normalized]}
    if normalized in FIXES:
        record["fix"] = FIXES[normalized]
    return record


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
    # the derivation behind a search-based rejection (why.py): the G4
    # emission chain, the G3 cycle path, the two G2 providers
    why = getattr(error, "why", None)
    if why is not None:
        record["why"] = why.to_json()
    return record


def report(error: RevlError) -> dict:
    """A failed compile as an agent-consumable document."""
    return {"ok": False, "diagnostics": [classify(error)]}


def ok(**payload) -> dict:
    return {"ok": True, **payload}
