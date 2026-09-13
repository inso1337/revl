"""The intent-vs-action refinement decision (roadmap item 470, slices 1-2).

Item 470's shape: something DECLARES an intent (the object an action goes to,
the verb it performs there, the ceiling it may spend, the tenant it runs in),
and the machinery that later fires the action must prove that what it is about
to do is a permitted refinement of that declaration. This module is that
decision's semantic kernel: the `Intent` record, the `Action` record, and the
one function that compares them.

WHY A KERNEL, AND WHY IT SHIPS FIRST. The four dimensions are checked today, but
in four places, in four vocabularies, and not one of them can say WHICH stated
intent an action exceeded:

  * the object is `cap_order.covers` (the tree's ONE partial order), and its
    ceiling parameters are deliberately ERASED at mint into the shipped
    `remainingUses` counter and EXEMPT from crossing-coverage
    (`cap_order.is_ceiling`), so a crossing is never compared against a stated
    amount at the moment it fires;
  * the tenant is the item-33 realm, compared as composition SHAPE ("tenants
    never reach each other"), not as "this action must run in the tenant the
    intent named";
  * the verb has no home at all: the boundary policy's patterns are flat globs
    over boundary tokens (`component Agent* may reach db`), so nothing in the
    tree can express "may reach `db`, but only to `execute` on it";
  * "no hidden extra capability" is the all-or-nothing scan over a ticket's
    capability set, whose refusal names the capabilities, never the intent.

The roadmap's exit criterion is narrower than "refuse the action". It is
*refused with the intent it violated*. That diagnostic is the part with no home
yet, and it is what this kernel is.

THE PROPERTY THE KERNEL CARRIES, and the reason it is worth landing alone: no
dimension is free by OMISSION. Every field that could read as permission when
left out is required (no defaults), so a half-written intent cannot be silently
read as an unconstrained one, and each of the three asymmetries below is spelled
out rather than defaulted:

  * an intent that declares no tenant (`tenant=None`) does not constrain
    tenancy, but an ACTION that declares no tenant can never be shown to be in
    a stated tenant, so it is refused against one (fail closed: unknown is not
    permitted);
  * an intent that states no ceiling does NOT leave the spend unbounded: an
    action that states a spend against an intent that states no ceiling is
    spending outside the stated intent, so it is refused (the one admitted
    pair is the exhaustive intent, no ceiling stated and none spent);
  * an action that states no amount cannot be shown to be within a stated
    ceiling, so it is refused.

The only two fields with a default are the two whose empty value is the NARROW
reading, so the property holds: `related=()` names one object, and `scopes=()`
permits no set-valued scope at all.

Reading `Intent` as a contract and `refine` as its satisfaction check also fixes
the direction the roadmap demands: refinement is checked against what was
declared, and nothing here INFERS an intent the caller never stated.

THE SET-VALUED DIMENSION (slice 2). The issue names one clause the four
dimensions above cannot express: "no broader recipient scope". It cannot live in
`object`, because a `cap_order` valuation is single-valued (one path, one host,
one table) while a recipient list is a SET, and it cannot live in `ceilings`,
because the comparison is containment and not `<=`. `Intent.scopes` /
`Action.scopes` are that dimension, named rather than hard-coded to recipients:
the same containment rule is what "no broader data scope" needs, over the coarse
data classes item 249's taint model already tracks (`web`, `net`, `fs`, `model`,
`input`, `secret`, `confidential`). Naming the scope keeps the kernel from
inventing a recipient concept the tree does not have yet: it holds whatever set a
caller declares and compares it, and the stage that introduces recipients as a
first-class notion supplies the name.

`related` is the other half of the issue's object clause, "same or EXPLICITLY-
related object". Relatedness is a declaration and never an inference, which is
item 470's scope note read one dimension down: two boundaries are related here
only because the intent named both.

ONE DELIBERATE NON-DECISION. The object dimension is delegated to
`cap_order.covers` and not reimplemented: "same or related object" over one
declared capability is exactly that order's job (token identity plus containment
in the `path` order and equality on the discrete kinds), and a second order here
would be a second source of truth. `related` widens the SET of capabilities the
order is read against, never the order itself.

NOT-YET (named in docs/design/470-intent-refinement.md §4). Nothing here is
wired: not the checker, not the class-(c) approval gate, not the lease path, not
the operator profile. This module is the decision those four stages will share.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .cap_order import Cap, covers, is_ceiling, is_registered, split_ceilings

__all__ = [
    "Action",
    "Intent",
    "Refusal",
    "Violation",
    "ceiling_params",
    "refine",
    "scope_members",
]


class Violation(enum.Enum):
    """Which part of a stated intent an action exceeded.

    The four the roadmap's exit criterion names plus the issue's set-valued
    scope, with `object` split in two because `covers` refuses two different
    things that an operator reads differently: a DIFFERENT boundary (the
    confused-deputy case, where the action reaches a capability the intent never
    named) and the SAME boundary outside
    the declared cone (a sibling path, another host, a dropped parameter).

      * ``EXTRA_CAPABILITY``: a capability the intent does not name.
      * ``OBJECT``: a named boundary, outside its declared scope.
      * ``VERB``: an operation the intent does not permit on that boundary.
      * ``CEILING``: an amount above the stated ceiling, an action that states
        no amount against a stated ceiling, or a spend on a ceiling the intent
        does not state.
      * ``SCOPE``: a set-valued scope (the issue's recipients, a data-class set)
        holding a member the intent does not permit, an action that states no
        members against a stated scope, or members in a scope the intent does
        not state.
      * ``TENANT``: a tenant other than the declared one, or an action that
        declares none against a stated tenant.
    """

    EXTRA_CAPABILITY = "extra_capability"
    OBJECT = "object"
    VERB = "verb"
    CEILING = "ceiling"
    SCOPE = "scope"
    TENANT = "tenant"


@dataclass(frozen=True)
class Refusal:
    """One action's failure to refine a stated intent, naming the intent.

    The shape mirrors `errors.RevlError` (`revl/errors.py`): a one-line
    `message` and an indented `hint`, with `__str__` rendering exactly those two
    lines, so a later stage wires this into the checker or the approval gate's
    diagnostic without introducing a second convention. There is deliberately no
    `filename`/`line` here: the same kernel is reachable from a runtime crossing
    that has no source location, and a field that is empty at half its call
    sites is a field every reader has to re-derive.
    """

    violation: Violation
    # the Intent field that was exceeded: object/verbs/ceilings/scopes/tenant
    dimension: str
    declared: str  # what the intent stated, rendered
    requested: str  # what the action asked for, rendered
    message: str
    hint: str

    def __str__(self) -> str:
        return f"{self.message}\n  {self.hint}"


def _canonical_ceilings(
    pairs: Iterable[tuple[str, int]],
) -> tuple[tuple[str, int], ...]:
    """Canonicalize a ceiling/amount map into sorted, name-unique pairs.

    The single validation point for both directions: an intent's stated
    `ceilings` and an action's `amounts`. Every name must be a CEILING-kind
    capability parameter, the same registry `cap_order` orders them by. A
    resource-kind name (`path`, `host`, `table`) is refused rather than accepted
    and ignored: those parameters live in the `object` dimension, and letting one
    be spelled in both would let a record state two different bounds for one
    fact, which is exactly the ambiguity this kernel exists to remove. A name
    bound twice is refused too: a mapping hides the contradiction, and the second
    spelling would silently win. Sorted by name so two records with the same
    bounds are equal (the `Cap.params` precedent).

    The VALUE goes through the same rule `cap_order._canon_value` applies to a
    parsed ceiling: a non-negative integer, and never a `bool` (which is an
    `int` in Python, and `calls=True` is not a count). A negative amount is the
    sign fail-open this check closes: `spent[name] > bound` reads a negative
    spend as proof of compliance, so a record is refused before it can be
    compared. A `str` amount (or a `str` bound) would make `refine` raise a bare
    `TypeError` out of the comparison, so it is refused as an unbuildable record
    instead: the kernel is fail-closed through `Refusal`, and a malformed record
    never gets that far.
    """
    seen: dict[str, int] = {}
    for name, value in pairs:
        if not is_ceiling(name):
            raise ValueError(
                f"`{name}` is not a ceiling parameter: the ceiling dimension "
                f"holds only calls/size/time (cap_order's closed registry); "
                f"resource parameters belong to the object dimension"
            )
        if name in seen:
            raise ValueError(
                f"ceiling parameter `{name}` is bound twice; a ceiling states one "
                f"bound, and a second spelling would silently win"
            )
        if not _is_amount(value):
            raise ValueError(
                f"`{name}={value!r}` is not a numeric bound: `{name}` is a "
                f"ceiling parameter and holds a non-negative integer count "
                f"(cap_order's ceiling registry), never a `{type(value).__name__}`"
            )
        if value < 0:
            raise ValueError(
                f"`{name}={value}` is negative: a ceiling is a count of at most "
                f"N, and neither a negative bound nor a negative spend can be "
                f"shown to be within one. Write a non-negative integer"
            )
        seen[name] = value
    return tuple(sorted(seen.items()))


def _is_amount(value: object) -> bool:
    """Whether `value` is a usable ceiling bound or spend.

    `bool` is excluded on purpose: it IS an `int` in Python, so `calls=True`
    would otherwise compare as `calls=1`. This mirrors `cap_order._canon_value`.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def ceiling_params(bounds: Mapping[str, int]) -> tuple[tuple[str, int], ...]:
    """Canonicalize a ceiling map into the sorted pair-tuple the records store."""
    return _canonical_ceilings(bounds.items())


def _canonical_members(name: str, members: Iterable[str]) -> frozenset[str]:
    """Canonicalize one scope's member set.

    A bare `str` is REFUSED for the reason `_canonical_verbs` refuses one:
    `frozenset("alice")` is the five characters `{'a','l','i','c','e'}`, so a
    caller who named one recipient would declare five single-character
    recipients and the intended one would be refused. On the ACTION side the
    same slip is worse than a refusal, because a one-character member set is a
    *narrower* request than the name the caller meant, so a check that iterated
    the string could admit a send the intent never permitted.
    """
    if isinstance(members, str):
        raise ValueError(
            f"scope `{name}` was given the bare string {members!r}, and a scope "
            f"holds a SET of members: iterating it would declare its "
            f"{len(set(members))} distinct characters instead of the one member. "
            f"Pass a set/frozenset of names, or `({members!r},)` for a single one"
        )
    canonical = frozenset(members)
    for member in canonical:
        if not isinstance(member, str) or not member:
            raise ValueError(
                f"scope `{name}` holds member names, and `{member!r}` is not a "
                f"non-empty string: a member is the name of one recipient, one "
                f"data class, or one other nameable party in that scope"
            )
    return canonical


def _canonical_scopes(
    pairs: Iterable[tuple[str, Iterable[str]]],
) -> tuple[tuple[str, frozenset[str]], ...]:
    """Canonicalize the set-valued dimension, for both records.

    The single validation point for an intent's permitted `scopes` and an
    action's requested ones, the sibling of `_canonical_ceilings`. Refinement
    here is set CONTAINMENT rather than the ceiling dimension's `<=`, which is
    the whole reason the dimension exists: the issue's "no broader recipient
    scope" and "no broader data scope" are both a requested set that must not
    escape a declared one, and neither is expressible as a `Cap` parameter
    (`cap_order`'s valuations are single-valued: one path, one host, one table).

    A scope name registered as a capability parameter (`path`, `host`, `table`,
    `calls`, `size`, `time`, and the budget aliases) is refused: those facts are
    already bounded by the object or the ceiling dimension, and a second bound on
    one fact is exactly the ambiguity this kernel exists to remove. A name bound
    twice is refused for the reason a ceiling bound twice is: a mapping hides the
    contradiction and the second spelling would silently win. Sorted by name so
    two records with the same scopes are equal.
    """
    seen: dict[str, frozenset[str]] = {}
    for name, members in pairs:
        if not isinstance(name, str) or not name:
            raise ValueError(
                f"`{name!r}` is not a scope name: a scope is named by a "
                f"non-empty string (the issue's `recipients`, a data-class set)"
            )
        if is_registered(name):
            raise ValueError(
                f"`{name}` is a capability parameter and cannot name a scope: "
                f"the object and ceiling dimensions already bound it, and one "
                f"fact bounded twice lets the weaker comparison win"
            )
        if name in seen:
            raise ValueError(
                f"scope `{name}` is bound twice; a scope states one member set, "
                f"and a second spelling would silently win"
            )
        seen[name] = _canonical_members(name, members)
    return tuple(sorted(seen.items(), key=lambda pair: pair[0]))


def scope_members(
    scopes: Mapping[str, Iterable[str]],
) -> tuple[tuple[str, frozenset[str]], ...]:
    """Canonicalize a scope map into the sorted pair-tuple the records store."""
    return _canonical_scopes(scopes.items())


def _canonical_verbs(verbs: Iterable[str]) -> frozenset[str]:
    """Canonicalize a verb declaration into the set an intent stores.

    A bare `str` is REFUSED rather than iterated. `frozenset("write")` is the
    five characters `{'w','r','e','t','i'}`, so a caller who wrote one verb would
    declare five and the intended verb would be refused while `w` was admitted:
    a widening, not a spelling slip. The verb dimension is a set of operation
    names and a string is ONE name, so the ambiguity is refused instead of
    guessed at (fail closed: an unbuildable declaration cannot be read as a
    permissive one).
    """
    if isinstance(verbs, str):
        raise ValueError(
            f"`verbs={verbs!r}` is a bare string, and `verbs` is a set of "
            f"operation names: iterating it would declare its "
            f"{len(set(verbs))} distinct characters instead of the one verb. "
            f"Pass a set/frozenset of names, or `verbs=({verbs!r},)` for a "
            f"single verb"
        )
    declared = frozenset(verbs)
    for verb in declared:
        if not isinstance(verb, str):
            raise ValueError(
                f"`verbs` holds operation names, and `{verb!r}` is a "
                f"`{type(verb).__name__}`: a verb is the string the action "
                f"performs"
            )
    return declared


def _render_params(params: Iterable[tuple[str, object]]) -> str:
    rendered = ", ".join(f"{name}={value}" for name, value in params)
    return rendered if rendered else "none"


def _render_members(members: frozenset[str]) -> str:
    return "{" + ", ".join(sorted(members)) + "}" if members else "{}"


def _render_scopes(scopes: Iterable[tuple[str, frozenset[str]]]) -> str:
    rendered = ", ".join(
        f"{name}={_render_members(members)}" for name, members in scopes
    )
    return rendered if rendered else "none"


def _canonical_objects(objects: Iterable[Cap], field: str) -> tuple[Cap, ...]:
    """Validate and de-duplicate a declared object list.

    Every entry goes through the same "no ceiling parameter on an object" rule
    the primary object goes through, so an explicitly-related object cannot
    smuggle in a second ceiling declaration that the ceiling dimension would
    never compare. Duplicates are dropped rather than refused (`Cap` is frozen
    and hashable, so the same spelling twice is one declaration written twice,
    not a contradiction) and declaration order is kept, because that is the order
    a refusal renders them in.
    """
    canonical: list[Cap] = []
    for cap in objects:
        if not isinstance(cap, Cap):
            raise ValueError(
                f"`{field}` holds capabilities, and `{cap!r}` is a "
                f"`{type(cap).__name__}`: use `cap_order.make_cap` or "
                f"`cap_order.parse_cap` to build one"
            )
        for name, _value in cap.params:
            if is_ceiling(name):
                raise ValueError(
                    f"`{name}` is a ceiling parameter and cannot be bound on "
                    f"`{field}`: a ceiling is declared once, in `ceilings`, "
                    f"which is the one comparison of it"
                )
        if cap not in canonical:
            canonical.append(cap)
    return tuple(canonical)


@dataclass(frozen=True)
class Intent:
    """What was declared: object, verbs, ceilings, tenant. All four required.

    `object` is a `cap_order.Cap` carrying its RESOURCE valuation only; its
    ceiling parameters are held separately in `ceilings` (see `from_cap`, which
    does the split with `cap_order.split_ceilings`). A bare token is `covers`'
    own top of its cone, so `Cap("fs.write")` with no `path` admits every path
    under that token. That is the existing meaning of a bare-token grant, reused
    and not widened.

    `verbs` is the set of operations permitted ON that object. The empty set is
    a legitimate contract meaning "no operation is permitted" and refuses every
    action (fail closed); it is not a spelling of "unconstrained". The set is
    canonicalized at construction, and a bare `str` is refused rather than
    iterated: `frozenset("write")` is the five single-character verbs, which
    would refuse `write` and admit `w`.

    `tenant` is the item-33 realm, or `None` when the intent does not constrain
    tenancy. `None` is a STATED absence (an argument the caller must write),
    never a default.

    `related` and `scopes` are the two dimensions whose OMISSION grants nothing,
    so they are the only two fields with a default, and the default is the empty
    declaration:

      * `related` is the issue's "same or EXPLICITLY-RELATED object": the further
        capabilities this intent names beside `object`. Omitting it declares one
        object, which is the narrow reading, and relatedness is never inferred
        (item 470's scope note): two boundaries are related here only because the
        intent said so.
      * `scopes` is the issue's "no broader recipient scope", generalized to the
        set-valued dimension it belongs to. Each entry maps a scope name onto the
        members permitted in it (`("recipients", {"alice", "bob"})`,
        `("data", {"invoices"})`). Omitting it states no scope, and an action may
        then request none: a scope the intent does not state is not a free one.
    """

    object: Cap
    verbs: frozenset[str]
    ceilings: tuple[tuple[str, int], ...]
    tenant: str | None
    related: tuple[Cap, ...] = ()
    scopes: tuple[tuple[str, frozenset[str]], ...] = ()

    def __post_init__(self) -> None:
        # A ceiling parameter smuggled into `object` would be compared twice,
        # by two different rules, and the weaker comparison would win. Refuse it
        # at construction (fail closed) rather than let the two dimensions
        # disagree about one bound.
        for name, _value in self.object.params:
            if is_ceiling(name):
                raise ValueError(
                    f"`{name}` is a ceiling parameter and cannot be bound on an "
                    f"intent's object: it belongs in `ceilings`, which is the "
                    f"one comparison of it (use Intent.from_cap to split it)"
                )
        # The verb set, the ceilings and the scopes go through the SAME checks an
        # action's do, and at construction rather than at comparison: a
        # hand-built `Intent` is held to exactly the contract `from_cap` routes
        # through, so `_canonical_ceilings` and `_canonical_scopes` really are the
        # one validation point for both directions and neither record can carry a
        # bound the other would refuse.
        object.__setattr__(self, "verbs", _canonical_verbs(self.verbs))
        object.__setattr__(self, "ceilings", _canonical_ceilings(self.ceilings))
        object.__setattr__(self, "related", _canonical_objects(self.related, "related"))
        object.__setattr__(self, "scopes", _canonical_scopes(self.scopes))

    @classmethod
    def from_cap(
        cls,
        cap: Cap,
        *,
        verbs: Iterable[str],
        tenant: str | None,
        related: Iterable[Cap] = (),
        scopes: Iterable[tuple[str, Iterable[str]]] = (),
    ) -> "Intent":
        """Build an intent from a declared capability spelling.

        This is the typed link the issue asks for: the SAME spelling the policy,
        the G4 check and the approval gate already speak (`fs.write(path="/tmp",
        calls=3)`) becomes the named dimensions, with the ceiling parameters
        peeled off by `cap_order.split_ceilings` (the one place that split is
        defined) instead of by a second parse here.

        A `related` spelling goes through the same split, and a ceiling on one is
        REFUSED rather than merged: the ceiling is one bound on the whole intent,
        declared on the primary spelling, and a per-object ceiling would be a
        second ceiling comparison the `ceilings` dimension never performs.
        """
        obj, ceilings = split_ceilings(cap)
        siblings: list[Cap] = []
        for sibling in related:
            if not isinstance(sibling, Cap):
                raise ValueError(
                    f"`related` holds capabilities, and `{sibling!r}` is a "
                    f"`{type(sibling).__name__}`"
                )
            bare, sibling_ceilings = split_ceilings(sibling)
            if sibling_ceilings:
                raise ValueError(
                    f"the related spelling `{sibling.to_str()}` states the "
                    f"ceiling(s) {', '.join(sorted(sibling_ceilings))}: an "
                    f"intent states one ceiling, on `{cap.to_str()}`, because "
                    f"the ceiling dimension compares one declared bound against "
                    f"one spend"
                )
            siblings.append(bare)
        return cls(
            obj,
            verbs,
            ceiling_params(ceilings),
            tenant,
            tuple(siblings),
            _canonical_scopes(scopes),
        )

    def objects(self) -> tuple[Cap, ...]:
        """Every capability this intent names: the primary one, then the
        explicitly-related ones in declaration order."""
        return (self.object, *self.related)

    def ceiling_map(self) -> dict[str, int]:
        return dict(self.ceilings)

    def scope_map(self) -> dict[str, frozenset[str]]:
        return dict(self.scopes)


@dataclass(frozen=True)
class Action:
    """What is about to execute: object, verb, amounts, tenant. All required.

    `object` carries the resource valuation of the boundary being reached;
    `amounts` is what this action SPENDS, in the units of `cap_order`'s ceiling
    registry, canonicalized through the same check as an intent's `ceilings` so
    one bound cannot be spelled in both dimensions. An action that spends nothing
    declares `amounts=()`, which is a different statement from an action whose
    spend is unknown: the kernel has no encoder for "unknown", so a caller that
    cannot say must not build an Action at all (an unbuildable argument is a
    stronger refusal than a refused one).

    `scopes` is what this action REACHES in each set-valued scope, read the same
    way `amounts` is read against `ceilings`: `("recipients", {"alice"})` reaches
    one recipient, `("recipients", ())` reaches none, and a scope the action does
    not mention at all is the unstated case the ceiling dimension already refuses
    against a stated bound. Its default is the empty declaration for the same
    reason `amounts` has none to default: an action that reaches nobody cannot
    exceed a scope, and an action that reaches someone must say so.
    """

    object: Cap
    verb: str
    amounts: tuple[tuple[str, int], ...]
    tenant: str | None
    scopes: tuple[tuple[str, frozenset[str]], ...] = ()

    def __post_init__(self) -> None:
        for name, _value in self.object.params:
            if is_ceiling(name):
                raise ValueError(
                    f"`{name}` is a ceiling parameter and cannot be bound on an "
                    f"action's object: what an action SPENDS goes in `amounts`"
                )
        object.__setattr__(self, "amounts", _canonical_ceilings(self.amounts))
        object.__setattr__(self, "scopes", _canonical_scopes(self.scopes))

    @classmethod
    def from_cap(
        cls,
        cap: Cap,
        *,
        verb: str,
        tenant: str | None,
        scopes: Iterable[tuple[str, Iterable[str]]] = (),
    ) -> "Action":
        """Build an action from the capability spelling of the crossing.

        The other half of `Intent.from_cap`'s typed link, and the same split:
        a caller that holds one spelling for what is about to run
        (`model.complete(host="prod", calls=1)`) gets the object valuation and
        the spend from `cap_order.split_ceilings` rather than peeling the
        parameters apart itself, which is where the two dimensions would drift.
        """
        obj, amounts = split_ceilings(cap)
        return cls(obj, verb, ceiling_params(amounts), tenant, _canonical_scopes(scopes))

    def amount_map(self) -> dict[str, int]:
        return dict(self.amounts)

    def scope_map(self) -> dict[str, frozenset[str]]:
        return dict(self.scopes)


def refine(intent: Intent, action: Action) -> Refusal | None:
    """Is `action` a permitted refinement of `intent`?

    `None` when every dimension the intent states admits the action; otherwise
    the ONE `Refusal` naming the intent it violated. Dimensions are checked from
    the narrowest authority outward (object, verb, ceiling, scope, tenant), so
    the finding is the first stated dimension the action exceeds, and a caller
    that fixes one and re-runs gets the next.

    The object dimension is `cap_order.covers`, evaluated with each capability
    the intent names as the wider side, so this function is strictly a READING of
    the tree's one partial order: every action `covers` refuses under every
    declared object is refused here too, and the refusal says which of the two
    findings it is.
    """
    violation = _object_violation(intent, action)
    if violation is not None:
        return violation

    if action.verb not in intent.verbs:
        return Refusal(
            violation=Violation.VERB,
            dimension="verbs",
            declared=_render_verbs(intent.verbs),
            requested=action.verb,
            message=(
                f"the action performs `{action.verb}` on "
                f"`{action.object.to_str()}`, which the declared intent does "
                f"not permit there"
            ),
            hint=(
                "the verb is part of a declared intent, not a detail of the "
                "call: authorizing an object is not authorizing every operation "
                "on it. Widen the intent's verbs, or perform one it permits "
                "(roadmap item 470)"
            ),
        )

    violation = _ceiling_violation(intent, action)
    if violation is not None:
        return violation

    violation = _scope_violation(intent, action)
    if violation is not None:
        return violation

    if intent.tenant is not None and action.tenant != intent.tenant:
        requested = action.tenant if action.tenant is not None else "<unstated>"
        return Refusal(
            violation=Violation.TENANT,
            dimension="tenant",
            declared=intent.tenant,
            requested=requested,
            message=(
                f"the action runs in tenant `{requested}`, and the declared "
                f"intent is confined to tenant `{intent.tenant}`"
            ),
            hint=(
                "the tenant is the item-33 realm: the same boundary in a "
                "different realm is a different system, and an action that "
                "states no realm cannot be shown to be in the declared one "
                "(fail closed). Run the action in the declared tenant"
            ),
        )

    return None


def _render_objects(intent: Intent) -> str:
    """Every object the intent names, as the refusal renders them.

    One declared object renders as its own spelling, byte-identical to what a
    single-object intent produced before `related` existed, so a refusal over an
    intent that names no related object reads exactly as it did.
    """
    return ", ".join(cap.to_str() for cap in intent.objects())


def _object_violation(intent: Intent, action: Action) -> Refusal | None:
    """The object dimension: token identity first, then the declared cones.

    An intent admits the action if ANY capability it names covers it, which is
    the issue's "same or explicitly-related object". Relatedness is a
    DECLARATION, never an inference: `related` holds the capabilities the intent
    itself named, so this stays a reading of `covers` over a declared set rather
    than a second, looser notion of "related".
    """
    for declared_object in intent.objects():
        if covers(declared_object, action.object):
            return None
    declared = _render_objects(intent)
    requested = action.object.to_str()
    if action.object.token not in {cap.token for cap in intent.objects()}:
        return Refusal(
            violation=Violation.EXTRA_CAPABILITY,
            dimension="object",
            declared=declared,
            requested=requested,
            message=(
                f"the action reaches `{requested}`, a capability the declared "
                f"intent does not name (it names `{declared}`)"
            ),
            hint=(
                "no hidden extra capability: an intent authorizes actions on the "
                "boundaries it names and on no other, related boundaries are "
                "related only because the intent said so, and distinct tokens "
                "are incomparable in the capability order. Name the boundary in "
                "the declared intent, or route the action through one it already "
                "names (roadmap item 470)"
            ),
        )
    return Refusal(
        violation=Violation.OBJECT,
        dimension="object",
        declared=declared,
        requested=requested,
        message=(
            f"the action reaches `{requested}`, outside the scope the declared "
            f"intent authorizes on `{action.object.token}` (`{declared}`)"
        ),
        hint=(
            "same or explicitly-related object: an intent admits every scope at "
            "or below one of the capabilities it names, in the capability order "
            "(a path under a declared path, an equal discrete value), and "
            "nothing beside them. A dropped parameter "
            "is a WIDER request, not an unconstraining one. Narrow the action "
            "into the declared cone, or widen the declared intent"
        ),
    )


def _unusable_amount(
    name: str,
    value: object,
    side: str,
    declared: str,
    requested: str,
) -> Refusal:
    """The refusal for a bound or a spend that is not a usable ceiling value."""
    what = "a negative integer" if _is_amount(value) else f"a `{type(value).__name__}`"
    return Refusal(
        violation=Violation.CEILING,
        dimension="ceilings",
        declared=declared,
        requested=requested,
        message=(
            f"the {side} states `{name}={value!r}`, which is {what}, and a "
            f"ceiling parameter holds a non-negative integer"
        ),
        hint=(
            "a ceiling is a static count of at most N: a value that is not one "
            "cannot be compared, and a negative spend reads as compliance with "
            "every bound (fail closed). Both records refuse such a value at "
            "construction; this one did not come through that check"
        ),
    )


def _unusable_amounts(intent: Intent, action: Action) -> Refusal | None:
    """Refuse a bound or a spend `_canonical_ceilings` would not accept.

    Both records canonicalize their values at construction, so a record built
    the documented way never reaches this check. A record that skipped
    `__post_init__` can: unpickling a frozen dataclass rebuilds it with
    `__new__` plus a state assignment, and a negative or non-numeric value can
    be planted with `object.__setattr__`. `refine` is fail-closed through
    `Refusal`, and a bare `TypeError` out of the comparison below is not a
    refusal, so both sides are checked before any comparison happens.
    """
    spent = action.amount_map()
    for name, bound in intent.ceilings:
        if not _is_amount(bound) or bound < 0:
            return _unusable_amount(
                name,
                bound,
                "declared intent",
                f"{name}={bound!r}",
                f"{name}={spent.get(name, '<unstated>')}",
            )
    for name in sorted(spent):
        amount = spent[name]
        if not _is_amount(amount) or amount < 0:
            return _unusable_amount(
                name,
                amount,
                "action",
                _render_params(intent.ceilings),
                f"{name}={amount!r}",
            )
    return None


def _ceiling_violation(intent: Intent, action: Action) -> Refusal | None:
    """The ceiling dimension, in the fail-closed order: a stated bound is met
    by a stated amount within it, and a spend on an unstated bound is refused."""
    unusable = _unusable_amounts(intent, action)
    if unusable is not None:
        return unusable
    spent = action.amount_map()
    for name, bound in intent.ceilings:
        if name not in spent:
            return Refusal(
                violation=Violation.CEILING,
                dimension="ceilings",
                declared=f"{name}={bound}",
                requested=f"{name}=<unstated>",
                message=(
                    f"the declared intent bounds `{name}={bound}`, and the "
                    f"action states no `{name}` to compare against it"
                ),
                hint=(
                    "an unstated amount is not an amount within the ceiling "
                    "(fail closed). State the spend, or drop the ceiling from "
                    "the declared intent"
                ),
            )
        if spent[name] > bound:
            return Refusal(
                violation=Violation.CEILING,
                dimension="ceilings",
                declared=f"{name}={bound}",
                requested=f"{name}={spent[name]}",
                message=(
                    f"the action spends `{name}={spent[name]}`, above the "
                    f"declared ceiling `{name}={bound}`"
                ),
                hint=(
                    "amount within the stated ceiling: the bound is the intent, "
                    "and an action above it is not a refinement of it. Reduce "
                    "the spend or widen the declared ceiling (roadmap item 470)"
                ),
            )
    for name in sorted(spent):
        if name not in intent.ceiling_map():
            return Refusal(
                violation=Violation.CEILING,
                dimension="ceilings",
                declared=_render_params(intent.ceilings),
                requested=f"{name}={spent[name]}",
                message=(
                    f"the action spends `{name}={spent[name]}`, a ceiling the "
                    f"declared intent does not state"
                ),
                hint=(
                    "a ceiling the intent does not state is not a free one "
                    "(fail closed): the intent bounded a different quantity, so "
                    "it has not authorized this spend. State the ceiling in the "
                    "declared intent, or stop spending it"
                ),
            )
    return None


def _scope_violation(intent: Intent, action: Action) -> Refusal | None:
    """The set-valued dimension: no broader recipient scope, no broader data
    scope. Refinement is containment, and the omission rules are the ceiling
    dimension's, read for sets instead of counts.

    Three refusals, in the order they are checked:

      * a stated scope the action does not mention. The action cannot be shown to
        stay inside a set it never named, exactly as an unstated amount cannot be
        shown to be within a stated ceiling. An action that reaches nobody says
        so explicitly, with an empty member set.
      * members outside the declared set. This is the issue's "no broader
        recipient scope": the refusal names the members that escaped, because
        "recipients: alice, bob" against "recipients: alice" is read by fixing
        the one extra name, not by rereading both sets.
      * members in a scope the intent does not state. A scope the intent did not
        bound is not a free one (the ceiling dimension's third rule): the intent
        authorized reaching a set of parties it never described, which is the
        confused-deputy shape one dimension over.

    The one place this dimension is deliberately kinder than the ceiling
    dimension is the EMPTY member set, and it is kinder because a set says
    something a count cannot: `("recipients", ())` is the statement "this action
    reaches nobody", and reaching nobody is a refinement of every declaration,
    including one that never mentioned the scope. So the rule an author reads off
    the three refusals above is one sentence: state what the action reaches, and
    reaching nothing is always admitted.
    """
    permitted = intent.scope_map()
    requested = action.scope_map()
    for name, members in intent.scopes:
        if name not in requested:
            return Refusal(
                violation=Violation.SCOPE,
                dimension="scopes",
                declared=f"{name}={_render_members(members)}",
                requested=f"{name}=<unstated>",
                message=(
                    f"the declared intent confines `{name}` to "
                    f"{_render_members(members)}, and the action states no "
                    f"`{name}` to compare against it"
                ),
                hint=(
                    "an unstated scope is not a scope within the declared one "
                    "(fail closed). State the members the action reaches, with "
                    f"an empty set when it reaches none, or drop `{name}` from "
                    "the declared intent"
                ),
            )
        extra = requested[name] - members
        if extra:
            return Refusal(
                violation=Violation.SCOPE,
                dimension="scopes",
                declared=f"{name}={_render_members(members)}",
                requested=f"{name}={_render_members(requested[name])}",
                message=(
                    f"the action reaches `{name}={_render_members(extra)}` "
                    f"outside the declared scope "
                    f"`{name}={_render_members(members)}`"
                ),
                hint=(
                    "no broader scope than the intent stated: a scope is a set "
                    "of parties, and refinement is a subset of it. Drop the "
                    "members the intent does not permit, or name them in the "
                    "declared intent (roadmap item 470)"
                ),
            )
    for name in sorted(requested):
        if name not in permitted and requested[name]:
            return Refusal(
                violation=Violation.SCOPE,
                dimension="scopes",
                declared=_render_scopes(intent.scopes),
                requested=f"{name}={_render_members(requested[name])}",
                message=(
                    f"the action reaches "
                    f"`{name}={_render_members(requested[name])}`, a scope the "
                    f"declared intent does not state"
                ),
                hint=(
                    "a scope the intent does not state is not a free one (fail "
                    "closed): the intent bounded no set of parties here, so it "
                    "has not authorized reaching these. State the scope in the "
                    "declared intent, or stop reaching outside the ones it states"
                ),
            )
    return None


def _render_verbs(verbs: frozenset[str]) -> str:
    return ", ".join(sorted(verbs)) if verbs else "<none>"
