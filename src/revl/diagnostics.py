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
_TAG = re.compile(r"\((G[1-9]|A[1-9]|R[1-5]|T[1-9])\)")

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
    "G9": "untrusted data cannot create authority without a declared declassification",
    "G-SECRET": "a capability-bound secret never leaves its capability's own "
                "extern bodies through any revl construct or declared crossing",
    "A1": "iteration boundaries exist only during activation",
    "A2": "no acquisition after a provision",
    "A3": "host-safe identifiers",
    "A5": "compensation accompanies an emission",
    "A6": "provide-methods match the service signature",
    "A8": "mid-body failure reverts and contains (L-Raise)",
    "A9": "a provide key is declared in the component's `provides` clause",
    "T1": "declared types are checked",
    "T2": "absence is Opt[T]; `null` has no type",
    "T3": "a hole is an obligation: it checks, but it never runs (docs/holes.md)",
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
    "G9": "an untrusted value cannot directly create authority — declassify it "
          "first: parse it with a `verified fn` that returns `Trusted[T]`, endorse "
          "it at a declared point (`endorse[<origin>](v, reason = \"...\")`), or "
          "gate it on a human approval",
    "G-SECRET": "a bound provider key has no declassifier and no allowed sink "
                "except a re-emission through its own bound capability - stop "
                "reflecting it into a revl value; a `secret NAME for CAP` value is "
                "a host-scope local handed straight to CAP's provider call",
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
    "A9": "the provide block's key must appear in the `provides` clause — rename "
          "the block to a declared key, or add the key (with its service) to the "
          "clause",
    "T1": "make the types agree at the call site, or change the declaration",
    "T2": "revl has no `null` — model absence as `Opt[T]` and unwrap with `??`, "
          "`?.` or `match`",
    # `hole` arrived with the typed-holes feature; this table is kept total by
    # test_explain_every_guarantee_has_a_fix, which is what caught its absence.
    "T3": "fill the hole in — a draft compiles, but it cannot be admitted into "
          "a running composition; `revl compile` lists every open obligation",
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
    (re.compile(r"is not a declared requirement"), "G1", "binding"),
    (re.compile(r"acquisition after `provide`"), "A2", "ordering"),
    (re.compile(r"no builtin method"), "T1", "stdlib"),
    (re.compile(r"verified fn .* is not total"), "G7", "totality"),
    # witnessed-inverse externs (item 243, docs/design/243-witnessed-externs.md).
    # These raises carry explicit codes, but the shapes are classified here too
    # so a message-only path still resolves to the `witnessed` category.
    (re.compile(r"witnessed extern .* cannot be called in "), "G4", "witnessed"),
    (re.compile(r"inverse of witnessed extern|witnessed extern .* must (return|declare)"
                r"|witness .* is a host object"), "G4", "witnessed"),
    # items 399/400: the acquire-with-`undo` and `deferred`-emission fn-body
    # refusals carry explicit codes too, classified here for the message-only path.
    (re.compile(r"`acquire` extern .* cannot be called in "), "G4", "acquire"),
    (re.compile(r"`deferred` emission extern .* cannot be called in "), "G4", "deferred"),
    (re.compile(r"unclassified extern"), "G8", "boundary"),
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
    if code in FIXES:
        # the exact rewrite, beside the guarantee, so an agent gets the fix
        # without a second `explain` call or parsing the prose hint
        record["fix"] = FIXES[code]
    # the derivation behind a search-based rejection (why.py): the G4
    # emission chain, the G3 cycle path, the two G2 providers
    why = getattr(error, "why", None)
    if why is not None:
        record["why"] = why.to_json()
    # item 274: the navigable-refusal map, copied verbatim beside the static
    # `fix`. Additive — a rejection with no `navigate` serializes exactly as
    # before, so `--json` consumers without navigate knowledge see a strict
    # superset. The record is already redacted for the untrusted-author view at
    # construction (navigate.py), so nothing here re-filters it.
    navigate = getattr(error, "navigate", None)
    if navigate is not None:
        record["navigate"] = navigate
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
    """A failed compile as an agent-consumable document.

    A multi-refusal compile raises a `RevlErrors` carrier (item 386) whose
    `.errors` holds every collected refusal; map `classify` over the list. A
    single `RevlError` (no `.errors`) still yields a one-element list, so every
    existing single-error consumer is unchanged.
    """
    errors = getattr(error, "errors", None) or [error]
    return {"ok": False, "diagnostics": [classify(e) for e in errors]}


def ok(**payload) -> dict:
    return {"ok": True, **payload}
