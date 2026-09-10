"""Policy diff over a recorded WAL (roadmap item 468, issue #820).

`revl simulate policy-diff OLD NEW --history RUN.wal` answers one question: what
would this policy change have done to the actions a run actually took. It reads
the recorded action set out of a WAL, decides each action under OLD and under
NEW through the policy engine's own allow/deny predicates, and prints the newly
allowed set, the newly denied set, and a blast radius whose bound is the WAL's
own action set rather than a number this module invents.

What it cannot say matters as much as what it can, so each limit is a field
rather than a caveat in prose:

* No writer in this tree records the declared capability scope. `scope.caps`
  reaches a record only where a timeline step was annotated BY HAND
  (`Timeline.annotate_step`), and the live recorder path (`Timeline.attach_wal`
  plus `Timeline.record_emission`) never annotates, so every effect record a run
  writes carries no scope and the recorded action set is EMPTY. That is not a
  legacy shape, it is the shape of every WAL a recorder writes today, so an
  unscoped record is withheld from the verdict and the diff refuses to report a
  change over it as clean instead of printing an empty diff (see
  :data:`revl.branch.SCOPE_NOTE` for the same blind spot on the fork's
  classifier).
* A realm-scoped rule decides an action by the realms its component joins, and
  the WAL records no realms. Those actions are undecided unless the caller
  supplies the composition the run was compiled from, and an undecided action is
  never reported as allowed or denied.
* A history the diff could not read WHOLE is withheld the same way: a torn tail
  (the crash itself) or a recording that never reached its `activation-complete`
  marker leaves the recorded action set a prefix of the run rather than the run,
  and a diff over a prefix cannot report the crossings it never saw as
  unaffected. `revl.branch` reports a torn tail as a `torn-tail` finding on the
  same reasoning.
* The capability verdict reads two of the legs an admission is refused by: the
  deny-lists and the closed allow-lists. The agent-sandbox allow-list, the
  taint-flow tier, the approval and declassify rules, the declaration-strength
  floors, the evidence bundle and the recovery surface decide by facts a WAL
  does not carry either, so `LEGS` names each one with the fact it reads and a
  pair whose surface MOVES on one of them is undecided with the leg named. An
  `unchanged` there would be the one wrong answer this surface can give, because
  the admission it previews could then refuse or admit the crossing while the
  preview said nothing happened.
* The bound is history, not traffic. This is a preview over what happened, and
  the reason a widening action that the run never took cannot appear here.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Optional

from . import policy as _policy
from .cap_order import CapError, split_ceilings, parse_cap
from .wal import KIND_EFFECT, read_wal, scope_host_confined, WALIntegrityError

#: The three verdicts one recorded action can carry. `UNDECIDED` is not a
#: fallback for an error: it is the answer for an action the policy decides by a
#: fact the WAL does not carry, and it is reported on every document because a
#: silent `allow` there would be the one wrong answer this surface can give.
ALLOW = "allow"
DENY = "deny"
UNDECIDED = "undecided"

#: What a policy diff over recorded history cannot say. Emptying this list is a
#: claim that a WAL now carries these too.
NOT_ANSWERABLE = (
    {"axis": "future traffic",
     "why": "the bound is the action set of one recorded run, so an action the "
            "run never took is invisible here no matter how the two policies "
            "treat it. `revl policy evaluate` answers the policy-only question "
            "over a whole composition, and `revl audit --diff` answers whether "
            "the generation itself widened"},
    {"axis": "unrecorded scope",
     "why": "no writer records the declared capability scope, so every effect "
            "record a run writes is unscoped, the recorded action set is empty "
            "and a change can hide inside a record whose capability the WAL does "
            "not carry; the diff withholds those records from its verdict and "
            "does not report the change clean while one is present"},
    {"axis": "legs outside the diff",
     "why": "an admission is refused on more legs than the two this diff reads; "
            "`legs` names each one with the fact it decides by, and a recorded "
            "pair whose surface moves on an unmodelled leg is undecided with the "
            "leg named rather than reported unchanged"},
    {"axis": "realms without a composition",
     "why": "a realm-scoped rule needs the component's realms, which only the "
            "compiled composition holds; without --composition every action such "
            "a policy would decide is undecided"},
)


@dataclass(frozen=True)
class Leg:
    """One leg an admission is refused on, and whether this diff reads it.

    `state` is `compared` for the two legs the capability verdict decides, the
    deny-lists and the closed allow-lists, and `unmodelled` for every other leg
    the gate refuses by. `decides` is the fact the leg reads, which is what makes
    a moved surface on an unmodelled leg a fact the WAL does not carry."""

    leg: str
    state: str
    decides: str


#: Every leg `revl.policy.evaluate` can refuse a crossing on, named by the
#: `Violation.kind` the gate mints, so a leg added to the gate is a leg missing
#: from this table (and
#: `test_the_legs_the_gate_refuses_by_are_enumerated_in_the_artifact` reads the
#: gate's own source to say so). `capability_verdict` reads the first two and
#: nothing else reads the rest: a pair whose surface moves on one of them is
#: undecided, because the same recording and the same audit could then be
#: admitted differently by the gate while this preview reported no change.
LEGS = (
    Leg("capability", "compared",
        "the closed allow-list a component or realm rule opens"),
    Leg("deny", "compared",
        "the deny-lists a `may not reach` rule writes over the token"),
    Leg("mcp-sandbox", "unmodelled",
        "the agent-sandbox allow-list, which decides the crossings of a "
        "component admitted through the MCP session and which a WAL does not "
        "name"),
    Leg("taint-flow", "unmodelled",
        "the taint reaches of the component and the reach approvals covering "
        "them, which are audit facts"),
    Leg("declassify", "unmodelled",
        "the origins the component declassified, which are audit facts"),
    Leg("declassify-approval", "unmodelled",
        "the approval edge an endorse must carry, which is an audit fact"),
    Leg("approval", "unmodelled",
        "the approval edges threaded onto the composition, which an IR carries "
        "and a WAL does not"),
    Leg("register", "unmodelled",
        "the declared register of the token, a declaration fact"),
    Leg("evidence", "unmodelled",
        "the evidence bundle the composition carries for a component"),
    Leg("teardown", "unmodelled",
        "the recovery surface a composition carries"),
    Leg("tenant", "unmodelled",
        "the realms of every other tenant and the reach it shares"),
)


class PolicyDiffError(RuntimeError):
    """A WAL or a policy could not be read, so the diff cannot be computed."""


@dataclass(frozen=True)
class Action:
    """One action a run recorded: the component that took it and the capability
    token it declared, with every recorded position that carried it.

    `resource` is the token's resource projection re-read through the capability
    partial order (`split_ceilings`, so a declared ceiling is peeled off), or
    None when the token binds no resource at all. It is a projection and not a
    second reading: `token` is what the policy is evaluated against, exactly as
    the boundary graph's own tokens are."""

    component: str
    token: str
    resource: Optional[str]
    witnesses: tuple = ()


def _cap_token(raw: object) -> tuple:
    """One recorded scope entry as `(token, resource_projection, ceilings)`.

    The token is the canonical `cap_order` spelling, the same string the
    boundary graph carries and the same one a `may reach` glob is matched
    against, so the diff decides exactly what the gate decides. `resource` is
    the token's resource-only projection (`split_ceilings`) or None when the
    token binds no resource, and `ceilings` is what was peeled off. A token the
    partial order cannot re-read keeps its recorded spelling and reports no
    projection, because a diff over recorded history must not drop an action it
    merely failed to parse."""
    text = str(raw)
    try:
        cap = parse_cap(text)
    except CapError:
        return text, None, {}
    resource, ceilings = split_ceilings(cap)
    return cap.to_str(), (resource.to_str() if not resource.is_bare() else None), \
        ceilings


def recorded_actions(wal: dict) -> dict:
    """The action set one WAL recorded, plus the records it could not classify.

    The action set is the crossings the run declared through `scope.caps`: the
    one place a WAL names a capability. A scope the reader classifies as
    host-confined is not a crossing and is counted apart, and an effect record
    with no scope at all is counted as unscoped, which is what the live
    classifier reads an absent scope as too. Nothing in this tree writes a
    declared scope into a record a run produces (`Timeline.annotate_step` is the
    only writer of `Step.scope` and the recorder never calls it), so on a WAL a
    recorder wrote the action set is empty and every effect record is unscoped
    (see :data:`revl.branch.SCOPE_NOTE`)."""
    by_key: dict = {}
    unscoped: list = []
    confined: list = []
    effects = 0
    for record in wal.get("records") or ():
        if record.get("record") != KIND_EFFECT:
            continue
        effects += 1
        scope = record.get("scope")
        caps = tuple(scope.get("caps") or ()) \
            if isinstance(scope, dict) else ()
        component = str(record.get("component"))
        where = {"component": component, "label": record.get("label"),
                 "seq": record.get("seq"), "kind": record.get("kind")}
        if not caps:
            (unscoped if scope is None else confined).append(where)
            continue
        if scope_host_confined(scope):
            confined.append(where)
            continue
        for raw in caps:
            token, resource, ceilings = _cap_token(raw)
            entry = by_key.setdefault(
                (component, token),
                {"component": component, "token": token, "resource": resource,
                 "ceilings": ceilings, "witnesses": []})
            entry["witnesses"].append((record.get("seq"), record.get("label")))
    actions = [Action(component=entry["component"], token=entry["token"],
                      resource=entry["resource"],
                      witnesses=tuple(entry["witnesses"]))
               for entry in by_key.values()]
    return {"actions": actions, "byToken": by_key, "effectRecords": effects,
            "unscoped": unscoped, "confined": confined}


def realms_for(name: str, realms: dict) -> Optional[frozenset]:
    """The realms one component joins, or None when they are not known.

    None is the honest answer for a component the caller did not hand a
    composition for, and it is what makes a realm-scoped rule undecided rather
    than silently permissive."""
    found = realms.get(name)
    if found is None:
        return None
    return frozenset(found)


def _decided_by_realms(policy) -> bool:
    return any(rule.scope == "realm" for rule in policy.rules)


def verdict(policy, name: str, token: str, realms: Optional[frozenset]) -> str:
    """Whether one policy allows, denies or cannot decide one crossing.

    The decision itself is `revl.policy.capability_verdict`, the two legs of the
    gate this diff reads (the deny-lists and the closed allow-lists). This wraps
    it with the one thing the gate can answer and the WAL alone cannot: a
    realm-scoped rule decides by the realms its component joins, and an unknown
    `realms` makes every action such a policy selects undecided rather than
    silently allowed. Every OTHER leg the gate refuses by is invisible here, so
    `moved_legs` is what keeps a pair whose unmodelled leg surface moved from
    being read as decided (`LEGS`)."""
    if realms is None and _decided_by_realms(policy):
        return UNDECIDED
    return _policy.capability_verdict(
        policy, name, realms if realms is not None else frozenset(), token)


def _rows(values) -> tuple:
    """One leg surface, made comparable: sorted, stringified, hashable. Two
    policies with equal rows read the same fact for this pair, so this leg cannot
    be why the pair moved over the same recording and the same audit."""
    return tuple(sorted(str(value) for value in values))


def _could_select(rule, name: str, realms: Optional[frozenset]) -> bool:
    """Whether one component-scoped rule could select this component. With no
    realms in hand a realm-scoped rule is INCLUDED rather than excluded: a rule
    that might decide the pair must not pass unnoticed because the caller did not
    hand the composition over."""
    if realms is None and rule.scope == "realm":
        return True
    return rule.selects(name, realms if realms is not None else frozenset())


#: The token namespace the declassify-approval leg asks about:
#: `policy.py:1981` builds `f"declassify.{origin}"` — one token per declassify
#: record — and `policy.py:1982` asks `approval_rule_for` for it.
DECLASSIFY_TOKEN_PREFIX = "declassify."


def _could_cover_declassify(pattern: str) -> bool:
    """Whether one approval rule could be the rule `approval_rule_for` returns for
    a `declassify.<origin>` token.

    The origins are the audit fact this leg is unmodelled for, so the tokens the
    leg asks about are exactly the `declassify.` namespace and this is the operand
    and namespace the gate itself selects in. A pattern with no wildcard matches
    one literal token, so it reaches that namespace only when it IS in it; a
    pattern WITH a wildcard is taken as reaching it, because deciding that a glob
    cannot match `declassify.<anything>` is not a claim this module can make and a
    rule left out by mistake is the one wrong answer this surface can give."""
    if any(char in pattern for char in "*?["):
        return True
    return pattern.startswith(DECLASSIFY_TOKEN_PREFIX)


def _teardown_rows(policy) -> tuple:
    """The teardown floor ONE policy carries, as the driver computes it.

    `policy.py:1696-1697` does not read the rule list: it reduces it to the
    STRONGEST floor (`max(..., key=_REGISTER_RANK.get)`) and the leg then refuses
    an entry below that one number, so two rule lists with the same strongest
    floor read the same requirement here and a rule added UNDER the floor is not
    a fact this leg decides by. The expression is repeated rather than
    re-spelled so the row cannot drift from the floor the gate computes."""
    if not policy.teardown_rules:
        return ()
    from .lower import _REGISTER_RANK  # noqa: PLC0415
    return (max((rule.strength for rule in policy.teardown_rules),
                key=lambda strength: _REGISTER_RANK.get(strength, 0)),)


def _approval_rows(policy, token: str) -> tuple:
    """The ONE approval rule the ordinary approval leg reads for this token, as
    the driver computes it.

    `policy.py:2414` reads `policy.approval_rule_for(token)`, which is the
    FIRST rule whose glob covers the token (`policy.py:587-594`), and the gate
    refuses on that rule existing. A covering rule BELOW the first one is
    shadowed for this token: the gate returns the same rule with the same ttl, so
    the leg reads nothing that changed and listing every covering rule would move
    the pair on a change the gate never sees. `_could_cover_declassify` above is
    deliberately the WIDER test because there the tokens are `declassify.<origin>`
    for origins the diff does not carry, so every rule that could be the first
    one for some origin has to stay in."""
    rule = policy.approval_rule_for(token)
    if rule is None:
        return ()
    return _rows(((rule.pattern, rule.ttl_ms),))


def _evidence_rows(policy, name: str, token: str, realms) -> tuple:
    """The evidence clauses of one policy that could grade this pair.

    An evidence rule is fail-closed over a conjunction of facets, so its surface
    is the facets and their thresholds, not the rule text: raising a threshold
    under an unchanged rule is exactly the kind of widening a diff of the rule
    text would miss."""
    rows = []
    for rule in policy.evidence_rules:
        if rule.scope == "capability":
            selected = _policy._matches_any(token, (rule.selector,))
        elif rule.scope == "component":
            # The gate's own selector is `_evidence_rule_selects`
            # (`policy.py:1445-1461`): for a component-scope rule it is the
            # component NAME glob plus, for an ORIGIN-scoped rule, the admission
            # origin the rule names (`policy.py:1459`). The origin is an audit
            # fact this diff does not carry, so the name glob is the whole of the
            # operand here and an origin-scoped rule is INCLUDED rather than
            # dropped — the same direction `_could_select` takes for a
            # realm-scoped rule without the composition. The origin itself stays
            # in the row below, so an origin the rule names is a moving surface.
            selected = fnmatchcase(name, rule.selector)
        else:
            selected = True
        if selected:
            rows.append((rule.scope, rule.selector, rule.origin, rule.require,
                         rule.self_attested))
    return tuple(rows)


def leg_surfaces(policy, name: str, token: str, realms) -> dict:
    """What every unmodelled leg reads out of ONE policy for ONE recorded pair.

    A surface has to carry everything the leg reads: the rules that select this
    component and this token, and their thresholds and patterns, because a rule
    changed under the same name is what a diff of the rule text misses. An entry
    that says the leg does not arm for this pair is the empty row, and the
    sandbox entry folds the unarmed policy into the allowed case, since an
    unarmed policy refuses nothing here and neither does an armed one whose list
    carries the token.

    Each row is selected with the operand the leg's own driver in `policy.py`
    selects with, named in the comment above it, because a row keyed on a
    different operand is a leg that silently never moves: `declassify` selects on
    the component and reads origins, `declassify-approval` selects in the
    `declassify.` token namespace, `approval` selects on the capability token and
    reads the first rule that covers it, and the last two are not the same
    expression even though both read one policy field."""
    return {
        "mcp-sandbox": policy.mcp_allow is None
                       or _policy._allowed(token, policy.mcp_allow),
        "taint-flow": _rows((rule.origin, rule.patterns, rule.without_approval)
                            for rule in policy.taint_flow_rules
                            if _policy._matches_any(token, rule.patterns)),
        # `policy.py:1965-1969` selects a declassify rule on the component
        # (`rule.selects(name, realms)`) and then matches its patterns against a
        # TAINT ORIGIN (`_matches_any(origin, rule.patterns)`), not against the
        # pair's capability token: the origins a component declassified are
        # precisely the audit fact this leg is unmodelled for, so every rule the
        # component selector arms is part of the surface and a rule the selector
        # arms must make the pair undecided rather than leave it unchanged.
        "declassify": _rows((rule.selector, rule.patterns)
                            for rule in policy.declassify_rules
                            if _could_select(rule, name, realms)),
        # `policy.py:1978-1984` selects on `approval_rule_for(f"declassify.{origin}")`
        # — a token in the `declassify.` namespace, built per declassify record —
        # so the operand is the approval rules a `declassify.<origin>` token can
        # reach, never `covers(token)` of the pair's capability token. Origins are
        # audit facts, so each such rule is a candidate for some origin, and a
        # change to one of them can be the reason this leg refuses where it
        # admitted.
        "declassify-approval": _rows((rule.pattern, rule.ttl_ms)
                                     for rule in policy.approval_rules
                                     if _could_cover_declassify(rule.pattern)),
        # `policy.py:2414` is `approval_rule_for(token)`: the FIRST approval
        # rule whose glob covers the pair's token, or `None`, and the gate refuses
        # on that rule existing (and reports the ttl it carries). The row is that
        # expression and not the list of every covering rule, because a covering
        # rule below the first one is shadowed for this token and the gate reads
        # the same rule either way.
        "approval": _approval_rows(policy, token),
        "register": _rows((rule.capability, rule.at_least)
                          for rule in policy.register_rules
                          if _policy._matches_any(token, (rule.capability,))),
        "evidence": _rows(_evidence_rows(policy, name, token, realms)),
        # the floor `policy.py:1696-1697` reduces the rules to, not the rule list
        "teardown": _teardown_rows(policy),
        "tenant": (policy.tenants_isolated,),
    }


def moved_legs(old, new, name: str, token: str, realms) -> tuple:
    """The unmodelled legs whose surface moves for one recorded pair.

    Only a leg that MOVED is named. A leg reading the same surface in both
    policies decides this pair identically over the same recording and the same
    audit, so it cannot be the reason the pair changed, and naming it would bury
    the leg that can be."""
    before = leg_surfaces(old, name, token, realms)
    after = leg_surfaces(new, name, token, realms)
    return tuple(leg.leg for leg in LEGS
                 if leg.state != "compared" and before[leg.leg] != after[leg.leg])


def _pair(old, new, action: Action, realms) -> dict:
    before = verdict(old, action.component, action.token, realms)
    after = verdict(new, action.component, action.token, realms)
    legs = moved_legs(old, new, action.component, action.token, realms)
    reasons = []
    if before == UNDECIDED or after == UNDECIDED:
        reasons.append("a realm-scoped rule decides this crossing by realms the "
                       "WAL does not record")
    if legs:
        reasons.append("the change moves the %s leg, which decides by a fact a "
                       "WAL does not carry" % ", ".join(legs))
    if reasons:
        move = UNDECIDED
    elif before != after:
        move = ALLOW if after == ALLOW else DENY
    else:
        move = "unchanged"
    return {"component": action.component, "token": action.token,
            "resource": action.resource, "before": before, "after": after,
            "move": move, "legs": list(legs), "reason": "; ".join(reasons),
            "witnesses": [{"seq": seq, "label": label}
                          for seq, label in action.witnesses]}


def _blast_radius(moves: list, cells: dict, recorded: dict) -> dict:
    """The bound a widening opens, measured against what the WAL recorded.

    The bound is the action set itself: how many distinct actions the run
    recorded, over how many components, and how many of its effect records the
    diff could not classify. A widening can only ever open an action from that
    set, so the ceiling on what this change reaches is already on the page.
    Resource scopes and declared ceilings come from `split_ceilings` over the
    recorded token, and an action whose token declares none reports an empty set
    rather than a guess."""
    gained = [m for m in moves if m["move"] == ALLOW]
    lost = [m for m in moves if m["move"] == DENY]
    undecided = [m for m in moves if m["move"] == UNDECIDED]
    resources: dict = {}
    ceilings: dict = {}
    for move in gained:
        entry = cells[(move["component"], move["token"])]
        if entry["resource"] is not None:
            resources.setdefault(move["component"], set()).add(entry["resource"])
        for name, value in (entry["ceilings"] or {}).items():
            ceilings.setdefault(name, set()).add((move["component"], value))
    return {
        "bound": len(recorded["actions"]),
        "boundKind": "recorded actions in this WAL",
        "recordedActions": len(recorded["actions"]),
        "recordedComponents": len({a.component for a in recorded["actions"]}),
        "effectRecords": recorded["effectRecords"],
        "unscopedRecords": len(recorded["unscoped"]),
        "confinedRecords": len(recorded["confined"]),
        "gained": gained,
        "gainedActions": len(gained),
        "gainedWitnesses": sum(len(m["witnesses"]) for m in gained),
        "lostActions": len(lost),
        "undecidedActions": len(undecided),
        "resourceScopes": {name: sorted(scopes)
                           for name, scopes in sorted(resources.items())},
        "declaredCeilings": {name: sorted(v for _c, v in pairs)
                             for name, pairs in sorted(ceilings.items())},
    }


def _withheld(recorded: dict, wal: dict) -> list:
    """The records the diff could not name, and why, as the artifact's own field.

    A record the diff cannot name is not a record it decided: whatever crossing
    it carried could hide a widening the report would otherwise print as clean,
    which is why a non-empty `withheld` puts the exit status where a widening
    does. A HISTORY that could not be read whole is the same refusal one level
    up, and it is reported here rather than as a clean diff over an empty action
    set: `revl.branch` treats a torn tail as a finding on exactly this reasoning
    (`branch.py:422-426`), and `--history` is the crash-recovery artefact where a
    torn tail is the expected shape. Those two findings carry `records: None`,
    because the count that field carries elsewhere is a count of records the diff
    could not name and here there is no such count."""
    out = []
    if recorded["unscoped"]:
        out.append({
            "axis": "unrecorded scope",
            "records": len(recorded["unscoped"]),
            "why": "no writer records the declared capability scope: `scope.caps` "
                   "reaches a record only where a timeline step was annotated by "
                   "hand (`Timeline.annotate_step`) and the live recorder path "
                   "(`Timeline.attach_wal` plus `record_emission`) never "
                   "annotates, so the recorded action set of a real run is empty "
                   "and a change can hide behind every one of these records",
        })
    if wal.get("torn"):
        out.append({
            "axis": "torn history",
            "records": None,
            "why": "the final record of the history is half written (the crash "
                   "itself), so the recorded action set may be shorter than what "
                   "actually ran and a crossing this diff never saw cannot be "
                   "reported as unaffected. `revl branch` reports the same tail "
                   "as a `torn-tail` finding",
        })
    elif not wal.get("complete"):
        out.append({
            "axis": "unfinished history",
            "records": None,
            "why": "the history carries no `activation-complete` record, so it "
                   "holds the activation prefix of a run that never committed "
                   "rather than a finished recording: an action missing from it "
                   "cannot be read here as an action the change did not affect",
        })
    return out


def diff(old, new, wal: dict, *, realms: Optional[dict] = None, label: str = "") -> dict:
    """The diff of two policies over the recorded actions of one WAL.

    `realms` maps a component name to the realms its composition places it in;
    it is `{}` when the caller supplied no composition, which is what leaves a
    realm-scoped rule undecided.

    A WAL that could not be read whole (`torn`, or a recording that never
    committed) is reported in `withheld` rather than read as an empty run, so a
    caller gating on `widened` never sees a clean diff over a history that was
    not read to the end."""
    recorded = recorded_actions(wal)
    realms = realms or {}
    moves = [_pair(old, new, action, realms_for(action.component, realms))
             for action in recorded["actions"]]
    moves.sort(key=lambda m: (m["component"], m["token"]))
    header = wal.get("header") or {}
    return {
        "kind": "revl.policy-diff",
        "schemaVersion": "1.0",
        "old": getattr(old, "source", "") or "",
        "new": getattr(new, "source", "") or "",
        "history": label,
        "session": header.get("session"),
        "generation": header.get("generation"),
        "complete": bool(wal.get("complete")),
        "torn": bool(wal.get("torn")),
        "oldRules": len(old.rules),
        "newRules": len(new.rules),
        "moves": moves,
        "newlyAllowed": [m for m in moves if m["move"] == ALLOW],
        "newlyDenied": [m for m in moves if m["move"] == DENY],
        "undecided": [m for m in moves if m["move"] == UNDECIDED],
        "unscoped": recorded["unscoped"],
        "withheld": _withheld(recorded, wal),
        "legs": [{"leg": leg.leg, "state": leg.state, "decides": leg.decides}
                 for leg in LEGS],
        "blastRadius": _blast_radius(moves, recorded["byToken"], recorded),
        "notAnswerable": list(NOT_ANSWERABLE),
    }


def widened(doc: dict) -> bool:
    """Whether one diff document says the change cannot be called clean.

    True when the change newly allows a recorded crossing, leaves one undecided,
    or could not name one at all — a record whose scope the WAL did not carry, or
    a history the diff could not read whole (a torn tail, a recording that never
    committed). The CLI and any caller that gates on this document read THIS
    function rather than re-deriving the condition, so the widened cases and the
    exit status cannot drift apart."""
    return bool(doc["newlyAllowed"] or doc["undecided"] or doc["withheld"])


def load_history(path: str) -> dict:
    """Read one WAL, re-raising a read failure as a `PolicyDiffError` so the CLI
    has one error type to catch (`revl.branch._load` sets the precedent)."""
    try:
        return read_wal(path)
    except OSError as error:
        raise PolicyDiffError(f"cannot read WAL {path}: {error}") from None
    except WALIntegrityError as error:
        raise PolicyDiffError(str(error)) from None


def render(doc: dict) -> str:
    """The human view: the two sets, the bound they open, and the legs."""
    lines = ["policy diff over " + (doc["history"] or "(unidentified WAL)")]
    counts = ("  actions    %d recorded  %d newly allowed  %d newly denied  %d undecided"
              % (len(doc["moves"]), len(doc["newlyAllowed"]),
                 len(doc["newlyDenied"]), len(doc["undecided"])))
    lines.append(counts)
    for entry in doc["withheld"]:
        if entry["records"] is None:
            lines.append("  withheld   %s: %s"
                         % (entry["axis"], entry["why"]))
        else:
            lines.append("  withheld   %s over %d record(s): %s"
                         % (entry["axis"], entry["records"], entry["why"]))
    for title, moves in (("NEWLY ALLOWED", doc["newlyAllowed"]),
                         ("NEWLY DENIED", doc["newlyDenied"])):
        lines.append("")
        if not moves:
            lines.append("  %s: none" % title.lower())
            continue
        lines.append("  %s:" % title)
        for move in moves:
            lines.append("    %-18s %-24s was %s, now %s  (%d recorded)"
                         % (move["component"], move["token"], move["before"],
                            move["after"], len(move["witnesses"])))
    if doc["undecided"]:
        lines.append("")
        lines.append("  UNDECIDED (never read as allowed or denied):")
        for move in doc["undecided"]:
            lines.append("    %-18s %-24s was %s, now %s: %s"
                         % (move["component"], move["token"], move["before"],
                            move["after"], move["reason"]))
    if doc["unscoped"]:
        lines.append("")
        lines.append("  UNSCOPED (recorded effect records carrying no capability "
                     "scope; nothing in this tree writes one, so these are the "
                     "crossings the diff cannot name):")
        for entry in doc["unscoped"]:
            lines.append("    %-18s %-24s seq %s"
                         % (entry["component"], entry["label"], entry["seq"]))
    radius = doc["blastRadius"]
    lines += ["", "  blast radius [%s: %d]"
              % (radius["boundKind"], radius["bound"]),
              "    components %d  effect records %d  unscoped %d  host-confined %d"
              % (radius["recordedComponents"], radius["effectRecords"],
                 radius["unscopedRecords"], radius["confinedRecords"]),
              "    opened     %d action(s) over %d recorded crossing(s)"
              % (radius["gainedActions"], radius["gainedWitnesses"]),
              "    closed     %d action(s)" % radius["lostActions"]]
    if radius["resourceScopes"]:
        for name, scopes in radius["resourceScopes"].items():
            lines.append("    resource   %-16s %s" % (name, ", ".join(scopes)))
    else:
        lines.append("    resource   none named by the opened actions")
    if radius["declaredCeilings"]:
        for name, values in radius["declaredCeilings"].items():
            lines.append("    ceiling    %-16s %s"
                         % (name, ", ".join(str(v) for v in values)))
    else:
        lines.append("    ceiling    none named by the opened actions")
    compared = [leg["leg"] for leg in doc["legs"] if leg["state"] == "compared"]
    lines += ["", "  legs       compared here: %s" % ", ".join(compared),
              "             a change that moves one of these leaves the pair "
              "undecided:"]
    for leg in doc["legs"]:
        if leg["state"] != "compared":
            lines.append("    %-20s %s" % (leg["leg"], leg["decides"]))
    lines.append("")
    lines.append("  cannot say (an axis this surface has no answer for at all):")
    for entry in doc["notAnswerable"]:
        lines.append("    cannot say %-24s %s" % (entry["axis"], entry["why"]))
    return "\n".join(lines)
