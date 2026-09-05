"""`revl analyze` liveness: derive a Petri net from a composition's IR and search
it for reachable dead states (roadmap item 438).

This is the ONLY revl-aware half. The math is in :mod:`revl.petri`; here we
answer the three questions that make the net mean something for a composition,
and word the report. The design note is `docs/analyze-liveness.md`.

WHAT IS A PLACE (question 1). A `(key, realm)` provision -- G2's own unit of
disjointness (426 §1.1). A bare key would fuse two providers that G2 keeps apart
by realm (the multi-tenant shape) into one place and invent contention that does
not exist; a coeffect is finer than the composition graph resolves. So the place
is the pair, realm `<shared>` when unnamed. Provisions carry two arc kinds:

* an ordinary SERVICE is SHARED -- a consumer READS it (a test arc), so any
  number of consumers coexist and an acyclic (G3-clean) graph is monotone and
  always completes. This is why every current composition analyzes LIVE.
* a CONSUMABLE coeffect -- a single-consumer stream/subscription -- is a real
  token a consumer CONSUMES. Two consumers of a capacity-1 coeffect contend, and
  whichever activates first strands the other. That interleaving is exactly the
  gap item 130's pointwise rules 3.1/3.6 cannot see, and G3 does not model
  because it is not a cycle.

An activation is a fire-once transition (it consumes a per-component control
token seeded in the initial marking) that READS its shared injects, CONSUMES its
consumable coeffects, and PRODUCES its own provisions plus a `done` token. A
composition is LIVE when the only reachable quiescent markings have every control
token spent (every component activated); a reachable dead marking with a control
token still held is a DEADLOCK -- the component that never activated.

THE BOUND (question 2). The marking graph is searched by bounded BFS (a
state-count cap and a per-place token cap). A result found strictly within the
bound is exact; a result that HIT the bound is inconclusive and says so -- it
never reports "no deadlock" as a guarantee (item 418). Where the graph is too
large, P-semiflows certify structural boundedness so at least the search space
is known finite.

REFUSE OR WARN (question 3). `revl analyze` REPORTS; it is not wired into the
admission gate. A false positive that blocks a legal composition is worse for
adoption than the deadlock, and the false-positive rate is not yet measured over
a large corpus, so this stays report-only. It exits nonzero on a PROVEN dead
state so CI can consume it, and zero (with a note) on an inconclusive bound.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import petri

SHARED = "<shared>"


# ---------------------------------------------------------------- net derivation


def _realm_of(entry: dict, key: str) -> str:
    """The realm a component provides/consumes `key` in: its `isolate` map, or
    the shared realm when unnamed (mirrors lower.py's `_realm`)."""
    return (entry.get("isolate") or {}).get(key, SHARED)


def _place(key: str, realm: str) -> str:
    return f"prov:{key}@{realm}"


def _consumable_keys(entry: dict) -> set[str]:
    """Keys this component provides as a SINGLE-CONSUMER coeffect rather than a
    shared service. Read from an explicit `consumable` list on the manifest
    entry (a provider of a `Stream[T]`/subscription surface). The current
    frontend emits none -- cross-component consumable provisions are not yet
    expressible (item 438 "VERIFIED ABSENT"), so every real composition takes
    the shared-service path and analyzes LIVE. The field is the seam the
    derivation is READY for: the day a multicast bridge (item 130 §4.1) makes a
    provided stream shareable, its provider marks the key consumable and this
    analysis is the guard that keeps two consumers from silently starving."""
    return set(entry.get("consumable") or [])


def _capacity(entry: dict, key: str) -> int:
    """How many tokens a consumable provision offers. Single-consumer by
    default (1); a provider may declare a larger fixed fan-out."""
    caps = entry.get("consumableCapacity") or {}
    return int(caps.get(key, 1))


@dataclass
class Derivation:
    """The derived net plus the labels the report needs to speak in revl terms:
    which transition is which component, which place is which provision, and who
    provides each consumable (so a starved consumer can name its rival)."""

    net: petri.Net
    initial: petri.Marking
    activation_of: dict[str, str]          # transition id -> component name
    provision_label: dict[str, str]        # place id -> human "(key, realm)"
    consumable_consumers: dict[str, list[str]]  # place id -> consuming components
    provider_of: dict[str, str]            # place id -> providing component


def _manifest_entries(document: dict) -> list[dict]:
    manifest = document.get("manifest") or {}
    entries = manifest.get("components")
    if entries is None:
        # a bare frontend document: synthesize entries from `components`
        entries = [
            {
                "name": c.get("name"),
                "inject": sorted(c.get("requires") or []),
                "provides": sorted(c.get("provides") or []),
                "isolate": c.get("isolate") or {},
                "routes": c.get("routes") or {},
            }
            for c in (document.get("components") or [])
        ]
    return entries


def _routed_realms(entry: dict, key: str) -> list[str] | None:
    route = (entry.get("routes") or {}).get(key)
    if route and route.get("realms"):
        return list(route["realms"])
    return None


def derive(document: dict) -> Derivation:
    """Build the Petri net for one compiled composition IR document."""
    entries = _manifest_entries(document)
    provider_of: dict[str, str] = {}
    consumable_places: dict[str, tuple[str, int]] = {}  # place -> (provider, capacity)
    provision_label: dict[str, str] = {}

    def label(key: str, realm: str) -> str:
        return f"`{key}`" if realm == SHARED else f"`{key}` in realm `{realm}`"

    # pass 1: register every provision, marking the consumable ones and who owns
    # them, so pass 2 can wire consumers with the right arc kind.
    for entry in entries:
        consumable = _consumable_keys(entry)
        for key in entry.get("provides") or []:
            realm = _realm_of(entry, key)
            place = _place(key, realm)
            provider_of.setdefault(place, entry["name"])
            provision_label[place] = label(key, realm)
            if key in consumable:
                consumable_places[place] = (entry["name"], _capacity(entry, key))

    transitions: list[petri.Transition] = []
    initial: dict[str, int] = {}
    activation_of: dict[str, str] = {}
    consumers: dict[str, list[str]] = {p: [] for p in consumable_places}

    for entry in entries:
        name = entry["name"]
        tid = f"act:{name}"
        activation_of[tid] = name
        ctl = f"ctl:{name}"
        initial[ctl] = 1
        consume = {ctl: 1}
        read: dict[str, int] = {}
        for key in entry.get("inject") or []:
            realms = _routed_realms(entry, key) or [_realm_of(entry, key)]
            for realm in realms:
                place = _place(key, realm)
                if place in consumable_places:
                    consume[place] = consume.get(place, 0) + 1
                    consumers[place].append(name)
                else:
                    read[place] = 1
        produce = {f"done:{name}": 1}
        for key in entry.get("provides") or []:
            realm = _realm_of(entry, key)
            place = _place(key, realm)
            produce[place] = _capacity(entry, key) if place in consumable_places else 1
        transitions.append(petri.Transition(tid, consume=consume, produce=produce, read=read))

    net = petri.Net(transitions)

    # AMBIENT/EXTERNAL coeffects: a provision place that some activation reads or
    # consumes but NO component in the composition produces is host-injected --
    # `requires log` with no in-composition provider is the boot environment's
    # contract (item 350), not an unmet dependency (that is admission's job, not
    # liveness's). It ARRIVES, so seed it available in the initial marking; a
    # read of it is then always satisfied. Without this, every consumer of an
    # ambient capability would be a false positive. A consumable external place
    # is seeded to its declared capacity so a real fan-out contention can still
    # surface even when the source is a host stream.
    produced = {p for t in transitions for p in t.produce}
    for t in transitions:
        for place in (*t.read, *t.consume):
            if place.startswith("ctl:") or place in produced:
                continue
            cap = consumable_places[place][1] if place in consumable_places else 1
            initial[place] = max(initial.get(place, 0), cap)

    return Derivation(
        net=net,
        initial=initial,
        activation_of=activation_of,
        provision_label=provision_label,
        consumable_consumers=consumers,
        provider_of=provider_of,
    )


# ------------------------------------------------------------------- analysis


@dataclass
class Finding:
    """One deadlocked component named in a report."""

    component: str
    waits_on: list[str]          # provision labels it could not obtain
    contended_with: list[str]    # rival consumers that took the tokens


@dataclass
class Report:
    composition: str
    verdict: str                 # "live" | "deadlock" | "inconclusive"
    activations: int
    places: int
    explored: int
    findings: list[Finding]
    bounded: bool                # structural boundedness certified
    bound_hit: bool

    def exit_code(self) -> int:
        # report-only: only a PROVEN dead state is nonzero (question 3).
        return 1 if self.verdict == "deadlock" else 0


def _control_holders(dead: tuple) -> list[str]:
    """Components whose control token survives a dead marking: they never
    activated -- the deadlocked ones. A control place is `ctl:<component>`."""
    return sorted(place.split(":", 1)[1]
                  for place, count in dead
                  if count and place.startswith("ctl:"))


def analyze_document(
    document: dict,
    *,
    name: str = "composition",
    max_states: int = 20000,
    max_tokens: int = 64,
) -> Report:
    """Derive the net and search it. Returns a report; never raises on a
    deadlock (report-only)."""
    d = derive(document)
    result = petri.reachable(d.net, d.initial, max_states=max_states, max_tokens=max_tokens)

    # a deadlock is a reachable dead marking that still holds a control token:
    # some activation could not complete on that path.
    starved: dict[str, set[str]] = {}
    for dead in result.dead_markings:
        for comp in _control_holders(dead):
            starved.setdefault(comp, set())
            # which consumable provisions did this activation still need?
            marking = dict(dead)
            entry_tid = f"act:{comp}"
            t = next(tr for tr in d.net.transitions if tr.id == entry_tid)
            for place, weight in t.consume.items():
                if place.startswith("ctl:"):
                    continue
                if marking.get(place, 0) < weight:
                    starved[comp].add(place)

    findings: list[Finding] = []
    for comp in sorted(starved):
        waits = sorted(starved[comp])
        rivals: set[str] = set()
        for place in waits:
            for other in d.consumable_consumers.get(place, []):
                if other != comp:
                    rivals.add(other)
        findings.append(Finding(
            component=comp,
            waits_on=[d.provision_label.get(p, p) for p in waits],
            contended_with=sorted(rivals),
        ))

    bounded = petri.structurally_bounded(d.net)
    if findings:
        verdict = "deadlock"
    elif result.conclusive():
        verdict = "live"
    else:
        verdict = "inconclusive"

    return Report(
        composition=name,
        verdict=verdict,
        activations=len(d.activation_of),
        places=len(d.net.places),
        explored=len(result.markings),
        findings=findings,
        bounded=bounded,
        bound_hit=not result.conclusive(),
    )


# --------------------------------------------------------------------- render


def render(report: Report) -> list[str]:
    """Human report lines for `revl analyze`."""
    lines: list[str] = []
    head = (f"{report.composition}: {report.activations} activations, "
            f"{report.places} net places, {report.explored} markings explored")
    lines.append(head)
    if report.verdict == "live":
        lines.append("  live: no reachable dead state (marking graph fully explored)")
    elif report.verdict == "inconclusive":
        certified = "certified bounded via P-semiflows" if report.bounded \
            else "boundedness NOT certified"
        lines.append("  inconclusive: search hit the bound before the marking graph "
                     "was exhausted")
        lines.append(f"  no deadlock found WITHIN the bound ({certified}) -- this is a "
                     "partial result, not a liveness guarantee (item 418)")
    else:
        lines.append("  DEADLOCK: a reachable state strands one or more activations")
        for f in report.findings:
            waits = ", ".join(f.waits_on)
            line = f"    @{f.component} never activates: waits on {waits}"
            if f.contended_with:
                rivals = ", ".join(f"@{r}" for r in f.contended_with)
                line += f", a single-consumer coeffect taken by {rivals}"
            lines.append(line)
        lines.append("  (report-only: `revl analyze` names the cycle, it does not "
                     "refuse admission -- question 3)")
    return lines


def to_json(report: Report) -> dict:
    return {
        "composition": report.composition,
        "verdict": report.verdict,
        "activations": report.activations,
        "places": report.places,
        "markingsExplored": report.explored,
        "structurallyBounded": report.bounded,
        "boundHit": report.bound_hit,
        "findings": [
            {
                "component": f.component,
                "waitsOn": f.waits_on,
                "contendedWith": f.contended_with,
            }
            for f in report.findings
        ],
    }
