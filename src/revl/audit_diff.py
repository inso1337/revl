"""`revl audit --diff` — the authority-drift gate.

This is the *authority* axis of the agent-gate story, deliberately distinct
from admission (which checks *correctness* — that running consumers stay
valid). Audit-diff answers a different question: between two generations of a
component, did the new one quietly WIDEN what it reaches outside the system?

The G8 boundary surface (`revl audit --json`) is the enumerable set of
boundary crossings — every emission call site and every reached host extern,
per component. A *crossing* here is one such reach:

    emit:<component>:<service.method>   an emission the component performs
    host:<component>:<extern-name>      host code the component reaches

Diffing two audits over that set gives three buckets:

    added      crossings present in NEW but not PREV  -> WIDENING (fails)
    removed    crossings present in PREV but not NEW  -> narrowing (safe)
    unchanged  crossings present in both

Adding authority is the dangerous direction, so **unacknowledged additions
fail** (nonzero exit). Removals and unchanged crossings always pass — giving
up authority never needs a gate. An intended widening is accepted with an
explicit ack (`--accept <crossing>` repeatable, or `--accept-all`); the ack
token is exactly the string printed after the `+` for each addition.
"""

from __future__ import annotations

from .distribute import distributability


def audit_report(ir: dict) -> dict:
    """Build the same dict `revl audit --json` emits, from a compiled ir.

    Reuses the one authoritative boundary computation (`_boundary`) rather
    than recomputing the surface a second, divergent way.
    """
    from .__main__ import _boundary  # noqa: PLC0415 — lazy, avoids import cycle

    boundary = _boundary(ir)
    manifest = ir.get("manifest") or {}
    declared_externs = [
        {"name": ext["name"], "class": ext.get("class"),
         "backends": sorted((ext.get("bodies") or {}).keys()),
         # item 373: carry the reach so the drift gate can read it. Absent unless
         # declared — a bare emission's audit entry is byte-identical to today's.
         **({"reach": ext["reach"]} if ext.get("reach") else {})}
        for ext in ir.get("externs") or []
    ]
    return {
        "manifest": manifest,
        "boundary": boundary,
        "externs": declared_externs,
        "distributability": distributability(ir),
    }


def crossings(audit: dict) -> set[str]:
    """The enumerable set of boundary crossings in an audit, as ack tokens.

    A crossing token is stable across generations for the same reach, so set
    difference is the whole diff. Four kinds, all drawn from the per-component
    G8 boundary table (the last two only when item 249 taint is in play):

        emit:<component>:<label>          an emission call site
        host:<component>:<name>           a reached host extern
        taint:<component>:<origin>        a value of <origin> reaches an emission here
        declassify:<component>:<origin>   an untrusted value of <origin> is declassified here

    The two taint tokens flow through `diff_crossings`/`evaluate` unchanged, so a
    newly-appearing `taint:` (web content newly routed into a send) or
    `declassify:` (a newly-added `endorse`) is a *widening* that fails the drift
    gate — the same mechanism that already catches "one more emission" now
    catching "one more declassification" (item 249, Decision 5).
    """
    out: set[str] = set()
    for component, stats in (audit.get("boundary") or {}).items():
        for label in stats.get("emissions") or []:
            out.add(f"emit:{component}:{label}")
        for extern in stats.get("externs") or []:
            out.add(f"host:{component}:{extern['name']}")
        taint = stats.get("taint") or {}
        for origin in taint.get("reaches") or []:
            out.add(f"taint:{component}:{origin}")
        for origin in taint.get("declassify") or []:
            out.add(f"declassify:{component}:{origin}")
    return out


def _reach_map(audit: dict) -> dict[str, dict | None]:
    """Extern name -> its reach dict (`{"kind","target"}`) or None (unconfined).

    The reach is a property of the EXTERN, not of a per-component crossing, so it
    is read from the audit's flat `externs` list rather than the boundary table.
    A bare emission has no `reach` key, which reads here as None = unconfined.
    """
    return {ext["name"]: ext.get("reach")
            for ext in (audit.get("externs") or [])}


def diff_reach(prev: dict, new: dict) -> dict:
    """Diff the REACH of every extern present in BOTH audits (item 373).

    The reach names what an irreversible crossing is bounded to. Adding/removing
    a crossing is already handled by `diff_crossings`; this catches the drift a
    crossing-set diff cannot see — an extern that STAYS but loosens its bound:

        confined(T) -> unconfined         WEAKENING (a removed bound)
        confined(a) -> confined(b)        WEAKENING (bound moved; b need not ⊇ a)
        unconfined  -> confined(T)        tightening (safe — a bound gained)
        unchanged                         stable

    A weakening is the dangerous direction — the same shape as a new crossing —
    so its token feeds `evaluate`'s `unacknowledged`. Only externs in both audits
    are compared: a newly-added confined extern is already flagged by its
    `host:` crossing, and a removed one gave up its authority entirely.
    """
    prev_reach = _reach_map(prev)
    new_reach = _reach_map(new)
    weakened: list[str] = []
    tightened: list[str] = []
    for name in prev_reach.keys() & new_reach.keys():
        before, after = prev_reach[name], new_reach[name]
        if before == after:
            continue
        if before is None:
            # unconfined -> confined: a bound was gained. Safe.
            tightened.append(f"reach-tightened:{name}")
        else:
            # confined -> unconfined, or confined -> a different bound. Both
            # loosen or move what the crossing is bounded to — reviewable.
            weakened.append(f"reach-weakened:{name}")
    return {"reach_weakened": sorted(weakened),
            "reach_tightened": sorted(tightened)}


def diff_crossings(prev: dict, new: dict) -> dict:
    """Compare two audits over their G8 boundary surfaces.

    Returns sorted `added` / `removed` / `unchanged` crossing-token lists, plus
    the reach drift (`reach_weakened` / `reach_tightened`, item 373).
    """
    prev_set = crossings(prev)
    new_set = crossings(new)
    return {
        "added": sorted(new_set - prev_set),
        "removed": sorted(prev_set - new_set),
        "unchanged": sorted(new_set & prev_set),
        **diff_reach(prev, new),
    }


def evaluate(prev: dict, new: dict, accepted: set[str] | None = None,
             accept_all: bool = False) -> dict:
    """The gate decision. `unacknowledged` are the additions that fail.

    exit-code contract: 0 iff `unacknowledged` is empty (a clean or fully
    acknowledged diff); nonzero otherwise.
    """
    accepted = accepted or set()
    delta = diff_crossings(prev, new)
    # item 373: a reach WEAKENING fails the gate the same way a new crossing
    # does — it is the authority axis loosening without a new token appearing.
    # Its `reach-weakened:<name>` token is acknowledged through the same
    # `--accept`/`--accept-all` path as an added crossing.
    gated = delta["added"] + delta["reach_weakened"]
    if accept_all:
        unacknowledged: list[str] = []
    else:
        unacknowledged = [c for c in gated if c not in accepted]
    return {
        "added": delta["added"],
        "removed": delta["removed"],
        "unchanged": delta["unchanged"],
        "reach_weakened": delta["reach_weakened"],
        "reach_tightened": delta["reach_tightened"],
        "acknowledged": sorted(c for c in gated
                               if accept_all or c in accepted),
        "unacknowledged": unacknowledged,
        "widened": bool(unacknowledged),
    }


def render(result: dict, prev_label: str) -> str:
    """Human-readable report of an `evaluate` result."""
    lines: list[str] = []
    added = result["added"]
    weakened = result.get("reach_weakened") or []
    tightened = result.get("reach_tightened") or []
    if not added and not result["removed"] and not weakened and not tightened:
        return (f"authority-drift: clean — the G8 boundary surface is "
                f"unchanged from {prev_label}.")

    acked = set(result["acknowledged"])
    if added:
        lines.append(
            f"authority-drift: {len(added)} new boundary crossing(s) added "
            f"since {prev_label}:")
        lines.append("")
        for crossing in added:
            mark = "  ~ " if crossing in acked else "  + "
            suffix = "  (acknowledged)" if crossing in acked else ""
            lines.append(f"{mark}{crossing}{suffix}")
        lines.append("")
        if any(c in result["unacknowledged"] for c in added):
            lines.append(
                "These WIDEN what the composition reaches outside the system.")
            lines.append(
                "Acknowledge an intended widening with --accept <crossing> "
                "(repeatable) or --accept-all.")
        else:
            lines.append("All additions acknowledged.")
    else:
        lines.append(f"authority-drift: no new crossings since {prev_label}.")

    # item 373: a reach weakening is a loosened bound on a crossing that STAYS —
    # confined -> unconfined, or a bound that moved. It fails the gate like an
    # addition, and is acknowledged through the same `--accept` token.
    if weakened:
        lines.append("")
        lines.append(
            f"reach WEAKENED (a crossing loosened what it is bounded to): "
            f"{len(weakened)}")
        for token in weakened:
            mark = "  ~ " if token in acked else "  + "
            suffix = "  (acknowledged)" if token in acked else ""
            lines.append(f"{mark}{token}{suffix}")
        if any(t in result["unacknowledged"] for t in weakened):
            lines.append(
                "A weakened reach is a widening — acknowledge with "
                "--accept <token> or --accept-all.")

    if tightened:
        lines.append("")
        lines.append(
            f"reach tightened (safe — a bound gained): {len(tightened)}")
        for token in tightened:
            lines.append(f"  - {token}")

    if result["removed"]:
        lines.append("")
        lines.append(
            f"narrowed (safe — authority given up): {len(result['removed'])}")
        for crossing in result["removed"]:
            lines.append(f"  - {crossing}")

    return "\n".join(lines)
