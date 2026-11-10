"""Admission gate and service-compatibility relation (DESIGN.md §5/§6.6).

Split out of `lower.py` (roadmap item 17): the runtime-admission gate that
decides whether a service redeclared against a running manifest is an
admissible replacement, plus the structural helpers it rests on. Pure code
movement — every name here is re-exported from `revl.lower`, so the public
import surface is unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import RevlError
from .parser import Program, ServiceDecl
from .typecheck import compatible
from .why import CHAIN, SET, TraceStep, WhyTrace

#: An un-isolated key lives in the shared realm — mirrors `lower.SHARED_REALM`.
_SHARED_REALM = ""


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
            # issue #1912: the `witnessed[caps]` bound, read back from the IR
            # key only a declared operation carries. `emission` above stays the
            # sound upper bound for every other reader; this is what
            # `_service_compatible` compares the bound against.
            witnessed=(tuple(mspec["witnessed"])
                       if mspec.get("witnessed") is not None else None),
        )
        # issue #1937: the declared taint qualifiers the IR keeps beside the
        # stripped types, read back so `taint.fold_ambient_composition` can
        # hold every unit compiled against this manifest to them
        ps = mspec.get("params") or []
        methods[mname].trusted_params = frozenset(
            i for i, p in enumerate(ps) if p.get("trusted"))
        methods[mname].untrusted_params = frozenset(
            i for i, p in enumerate(ps) if p.get("untrusted"))
        methods[mname].returns_qualifier = mspec.get("returns_qualifier")
        # issue #1952: the comment block the IR carries on the operation, read
        # back so this projection is faithful to the entry it came from — the
        # rebuilt decl re-emits the author's text rather than dropping it. `doc`
        # is not part of the interface relation below (`_service_equal` /
        # `_service_compatible` read named fields), so carrying it cannot make a
        # doc-only edit read as an interface change.
        methods[mname].doc = mspec.get("doc")
    return ServiceDecl(name, methods, 0, commutative=bool(spec.get("commutative")))


def _service_equal(a: ServiceDecl, b: ServiceDecl) -> bool:
    def shape(svc: ServiceDecl):
        return (
            svc.commutative,
            {
                m.name: (tuple(m.params), m.returns, m.emission, m.async_,
                         m.commutative, m.capabilities,
                         tuple(m.witnessed) if m.witnessed is not None else None)
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
    kind: str               # added | removed | signature | emission | witnessed | commutative
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


def _witnessed_caps(decl) -> tuple | None:
    """A `witnessed` declaration's scope in `_caps_widen`'s alphabet: bare
    `witnessed` (the empty tuple) is `None` — "any capability" — exactly as a
    bare `emission` is. `None` for the declaration itself (not witnessed)."""
    if getattr(decl, "witnessed", None) is None:
        return None
    return tuple(decl.witnessed) or None


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
        if nm.witnessed is not None and om.witnessed is None:
            # issue #1912, the `emission` rule one class down: a `witnessed`
            # bound is a LOOSER provider obligation than plain purity, so
            # introducing one lets a retained provider do what it promised it
            # would not — the same loophole as introducing an `emission`.
            return _Drift(mname, "witnessed",
                          f"`{mname}` becomes a `witnessed` operation — a "
                          f"running provider of it was admitted as a plain "
                          f"`fn` and may not perform the witnessed class")
        if om.witnessed is not None and nm.witnessed is None and strict:
            # dropping it is safe for a consumer, but the retained provider was
            # validated against the witnessed bound
            return _Drift(mname, "witnessed",
                          f"`{mname}` is no longer `witnessed`, but a running "
                          f"provider implements it under that bound (A6)")
        if nm.emission and om.emission and _caps_widen(om.capabilities, nm.capabilities):
            return _Drift(mname, "emission",
                          f"`{mname}` widens its emission capabilities from "
                          f"{_caps_str(om.capabilities)} to "
                          f"{_caps_str(nm.capabilities)}")
        if _caps_widen(_witnessed_caps(om), _witnessed_caps(nm)):
            return _Drift(mname, "witnessed",
                          f"`{mname}` widens its witnessed capabilities from "
                          f"{_caps_str(_witnessed_caps(om))} to "
                          f"{_caps_str(_witnessed_caps(nm))}")
        if strict and _caps_widen(_witnessed_caps(nm), _witnessed_caps(om)):
            return _Drift(mname, "witnessed",
                          f"`{mname}` narrows its witnessed capabilities from "
                          f"{_caps_str(_witnessed_caps(om))} to "
                          f"{_caps_str(_witnessed_caps(nm))}, but a running "
                          f"provider performs the witnessed class under the "
                          f"old scope (G4)")
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


_IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b(?!\s*:)")


def _spelling_names(spelling: str | None) -> set[str]:
    """Every name a written type spells: heads, arguments, function parts. A
    name followed by `:` is a field label, not a type."""
    return set(_IDENT.findall(spelling or ""))


def _decl_refs(spec: dict) -> list[str]:
    """The type spellings one declaration is written in terms of."""
    if spec.get("kind") == "variant":
        return [c.get("payload") for c in spec.get("cases") or []
                if c.get("payload")]
    return [t for t in (spec.get("fields") or {}).values() if t]


def _decl_key(spec: dict | None) -> tuple | None:
    """The declaration's content. Fields compare as a SET (the record relation
    is by field set). Type parameters are read by POSITION, so `Box[T] = {v: T}`
    and `Box[U] = {v: U}` are one declaration while `P[T, U]` and `P[U, T]`
    over the same body are not.

    Variant cases compare IN ORDER, on purpose: case order is observable. The
    WIT export (`export_wit`) numbers a variant's cases by position on the
    canonical-ABI wire and the JSON schema lists them in order, so a reorder is
    a different encoding, not a spelling change. It is refused with the other
    declaration changes."""
    if not isinstance(spec, dict):
        return None
    params = tuple(spec.get("params") or ())
    slot = {p: f"${i}" for i, p in enumerate(params)}

    def norm(ty):
        if not isinstance(ty, str) or not slot:
            return ty
        return _IDENT.sub(lambda m: slot.get(m.group(0), m.group(0)), ty)

    if spec.get("kind") == "variant":
        return ("variant", len(params), tuple(
            (c.get("name"), norm(c.get("payload")))
            for c in spec.get("cases") or []))
    return ("record", len(params), tuple(sorted(
        (k, norm(v)) for k, v in (spec.get("fields") or {}).items())))


def _decl_differs(name: str, mine: dict, theirs: dict,
                  seen: set[str] | None = None) -> bool:
    """Does `name` mean something different on the two sides: declared on one
    only, declared differently, or declared alike over a declaration it uses
    that differs? A name declared on NEITHER side is an opaque nominal and
    means itself. `seen` makes a recursive type terminate (a revisit adds no
    difference the first visit did not already find)."""
    seen = set() if seen is None else seen
    if name in seen:
        return False
    seen.add(name)
    a, b = mine.get(name), theirs.get(name)
    if a is None and b is None:
        return False
    if _decl_key(a) != _decl_key(b):
        return True
    params = set(a.get("params") or ())
    return any(_decl_differs(ref, mine, theirs, seen)
               for t in _decl_refs(a) for ref in _spelling_names(t) - params)


def _changed_names(accepted: str | None, exported: str | None,
                   accepted_types: dict | None,
                   exported_types: dict | None) -> list[str]:
    """The nominal names both spellings mention whose meaning differs between
    the successor's declarations and the predecessor's, sorted."""
    mine, theirs = accepted_types or {}, exported_types or {}
    shared = _spelling_names(accepted) & _spelling_names(exported)
    return sorted(n for n in shared if _decl_differs(n, mine, theirs))


def _own_differences(names: list[str], mine: dict, theirs: dict) -> list[str]:
    """The declarations, among `names` and everything they reach, that differ
    on their own: the ones worth showing, since a name whose body is spelled
    the same but reaches a changed one differs only through it."""
    out: set[str] = set()
    todo, seen = list(names), set()
    while todo:
        n = todo.pop()
        if n in seen or (n not in mine and n not in theirs):
            continue
        seen.add(n)
        if _decl_key(mine.get(n)) != _decl_key(theirs.get(n)):
            out.add(n)
        for side in (mine, theirs):
            spec = side.get(n)
            if spec:
                params = set(spec.get("params") or ())
                todo += [r for t in _decl_refs(spec)
                         for r in _spelling_names(t) - params]
    return sorted(out)


def _render_decl(name: str, spec: dict | None) -> str:
    """One declaration as backticked text, or the fact that it has none."""
    if not isinstance(spec, dict):
        return f"`{name}` is not declared"
    params = list(spec.get("params") or ())
    head = f"{name}[{', '.join(params)}]" if params else name
    if spec.get("kind") == "variant":
        cases = " | ".join(
            f"{c['name']}({c['payload']})" if c.get("payload") else c["name"]
            for c in spec.get("cases") or [])
        return f"`{head} = {cases}`"
    fields = ", ".join(f"{k}: {v}" for k, v in (spec.get("fields") or {}).items())
    return f"`{head} = {{{fields}}}`"


def _handoff_detail(accepted: str | None, exported: str | None,
                    accepted_types: dict | None,
                    exported_types: dict | None) -> tuple[str, str]:
    """What each spelling means where the two versions disagree about it, as
    a suffix for the refusal text (`""` when the spellings carry the whole
    difference, which is every case that spells a nominal the same way and
    declares it alike)."""
    mine, theirs = accepted_types or {}, exported_types or {}
    shown = _own_differences(
        _changed_names(accepted, exported, mine, theirs), mine, theirs)
    if not shown:
        return "", ""
    return tuple(  # type: ignore[return-value]
        " (" + "; ".join(_render_decl(n, side.get(n)) for n in shown) + ")"
        for side in (mine, theirs))


def _handoff_compatible(accepted: str | None, exported: str | None,
                        accepted_types: dict | None = None,
                        exported_types: dict | None = None) -> bool:
    """May the predecessor's *exported* state (`exported`) be accepted at the
    successor's *accepted* shape? Value-flow is old -> new, so this is the same
    covariant `compatible(expected, actual)` §5 uses at a return position —
    the accepted type must admit everything the exported type produces.

    A nominal name is only a NAME: `St` in the running version and `St` in the
    replacement may be two different records, and `compatible` takes `expected
    == actual` for the same name without consulting any declaration. The two
    declared-type tables are the predecessor's and the successor's own, which
    one table cannot stand in for, so a name both spellings mention whose
    declaration (or anything it reaches) differs between the versions is
    renamed per side before the comparison. The two sides then differ as
    opaque nominals, which `compatible` refuses, exactly as it refuses two
    differently named records.

    Where a table is absent, exactly:

    * A name declared on NEITHER side (a builtin, an opaque nominal) is never
      renamed: equal spellings are equal, as before.
    * A name declared on only ONE side is a changed name and is refused, even
      if its spelling is unchanged. That includes a running document that
      carries no `types` at all (a hand-built or older manifest) meeting a
      candidate that declares the name: the predecessor's declaration cannot
      be read, so sameness cannot be proved and the gate fails closed. The
      remedy is to supply the running document with its `types`.
    * With no table on either side nothing is renamed and this is
      `compatible(accepted, exported)` unchanged.
    * A manifest projection with no `components` exports no hand-off, so this
      is never reached."""
    changed = _changed_names(accepted, exported, accepted_types, exported_types)
    if changed:
        accepted = _tag_names(accepted, changed, "@successor")
        exported = _tag_names(exported, changed, "@predecessor")
    return compatible(accepted, exported)


def _tag_names(spelling: str | None, names: list[str], tag: str) -> str | None:
    if spelling is None:
        return None
    wanted = set(names)
    return _IDENT.sub(lambda m: m.group(0) + tag if m.group(0) in wanted
                      else m.group(0), spelling)


# ------------------------------------------- declared types behind a service
#
# A service signature spells a record or variant by NAME, and `compatible`
# takes the same name on both sides to be the same type. The running manifest
# fixes what each name meant when its consumers and providers were checked
# (`Rec = {a: Int}`), and a candidate may declare the same name differently
# (`Rec = {a: Int, b: Int}`) over a signature whose spelling did not move, or
# without redeclaring the service at all. Both are the hand-off gate's
# problem (see `_handoff_compatible`), read at the service boundary: the
# running service's signature is resolved against the running declarations and
# against the candidate's, and a name it reaches whose meaning differs between
# the two is a signature difference, whatever the spelling says.


def _service_type_drift(old: ServiceDecl, candidate_types: dict | None,
                        running_types: dict | None) -> _Drift | None:
    """The first method of the running service `old` whose signature reaches a
    declared type that means something else in `candidate_types` than in
    `running_types`, else `None`.

    The comparison is the hand-off gate's (`_decl_differs`): a declaration is
    compared for identity, and so is everything it reaches, so a record gaining,
    losing or retyping a field, a `Secret` field becoming plain, or a change
    nested under another record, a `Map` or a `List` is all a difference. It
    is stricter than a field-by-field relation on purpose, as it is there. A
    name declared on neither side is an opaque nominal and means itself; a name
    declared on one side only differs, so a running manifest that carries no
    `types` makes every name the candidate declares unprovable and refused."""
    mine, theirs = candidate_types or {}, running_types or {}
    for mname, method in old.methods.items():
        spelled = [t for _, t in method.params if t]
        if method.returns:
            spelled.append(method.returns)
        names = set().union(*(_spelling_names(t) for t in spelled)) \
            if spelled else set()
        changed = sorted(n for n in names if _decl_differs(n, mine, theirs))
        if not changed:
            continue
        shown = _own_differences(changed, mine, theirs)
        both = "; ".join(
            f"the running manifest declares {_render_decl(n, theirs.get(n))} "
            f"and the candidate declares {_render_decl(n, mine.get(n))}"
            for n in shown)
        used = ", ".join(f"`{n}`" for n in changed)
        return _Drift(
            mname, "signature",
            f"`{mname}` uses {used}, whose declaration differs ({both}) — a "
            f"running consumer or provider was checked against the running "
            f"declaration")
    return None


def _admit_service_types(program: Program, running: dict[str, ServiceDecl],
                         redeclared: dict[str, ServiceDecl], ambient: dict,
                         types: dict) -> None:
    """Gate the declared types behind every running service a running
    component touches (§5/§6.6, the half `_admit_service_replacement` cannot
    see because it reads signatures as spellings).

    `running` is the running manifest's services, `redeclared` the candidate's
    own, `types` the candidate's declared-type table and
    `ambient["handoff_types"]` the running version's (the manifest's `types`).
    The candidate's meaning for a name is its own declaration where it has one
    and the running declaration otherwise: a name it does not mention keeps
    the meaning every running component was checked against, and a record it
    does not redeclare still reaches a record it does.

    It runs for a service the candidate does not redeclare too, which is the
    case a type-only edit hides in. A service nothing running consumes or
    provides is not checked, so an edit to the types of an untouched service
    does not refuse (and neither does an edit to a type no touched service
    reaches). The refusal is the interface-drift refusal, naming the running
    components it breaks and both declarations."""
    running_types = ambient.get("handoff_types") or {}
    effective = {**running_types, **(types or {})}
    for name, old in running.items():
        touch = _service_touchers(name, ambient)
        if not (touch.consumers or touch.providers or touch.unresolved):
            continue
        drift = _service_type_drift(old, effective, running_types)
        if drift is not None:
            raise _drift_error(program, redeclared.get(name) or old, drift,
                               touch)


def _admit_handoff_replacement(program: Program, components: list[dict],
                               ambient: dict,
                               types: dict | None = None) -> None:
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
    lossy, choice the author made explicit).

    `types` is the candidate's declared-type table and
    `ambient["handoff_types"]` the running version's: each spelling means what
    ITS version's declarations say (see `_handoff_compatible`)."""
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
        running_types = ambient.get("handoff_types")
        if _handoff_compatible(accepted, exported, types, running_types):
            continue
        line = next((c.line for c in program.components
                     if c.name == comp.get("name")), 0)
        detail = _handoff_detail(accepted, exported, types, running_types)
        raise _handoff_error(program, comp, key, accepted, exported, old, line,
                             detail)


def _handoff_error(program: Program, comp: dict, key: str,
                   accepted: str | None, exported: str | None,
                   old: dict, line: int = 0,
                   detail: tuple[str, str] = ("", "")) -> RevlError:
    accepted_at, exported_at = detail
    old_name = old.get("component") or "the running provider"
    new_name = comp.get("name") or "the replacement"
    # the phrase "differs from the running manifest" routes this through
    # diagnostics.classify to the same (G2, "admission") bucket every other
    # admission refusal carries — a swap refused, nothing changed.
    message = (f"state hand-off on `{key}` differs from the running manifest: "
               f"`{new_name}` accepts `{accepted}`{accepted_at}, but "
               f"`{old_name}` exports `{exported}`{exported_at} — the successor "
               f"cannot hold the predecessor's state, and dropping it on the "
               f"swap would be residue")
    if accepted_at or exported_at:
        # the spellings agree and the declaration behind a name moved, so
        # "widen the accepted type" would not change the verdict
        hint = ("a successor's `handoff` must hold everything the running "
                "provider's `handoff` exports (state compatibility, the §5 "
                "relation pointed at state — docs/state-handoff.md), and a "
                "record or variant it names must be declared exactly as the "
                "running version declares it: keep the declaration unchanged, "
                "or migrate the shape explicitly")
    else:
        hint = ("a successor's `handoff` type must accept everything the "
                "running provider's `handoff` exports (state compatibility, "
                "the §5 relation pointed at state — docs/state-handoff.md); "
                "widen the accepted type or migrate the shape explicitly")
    steps = [
        TraceStep(f"{new_name}.{key}", "accept", program.filename, line,
                  f"accepts state `{accepted}`{accepted_at}"),
        TraceStep(f"{old_name}.{key}", "export", None, None,
                  f"running provider exports state `{exported}`{exported_at}"),
    ]
    why = WhyTrace(kind="state-handoff-drift", subject=key, shape=SET, steps=steps)
    return RevlError(program.filename, line, message, hint=hint, why=why)


# ------------------------------- withdrawn provisions (§5, item 186 / issue #86)
#
# The replacement leg of ambient admission. `compile_files(X, manifest=M,
# replacing=R)` withdraws `R` — plus every running component `X` redeclares by
# name — from the running composition and links the remainder against `X`. G2
# and G3 then run over `M \ R ∪ X`, which is the right union for CONFLICTS: two
# providers of one key, a cycle that closes through the manifest. What that
# union cannot see is a LOSS. A key `R` provided and nothing in `M \ R ∪ X`
# provides again simply vanishes from the provider table, and a retained running
# consumer of it produces no edge at all — the linker has nothing to complain
# about, so the swap is admitted and the consumer deactivates into PENDING at
# runtime, holding its resources, with no diagnostic anywhere in the admission.
#
# `revl plan` already PREDICTS that outcome (its `diverted` list: "a required
# provision is withdrawn and nothing replaces it — the component deactivates and
# stays PENDING"), which is the proof that the gate could see it and did not.
#
# docs/design/186-ambient-admission-guarantees.md decides the direction: "a
# replacement that would leave a live consumer unmet is REFUSED by default …
# there is no opt-in to strand consumers; a composition that wants a key gone
# unloads the consumer first". That is item 460's rule (a documented refusal
# with the running composition intact beats an apparently successful replacement
# that quietly breaks it) and the row-level rule of docs/composition-rows.md (a
# provision removed upstream is a refusal), applied at the admission gate.
#
# Scope, kept narrow on purpose. This refuses only a provision that the running
# manifest HAD and this admission DROPS. A requirement that was already unmet
# before the admission stays admissible — an incremental composition legitimately
# admits a consumer before its provider, and the runtime holds it PENDING until
# the provider arrives. Only the transition met -> unmet is a refusal, because
# only that one is caused by the admission under review.


def _realm_of(entry: dict, key: str) -> str:
    """The realm `entry` isolates `key` into, shared when it isolates nothing.
    Reads the manifest shape (`isolate`) that both a running entry and a freshly
    lowered component carry."""
    return (entry.get("isolate") or {}).get(key, _SHARED_REALM)


def _admit_provision_withdrawal(program: Program, components: list[dict],
                                ambient: dict,
                                templates: set | None = None) -> None:
    """Refuse an admission that withdraws a provision a RETAINED running
    component still requires (§5, roadmap item 186).

    `ambient["withdrawn"]` is what this admission drops from the running
    manifest (`replacing=` plus every running component the candidate
    redeclares by name); `ambient["components"]` is what it retains. A key the
    withdrawn set provided, that neither the retained set nor the candidate
    provides again in the same realm, is LOST — and a retained component that
    requires a lost key is refused by name, against the candidate that dropped
    it.

    A routed key (`realms(...)`, item 162) is left to the link-time per-realm
    provider check, which already refuses a route leg with no provider; checking
    it here too would report one loss twice."""
    withdrawn_entries = ambient.get("withdrawn") or []
    if not withdrawn_entries:
        return                      # a cold start or a pure addition: nothing lost
    templates = templates or set()
    retained = ambient.get("components") or []

    # what the RESULTING composition provides, per (key, realm): the retained
    # running entries plus the newly admitted components. A spawn template is
    # not statically composed (each instance gets its own fresh local realm), so
    # its provisions cannot stand in for a withdrawn static one.
    after: set = set()
    for entry in retained:
        for key in entry.get("provides") or []:
            after.add((key, _realm_of(entry, key)))
    for comp in components:
        if comp.get("name") in templates:
            continue
        for key in comp.get("provides") or []:
            after.add((key, _realm_of(comp, key)))

    lost: dict[tuple[str, str], str] = {}
    for entry in withdrawn_entries:
        for key in entry.get("provides") or []:
            realm = _realm_of(entry, key)
            if (key, realm) not in after:
                lost.setdefault((key, realm), entry.get("name") or "")
    if not lost:
        return

    for entry in retained:
        routed = entry.get("routes") or {}
        for key in entry.get("inject") or []:
            if key in routed:
                continue
            realm = _realm_of(entry, key)
            if (key, realm) in lost:
                raise _withdrawal_error(program, components,
                                        entry.get("name") or "a running component",
                                        key, realm, lost[(key, realm)])


def _withdrawal_error(program: Program, components: list[dict], consumer: str,
                      key: str, realm: str, provider: str) -> RevlError:
    # the candidate component that replaced the withdrawn provider carries the
    # line; a withdrawal named only by `replacing=` has no source position in
    # this text, so the refusal reports line 0 rather than inventing one.
    line = next((c.line for c in program.components if c.name == provider), 0)
    where = "" if realm == _SHARED_REALM else f" in realm `{realm}`"
    message = (f"this admission withdraws the running provider of `{key}`{where} "
               f"(`{provider}`) and nothing provides it again, but the running "
               f"component `{consumer}` still requires it (G2)")
    hint = ("a replacement must keep every provision a retained running component "
            f"consumes: provide `{key}`{where} in this admission, or unload "
            f"`{consumer}` first. Admitting it would deactivate `{consumer}` into "
            "PENDING with its resources still held — a refusal with the running "
            "composition intact is the better outcome (docs/design/"
            "186-ambient-admission-guarantees.md)")
    why = WhyTrace(
        kind="unmet-requirement", subject=key, shape=CHAIN,
        steps=[
            TraceStep(provider, "provider", None, None,
                      f"running provider of `{key}`{where}, withdrawn by this "
                      f"admission"),
            TraceStep(consumer, "consumer", None, None,
                      f"retained running component, requires `{key}`{where}"),
        ])
    return RevlError(program.filename, line, message, hint=hint, why=why,
                     code="G2", category="admission")


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
                       mcp_components=None, *, profile=None) -> None:
    """Refuse admission when the composition breaches the boundary policy.

    `policy` is a `revl.policy.Policy`, `audit` the `revl audit` graph
    (`revl.audit_diff.audit_report`). Raises on the first violation (admission
    is all-or-nothing); returns silently on a clean policy. Kept a thin
    forwarder so the admission module names the third gate leg alongside the
    other two, without importing the policy engine at module load. `profile`
    (item 274) is the untrusted-author admission profile, threaded so the
    navigable-refusal map is redacted for an untrusted author."""
    from .policy import enforce  # noqa: PLC0415 — lazy, additive to §5

    enforce(policy, audit, mcp_components=mcp_components, profile=profile)
