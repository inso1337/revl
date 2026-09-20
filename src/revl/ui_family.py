"""The reserved computer-use capability namespace (roadmap item 521,
docs/design/532-typed-computer-use.md, Slice 1).

A computer-use agent is the one program shape that breaks G8 by construction:
its reach is "whatever the GUI permits", which is not an enumerable boundary,
and its inputs (a screenshot, an OCR result, a window title) are
attacker-influenced. revl's answer is not a new stratum-1 construct - the
effect surface is OS- and toolkit-specific - but a HOST-BACKED EXTERN FAMILY
whose verbs are ordinary capability-scoped emissions. `ui.click` is a
capability token exactly like `db.write`, so every authority surface that
already reads capability tokens (the G4 subset check, the G8 audit reach,
`secret K for C`, the item-246 approval gate, the `capability <glob>` policy
rules) reads a UI verb for free.

That only works if the namespace is CLOSED. This module is the one place the
roots and the verb set are written down, and the one function that decides
whether a declared capability token in that namespace is admissible.

Three refusals, all fail-closed, all citing G8:

1. THE ROOT ALONE (`emission[ui]`, `emission[screen]`). The root names the
   whole GUI surface. That is the "act on anything" verb the item refuses to
   admit as `*`, and it is refused here rather than admitted and annotated,
   because an unenumerable boundary on the audit surface is a G8 hole whatever
   it is labelled.

2. AN UNDECLARED VERB (`emission[ui.drag]`). The verb set is closed, so
   adding a verb is a change to this table with an emission class and a
   documented failure direction, not a capability string an author invents.
   The failure direction matters: an open verb set means a typo
   (`emission[ui.clik]`) partitions the token's authority cone and silently
   escapes every `capability ui.click ...` rule written against it.

3. A DEEPER TOKEN (`emission[ui.click.pixel]`). The fallback ladder's lower
   rungs are a DECLARED, CHECKED depth (design §4), and Slice 1 admits only
   the top rung. A rung token is refused until the slice that checks its
   prefix-closure lands, because admitting the spelling before the check
   exists is precisely the fail-open shape: the declaration would outlive the
   thing meant to bound it.

What this module does NOT decide, and must not be read as deciding: whether a
given target actually matches its evidence at execution time, which rung an
agent loop tries first, or how a host drives a desktop. Those belong to the
computer-use substrate (roadmap item 539, upstream inso1337/revl-harness#11).
revl bounds the reach; it does not claim the order.
"""

from __future__ import annotations

#: The reserved roots. A declared capability token whose FIRST dotted segment
#: is one of these is in the computer-use namespace and is checked here.
ROOTS: tuple[str, ...] = ("screen", "ui")

#: CLOSED registry: root -> verb -> the emission class the verb names. The
#: class text is what `revl audit` and the design doc call the verb; it is not
#: a free-form comment, it is the one sentence that says what crosses.
VERBS: dict[str, dict[str, str]] = {
    "screen": {
        "observe": "read a screen region; the result is attacker-influenced "
                   "content and never a trusted reference",
    },
    "ui": {
        "find": "resolve a semantic target from observed content; the result "
                "is a claim requiring verification, not authority",
        "click": "actuate a semantic target",
        # `text`, not `type`: `type` is a RESERVED KEYWORD, so `ui.type` cannot
        # be spelled in a dotted capability token at the declaration site OR in
        # a `secret K for C` binding. A verb that can be declared but not
        # selected by the surfaces that bound it is worse than a verb with a
        # different name (design §3, "what the issue's sketch called `ui.type`").
        "text": "generate key input into a semantic target",
        "download": "a file arrives on the host - a real emission with its "
                    "own capability, never a side effect of a click",
    },
}

#: The design doc this namespace is specified by, cited in every refusal so a
#: reader lands on the argument rather than on the table.
DESIGN = "docs/design/532-typed-computer-use.md"


def spellings() -> list[str]:
    """Every admissible computer-use capability token, sorted. The refusal
    messages enumerate this rather than describing it, so the diagnostic and
    the registry cannot drift (item 274: a refusal names the nearest allowed
    space)."""
    return sorted(f"{root}.{verb}"
                  for root, verbs in VERBS.items() for verb in verbs)


def is_reserved(token: str) -> bool:
    """Is this DECLARED capability token in the computer-use namespace?

    Reads the first dotted segment only, so `ui` and `ui.anything` are both
    in the namespace and both reach `refusal()`. A token that is merely a
    SELECTOR (a `capability <glob>` policy rule, a `secret K for C` binding)
    does not come through here: this is the declaration-site check, and a
    policy written against a verb must keep working."""
    return token.split(".", 1)[0] in ROOTS


def refusal(token: str, kind: str) -> tuple[str, str] | None:
    """`(message, hint)` if this declared token is inadmissible, else `None`.

    `kind` is the classification the token was declared under (`emission` or
    `witnessed`), so the message quotes the declaration the author wrote.
    """
    if not is_reserved(token):
        return None
    segments = token.split(".")
    root = segments[0]
    allowed = ", ".join(f"`{s}`" for s in spellings())
    if len(segments) == 1:
        return (
            f"`{kind}[{root}]` names the whole GUI surface, which is not an "
            f"enumerable boundary",
            f"a computer-use capability names ONE verb ({allowed}); an "
            f"act-on-anything verb is the shape that makes a computer-use "
            f"agent unauditable, so it is refused at admission rather than "
            f"admitted as `*` (G8, roadmap item 521, {DESIGN})",
        )
    verb = segments[1]
    if verb not in VERBS[root]:
        return (
            f"`{token}` is not a declared computer-use verb",
            f"the computer-use verb set is CLOSED ({allowed}); a new verb is "
            f"a change to `revl.ui_family` with its own emission class, not a "
            f"capability string, because an invented token narrows nothing "
            f"and escapes every policy rule written against the real one "
            f"(G8, roadmap item 521, {DESIGN})",
        )
    if len(segments) > 2:
        return (
            f"`{token}` descends the computer-use fallback ladder, which is "
            f"not admissible yet",
            f"`{root}.{verb}` is the ladder's top rung, a semantic target; a "
            f"selector or pixel rung is a DISTINCT declared capability whose "
            f"prefix-closure is checked in a later slice, and it is refused "
            f"until that check exists rather than admitted ahead of it "
            f"(G8, roadmap item 521, {DESIGN} §4)",
        )
    return None


# ------------------------------------------------- the reversibility classes
#
# Roadmap item 522 (issue #1196), docs/design/536-ui-transactions.md, Slice 1.
#
# A UI transaction that treats an unclassified step as reversible is the
# fail-open shape: the "clean teardown" claim outlives the thing that was
# meant to bound it. So every computer-use verb carries a REVERSIBILITY CLASS,
# and the class is REGISTRY-OWNED. An author states what a verb is FOR; an
# author does not get to state that a click is reversible, for the same reason
# item 249 derives sink-ness from the granting side: a classification an
# author can lower is a classification a careless author lowers.

REVERSIBLE = "reversible"
COMPENSATABLE = "compensatable"
CONFIRM_REQUIRED = "confirm-required"
IRREVERSIBLE = "irreversible"
UNKNOWN = "unknown"

#: The five classes, and what the system does with each. The text is the rule,
#: not a comment: `docs/rejections.md` and the refusals below quote it.
OBLIGATION: dict[str, str] = {
    REVERSIBLE: "the step changes no state the target owns, so the "
                "transaction needs nothing from it: no compensation is "
                "registered and none is required",
    COMPENSATABLE: "an inverse exists but revl cannot synthesise it, so the "
                   "declaration MUST carry `compensate` - a compensatable "
                   "step with nothing registered is residue that was "
                   "avoidable, and the LIFO run would skip it silently",
    CONFIRM_REQUIRED: "the step may proceed only behind an explicit human "
                      "confirmation. No verb is BORN in this class: it is the "
                      "class a token is RAISED to by the operator's existing "
                      "approval authority, and Slice 1 does not implement the "
                      "raise (design doc §6, with the measurement)",
    IRREVERSIBLE: "no inverse exists. The transaction may not claim a clean "
                  "teardown for it: it reports `uncompensated`, and a "
                  "declared `compensate` is REFUSED rather than believed",
    UNKNOWN: "revl cannot tell, which the transaction must treat exactly as "
             "`irreversible` - the direction that is safe when the answer is "
             "missing. A click may trigger something no inverse describes, so "
             "a declared `compensate` is REFUSED here too",
}

CLASSES: tuple[str, ...] = (
    REVERSIBLE, COMPENSATABLE, CONFIRM_REQUIRED, IRREVERSIBLE, UNKNOWN)

#: token -> class. Every admissible spelling appears exactly once; the
#: exhaustiveness is a test, not a convention (an unclassified verb would
#: default to "not checked", which is the fail-open default this item exists
#: to remove).
#:
#: `screen.observe` and `ui.find` READ. Nothing in the target changes, so
#: there is nothing to undo. `ui.text` generates key input into a named field:
#: the inverse is restoring that field, which the author knows and revl does
#: not. `ui.click` actuates a control whose effect the application never
#: published, so revl has no answer at all. `ui.download` lands a file on the
#: host; deleting it afterwards does not un-fetch it, and the fetch may
#: already have been metered or logged on the other side.
REVERSIBILITY: dict[str, str] = {
    "screen.observe": REVERSIBLE,
    "ui.find": REVERSIBLE,
    "ui.text": COMPENSATABLE,
    "ui.click": UNKNOWN,
    "ui.download": IRREVERSIBLE,
}

#: The classes for which a declared inverse is a FALSE CLEANLINESS CLAIM. An
#: extern-declared `compensate` is what the residue and erase reports read to
#: mark a crossing compensated (item 254), so admitting one here would make
#: `no_residue` printable for a step that left residue - the exact outcome
#: the item's exit test forbids.
NO_INVERSE: frozenset[str] = frozenset({IRREVERSIBLE, UNKNOWN})


def reversibility(token: str) -> str | None:
    """The reversibility class of an admissible computer-use token, or `None`
    when the token is not one. Parameters (item 294) are stripped: a narrowed
    `ui.click(...)` is the same operation as `ui.click`, and a valuation that
    could change the class would be an author-side opt-out."""
    return REVERSIBILITY.get(token.split("(", 1)[0])


def teardown_refusal(token: str, kind: str, name: str,
                     has_compensate: bool) -> tuple[str, str] | None:
    """`(message, hint)` if this extern declaration's compensation does not
    match its verb's reversibility class, else `None`.

    Two refusals, both fail-closed, both G4 (`every mutation carries an
    inverse, or admits irreversibility`):

    1. A verb with NO INVERSE may not declare one. The declared `compensate`
       is not decoration: it is the fact the residue and erase reports read,
       so believing it turns `uncompensated` into `no_residue` for a step
       that left residue.

    2. A COMPENSATABLE verb must declare one. Its inverse exists and only the
       author can write it; a missing compensation is residue the transaction
       could have avoided and will not even be able to name.

    A service method's `emission[...]` scope does NOT come through here. The
    obligation belongs to the declaration that actually crosses, and a service
    method declares an interface, not a crossing."""
    cls = reversibility(token)
    if cls is None:
        return None
    if cls in NO_INVERSE and has_compensate:
        return (
            f"`{kind}[{token}]` is {cls}, so extern `{name}` may not declare "
            f"`compensate`",
            f"{OBLIGATION[cls]}; drop the `compensate` clause - a UI "
            f"transaction over this step must report `uncompensated`, and a "
            f"registered inverse revl cannot honour would let it print "
            f"`no_residue` instead (G4, roadmap item 522, "
            f"docs/design/536-ui-transactions.md)",
        )
    if cls == COMPENSATABLE and not has_compensate:
        return (
            f"`{kind}[{token}]` is compensatable, so extern `{name}` must "
            f"declare `compensate`",
            f"{OBLIGATION[cls]}; write `compensate <inverse>()` before the "
            f"`= @backend` body (G4, roadmap item 522, "
            f"docs/design/536-ui-transactions.md)",
        )
    return None
