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

AN ARM MAY ALSO NAME A COUNCIL (item 516 slice 2)
-------------------------------------------------
The right of an arm is a `model role` or a `model council`
(`docs/design/543-model-council.md`), and the two share one namespace, which
`revl.model_council` enforces by refusing a council that takes a declared
role's name. A council's own placement is `Council.residence`, the MOST
permissive of its members', so an arm routing a confidentiality origin to a
council with one `off_device` member is refused - and the refusal names THE
MEMBER, because the members are placed separately and that is the construct.
The council table is threaded in from `revl.model_council.check()` rather than
computed here: this slice reads two validated tables and re-derives neither
(design note 543 section 14).

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

It also writes NO IR, and that is a decision rather than an omission (issue
#1311). The linked composition carries no route, no arm, no role and no
residence, at any depth, and `tests/test_1311_model_routes_not_in_ir.py` scans
a compiled document for the whole vocabulary to keep it that way. A consumer
holding only a linked composition can derive the component's declared ACTIONS
from its provide block and nothing else; the route table travels BY VALUE,
from `check()` to the consumer that needs it, which is how item 517's decision
object and item 518's `ShadowPlan.route_table` already read it. Section 4.1 of
`docs/design/531-model-placement.md` is the contract, including what would
justify an IR section later and why neither has landed.

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
unrankable one a fallback lands on. A `model council` is a placement and not a
candidate: it may stand alone on the right of an arrow, and it may not appear
in a set of two or more, because a council already aggregates its members by a
declared rule and a scheduler picking among candidates is a second, undeclared
aggregation over the same call.

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

_COUNCIL_DESIGN = "docs/design/543-model-council.md"


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


def _council_ceiling_refusal(action: str, component: str, origin: str,
                             name: str, off: tuple) -> tuple[str, str]:
    """The message and hint for an arm that routes a confidentiality origin to
    a council one of whose members is placed off the device (item 516 slice 2).

    It names the MEMBER, and that is the whole reason this refusal exists
    beside the role one rather than reusing it. A council's members are placed
    separately (design note 543 section 6), so "the council is off_device" is
    a derived fact and the actionable one is which member derived it: an author
    told "`Release` may not read a confidential input" has to re-read three
    placements to find out why, and an author told "its `proposer` runs on
    `vast`, declared `off_device`" has the arm to edit.

    The first off-device member in DECLARATION ORDER is the one the message
    names; the hint names all of them, because moving one member of two off
    the device leaves the refusal standing and an author who fixed the named
    one and recompiled would otherwise learn that one refusal at a time.
    """
    first = off[0]
    message = (
        f"action `{action}` ({component}) routes the `{origin}` origin to "
        f"model council `{name}`, whose member `{first['function']}` runs on "
        f"model role `{first['role']}`, declared `{first['residence']}` on "
        f"line {first['line']}: a {origin} input may not leave the device "
        f"({CODE})")
    named = ", ".join(f"`{m['function']}` on `{m['role']}`" for m in off)
    hint = (
        f"a council is asked ONE question with ONE input, so giving a "
        f"`{origin}` value to `{name}` gives it to every member, and the "
        f"council's residence is the most permissive of its members' rather "
        f"than the least. Place the member on a role declared `on_device`, or "
        f"route `{origin}` somewhere that stays on the device. Off the device: "
        f"{named} ({_COUNCIL_DESIGN})")
    return message, hint


def _council_placement(council, table: dict) -> dict:
    """The `{origin: placement}` entry a council-naming arm records.

    `role` is the COUNCIL's name and `residence` is `Council.residence`, the
    most permissive of its members' (design note 543 section 6), so every
    reader that already understands a role placement - `admits`, `reach_of`,
    item 518's `ShadowPlan.route_table` - reads a council placement without
    knowing the word. `council` is what tells the ones that care which noun to
    say, and `members` carries the per-member placement the join was computed
    from, because the whole point of a council is that its members are placed
    SEPARATELY and a refusal has to be able to name the one that made the
    difference (item 516, `docs/design/543-model-council.md` section 6).

    Read off `model_council.check()`'s validated table and `roles()`'s. Nothing
    here re-derives a residence, a member or a role: a member's `line` is the
    line its `model role` was DECLARED on, which is the line every other
    placement refusal in this file cites, and not the line the member was
    written on - the diagnostic already points at the arm.
    """
    return {
        "role": council.name,
        "residence": council.residence,
        "council": council.name,
        # item 516 slice 4: `scoped` says which of the two readings of
        # `residence` applies. False is slice 1's - one question, one input,
        # every member - and `residence` is the join over all of them. True
        # means the council withholds by declaration, and the join that
        # matters is over the members whose `inputs` name the origin.
        "scoped": council.scoped,
        "members": tuple(
            {"function": m.function, "role": m.role,
             "residence": m.residence, "line": table[m.role].line,
             "inputs": m.inputs}
            for m in council.members),
        "member_roles": tuple(m.role for m in council.members),
    }


def receiving_members(placement, origin: str | None = None) -> tuple:
    """The members of a council placement that are GIVEN `origin`.

    Every member of an unscoped council, which is slice 1's reading and the
    one every program that predates item 516 slice 4 gets. For a scoped
    council it is the members whose `reads` clause names the origin, and that
    tuple may be empty: a council that is handed an origin it gives to no
    member places nothing, which `check` refuses by name.
    """
    members = tuple((placement or {}).get("members") or ())
    if origin is None or not (placement or {}).get("scoped"):
        return members
    return tuple(m for m in members if origin in (m.get("inputs") or ()))


def placement_ceiling(placement, origin: str) -> str | None:
    """The residence item 514's rule compares for `origin` (item 516 slice 4).

    A role placement's own residence, and for a council the join over the
    members that RECEIVE the origin. The two differ only for a council that
    withholds by declaration: its `residence` stays the join over EVERY
    member, because that is what a reader who does not know about per-member
    inputs must get, and this is what a reader judging one origin must use.
    """
    if not (placement or {}).get("scoped"):
        return (placement or {}).get("residence")
    return "off_device" if off_device_members(placement, origin) else "on_device"


def off_device_members(placement, origin: str | None = None) -> tuple:
    """The members of a council placement that are placed off the device.

    Empty for a role placement and for a council whose members all stay on the
    device, which is the pair of cases the ceiling admits. In DECLARATION
    ORDER, so the member a refusal names is the first one an author reading
    the council top to bottom would reach.

    With `origin`, only the members that RECEIVE it are judged (item 516 slice
    4). Without it the whole declared set is, which is the conservative join
    and stays the answer for every council that declares no per-member input.
    """
    return tuple(m for m in receiving_members(placement, origin)
                 if m.get("residence") == "off_device")


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


def check(program, filename: str | None = None,
          councils: dict | None = None) -> dict[str, dict[str, dict]]:
    """Check every `route model` block in the program against its role table.

    Returns `{component: {action: {origin: {role, residence, candidates,
    line}}}}` for the blocks that passed. `role` and `residence` are the arm's
    HEAD and are what item 514's flow walk reads; `candidates` is the item-515
    ordered set, a one-tuple for an arm written with a single role; `line` is
    the arm's line, which item 519's attenuation refusal points at. A program
    with no block gets `{}`.

    `councils` is `revl.model_council.check()`'s validated table, which an arm
    may name where it names a role (item 516 slice 2). It is threaded in
    rather than computed here for the reason design note 543 section 14 gives:
    this slice reads both already-validated tables and re-derives neither. The
    default `None` is the same table as `{}` and leaves every program that
    declares no council byte-identical through here, which is every program on
    the tree that predates item 516.
    """
    from .parser import ModelRouteStmt  # noqa: PLC0415 - import cycle

    filename = filename or program.filename
    table = roles(program, filename)
    councils = councils or {}
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
                # item 515: an arm is an ORDERED CANDIDATE SET, and every
                # rule below runs on every member of it. An arm whose head
                # stays on the device and whose fallback does not is precisely
                # the fail-open shape a scheduler introduces, so the head gets
                # no special treatment beyond being first.
                candidates = list(getattr(arm, "candidates", None)
                                  or (arm.role,))
                seen: list[str] = []
                resolved: list[Role] = []
                council_placement = None
                for name in candidates:
                    if name == "*":
                        raise RevlError(
                            where, arm.line,
                            f"`{arm.origin} -> *` in `route model on "
                            f"{stmt.action}` ({comp.name}) places the origin "
                            f"on any available role",
                            hint="a route names the roles an action may reach, "
                                 "and `*` on the right names none of them: a "
                                 "placement that may pick a role no arm names "
                                 "is the shape that puts a workload on "
                                 "whatever hardware happened to be free. Name "
                                 "the candidates in the order the scheduler "
                                 "should try them — `confidential -> fast | "
                                 "small` (docs/design/539-model-portfolio.md)",
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
                    council = councils.get(name) if role is None else None
                    if role is None and council is None:
                        known = ", ".join(sorted(table)) or "none"
                        raise RevlError(
                            where, arm.line,
                            f"`{arm.origin} -> {name}` in `route model on "
                            f"{stmt.action}` ({comp.name}) names no declared "
                            f"model role",
                            hint=f"a role is a declared placement, not a host "
                                 f"detail: an undeclared name has no "
                                 f"residence, so the placement cannot be "
                                 f"checked and is refused rather than "
                                 f"assumed. Declare it — `model role {name} "
                                 f"on_device` or `model role {name} "
                                 f"off_device`. Declared roles: {known}",
                            code=CODE, category=CATEGORY,
                        )
                    if arm.origin == "secret":
                        noun = "council" if council is not None else "role"
                        raise RevlError(
                            where, arm.line,
                            f"action `{stmt.action}` ({comp.name}) routes the "
                            f"`secret` origin to model {noun} `{name}`: a "
                            f"capability-bound secret never reaches a model "
                            f"prompt, on the device or off it (G-SECRET-FLOW)",
                            hint="a `secret NAME for CAP` value is a host-scope "
                                 "local handed to CAP's own provider call; an "
                                 "LLM prompt is a disclosure sink for it at "
                                 "every residence, so there is no role this "
                                 "arm could name. Drop the arm",
                            code="G-SECRET-FLOW", category=CATEGORY,
                        )
                    if council is not None:
                        # A COUNCIL IS A PLACEMENT, NOT A CANDIDATE (items 515
                        # and 516). A council already aggregates its members
                        # by a declared, total rule that names no value when
                        # they disagree (G-COUNCIL-SPLIT). A scheduler
                        # choosing among candidates is a second aggregation
                        # over the same call, written nowhere and decided at
                        # run time, so the two constructs do not nest.
                        if len(candidates) > 1:
                            raise RevlError(
                                where, arm.line,
                                f"model council `{name}` is one of "
                                f"{len(candidates)} candidates for "
                                f"`{arm.origin}` in `route model on "
                                f"{stmt.action}` ({comp.name})",
                                hint="a council is a placement whose "
                                     "aggregation is declared and total; a "
                                     "candidate set is a scheduling decision "
                                     "made at run time. Nesting one in the "
                                     "other leaves two aggregations over one "
                                     "call and no written rule for which "
                                     "applies. Route the origin to the "
                                     "council alone, or name only roles in "
                                     "the candidate set "
                                     "(docs/design/539-model-portfolio.md)",
                                code=CODE, category=CATEGORY,
                            )
                        council_placement = _council_placement(council, table)
                        # The council's own ceiling, which is the one number
                        # this slice reads from `model_council.check()` (design
                        # note 543 section 6). It is the MOST permissive of its
                        # members', because giving an input to a council gives
                        # it to every member - and the refusal names the member
                        # that made the join `off_device`, not the council,
                        # because the members are placed separately and that IS
                        # the construct.
                        off = off_device_members(council_placement, arm.origin)
                        # A SCOPED council (item 516 slice 4) never reaches the
                        # refusal below for `confidential`, and that is the
                        # point rather than an accident: `model_council`
                        # refuses a member declared `reads confidential` on an
                        # `off_device` role, so every member that RECEIVES the
                        # origin is already on the device and `off` is empty.
                        # The members that stay off the device are the ones the
                        # declaration withholds it from.
                        if arm.origin in CONFIDENTIALITY_ORIGINS and off:
                            message, hint = _council_ceiling_refusal(
                                stmt.action, comp.name, arm.origin, name, off)
                            raise RevlError(
                                where, arm.line, message, hint=hint,
                                code=CODE, category=CATEGORY,
                            )
                        break
                    if (arm.origin in CONFIDENTIALITY_ORIGINS
                            and role.off_device):
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
                if council_placement is not None:
                    # `candidates` and `line` are additive and uniform across
                    # every placement shape (item 519). For a council they are
                    # the MEMBER roles, because those are the roles the arm
                    # actually reaches: a council asks every member, so the
                    # attenuation fold has to read all of them and not the
                    # council's own name, which is not a role.
                    council_placement["candidates"] = tuple(
                        council_placement["member_roles"])
                    council_placement["line"] = arm.line
                    arms[arm.origin] = council_placement
                    continue
                head = resolved[0]
                if len(resolved) > 1:
                    _check_candidate_set(where, comp, stmt, arm, resolved)
                # `candidates` and `line` are additive (items 515 and 519): the
                # attenuation refusal points at the ARM that routes through the
                # role, not at the component head. A consumer reading
                # `role`/`residence` is unaffected.
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

    `council` is the council's name when the arm named a council rather than a
    role (item 516 slice 2), and `member` / `member_role` are the member whose
    own placement made the council's join `off_device`. They are what lets the
    sentence name the member, which is what makes "the members are placed
    separately" a fact about a VALUE rather than about a declaration. All
    three are None for a role placement, and a reader that ignores them gets
    the role-shaped verdict this type has always had.
    """
    ok: bool
    reason: str
    role: str | None = None
    residence: str | None = None
    council: str | None = None
    member: str | None = None
    member_role: str | None = None


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
                       star.get("residence"), star.get("council"))
    # The ceiling this ORIGIN is judged against. For a role, and for a council
    # asked one question with one input, it is the placement's own residence.
    # For a council that withholds by declaration (item 516 slice 4) it is the
    # join over the members that RECEIVE the origin - which is the whole point
    # of the slice, and the reason it cannot be read off `residence`: a council
    # with a cloud proposer and a local adversary is `off_device` as a council
    # and `on_device` for the origin only the adversary is given.
    residence = placement_ceiling(placement, origin)
    if residence == "off_device":
        # A council placement carries its members, so the refusal can name the
        # one whose own residence made the join `off_device` (item 516 slice
        # 2). `check()` refuses writing this arm, so this is the same
        # belt-and-braces path the role case is: if a later slice ever admits
        # such an arm, the VALUE is still refused, and still by member.
        off = off_device_members(placement, origin)
        first = off[0] if off else {}
        return Verdict(False, "off_device", placement.get("role"),
                       residence, placement.get("council"),
                       first.get("function"), first.get("role"))
    return Verdict(True, "ok", placement.get("role"),
                   residence, placement.get("council"))


def ceiling_refusal(verdict: Verdict, origin: str, action: str,
                    component: str, crossing: str, capability: str,
                    index: int, chain: str) -> tuple[str, str]:
    """The message and hint for a refused value-level placement.

    Written here rather than in `revl.taint` so the sentence a ceiling refusal
    says sits beside the rule that decides it, and so `check()`'s eleven
    declaration refusals and this one stay recognisably the same diagnostic.
    """
    where = f"argument {index + 1} of `{crossing}` (`{capability}`)"
    # The noun the sentence says about the thing the arm named. An arm may name
    # a role or a council (item 516 slice 2) and calling a council a role would
    # send the author to a `model role` declaration that does not exist.
    noun = "model council" if verdict.council else "model role"
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
            f"through no arm: the catch-all `*` routes to {noun} "
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
    elif verdict.council:
        message = (
            f"a `{origin}` value reaches the model crossing {where} in action "
            f"`{action}` ({component}), which routes the `{origin}` origin to "
            f"model council `{verdict.council}`, whose member "
            f"`{verdict.member}` runs on model role `{verdict.member_role}`: "
            f"a {origin} input may not leave the device ({CODE})")
        hint = (
            f"a council is asked one question with one input, so this value "
            f"reaches every member of `{verdict.council}` and the member above "
            f"is the one placed off the device. Place it on a role declared "
            f"`on_device`, or keep the `{origin}` value out of this crossing. "
            f"The path is {chain}.")
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


# ---------------------------------------------------------------------------
# The crossing side (roadmap item 512, slice S4)
# ---------------------------------------------------------------------------
#
# `check()` decides which roles an action MAY reach. This decides which it
# DOES. The two are not the same question and the gap between them is the one
# item 515's inheritance note names: a scheduler that picks a role no arm names
# has widened the placement, and a permission that cannot be widened is the
# only kind worth writing down.
#
# WHICH WAY IT FAILS. A crossing placed on a role the block does not name is
# REFUSED, never accommodated by widening the block. An action with no block is
# untouched, which is the same line `admits()` draws for an unrouted component:
# a program that declared no placement here is judged by the rules that judged
# it before item 512, and making `route model` mandatory would be a different
# item. What this closes is the case where the author DID declare a placement
# and a crossing went somewhere else.


def role_of_crossing(capability, roles) -> str | None:
    """The model role a crossing is placed on, or None.

    A `model.<tail>` capability token (item 343, which already parses) names a
    role exactly when `<tail>` is a DECLARED `model role`. That reading is
    opt-in by construction and cannot disturb a shipped program: `model.complete`
    is an OPERATION token, and it stays one in every compilation that does not
    declare a role called `complete`. A program that declares `model role local
    on_device` and writes `emission[model.local]` has said, in the program,
    which placement the crossing is.

    The ambiguity is real and is left visible rather than resolved by a second
    syntax: naming a role after an operation word makes that operation token a
    placement. It is the author's own choice of name, it is in the program, and
    the alternative - a separate spelling for "this crossing is on role R" -
    would be a second vocabulary for a fact item 343's token already carries.

    A parameterised capability (item 294, `model.complete(calls=3)`) is read by
    its token head, the same way every other reader of a capability token reads
    one.
    """
    if not capability:
        return None
    head, _, tail = str(capability).partition(".")
    if head != MODEL_SCOPE or not tail:
        return None
    tail = tail.split("(", 1)[0]
    return tail if tail in (roles or {}) else None


def reach_of(arms) -> frozenset:
    """Every model role one action's `route model` block names.

    Read off `check()`'s already-validated table rather than re-derived from
    the AST, the discipline section 9 of the design note set for item 514.

    `candidates` is read when a placement carries one, and no placement does
    today. That is deliberate: roadmap item 515 turns an arm into an ORDERED
    CANDIDATE SET (`<origin> -> a | b | c`), and every candidate in a set is a
    role the action may reach. Reading the key here means the reach widens with
    that surface the moment `check()` records it, instead of this rule refusing
    a fallback the program plainly names. It is the one line that has to move
    when the arm grows, and it is already written.
    """
    out: set[str] = set()
    for placement in (arms or {}).values():
        if not isinstance(placement, dict):
            continue
        if placement.get("role"):
            out.add(placement["role"])
        out.update(c for c in (placement.get("candidates") or ()) if c)
        # item 516 slice 2: an action routed to a COUNCIL reaches every
        # member's role, because the council is one question asked of every
        # member. A crossing placed on a member's role is therefore inside the
        # placement the block names, and refusing it would widen nothing and
        # protect nothing.
        out.update(r for r in (placement.get("member_roles") or ()) if r)
    return frozenset(out)


def reach_refusal(role: str, action: str, component: str, crossing: str,
                  capability: str, arms) -> tuple[str, str]:
    """The message and hint for a crossing placed outside its action's reach."""
    reach = sorted(reach_of(arms))
    named = ", ".join(f"`{r}`" for r in reach) or "no role at all"
    message = (
        f"the model crossing `{crossing}` (`{capability}`) in action "
        f"`{action}` ({component}) is placed on model role `{role}`, which no "
        f"arm of its `route model` block names: the block reaches {named} "
        f"({CODE})")
    hint = (
        f"a `route model` block is the complete list of roles the action may "
        f"reach, so a crossing on a role outside it widens the placement "
        f"instead of using it - and a placement that can be widened is not a "
        f"permission. Add an arm naming `{role}` to `route model on {action}`, "
        f"or place this crossing on a role the block already names. Dropping "
        f"the block is not the fix: it would leave the action with no declared "
        f"placement at all (docs/design/554-route-model-remaining.md).")
    return message, hint
