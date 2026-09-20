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

WHAT THIS MODULE DOES NOT DO
----------------------------
It checks the DECLARATION. It does not yet look at the values that flow into
the action - that is the origin ceiling of item 514, which is what makes the
`*` rule above bite on a real value rather than on a written arm. It does not
select a model at run time either: item 512 is a permission, not a scheduler
(item 515 owns the scheduling inside the boundary this draws). See the design
doc's non-goals.
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

_ORIGIN_VOCABULARY = ", ".join(sorted(ORIGIN_CLASSES) + ["*"])
_RESIDENCE_VOCABULARY = ", ".join(RESIDENCES)


# The reach of a role that declares none. UNDECLARED IS NOT EMPTY: a model is
# an authority surrogate, so the question "how far does this role reach" always
# has an answer, and the answer a declaration does not give is the unnameable
# `*` - the one token no `requires` key can name and that `cap_order.covers`
# therefore covers with nothing (item 519).
#
# This is the whole failure direction of the item. Reading an undeclared reach
# as EMPTY would make an unknown model inert in the product, which is the
# fail-open shape: the component that consults a model it has said nothing
# about is exactly the one whose effective ceiling is unknown, and an unknown
# ceiling is refused here rather than assumed to be zero. It is the same choice
# `_spawn_emission_surface` already makes for a service method that declares
# `emission` with no capability list, where `None` becomes `*` and not `set()`.
UNDECLARED_REACH = ("*",)


@dataclass(frozen=True)
class Role:
    """A validated `model role` declaration.

    `reach` is the `reaches [...]` clause of item 519, as declared: a tuple of
    capability tokens, `("*",)` for `reaches [*]`, `()` for `reaches []`, and
    `None` when the clause was omitted. Read it through `reach_tokens` rather
    than directly, which is what resolves `None` to `UNDECLARED_REACH`.
    """
    name: str
    residence: str
    line: int
    reach: tuple | None = None

    @property
    def off_device(self) -> bool:
        return self.residence == "off_device"

    @property
    def reach_declared(self) -> bool:
        """Whether the role wrote a `reaches [...]` clause at all.

        The diagnostic reads this so it can say `declares no reach` instead of
        claiming the author wrote `reaches [*]`, which they did not."""
        return self.reach is not None

    @property
    def reach_tokens(self) -> tuple:
        """The capability tokens a call to this role can reach.

        `UNDECLARED_REACH` when the clause is absent. Every consumer goes
        through here, so the fail-closed reading of silence is decided once."""
        return UNDECLARED_REACH if self.reach is None else self.reach


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
        table[decl.name] = Role(decl.name, decl.residence, decl.line,
                                getattr(decl, "reach", None))
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
                # `line` is additive (item 519): the attenuation refusal
                # points at the ARM that routes through the role, not at the
                # component head. A consumer reading `role`/`residence` is
                # unaffected.
                arms[arm.origin] = {"role": role.name,
                                    "residence": role.residence,
                                    "line": arm.line}
            actions[stmt.action] = arms
        if actions:
            placed[comp.name] = actions
    return placed
