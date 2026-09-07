"""`revl goal audit` — the blind-spot report (roadmap item 441 / issue #120, S2).

441 §5.2 answers the item's hardest question — "the criteria are drafted by the
agent, what does the operator review?" — with "not the criteria, the blind
spot":

    > **The unobserved authority set**: the class-(c) capabilities in the run's
    > reach closure that no criterion in the contract observes.

Reviewing a criterion's WORDING is reviewing a claim by reading the claim (§5.1),
and the drafter is the party with an interest in weak criteria. What an operator
CAN act on, cheaply and from headers alone, is the set difference §C7 names:

    unobserved  =  classC(reach of the whole run)  minus  classC(contract's cone)

Both sides are already built and this module only composes them:

  * the AUTHORITY side is every class-(c) crossing the composition reaches — the
    same emission/host surface `revl audit` enumerates (`audit_diff.audit_report`
    then `policy.component_reach`), unioned over every component;
  * the OBSERVATION side is the goal contract's own capability cone: the
    transitive reach from every contract component through its `requires`
    wiring, so a criterion that reads a verifier is credited with whatever that
    verifier's provider reaches.

The report is the difference, rendered per capability token in the spelling
`revl audit` uses. It is off-by-default in the strongest sense: a composition
with no goal service (§2.1 — no `Criterion`/`Guard` operation, no `termination`
key in the services IR) has no contract, and the command says so rather than
rendering an empty report.

This is a PURE function of a compiled IR — no session, no policy, no runtime —
which is why it is the slice the item recommends building first and separately
(§11.1 recommendation 1). It reads the L5 header fact that S1 landed (issue #520,
`services[...]["methods"][...]["termination"]`); nothing here lowers a body.

Granularity, stated because a blind-spot report is a safety surface: the cone is
computed at COMPONENT granularity — the `component_reach` walk §5.2 cites — so a
contract observes a capability when it reaches, through its verifiers, the
COMPONENT that crosses it. The finer per-criterion attribution in the §5.2
rendering sketch (naming WHICH criterion reads a capability) is a presentation
refinement over the same set difference and is not computed here; it does not
change which tokens are unobserved. What the report does not and cannot say is
whether an observed criterion is a GOOD proxy — that is the feature's honest
ceiling (§8, 441 §5.4).
"""

from __future__ import annotations

from .audit_diff import audit_report
from .policy import Reach, component_reach

# ----------------------------------------------------------- the header fact

def goal_operations(ir: dict) -> dict[str, dict[str, str]]:
    """`{service: {operation: "criterion"|"guard"}}` for every goal service.

    A goal service is any service with at least one `Criterion` or `Guard`
    operation (§2.1); the marker is the L5 `termination` key on the operation's
    services-IR entry. A service with no marked operation is absent from the
    result, so an empty dict means "no goal service in this composition".
    """
    out: dict[str, dict[str, str]] = {}
    for svc, decl in (ir.get("services") or {}).items():
        marked = {
            op: meta["termination"]
            for op, meta in (decl.get("methods") or {}).items()
            if meta.get("termination")
        }
        if marked:
            out[svc] = marked
    return out


def contract_components(ir: dict) -> list[dict]:
    """The components that make up the contract: every component providing a
    goal service, with the criteria and guards it provides.

    Each entry is ``{"name", "provisions": [{"key", "service", "criteria",
    "guards"}]}``. A component providing two goal services carries two
    provisions; a component providing none is not a contract component.
    """
    goals = goal_operations(ir)
    out: list[dict] = []
    for comp in ir.get("components") or []:
        provisions = []
        for key, svc in (comp.get("provides") or {}).items():
            marked = goals.get(svc)
            if not marked:
                continue
            provisions.append({
                "key": key,
                "service": svc,
                "criteria": sorted(op for op, kind in marked.items()
                                   if kind == "criterion"),
                "guards": sorted(op for op, kind in marked.items()
                                 if kind == "guard"),
            })
        if provisions:
            out.append({"name": comp["name"],
                        "provisions": sorted(provisions,
                                             key=lambda p: p["key"])})
    return out


# --------------------------------------------------------------- the reach

# The extern classes that ARE an irreversible crossing (class-(c)); a witnessed
# extern is class-(a) (item 243, transactional with a registered inverse) and a
# criterion may not even reach one (S1 L1), so it is not authority to observe.
_CLASS_C_EXTERN = frozenset({"emission"})


def _class_c_reach(audit: dict, name: str) -> list[Reach]:
    """The class-(c) boundaries one component reaches, off the audit graph.

    A scoped service emission (`component_reach` kind ``emission``) is an
    irreversible crossing. A reached host extern (kind ``host``) is class-(c)
    only when its declaration is a plain `emission`; a `witnessed` extern is
    class-(a) and is not counted. Pure/acquire host code never appears as a
    crossing here.
    """
    stats = (audit.get("boundary") or {}).get(name) or {}
    ext_class = {e.get("name"): e.get("class")
                 for e in stats.get("externs") or []}
    out: list[Reach] = []
    for reach in component_reach(audit, name):
        if reach.kind == "emission":
            out.append(reach)
        elif reach.kind == "host" and ext_class.get(reach.via) in _CLASS_C_EXTERN:
            out.append(reach)
    return out


def _provider_of_key(ir: dict) -> dict[str, str]:
    """`{wiring-key: providing-component}`. A key is provided by exactly one
    component in a well-formed composition (G2), so the map is unambiguous."""
    out: dict[str, str] = {}
    for comp in ir.get("components") or []:
        for key in (comp.get("provides") or {}):
            out[key] = comp["name"]
    return out


def _cone(ir: dict, roots: list[str]) -> set[str]:
    """Every component reachable from `roots` through `requires` edges,
    including the roots — the contract's capability cone at component
    granularity (§5.2, the `component_reach` walk followed transitively).

    A criterion body only READS (S1 L1), so a contract component's own reach
    carries no class-(c) crossing; the authority it OBSERVES is whatever the
    providers of its verifiers reach, which is exactly this closure.
    """
    provider = _provider_of_key(ir)
    by_name = {c["name"]: c for c in ir.get("components") or []}
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        name = stack.pop()
        if name in seen or name not in by_name:
            continue
        seen.add(name)
        for key in (by_name[name].get("requires") or {}):
            nxt = provider.get(key)
            if nxt is not None and nxt not in seen:
                stack.append(nxt)
    return seen


# --------------------------------------------------------------- the report

def blind_spot(ir: dict) -> dict | None:
    """The §5.2 unobserved-authority report, or ``None`` when the composition
    has no goal service (there is nothing to audit, and §5.3 refuses an empty
    contract rather than rendering one).

    The returned dict:

      * ``contract``      — `contract_components(ir)`;
      * ``criteria``/``guards`` — the total marked-operation counts;
      * ``reachClassC``   — every class-(c) token the run reaches, sorted;
      * ``observed``      — the subset inside the contract's cone, each with the
                            reaching component and the via-label, sorted;
      * ``unobserved``    — the complement, same shape: the blind spot;
      * ``observes``/``of`` — the "observes N of M" headline (§11.3).
    """
    contract = contract_components(ir)
    if not contract:
        return None

    audit = audit_report(ir)
    components = [c["name"] for c in ir.get("components") or []]

    # authority: token -> the (component, via) crossings that reach it.
    authority: dict[str, list[dict]] = {}
    for name in components:
        for reach in _class_c_reach(audit, name):
            authority.setdefault(reach.token, []).append(
                {"component": name, "via": reach.via, "kind": reach.kind})

    cone = _cone(ir, [c["name"] for c in contract])
    observed_tokens = {
        reach.token
        for name in cone
        for reach in _class_c_reach(audit, name)
    }

    def _entry(token: str) -> dict:
        return {"token": token,
                "crossings": sorted(authority[token],
                                    key=lambda x: (x["component"], x["via"]))}

    observed = [_entry(t) for t in sorted(authority) if t in observed_tokens]
    unobserved = [_entry(t) for t in sorted(authority)
                  if t not in observed_tokens]

    criteria = sum(len(p["criteria"]) for c in contract for p in c["provisions"])
    guards = sum(len(p["guards"]) for c in contract for p in c["provisions"])

    return {
        "contract": contract,
        "criteria": criteria,
        "guards": guards,
        "reachClassC": sorted(authority),
        "observed": observed,
        "unobserved": unobserved,
        "observes": len(observed),
        "of": len(authority),
    }


def render(report: dict | None) -> str:
    """The human rendering of `blind_spot`. `None` renders the no-contract
    line the command prints when nothing declares a criterion."""
    if report is None:
        return ("no goal service: no component provides a `Criterion` or "
                "`Guard` operation, so this composition has no termination "
                "contract to audit (docs/design/458-termination-language-"
                "surface.md §2.1)")

    names = ", ".join(f"`{c['name']}`" for c in report["contract"])
    lines = [
        f"goal contract over {names} freezes {report['criteria']} "
        f"criteria and {report['guards']} guards.",
        f"this run reaches {report['of']} class-(c) capabilities; the contract "
        f"observes {report['observes']}.",
        "",
    ]
    if report["observed"]:
        lines.append("  observed")
        for entry in report["observed"]:
            where = ", ".join(f"{c['component']}:{c['via']}"
                              for c in entry["crossings"])
            lines.append(f"    {entry['token']:<28} {where}")
        lines.append("")
    lines.append("  UNOBSERVED")
    if report["unobserved"]:
        for entry in report["unobserved"]:
            where = ", ".join(f"{c['component']}:{c['via']}"
                              for c in entry["crossings"])
            lines.append(f"    {entry['token']:<28} {where}"
                         f"   no criterion reads this")
    else:
        lines.append("    (none — every class-(c) capability is in the "
                     "contract's cone)")
    lines.append("")
    n = len(report["unobserved"])
    if n:
        lines.append(
            f"the contract can be satisfied without any statement about the "
            f"{n} unobserved "
            f"{'capability' if n == 1 else 'capabilities'}. "
            f"ratify, amend, or narrow the run's authority.")
    else:
        lines.append("every class-(c) capability this run reaches is observed "
                     "by the contract's cone.")
    return "\n".join(lines)
