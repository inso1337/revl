"""Federated compositions: verified contracts between sovereign systems
(roadmap item 58).

Placement (item 55) splits *one* composition across processes. The org-scale
question is different: two compositions, owned by two teams, deployed
independently — composition **A** consumes a service composition **B**
provides. Today that seam is where microservice architectures bleed: the
contract is prose, and drift is found in production.

revl already owns both halves of the fix and this module only points them
*across* the deployment boundary:

  * **the manifest is the contract.** A composition's compiled IR carries a
    `services` table — the exact interface shapes (parameter/return types, the
    `emission`/capability classification) each service declares. A consumer A
    pins the shape of every service it *requires from outside itself*; that
    pinned projection is A's **consumer surface** — a small, versioned,
    language-neutral artifact A publishes.

  * **the §5/drift check is the checker.** Whether B's *new* manifest still
    satisfies A's *pinned* requirement is exactly the question
    `admission._service_compatible` (DESIGN §5) already answers for a running
    consumer, and that item 64 (`revl version`) already reads off for a semver
    bump. This module hands A's pinned services (as `old`) and B's current
    services (as `new`) to the *same* `version.diff_services`, in the *same*
    consumer regime, and reads its verdict: **any change it classifies MAJOR is
    a contract break** — B's deploy would invalidate a call site A already
    shipped. A minor change (an added method, a widened parameter, a dropped
    emission) is additive or strictly-purer surface and leaves A intact.

The workflow this enables — consumer-driven contract testing, compiler-verified,
with no test code written:

  1. A's CI runs `revl contract export` and registers the resulting consumer
     surface with B (roadmap item 49's registry is where it would live).
  2. B's CI, before it deploys, runs `revl contract check` for every registered
     consumer: *"would this deploy break A?"* A MAJOR drift fails the gate and
     names the exact requirement that broke and why.

Nothing here re-implements the relation: the classification is
`version.diff_services`', which is `admission._service_compatible`'s, the same
predicate the runtime-admission gate and derived versioning already trust. This
module is the projection (`consumer_surface`) and the cross-boundary framing
(`check`); the verdict is borrowed whole. See docs/federation.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from .version import MAJOR, _services_of, diff_services

# MAJOR.MINOR, same discipline as the interchange format (item 28): additive
# changes bump MINOR, a removed/re-shaped member bumps MAJOR. A consumer of the
# artifact gates on MAJOR and ignores unknown members.
CONTRACT_VERSION = "1.0"

# Self-identifying tag: a consumer surface is a projection of an interchange
# document, so it carries its own `kind` to keep the two apart.
CONTRACT_KIND = "revl.contract.consumer"


@dataclass(frozen=True)
class Break:
    """One requirement of A's that B's current manifest no longer satisfies."""
    service: str
    method: str | None      # the operation, or None for a service-wide break
    kind: str               # removed | signature | emission | commutative |
    #                         service-removed  (every drift kind §5 reports)
    reason: str             # the drift predicate's own "what and why"


def consumer_surface(ir: dict, *, consumer: str | None = None) -> dict:
    """Project composition A's compiled IR into its **consumer surface**: the
    pinnable contract of everything A requires from *outside itself*.

    A service is part of the surface when some component of A *requires* it and
    *no* component of A *provides* it — i.e. it is supplied by another sovereign
    composition (B). Its full interface shape is copied verbatim from A's
    `services` table, so the pin carries the exact parameter/return types and
    the `emission`/capability classification the drift check needs; a provider
    can verify against it WITHOUT running A (or even revl).

    A service A both requires and provides internally is *not* external and is
    omitted: the contract is only about the cross-boundary seam.
    """
    services = ir.get("services") or {}
    provided: set[str] = set()
    required: set[str] = set()
    for comp in ir.get("components") or []:
        provided |= set((comp.get("provides") or {}).values())
        required |= set((comp.get("requires") or {}).values())
    external = required - provided
    requires = {name: services[name]
                for name in sorted(external) if name in services}
    return {
        "schema_version": CONTRACT_VERSION,
        "kind": CONTRACT_KIND,
        "consumer": consumer,
        "requires": requires,
    }


def _pinned_requires(consumer_doc: dict) -> dict:
    """The `requires` table of a consumer-surface artifact, with a pointed
    message when the document is the wrong kind or shape."""
    kind = consumer_doc.get("kind")
    if kind != CONTRACT_KIND:
        raise ValueError(
            f"not a consumer-surface artifact (kind={kind!r}, expected "
            f"{CONTRACT_KIND!r}): produce one with `revl contract export "
            f"<A-sources> --json`. An `revl audit`/`compile` document is the "
            f"whole composition, not the pinned consumer surface.")
    requires = consumer_doc.get("requires")
    if not isinstance(requires, dict):
        raise ValueError(
            "consumer-surface artifact has no `requires` table (it is the "
            "pinned contract of what the consumer needs from a provider)")
    return requires


def check(consumer_doc: dict, provider_ir: dict) -> dict:
    """Does provider B's current manifest still satisfy consumer A's pinned
    surface? The cross-boundary drift check.

    `consumer_doc` is A's `consumer_surface` artifact; `provider_ir` is B's
    freshly compiled composition (it carries the `services` table — an `revl
    audit --json` report deliberately does not, and is rejected with the same
    message `revl version` uses).

    The pinned requirement is fed to `version.diff_services` as the *old*
    interface and B's current service as the *new* one — the identical consumer
    regime item 64 runs — so the classification is the real §5 predicate's, not
    a second copy. **Every change it grades MAJOR is a contract break** (a
    removed or narrowed operation, an operation that gained an emission A's G8
    did not account for, a widened capability scope, a service A requires that B
    no longer provides). A minor change is compatible surface and leaves A's
    shipped call sites valid.

    Returns a machine-readable verdict:

        {
          "satisfied": bool,          # False iff B breaks A
          "consumer": "<label>|None",
          "pinned": ["Service", ...], # services A requires from B
          "breaks":     [ {service, method, kind, reason}, ... ],
          "compatible": [ {service, method, kind, reason}, ... ],
        }
    """
    pinned = _pinned_requires(consumer_doc)
    provider_services = _services_of(provider_ir, what="the provider composition")
    # restrict B's manifest to exactly what A pins: B's other services are none
    # of A's business, and a pinned service B *lacks* falls out of `diff_services`
    # as a `service-removed` MAJOR break ("B no longer provides what A requires").
    provider_pinned = {name: provider_services[name]
                       for name in pinned if name in provider_services}
    changes = diff_services(pinned, provider_pinned)
    breaks = [c for c in changes if c.bump == MAJOR]
    compatible = [c for c in changes if c.bump != MAJOR]

    def _row(c) -> dict:
        return {"service": c.service, "method": c.method,
                "kind": c.kind, "reason": c.reason}

    return {
        "satisfied": not breaks,
        "consumer": consumer_doc.get("consumer"),
        "pinned": sorted(pinned),
        "breaks": [_row(c) for c in breaks],
        "compatible": [_row(c) for c in compatible],
    }


def render(result: dict, consumer_label: str, provider_label: str) -> str:
    """Human-readable contract verdict: pass ("B still satisfies A") or, for a
    break, every requirement that drifted with the drift predicate's own why."""
    consumer = result.get("consumer") or consumer_label
    pinned = result["pinned"]
    breaks = result["breaks"]
    compatible = result["compatible"]
    lines: list[str] = []

    def _where(row: dict) -> str:
        return (f"{row['service']}.{row['method']}"
                if row["method"] else row["service"])

    if result["satisfied"]:
        lines.append(
            f"contract OK — {provider_label} still satisfies {consumer}'s "
            f"pinned surface ({len(pinned)} service(s): "
            f"{', '.join(pinned) or '—'}).")
        if compatible:
            lines.append("")
            lines.append("compatible changes (additive or strictly purer — a "
                         "shipped call site of the consumer stays valid):")
            for row in compatible:
                lines.append(f"  [ok] {_where(row)}  ({row['kind']})")
                lines.append(f"      {row['reason']}")
        return "\n".join(lines)

    lines.append(
        f"contract BROKEN — deploying {provider_label} would break "
        f"{consumer}: {len(breaks)} pinned requirement(s) drift.")
    lines.append("")
    lines.append("breaking (the consumer's shipped call site is invalidated):")
    for row in breaks:
        lines.append(f"  [break] {_where(row)}  ({row['kind']})")
        lines.append(f"      {row['reason']}")
    if compatible:
        lines.append("")
        lines.append("also changed, but compatible:")
        for row in compatible:
            lines.append(f"  [ok] {_where(row)}  ({row['kind']})")
            lines.append(f"      {row['reason']}")
    return "\n".join(lines)
