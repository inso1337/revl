"""`revl erase-report --realm <r>` — right-to-erasure evidence from the type
system (roadmap item 29).

Nobody generates erasure evidence from a compiler today. Every ingredient
already exists in this toolchain and works on its own; this module is a
*report generator* that composes three of them into one auditor-facing
artifact scoped to a single realm:

  1. IN-PROCESS STATE GONE — the runtime no-residue proof (R4). Booting the
     composition on the cordis runtime and tearing it down proves the
     registry, provisions, effect disposables and event listeners all return
     to baseline (`revl.mcp.session.Session.unload`). The realm's components
     are part of that teardown, and the provisions they held (enumerated
     statically) are the in-process state the proof erases.

  2. BOUNDARY CROSSINGS, COMPENSATED vs BARE — every emission call site and
     reached host extern the realm's components make, read from the G8
     boundary surface (`revl.query.Composition`), each tagged as compensated
     (a `compensate` clause is attached) or bare (nothing was done about it).

  3. OTHER REALMS PROVABLY UNTOUCHED — the withdrawal cascade (`revl.query.
     withdrawal`, EXACT precision) of the realm's components. G2 makes each
     `(key, realm)` provision unique, so tearing down realm `r` cannot orphan
     a consumer in another realm; the `survivors` set is the proof, and this
     report checks that every component outside the realm is a survivor.

HONEST SCOPE (paper §6.1, compensation is NOT inversion). The report states
its own scope in its header. A `compensate` clause is a second boundary
crossing chosen to offset the first; it does not un-issue the first. Anything
downstream that already observed a crossing — a replica, a trigger, a webhook,
a human — has already observed it. This report ENUMERATES that exposure so an
auditor can see exactly what left the system and whether anything was done
about it. It does NOT, and cannot, undo it. The state-gone proof is about
in-process runtime state only; it says nothing about data that already
crossed the boundary.

This module is READ-ONLY on the machinery it composes: it consumes the
outputs of `query`, `audit_diff` and `session` and adds only the realm-scoped
composition and its rendering. It reuses `interchange`'s versioned-document
discipline for the `--json` form.
"""

from __future__ import annotations

from .lower import SHARED_REALM
from .query import Composition, classify_compensation, withdrawal

# Report identity — a versioned, self-describing artifact, in the spirit of
# `interchange.stamp`. Bump MINOR for an additive change, MAJOR for a breaking
# one (a removed or re-shaped member).
ERASE_REPORT_VERSION = "1.0"
ERASE_REPORT_KIND = "revl.erase-report"

# The header the report states about itself. This is load-bearing prose: it is
# the difference between "we deleted the data" (false) and "we can prove the
# in-process state is gone and here is exactly what already left the system"
# (what this artifact actually establishes).
HONEST_SCOPE = {
    "title": "What this report proves — and what it does not",
    "proves": [
        "in-process state gone: booting and tearing down the composition on "
        "the runtime returns the registry, provisions, effect disposables and "
        "event listeners to baseline (R4, no residue). The realm's components "
        "are torn down as part of that proof.",
        "boundary crossings enumerated: every emission and reached host extern "
        "the realm's components make is listed, each marked compensated or "
        "bare.",
        "other realms untouched: withdrawing the realm's components cannot "
        "orphan a consumer in another realm (G2 makes each `(key, realm)` "
        "provision unique); the `survivors` set is the exact proof.",
    ],
    "doesNotProve": [
        "compensation is NOT inversion (paper §6.1). A `compensate` clause is "
        "a second boundary crossing chosen to offset the first; it does not "
        "un-issue it. This report ENUMERATES the exposure — it does not undo "
        "it.",
        "external erasure: data that already crossed the boundary (a replica, "
        "a downstream trigger, a webhook, a human that observed a crossing) is "
        "outside this system and outside this proof. The state-gone proof is "
        "about in-process runtime state only.",
        "a bare crossing left the system with nothing done about it; the "
        "report lists it precisely so it can be handled out of band.",
    ],
    "reference": "paper §6.1; docs/replay.md §4.2; docs/erase-report.md",
}


# --------------------------------------------------------------- realm model

def _isolated_realms(entry: dict) -> set[str]:
    """The set of realms a component isolates any of its keys into. The empty
    `SHARED_REALM` is deliberately excluded — a component that isolates nothing
    lives in the shared realm and is not a member of any named realm."""
    realms = set((entry.get("isolate") or {}).values())
    realms.discard(SHARED_REALM)
    return realms


def realms_of(ir: dict) -> list[str]:
    """Every named realm the composition declares, sorted."""
    index = Composition(ir)
    found: set[str] = set()
    for entry in index.entries.values():
        found |= _isolated_realms(entry)
    return sorted(found)


def _members(index: Composition, realm: str) -> list[str]:
    """Components with at least one key isolated into `realm`, in load order."""
    members = [name for name, entry in index.entries.items()
               if realm in _isolated_realms(entry)]
    order = {name: i for i, name in enumerate(index.load_order)}
    return sorted(members, key=lambda n: (order.get(n, len(order)), n))


# --------------------------------------------------------------- sections

def _in_realm_keys(index: Composition, component: str, realm: str) -> list[str]:
    """The provision/injection keys `component` isolates into `realm`."""
    entry = index.entries.get(component) or {}
    return sorted(k for k in (entry.get("isolate") or {})
                  if (entry.get("isolate") or {}).get(k) == realm)


def _provisions(index: Composition, members: list[str], realm: str) -> list[dict]:
    """The provisions the realm's members serve into this realm — the
    in-process state an erasure eliminates."""
    out = []
    for name in members:
        entry = index.entries.get(name) or {}
        for key in entry.get("provides") or []:
            if index.realm(name, key) == realm:
                out.append({"key": key, "realm": realm, "provider": name})
    return sorted(out, key=lambda p: (p["key"], p["provider"]))


def _is_network_cap(cap: object) -> bool:
    """A capability token names the network boundary iff it is `net.*` — the
    scope `import openapi` gives a compensate-grade endpoint (item 254 §4,
    `import_openapi._net_cap`). The `*` first-class widening is deliberately NOT
    network: an unnameable boundary is not proven to be an API crossing."""
    return isinstance(cap, str) and cap.startswith("net.")


def _is_network(crossing: dict) -> bool:
    return any(_is_network_cap(c) for c in (crossing.get("capabilities") or []))


def _network_boundary(all_cross: list[dict], witnessed: list[dict]) -> dict:
    """Item 254 / item 248 measurement, extended to the wire. Roll the crossings
    whose capability scope is a `net.*` cap into the one number item 254 promises:
    the fraction of the realm's API traffic that became witnessed (revertible) or
    compensable (an offset attached), the honest remainder left bare.

    A witnessed[net.*] inverse is host-restorable and counts as the strongest end
    (revertible); a compensate-bearing net emission counts as compensable; a bare
    net emission is the residue. Compensation is not inversion (§6.1): a
    compensable crossing still left the system — this counts *what was done about
    it*, not whether observation was undone."""
    net_irreversible = [c for c in all_cross if _is_network(c)]
    net_witnessed = [c for c in witnessed if _is_network(c)]
    compensated = [c for c in net_irreversible if c["compensated"]]
    bare = [c for c in net_irreversible if not c["compensated"]]
    total = len(net_irreversible) + len(net_witnessed)
    compensable = len(compensated) + len(net_witnessed)
    fraction = round(compensable / total, 4) if total else None
    return {
        "total": total,
        "witnessedCount": len(net_witnessed),
        "compensatedCount": len(compensated),
        "compensableCount": compensable,
        "bareCount": len(bare),
        "compensableFraction": fraction,
        "compensableTokens": sorted(
            c["token"] for c in compensated + net_witnessed if c.get("token")),
        "bareTokens": sorted(c["token"] for c in bare if c.get("token")),
        "note": "the fraction of this realm's network (API) boundary crossings "
                "that became witnessed (revertible) or compensable (an offset "
                "attached); the remainder is bare emission. Compensation is not "
                "inversion (§6.1) — a compensable crossing still left the system, "
                "it is not un-issued.",
    }


def _crossings(index: Composition, members: list[str],
               residue: list | None = None) -> dict:
    """Aggregate the G8 boundary surface of the realm's members: every
    emission and reached host extern, each tagged bare / compensated /
    unresolved (item 247 gap 2, docs/design/247-compensate.md Decision 2).

    Read straight from `query.Composition`'s per-scope facts — the same
    surface `revl audit` prints, so this can never disagree with it.

    `residue` is the runtime `compensation-residue` from an abort / `revl
    recover`. When given, a compensated crossing whose offset did not land is
    lifted into the third state `unresolved`; when omitted the report is the
    static two-state (bare/compensated) surface, byte-identical to before."""
    emissions: list[dict] = []
    externs: list[dict] = []
    witnessed: list[dict] = []
    widenings: list[dict] = []
    seen_emit: set = set()
    seen_host: set = set()
    seen_witnessed: set = set()
    seen_widen: set = set()
    for name in members:
        for scope_id in index.scopes_of.get(name, []):
            scope = index.scopes[scope_id]
            facts = scope["facts"]
            # the `*` first-class-value widening, off the SAME detection
            # `approval.ClassMap` raises a class-(c) crossing for
            # (`Composition.value_widens`, item 414). An emitting callable
            # handed on as a value escapes this scope and may be dispatched by
            # whoever receives it, reaching a boundary no `emission[...]` list
            # can name (`*`). Without this the erase report was a second class
            # fold blind to the widening, so an emission reached through a
            # first-class dispatched callable was omitted while the report
            # claimed every emission is listed (a false-safe). One `*` crossing
            # per component, matching the audit surface's own `*` entry.
            if index.value_widens(scope["nodes"]) and name not in seen_widen:
                seen_widen.add(name)
                widenings.append({
                    "component": name, "scope": scope["kind"],
                    "capability": "*", "actionClass": "c",
                    # a `*` widening carries no compile-time compensate clause
                    # and can never be proven reversible; bare by construction.
                    "compensated": False,
                    "token": f"widen:{name}:*",
                    "note": "an emitting callable is handed on as a value; the "
                            "boundary it may reach cannot be named (`*`), so it "
                            "cannot be proven reversible",
                })
            for fact in facts["emissions"]:
                mark = (name, fact["key"], fact["method"])
                if mark in seen_emit:
                    continue
                seen_emit.add(mark)
                emissions.append({
                    "component": name, "scope": scope["kind"],
                    "key": fact["key"], "method": fact["method"],
                    "service": fact.get("service"),
                    "label": f"{fact['key']}.{fact['method']}",
                    "compensated": bool(fact["compensated"]),
                    # a service-op emission fires at the call — class (c) (item
                    # 245, Decision 2); deferral is an extern-declaration property,
                    # not spellable on a service method.
                    "actionClass": "c",
                    # item 254: the capability the crossing is scoped to, so the
                    # network-boundary fold (§4) can tell an API crossing from an
                    # in-process one. A service-op emission is scoped to its
                    # service, never a `net.*` cap.
                    "capabilities": [fact["service"]] if fact.get("service") else [],
                    "token": f"emit:{name}:{fact['key']}.{fact['method']}",
                })
            for fact in facts["externs"]:
                if not fact.get("emission"):
                    # item 246 (closing the noted gap): a witnessed extern crosses
                    # the boundary too (item 243), but it is REVERTIBLE by its
                    # registered inverse — class (a), auto-approved silently. It is
                    # kept in its OWN bucket, tagged actionClass "a", and never
                    # folded into the bare/compensated residue totals, which count
                    # only irreversible crossings. Other non-emission host code
                    # (pure/acquire) is not a boundary crossing at all.
                    if fact.get("class") == "witnessed":
                        mark = (name, fact["name"])
                        if mark not in seen_witnessed:
                            seen_witnessed.add(mark)
                            wentry = index.externs.get(fact["name"]) or {}
                            witnessed.append({
                                "component": name, "scope": scope["kind"],
                                "name": fact["name"], "class": "witnessed",
                                "actionClass": "a", "revertible": True,
                                # item 254: caps so a witnessed[net.*] inverse is
                                # counted as the compensable-or-better end of the
                                # network boundary.
                                "capabilities": sorted(
                                    wentry.get("capabilities") or []),
                                "token": f"witnessed:{name}:{fact['name']}"})
                    continue
                mark = (name, fact["name"])
                if mark in seen_host:
                    continue
                seen_host.add(mark)
                # item 254: an emission EXTERN may OWN its reversal through an
                # extern-declared `compensate` slot (the shape `import openapi`
                # emits for a compensate-grade endpoint, wired to fire at teardown
                # by commit 4413fc8). Read that clause off the extern IR entry —
                # the per-scope fact does not carry it — so a compensate-bearing
                # network emission is tagged `compensated`, not misreported bare.
                ext_entry = index.externs.get(fact["name"]) or {}
                externs.append({
                    "component": name, "scope": scope["kind"],
                    "name": fact["name"], "class": fact.get("class"),
                    # item 245, Decision 2: the (a)/(b)/(c) action class off the
                    # checked classification. A deferred emission is class (b);
                    # any other reached emission extern is class (c).
                    "actionClass": "b" if fact.get("deferred") else "c",
                    "backends": fact.get("backends") or [],
                    # item 254: the extern's declared capability scope, so a
                    # `net.*` emission extern is recognised at the network boundary.
                    "capabilities": sorted(ext_entry.get("capabilities") or []),
                    # compensated iff the extern DECLARES a `compensate` slot (an
                    # offset attached, item 247). Absent = bare, as before.
                    "compensated": ext_entry.get("compensate") is not None,
                    "token": f"host:{name}:{fact['name']}",
                })
    emissions.sort(key=lambda e: (e["component"], e["label"]))
    externs.sort(key=lambda e: (e["component"], e["name"]))
    witnessed.sort(key=lambda e: (e["component"], e["name"]))
    widenings.sort(key=lambda e: (e["component"], e["scope"]))
    # a `*` widening is an irreversible (class-(c)) crossing, so it is folded
    # into the bare/compensated totals alongside emissions and host externs,
    # unlike the class-(a) `witnessed` bucket, which stays separate. It is
    # always bare (nothing can compensate an unnameable boundary).
    all_cross = emissions + externs + widenings
    # item 247 gap 2: overlay the runtime residue to split the compensated
    # crossings into those that landed and those left unresolved. With no
    # residue this leaves every compensated crossing compensated and the
    # unresolved set empty — the pre-247 two-state surface, byte-identical.
    partition = classify_compensation(all_cross, residue)
    bare = [c for c in all_cross if not c["compensated"]]
    compensated = [c for c in all_cross if c["compensated"]]
    unresolved = partition["unresolved"]
    unresolved_tokens = sorted(
        c["token"] for c in unresolved if c.get("token"))
    network = _network_boundary(all_cross, witnessed)
    return {
        "emissions": emissions,
        "externs": externs,
        # item 246: witnessed (class-(a)) crossings, additive and separate from
        # the irreversible totals — a reader that wants the auto-approve action
        # class off the aggregation finds it here, tagged actionClass "a".
        "witnessed": witnessed,
        # item 414: the `*` first-class-value widenings (class (c), capability
        # `*`), enumerated so an emission reached through a first-class
        # dispatched callable is no longer invisible to the completeness claim.
        # Folded into the totals below (they are irreversible), and also listed
        # here so a reader can find the widening crossings on their own. Empty
        # (and the totals unchanged) for a realm that widens nothing.
        "widenings": widenings,
        "total": len(all_cross),
        # `compensatedCount` counts crossings with an offset ATTACHED (the
        # static compile-time judgment), unchanged for back-compat. The runtime
        # overlay of which attached offsets did NOT land is `unresolvedCount`
        # below — additive, so the static report is byte-identical without it.
        "compensatedCount": len(compensated),
        "bareCount": len(bare),
        "bareTokens": sorted(c["token"] for c in bare),
        # item 247 gap 2: the third state. Empty (and unresolvedCount 0) when no
        # residue was supplied, so the static report is unchanged.
        "unresolved": unresolved,
        "unresolvedCount": len(unresolved),
        "unresolvedTokens": unresolved_tokens,
        # item 254 (item 248's measurement, extended to the network boundary):
        # of the crossings scoped to a `net.*` capability — the realm's API
        # traffic — the fraction that became WITNESSED (revertible) or
        # COMPENSABLE (an offset attached), with the bare-emission remainder
        # named. Empty (`total` 0, `compensableFraction` None) for a realm that
        # touches no network boundary, so a network-free report is byte-identical
        # but for this additive member.
        "networkBoundary": network,
        "note": "a crossing is bare when nothing was done about it, "
                "compensated when an offset landed, unresolved when an offset "
                "was owed but did not land. Compensation is not inversion "
                "(paper §6.1): a compensated crossing still left the system, "
                "and an unresolved one is still out there.",
    }


def _others_untouched(ir: dict, index: Composition, members: list[str],
                      realm: str) -> dict:
    """Withdraw the realm's components and read the exact fallout. `survivors`
    (EXACT, from `query.withdrawal`) is the proof that no component outside the
    realm loses a provision — G2 makes cross-realm orphaning impossible."""
    member_set = set(members)
    gone: set = set(members)
    per_member = []
    for name in members:
        w = withdrawal(ir, name)
        cascade = [c["component"] for c in w["cascade"]]
        gone.update(cascade)
        per_member.append({
            "component": name, "provides": w["provides"],
            "cascade": cascade, "survivors": w["survivors"],
        })

    outside = sorted(set(index.entries) - member_set)
    # a breach is a component OUTSIDE the realm that erasing the realm breaks.
    # Under G2 this is provably empty for realm-isolated provisions.
    breached = sorted(gone - member_set)
    survivors = sorted(set(index.entries) - gone)

    # realm of every other component, so the auditor sees which realms the
    # survivors belong to.
    others_by_realm: dict[str, list[str]] = {}
    for name in outside:
        for r in sorted(_isolated_realms(index.entries[name])) or [SHARED_REALM]:
            others_by_realm.setdefault(r, []).append(name)

    return {
        "withdrawnComponents": members,
        "survivors": survivors,
        "outsideRealm": outside,
        "breached": breached,
        "untouched": not breached,
        "otherRealms": {r: names for r, names in sorted(others_by_realm.items())
                        if r != realm},
        "perMember": per_member,
        "guarantee": "EXACT — G2 makes each `(key, realm)` provision unique, so "
                     "a realm's components have no consumers in another realm; "
                     "G3 makes the cascade terminate. `survivors` is the exact "
                     "set that keeps every provision.",
    }


def _prove_no_residue(ir: dict) -> dict:
    """Boot the composition on the runtime and tear it down, returning the R4
    no-residue proof. Reuses `Session.load`/`Session.unload` verbatim — the
    one authoritative teardown proof, not a re-derived one.

    When the cordis runtime is not installed this returns a `proven: None`
    stub with the reason, so the static sections still render; the runtime
    proof is the one section that needs the backend."""
    from .mcp.session import Session, SessionError  # noqa: PLC0415 — lazy: cordis

    session = Session()
    try:
        session.load(ir)
    except SessionError as error:
        return {"available": False, "proven": None, "reason": str(error)}
    try:
        result = session.unload()
    except SessionError as error:  # pragma: no cover — load succeeded
        return {"available": False, "proven": None, "reason": str(error)}
    return {
        "available": True,
        "proven": bool(result["noResidue"]),
        "checks": result["checks"],
        "detail": result["detail"],
        "note": "R4: after teardown the registry, provisions, effect "
                "disposables and event listeners are all back to baseline. "
                "This is in-process state only — it does not speak to data "
                "that already crossed the boundary.",
    }


# --------------------------------------------------------------- entry point

def build_report(ir: dict, realm: str, *, prove_residue: bool = True,
                 compensation_residue: list | None = None) -> dict:
    """The composed, versioned erase-report for one realm.

    `prove_residue=False` skips the runtime section (for a pure static report
    or where the cordis runtime is unavailable).

    `compensation_residue` (item 247 gap 2) is the runtime `compensation-
    residue` from an abort / `revl recover`. When given, the boundary-crossings
    section reports the third audit state `unresolved` for any compensated
    crossing whose best-effort offset did not land; when omitted the crossings
    section is the static two-state (bare/compensated) surface, unchanged."""
    index = Composition(ir)
    known = realms_of(ir)
    if realm not in known:
        return {
            "ok": False, "kind": ERASE_REPORT_KIND,
            "schema_version": ERASE_REPORT_VERSION,
            "error": f"unknown realm: {realm!r}",
            "knownRealms": known,
        }

    members = _members(index, realm)
    residue = _prove_no_residue(ir) if prove_residue else {
        "available": False, "proven": None, "reason": "runtime proof skipped"}
    provisions = _provisions(index, members, realm)

    components = []
    for name in members:
        entry = index.entries.get(name) or {}
        components.append({
            "component": name,
            "keys": _in_realm_keys(index, name, realm),
            "provides": sorted(entry.get("provides") or []),
            "injects": sorted(entry.get("inject") or []),
        })

    state_gone = {
        "provisionsErased": provisions,
        "noResidueProof": residue,
        # the whole realm's in-process state is gone iff the teardown proof
        # holds (or, if the runtime is unavailable, this is unproven).
        "proven": residue.get("proven"),
    }
    crossings = _crossings(index, members, compensation_residue)
    others = _others_untouched(ir, index, members, realm)

    return {
        "ok": True,
        "kind": ERASE_REPORT_KIND,
        "schema_version": ERASE_REPORT_VERSION,
        "realm": realm,
        "knownRealms": known,
        "honestScope": HONEST_SCOPE,
        "components": components,
        "inProcessStateGone": state_gone,
        "boundaryCrossings": crossings,
        "otherRealmsUntouched": others,
        "summary": {
            "components": len(members),
            "provisionsErased": len(provisions),
            "boundaryCrossings": crossings["total"],
            "bareCrossings": crossings["bareCount"],
            "compensatedCrossings": crossings["compensatedCount"],
            "unresolvedCrossings": crossings["unresolvedCount"],
            # item 254: the network-boundary headline (item 248, extended). 0 /
            # None for a realm that touches no `net.*` cap.
            "networkCrossings": crossings["networkBoundary"]["total"],
            "networkCompensableFraction":
                crossings["networkBoundary"]["compensableFraction"],
            "stateGoneProven": residue.get("proven"),
            "otherRealmsUntouched": others["untouched"],
        },
    }


# --------------------------------------------------------------- rendering

def render(report: dict) -> str:
    """Human rendering. The structured report is the product; this is the
    auditor's readable view — the header states scope first, every claim
    carries its own precision."""
    if not report.get("ok"):
        lines = [f"error: {report.get('error')}"]
        if report.get("knownRealms"):
            lines.append("  known realms: " + ", ".join(report["knownRealms"]))
        return "\n".join(lines)

    realm = report["realm"]
    scope = report["honestScope"]
    out = [
        f"REALM ERASURE REPORT — realm `{realm}`",
        f"  {report['kind']} v{report['schema_version']}",
        "",
        f"  {scope['title']}",
        "  PROVES:",
    ]
    out += [f"    + {line}" for line in scope["proves"]]
    out.append("  DOES NOT PROVE:")
    out += [f"    - {line}" for line in scope["doesNotProve"]]
    out.append(f"    ({scope['reference']})")

    out.append("")
    out.append("  realm components: "
               + ", ".join(c["component"] for c in report["components"]))

    # 1. state gone
    state = report["inProcessStateGone"]
    residue = state["noResidueProof"]
    out.append("")
    out.append("  [1] IN-PROCESS STATE GONE")
    prov = state["provisionsErased"]
    out.append("      provisions erased: " + (", ".join(
        f"{p['key']}@{p['realm']} (was {p['provider']})" for p in prov) or "none"))
    if residue.get("available"):
        verdict = "PROVEN — no residue" if residue["proven"] else "FAILED — residue left"
        out.append(f"      runtime teardown (R4): {verdict}")
        for name, ok in residue["checks"].items():
            out.append(f"        {'ok ' if ok else 'FAIL'} {name}")
    else:
        out.append(f"      runtime teardown (R4): NOT RUN — {residue.get('reason')}")
    out.append(f"      note: {residue.get('note', '')}".rstrip())

    # 2. crossings
    cross = report["boundaryCrossings"]
    unresolved_n = cross.get("unresolvedCount", 0)
    out.append("")
    header = (f"  [2] BOUNDARY CROSSINGS — {cross['total']} "
              f"({cross['compensatedCount']} compensated, {cross['bareCount']} bare")
    header += f", {unresolved_n} UNRESOLVED)" if unresolved_n else ")"
    out.append(header)
    if not cross["emissions"] and not cross["externs"] \
            and not cross.get("widenings"):
        out.append("      none — this realm made no irreversible boundary "
                   "crossing (fully revertible, G8)")
    for c in cross["emissions"]:
        tag = "[compensated]" if c["compensated"] else "[BARE]"
        out.append(f"      {tag:<14} {c['component']}  emit {c['label']}")
    for c in cross["externs"]:
        # item 254: an emission extern that owns a `compensate` slot is
        # compensated, not bare (the network compensate-grade case).
        tag = "[compensated]" if c["compensated"] else "[BARE]"
        out.append(f"      {tag:<14} {c['component']}  host {c['name']}()")
    # item 414: a `*` widening, an emitting callable escaping in value
    # position, reaching a boundary that cannot be named.
    for c in cross.get("widenings") or []:
        out.append(f"      {'[BARE]':<14} {c['component']}  widen `*` "
                   "(first-class emitting callable escapes)")
    # item 247 gap 2: the unresolved residue, named so an operator sees exactly
    # which owed offset is still out in the world.
    for c in cross.get("unresolved") or []:
        rec = c.get("residue") or {}
        err = (rec.get("error") or {}).get("message", "")
        label = c.get("label") or c.get("method") or rec.get("method") or "?"
        comp = c.get("component") or rec.get("component") or "?"
        out.append(f"      {'[UNRESOLVED]':<14} {comp}  offset {label}"
                   + (f"  — {err}" if err else ""))
    # item 254: the network-boundary headline, printed only when the realm
    # actually touches a `net.*` cap so a network-free report is unchanged.
    net = cross.get("networkBoundary") or {}
    if net.get("total"):
        pct = f"{net['compensableFraction'] * 100:.1f}%"
        out.append(
            f"      network boundary: {net['compensableCount']} of "
            f"{net['total']} API crossing(s) witnessed/compensable ({pct}) — "
            f"{net['witnessedCount']} witnessed, {net['compensatedCount']} "
            f"compensated, {net['bareCount']} bare")
    out.append(f"      note: {cross['note']}")

    # 3. others untouched
    others = report["otherRealmsUntouched"]
    out.append("")
    verdict = ("PROVEN untouched" if others["untouched"]
               else f"BREACHED — {', '.join(others['breached'])}")
    out.append(f"  [3] OTHER REALMS UNTOUCHED — {verdict}")
    out.append("      withdrawing: " + ", ".join(others["withdrawnComponents"]))
    out.append("      survivors (keep every provision): "
               + (", ".join(others["survivors"]) or "none"))
    for r, names in others["otherRealms"].items():
        label = r or "(shared)"
        out.append(f"        realm `{label}`: {', '.join(names)}")
    out.append(f"      {others['guarantee']}")

    return "\n".join(out)
