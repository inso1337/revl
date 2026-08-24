"""Admission gate and service-compatibility relation (DESIGN.md §5/§6.6).

Split out of `lower.py` (roadmap item 17): the runtime-admission gate that
decides whether a service redeclared against a running manifest is an
admissible replacement, plus the structural helpers it rests on. Pure code
movement — every name here is re-exported from `revl.lower`, so the public
import surface is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import RevlError
from .parser import Program, ServiceDecl
from .typecheck import compatible
from .why import SET, TraceStep, WhyTrace


def _service_from_ir(name: str, spec: dict) -> ServiceDecl:
    """Rebuild a checkable ServiceDecl from a v1 IR services entry."""
    from .parser import MethodDecl

    methods = {}
    for mname, mspec in (spec.get("methods") or {}).items():
        params = [(p.get("name"), p.get("type")) for p in mspec.get("params") or []]
        methods[mname] = MethodDecl(
            mname,
            params,
            mspec.get("returns"),
            bool(mspec.get("emission")),
            0,
            async_=bool(mspec.get("async")),
            commutative=bool(mspec.get("commutative")),
            capabilities=(tuple(mspec["capabilities"])
                          if mspec.get("capabilities") is not None else None),
        )
    return ServiceDecl(name, methods, 0, commutative=bool(spec.get("commutative")))


def _service_equal(a: ServiceDecl, b: ServiceDecl) -> bool:
    def shape(svc: ServiceDecl):
        return (
            svc.commutative,
            {
                m.name: (tuple(m.params), m.returns, m.emission, m.async_,
                         m.commutative, m.capabilities)
                for m in svc.methods.values()
            },
        )

    return shape(a) == shape(b)


# ------------------------------------------------- admission compatibility (§5)
#
# The runtime-admission gate (compile_files with a running manifest) used to
# demand that a redeclared service be *structurally identical* to the one the
# running composition already carries (`_service_equal`). §5/§6.6 of the
# roadmap calls this out: exact match, no structural compatibility.
#
# We replace exact-match with a *compatibility relation*. The relation is not
# a generic subtype rule; it is grounded in what admission is protecting — the
# running components that already touch the interface — and it is oriented by
# the direction in which each such component uses it:
#
#   * A running CONSUMER injected the key and called methods on it. Its call
#     sites were type-checked against the *old* interface and are never
#     recompiled. For them to keep type-checking against the new interface a
#     method may be *added*, a parameter may only *widen* (contravariant: a
#     value the consumer passed still fits), a return may only *narrow*
#     (covariant: the consumer still uses the result at the old type), and an
#     `emission` may be *dropped* but never *introduced* (an unmarked call site
#     silently crossing the boundary breaks G4/G8; tightening purity is safe).
#     A method a consumer might call may not be removed.
#
#   * A running PROVIDER implements the interface, in full: A6 requires a
#     provider to implement *every* method of the service it provides, with
#     matching arity/`async`/parameter/return types and a checked emission
#     classification, so it conforms to exactly the old interface. A provider
#     is retained only when this admission does not replace it; a swap that
#     re-provides the key withdraws the old provider (G2 forbids two providers
#     of one key), so the common hot-swap has *no* retained provider. Where a
#     provider *is* retained, the interface cannot change at all — an added
#     method is an unfilled obligation, a retyped one fails A6, a re-scoped
#     emission may outlaw one the provider makes — so the only admissible
#     replacement is identity. The relaxation above is therefore sound only
#     when the provider is being replaced by one checked against the new shape.
#
# The manifest carries `inject`/`provides` as key lists, and `compile_files`
# threads a key -> service map (`provision_services`) alongside it, so the
# check is *consumer/provider-relative*: it identifies which running
# components touch this service, applies the strict (identity) rule when a
# provider is retained and the consumer-subtype rule when it is not, and — when
# it can prove nothing running touches the service — admits any change at all
# (there is nothing to break). Where a key cannot be resolved to a service the
# gate stays conservative: it assumes the service is both provided and consumed,
# because soundness beats the relaxation.


@dataclass(frozen=True)
class _Drift:
    """One reason a replacement interface is not compatible."""
    method: str | None      # the offending method, or None for a service-wide clash
    kind: str               # added | removed | signature | emission | commutative
    reason: str             # human-readable "what and why"


def _caps_widen(old_caps: tuple | None, new_caps: tuple | None) -> bool:
    """Does the new emission touch *more* capabilities than the old one?

    `None` is bare `emission` — "any capability", the widest set — so a bare
    emission never widens (it was already unbounded) and narrowing *to* a
    bare emission from a scoped one always does."""
    if old_caps is None:
        return False
    if new_caps is None:
        return True
    return not set(new_caps) <= set(old_caps)


def _caps_str(caps: tuple | None) -> str:
    return "any" if caps is None else "[" + ", ".join(caps) + "]"


def _service_compatible(new: ServiceDecl, old: ServiceDecl,
                        providers_retained: bool) -> _Drift | None:
    """Is `new` an admissible replacement for the running `old`?

    Returns `None` when compatible, else the first `_Drift` that breaks it.

    Two regimes, because the two sides of an interface use it in opposite
    directions:

    * ``providers_retained`` — a running provider is *not* part of this swap
      and keeps implementing the interface. A provider is validated against
      the *whole* service (A6: every method implemented, arity/async/parameter
      and return types matched, emission/capability classification checked), so
      it conforms to exactly ``old``. Nothing about the interface may change:
      an added method is an unfilled obligation, a retyped one fails A6, a
      newly-scoped emission may outlaw an emission it makes. The only
      admissible replacement is *identity* — this is the pre-§5 behaviour, and
      it is the sound one whenever the provider stays.

    * otherwise — the provider is being replaced by one checked against
      ``new`` in this very compilation, so only running *consumers* constrain
      the change. Their call sites were type-checked against ``old`` and are
      never recompiled, so ``new`` must keep them valid: a method may be
      added, a parameter may only *widen* (contravariant — a value a consumer
      passed still fits), a return may only *narrow* (covariant — the consumer
      still uses the result at the old type), an ``emission`` may be *dropped*
      but never introduced (an unmarked call site silently crossing the
      boundary breaks G4/G8; tightening purity is safe), and ``async`` /
      commutativity are fixed (they change how a call site is written).
    """
    if _service_equal(new, old):
        return None
    strict = providers_retained
    if old.commutative != new.commutative:
        verb = "drops" if old.commutative else "adds"
        return _Drift(None, "commutative",
                      f"the service {verb} its `commutative` promise, which "
                      f"reorders every provider's calls")
    if strict:
        added = [m for m in new.methods if m not in old.methods]
        if added:
            return _Drift(added[0], "added",
                          f"method `{added[0]}` is added, but the running "
                          f"provider does not implement it (A6 completeness)")
    for mname, om in old.methods.items():
        nm = new.methods.get(mname)
        if nm is None:
            return _Drift(mname, "removed",
                          f"method `{mname}` is removed, but a running consumer "
                          f"may still call it")
        if len(nm.params) != len(om.params):
            return _Drift(mname, "signature",
                          f"`{mname}` now takes {len(nm.params)} parameter(s), "
                          f"was {len(om.params)} — existing call sites pass "
                          f"{len(om.params)}")
        for (opn, opt), (npn, npt) in zip(om.params, nm.params):
            if opt == npt:
                continue
            if strict:
                return _Drift(mname, "signature",
                              f"`{mname}` parameter `{npn}` changes from "
                              f"`{opt}` to `{npt}`, but a running provider "
                              f"implements it at `{opt}` (A6)")
            if opt is None:
                # old accepted anything here; a newly-typed position may reject
                # a value the consumer passes
                return _Drift(mname, "signature",
                              f"`{mname}` parameter `{npn}` is now `{npt}`, was "
                              f"untyped — it may reject a value a consumer passes")
            if npt is None:
                continue  # widened to untyped: accepts everything old did
            if not compatible(npt, opt):
                # contravariant: a value the consumer passed must still fit
                return _Drift(mname, "signature",
                              f"`{mname}` parameter `{npn}` narrows from `{opt}` "
                              f"to `{npt}` — a value a consumer passes no longer "
                              f"fits")
        if om.returns != nm.returns:
            if strict:
                return _Drift(mname, "signature",
                              f"`{mname}` return changes from `{om.returns}` to "
                              f"`{nm.returns}`, but a running provider implements "
                              f"it at `{om.returns}` (A6)")
            if om.returns is None:
                pass  # old returned nothing usable; a new return is ignored
            elif nm.returns is None:
                return _Drift(mname, "signature",
                              f"`{mname}` no longer returns a value (was "
                              f"`{om.returns}`), but a consumer uses the result")
            elif not compatible(om.returns, nm.returns):
                # covariant: the consumer still uses the result at the old type
                return _Drift(mname, "signature",
                              f"`{mname}` return widens from `{om.returns}` to "
                              f"`{nm.returns}` — a consumer uses the result as "
                              f"`{om.returns}`")
        if nm.emission and not om.emission:
            return _Drift(mname, "emission",
                          f"`{mname}` becomes an `emission` — a running "
                          f"consumer's call site is unmarked and would silently "
                          f"cross the boundary (G4/G8)")
        if om.emission and not nm.emission and strict:
            # dropping an emission is safe for a consumer, but a retained
            # provider was validated against the emission classification
            return _Drift(mname, "emission",
                          f"`{mname}` is no longer an `emission`, but a running "
                          f"provider implements it as one (A6)")
        if nm.emission and om.emission and _caps_widen(om.capabilities, nm.capabilities):
            return _Drift(mname, "emission",
                          f"`{mname}` widens its emission capabilities from "
                          f"{_caps_str(om.capabilities)} to "
                          f"{_caps_str(nm.capabilities)}")
        if nm.emission and om.emission and strict \
                and _caps_widen(nm.capabilities, om.capabilities):
            # narrowing the capability scope may outlaw an emission the retained
            # provider already makes (G4)
            return _Drift(mname, "emission",
                          f"`{mname}` narrows its emission capabilities from "
                          f"{_caps_str(om.capabilities)} to "
                          f"{_caps_str(nm.capabilities)}, but a running provider "
                          f"emits under the old scope (G4)")
        if nm.async_ != om.async_:
            verb = "becomes" if nm.async_ else "is no longer"
            return _Drift(mname, "signature",
                          f"`{mname}` {verb} `async` — a consumer's call site "
                          f"awaits it differently")
        if nm.commutative != om.commutative:
            verb = "adds" if nm.commutative else "drops"
            return _Drift(mname, "commutative",
                          f"`{mname}` {verb} its `commutative` promise")
    return None


@dataclass(frozen=True)
class _Touchers:
    """Which running components touch a service, from the manifest's key lists."""
    consumers: tuple[str, ...]      # retained components that inject the service
    providers: tuple[str, ...]      # retained components that provide it
    unresolved: bool                # a touched key could not be mapped to a service


def _service_touchers(service_name: str, ambient: dict) -> _Touchers:
    """Running consumers and providers of `service_name`, read off the ambient
    manifest's `inject`/`provides` key lists via the `provision_services`
    key -> service map that `compile_files` supplies.

    `unresolved` is True when a component injects or provides a key we cannot
    resolve to a service: we then cannot rule out that it touches this one, so
    the caller stays conservative."""
    key_service = ambient.get("provision_services") or {}
    consumers: list[str] = []
    providers: list[str] = []
    unresolved = False
    for entry in ambient.get("components") or []:
        name = entry.get("name")
        for key in entry.get("inject") or []:
            svc = key_service.get(key)
            if svc == service_name:
                consumers.append(name)
            elif svc is None:
                unresolved = True
        for key in entry.get("provides") or []:
            svc = key_service.get(key)
            if svc == service_name:
                providers.append(name)
            elif svc is None:
                unresolved = True
    return _Touchers(tuple(dict.fromkeys(consumers)),
                     tuple(dict.fromkeys(providers)), unresolved)


def _admit_service_replacement(program: Program, new: ServiceDecl,
                               old: ServiceDecl, ambient: dict) -> None:
    """Gate a service redeclared against the running manifest (§5/§6.6).

    Admits the replacement when it is compatible with every running component
    that touches the interface — or when nothing running touches it at all.
    Raises a drift diagnostic naming the offending method, why it broke, and
    the affected running component otherwise."""
    touch = _service_touchers(new.name, ambient)
    # a retained provider (or an unresolved key that might be one) pins the
    # shared methods' parameter/return types to the old interface (A6)
    providers_retained = bool(touch.providers) or touch.unresolved
    drift = _service_compatible(new, old, providers_retained)
    if drift is None:
        return
    # nothing running consumes or provides this service, and every touched key
    # resolved: the change breaks no one, so it is admissible however it drifts
    if not touch.consumers and not touch.providers and not touch.unresolved:
        return
    raise _drift_error(program, new, drift, touch)


def _drift_error(program: Program, new: ServiceDecl, drift: _Drift,
                 touch: _Touchers) -> RevlError:
    # name every running component that touches the interface — a removed or
    # retyped method may strand a consumer's call site or a retained provider's
    # `provide` block, so both are "affected"
    affected = tuple(dict.fromkeys(touch.consumers + touch.providers))
    who = (", ".join(f"`{n}`" for n in affected)
           if affected else "a running consumer")
    # the phrase "differs from the running manifest" is load-bearing: the
    # structured-diagnostics table classifies it as an admission (G2) rejection
    message = (f"service `{new.name}` differs from the running manifest in a "
               f"way that breaks {who}: {drift.reason}")
    hint = ("an admitted component may add methods and widen an interface, but "
            "it may not break a running consumer's or provider's use of it "
            "(interface drift, DESIGN §6.6)")
    steps = [
        TraceStep(new.name, "service", program.filename, new.line,
                  f"{drift.kind}: {drift.reason}"),
    ]
    for name in affected:
        role = "consumer" if name in touch.consumers else "provider"
        steps.append(TraceStep(name, role, None, None,
                               f"running {role} of `{new.name}`"))
    why = WhyTrace(kind="interface-drift", subject=new.name, shape=SET,
                   steps=steps)
    # the code/category are left to diagnostics.classify: the load-bearing
    # phrase above routes every admission drift to (G2, "admission"), the same
    # classification the exact-match gate carried, so downstream consumers see
    # no change of shape.
    return RevlError(program.filename, new.line, message, hint=hint, why=why)


# --------------------------------------------------- state hand-off (§5, item 53)
#
# The fourth admission concern: a *stateful* provider's live world (its
# effect-created Map, a session store's entries) is torn down LIFO on a swap
# and the successor starts cold (item 23 drains in-flight calls but carries no
# state). For a stateless provider that is correct; for a stateful one it is
# "restart with extra steps" — the data is silently dropped.
#
# Item 53 closes the gap the Erlang way (`code_change`): a component may name a
# `handoff` — the declared *shape* of the state it exports when replaced, and
# the shape it accepts when it is the replacement. The gate proves the two
# sides *fit* before any swap threads the value across, reusing the very §5
# compatibility relation the interface-drift check rests on
# (`typecheck.compatible`), pointed at STATE instead of interface. The
# threading itself lives in `mcp.session` (the swap path); this is the checked
# admission that makes it sound — a swap whose successor cannot hold the
# predecessor's state is *refused*, not allowed to drop it at runtime.
#
# Orientation. The predecessor *exports* a value of its declared type `E`; the
# successor *accepts* it at its declared type `A`. The value flows old -> new,
# so — exactly the covariant value-flow `_service_compatible` uses for a
# consumer's returns — `A` must accept everything an `E` produces:
# `compatible(expected=A, actual=E)`. When they disagree the swap is refused
# with a §5-shaped why-trace naming the offending key and the two shapes.


def _handoff_compatible(accepted: str | None, exported: str | None) -> bool:
    """May the predecessor's *exported* state (`exported`) be accepted at the
    successor's *accepted* shape? Value-flow is old -> new, so this is the same
    covariant `compatible(expected, actual)` §5 uses at a return position —
    the accepted type must admit everything the exported type produces."""
    return compatible(accepted, exported)


def _admit_handoff_replacement(program: Program, components: list[dict],
                               ambient: dict) -> None:
    """Gate every state hand-off a candidate provider *accepts* against the
    predecessor it replaces (§5/item 53).

    For each candidate component that declares a `handoff` on a key some
    running provider also handed off, the candidate's *accepted* shape must be
    §5-compatible with the predecessor's *exported* shape. Refuses the swap
    (an admission rejection, same shape as interface drift) when they disagree,
    rather than letting the runtime drop the state. A key the successor accepts
    but nothing running exported — or a running export the successor does not
    accept — is not a conflict: the first starts cold, the second opts out of
    inheriting (a stateless successor of a stateful provider is a valid, if
    lossy, choice the author made explicit)."""
    running = ambient.get("handoffs") or {}
    if not running:
        return
    for comp in components:
        h = comp.get("handoff")
        if not isinstance(h, dict):
            continue
        key = h.get("key")
        old = running.get(key)
        if old is None:
            continue  # nothing running exported this key's state — a cold key
        accepted = h.get("type")
        exported = old.get("type")
        if _handoff_compatible(accepted, exported):
            continue
        line = next((c.line for c in program.components
                     if c.name == comp.get("name")), 0)
        raise _handoff_error(program, comp, key, accepted, exported, old, line)


def _handoff_error(program: Program, comp: dict, key: str,
                   accepted: str | None, exported: str | None,
                   old: dict, line: int = 0) -> RevlError:
    old_name = old.get("component") or "the running provider"
    new_name = comp.get("name") or "the replacement"
    # the phrase "differs from the running manifest" routes this through
    # diagnostics.classify to the same (G2, "admission") bucket every other
    # admission refusal carries — a swap refused, nothing changed.
    message = (f"state hand-off on `{key}` differs from the running manifest: "
               f"`{new_name}` accepts `{accepted}`, but `{old_name}` exports "
               f"`{exported}` — the successor cannot hold the predecessor's "
               f"state, and dropping it on the swap would be residue")
    hint = ("a successor's `handoff` type must accept everything the running "
            "provider's `handoff` exports (state compatibility, the §5 relation "
            "pointed at state — docs/state-handoff.md); widen the accepted type "
            "or migrate the shape explicitly")
    steps = [
        TraceStep(f"{new_name}.{key}", "accept", program.filename, line,
                  f"accepts state `{accepted}`"),
        TraceStep(f"{old_name}.{key}", "export", None, None,
                  f"running provider exports state `{exported}`"),
    ]
    why = WhyTrace(kind="state-handoff-drift", subject=key, shape=SET, steps=steps)
    return RevlError(program.filename, line, message, hint=hint, why=why)


# ------------------------------------------------------- boundary policy (§5)
#
# The third leg of the gate (roadmap item 33). Where `_admit_service_replacement`
# above checks *correctness* (a redeclared interface keeps running consumers
# valid), the boundary policy checks *absolute authority*: does any admitted
# component reach a capability it may not? The check is set operations over the
# G8 audit graph, so it lives in `revl.policy`; this is the admission-side
# entry point, additive to the §5 gate — a violation raises the same
# `RevlError`-with-why-trace shape every other refusal here carries.


def admit_under_policy(policy, audit: dict,
                       mcp_components=None) -> None:
    """Refuse admission when the composition breaches the boundary policy.

    `policy` is a `revl.policy.Policy`, `audit` the `revl audit` graph
    (`revl.audit_diff.audit_report`). Raises on the first violation (admission
    is all-or-nothing); returns silently on a clean policy. Kept a thin
    forwarder so the admission module names the third gate leg alongside the
    other two, without importing the policy engine at module load."""
    from .policy import enforce  # noqa: PLC0415 — lazy, additive to §5

    enforce(policy, audit, mcp_components=mcp_components)
