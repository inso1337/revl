"""What the gate would have decided if the agent had acted differently (issue
#1752).

A session driven through `revl_act` (issue #1708) keeps its actions in order,
and the approvals minted between them. This module takes that log, builds a
variant with one action replaced, inserted or dropped, and decides both arms
with the gate's own pure parts: the class map's `classify_call` and
`build_ticket`, and the recorded approvals replayed at the positions they were
minted, single-use per ticket hash as the live ledger spends them.

Nothing here runs a host body, enqueues a deferral or touches a session: the
inputs are the class map and plain data, and the output is a report. That is
the property `liveEffects: 0` states, and it holds by shape, since this module
imports no runtime tier and calls nothing that could fire.

The recorded arm is decided too, and compared with the receipts the live gate
wrote. Where the recompute does not reproduce a receipt (a standing grant, a
distilled rule or a quorum covered the call, none of which this slice
simulates, or the operation raised at run time), the report says so per step,
so a divergence between the arms is never read off an arm that did not
reproduce the recording.
"""

from __future__ import annotations

from collections import Counter

from .approval import _args_digest

#: Printed on every report: what a gate-level recompute cannot see.
BOUNDS = (
    "a call's reach is its static closure: a crossing listed here is one the "
    "call may make, and whether it fires depends on data this recompute does "
    "not have",
    "result values and data-dependent paths are not computed: nothing is run",
    "standing grants, distilled auto-approve rules and quorum votes are not "
    "simulated; a recorded step they covered is reported as not reproduced",
    "a refused (revoked) ticket and an operation that raised at run time are "
    "read from the recording, not recomputed",
    "an approval's ttl is not simulated: a recorded yes stays available until "
    "an identical call spends it",
)

#: The decision fields compared between the two arms.
COMPARED = ("class", "outcome", "ticket", "witnessed", "deferred", "residue")


class CounterfactualActError(ValueError):
    """The question cannot be asked of this log as posed."""


def variant(calls: list, approvals: dict, at: int, *, replace=None,
            insert=None, drop: bool = False) -> tuple[list, dict, str]:
    """The variant arm: the recorded calls with one substitution, and the
    recorded approvals moved to the positions they keep relative to the calls
    around them. `approvals` maps a position n (available before call n) to the
    ticket hashes minted there."""
    chosen = [k for k, v in (("replace", replace), ("insert", insert),
                             ("drop", drop or None)) if v is not None]
    if len(chosen) != 1:
        raise CounterfactualActError(
            "name exactly one substitution: `replace` or `insert` (an action "
            "`{key, method, args}`), or `drop: true`")
    kind = chosen[0]
    last = len(calls) if kind == "insert" else len(calls) - 1
    if isinstance(at, bool) or not isinstance(at, int) or not 0 <= at <= last:
        raise CounterfactualActError(
            f"`at` must name a recorded action, 0..{len(calls) - 1}"
            + (f" (or {len(calls)} to insert at the end)" if kind == "insert"
               else ""))
    action = replace if kind == "replace" else insert
    if action is not None:
        action = _action(action)
    if kind == "replace":
        out = calls[:at] + [action] + calls[at + 1:]
        moved = dict(approvals)
    elif kind == "insert":
        out = calls[:at] + [action] + calls[at:]
        moved = _shift(approvals, at, +1)
    else:
        out = calls[:at] + calls[at + 1:]
        moved = _shift(approvals, at, -1)
    return out, moved, kind


def _action(action) -> dict:
    if not isinstance(action, dict) or not action.get("key") \
            or not action.get("method"):
        raise CounterfactualActError(
            "a substituted action is `{key, method, args}` with `key` and "
            "`method` set")
    args = action.get("args") or []
    if not isinstance(args, list):
        raise CounterfactualActError("`args` is a list of positional arguments")
    return {"key": action["key"], "method": action["method"], "args": list(args)}


def _shift(approvals: dict, at: int, delta: int) -> dict:
    """An approval minted before the call at `at` stays before whatever now
    stands there; one minted later moves with the calls after `at`."""
    moved: dict = {}
    for position, hashes in approvals.items():
        target = position if position <= at else position + delta
        moved.setdefault(target, []).extend(hashes)
    return moved


def decide_arm(class_map, calls: list, approvals: dict, *,
               record_values: str) -> dict:
    """Decide every call of one arm, in order, against the approvals available
    before it. Returns the steps, the per-arm totals and the approvals left
    unspent."""
    available: Counter = Counter()
    steps = []
    for seq, call in enumerate(calls):
        available.update(approvals.get(seq, ()))
        steps.append(_decide(class_map, seq, call, available, record_values))
    available.update(h for n, hs in approvals.items() if n >= len(calls)
                     for h in hs)
    unused = sorted(h for h, n in available.items() for _ in range(n))
    return {"steps": steps, "totals": _totals(steps, unused)}


def _decide(class_map, seq: int, call: dict, available: Counter,
            record_values: str) -> dict:
    key, method, args = call["key"], call["method"], call.get("args") or []
    step = {"seq": seq, "key": key, "method": method,
            "argsDigest": _args_digest(args)}
    reach = class_map.classify_call(key, method)
    if reach is None:
        # the live gate refuses an unclassifiable call, and so does this one
        return {**step, "class": None, "outcome": "unresolved", "ticket": None,
                "witnessed": [], "deferred": [], "residue": []}
    klass = reach["class"]
    step.update({"class": klass, "ticket": None,
                 "witnessed": _witnessed(class_map, reach),
                 "deferred": _named(reach, "b"), "residue": []})
    if klass in (None, "a"):
        return {**step, "outcome": "executed"}
    if klass == "b":
        return {**step, "outcome": "deferred"}
    ticket = class_map.build_ticket(reach, args, record_values=record_values)
    if available[ticket["hash"]] > 0:
        available[ticket["hash"]] -= 1
        return {**step, "outcome": "executed", "approvalSpent": ticket["hash"],
                "residue": _named(reach, "c")}
    return {**step, "outcome": "ticket", "ticket": ticket["hash"]}


def _named(reach: dict, action_class: str) -> list:
    names = []
    for crossing in reach.get("crossings") or ():
        if crossing.get("actionClass") != action_class:
            continue
        names.append(crossing.get("name") or (
            f"{crossing.get('key')}.{crossing.get('method')}"
            if crossing.get("kind") == "emission"
            else crossing.get("capability")))
    return names


def _witnessed(class_map, reach: dict) -> list:
    externs = class_map.index.externs
    out = []
    for name in _named(reach, "a"):
        undo = (externs.get(name) or {}).get("undo") or {}
        callee = (undo.get("callee") or {}).get("name")
        out.append({"extern": name, "inverse": callee})
    return out


def _totals(steps: list, unused: list) -> dict:
    executed = [s for s in steps if s["outcome"] == "executed"]
    return {
        "tickets": [s["ticket"] for s in steps if s["outcome"] == "ticket"],
        "residue": [r for s in executed for r in s["residue"]],
        "witnessed": sum(len(s["witnessed"]) for s in executed),
        "deferred": [d for s in steps if s["outcome"] == "deferred"
                     for d in s["deferred"]],
        "unusedApprovals": unused,
    }


def reproduction(receipts: list, steps: list) -> list:
    """The recorded steps the recompute does not reproduce, each with why."""
    misses = []
    for receipt, step in zip(receipts, steps):
        recorded = receipt.get("outcome")
        if recorded == "raised" and step["outcome"] == "executed":
            continue        # the gate let it run; the raise is run-time data
        if receipt.get("class") == step["class"] and recorded == step["outcome"]:
            continue
        misses.append({"seq": step["seq"], "recorded": {
            "class": receipt.get("class"), "outcome": recorded},
            "recomputed": {"class": step["class"], "outcome": step["outcome"]},
            "why": _miss_reason(recorded, step)})
    return misses


def _miss_reason(recorded: str, step: dict) -> str:
    if recorded == "executed" and step["outcome"] == "ticket":
        return ("the recording executed this call under an authority this "
                "slice does not simulate (a standing grant, a distilled rule or "
                "a quorum)")
    if recorded == "refused":
        return "an operator revoked this ticket in the recording"
    return "the recompute decided differently from the live gate"


def align(kind: str, at: int, recorded: list, varied: list) -> list:
    """Pair each recorded step with the variant step that stands in its place."""
    pairs = []
    for i, step in enumerate(recorded):
        if i < at or kind == "replace":
            j = i
        elif kind == "drop":
            j = None if i == at else i - 1
        else:
            j = i + 1
        pairs.append((step, varied[j] if j is not None else None))
    if kind == "insert":
        pairs.insert(at, (None, varied[at]))
    return pairs


def divergence(kind: str, at: int, recorded: list, varied: list) -> dict:
    steps = []
    for old, new in align(kind, at, recorded, varied):
        changed = list(COMPARED) if old is None or new is None else [
            f for f in COMPARED if old.get(f) != new.get(f)]
        if changed:
            steps.append({"recorded": old, "variant": new, "changed": changed})
    first = steps[0] if steps else None
    position = (lambda s: (s["recorded"] or s["variant"])["seq"])
    # the recorded steps after the substitution: for an insert, the recorded
    # step at `at` itself now follows the inserted one
    after = at - 1 if kind == "insert" else at
    downstream = [s for s in steps if s["recorded"] is not None
                  and s["recorded"]["seq"] > after]
    return {"first": position(first) if first else None,
            "steps": steps, "downstream": downstream}


def delta(recorded: dict, varied: dict) -> dict:
    out = {}
    for field in ("tickets", "residue", "deferred", "unusedApprovals"):
        old, new = Counter(recorded[field]), Counter(varied[field])
        out[field] = {"added": sorted((new - old).elements()),
                      "removed": sorted((old - new).elements())}
    out["witnessed"] = varied["witnessed"] - recorded["witnessed"]
    return out


def report(class_map, calls: list, receipts: list, approvals: dict, at: int, *,
           record_values: str, replace=None, insert=None,
           drop: bool = False) -> dict:
    """The whole counterfactual: both arms, the reproduction check, the
    divergence and the bounds."""
    varied_calls, varied_approvals, kind = variant(
        calls, approvals, at, replace=replace, insert=insert, drop=drop)
    base = decide_arm(class_map, calls, approvals, record_values=record_values)
    alt = decide_arm(class_map, varied_calls, varied_approvals,
                     record_values=record_values)
    misses = reproduction(receipts, base["steps"])
    substituted = None
    if kind != "drop":
        action = varied_calls[at]
        substituted = {"key": action["key"], "method": action["method"],
                       "argsDigest": _args_digest(action["args"])}
    return {
        "at": at,
        "substitution": {"kind": kind, "action": substituted},
        "recorded": base,
        "variant": alt,
        "reproducesRecording": not misses,
        "notReproduced": misses,
        "divergence": divergence(kind, at, base["steps"], alt["steps"]),
        "delta": delta(base["totals"], alt["totals"]),
        "liveEffects": 0,
        "bounds": list(BOUNDS),
    }
