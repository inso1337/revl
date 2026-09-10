"""The intent-vs-action refinement decision (roadmap item 470, slice 1).

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
dimension is free by OMISSION. Every field of `Intent` and `Action` is required
(no defaults), so a half-written intent cannot be silently read as an
unconstrained one, and each of the three asymmetries below is spelled out rather
than defaulted:

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

Reading `Intent` as a contract and `refine` as its satisfaction check also fixes
the direction the roadmap demands: refinement is checked against what was
declared, and nothing here INFERS an intent the caller never stated.

TWO DELIBERATE NON-DECISIONS. First, the object dimension is delegated to
`cap_order.covers` and not reimplemented: "same or related object" is exactly
that order's job (token identity plus containment in the `path` order and
equality on the discrete kinds), and a second order here would be a second
source of truth. Second, the "broader recipient scope" dimension the issue also
names is NOT modelled, because no recipient concept exists in the tree at all
(`grep -r recipient src/` is empty; the word appears only in
`docs/interop-bridge.md` and the roadmap). An unused `recipient` field would be
the speculative framework item 470 does not ask for; the follow-up that
introduces recipients owns adding it.

NOT-YET (named in docs/design/470-intent-refinement.md §4). Nothing here is
wired: not the checker, not the class-(c) approval gate, not the lease path, not
the operator profile. This module is the decision those four stages will share.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .cap_order import Cap, covers, is_ceiling, split_ceilings

__all__ = [
    "Action",
    "Intent",
    "Refusal",
    "Violation",
    "ceiling_params",
    "refine",
]


class Violation(enum.Enum):
    """Which part of a stated intent an action exceeded.

    The four the roadmap's exit criterion names, with `object` split in two
    because `covers` refuses two different things that an operator reads
    differently: a DIFFERENT boundary (the confused-deputy case, where the
    action reaches a capability the intent never named) and the SAME boundary
    outside
    the declared cone (a sibling path, another host, a dropped parameter).

      * ``EXTRA_CAPABILITY``: a capability the intent does not name.
      * ``OBJECT``: the named boundary, outside its declared scope.
      * ``VERB``: an operation the intent does not permit on that boundary.
      * ``CEILING``: an amount above the stated ceiling, an action that states
        no amount against a stated ceiling, or a spend on a ceiling the intent
        does not state.
      * ``TENANT``: a tenant other than the declared one, or an action that
        declares none against a stated tenant.
    """

    EXTRA_CAPABILITY = "extra_capability"
    OBJECT = "object"
    VERB = "verb"
    CEILING = "ceiling"
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
    dimension: str  # the Intent field that was exceeded: object/verbs/ceiling/tenant
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
    """

    object: Cap
    verbs: frozenset[str]
    ceilings: tuple[tuple[str, int], ...]
    tenant: str | None

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
        # The verb set and the ceilings go through the SAME checks an action's
        # do, and at construction rather than at comparison: a hand-built
        # `Intent` is held to exactly the contract `from_cap` routes through,
        # so `_canonical_ceilings` really is the one validation point for both
        # directions and neither record can carry a bound the other would refuse.
        object.__setattr__(self, "verbs", _canonical_verbs(self.verbs))
        object.__setattr__(self, "ceilings", _canonical_ceilings(self.ceilings))

    @classmethod
    def from_cap(
        cls, cap: Cap, *, verbs: Iterable[str], tenant: str | None
    ) -> "Intent":
        """Build an intent from a declared capability spelling.

        This is the typed link the issue asks for: the SAME spelling the policy,
        the G4 check and the approval gate already speak (`fs.write(path="/tmp",
        calls=3)`) becomes the four named dimensions, with the ceiling
        parameters peeled off by `cap_order.split_ceilings` (the one place that
        split is defined) instead of by a second parse here.
        """
        obj, ceilings = split_ceilings(cap)
        return cls(obj, verbs, ceiling_params(ceilings), tenant)

    def ceiling_map(self) -> dict[str, int]:
        return dict(self.ceilings)


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
    """

    object: Cap
    verb: str
    amounts: tuple[tuple[str, int], ...]
    tenant: str | None

    def __post_init__(self) -> None:
        for name, _value in self.object.params:
            if is_ceiling(name):
                raise ValueError(
                    f"`{name}` is a ceiling parameter and cannot be bound on an "
                    f"action's object: what an action SPENDS goes in `amounts`"
                )
        object.__setattr__(self, "amounts", _canonical_ceilings(self.amounts))

    def amount_map(self) -> dict[str, int]:
        return dict(self.amounts)


def refine(intent: Intent, action: Action) -> Refusal | None:
    """Is `action` a permitted refinement of `intent`?

    `None` when every dimension the intent states admits the action; otherwise
    the ONE `Refusal` naming the intent it violated. Dimensions are checked in
    the order the intent declares them (object, verb, ceiling, tenant), so the
    finding is the first stated dimension the action exceeds, and a caller that
    fixes one and re-runs gets the next.

    The object dimension is `cap_order.covers`, evaluated with the intent as the
    wider side, so this function is strictly a READING of the tree's one partial
    order: every action `covers` refuses is refused here too, and the refusal
    says which of the two findings it is.
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
                f"`{intent.object.to_str()}`, which the declared intent does "
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


def _object_violation(intent: Intent, action: Action) -> Refusal | None:
    """The object dimension: token identity first, then the declared cone."""
    if covers(intent.object, action.object):
        return None
    declared = intent.object.to_str()
    requested = action.object.to_str()
    if intent.object.token != action.object.token:
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
                "no hidden extra capability: an intent over one boundary "
                "authorizes actions on that boundary and on no other, and "
                "distinct tokens are incomparable in the capability order. Widen "
                "the declared intent, or route the action through the declared "
                "capability (roadmap item 470)"
            ),
        )
    return Refusal(
        violation=Violation.OBJECT,
        dimension="object",
        declared=declared,
        requested=requested,
        message=(
            f"the action reaches `{requested}`, outside the scope the declared "
            f"intent authorizes on `{intent.object.token}` (`{declared}`)"
        ),
        hint=(
            "same or related object: an intent admits every scope at or below "
            "it in the capability order (a path under the declared path, an "
            "equal discrete value) and nothing beside it. A dropped parameter "
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


def _render_verbs(verbs: frozenset[str]) -> str:
    return ", ".join(sorted(verbs)) if verbs else "<none>"
