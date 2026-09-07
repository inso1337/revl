"""Restart reconciliation of declared liveness from durable world state
(roadmap item 477 follow-up 2, issue #624 — the `reconcileLivenessFromWorld`
slice the design note left as design-only).

WHY THIS EXISTS. On restart the in-memory expected-liveness map is GONE: it
described the world the crashed process believed in, not the one now running.
#622 wired a live silence observer, but a provider that went silent WHILE the
supervisor was down produced no live signal for anyone to observe — the observer
was not running. Reconciliation is the other half: rebuild what liveness the new
process may EXPECT from the durable world (the WAL the run held open, the E-Stop
latch item 443 arms, and a durable causal trace a prior run wrote), rather than
trusting a stale map or silently re-adopting a provider as freshly live.

THE ONE RULE. Reconciliation never invents authority and never re-adopts a
provider as live. The strongest thing durable evidence can say about a
liveness-managed provider after a crash is a NEGATIVE or an UNKNOWN:

  * ``expired``    — a prior process's own durable trace records this provider
    withdrawn with a ``LIVENESS_EXPIRED`` root. It was already dead by expiry
    before the crash; that is attested, not guessed.
  * ``halted``     — an armed E-Stop latch (item 443) covers the world. The
    operator stopped it; it is durably NOT live and the operator reconciles the
    halt (`revl recover --wal`), not this pass.
  * ``unresolved`` — everything else. Valid, stale, partial or contradictory
    world state all collapse to here: we do not KNOW the provider is live, so it
    must be RE-OBSERVED (the #622 observer, once the generation re-activates)
    before anything treats it as live. This is the conservative default and the
    ONLY thing an absent, empty, or unreadable WAL can produce.

There is deliberately no ``live`` verdict. "The crashed process recorded
activation-complete" means it BELIEVED the generation live — a belief that is
stale the instant the process dies, so it yields ``unresolved`` with that reason,
never a re-adoption.

SUPPORTED DURABLE EVIDENCE, and refusal. The evidence this reads is exactly:
the tier-agnostic WAL (:mod:`revl.wal`) — its header ``generation``, its
terminal ``activation-complete`` marker, and its steady-state residue records;
the E-Stop latch (:mod:`revl.estop`); and a durable causal trace
(:mod:`revl.why_runtime`). A WAL whose header version this reader cannot
understand, or that is mid-file corrupt, FAILS CLOSED: :func:`revl.wal.read_wal`
raises and reconciliation reports every managed provider ``unresolved`` with the
integrity error as the reason, rather than reading a possibly-forged world. Any
OTHER evidence shape is simply not consulted — reconciliation reports what it
could not resolve, it does not pretend an unsupported source told it something.

NOT OUR OWNERS. A component named in durable residue that is NOT a
liveness-managed provider of the composition being restarted is reported under
``foreign`` and left ALONE: reconciliation does not clean up, expire, or re-adopt
an owner it does not manage (issue #624 acceptance — "no cleanup of unrelated
owners").
"""

from __future__ import annotations

from . import estop, why_runtime


#: The single sentence reconciliation is allowed to claim, kept deliberately
#: narrow (mirrors :data:`revl.wal.WAL_GUARANTEE`'s posture). Written into every
#: report so a consumer cannot read more into a verdict than it states.
RECONCILE_GUARANTEE = (
    "reconciliation rebuilds only the EXPECTED liveness of the restarting "
    "composition's declared-ceiling providers from durable world evidence. It "
    "reports each as expired (durably attested dead), halted (operator E-Stop), "
    "or unresolved (must be re-observed before being treated as live). It never "
    "re-adopts a provider as freshly live and never touches an owner it does not "
    "manage."
)


def _managed_ceilings(ir: dict) -> dict[str, int]:
    """The declared-ceiling providers of the composition being restarted —
    the ONLY owners reconciliation speaks about. A component with no
    ``liveness_ceiling_ms`` is not liveness-managed and is never reported."""
    out: dict[str, int] = {}
    for comp in (ir or {}).get("components") or []:
        ceiling = comp.get("liveness_ceiling_ms")
        if ceiling is not None:
            out[comp["name"]] = ceiling
    return out


def _expired_from_trace(trace_events: list[dict]) -> dict[str, dict]:
    """Components a durable causal trace attests were withdrawn with a
    ``LIVENESS_EXPIRED`` root — durably dead by expiry BEFORE the crash. Read
    through the same :class:`why_runtime.Trace` the live runtime writes, so the
    evidence is the prior process's own recording, not a re-derivation."""
    expired: dict[str, dict] = {}
    trace = why_runtime.Trace(trace_events)
    for name in trace.components():
        chain = trace.cause_chain(name, prefer=why_runtime.WITHDRAW)
        if chain and chain[-1].cause.get("kind") == why_runtime.LIVENESS_EXPIRED:
            expired[name] = chain[-1].cause
    return expired


def _residue_components(wal: dict) -> set[str]:
    """Component names a WAL's steady-state / crossing residue records name as
    still 'out in the world' at the crash. Used only to surface FOREIGN owners
    (those not managed here) — never to expire or adopt one."""
    names: set[str] = set()
    for record in wal.get("records") or []:
        crossing = record.get("crossing") or {}
        name = record.get("component") or crossing.get("component")
        if name:
            names.add(name)
    return names


def reconcile_liveness_from_world(
    ir: dict,
    *,
    wal_path: str | None = None,
    latch_path: str | None = None,
    trace_events: list[dict] | None = None,
    trace_path: str | None = None,
) -> dict:
    """Rebuild the expected liveness of `ir`'s declared-ceiling providers from
    durable world state after a restart. Pure and side-effect free: it READS the
    world and returns a verdict; it withdraws nothing and adopts nothing.

    (The design note names this ``reconcileLivenessFromWorld``; this is its
    Python spelling.)

    Evidence, all optional — each absent source simply contributes nothing:

    * `wal_path` — a durable WAL (read through the tier-agnostic
      :func:`revl.wal.read_wal`, so a WAL any tier wrote is accepted). Its
      ``activation-complete`` marker distinguishes "the process believed the
      generation live" (stale — ``unresolved``) from "crashed mid-activation"
      (``unresolved``, possibly stuck). A missing file is a valid
      nothing-durable input. A version-unsupported or mid-file-corrupt WAL FAILS
      CLOSED (see module docstring).
    * `latch_path` — an E-Stop latch (:func:`revl.estop.read_latch`). An armed
      latch means the world was HALTED: every managed provider is ``halted``.
    * `trace_events` / `trace_path` — a durable causal trace. A provider it
      records withdrawn with a ``LIVENESS_EXPIRED`` root is ``expired``.

    Returns ``{verdict, generation, evidence, components, unresolved, expired,
    halted, foreign, guarantee}``. ``verdict`` is ``"clean"`` when nothing is
    unresolved (every managed provider is durably expired or halted, or there
    are none), ``"refused"`` when the WAL failed its integrity gate, and
    ``"unresolved"`` whenever at least one provider must be re-observed.
    """
    from .wal import WALIntegrityError, read_wal  # noqa: PLC0415 — lazy core

    managed = _managed_ceilings(ir)

    # -- gather durable evidence, fail-closed on a corrupt WAL -------------
    wal: dict | None = None
    wal_error: str | None = None
    generation = None
    if wal_path is not None:
        try:
            wal = read_wal(wal_path)
        except FileNotFoundError:
            wal = None  # a valid nothing-to-reconcile input
        except WALIntegrityError as exc:
            wal_error = str(exc)
        except OSError as exc:
            wal_error = f"cannot read WAL {wal_path}: {exc}"
        if wal is not None:
            generation = (wal.get("header") or {}).get("generation")

    halt = estop.read_latch(latch_path) if latch_path is not None else None

    if trace_events is None and trace_path is not None:
        try:
            trace_events = why_runtime.read_trace(trace_path)
        except (OSError, ValueError):
            trace_events = None  # an unreadable trace attests nothing
    expired_by_trace = _expired_from_trace(trace_events) if trace_events else {}

    evidence = {
        "wal": None if wal is None else {
            "path": wal_path,
            "generation": generation,
            "activationComplete": bool(wal.get("complete")),
            "torn": bool(wal.get("torn")),
            "records": len(wal.get("records") or []),
        },
        "walError": wal_error,
        "halt": halt,
        "trace": {"events": len(trace_events)} if trace_events else None,
    }

    # -- classify each MANAGED provider, conservatively --------------------
    components: list[dict] = []
    for name in sorted(managed):
        ceiling = managed[name]
        row = {"component": name, "ceilingMs": ceiling}
        if wal_error is not None:
            # the world we would reconcile against is unreadable; trust NOTHING
            # from it. The provider is unresolved, with the integrity error said
            # plainly — never read a possibly-forged world as if it were clean.
            row.update(status="unresolved",
                       reason=f"durable WAL failed its integrity gate, so no "
                              f"world evidence is trustworthy: {wal_error}")
        elif halt is not None:
            row.update(status="halted",
                       reason="an E-Stop latch is armed (item 443): the world "
                              "was HALTED, so this provider is durably NOT live; "
                              "the operator reconciles the halt, not this pass",
                       halt=halt)
        elif name in expired_by_trace:
            cause = expired_by_trace[name]
            row.update(status="expired",
                       reason="a durable causal trace attests this provider was "
                              "withdrawn with a LIVENESS_EXPIRED root before the "
                              "crash — dead by expiry, not guessed",
                       ceilingMsObserved=cause.get("ceilingMs"),
                       silentMs=cause.get("silentMs"))
        elif wal is None:
            row.update(status="unresolved",
                       reason="no durable evidence of this provider's liveness "
                              "survived the crash; it must be re-observed before "
                              "being treated as live")
        elif wal.get("complete"):
            row.update(status="unresolved",
                       reason="the crashed process recorded activation-complete "
                              "(it BELIEVED this provider live), but that belief "
                              "is stale across the crash — re-observe before "
                              "treating it as live; never re-adopt on the strength "
                              "of the old process's belief")
        else:
            row.update(status="unresolved",
                       reason="the WAL has no activation-complete marker: the "
                              "process crashed before the generation was durable, "
                              "so this provider may have been stuck mid-activation "
                              "— recovery rolls the shape back and liveness is "
                              "re-observed, never assumed")
        components.append(row)

    # -- FOREIGN owners: named in residue, not managed here — left ALONE ---
    foreign: list[str] = []
    if wal is not None:
        for name in sorted(_residue_components(wal)):
            if name not in managed:
                foreign.append(name)

    unresolved = [c["component"] for c in components if c["status"] == "unresolved"]
    expired = [c["component"] for c in components if c["status"] == "expired"]
    halted = [c["component"] for c in components if c["status"] == "halted"]

    if wal_error is not None:
        verdict = "refused"
    elif unresolved:
        verdict = "unresolved"
    else:
        verdict = "clean"

    return {
        "verdict": verdict,
        "generation": generation,
        "evidence": evidence,
        "components": components,
        "unresolved": unresolved,
        "expired": expired,
        "halted": halted,
        "foreign": foreign,
        "guarantee": RECONCILE_GUARANTEE,
    }


def render(report: dict) -> list[str]:
    """Human report lines for the reconciliation verdict."""
    lines: list[str] = []
    gen = report.get("generation")
    head = f"reconcile liveness from world: {report['verdict'].upper()}"
    if gen is not None:
        head += f" (generation {gen})"
    lines.append(head)
    if not report["components"]:
        lines.append("  no declared-ceiling providers in this composition — "
                     "nothing to reconcile")
    for row in report["components"]:
        lines.append(f"  @{row['component']} ({row['ceilingMs']}ms ceiling): "
                     f"{row['status'].upper()} — {row['reason']}")
    if report["foreign"]:
        lines.append("  foreign (named in residue, NOT managed here, left "
                     f"untouched): {', '.join(report['foreign'])}")
    lines.append(f"  guarantee: {report['guarantee']}")
    return lines
