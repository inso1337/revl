"""The item-260 crossing ceiling of each capability an approval ticket asks
about (issue #1755, part 1).

An operator answering a class-(c) ticket, or minting a standing grant with
`uses=N`, needs the worst case in front of them: how many times can this call's
component cross the capability? `revl.cardinality` already answers that per
component and capability, with an honest `unbounded` where it cannot prove a
bound. This module reads that answer for the capabilities a ticket names and
shapes it for the ticket:

    {verdict: bounded | bounded-symbolic | unbounded, ceiling, reason}

`ceiling` is the proved count for `bounded`, the fuel expression for
`bounded-symbolic` (`"3 * config.max_steps"` when an iteration crosses more
than once), and None for `unbounded`. The ceiling is the component's, per
activation, as item 260 defines it, not a count for this one call.

Analysis only: nothing here changes what the gate decides. The refusal of an
`unbounded` capability is the policy's opt-in (`Policy.approvals_bounded`),
applied by the session.
"""

from __future__ import annotations

#: The worst verdict wins when a capability is crossed in several places.
_RANK = {"bounded": 0, "bounded-symbolic": 1, "unbounded": 2}


def ceilings(card: dict, reach: dict, tokens, externs: dict,
             carried) -> dict:
    """`{token: ceiling}` for each of `tokens` (the ticket's class-(c)
    capabilities), read from `card` (`revl.cardinality.cardinality(ir)`).

    `carried(component, key, method)` gives the tokens a service emission
    contributes (`ClassMap._carried_caps`), and `externs` the IR's extern
    declarations, so each crossing in `reach` is mapped from the token the
    ticket names to the key the cardinality report uses: the require key for a
    service emission, the extern name for a host extern."""
    wanted = set(tokens)
    found: dict = {}
    for crossing in reach.get("crossings") or ():
        for token, component, key in _keys(crossing, externs, carried):
            if token in wanted:
                # one row per (component, key): the report's count for a
                # component already totals every crossing site it has
                entry = ((card.get(component) or {}).get("per_capability")
                         or {}).get(key)
                found.setdefault(token, {})[(component, key)] = _shape(
                    entry, component, key)
    return {token: _worst(list((found.get(token) or {}).values()))
            for token in sorted(wanted)}


def _keys(crossing: dict, externs: dict, carried):
    """(ticket token, component, cardinality key) for one crossing."""
    component = crossing.get("component")
    kind = crossing.get("kind")
    if kind == "emission":
        key = crossing.get("key")
        for token in carried(component, key, crossing.get("method")):
            yield token, component, key
    elif kind == "extern":
        name = crossing.get("name")
        for token in (externs.get(name) or {}).get("capabilities") or [name]:
            yield token, component, name
    elif kind == "widening":
        yield crossing.get("capability"), component, crossing.get("capability")


def _shape(entry: dict | None, component: str, key: str) -> dict:
    if entry is None:
        # fail closed: a crossing the cardinality report has no row for is not
        # proved bounded, whatever the reason the row is missing
        return {"verdict": "unbounded", "ceiling": None,
                "reason": f"no crossing ceiling is recorded for `{key}` in "
                          f"component `{component}`"}
    kind = entry.get("kind")
    if kind == "bounded":
        bound = entry.get("bound")
        return {"verdict": "bounded", "ceiling": bound,
                "reason": f"`{component}` crosses `{key}` at most {bound} "
                          f"time(s) per activation (proved, item 260)"}
    if kind == "bounded-symbolic":
        expr, per_iter = entry.get("expr"), entry.get("per_iter") or 1
        ceiling = expr if per_iter == 1 else f"{per_iter} * {expr}"
        return {"verdict": "bounded-symbolic", "ceiling": ceiling,
                "reason": f"`{component}` crosses `{key}` {per_iter} time(s) "
                          f"per iteration of a loop fuelled by `{expr}` "
                          f"(proved, item 260)"}
    return {"verdict": "unbounded", "ceiling": None,
            "reason": entry.get("reason") or "not proved bounded (item 260)"}


def _worst(shapes: list | None) -> dict:
    if not shapes:
        return {"verdict": "unbounded", "ceiling": None,
                "reason": "no crossing of this capability is in the call's "
                          "reach closure, so no ceiling can be read for it"}
    worst = max(shapes, key=lambda s: _RANK[s["verdict"]])
    bounded = [s["ceiling"] for s in shapes if s["verdict"] == "bounded"]
    if worst["verdict"] == "bounded" and len(bounded) > 1:
        # two crossing sites of one capability, each proved: the sum bounds it
        return {**worst, "ceiling": sum(bounded),
                "reason": "; ".join(s["reason"] for s in shapes)}
    return worst


def first_unbounded(ceilings_by_token: dict) -> tuple | None:
    """`(token, reason)` of the first `unbounded` ceiling, or None."""
    for token in sorted(ceilings_by_token):
        entry = ceilings_by_token[token]
        if entry["verdict"] == "unbounded":
            return token, entry["reason"]
    return None
