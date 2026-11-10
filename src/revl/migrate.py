"""`revl migrate`: plan one component version change against its real consumers.

This is the first, read-only slice of issue #2685. A reviewer who wants to move
`cache@1` consumers to `cache@2` otherwise has to run the diff, the version
derivation and the compatibility predicate by hand and join the results. This
module joins them, and nothing else:

  * `composition_diff.diff` reports the structural delta (membership, wiring,
    broken requirements);
  * `audit_diff` reports the authority delta (new crossings, weakened reach,
    widened capability scope);
  * `version.derive` reports the interface bump and its classified changes;
  * `admission._service_compatible` decides whether each real consumer's
    pinned shape still holds against the new version;
  * `admission._handoff_compatible` decides whether declared live state still
    fits the successor; and
  * `adapt.bridge_plan` decides whether an incompatible consumer is reachable
    through a proposed adapter or cannot move at all.

The plan writes nothing and admits nothing: it is a report over its input
documents (a `.rvl` source is compiled on the spot, exactly as `revl diff`
does). Policy-driven rules (for example "refuse every widening") are a later
slice and are deliberately not invented here.

Classification is read off those facts only:

  * `refused`: a real consumer cannot move, or the declared state hand-off is
    incompatible (the swap would be refused, nothing changes);
  * `needs-approval`: the new version widens the measured authority surface (a
    new crossing or host backend, a weakened reach, a widened emission
    capability or emission ceiling, a dropped or weakened recovery), a
    consumer needs an authored adapter, or the successor drops the
    predecessor's state;
  * `needs-handoff`: the successor declares a compatible state hand-off;
  * `needs-canary`: every real consumer can move, but the interface changed,
    so the change should be exercised on a slice before the remainder;
  * `safe`: no consumer work, no authority widening, no state crossing.

The classification is about migration work, not about behaviour: `safe` says
the measured facts require no consumer or authority action, never that the new
version behaves identically.
"""

from __future__ import annotations

from .admission import (_caps_widen, _handoff_compatible, _service_compatible,
                        _service_equal, _service_from_ir, _service_type_drift)
from .adapt import bridge_plan
from .audit_diff import (audit_report, diff_backends, diff_capability_scopes,
                         diff_cardinality, diff_crossings, diff_recovery,
                         diff_registers)
from .compiler import _running_handoffs
from .composition_diff import _components, _realm
from .composition_diff import diff as composition_diff
from .version import PATCH, derive as derive_version

# plan-level classifications, from the issue's five-way split
SAFE = "safe"
NEEDS_CANARY = "needs-canary"
NEEDS_HANDOFF = "needs-handoff"
NEEDS_APPROVAL = "needs-approval"
REFUSED = "refused"

# per-consumer verdicts
MOVABLE = "movable"
NEEDS_ADAPTER = "needs-adapter"
BLOCKED = "blocked"


# --------------------------------------------------------------- input shapes

def _services(ir: dict) -> dict:
    return ir.get("services") or {}


def _decl(ir: dict, name: str | None):
    """The `ServiceDecl` a compiled document declares under `name`."""
    if not name:
        return None
    spec = _services(ir).get(name)
    if spec is None:
        return None
    return _service_from_ir(name, spec)


def _require_consumers(ir: dict) -> None:
    """`--against` must name each consumer's required service.

    A compiled composition (`revl compile -o`) carries `requires` as a
    `{key: service}` map. An audit/interchange document only carries bare key
    lists, so which shape a consumer pinned cannot be read; refuse with a
    pointed message rather than guessing a shape or reporting "movable" for
    every consumer.
    """
    if not ir.get("components"):
        raise ValueError(
            "the --against composition has no `components` table: pass a "
            "compiled composition document (produce one with `revl compile "
            "<sources> -o comp.json` or `revl version <sources> "
            "--emit-manifest`)")
    if not isinstance(ir["components"], list):
        raise ValueError(
            "the --against composition has a malformed `components` table: "
            "expected a list of component records")
    for comp in ir["components"]:
        requires = comp.get("requires") if isinstance(comp, dict) else None
        if requires is None:
            continue
        if not isinstance(requires, dict):
            raise ValueError(
                f"the --against composition does not name the services "
                f"consumer `{comp.get('name')}` requires: pass a compiled "
                f"composition document (`revl compile -o` or `revl version "
                f"--emit-manifest`), not an audit/interchange report")
        for key, service in requires.items():
            if not isinstance(service, str):
                raise ValueError(
                    f"the --against composition does not name the service "
                    f"consumer `{comp.get('name')}` requires for `{key}`: pass "
                    f"a compiled composition document (`revl compile -o` or "
                    f"`revl version --emit-manifest`), not an "
                    f"audit/interchange report")


def provided_surface(ir: dict, label: str = "a composition") -> dict:
    """`(key, realm) -> {component, service, decl}` for every provision.

    Keyed by realm for the same reason `composition_diff` is: two realms may
    each provide `db`, and a consumer is satisfied by the provider in ITS
    realm, so a plan that keyed by key alone would check a consumer against
    the wrong version. A provision whose service is unnamed or undeclared is
    an input error, never silently skipped: skipping it could make a changed
    provision read as unchanged.
    """
    out: dict[tuple[str, str], dict] = {}
    for name, rec in _components(ir).items():
        for key, service in (rec.get("provides") or {}).items():
            if not isinstance(service, str):
                raise ValueError(
                    f"{label} provides `{key}` without naming the service it "
                    f"provides (component `{name}`)")
            decl = _decl(ir, service)
            if decl is None:
                raise ValueError(
                    f"{label} provides `{key}` as service `{service}`, but the "
                    f"document does not declare service `{service}`")
            out[(key, _realm(rec, key))] = {
                "component": name,
                "service": service,
                "decl": decl,
            }
    return out


def _changed_surface(old: dict, new: dict, old_types: dict | None = None,
                     new_types: dict | None = None) -> dict:
    """The provisions whose shape or presence changed between OLD and NEW.

    Only keys the old version provided are considered: a key the new version
    adds has no consumer in the running composition, so it is not a migration
    of anyone. A key the new version withdraws, or whose service shape moved,
    is exactly what the real consumers are pinned to. The shape includes the
    declared types the service's signatures reach: the same spelling over a
    record that gained a field is a reshaped provision, as the admission gate
    reads it (`admission._service_type_drift`).
    """
    changed: dict[tuple[str, str], dict] = {}
    for key, before in old.items():
        after = new.get(key)
        if after is None:
            changed[key] = {"kind": "withdrawn", "before": before, "after": None}
            continue
        if before["decl"] is not None and after["decl"] is not None:
            if _service_equal(after["decl"], before["decl"]) and \
                    _service_type_drift(before["decl"], new_types,
                                        old_types) is None:
                continue
            changed[key] = {"kind": "reshaped", "before": before,
                            "after": after}
        elif before["service"] != after["service"]:
            changed[key] = {"kind": "renamed", "before": before, "after": after}
    return changed


# ------------------------------------------------------------- the consumers

def _pin_against(against_ir: dict, old_surface: dict, changed: dict) -> None:
    """The `--against` composition must still run the OLD version.

    Without this pin, a composition already compiled against NEW would report
    every consumer movable and the old version removable: the opposite of the
    question the plan is asked. A changed provision whose running provider is
    not the OLD shape is an input error, not a plan.
    """
    against_surface = provided_surface(against_ir, "the --against composition")
    for (key, realm), change in changed.items():
        provider = against_surface.get((key, realm))
        where = f" in realm `{realm}`" if realm else ""
        before = change["before"]
        if provider is None:
            raise ValueError(
                f"the --against composition does not provide `{key}`{where}: "
                f"pass the composition the old version is running in")
        if not _service_equal(provider["decl"], before["decl"]):
            raise ValueError(
                f"the --against composition does not run the old version of "
                f"`{key}`{where}: its provider is `{provider['service']}` by "
                f"`{provider['component']}`, while the OLD version provides "
                f"`{before['service']}` by `{before['component']}`; pass the "
                f"composition that still runs the old version")


def _consumer_plan(against: dict, new_ir: dict, new_surface: dict,
                   name: str, key: str, realm: str,
                   need_name: str) -> dict:
    """Classify one real consumer: can it move to the new provision now?"""
    need = _decl(against, need_name)
    entry = {
        "component": name,
        "key": key,
        "realm": realm,
        "service": need_name,
    }
    if need is None:
        entry.update({
            "verdict": BLOCKED,
            "difference": {
                "method": None,
                "kind": "service-undeclared",
                "reason": (f"consumer `{name}` requires service `{need_name}` "
                           f"for `{key}`, which the --against composition does "
                           f"not declare"),
            },
            "adapter": None,
        })
        return entry

    after = new_surface.get((key, realm))
    if after is None or after["decl"] is None:
        where = f" in realm `{realm}`" if realm else ""
        entry.update({
            "verdict": BLOCKED,
            "difference": {
                "method": None,
                "kind": "withdrawn",
                "reason": (f"the new version provides no `{key}`{where}, so "
                           f"consumer `{name}` loses its provider"),
            },
            "adapter": None,
        })
        return entry

    drift = _service_compatible(after["decl"], need, providers_retained=False)
    if drift is None:
        # the spellings hold; the declarations behind the names the consumer
        # pinned may not (the admission gate's reading of the same pair)
        drift = _service_type_drift(need, new_ir.get("types"),
                                    against.get("types"))
    if drift is None:
        entry.update({"verdict": MOVABLE, "difference": None, "adapter": None})
        return entry

    # Incompatible against the direct gate: is it reachable across a proposed
    # adapter? `revl adapt` owns that predicate; this plan only reports it.
    bridge = bridge_plan(
        need, after["decl"], {},
        req_types=against.get("types") or {},
        prov_types=new_ir.get("types") or {})
    difference = {"method": drift.method, "kind": drift.kind,
                  "reason": drift.reason}
    if bridge.ok:
        entry.update({
            "verdict": NEEDS_ADAPTER,
            "difference": difference,
            "adapter": {
                "methods": [mp.method for mp in bridge.methods],
                "merges": list(bridge.merges),
            },
        })
        return entry
    entry.update({
        "verdict": BLOCKED,
        "difference": difference,
        "adapter": {
            "refusals": [
                {"method": r.method, "position": r.position,
                 "clause": r.clause, "reason": r.reason}
                for r in bridge.refusals
            ],
        },
    })
    return entry


def consumers(against: dict, changed: dict, old_surface: dict,
              new_surface: dict, new_ir: dict) -> list[dict]:
    """Every real consumer of a changed provision, classified.

    Consumers come from the `--against` composition's `requires` edges, keyed
    by `(component, key, realm)`: the composition the change actually runs
    against, not the set of consumers a semver range imagines.
    """
    plans: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for name, rec in sorted(_components(against).items()):
        for key, need_name in sorted((rec.get("requires") or {}).items()):
            realm = _realm(rec, key)
            if (key, realm) not in changed:
                continue
            provider = old_surface.get((key, realm))
            if provider is not None and provider["component"] == name:
                continue  # the provider is not its own consumer
            ident = (name, key, realm)
            if ident in seen:
                continue
            seen.add(ident)
            plans.append(_consumer_plan(against, new_ir, new_surface,
                                        name, key, realm, need_name))
    return plans


# --------------------------------------------------------------- the authority

def _authority_methods(ir: dict) -> dict:
    """`(service, method) -> MethodDecl` for every emission/witnessed operation.

    A new or widened emission capability is authored on the service
    declaration, and `audit_diff`'s crossing set only sees call sites, so the
    declaration side is compared here with the same `_caps_widen` the admission
    gate uses.
    """
    out = {}
    for name, spec in _services(ir).items():
        decl = _service_from_ir(name, spec or {})
        for mname, method in decl.methods.items():
            if method.emission or method.witnessed is not None:
                out[(name, mname)] = method
    return out


def _declared_capability_drift(old_ir: dict, new_ir: dict) -> dict:
    old = _authority_methods(old_ir)
    new = _authority_methods(new_ir)
    widened: list[str] = []
    narrowed: list[str] = []
    for service, method in sorted(set(old) | set(new)):
        token = f"capability:{service}.{method}"
        before, after = old.get((service, method)), new.get((service, method))
        if before is None:
            widened.append(token)  # a new emission/witnessed operation
            continue
        if after is None:
            narrowed.append(token)
            continue
        if after.emission and not before.emission:
            widened.append(token)
        elif before.emission and not after.emission:
            narrowed.append(token)
        elif after.emission and before.emission:
            if _caps_widen(before.capabilities, after.capabilities):
                widened.append(token)
            elif _caps_widen(after.capabilities, before.capabilities):
                narrowed.append(token)
        before_witnessed = (tuple(before.witnessed) or None
                            if before.witnessed is not None else None)
        after_witnessed = (tuple(after.witnessed) or None
                           if after.witnessed is not None else None)
        if after.witnessed is not None and before.witnessed is None:
            widened.append(token + ":witnessed")
        elif before.witnessed is not None and after.witnessed is None:
            narrowed.append(token + ":witnessed")
        elif _caps_widen(before_witnessed, after_witnessed):
            widened.append(token + ":witnessed")
        elif _caps_widen(after_witnessed, before_witnessed):
            narrowed.append(token + ":witnessed")
    return {"widened": widened, "narrowed": narrowed}


def authority(old_ir: dict, new_ir: dict) -> dict:
    """The authority delta between two generations, widened direction named.

    Reuses every widening bucket `audit_diff` derives, so a traversal that
    widens without a new crossing cannot read as safe: crossing additions
    (`emit:`, `host:`, `taint:`, `declassify:`, `secret:`, `env:`), weakened
    reach (`reach-weakened:`), widened emission scope (`scope:`), added host
    bodies (`backend:`), weakened idempotency registers (`register:`), widened
    emission ceilings (`cardinality:`), dropped or weakened recovery
    (`recovery:`), and the service declaration side the audit surface is blind
    to (`capability:`). The safe direction of each bucket is reported under
    `narrowed`.
    """
    before = audit_report(old_ir)
    after = audit_report(new_ir)
    crossings = diff_crossings(before, after)
    scopes = diff_capability_scopes(before, after)
    backends = diff_backends(before, after)
    registers = diff_registers(before, after)
    cardinality = diff_cardinality(before, after)
    recovery = diff_recovery(before, after)
    declared = _declared_capability_drift(old_ir, new_ir)
    widened = list(crossings.get("added") or [])
    widened += list(crossings.get("reach_weakened") or [])
    widened += list(scopes.get("scope_widened") or [])
    widened += list(backends.get("backends_added") or [])
    widened += list(registers.get("registers_weakened") or [])
    widened += list(cardinality.get("cardinality_widened") or [])
    widened += list(recovery.get("recovery_dropped") or [])
    widened += list(recovery.get("recovery_weakened") or [])
    widened += declared["widened"]
    narrowed = list(crossings.get("removed") or [])
    narrowed += list(crossings.get("reach_tightened") or [])
    narrowed += list(scopes.get("scope_tightened") or [])
    narrowed += list(backends.get("backends_removed") or [])
    narrowed += list(registers.get("registers_strengthened") or [])
    narrowed += list(cardinality.get("cardinality_tightened") or [])
    narrowed += list(recovery.get("recovery_added") or [])
    narrowed += declared["narrowed"]
    return {
        "widened": sorted(dict.fromkeys(widened)),
        "narrowed": sorted(dict.fromkeys(narrowed)),
    }


# ---------------------------------------------------------------- state handoff

def handoffs(old_ir: dict, new_ir: dict) -> list[dict]:
    """What happens to each predecessor state hand-off on the swap.

    The value flows old -> new, so a successor's accepted shape must admit
    everything the predecessor exports. A predecessor that exported state and
    a successor that declares no hand-off explicitly opts out: the successor
    starts cold, which is a valid choice but a lossy one the plan surfaces.
    The export side is `compiler._running_handoffs`, the same reading the
    admission gate uses, so the plan cannot disagree with the swap.
    """
    old = _running_handoffs(old_ir)
    new = _running_handoffs(new_ir)
    entries: list[dict] = []
    for key, exported in sorted(old.items()):
        accepted = new.get(key)
        entry = {"key": key, "from": exported, "to": accepted}
        if accepted is None:
            entry["verdict"] = "state-dropped"
            entry["reason"] = (
                f"`{key}` exports state from `{exported['component']}`, but "
                f"the new version declares no hand-off on `{key}`: the "
                f"successor starts cold and the predecessor's state is "
                f"dropped")
        elif _handoff_compatible(accepted.get("type"), exported.get("type"),
                                 new_ir.get("types"), old_ir.get("types")):
            entry["verdict"] = "compatible"
            entry["reason"] = (
                f"`{accepted.get('type')}` accepts the state `"
                f"{exported.get('type')}` the predecessor exports")
        else:
            entry["verdict"] = "incompatible"
            entry["reason"] = (
                f"`{accepted.get('type')}` cannot hold the state `"
                f"{exported.get('type')}` the predecessor exports: the swap "
                f"would be refused before teardown")
        entries.append(entry)
    return entries


# ------------------------------------------------------------- classification

def _classification(consumer_plans: list[dict], authority_delta: dict,
                    handoff_plans: list[dict],
                    bump: dict) -> tuple[str, str]:
    blocked = [c["component"] for c in consumer_plans
               if c["verdict"] == BLOCKED]
    if blocked:
        return REFUSED, (
            "real consumer(s) still pinned to the old interface cannot move: "
            + ", ".join(f"`{name}`" for name in blocked))
    incompatible = [h["key"] for h in handoff_plans
                    if h["verdict"] == "incompatible"]
    if incompatible:
        return REFUSED, (
            "the declared state hand-off cannot cross the swap on "
            + ", ".join(f"`{key}`" for key in incompatible))
    if authority_delta["widened"]:
        return NEEDS_APPROVAL, (
            "the new version widens authority: "
            + ", ".join(f"`{token}`" for token in authority_delta["widened"]))
    dropped = [h["key"] for h in handoff_plans
               if h["verdict"] == "state-dropped"]
    if dropped:
        return NEEDS_APPROVAL, (
            "the successor drops the predecessor's live state on "
            + ", ".join(f"`{key}`" for key in dropped))
    adapter = [c["component"] for c in consumer_plans
               if c["verdict"] == NEEDS_ADAPTER]
    if adapter:
        return NEEDS_APPROVAL, (
            "consumer(s) need an authored adapter before they can move: "
            + ", ".join(f"`{name}`" for name in adapter))
    if any(h["verdict"] == "compatible" for h in handoff_plans):
        return NEEDS_HANDOFF, (
            "the successor declares a compatible state hand-off: live state "
            "must cross the swap")
    if bump["bump"] != PATCH:
        return NEEDS_CANARY, (
            f"every real consumer can move, but the interface change derives "
            f"a {bump['bump'].upper()} bump; exercise it on a canary slice "
            f"first")
    return SAFE, (
        "no consumer changes, no authority widening and no state hand-off: "
        "the measured facts require no migration work")


def _rollback(handoff_plans: list[dict], classification: str) -> dict:
    if classification == REFUSED:
        return {
            "possible": True,
            "reason": ("the plan refuses the migration, so nothing is applied "
                       "and there is nothing to roll back"),
        }
    if any(h["verdict"] == "compatible" for h in handoff_plans):
        return {
            "possible": False,
            "reason": ("live state is threaded forward through a declared "
                       "hand-off; the reverse hand-off is not checked, so a "
                       "rollback is not proven to restore it"),
        }
    if any(h["verdict"] in ("incompatible", "state-dropped")
           for h in handoff_plans):
        return {
            "possible": False,
            "reason": ("the forward swap drops or refuses the predecessor's "
                       "live state; a rollback cannot restore it"),
        }
    return {
        "possible": True,
        "reason": ("re-providing the old version reverses the wiring; no live "
                   "state crosses the swap"),
    }


# --------------------------------------------------------------------- plan

def plan(old_ir: dict, new_ir: dict, against_ir: dict, *,
         old_label: str = "OLD", new_label: str = "NEW",
         against_label: str = "COMPOSITION") -> dict:
    """Build the read-only migration plan.

    `old_ir` and `new_ir` are the two compiled versions of the component
    (what `revl compile -o` emits); `against_ir` is the compiled composition
    whose real consumers decide compatibility, which must still run the OLD
    version. Raises `ValueError` when an input cannot supply the facts the
    plan needs.
    """
    _require_consumers(against_ir)
    old_surface = provided_surface(old_ir, "the OLD version")
    new_surface = provided_surface(new_ir, "the NEW version")
    changed = _changed_surface(old_surface, new_surface, old_ir.get("types"),
                               new_ir.get("types"))
    _pin_against(against_ir, old_surface, changed)
    bump = derive_version(old_ir, new_ir)
    delta = composition_diff(old_ir, new_ir)
    authority_delta = authority(old_ir, new_ir)
    consumer_plans = consumers(against_ir, changed, old_surface, new_surface,
                               new_ir)
    handoff_plans = handoffs(old_ir, new_ir)
    classification, reason = _classification(
        consumer_plans, authority_delta, handoff_plans, bump)

    added = [key for key in new_surface if key not in old_surface]
    surface = {
        "changed": [
            {
                "key": key,
                "realm": realm,
                "kind": change["kind"],
                "from": _surface_json(change["before"]),
                "to": _surface_json(change["after"]),
            }
            for (key, realm), change in sorted(changed.items())
        ],
        "added": [
            {"key": key, "realm": realm,
             "component": new_surface[(key, realm)]["component"],
             "service": new_surface[(key, realm)]["service"]}
            for key, realm in sorted(added)
        ],
    }

    pinned = [c for c in consumer_plans if c["verdict"] != MOVABLE]
    old_removable = not pinned and not any(
        h["verdict"] == "incompatible" for h in handoff_plans)

    is_changed = bool(changed or surface["added"] or authority_delta["widened"]
                      or authority_delta["narrowed"]
                      or bump["bump"] != PATCH or handoff_plans)
    return {
        "command": "migrate",
        "old": old_label,
        "new": new_label,
        "against": against_label,
        "changed": is_changed,
        "surface": surface,
        "bump": bump,
        "consumers": consumer_plans,
        "authority": authority_delta,
        "handoff": handoff_plans,
        "classification": classification,
        "classificationReason": reason,
        "oldRemovable": old_removable,
        "rollback": _rollback(handoff_plans, classification),
        "structural": delta,
    }


def _surface_json(surface: dict | None) -> dict | None:
    if surface is None:
        return None
    return {"component": surface["component"], "service": surface["service"]}


# ------------------------------------------------------------------- render

def render(plan: dict) -> str:
    """Human-readable migration plan: surface, bump, consumers, authority,
    hand-off, then the verdict and its consequences."""
    from .version import render as render_version  # noqa: PLC0415

    lines = [
        f"migrate plan: {plan['old']} -> {plan['new']} "
        f"against {plan['against']}",
        "",
    ]
    if not plan["changed"]:
        lines.append(
            "surface: no interface or authority change between the two "
            "versions.")
    else:
        if plan["surface"]["changed"]:
            lines.append("surface: changed provisions")
            for entry in plan["surface"]["changed"]:
                where = f" in realm `{entry['realm']}`" if entry["realm"] else ""
                before = (f"`{entry['from']['service']}` "
                          f"({entry['from']['component']})"
                          if entry["from"] else "nothing")
                after = (f"`{entry['to']['service']}` "
                         f"({entry['to']['component']})"
                         if entry["to"] else "withdrawn")
                lines.append(f"  ~ `{entry['key']}`{where}: {before} -> {after}")
        if plan["surface"]["added"]:
            lines.append("surface: added provisions (no consumer to migrate)")
            for entry in plan["surface"]["added"]:
                where = f" in realm `{entry['realm']}`" if entry["realm"] else ""
                lines.append(f"  + `{entry['key']}`{where} "
                             f"(`{entry['service']}`, "
                             f"`{entry['component']}`)")
        lines.append("")
        lines.append(render_version(plan["bump"], plan["old"]))

    # consumers, when the surface has any
    if plan["consumers"]:
        lines.append("")
        lines.append(f"consumers ({len(plan['consumers'])}):")
        marks = {MOVABLE: "+", NEEDS_ADAPTER: "~", BLOCKED: "-"}
        for consumer in plan["consumers"]:
            where = (f" in realm `{consumer['realm']}`"
                     if consumer["realm"] else "")
            head = (f"  {marks[consumer['verdict']]} {consumer['component']}  "
                    f"{consumer['verdict']}  (`{consumer['key']}`{where})")
            lines.append(head)
            if consumer["difference"] is not None:
                lines.append(f"      {consumer['difference']['reason']}")
            adapter = consumer["adapter"]
            if adapter and adapter.get("refusals"):
                for refusal in adapter["refusals"]:
                    lines.append(
                        f"      adapter refused ({refusal['clause']}): "
                        f"{refusal['reason']}")
            elif adapter:
                methods = ", ".join(f"`{m}`" for m in adapter["methods"])
                lines.append(f"      adapter would bridge {methods}")

    # authority
    widened = plan["authority"]["widened"]
    narrowed = plan["authority"]["narrowed"]
    if widened or narrowed:
        lines.append("")
        lines.append("authority:")
        for token in widened:
            lines.append(f"  + {token}")
        for token in narrowed:
            lines.append(f"  - {token}")

    # state hand-off
    if plan["handoff"]:
        lines.append("")
        lines.append("state hand-off:")
        for entry in plan["handoff"]:
            lines.append(f"  {entry['key']}: {entry['verdict']}")
            lines.append(f"      {entry['reason']}")

    lines.append("")
    lines.append(f"classification: {plan['classification']}")
    lines.append(f"  {plan['classificationReason']}")
    lines.append(
        "old version removable: "
        + ("yes (no consumer is still pinned to it)"
           if plan["oldRemovable"]
           else "no (a consumer or state hand-off still needs it)"))
    rollback = plan["rollback"]
    lines.append(
        f"rollback: {'possible' if rollback['possible'] else 'not proven'}")
    lines.append(f"  {rollback['reason']}")
    return "\n".join(lines)
