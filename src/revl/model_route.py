"""Model placement as a checked route condition (roadmap item 512).

`docs/design/531-model-placement.md` is the design; this module is the whole
rule set behind it, kept in one file so each refusal sits beside the rule it
enforces (the `revl.retention` discipline: the parser reads the shape, this
reads the meaning).

The surface is two declarations:

    model role local  on_device
    model role cloud  off_device

    component Classifier requires llm: Completions provides out: Answer {
      route model on classify {
        confidential -> local,
        * -> cloud
      }
      provide out { fn classify(text: Str) -> Label = ... }
    }

A `model role` is a DECLARED PLACEMENT: a name plus where a call to it runs.
A `route model` block places one action's model calls by the ORIGIN CLASS of
what the action is given, reusing the item-249 lattice rather than inventing a
second vocabulary.

WHICH WAY EVERY DECISION FAILS
------------------------------
Every rule below refuses when it is unsure. There is no "assume the role is
fine", no "assume the residence is the safe one", and above all no implicit
arm: an origin no arm names is NOT routed, and a route that names nothing
places nothing. The bug this shape exists to avoid is the one the repo keeps
finding - state keyed to a thing that outlived the thing meant to bound it,
failing open - and its model-placement form is a route that quietly admits
"any model" for an input it did not think about.

The catch-all `*` is the one place that reading could go wrong, so it is
narrowed by definition: **`*` never covers a confidentiality origin.** It
stands for the origin classes not named by another arm AND not in
`CONFIDENTIALITY_ORIGINS`. A confidential value is placed only by an arm that
names `confidential`, or it is not placed at all. Writing `* -> cloud` can
therefore never be the sentence that sends a confidential input off the
device.

THE VALUE SIDE (item 514)
-------------------------
`check()` decides the DECLARATION. `admits()` at the bottom of this file
decides a VALUE: given one action's arms and the origin a value actually
carries, it says whether that value may cross into this action's model call.
`revl.taint` calls it at every `model.*` crossing, which is what makes the `*`
rule above bite on a real value rather than on a written arm.

Its three refusing verdicts all fail closed, and the ordering is deliberate:
an action a routed component did not route places nothing, an origin no arm
names is not placed by `*`, and a placement that is `off_device` is refused
even though `check()` already refused writing it. None of them defaults to
"permitted because the placement could not be determined".

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not select a model at run time: item 512 is a permission, not a
scheduler (item 515 owns the scheduling inside the boundary this draws). The
ceiling is a CONFIDENTIALITY ceiling: it is the item-514 join of item 249's
lattice with a declared residence, and it says nothing about which roles an
action's non-confidential origins reach. That is the crossing side, slice 4.
See the design doc's non-goals.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import RevlError
from .taint import ORIGIN_CLASSES

# Where a call to a role executes. A CLOSED vocabulary, checked at compile time
# so a typo is a refusal rather than a silent fallback - the same reason
# `KNOWN_STRATEGIES` is closed for `isolate ... in realms(...)`.
#
# `on_device`  - the call runs on the device the component runs on; the prompt
#                does not leave it.
# `off_device` - the call leaves the device. Everything else about it (which
#                host, which operator, which jurisdiction) is a property of the
#                provider, not of this declaration, and is item 515's.
RESIDENCES = ("on_device", "off_device")

# The origins a model role may never receive, whatever its residence. `secret`
# is a capability-bound provider key (item 256): G-SECRET-FLOW already names an
# LLM prompt as a disclosure sink for it, so an arm that places one is a
# declaration contradicting a shipped guarantee, not a new policy.
# `confidential` is the `Secret[T]` value qualifier, which an `on_device` role
# may receive and an `off_device` role may not.
CONFIDENTIALITY_ORIGINS = ("confidential", "secret")

# The code every refusal in this file carries, except the `secret` arm, which
# cites the guarantee that already forbids it.
CODE = "G-MODEL-PLACE"
CATEGORY = "model-placement"

# The capability scope head that makes a crossing a MODEL crossing (item 343's
# `model.<op>` token, already in `taint._SOURCE_CLASS_SCOPES`). A crossing is a
# model call because of the capability it declares, never because of its name
# or an author annotation - the `retention.persistence_sink_of` discipline, and
# for the same reason: the side that grants the authority is the side that
# knows what the crossing is.
MODEL_SCOPE = "model"

_ORIGIN_VOCABULARY = ", ".join(sorted(ORIGIN_CLASSES) + ["*"])
_RESIDENCE_VOCABULARY = ", ".join(RESIDENCES)


@dataclass(frozen=True)
class Role:
    """A validated `model role` declaration."""
    name: str
    residence: str
    line: int

    @property
    def off_device(self) -> bool:
        return self.residence == "off_device"


def roles(program, filename: str | None = None) -> dict[str, Role]:
    """The program's `model role` table, validated.

    Refuses an unknown residence and a re-declared name. A program that
    declares no role returns `{}` and pays one empty-list test, so every
    program on the tree today is byte-identical through here.
    """
    filename = filename or program.filename
    table: dict[str, Role] = {}
    for decl in getattr(program, "model_roles", ()) or ():
        if decl.residence not in RESIDENCES:
            raise RevlError(
                filename, decl.line,
                f"unknown residence `{decl.residence}` for model role "
                f"`{decl.name}`",
                hint=f"a residence is validated at compile time so a typo is not "
                     f"a silent placement; the vocabulary is: "
                     f"{_RESIDENCE_VOCABULARY}. `on_device` means the call does "
                     f"not leave the device, `off_device` that it does "
                     f"(docs/design/531-model-placement.md)",
                code=CODE, category=CATEGORY,
            )
        if decl.name in table:
            first = table[decl.name]
            raise RevlError(
                filename, decl.line,
                f"model role `{decl.name}` is declared twice (first on line "
                f"{first.line}, as `{first.residence}`; here as "
                f"`{decl.residence}`)",
                hint="a role is one declared placement, so a route arm naming it "
                     "reads one residence and not two; give the second placement "
                     "its own role name",
                code=CODE, category=CATEGORY,
            )
        table[decl.name] = Role(decl.name, decl.residence, decl.line)
    return table


def _action_names(component) -> set[str]:
    """Every provide-method name the component declares.

    A `route model on <action>` names one of these. Read straight off the AST,
    because the route is a PRELUDE declaration and is validated before the
    provide blocks are lowered.
    """
    from .parser import ProvideStmt  # noqa: PLC0415 - import cycle

    names: set[str] = set()
    for stmt in component.body:
        if isinstance(stmt, ProvideStmt):
            names.update(m.name for m in stmt.methods)
    return names


def check(program, filename: str | None = None) -> dict[str, dict[str, dict]]:
    """Check every `route model` block in the program against its role table.

    Returns `{component: {action: {origin: role}}}` for the blocks that passed,
    which is what item 514's flow walk will read. A program with no block gets
    `{}`.
    """
    from .parser import ModelRouteStmt  # noqa: PLC0415 - import cycle

    filename = filename or program.filename
    table = roles(program, filename)
    placed: dict[str, dict[str, dict]] = {}
    for comp in program.components:
        where = comp.source or filename
        actions: dict[str, dict] = {}
        declared_actions: set[str] | None = None
        for stmt in comp.body:
            if not isinstance(stmt, ModelRouteStmt):
                continue
            if declared_actions is None:
                declared_actions = _action_names(comp)
            if stmt.action in actions:
                raise RevlError(
                    where, stmt.line,
                    f"action `{stmt.action}` is routed twice in {comp.name}",
                    hint="one action has one model placement; a second block "
                         "would leave which one binds to declaration order. "
                         "Merge the arms into a single `route model on "
                         f"{stmt.action}` block",
                    code=CODE, category=CATEGORY,
                )
            if not stmt.arms:
                raise RevlError(
                    where, stmt.line,
                    f"`route model on {stmt.action}` in {comp.name} names no "
                    f"role",
                    hint="an empty route places nothing, which reads as "
                         "permissive and is not: name the roles the action may "
                         "reach, or drop the block",
                    code=CODE, category=CATEGORY,
                )
            if stmt.action not in declared_actions:
                known = ", ".join(sorted(declared_actions)) or "none"
                raise RevlError(
                    where, stmt.line,
                    f"`route model on {stmt.action}` names no action of "
                    f"{comp.name}",
                    hint=f"a route is keyed to the action it places, so a "
                         f"renamed or deleted method leaves a block that "
                         f"protects nothing while still reading as a placement. "
                         f"{comp.name} declares: {known}",
                    code=CODE, category=CATEGORY,
                )
            arms: dict[str, dict] = {}
            for arm in stmt.arms:
                if arm.origin != "*" and arm.origin not in ORIGIN_CLASSES:
                    raise RevlError(
                        where, arm.line,
                        f"unknown origin class `{arm.origin}` in `route model "
                        f"on {stmt.action}`",
                        hint=f"an arm is keyed by an origin class from the taint "
                             f"lattice (item 249), so a typo is a refusal rather "
                             f"than an arm that never matches; the vocabulary "
                             f"is: {_ORIGIN_VOCABULARY}",
                        code=CODE, category=CATEGORY,
                    )
                if arm.origin in arms:
                    raise RevlError(
                        where, arm.line,
                        f"origin `{arm.origin}` is routed twice in `route model "
                        f"on {stmt.action}` ({comp.name})",
                        hint="two arms for one origin make the placement depend "
                             "on match order; name each origin once",
                        code=CODE, category=CATEGORY,
                    )
                role = table.get(arm.role)
                if role is None:
                    known = ", ".join(sorted(table)) or "none"
                    raise RevlError(
                        where, arm.line,
                        f"`{arm.origin} -> {arm.role}` in `route model on "
                        f"{stmt.action}` ({comp.name}) names no declared model "
                        f"role",
                        hint=f"a role is a declared placement, not a host "
                             f"detail: an undeclared name has no residence, so "
                             f"the placement cannot be checked and is refused "
                             f"rather than assumed. Declare it — `model role "
                             f"{arm.role} on_device` or `model role {arm.role} "
                             f"off_device`. Declared roles: {known}",
                        code=CODE, category=CATEGORY,
                    )
                if arm.origin == "secret":
                    raise RevlError(
                        where, arm.line,
                        f"action `{stmt.action}` ({comp.name}) routes the "
                        f"`secret` origin to model role `{arm.role}`: a "
                        f"capability-bound secret never reaches a model prompt, "
                        f"on the device or off it (G-SECRET-FLOW)",
                        hint="a `secret NAME for CAP` value is a host-scope "
                             "local handed to CAP's own provider call; an LLM "
                             "prompt is a disclosure sink for it at every "
                             "residence, so there is no role this arm could name. "
                             "Drop the arm",
                        code="G-SECRET-FLOW", category=CATEGORY,
                    )
                if arm.origin in CONFIDENTIALITY_ORIGINS and role.off_device:
                    raise RevlError(
                        where, arm.line,
                        f"action `{stmt.action}` ({comp.name}) routes the "
                        f"`{arm.origin}` origin to model role `{arm.role}`, "
                        f"which is declared `{role.residence}` on line "
                        f"{role.line}: a {arm.origin} input may not leave the "
                        f"device (G-MODEL-PLACE)",
                        hint=f"route `{arm.origin}` to a role declared "
                             f"`on_device`, or declare `{arm.role}` "
                             f"`on_device` if this placement really does stay "
                             f"on the device. Dropping the arm also works: `*` "
                             f"never covers a confidentiality origin, so an "
                             f"unnamed `{arm.origin}` is not placed at all "
                             f"(docs/design/531-model-placement.md)",
                        code=CODE, category=CATEGORY,
                    )
                arms[arm.origin] = {"role": role.name,
                                    "residence": role.residence}
            actions[stmt.action] = arms
        if actions:
            placed[comp.name] = actions
    return placed


def model_crossing_of(capabilities) -> str | None:
    """The `model.*` capability token a crossing declares, or None when the
    crossing is not a model call.

    The exact shape of `retention.persistence_sink_of` on this module's scope:
    only the FIRST capability with a matching head is reported, because a
    crossing declares one resource scope.
    """
    for cap in capabilities or ():
        if str(cap).split(".", 1)[0] == MODEL_SCOPE:
            return str(cap)
    return None


@dataclass(frozen=True)
class Verdict:
    """What an action's route table says about one value's origin (item 514).

    `ok` is the only admitting value and it is reached by exactly one path:
    the block names an arm for this origin and that arm's role is declared
    `on_device`. Everything else is a refusal, including every case where the
    placement could not be determined.

    `reason` is one of:

    * `ok`          - an arm names the origin and its role stays on the device;
    * `unrouted`    - the component declares a `route model` block for some
                      other action and none for this one, so this action's
                      model calls have no declared placement;
    * `unplaced`    - the block names no arm for this origin. `*` does not
                      cover a confidentiality origin (section 2.1 of the design
                      note), so this is the verdict `* -> cloud` gets;
    * `off_device`  - an arm names the origin and places it off the device.
                      `check()` refuses writing that arm, so this is the
                      belt-and-braces path: if a future slice ever admits such
                      an arm, the VALUE is still refused here.

    `role` and `residence` name the placement the value would have reached, so
    the diagnostic can name both the origin and the role the exit test asks
    for. Under `unplaced` they carry the `*` arm's role when the block has one,
    which is the role the author believes the value is going to.
    """
    ok: bool
    reason: str
    role: str | None = None
    residence: str | None = None


_OK = Verdict(True, "ok")


def admits(arms: dict | None, origin: str, routed_component: bool) -> Verdict:
    """Whether a value carrying `origin` may cross into this action's model
    call (roadmap item 514, design note section 2.1 and slice S2).

    `arms` is one action's entry from `check()`'s return value - the
    `{origin: {role, residence}}` table built FOR this - or None when the
    action carries no `route model` block. `routed_component` says whether the
    component declares a block for any action at all.

    The ceiling is a CONFIDENTIALITY ceiling: an origin that is not a
    confidentiality origin is not this rule's business (which roles an action's
    other origins reach is the crossing side, slice 4), so it admits. Every
    other path either names an on-device placement or refuses.
    """
    if origin not in CONFIDENTIALITY_ORIGINS:
        return _OK
    if arms is None:
        # An unrouted action in an UNROUTED component is the state of the world
        # before item 512: the program declared no placement anywhere, so there
        # is no ceiling to be above and item 256's own disclosure fence is the
        # whole rule. An unrouted action in a component that DID route is the
        # fail-open shape this item exists to remove - the author declared that
        # placement is a property of this component, and this action's model
        # calls escaped it.
        return _OK if not routed_component else Verdict(False, "unrouted")
    placement = arms.get(origin)
    if placement is None:
        star = arms.get("*") or {}
        return Verdict(False, "unplaced", star.get("role"),
                       star.get("residence"))
    if placement.get("residence") == "off_device":
        return Verdict(False, "off_device", placement.get("role"),
                       placement.get("residence"))
    return Verdict(True, "ok", placement.get("role"),
                   placement.get("residence"))


def ceiling_refusal(verdict: Verdict, origin: str, action: str,
                    component: str, crossing: str, capability: str,
                    index: int, chain: str) -> tuple[str, str]:
    """The message and hint for a refused value-level placement.

    Written here rather than in `revl.taint` so the sentence a ceiling refusal
    says sits beside the rule that decides it, and so `check()`'s eleven
    declaration refusals and this one stay recognisably the same diagnostic.
    """
    where = f"argument {index + 1} of `{crossing}` (`{capability}`)"
    if verdict.reason == "unrouted":
        message = (
            f"a `{origin}` value reaches the model crossing {where} in action "
            f"`{action}` ({component}), which declares no `route model` "
            f"placement: {component} routes some of its actions and not this "
            f"one, so where this value's prompt goes is undeclared "
            f"({CODE})")
        hint = (
            f"an undeclared placement is refused rather than assumed, the way "
            f"an undeclared role is: add `route model on {action} {{ "
            f"{origin} -> <an on_device role> }}` to {component}, or take the "
            f"`{origin}` value out of this crossing. The path is {chain}.")
    elif verdict.reason == "unplaced" and verdict.role:
        message = (
            f"a `{origin}` value reaches the model crossing {where} in action "
            f"`{action}` ({component}), whose `route model` block places it "
            f"through no arm: the catch-all `*` routes to model role "
            f"`{verdict.role}` (declared `{verdict.residence}`) and `*` never "
            f"covers a confidentiality origin ({CODE})")
        hint = (
            f"`*` stands for the origins no other arm names AND that are not "
            f"confidentiality origins, so `* -> {verdict.role}` is not the "
            f"sentence that places this value. Name it: `{origin} -> <an "
            f"on_device role>` in `route model on {action}`, or keep the "
            f"`{origin}` value out of the crossing. The path is {chain}.")
    elif verdict.reason == "unplaced":
        message = (
            f"a `{origin}` value reaches the model crossing {where} in action "
            f"`{action}` ({component}), whose `route model` block names no arm "
            f"for the `{origin}` origin ({CODE})")
        hint = (
            f"an origin no arm names is not placed at all, which is a refusal "
            f"and not a default: add `{origin} -> <an on_device role>` to "
            f"`route model on {action}`, or keep the `{origin}` value out of "
            f"the crossing. The path is {chain}.")
    else:
        message = (
            f"a `{origin}` value reaches the model crossing {where} in action "
            f"`{action}` ({component}), which routes the `{origin}` origin to "
            f"model role `{verdict.role}`, declared `{verdict.residence}`: a "
            f"{origin} input may not leave the device ({CODE})")
        hint = (
            f"route `{origin}` to a role declared `on_device`, or keep the "
            f"`{origin}` value out of this crossing. The path is {chain}.")
    return message, hint
