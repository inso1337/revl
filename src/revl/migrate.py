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
does).

The second slice reads the operator's boundary policy (`--policy FILE`, see
docs/boundary-policy.md). The policy is evaluated over the composition as it
would stand AFTER the migration (the old version's components replaced by the
new version's), by the same `policy.evaluate` and `policy.approval_admission`
the gate runs, and only the violations the migration INTRODUCES count: a rule
the composition already breaks before the change is reported as pre-existing,
not blamed on the new version. No rule is invented here: "refuse every
widening" is written in the policy file as an allow-list, as it is for
admission.

Classification is read off those facts only:

  * `refused`: a real consumer cannot move, the declared state hand-off is
    incompatible, or the policy refuses the composition after the migration
    (the swap would be refused, nothing changes);
  * `needs-approval`: the new version widens the measured authority surface (a
    new crossing or host backend, a weakened reach, a widened emission
    capability or emission ceiling, a dropped or weakened recovery), the
    policy requires an approval for a capability the new version reaches, a
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
                        _service_equal, _service_from_ir)
from .adapt import bridge_plan
from .audit_diff import (audit_report, diff_backends, diff_capability_scopes,
                         diff_cardinality, diff_crossings, diff_inverses,
                         diff_recovery, diff_registers)
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


def _changed_surface(old: dict, new: dict) -> dict:
    """The provisions whose shape or presence changed between OLD and NEW.

    Only keys the old version provided are considered: a key the new version
    adds has no consumer in the running composition, so it is not a migration
    of anyone. A key the new version withdraws, or whose service shape moved,
    is exactly what the real consumers are pinned to.
    """
    changed: dict[tuple[str, str], dict] = {}
    for key, before in old.items():
        after = new.get(key)
        if after is None:
            changed[key] = {"kind": "withdrawn", "before": before, "after": None}
            continue
        if before["decl"] is not None and after["decl"] is not None:
            if _service_equal(after["decl"], before["decl"]):
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
    (`recovery:`), a weakened inverse class (`inverse:`, which also carries a
    recovery kind move, issue #3182), and the service declaration side the audit surface is blind
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
    inverses = diff_inverses(before, after)
    declared = _declared_capability_drift(old_ir, new_ir)
    widened = list(crossings.get("added") or [])
    widened += list(crossings.get("reach_weakened") or [])
    widened += list(scopes.get("scope_widened") or [])
    widened += list(backends.get("backends_added") or [])
    widened += list(registers.get("registers_weakened") or [])
    widened += list(cardinality.get("cardinality_widened") or [])
    widened += list(recovery.get("recovery_dropped") or [])
    widened += list(recovery.get("recovery_weakened") or [])
    widened += list(inverses.get("inverse_weakened") or [])
    widened += declared["widened"]
    narrowed = list(crossings.get("removed") or [])
    narrowed += list(crossings.get("reach_tightened") or [])
    narrowed += list(scopes.get("scope_tightened") or [])
    narrowed += list(backends.get("backends_removed") or [])
    narrowed += list(registers.get("registers_strengthened") or [])
    narrowed += list(cardinality.get("cardinality_tightened") or [])
    narrowed += list(recovery.get("recovery_added") or [])
    narrowed += list(inverses.get("inverse_strengthened") or [])
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


# ------------------------------------------------- the composition after it

# top-level IR keys `compose_after` rebuilds itself; every other table is merged
# generically (see `_merge_table`).
_COMPOSED_KEYS = frozenset({"components", "services", "manifest"})


def _names(ir: dict) -> list[str]:
    return [c["name"] for c in ir.get("components") or []]


def _manifest_entries(ir: dict) -> list[dict]:
    """The composition-manifest rows of `ir`, one per component.

    The document's own `manifest.components` rows are preferred, because they
    carry what the linker recorded (`intercept`, `routes`). A document without
    them (a hand-built one) gets rows derived from its component records, in
    the shape the linker writes, so the audit reads realms and keys the same way
    either way."""
    rows = (ir.get("manifest") or {}).get("components")
    if rows:
        return [dict(row) for row in rows]
    out = []
    for comp in ir.get("components") or []:
        row = {"name": comp["name"], "file": comp.get("source", ""),
               "inject": sorted(comp.get("requires") or {}),
               "provides": list((comp.get("provides") or {}))}
        if comp.get("isolate"):
            row["isolate"] = dict(comp["isolate"])
        out.append(row)
    return out


def _splice(existing: list, replaced: set, incoming: list, name) -> list:
    """`existing` with every item named in `replaced` swapped for the incoming
    item of the same name AT ITS ORIGINAL POSITION, the rest of the incoming
    items appended. In place, because the audit and the policy walk components
    in document order: a violation that names a pair of components names them
    in that order, so moving the replaced component to the end would turn an
    unchanged violation into a different one."""
    incoming_by = {name(item): item for item in incoming}
    placed: set = set()
    out = []
    for item in existing:
        key = name(item)
        if key not in replaced:
            out.append(item)
        elif key in incoming_by and key not in placed:
            out.append(incoming_by[key])
            placed.add(key)
    out += [item for item in incoming if name(item) not in placed]
    return out


def _merge_table(retained, added):
    """A top-level table of the composed document that has no better rule:
    lists concatenate without repeats, mappings take the new version's entries
    over the old, and anything else keeps the retained composition's value.
    Components, services, the manifest and externs have their own rules in
    `compose_after`."""
    if isinstance(retained, list) and isinstance(added, list):
        out = list(retained)
        out += [item for item in added if item not in out]
        return out
    if isinstance(retained, dict) and isinstance(added, dict):
        return {**retained, **added}
    return retained if retained is not None else added


def _composed_externs(old_ir: dict, new_ir: dict, against_ir: dict,
                      retained: list[dict]) -> list[dict] | None:
    """The extern declarations of the composition after the migration.

    The new version's declarations replace a same-named one in place. A
    declaration only the OLD version carried is withdrawn unless a component
    that stays still reaches it, which the audit of the unchanged composition
    says: an extern is shared host code, and a retained component may be what
    keeps it reachable. Without the withdrawal a stale declaration of the old
    version would keep feeding the register floors and the declared-host
    surface after the version that owned it is gone."""
    against_ext = against_ir.get("externs")
    new_ext = new_ir.get("externs")
    if against_ext is None and new_ext is None:
        return None
    against_ext = list(against_ext or [])
    new_ext = list(new_ext or [])
    new_names = {e["name"] for e in new_ext}
    old_only = ({e["name"] for e in old_ir.get("externs") or []}
                - new_names)
    boundary = audit_report(against_ir).get("boundary") or {}
    kept = {c["name"] for c in retained}
    still_reached = {ext["name"] for comp, stats in boundary.items()
                     if comp in kept
                     for ext in stats.get("externs") or []}
    withdrawn = old_only - still_reached
    return _splice(against_ext, withdrawn | new_names, new_ext,
                   lambda e: e["name"])


def compose_after(old_ir: dict, new_ir: dict, against_ir: dict) -> dict:
    """The composition as it would stand once the migration is applied: the
    components of the OLD version (and any component the NEW version redeclares
    by name, the way `compile_files(..., manifest=running)` withdraws one) are
    replaced by the NEW version's, each in the position it held.

    A document-level reconstruction over the three compiled inputs: nothing is
    recompiled, linked or admitted (the manifest carries the rows and no
    `loadOrder`). `components`, `services`, `manifest` and `externs` are rebuilt; every other table (types, secrets, environment
    contracts) is merged by `_merge_table`, which adds the new version's
    declarations and withdraws none, so for those tables the composed audit can
    only see more of the composition, never less."""
    replaced = set(_names(old_ir)) | set(_names(new_ir))
    retained = [c for c in against_ir.get("components") or []
                if c.get("name") not in replaced]
    after = dict(against_ir)
    after["components"] = _splice(
        list(against_ir.get("components") or []), replaced,
        list(new_ir.get("components") or []), lambda c: c["name"])
    after["services"] = {**_services(against_ir), **_services(new_ir)}
    rows = _splice(_manifest_entries(against_ir), replaced,
                   _manifest_entries(new_ir), lambda row: row["name"])
    manifest = dict(against_ir.get("manifest") or {})
    manifest["components"] = rows
    # the link order is the linker's to compute, and neither the audit nor the
    # policy reads it; a stale order would claim a link that was never made
    manifest.pop("loadOrder", None)
    after["manifest"] = manifest
    externs = _composed_externs(old_ir, new_ir, against_ir, retained)
    if externs is not None:
        after["externs"] = externs
    for key in set(against_ir) | set(new_ir):
        if key in _COMPOSED_KEYS or key == "externs":
            continue
        merged = _merge_table(against_ir.get(key), new_ir.get(key))
        if merged is not None:
            after[key] = merged
    return after


# --------------------------------------------------------------------- policy

#: policy violation kinds that ask for an approval rather than forbid the reach
#: (`capability X requires approval`, docs/boundary-policy.md): the policy says
#: the capability may be reached once an approval is obtained and threaded, so
#: the plan classifies them `needs-approval` and names what to obtain. The gate
#: itself still refuses the composition until the approval edge is in the code,
#: which is what "needs" means here. Every other kind is a refusal.
_APPROVAL_KINDS = frozenset({"approval", "declassify-approval"})


def _violation_json(violation) -> dict:
    return {"kind": violation.kind, "component": violation.component,
            "token": violation.token, "message": violation.message}


def _violation_key(violation) -> tuple:
    """What makes two violations the same violation across the migration: the
    rule that fired, who breaks it and on what. Not the message, which embeds
    the chain and the emission label and so changes when a new version renames
    a require key without changing the breach. A tenant violation names a PAIR,
    and the pair has no order."""
    if violation.kind == "tenant":
        pair = frozenset(step.name for step in violation.why.steps
                         if step.kind == "component")
        return (violation.kind, violation.token, pair)
    return (violation.kind, violation.component, violation.token)


def _composition_violations(policy, ir: dict) -> list:
    """Every violation the gate would raise over `ir` that a composition
    document can decide: the closed `policy.evaluate` legs (allow, deny, pair,
    tenants, declassify, taint flow, idempotent teardown) plus the approval
    admission. The evidence rules read the candidate's published evidence
    bundle, which a compiled document does not carry, so they are withheld here
    and reported as undecided instead of failing for want of evidence the gate
    would have. Register floors read the audit alone and are decided. The
    sandbox profile (`mcp`) applies to components admitted through the MCP
    session, which a plan has none of, so it never selects."""
    import dataclasses  # noqa: PLC0415
    import warnings  # noqa: PLC0415

    from .policy import approval_admission, evaluate  # noqa: PLC0415

    decidable = dataclasses.replace(policy, evidence_rules=())
    with warnings.catch_warnings():
        # an inert-rule warning describes the policy, not the migration; it is
        # `revl policy evaluate`'s to raise, once, against the real composition
        warnings.simplefilter("ignore")
        found = list(evaluate(decidable, audit_report(ir)))
        found += approval_admission(decidable, ir)
    return found


def policy_verdict(policy, against_ir: dict, after_ir: dict) -> dict:
    """What the boundary policy says about the migration.

    The composition is evaluated before and after; only a violation the
    migration INTRODUCES is `new`. A violation both states carry is
    `preexisting`: the composition already breaks that rule, the new version
    did not cause it, and blaming it on the new version would make every plan
    over a composition that is already out of policy read as refused. A
    violation the migration resolves is `resolved`."""
    before = _composition_violations(policy, against_ir)
    after = _composition_violations(policy, after_ir)
    before_keys = {_violation_key(v) for v in before}
    after_keys = {_violation_key(v) for v in after}
    return {
        "source": policy.source,
        "new": [_violation_json(v) for v in after
                if _violation_key(v) not in before_keys],
        "preexisting": [_violation_json(v) for v in after
                        if _violation_key(v) in before_keys],
        "resolved": [_violation_json(v) for v in before
                     if _violation_key(v) not in after_keys],
        "undecided": {"evidenceRules": len(policy.evidence_rules)},
    }


# ------------------------------------------------------------- classification

def _classification(consumer_plans: list[dict], authority_delta: dict,
                    handoff_plans: list[dict], bump: dict,
                    policy: dict | None = None) -> tuple[str, str]:
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
    new_violations = (policy or {}).get("new") or []
    refusing = [v for v in new_violations if v["kind"] not in _APPROVAL_KINDS]
    if refusing:
        return REFUSED, (
            "the boundary policy refuses the composition after the migration: "
            + "; ".join(f"{v['kind']} `{v['component']}` -> `{v['token']}`"
                        for v in refusing))
    approving = [v for v in new_violations if v["kind"] in _APPROVAL_KINDS]
    if authority_delta["widened"] or approving:
        reasons = []
        if authority_delta["widened"]:
            reasons.append(
                "the new version widens authority: " + ", ".join(
                    f"`{token}`" for token in authority_delta["widened"]))
        if approving:
            reasons.append(
                "the boundary policy requires an approval for: " + ", ".join(
                    f"`{v['token']}` ({v['component']})" for v in approving))
        return NEEDS_APPROVAL, "; ".join(reasons)
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
         against_label: str = "COMPOSITION", policy=None) -> dict:
    """Build the read-only migration plan.

    `old_ir` and `new_ir` are the two compiled versions of the component
    (what `revl compile -o` emits); `against_ir` is the compiled composition
    whose real consumers decide compatibility, which must still run the OLD
    version. `policy` is a loaded `policy.Policy`: when given, it is evaluated
    over the composition as it would stand after the migration and the plan
    carries the verdict under `policy`. Raises `ValueError` when an input
    cannot supply the facts the plan needs.
    """
    _require_consumers(against_ir)
    old_surface = provided_surface(old_ir, "the OLD version")
    new_surface = provided_surface(new_ir, "the NEW version")
    changed = _changed_surface(old_surface, new_surface)
    _pin_against(against_ir, old_surface, changed)
    bump = derive_version(old_ir, new_ir)
    delta = composition_diff(old_ir, new_ir)
    authority_delta = authority(old_ir, new_ir)
    consumer_plans = consumers(against_ir, changed, old_surface, new_surface,
                               new_ir)
    handoff_plans = handoffs(old_ir, new_ir)
    policy_result = (policy_verdict(policy, against_ir,
                                    compose_after(old_ir, new_ir, against_ir))
                     if policy is not None else None)
    classification, reason = _classification(
        consumer_plans, authority_delta, handoff_plans, bump, policy_result)

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
    result = {
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
    if policy_result is not None:
        # present only when a policy was given, so a plan without `--policy`
        # keeps the exact shape it had before the policy slice
        result["policy"] = policy_result
    return result


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

    # boundary policy
    verdict = plan.get("policy")
    if verdict is not None:
        lines.append("")
        lines.append(f"boundary policy ({verdict['source'] or 'policy'}):")
        if not (verdict["new"] or verdict["preexisting"]
                or verdict["resolved"]):
            lines.append("  no violation before or after the migration")
        for label, mark in (("new", "+"), ("preexisting", "="),
                            ("resolved", "-")):
            for item in verdict[label]:
                lines.append(f"  {mark} {label}: {item['kind']} "
                             f"`{item['component']}` -> `{item['token']}`")
                lines.append(f"      {item['message']}")
        undecided = verdict["undecided"]
        if undecided["evidenceRules"]:
            lines.append(
                f"  not decided here: {undecided['evidenceRules']} evidence "
                f"rule(s), which read the candidate's published evidence at "
                f"the gate")

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
