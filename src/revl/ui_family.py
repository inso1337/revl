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


# ----------------------------------------------------------- the ladder rungs
#
# Roadmap item 521, design 532 §4, Slice 3. The item's open decision was how
# far down the fallback ladder is admissible, and §4.2 settles it by making a
# RUNG PART OF THE CAPABILITY TOKEN: descending is crossing a different
# declared boundary, not retrying the same one.
#
#   ui.click              rung 0, a semantic target
#   ui.click.selector     rung 1, a structural query over a rendered document
#   ui.click.pixel        rung 2, a position
#
# WHY THE DEPTH HAS TO BE BOUNDED AT ALL, in §4.1's own terms. Rung 0 fails
# CLOSED by nature: an identity the application publishes either resolves or
# errors. Rungs 1 and 2 fail OPEN by nature: a selector matches a different
# control that satisfies it, and a pixel always hits something. That asymmetry
# is the reason, not a preference for typed APIs.
#
# WHICH VERBS CARRY RUNGS. The ones that ACT on a target, and only those. A
# rung is a way of NAMING a target for an actuation, and §4.1 states both
# lower rungs' failure direction as "the action succeeds on the wrong thing",
# which is a sentence about an action. `screen.observe` has no target at all.
# `ui.find` PRODUCES one from observed content, so a rung on it would name the
# substrate's resolution strategy, and §4.3 and §7 put the strategy and the
# order outside what revl claims. Slice 4's `bounds` field is the other half
# of this: a target records the region it was resolved within, which is what a
# lower rung has to stay inside.

#: rung name -> what it names, and how it fails. The text is the rule: the
#: refusals quote it, so an author learns the failure direction of the depth
#: they asked for rather than only that it was refused.
RUNGS: dict[str, str] = {
    "selector": "a structural query over a document the application renders; "
                "it is a derivation from content that may be "
                "attacker-influenced, and when the binding is wrong the query "
                "matches a DIFFERENT control that satisfies it and the action "
                "succeeds on the wrong thing",
    "pixel": "a position, which binds nothing at all: `(842, 611)` is not "
             "authority, and the same pixel is Delete, Send or Approve after "
             "a layout change",
}

#: rung name -> its depth below the semantic rung. A bare verb is depth 0.
RUNG_DEPTH: dict[str, int] = {"selector": 1, "pixel": 2}


def runged_verbs() -> tuple[str, ...]:
    """The verbs a rung may be spelled on: the ones that take a target.

    A function rather than a constant because `TARGET_CONSUMERS` is defined
    with the target record further down, and the two answer the same question:
    a rung names a target, and these are the verbs that receive one.
    """
    return TARGET_CONSUMERS


def rung_of(token: str) -> str | None:
    """The rung a declared token names, or `None` when it names the semantic
    rung or is not in this family. `ui.click.pixel` -> `pixel`."""
    segments = _bare(token).split(".")
    if len(segments) != 3 or segments[0] not in ROOTS:
        return None
    return segments[2] if segments[2] in RUNGS else None


def verb_of(token: str) -> str | None:
    """The VERB a token names, with any rung and any item-294 valuation
    stripped: `ui.click.pixel(app="Billing")` -> `ui.click`. `None` outside
    the family.

    This is what the per-verb tables in this module resolve through, so a rung
    cannot arrive carrying a weaker obligation than the verb it descends from
    just because it is spelled in a different file. `taint_roles` reaches the
    same answer by longest-prefix resolution; this is the explicit form, for
    the tables that are exact-match by nature.
    """
    segments = _bare(token).split(".")
    if len(segments) < 2 or segments[0] not in ROOTS:
        return None
    verb = f"{segments[0]}.{segments[1]}"
    return verb if segments[1] in VERBS.get(segments[0], {}) else None


def depth_of(token: str) -> int:
    """How far down the ladder a declared token reaches: 0 for a semantic
    target, 1 for a selector, 2 for a pixel, and 0 for anything outside the
    family - the answer that makes `max(depth_of(t) for t in reach)` safe over
    a mixed capability set."""
    rung = rung_of(token)
    return RUNG_DEPTH[rung] if rung is not None else 0


def prefix_of(token: str) -> str | None:
    """The token one rung SHALLOWER than this one, or `None` when there is
    none. `ui.click.pixel` -> `ui.click`; a semantic token has no shallower
    spelling."""
    return verb_of(token) if rung_of(token) is not None else None


def prefix_closure_refusal(component: str,
                           tokens) -> tuple[str, str] | None:
    """`(message, hint)` when a component's reached UI tokens are not
    prefix-closed, else `None`.

    Design 532 §4.2(4): `ui.click.pixel` is admissible only where `ui.click`
    is. A program that can reach pixels but not semantic targets has no
    ladder, it has a PIXEL DRIVER, and the ordering claim would be vacuous for
    it. This is the strongest form of "never inverts that order" revl can
    honestly check: a property of the DECLARATION, not of the loop. §4.3 says
    why the loop is not checkable here, and that is item 539's problem rather
    than a gap in this one.

    `tokens` is the component's reached capability tokens, off the G8 audit
    reach. Parameters are stripped, so a narrowed `ui.click(app="Billing")`
    closes a `ui.click.pixel`: item 294's valuations NARROW a capability, and
    a component holding the narrower semantic token still holds a semantic
    token. Reporting the lowest offending spelling first makes the diagnostic
    stable across two components with the same defect.
    """
    bare = {_bare(str(t)) for t in tokens or ()}
    for token in sorted(bare):
        shallower = prefix_of(token)
        if shallower is None or shallower in bare:
            continue
        rung = rung_of(token)
        return (
            f"component `{component}` reaches `{token}` without reaching "
            f"`{shallower}`",
            f"a rung is a strictly WEAKER way to name the same target "
            f"({RUNGS[rung]}), so it is admissible only where the semantic "
            f"target is too: a component that reaches {rung}s and not "
            f"semantic targets has no fallback ladder, it has a {rung} "
            f"driver, and the ordering a ladder claims is vacuous for it. "
            f"Reach `{shallower}` as well, or drop `{token}` "
            f"(G8, roadmap item 521, {DESIGN} §4.2)",
        )
    return None


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
    if len(segments) > 3:
        rungs = ", ".join(f"`{r}`" for r in sorted(RUNGS))
        return (
            f"`{token}` is deeper than the computer-use fallback ladder goes",
            f"the ladder has exactly three rungs: `{root}.{verb}` is the "
            f"semantic target and {rungs} are the two below it. A fourth "
            f"level would be a depth with no failure direction written down "
            f"for it, and an unbounded ladder is the same as no guarantee "
            f"(G8, roadmap item 521, {DESIGN} §4.2)",
        )
    if len(segments) == 3:
        rung = segments[2]
        if rung not in RUNGS:
            rungs = ", ".join(f"`{root}.{verb}.{r}`" for r in sorted(RUNGS))
            return (
                f"`{token}` is not a declared rung of the computer-use "
                f"fallback ladder",
                f"the rung set is CLOSED ({rungs}); a rung names HOW a target "
                f"is bound and each one carries its own failure direction, so "
                f"an invented rung is a depth an auditor cannot grade and a "
                f"`capability {root}.*.pixel` rule cannot select "
                f"(G8, roadmap item 521, {DESIGN} §4.1)",
            )
        if f"{root}.{verb}" not in runged_verbs():
            actuations = ", ".join(f"`{v}`" for v in runged_verbs())
            return (
                f"`{root}.{verb}` does not act on a target, so `{token}` "
                f"names no rung of the ladder",
                f"a rung is a way of NAMING A TARGET FOR AN ACTUATION, and "
                f"both lower rungs fail by the ACTION succeeding on the wrong "
                f"thing. The verbs that take a target are {actuations}; "
                f"`screen.observe` has none, and `ui.find` PRODUCES one, so a "
                f"rung there would name the substrate's resolution strategy, "
                f"which revl does not claim ({DESIGN} §4.3 and §7) "
                f"(G8, roadmap item 521, {DESIGN} §4.2)",
            )
    return None


# ------------------------------------------------- the reversibility classes
#
# Roadmap item 522 (issue #1196), docs/design/538-ui-transactions.md, Slice 1.
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
    could change the class would be an author-side opt-out.

    A LADDER RUNG resolves to its VERB's class (slice 3). `ui.click.pixel` is
    the same operation as `ui.click` reached by a weaker naming, so it cannot
    be more reversible than the verb it descends from; an exact-match lookup
    would have given a rung no class at all, which `teardown_refusal` reads as
    "not a computer-use verb" and lets past. That is the fail-open direction,
    reached by adding a spelling in a different file."""
    bare = token.split("(", 1)[0]
    if bare in REVERSIBILITY:
        return REVERSIBILITY[bare]
    verb = verb_of(bare)
    return REVERSIBILITY.get(verb) if verb is not None else None


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
            f"docs/design/538-ui-transactions.md)",
        )
    if cls == COMPENSATABLE and not has_compensate:
        return (
            f"`{kind}[{token}]` is compensatable, so extern `{name}` must "
            f"declare `compensate`",
            f"{OBLIGATION[cls]}; write `compensate <inverse>()` before the "
            f"`= @backend` body (G4, roadmap item 522, "
            f"docs/design/538-ui-transactions.md)",
        )
    return None


# ------------------------------------------------------- the taint roles
#
# Roadmap item 521 (issue #1195), docs/design/532-typed-computer-use.md §5,
# Slice 2.
#
# Item 249's derived classes read a capability token's dotted HEAD, which is
# why a reserved root is the granularity those tables want: `screen` joins the
# SOURCE classes and `ui` the SINK classes in `revl.taint`. But the head alone
# is too coarse for this family by exactly one verb, and the exception is the
# design's own reading rather than a convenience:
#
#   `ui.find` SITS ON BOTH SIDES. It consumes observed content and produces a
#   target, so it is a source whose output is a claim (design §5). If the head
#   rule made it a sink too, the family's canonical program - observe, find,
#   click - could not be written at all without endorsing the observation
#   BEFORE anything examined it, which is the opposite of what the item wants:
#   a target must stay `Untrusted` all the way to the actuation, and the
#   endorsement belongs at the actuation, where an operator can see what is
#   being claimed about it.
#
# So the taint role is REGISTRY-OWNED, per verb, exactly as the reversibility
# class above is, and for the same reason: a classification an author can
# lower is a classification a careless author lowers.

#: The return is attacker-influenced content. Under `taint_strict` the crossing
#: mints `SOURCE_ORIGIN` on its return with no author qualifier.
SOURCE = "source"

#: EVERY argument is a position where a value IS authority. A capability token
#: carries no parameter roles - item 294's parameters narrow the capability,
#: they do not name the parameters - so revl cannot tell `ui.text`'s target
#: from its value, and the honest derivation is all-arguments. That is the same
#: shape `shell`/`exec`/`terminal` already have, and per-parameter precision
#: remains available only through an explicit `Trusted[T]` annotation, which is
#: author-side and therefore never the derivation.
SINK = "sink"

#: token -> the roles it carries. Every admissible spelling appears exactly
#: once; the exhaustiveness is a test, not a convention.
#:
#: `screen.observe` reads pixels: pure source, and the one the item's premise
#: names ("a target discovered by looking at pixels is untrusted input").
#: `ui.find` derives a target from that content: source, not sink, per the
#: paragraph above. `ui.click` actuates: the target IS the authority, the same
#: position a shell string occupies. `ui.text` generates KEY INPUT, which is
#: not inert text - a newline submits, a tab moves focus, a shortcut is a
#: command - so observed content reaching it chooses what happens and not only
#: what is written. `ui.download` names what gets fetched onto the host, and an
#: untrusted name there is the file-on-the-host decision made by the screen.
TAINT_ROLES: dict[str, frozenset[str]] = {
    "screen.observe": frozenset({SOURCE}),
    "ui.find": frozenset({SOURCE}),
    "ui.click": frozenset({SINK}),
    "ui.text": frozenset({SINK}),
    "ui.download": frozenset({SINK}),
}

#: The coarse origin class a computer-use SOURCE verb mints. Both source verbs
#: mint `screen` rather than one minting `ui`: what makes a resolved target
#: untrusted is that it was derived by looking at a screen, and a second origin
#: name for the same provenance would let a policy rule written against one
#: spelling miss the other. `ui` names the SINK side of this family and is
#: deliberately not an origin.
SOURCE_ORIGIN = "screen"


def _bare(token: str) -> str:
    """The token with any item-294 parameter valuation stripped. A narrowed
    `ui.click(host="a")` is the same operation as `ui.click`; a valuation that
    could change a taint role would be an author-side opt-out."""
    return token.split("(", 1)[0]


def taint_roles(token: str) -> frozenset[str]:
    """The taint roles of a computer-use token; empty for anything else.

    Resolved by the LONGEST REGISTERED PREFIX, not by exact match, and that is
    the forward-safety this table owes the ladder. Slice 3 will admit the rung
    tokens (`ui.click.selector`, `ui.click.pixel`), and a rung is a strictly
    WEAKER way to name the same target - a selector may match a different
    control that satisfies it, a pixel always hits something - so a rung must
    never carry a weaker taint role than the verb it descends from. An exact
    match would have given `ui.click.pixel` no role at all, which is the
    fail-open direction, reached by adding a spelling in a different file.
    """
    segments = _bare(token).split(".")
    for depth in range(len(segments), 0, -1):
        roles = TAINT_ROLES.get(".".join(segments[:depth]))
        if roles:
            return roles
    return frozenset()


def is_taint_sink(token: str) -> bool:
    """Is every argument of this computer-use verb a taint sink?

    False for a token outside the family, so a caller may ask about any
    capability token. `revl.taint._sink_of` consults this for a token under a
    reserved root INSTEAD OF its head rule, which is what keeps `ui.find` off
    the sink side.

    A token under the `ui` root that this table cannot resolve at all is
    treated as a SINK. Such a token cannot be declared today (the verb set is
    closed and `refusal()` rejects it), so this is not a live path; it is the
    default that decides which way an unrecognised spelling falls if one ever
    reaches here - through an ambient manifest, say - and the default has to be
    the side that refuses.
    """
    bare = _bare(token)
    roles = taint_roles(bare)
    if roles:
        return SINK in roles
    return bare.split(".", 1)[0] == "ui"


def source_origin(token: str) -> str | None:
    """The origin class this computer-use verb mints on its return, or `None`
    when the verb is not a source. `revl.taint._origin_of` consults this before
    its head rule, because `ui.find`'s head is the SINK root and the head rule
    would otherwise mint the literal token as an origin nothing matches."""
    return SOURCE_ORIGIN if SOURCE in taint_roles(token) else None


# ------------------------------------------------------- the target record
#
# Roadmap item 521 (issue #1195), docs/design/565-ui-target-binding.md,
# Slice 4. Design 532 §5 ends by listing the binding a verified target
# carries, and then leaves it as prose. Slices 1 to 3 spell the family's
# AUTHORITY; this is the slice that spells its DATA, and the two are not the
# same claim.
#
# WHAT A BARE STRING TARGET CANNOT DO, written as the defect rather than as a
# preference. `ui.find` returning `Str` means an actuation names its target BY
# NAME, and a name is re-resolved at every use:
#
#   * nothing binds the actuation to the observation it came from. The
#     evidence that justified "this is the Approve button" is not carried, so
#     no later step can check that the control acted on is the control that
#     was looked at;
#   * nothing expires. A target resolved before a dialog opened is still
#     spellable afterwards, and the spelling still resolves - to a different
#     control. That is rung 1's failure direction (design 532 §4.1) reached
#     from rung 0, by waiting;
#   * nothing survives a phase boundary. Item 522's transaction re-resolves a
#     target by name at every phase, which is a check-to-use race at every
#     boundary, and its postcondition verdict is POSITIONAL for the same
#     reason - with no target identity to bind to, "a read follows an
#     actuation in the same method" is the strongest statement available
#     (PR #1287's own "what is not verified").
#
# A record answers all three by being ONE VALUE THAT TRAVELS: the evidence
# hash, the expiry and the identity are carried by the thing that is passed,
# so a later phase has them without asking the screen a second time.
#
# The field set is REGISTRY-OWNED and CLOSED, for the reason the reversibility
# class and the taint role are: a binding an author can drop is a binding a
# careless author drops, and a target missing its expiry is exactly the target
# whose staleness nothing can notice.

#: The reserved record name. A program that declares a computer-use verb
#: declares this record, and revl checks it. The record itself is the
#: AUTHOR'S - revl ships no `UiTarget` type, exactly as it ships no `ui.click`
#: extern - because the family is host-backed and its data shape is as
#: OS-specific as its effect surface. What revl owns is the FLOOR.
TARGET_TYPE = "UiTarget"

#: field -> the declared type it must carry. Checked as a FLOOR and not as a
#: ceiling: a field the registry does not name is the author's own and is
#: admitted, because refusing it would be a false refusal with no soundness
#: gain, while a MISSING field is a binding nothing can recover later.
TARGET_FIELDS: dict[str, str] = {
    "application": "Str",
    "window": "Str",
    "role": "Str",
    "name": "Str",
    "evidence": "Str",
    "action": "Str",
    "session": "Str",
    "bounds": "Str",
    "expiry": "Int",
    "confirm": "Bool",
}

#: field -> what it binds. This is the rule and not a comment: the refusals
#: below quote it, so an author reading a diagnostic learns why the field
#: exists rather than only that it is missing. Its keys are `TARGET_FIELDS`'
#: keys exactly; the equality is a test, not a convention.
TARGET_FIELD_BINDS: dict[str, str] = {
    "application": "the application the control belongs to - the coarsest "
                   "thing an operator recognises in an audit line",
    "window": "the window within that application; an application identity "
              "alone does not distinguish two documents open side by side",
    "role": "the accessibility-tree role, empty where the platform publishes "
            "none. Empty is a MEASUREMENT (no tree was available) and is not "
            "the same as absent",
    "name": "the control's own name within that role, which is what the "
            "author asked for and what an audit line can be read against",
    "evidence": "the screenshot, DOM or accessibility-tree evidence hash the "
                "binding was taken from. Without it nothing connects the "
                "actuation to the observation that justified it",
    "action": "the ONE action type this target admits. A target resolved for "
              "a read is not a target for a click, and a record that does "
              "not say so lets one become the other by being passed along",
    "session": "the user, session or task identity the resolution happened "
               "under, so a target cannot be carried into another one",
    "bounds": "the region or coordinate bound the target was resolved "
              "within, which is what a lower rung would have to stay inside",
    "expiry": "the instant the binding stops being a binding. A target with "
              "no expiry is a target whose staleness nothing can notice, "
              "which is the defect this record exists to remove",
    "confirm": "whether a human confirmation is required before acting. "
               "Item 522 owns the gate; the target carries the fact so the "
               "gate has something to read that the screen did not supply",
}

#: The verbs whose RETURN is a target. `screen.observe` is deliberately not
#: here: it returns observed CONTENT, and content is not a target - making it
#: one would mean every screen read minted authority, which is the opposite of
#: the item's premise.
TARGET_PRODUCERS: tuple[str, ...] = ("ui.find",)

#: The verbs that ACT on a target, each of which must receive one. This is the
#: half that closes the re-resolution race: an actuation that takes a `Str`
#: names its target again, and a name resolved twice is two targets.
TARGET_CONSUMERS: tuple[str, ...] = ("ui.click", "ui.text", "ui.download")


def target_record_shape() -> str:
    """The declaration an author has to write, rendered from the registry so
    the diagnostic and the table cannot drift (item 274: a refusal names the
    nearest allowed space)."""
    body = "\n".join(f"  {name}: {type_}"
                     for name, type_ in TARGET_FIELDS.items())
    return f"type {TARGET_TYPE} = {{\n{body}\n}}"


def target_record_refusal(
        declared: dict[str, str] | None) -> tuple[str, str] | None:
    """`(message, hint)` when the program's `UiTarget` does not carry the
    registry's binding, else `None`.

    `declared` is the record's field table (name -> declared type), or `None`
    when the program declares no `UiTarget` at all, or declares it as
    something other than a record.

    Two refusals, both fail-closed:

    1. NO RECORD. A program that declares a computer-use verb and no target
       record has nothing for the verb to carry. Refused with the shape.
    2. A MISSING OR MIS-TYPED FIELD. Reported one field at a time, nearest
       first in registry order, with what that field binds - the `expiry`
       case is the one design 532 §10 names as this slice's oracle, and it is
       not special-cased: it is refused by the same rule as the other nine.
    """
    if declared is None:
        return (
            f"a computer-use program must declare the target record "
            f"`{TARGET_TYPE}`",
            f"`ui.find` resolves a target and the actuation verbs act on one, "
            f"so the binding has to be a value that travels between them; "
            f"write\n\n{target_record_shape()}\n\n"
            f"(G8, roadmap item 521, docs/design/565-ui-target-binding.md)",
        )
    for name, type_ in TARGET_FIELDS.items():
        if name not in declared:
            return (
                f"the target record `{TARGET_TYPE}` does not carry `{name}`",
                f"{name} binds {TARGET_FIELD_BINDS[name]}; add "
                f"`{name}: {type_}` (G8, roadmap item 521, "
                f"docs/design/565-ui-target-binding.md)",
            )
        if declared[name] != type_:
            return (
                f"the target record `{TARGET_TYPE}` declares `{name}` as "
                f"`{declared[name]}`, not `{type_}`",
                f"{name} binds {TARGET_FIELD_BINDS[name]}, and the type is "
                f"registry-owned so every reader of a target agrees on what "
                f"it holds (G8, roadmap item 521, "
                f"docs/design/565-ui-target-binding.md)",
            )
    return None


def target_signature_refusal(token: str, kind: str, name: str,
                             returns: str | None,
                             param_types) -> tuple[str, str] | None:
    """`(message, hint)` when a computer-use extern's SIGNATURE does not carry
    the target, else `None`.

    `returns` and `param_types` are the declared types with any item-249
    qualifier already stripped, which is why this asks for `UiTarget` and not
    for `Untrusted[UiTarget]`. The `Untrusted` half is slice 2's DERIVATION:
    `ui.find` is a source and mints `screen` on its return with no author
    qualifier, under `taint_strict`. Requiring the author to write it here
    would contradict that slice's own argument - a classification an author
    writes is a classification an author can forget - so this slice owns the
    `UiTarget` half, which is not profile-gated, and slice 2 owns the
    `Untrusted` half, which is.

    A service method's `emission[...]` scope does NOT come through here, for
    the reason item 522's `teardown_refusal` gives: the obligation belongs to
    the declaration that actually crosses, and a service method declares an
    interface rather than a crossing.
    """
    bare = verb_of(token) or _bare(token)
    if bare in TARGET_PRODUCERS:
        if strip_qualifiers_shallow(returns) != TARGET_TYPE:
            shown = f"`{returns}`" if returns else "nothing"
            return (
                f"`{kind}[{bare}]` resolves a target, so extern `{name}` must "
                f"return `{TARGET_TYPE}`, not {shown}",
                f"a target returned as a bare value is a target named by "
                f"NAME, and a name is re-resolved at every use: nothing binds "
                f"the actuation to the observation it came from, nothing "
                f"expires, and nothing survives a phase boundary. Return "
                f"`{TARGET_TYPE}` (or `Untrusted[{TARGET_TYPE}]`, which is the "
                f"same declaration once the qualifier is read) (G8, roadmap "
                f"item 521, docs/design/565-ui-target-binding.md)",
            )
        return None
    if bare in TARGET_CONSUMERS:
        types = [strip_qualifiers_shallow(t) for t in (param_types or ())]
        if TARGET_TYPE not in types:
            shown = ", ".join(f"`{t}`" for t in types) or "no parameters"
            return (
                f"`{kind}[{bare}]` acts on a target, so extern `{name}` must "
                f"take a `{TARGET_TYPE}` parameter; it declares {shown}",
                f"an actuation that receives its target as a bare value "
                f"resolves it a second time, and a name resolved twice is two "
                f"targets - the check-to-use race a UI transaction hits at "
                f"every phase boundary (roadmap item 522). Declare the target "
                f"parameter as `{TARGET_TYPE}` (G8, roadmap item 521, "
                f"docs/design/565-ui-target-binding.md)",
            )
    return None


def strip_qualifiers_shallow(type_name: str | None) -> str | None:
    """`Untrusted[UiTarget]` -> `UiTarget`, and anything else unchanged.

    A leaf-module copy of the ONE case `revl.taint.strip_qualifiers` handles
    that matters here, so this module keeps its no-import property (every
    consumer imports IT, never the other way round). It is shallow on purpose:
    `List[UiTarget]` is NOT a target, it is a list, and a consumer that takes
    a list of targets has not named which one it acts on.
    """
    if not type_name:
        return type_name
    text = type_name.strip()
    for qualifier in ("Untrusted", "Trusted", "Secret"):
        head = qualifier + "["
        if text.startswith(head) and text.endswith("]"):
            return strip_qualifiers_shallow(text[len(head):-1])
    return text


# ------------------------------------------------- where a target comes from
#
# Roadmap item 521 (issue #1195), issue #1371,
# docs/design/565-ui-target-binding.md §7. Slice 4 landed the record and the
# two signature obligations, and §7 recorded what they do not claim: "revl
# checks the signature, not the dataflow between two crossings. A program may
# resolve a target and act on a different one." That is the check-to-use race,
# and the sentence after it - "the taint discipline of slice 2 is what bounds
# that" - is the half that measured wrong.
#
# WHAT `taint_strict` ACTUALLY BOUNDS, measured on `fc0d84ce`. `ui.find` is a
# SOURCE, so a resolved target carries the `screen` origin, and `ui.click` is
# an all-arguments SINK. So under `taint_strict`:
#
#   resolve, then click what was resolved          REFUSED   (untrusted -> sink)
#   click a `UiTarget` record literal the program wrote itself   ADMITTED
#
# The discipline named as the bound refuses the HONEST program and admits the
# forged one, because a literal is not untrusted data: nobody looked at a
# screen to write it, so there is no origin on it to refuse. Taint bounds what
# a target is DERIVED FROM. It cannot bound a target that is derived from
# nothing, and a target derived from nothing is exactly the one no observation
# justifies.
#
# THE INVARIANT THIS CLOSES ON, stated as the property rather than as the
# three refusals below: in an admitted program, every `UiTarget` value
# originates in a target-producing crossing. The refusals are placed so that
# the property holds by CONSTRUCTION rather than by a walk being complete: a
# target can only enter a program by being built (refused), by being declared
# at a host boundary that is not a resolution (refused), or by being returned
# from `ui.find`. There is no dataflow position to miss, because what is
# refused is the CONSTRUCTION and not the flow to a particular use.
#
# WHAT IT STILL DOES NOT CLAIM, and issue #1371 says so itself: not that the
# target is the one resolved for THIS step (a program that resolves two and
# acts on the second acted on a target it resolved), and not that the
# resolution is still fresh. Both need a substrate that actually resolves a
# target (item 539, upstream inso1337/revl-harness#11) and neither is promised
# here.

#: A target the program BUILT: a record literal carrying the declared target's
#: fields. Nothing looked at a screen to produce it.
CONSTRUCTED = "constructed"

#: A target the program EDITED: a functional update rewriting a registry-owned
#: field of a target that was resolved. The value is part one control's
#: binding and part another's.
REBOUND = "rebound"

#: A target a HOST BOUNDARY handed back without declaring the resolution: an
#: extern returning the record under some capability other than `ui.find`, or
#: under none.
MINTED = "minted"

#: origin -> what the program did, and why it is not a resolution. The text is
#: the rule, not a comment: the refusals quote it, so an author reads the
#: reason rather than only the verdict.
TARGET_ORIGINS: dict[str, str] = {
    CONSTRUCTED: "a record literal supplies all ten fields with no "
                 "observation behind any of them, so the evidence hash names "
                 "a screen nobody read and the expiry is an instant nobody "
                 "measured",
    REBOUND: "an update keeps the resolved target's evidence hash while "
             "renaming what it points at, so the binding says one control "
             "was looked at and the actuation lands on another",
    MINTED: "a host boundary that hands back a target has RESOLVED one, and "
            "a resolution that is not declared as one is a screen read that "
            "no capability bounds and no audit reach names",
}


def target_origin_refusal(origin: str, where: str) -> tuple[str, str] | None:
    """`(message, hint)` for a `UiTarget` that no target-producing crossing
    resolved, else `None` for an origin this does not refuse.

    `where` names the thing the diagnostic points at: the FIELD for an
    update, the EXTERN for a mint, and nothing for a literal (its line is
    already the diagnostic's). It is quoted into the message so two of these
    in one program are told apart by their text.

    All three are G8, like slice 4's own three, and for slice 4's reason: the
    target is what makes an actuation's reach enumerable, and a target the
    program wrote for itself is a crossing whose reach is whatever the author
    typed.
    """
    producers = ", ".join(f"`{p}`" for p in TARGET_PRODUCERS)
    if origin == CONSTRUCTED:
        return (
            f"`{TARGET_TYPE}` is built as a record literal here, which is a "
            f"target nothing resolved",
            f"{TARGET_ORIGINS[CONSTRUCTED]}. {producers} is what resolves a "
            f"target: it is the crossing that looks at the screen, and the "
            f"evidence it returns is the only thing connecting an actuation "
            f"to the observation that justified it. Note that the taint "
            f"discipline does NOT catch this - a literal carries no origin to "
            f"refuse, so a forged target is cleaner than a real one. Bind the "
            f"target with `emit <{TARGET_PRODUCERS[0]} extern>(...)` (G8, "
            f"roadmap item 521, docs/design/565-ui-target-binding.md §7)",
        )
    if origin == REBOUND:
        binds = TARGET_FIELD_BINDS.get(where, "part of the target's binding")
        return (
            f"the resolved target's `{where}` is rewritten before the "
            f"actuation, so the target acted on is not the target resolved",
            f"`{where}` binds {binds}, and the ten registry fields are ONE "
            f"binding: {TARGET_ORIGINS[REBOUND]}. Resolve the target you mean "
            f"with `{TARGET_PRODUCERS[0]}` instead of editing the one you "
            f"have; a field the registry does not name is the author's own "
            f"and may still be updated (G8, roadmap item 521, "
            f"docs/design/565-ui-target-binding.md §7)",
        )
    if origin == MINTED:
        return (
            f"extern `{where}` returns `{TARGET_TYPE}` without declaring "
            f"`emission[{TARGET_PRODUCERS[0]}]`",
            f"{TARGET_ORIGINS[MINTED]}. An actuation may only act on a target "
            f"a declared resolution produced, and a second host boundary "
            f"returning the record is the way around that rule rather than a "
            f"use of it. Declare this extern "
            f"`emission[{TARGET_PRODUCERS[0]}]`, or return something that is "
            f"not a target (G8, roadmap item 521, "
            f"docs/design/565-ui-target-binding.md §7)",
        )
    return None
