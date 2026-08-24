"""Derived semantic versioning (roadmap item 64).

The version number is *computed*, never chosen. Elm derives a package's
version from the diff of its public API; revl goes one deeper because its
interfaces carry *effects* (emissions crossing the G8 boundary), so the same
drift machinery that admission and `revl plan` already run over two service
tables also tells us the required semver bump.

The bump is a *measurement*, read straight off the classification the drift
predicate (`admission._service_compatible`, DESIGN §5) already produces for
every change between a previous composition and the current one:

  * additive surface (a method or service added, a parameter widened, a
    return narrowed, a capability scope narrowed) is backward compatible for
    a running consumer -> **minor**;
  * any breaking reshape (a method or service removed, a parameter narrowed,
    a return widened, an arity/`async`/`commutative` change) invalidates a
    running consumer's call site -> **major**;
  * a capability change is a semver event even when the *shape* is
    compatible: an operation that **gains an emission** changes what a
    consumer's G8 audit means (an unmarked call site would silently cross the
    boundary) -> **major**; an operation that **loses an emission** is
    strictly purer, the direction §5 already admits as safe -> **minor**.

The classification is not reimplemented here: for every shared operation this
module hands a single-method projection of the old and new service to the
real `_service_compatible` predicate and reads its `_Drift` verdict. A drift
is a breaking change (major); its absence means the change (if any) is
compatible (minor), with the emission-loss case named explicitly because the
predicate — correctly — does not flag dropping an emission as a *break*.

`revl version --against <previous.json>` prints the required bump and why.
The registry (roadmap item 49, phase 2) would later refuse a publish whose
*declared* version contradicts this *computed* one — this module is the
measurement half; it does not touch the registry.
"""

from __future__ import annotations

from dataclasses import dataclass

from .lower import _service_compatible, _service_equal, _service_from_ir
from .parser import ServiceDecl

# bump lattice: the composition's bump is the join (max) over every change.
MAJOR, MINOR, PATCH = "major", "minor", "patch"
_RANK = {PATCH: 1, MINOR: 2, MAJOR: 3}


@dataclass(frozen=True)
class Change:
    """One classified interface change and the bump it forces."""
    service: str
    method: str | None      # the operation, or None for a service-wide change
    kind: str               # added | removed | signature | emission |
    #                         commutative | emission-loss | service-added |
    #                         service-removed
    bump: str               # major | minor | patch
    reason: str             # human-readable "what and why"


def _services_of(document: dict, *, what: str) -> dict:
    """The `services` table of a compiled composition document.

    `--against` takes a *compiled IR* document (what `revl compile` emits, or
    `revl version --emit-manifest`): only that carries the interface shapes —
    parameter/return types and the `emission` classification — that the drift
    predicate reads. An `revl audit --json` interchange document deliberately
    does not (it is the boundary surface, not the interface table), so we fail
    with a pointed message rather than silently diffing nothing.
    """
    services = document.get("services")
    if isinstance(services, dict):
        return services
    raise ValueError(
        f"{what} has no `services` table: pass a compiled composition "
        f"document (produce one with `revl compile <sources> -o prev.json`, "
        f"or `revl version <sources> --emit-manifest`). An `revl audit "
        f"--json` report carries the boundary surface, not the interface "
        f"table the version diff needs.")


def _one_method(name: str, svc: ServiceDecl, method: str) -> ServiceDecl:
    """A single-method projection of `svc`, with the service-level
    `commutative` promise zeroed.

    Feeding the real predicate one method at a time is what turns its
    first-drift-only answer into a per-operation classification. The
    service-level `commutative` flag is neutralised on both sides (handled
    once, service-wide, by the caller) so it cannot re-fire inside every
    per-method comparison."""
    return ServiceDecl(name, {method: svc.methods[method]}, 0, commutative=False)


def _classify_method(service: str, method: str,
                     old_svc: ServiceDecl, new_svc: ServiceDecl) -> Change | None:
    """Classify one operation present in both the old and new service.

    Reuses `_service_compatible` in the *consumer* regime
    (``providers_retained=False``): semver protects any downstream consumer of
    the published interface, and a consumer's call sites are exactly what §5's
    non-strict relation keeps valid. A returned `_Drift` is therefore a break
    (major); its absence is a compatible change (minor) — or, when the method
    is byte-identical, no change at all.
    """
    one_old = _one_method(service, old_svc, method)
    one_new = _one_method(service, new_svc, method)
    drift = _service_compatible(one_new, one_old, providers_retained=False)
    if drift is not None:
        # every drift the consumer regime reports is a break: a removed method,
        # a narrowed parameter, a widened return, an *introduced* emission or a
        # widened capability scope, an async/commutative flip. All -> major.
        return Change(service, method, drift.kind, MAJOR, drift.reason)

    om = old_svc.methods[method]
    nm = new_svc.methods[method]
    if om.emission and not nm.emission:
        # the predicate does not flag dropping an emission as a break (it is
        # safe for a consumer), but it is still a semver event: the operation
        # is strictly purer, so a minor bump records the narrowed authority.
        return Change(service, method, "emission-loss", MINOR,
                      f"`{method}` is no longer an `emission` — strictly purer "
                      f"(a consumer's G8 audit only ever shrinks)")
    if _service_equal(one_new, one_old):
        return None  # identical operation: contributes nothing
    # compatible but not identical: a widened parameter, a narrowed return, a
    # narrowed capability scope. Additive/relaxing surface -> minor.
    return Change(service, method, "widened", MINOR,
                  f"`{method}` changes compatibly (a widened parameter, a "
                  f"narrowed return, or a narrowed capability scope) — a "
                  f"running consumer's call site stays valid")


def diff_services(old_services: dict, new_services: dict) -> list[Change]:
    """Every classified change between two service tables, service by service,
    operation by operation. Reuses the real drift predicate throughout."""
    changes: list[Change] = []
    old_names, new_names = set(old_services), set(new_services)

    for name in sorted(new_names - old_names):
        methods = (new_services[name] or {}).get("methods") or {}
        changes.append(Change(
            name, None, "service-added", MINOR,
            f"service `{name}` is new ({len(methods)} operation(s)) — "
            f"additive surface, breaks no running consumer"))

    for name in sorted(old_names - new_names):
        changes.append(Change(
            name, None, "service-removed", MAJOR,
            f"service `{name}` is removed — a running consumer that injected "
            f"it can no longer resolve it"))

    for name in sorted(old_names & new_names):
        old_svc = _service_from_ir(name, old_services[name] or {})
        new_svc = _service_from_ir(name, new_services[name] or {})
        if old_svc.commutative != new_svc.commutative:
            verb = "drops" if old_svc.commutative else "adds"
            changes.append(Change(
                name, None, "commutative", MAJOR,
                f"service `{name}` {verb} its `commutative` promise, which "
                f"reorders every consumer's calls"))
        old_methods, new_methods = set(old_svc.methods), set(new_svc.methods)
        for method in sorted(new_methods - old_methods):
            changes.append(Change(
                name, method, "added", MINOR,
                f"`{method}` is added to service `{name}` — additive surface, "
                f"breaks no running consumer"))
        for method in sorted(old_methods - new_methods):
            changes.append(Change(
                name, method, "removed", MAJOR,
                f"`{method}` is removed from service `{name}`, but a running "
                f"consumer may still call it"))
        for method in sorted(old_methods & new_methods):
            change = _classify_method(name, method, old_svc, new_svc)
            if change is not None:
                changes.append(change)
    return changes


def _join(changes: list[Change]) -> str:
    """The composition bump is the join over its changes: any major -> major,
    else any minor -> minor, else patch (identical interface, no bump)."""
    bump = PATCH
    for change in changes:
        if _RANK[change.bump] > _RANK[bump]:
            bump = change.bump
    return bump


def _bump_version(previous: str, bump: str) -> str:
    """Apply `bump` to a `MAJOR.MINOR.PATCH` string."""
    parts = previous.strip().lstrip("v").split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(
            f"previous version {previous!r} is not MAJOR.MINOR.PATCH")
    major, minor, patch = (int(p) for p in parts)
    if bump == MAJOR:
        return f"{major + 1}.0.0"
    if bump == MINOR:
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def derive(previous: dict, current_ir: dict,
           previous_version: str | None = None) -> dict:
    """Compute the required semver bump from a previous composition document
    and the current composition IR.

    Returns a machine-readable verdict:

        {
          "bump": "major" | "minor" | "patch",
          "changes": [ {service, method, kind, bump, reason}, ... ],
          "previousVersion": "1.4.2" | None,   # echoed, if supplied
          "nextVersion": "2.0.0"  | None,       # computed, if supplied
        }

    `previous` is a compiled IR document (it carries the `services` table);
    `current_ir` is the freshly compiled current composition. The bump is the
    join over every classified change; an identical interface yields `patch`
    (no minor or major event) and an empty change list.
    """
    old_services = _services_of(previous, what="the previous manifest")
    new_services = _services_of(current_ir, what="the current composition")
    changes = diff_services(old_services, new_services)
    bump = _join(changes)
    result: dict = {
        "bump": bump,
        "changes": [
            {"service": c.service, "method": c.method, "kind": c.kind,
             "bump": c.bump, "reason": c.reason}
            for c in changes
        ],
        "previousVersion": previous_version,
        "nextVersion": (_bump_version(previous_version, bump)
                        if previous_version else None),
    }
    return result


def render(result: dict, against_label: str) -> str:
    """Human-readable derivation: each change -> its classification -> its
    bump contribution, then the computed composition bump (and next version,
    when a previous version was supplied)."""
    bump = result["bump"]
    changes = result["changes"]
    lines: list[str] = []

    if not changes:
        lines.append(
            f"version: PATCH — the interface is unchanged from "
            f"{against_label}; the only permitted bump is a patch (bug fixes).")
        if result.get("previousVersion"):
            lines.append(
                f"  {result['previousVersion']} -> {result['nextVersion']}")
        return "\n".join(lines)

    majors = [c for c in changes if c["bump"] == MAJOR]
    minors = [c for c in changes if c["bump"] == MINOR]

    lines.append(f"version: {bump.upper()} bump required against {against_label}")
    if result.get("previousVersion"):
        lines.append(
            f"  {result['previousVersion']} -> {result['nextVersion']}")
    lines.append("")
    lines.append(f"why: {len(changes)} interface change(s) "
                 f"({len(majors)} major, {len(minors)} minor)")

    def _emit(bucket: list[dict], header: str) -> None:
        if not bucket:
            return
        lines.append("")
        lines.append(header)
        for change in bucket:
            where = (f"{change['service']}.{change['method']}"
                     if change["method"] else change["service"])
            lines.append(f"  [{change['bump']}] {where}  ({change['kind']})")
            lines.append(f"      {change['reason']}")

    _emit(majors, "breaking (major) — a running consumer's use is invalidated:")
    _emit(minors, "compatible (minor) — additive or strictly-purer surface:")
    return "\n".join(lines)
