"""The reserved computer-use capability namespace (roadmap item 521,
docs/design/531-typed-computer-use.md, Slice 1).

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
DESIGN = "docs/design/531-typed-computer-use.md"


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
