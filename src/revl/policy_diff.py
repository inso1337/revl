"""Policy diff over a recorded WAL (roadmap item 468, issue #820).

`revl simulate policy-diff OLD NEW --history RUN.wal` answers one question: what
would this policy change have done to the actions a run actually took. It reads
the recorded action set out of a WAL, decides each action under OLD and under
NEW through the policy engine's own allow/deny predicates, and prints the newly
allowed set, the newly denied set, and a blast radius whose bound is the WAL's
own action set rather than a number this module invents.

What it cannot say matters as much as what it can, so each limit is a field
rather than a caveat in prose:

* A WAL names a capability only where the recorded scope declares one. An
  effect record with no scope reads as no declared crossing, exactly as the live
  classifier reads it, and is never resolved to the label it recorded; a WAL
  written before that scope became durable cannot say whether the scope was
  absent or never written down (see :data:`revl.branch.SCOPE_NOTE` for the same
  blind spot on the fork's classifier).
* A realm-scoped rule decides an action by the realms its component joins, and
  the WAL records no realms. Those actions are undecided unless the caller
  supplies the composition the run was compiled from, and an undecided action is
  never reported as allowed or denied.
* The bound is history, not traffic. This is a preview over what happened, and
  the reason a widening action that the run never took cannot appear here.
"""

from __future__ import annotations

from dataclasses import dataclass
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
     "why": "an effect record whose step carried no capability scope cannot be "
            "named from the WAL alone, so it is counted as unscoped rather than "
            "resolved to the label it recorded, and a WAL written before that "
            "field became durable cannot say whether the scope was empty or "
            "never written down"},
    {"axis": "realms without a composition",
     "why": "a realm-scoped rule needs the component's realms, which only the "
            "compiled composition holds; without --composition every action such "
            "a policy would decide is undecided"},
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
    classifier reads an absent scope as too. The two are counted separately
    because the WAL cannot tell them apart: a pre-item-250 record that never had
    its scope written down is indistinguishable from a step that declared none
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

    The decision itself is `revl.policy.capability_verdict`, the same predicate
    the capability leg of the gate reads, so the diff cannot disagree with the
    admission it previews. This wraps it with the one thing the gate can answer
    and the WAL alone cannot: a realm-scoped rule decides by the realms its
    component joins, and an unknown `realms` makes every action such a policy
    selects undecided rather than silently allowed."""
    if realms is None and _decided_by_realms(policy):
        return UNDECIDED
    return _policy.capability_verdict(
        policy, name, realms if realms is not None else frozenset(), token)


def _pair(old, new, action: Action, realms) -> dict:
    before = verdict(old, action.component, action.token, realms)
    after = verdict(new, action.component, action.token, realms)
    if before == UNDECIDED or after == UNDECIDED:
        move = UNDECIDED
    elif before != after:
        move = ALLOW if after == ALLOW else DENY
    else:
        move = "unchanged"
    return {"component": action.component, "token": action.token,
            "resource": action.resource, "before": before, "after": after,
            "move": move, "witnesses": [{"seq": seq, "label": label}
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


def diff(old, new, wal: dict, *, realms: Optional[dict] = None, label: str = "") -> dict:
    """The diff of two policies over the recorded actions of one WAL.

    `realms` maps a component name to the realms its composition places it in;
    it is `{}` when the caller supplied no composition, which is what leaves a
    realm-scoped rule undecided."""
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
        "blastRadius": _blast_radius(moves, recorded["byToken"], recorded),
        "notAnswerable": list(NOT_ANSWERABLE),
    }


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
    """The human view: the two sets, then the bound they open."""
    lines = ["policy diff over " + (doc["history"] or "(unidentified WAL)")]
    counts = ("  actions    %d recorded  %d newly allowed  %d newly denied  %d undecided"
              % (len(doc["moves"]), len(doc["newlyAllowed"]),
                 len(doc["newlyDenied"]), len(doc["undecided"])))
    lines.append(counts)
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
        lines.append("  UNDECIDED (a realm-scoped rule needs the composition):")
        for move in doc["undecided"]:
            lines.append("    %-18s %-24s" % (move["component"], move["token"]))
    if doc["unscoped"]:
        lines.append("")
        lines.append("  UNSCOPED (a recorded step with no capability scope, read "
                     "as no declared crossing):")
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
    for entry in doc["notAnswerable"]:
        lines.append("    cannot say %-24s %s" % (entry["axis"], entry["why"]))
    return "\n".join(lines)
