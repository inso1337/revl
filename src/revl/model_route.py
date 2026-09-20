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

WHAT ITEM 515 ADDS HERE
-----------------------
`docs/design/539-model-portfolio.md` is the portfolio's design. Two things of
its slice 1 live in this file, because both are rules over the same
declaration:

    model role fast  on_device device gpu memory 6144 quant q4_k_m
    model role small on_device device cpu memory  512 quant int8

    route model on classify { confidential -> fast | small }

An optional `device` clause states the resource a placement DEMANDS - a class
from a closed vocabulary, a resident-memory floor, an opaque quantisation tag
(`revl.model_profile`). An arm may name an ORDERED CANDIDATE SET, which is the
scheduling surface, and the set is closed: `*` on the right of an arrow is
refused by name, so a scheduler cannot pick a role no arm names. Within a set,
residence is uniform, so no fallback carries work across the line item 514's
ceiling drew, and every candidate is profiled, so no candidate is the
unrankable one a fallback lands on.

What revl still does NOT know is whether the declared device exists. The
profile is a claim about hardware, checked against arms and ceilings and
against nothing else.

WHAT ITEM 519 ADDS HERE
-----------------------
`docs/design/541-model-in-attenuation.md` is the design. A second optional
clause states the capability tokens a call to the role can itself reach, which
is what puts the role in the capability attenuation product:

    model role fast on_device device gpu memory 6144 quant q4_k_m
                              reaches [net, fs.read]

Omitting it leaves the reach UNDECLARED, which is `UNDECLARED_REACH` and not
an empty set. The fold itself is `revl.lower`'s, beside the spawn-edge fold it
reuses; what lives here is the reading of silence.

THE TWO CLAUSES AFTER A RESIDENCE
---------------------------------
`device ...` is written first and `reaches [...]` second, each independently
omittable, and the parser refuses the other order rather than accepting two
spellings of one declaration. The order follows the reading: `device` refines
the residence in front of it, since both answer where the call runs, and the
bracketed capability list reads last, where `requires` and `emission` have
already taught a reader to expect one.

They also fail in opposite directions, which is why neither defaults to the
other. An omitted `device` clause is a role making no resource claim, refused
only where a claim is needed (an ordered candidate set has to be rankable). An
omitted `reaches` clause is a reach nobody wrote down, refused wherever it is
read.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import RevlError
from .model_profile import DEVICE_CLASSES, DeviceProfile
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

# The origins the VALUE-level ceiling judges (item 514). `secret` is
# deliberately NOT one of them, and the omission is a decision rather than an
# oversight. A bound provider key is already refused at every crossing kind by
# item 256's own rule, so a ceiling arm for it would only ever restate a
# shipped refusal under the wrong code - and the ONE crossing item 256 admits,
# the section-4b re-entry into the same bound capability's own extern body, is
# the provider making its own call. Refusing that under G-MODEL-PLACE would
# contradict a landed guarantee to no purpose. `check()` keeps refusing an ARM
# that names `secret`, citing G-SECRET-FLOW, which is the declaration half and
# is unaffected by this.
CEILING_ORIGINS = ("confidential",)

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
_DEVICE_VOCABULARY = ", ".join(DEVICE_CLASSES)


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

    A role carries two independently optional clauses, and both default to
    `None`, so `Role(name, residence, line)` is still the whole declaration
    for a role written against item 512 alone.

    `profile` is the item-515 device clause, or `None` for a role declared
    without one. It is the resource the placement DEMANDS; what a member is
    actually loaded onto is the provider's published profile and reaches revl
    only inside an opaque `placement_digest` (`revl.model_profile`).

    `reach` is the `reaches [...]` clause of item 519, as declared: a tuple of
    capability tokens, `("*",)` for `reaches [*]`, `()` for `reaches []`, and
    `None` when the clause was omitted. Read it through `reach_tokens` rather
    than directly, which is what resolves `None` to `UNDECLARED_REACH`.

    The two mean different things when omitted, and deliberately so. An
    omitted `device` clause is a role that makes no resource claim, which is
    refused only where a claim is needed (an ordered candidate set); an
    omitted `reaches` clause is a role whose reach is UNDECLARED, which is the
    unnameable `*` and is refused wherever it is read.
    """
    name: str
    residence: str
    line: int
    profile: DeviceProfile | None = None
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
                                _profile(decl, filename),
                                getattr(decl, "reach", None))
    return table


def _profile(decl, filename: str) -> DeviceProfile | None:
    """Validate the optional item-515 `device` clause on one role.

    Two refusals, both closed, and one deliberate non-refusal.

    The device class is checked against `DEVICE_CLASSES`, so `device gpu0` is
    a refusal and not a placement that matches any device. The memory floor
    must be positive, because `memory 0` reads as "no requirement" while
    looking like a declared one, and a floor nothing can fail to meet is the
    fail-open shape.

    The quantisation tag is NOT checked against a vocabulary. Quantisation
    tags are an open world the compiler must not learn (item 538: a
    quantisation is "a property of a host and nothing under the compiler
    should learn what one is"), so the tag is carried, compared for equality,
    and fed to the provider's digest. A closed list here would be wrong within
    a release.
    """
    clause = getattr(decl, "profile", None)
    if clause is None:
        return None
    if clause.device not in DEVICE_CLASSES:
        raise RevlError(
            filename, clause.line,
            f"unknown device class `{clause.device}` on model role "
            f"`{decl.name}`",
            hint=f"a device class is checked at compile time so a typo is a "
                 f"refusal rather than a placement that matches any device; "
                 f"the vocabulary is: {_DEVICE_VOCABULARY} "
                 f"(docs/design/539-model-portfolio.md)",
            code=CODE, category=CATEGORY,
        )
    if clause.memory_mib <= 0:
        raise RevlError(
            filename, clause.line,
            f"model role `{decl.name}` declares `memory {clause.memory_mib}`, "
            f"which no placement can fail to meet",
            hint="a memory floor is the resident MiB the member needs, so a "
                 "floor of zero or less is a requirement that reads as "
                 "declared and rules nothing out. State the real floor, or "
                 "drop the `device` clause if the placement has no resource "
                 "requirement to declare",
            code=CODE, category=CATEGORY,
        )
    return DeviceProfile(clause.device, clause.memory_mib, clause.quant,
                         clause.line)


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


def _check_candidate_set(where, comp, stmt, arm, resolved) -> None:
    """The two rules that exist only because an arm may name more than one
    role (roadmap item 515).

    A one-candidate arm is a placement. A multi-candidate arm is a placement
    plus a scheduling decision, and these are the two ways that decision can
    undo something already decided.

    **Residence is uniform across the set.** Item 514's origin ceiling refuses
    a value whose origin reaches an `off_device` role, and it reads one
    residence per origin. A set whose head is `on_device` and whose fallback
    is `off_device` would let a scheduler move a workload across the line the
    ceiling already drew, at a moment no admission check is watching. This is
    the repo's recurring shape in its scheduling form: state keyed to a
    placement that outlived the placement meant to bound it.

    **Every candidate declares a device profile.** The scheduler ranks the set
    by the profile; a candidate with no `device` clause is unrankable, and an
    unrankable candidate is the one a fallback lands on when the profiled ones
    are unavailable, which is the fail-open answer to "the declared device is
    not there". Fail-closed means the whole set is comparable or the program
    does not compile. A single-candidate arm needs no profile, which is why
    nothing written against item 512 stops compiling.
    """
    head = resolved[0]
    for role in resolved[1:]:
        if role.residence != head.residence:
            raise RevlError(
                where, arm.line,
                f"the candidates for `{arm.origin}` in `route model on "
                f"{stmt.action}` ({comp.name}) do not agree on residence: "
                f"`{head.name}` is `{head.residence}` (line {head.line}) and "
                f"`{role.name}` is `{role.residence}` (line {role.line})",
                hint="a candidate set is what a scheduler may fall back to, so "
                     "a set spanning both residences lets a fallback carry the "
                     "work off the device after admission decided it stays. "
                     "Split the arm's roles into one set per residence, and "
                     "route the origin to the one it is allowed to reach",
                code=CODE, category=CATEGORY,
            )
    unprofiled = [r.name for r in resolved if r.profile is None]
    if unprofiled:
        raise RevlError(
            where, arm.line,
            f"candidate(s) {', '.join(unprofiled)} for `{arm.origin}` in "
            f"`route model on {stmt.action}` ({comp.name}) declare no device "
            f"profile, so the candidate set cannot be ordered",
            hint="an arm naming one role is a placement; an arm naming several "
                 "is a placement plus a scheduling decision, and the declared "
                 "device profile is what that decision is made on. A candidate "
                 "with no `device` clause is not comparable to one that has "
                 "it, and an incomparable candidate is the one a fallback "
                 "lands on when the profiled ones are unavailable. Give every "
                 "candidate a `device <class> memory <MiB> quant <tag>` "
                 "clause, or route the origin to a single role "
                 "(docs/design/539-model-portfolio.md)",
            code=CODE, category=CATEGORY,
        )


def check(program, filename: str | None = None) -> dict[str, dict[str, dict]]:
    """Check every `route model` block in the program against its role table.

    Returns `{component: {action: {origin: {role, residence, candidates,
    line}}}}` for the blocks that passed. `role` and `residence` are the arm's
    HEAD and are what item 514's flow walk reads; `candidates` is the item-515
    ordered set, a one-tuple for an arm written with a single role; `line` is
    the arm's line, which item 519's attenuation refusal points at. A program
    with no block gets `{}`.
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
                # item 515: an arm is an ORDERED CANDIDATE SET, and every rule
                # below runs on every member of it. An arm whose head stays on
                # the device and whose fallback does not is precisely the
                # fail-open shape a scheduler introduces, so the head gets no
                # special treatment beyond being first.
                candidates = list(getattr(arm, "candidates", (arm.role,)))
                seen: list[str] = []
                resolved: list[Role] = []
                for name in candidates:
                    if name == "*":
                        raise RevlError(
                            where, arm.line,
                            f"`{arm.origin} -> *` in `route model on "
                            f"{stmt.action}` ({comp.name}) places the origin on "
                            f"any available role",
                            hint="a route names the roles an action may reach, "
                                 "and `*` on the right names none of them: a "
                                 "placement that may pick a role no arm names "
                                 "is the shape that puts a workload on whatever "
                                 "hardware happened to be free. Name the "
                                 "candidates in the order the scheduler should "
                                 "try them — `confidential -> fast | small` "
                                 "(docs/design/539-model-portfolio.md)",
                            code=CODE, category=CATEGORY,
                        )
                    if name in seen:
                        raise RevlError(
                            where, arm.line,
                            f"model role `{name}` appears twice among the "
                            f"candidates for `{arm.origin}` in `route model on "
                            f"{stmt.action}` ({comp.name})",
                            hint="the candidate set is ordered, so a repeated "
                                 "name has two positions and no defined "
                                 "preference; name each candidate once",
                            code=CODE, category=CATEGORY,
                        )
                    seen.append(name)
                    role = table.get(name)
                    if role is None:
                        known = ", ".join(sorted(table)) or "none"
                        raise RevlError(
                            where, arm.line,
                            f"`{arm.origin} -> {name}` in `route model on "
                            f"{stmt.action}` ({comp.name}) names no declared "
                            f"model role",
                            hint=f"a role is a declared placement, not a host "
                                 f"detail: an undeclared name has no residence, "
                                 f"so the placement cannot be checked and is "
                                 f"refused rather than assumed. Declare it — "
                                 f"`model role {name} on_device` or `model role "
                                 f"{name} off_device`. Declared roles: {known}",
                            code=CODE, category=CATEGORY,
                        )
                    if arm.origin == "secret":
                        raise RevlError(
                            where, arm.line,
                            f"action `{stmt.action}` ({comp.name}) routes the "
                            f"`secret` origin to model role `{name}`: a "
                            f"capability-bound secret never reaches a model "
                            f"prompt, on the device or off it (G-SECRET-FLOW)",
                            hint="a `secret NAME for CAP` value is a host-scope "
                                 "local handed to CAP's own provider call; an "
                                 "LLM prompt is a disclosure sink for it at "
                                 "every residence, so there is no role this arm "
                                 "could name. Drop the arm",
                            code="G-SECRET-FLOW", category=CATEGORY,
                        )
                    if arm.origin in CONFIDENTIALITY_ORIGINS and role.off_device:
                        raise RevlError(
                            where, arm.line,
                            f"action `{stmt.action}` ({comp.name}) routes the "
                            f"`{arm.origin}` origin to model role `{name}`, "
                            f"which is declared `{role.residence}` on line "
                            f"{role.line}: a {arm.origin} input may not leave "
                            f"the device (G-MODEL-PLACE)",
                            hint=f"route `{arm.origin}` to a role declared "
                                 f"`on_device`, or declare `{name}` "
                                 f"`on_device` if this placement really does "
                                 f"stay on the device. Dropping the arm also "
                                 f"works: `*` never covers a confidentiality "
                                 f"origin, so an unnamed `{arm.origin}` is not "
                                 f"placed at all "
                                 f"(docs/design/531-model-placement.md)",
                            code=CODE, category=CATEGORY,
                        )
                    resolved.append(role)
                head = resolved[0]
                if len(resolved) > 1:
                    _check_candidate_set(where, comp, stmt, arm, resolved)
                # `line` is additive (item 519): the attenuation refusal
                # points at the ARM that routes through the role, not at the
                # component head. A consumer reading `role`/`residence`/
                # `candidates` is unaffected.
                arms[arm.origin] = {
                    "role": head.name,
                    "residence": head.residence,
                    "candidates": tuple(r.name for r in resolved),
                    "line": arm.line,
                }
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

    The ceiling is a CONFIDENTIALITY ceiling: an origin outside
    `CEILING_ORIGINS` is not this rule's business - which roles an action's
    other origins reach is the crossing side, slice 4, and the bound-key
    `secret` origin is item 256's - so it admits. Every other path either names
    an on-device placement or refuses.
    """
    if origin not in CEILING_ORIGINS:
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
