"""`revl audit --diff` — the authority-drift gate.

This is the *authority* axis of the agent-gate story, deliberately distinct
from admission (which checks *correctness* — that running consumers stay
valid). Audit-diff answers a different question: between two generations of a
component, did the new one quietly WIDEN what it reaches outside the system?

The G8 boundary surface (`revl audit --json`) is the enumerable set of
boundary crossings — every emission call site and every reached host extern,
per component. A *crossing* here is one such reach:

    emit:<component>:<service.method>   an emission the component performs
    host:<component>:<extern-name>      host code the component reaches

Diffing two audits over that set gives three buckets:

    added      crossings present in NEW but not PREV  -> WIDENING (fails)
    removed    crossings present in PREV but not NEW  -> narrowing (safe)
    unchanged  crossings present in both

Adding authority is the dangerous direction, so **unacknowledged additions
fail** (nonzero exit). Removals and unchanged crossings always pass — giving
up authority never needs a gate. An intended widening is accepted with an
explicit ack (`--accept <crossing>` repeatable, or `--accept-all`); the ack
token is exactly the string printed after the `+` for each addition.
"""

from __future__ import annotations

from .cardinality import cardinality
from .distribute import distributability


def audit_report(ir: dict) -> dict:
    """Build the same dict `revl audit --json` emits, from a compiled ir.

    Reuses the one authoritative boundary computation (`_boundary`) rather
    than recomputing the surface a second, divergent way.
    """
    from .__main__ import _boundary  # noqa: PLC0415 — lazy, avoids import cycle

    boundary = _boundary(ir)
    manifest = ir.get("manifest") or {}
    declared_externs = [
        {"name": ext["name"], "class": ext.get("class"),
         "backends": sorted((ext.get("bodies") or {}).keys()),
         # item 373: carry the reach so the drift gate can read it. Absent unless
         # declared — a bare emission's audit entry is byte-identical to today's.
         **({"reach": ext["reach"]} if ext.get("reach") else {}),
         # item 309: carry the idempotency register so the `--recovery` view and
         # the register/idempotent-teardown policy floors read it. Absent unless
         # declared — a non-idempotent extern's audit entry is byte-identical.
         **({"register": ext["register"]} if ext.get("register") else {}),
         **({"undo_idempotent": True} if ext.get("undo_idempotent") else {}),
         **({"idempotent": True} if ext.get("idempotent") else {}),
         **({"idempotency_key": ext["idempotency_key"]}
            if ext.get("idempotency_key") else {})}
        for ext in ir.get("externs") or []
    ]
    return {
        "manifest": manifest,
        "boundary": boundary,
        "externs": declared_externs,
        # item 309/290: the per-capability-token register floor input. Maps each
        # capability token an idempotent extern scopes to its declaration
        # register, so a `capability <glob> requires register <level>` (290) can
        # refuse below the floor. Absent tokens carry no register.
        "capability_registers": _capability_registers(ir),
        # item 309: the recovery surface the `requires idempotent-teardown`
        # (strength) floor reads — every inverse, deferred emission, and
        # compensation, with its declaration register (or None for a fenced,
        # unregistered entry).
        "recovery_surface": _recovery_surface(ir),
        "distributability": distributability(ir),
        # item 260 (docs/design/260-emission-cardinality-bounds.md): the
        # per-component crossing-count ceilings. A top-level key next to
        # `distributability`, always present (empty `{}` when nothing crosses -
        # the LOW-finding precision). The per-component `boundary[...]` entries
        # are NOT extended, so a crossing-free component's boundary entry stays
        # byte-identical (§1, Exit-5).
        "cardinality": cardinality(ir),
        # item 259 slice 1 (docs/design/259-checked-parallel-emissions.md): the
        # derived parallelizable partition of each component's straight-line
        # emission runs. ADDITIVE and PRESENT ONLY when some component has a
        # group of size > 1 (a body with no parallelizable group renders
        # byte-identically to before - the common case must not perturb the
        # audit surface or its goldens). Each group is `{"group": [indices]}`,
        # the emission-step indices a later slice's runtime may fire
        # concurrently; a singleton group is a plain sequential emission and is
        # elided from the render (only components carrying a real parallel group
        # appear). This is the slice-1 exit surface: the derivation is
        # inspectable before any runtime consumes it.
        **_parallel_plan_surface(ir),
        # item 256 Slice 2 (docs/design/256-capability-bound-secrets.md §5): the
        # audit secrets table. NAME and CAPABILITY only - never the value, and no
        # length, hash, or timing (§5a, attack A5). The value lives solely in the
        # driver's run-time `_REVL_SECRETS`, never in the IR, so there is nothing
        # here to leak. ADDITIVE and PRESENT ONLY when the program binds a secret,
        # so a secret-free composition's audit surface is byte-identical to before
        # (the same conditional-presence discipline as `parallel_plan` above).
        # Binding a secret, or rebinding it to a wider capability, surfaces as a
        # `secret:<capability>:<name>` crossing the drift gate flags as a widening
        # (see `crossings`); removing a binding is a narrowing.
        **_secrets_surface(ir),
    }


def _secrets_table(ir: dict) -> list:
    """The audit secrets table: one `{"name","capability"}` row per bound secret
    (item 256, Slice 2, §5). NAME and CAPABILITY only - the value is NEVER present
    (it lives solely in the driver's run-time `_REVL_SECRETS`, never in the IR),
    and there is no length, hash, or timing field (§5a, attack A5). Built purely
    statically from `ir["secrets"]`, the manifest-visible rows Slice 1 lowered."""
    return [{"name": row["name"], "capability": row["capability"]}
            for row in ir.get("secrets") or []]


def _secrets_surface(ir: dict) -> dict:
    """The additive `secrets` audit key, or `{}` when the program binds no secret,
    so a secret-free composition's audit surface stays byte-identical to before
    (the same conditional-presence discipline as `_parallel_plan_surface`)."""
    table = _secrets_table(ir)
    return {"secrets": table} if table else {}


def _parallel_plan_surface(ir: dict) -> dict:
    """The additive `parallel_plan` audit key, or `{}` when nothing is
    parallelizable (so the surface is byte-identical for the common case). Only
    components with a group of size > 1 are listed, each group wrapped
    `{"group": [indices]}` in the derived plan order (item 259 §8, S2.4)."""
    from .parallel import has_parallel_group, parallel_plan  # noqa: PLC0415

    plan = parallel_plan(ir)
    if not has_parallel_group(plan):
        return {}
    surface = {
        comp: [{"group": group} for group in groups]
        for comp, groups in plan.items()
        if any(len(group) > 1 for group in groups)
    }
    return {"parallel_plan": surface}


def _recovery_surface(ir: dict) -> list:
    """Every reversible boundary crossing that could be replayed on recovery,
    with its idempotency register (item 309). A witnessed/acquire extern's `undo`
    is an INVERSE; a `deferred` emission is an OWED-EMISSION; an emission's
    `compensate` is a COMPENSATION. `register` is `None` for an entry with no
    idempotency claim (a fenced/human-finish entry the strength floor refuses)."""
    out: list = []
    for ext in ir.get("externs") or []:
        cls = ext.get("class")
        register = ext.get("register")
        if ext.get("undo") is not None or cls == "witnessed":
            out.append({"name": ext["name"], "kind": "inverse",
                        "register": register})
        if ext.get("deferred"):
            out.append({"name": ext["name"], "kind": "owed-emission",
                        "register": register})
        if ext.get("compensate") is not None:
            out.append({"name": ext["name"], "kind": "compensation",
                        "register": register})
    return out


def _capability_registers(ir: dict) -> dict:
    """Map each capability token an idempotent extern scopes to its declaration
    register (item 309/290, §3.2). An extern's register attaches to every
    capability token in its `capabilities` scope; a token declared by several
    externs takes the STRONGEST register (the floor the policy can safely
    assert). Absent for a token no idempotent extern scopes."""
    from .lower import _REGISTER_RANK  # noqa: PLC0415 — the partial order

    out: dict = {}

    def _record(tokens, register: str) -> None:
        for token in tokens:
            prior = out.get(token)
            if prior is None or _REGISTER_RANK.get(register, -1) \
                    > _REGISTER_RANK.get(prior, -1):
                out[token] = register

    for ext in ir.get("externs") or []:
        register = ext.get("register")
        if register:
            _record(ext.get("capabilities") or [ext.get("name")], register)
    # service-method emissions carry the same item-44/309 `idempotent` claim; a
    # keyed method registers `keyed`, a bare one `declared` (the keyed form
    # extends to service methods, §1b).
    for method in _iter_service_methods(ir):
        if not method.get("idempotent"):
            continue
        register = "keyed" if method.get("idempotency_key") else "declared"
        _record(method.get("capabilities") or [], register)
    return out


def _iter_service_methods(ir: dict):
    for svc in (ir.get("services") or {}).values():
        for method in (svc.get("methods") or {}).values():
            yield method


def crossings(audit: dict) -> set[str]:
    """The enumerable set of boundary crossings in an audit, as ack tokens.

    A crossing token is stable across generations for the same reach, so set
    difference is the whole diff. Four kinds, all drawn from the per-component
    G8 boundary table (the last two only when item 249 taint is in play):

        emit:<component>:<label>          an emission call site
        host:<component>:<name>           a reached host extern
        taint:<component>:<origin>        a value of <origin> reaches an emission here
        declassify:<component>:<origin>   an untrusted value of <origin> is declassified here
        secret:<capability>:<name>        a secret bound to a capability (item 256)

    The two taint tokens flow through `diff_crossings`/`evaluate` unchanged, so a
    newly-appearing `taint:` (web content newly routed into a send) or
    `declassify:` (a newly-added `endorse`) is a *widening* that fails the drift
    gate — the same mechanism that already catches "one more emission" now
    catching "one more declassification" (item 249, Decision 5).

    The `secret:` token (item 256, Slice 2) is a composition-level crossing, not a
    per-component one: it is drawn from the top-level `secrets` table, so binding a
    new secret, or rebinding one to a *wider* capability (a different token), is an
    addition the gate flags as a widening; removing a binding is a narrowing. The
    token carries name and capability only (the value is never in the audit at
    all), so the drift gate reveals the authority fact (which key, which
    capability), never the secret.
    """
    out: set[str] = set()
    for component, stats in (audit.get("boundary") or {}).items():
        for label in stats.get("emissions") or []:
            out.add(f"emit:{component}:{label}")
        for extern in stats.get("externs") or []:
            out.add(f"host:{component}:{extern['name']}")
        taint = stats.get("taint") or {}
        for origin in taint.get("reaches") or []:
            out.add(f"taint:{component}:{origin}")
        for origin in taint.get("declassify") or []:
            out.add(f"declassify:{component}:{origin}")
    # item 256 Slice 2: the bound-secret crossings, read from the composition-level
    # `secrets` table (name + capability only, never a value). Absent for a
    # secret-free composition, so its crossing set is byte-identical to before.
    for row in audit.get("secrets") or []:
        out.add(f"secret:{row['capability']}:{row['name']}")
    return out


def _reach_map(audit: dict) -> dict[str, dict | None]:
    """Extern name -> its reach dict (`{"kind","target"}`) or None (unconfined).

    The reach is a property of the EXTERN, not of a per-component crossing, so it
    is read from the audit's flat `externs` list rather than the boundary table.
    A bare emission has no `reach` key, which reads here as None = unconfined.
    """
    return {ext["name"]: ext.get("reach")
            for ext in (audit.get("externs") or [])}


def diff_reach(prev: dict, new: dict) -> dict:
    """Diff the REACH of every extern present in BOTH audits (item 373).

    The reach names what an irreversible crossing is bounded to. Adding/removing
    a crossing is already handled by `diff_crossings`; this catches the drift a
    crossing-set diff cannot see — an extern that STAYS but loosens its bound:

        confined(T) -> unconfined         WEAKENING (a removed bound)
        confined(a) -> confined(b)        WEAKENING (bound moved; b need not ⊇ a)
        unconfined  -> confined(T)        tightening (safe — a bound gained)
        unchanged                         stable

    A weakening is the dangerous direction — the same shape as a new crossing —
    so its token feeds `evaluate`'s `unacknowledged`. Only externs in both audits
    are compared: a newly-added confined extern is already flagged by its
    `host:` crossing, and a removed one gave up its authority entirely.
    """
    prev_reach = _reach_map(prev)
    new_reach = _reach_map(new)
    weakened: list[str] = []
    tightened: list[str] = []
    for name in prev_reach.keys() & new_reach.keys():
        before, after = prev_reach[name], new_reach[name]
        if before == after:
            continue
        if before is None:
            # unconfined -> confined: a bound was gained. Safe.
            tightened.append(f"reach-tightened:{name}")
        else:
            # confined -> unconfined, or confined -> a different bound. Both
            # loosen or move what the crossing is bounded to — reviewable.
            weakened.append(f"reach-weakened:{name}")
    return {"reach_weakened": sorted(weakened),
            "reach_tightened": sorted(tightened)}


def diff_crossings(prev: dict, new: dict) -> dict:
    """Compare two audits over their G8 boundary surfaces.

    Returns sorted `added` / `removed` / `unchanged` crossing-token lists, plus
    the reach drift (`reach_weakened` / `reach_tightened`, item 373).
    """
    prev_set = crossings(prev)
    new_set = crossings(new)
    return {
        "added": sorted(new_set - prev_set),
        "removed": sorted(prev_set - new_set),
        "unchanged": sorted(new_set & prev_set),
        **diff_reach(prev, new),
    }


# --------------------------------------------------------------------------
# Slice 2 (item 261 §7 / item 64 debt): the audit-surface differs that HARDEN
# the `audit_diff` foundation. Today `diff_crossings` and `diff_reach` are the
# only two differs; the surfaces below (`boundary[*].capabilities`,
# `externs[*].backends`, `recovery_surface`, `capability_registers`,
# `cardinality`) were carried in `audit_report` but differenced by nothing, so a
# scope widening on a STABLE crossing, a new backend host body, a dropped
# recovery inverse, a weakened idempotency register, and a raised emission
# ceiling all escaped the crossing/reach diff. Each helper below closes one such
# blind spot. Every bucket is sorted, so the diff is a pure, deterministic
# function of the two audits (§4, the determinism invariant). Each mirrors the
# `diff_reach` shape: a WIDENED bucket (the dangerous direction) and a TIGHTENED
# bucket (the safe direction), keyed by structured tokens the changelog renders.


def _scope_covers(wide, narrow) -> bool:
    """True when the capability scope `wide` reaches everything `narrow` does:
    every token in `narrow` is matched by some glob token in `wide`. `send.*`
    covers `send.mail`; `*` (an unscoped bare emission) covers everything. This
    is the same `fnmatchcase` glob semantics `lower._approval_covers` and
    `policy` use, so scope comparison never invents a second order."""
    from fnmatch import fnmatchcase  # noqa: PLC0415 - stdlib, no cycle
    return all(any(fnmatchcase(n, w) for w in wide) for n in narrow)


def _capability_scopes(audit: dict) -> dict[tuple[str, str], list]:
    """`(component, emission-label) -> declared capability scope`, read off the
    per-component `boundary[*].capabilities` map (`__main__._boundary` builds it,
    ~234). This is the NESTED leaf a scope-free `emit:C:label` crossing token
    cannot carry, so `diff_crossings` is blind to a widening here."""
    out: dict[tuple[str, str], list] = {}
    for comp, stats in (audit.get("boundary") or {}).items():
        for label, scope in (stats.get("capabilities") or {}).items():
            out[(comp, label)] = scope
    return out


def diff_capability_scopes(prev: dict, new: dict) -> dict:
    """Diff the per-emission capability SCOPE of every crossing present in BOTH
    audits (the HIGH/CRITICAL: `send.mail -> send.*` on a stable `emit:C:notify`).

    Scope is not in the crossing token, so a widening on a crossing that STAYS is
    invisible to `diff_crossings`. Only labels present in both are compared: a
    brand-new emission is already a `diff_crossings` addition. A scope that grows
    (the after-scope covers strictly more) is a WIDENING; one that shrinks is a
    tightening; a scope that MOVED to an incomparable set is treated as a
    widening (the conservative direction, as `diff_reach` treats a moved bound).
    """
    prev_scopes = _capability_scopes(prev)
    new_scopes = _capability_scopes(new)
    widened: list[str] = []
    tightened: list[str] = []
    for comp, label in prev_scopes.keys() & new_scopes.keys():
        before, after = prev_scopes[(comp, label)], new_scopes[(comp, label)]
        if before == after:
            continue
        after_covers = _scope_covers(after, before)
        before_covers = _scope_covers(before, after)
        if after_covers and before_covers:
            continue  # same reach, tokens merely reordered
        if after_covers:
            widened.append(f"scope:{comp}:{label}")
        elif before_covers:
            tightened.append(f"scope:{comp}:{label}")
        else:
            # moved to an incomparable scope: conservatively a widening.
            widened.append(f"scope:{comp}:{label}")
    return {"scope_widened": sorted(widened),
            "scope_tightened": sorted(tightened)}


def diff_backends(prev: dict, new: dict) -> dict:
    """Diff the `backends` host-body set of every extern present in BOTH audits
    (the CRITICAL second instance: `backends: ["rust"] -> ["py","rust"]`).

    A newly-added backend is NEW reachable host code (a real widening) that
    `diff_reach` cannot see - it reads only `reach`. Only externs in both are
    compared: a brand-new extern is already flagged by its `host:` crossing.
    A dropped backend is a narrowing (host code given up).
    """
    prev_be = {ext["name"]: set(ext.get("backends") or [])
               for ext in prev.get("externs") or []}
    new_be = {ext["name"]: set(ext.get("backends") or [])
              for ext in new.get("externs") or []}
    added: list[str] = []
    removed: list[str] = []
    for name in prev_be.keys() & new_be.keys():
        for backend in new_be[name] - prev_be[name]:
            added.append(f"backend:{name}:{backend}")
        for backend in prev_be[name] - new_be[name]:
            removed.append(f"backend:{name}:{backend}")
    return {"backends_added": sorted(added),
            "backends_removed": sorted(removed)}


def diff_recovery(prev: dict, new: dict) -> dict:
    """Diff the `recovery_surface` between two audits (the CRITICAL: a dropped
    inverse turns a reversible effect irreversible - item 273's class change).

    Each recovery entry is `{name, kind, register}`; entries are keyed by
    `(name, kind)`. An entry that DISAPPEARS is a dropped recovery (the reversible
    effect can no longer be undone) - the most security-relevant delta a release
    can carry, and invisible to every crossing/reach/interface differ. An entry
    whose idempotency `register` WEAKENS (a lower `_REGISTER_RANK`, or lost
    entirely) is a weakened recovery. A newly-appearing entry is a GAINED recovery
    (safe). Buckets are sorted, so the diff is deterministic over the unhashable
    `list[dict]` surface without ever hashing a dict.
    """
    from .lower import _REGISTER_RANK  # noqa: PLC0415 - the register partial order
    prev_r = {(e["name"], e["kind"]): e.get("register")
              for e in prev.get("recovery_surface") or []}
    new_r = {(e["name"], e["kind"]): e.get("register")
             for e in new.get("recovery_surface") or []}
    dropped: list[str] = []
    weakened: list[str] = []
    added: list[str] = []
    for name, kind in prev_r.keys() - new_r.keys():
        dropped.append(f"recovery:{name}:{kind}")
    for name, kind in new_r.keys() - prev_r.keys():
        added.append(f"recovery:{name}:{kind}")
    for key in prev_r.keys() & new_r.keys():
        before = _REGISTER_RANK.get(prev_r[key], -1)
        after = _REGISTER_RANK.get(new_r[key], -1)
        if after < before:
            weakened.append(f"recovery:{key[0]}:{key[1]}")
    return {"recovery_dropped": sorted(dropped),
            "recovery_weakened": sorted(weakened),
            "recovery_added": sorted(added)}


def diff_registers(prev: dict, new: dict) -> dict:
    """Diff the `capability_registers` idempotency-floor map (item 309/290).

    Each entry maps a capability token to its declaration register. A register
    that DROPS in strength (`keyed -> declared`, or a token that loses its floor
    entirely) is a WEAKENED floor - a running consumer that relied on the
    stronger idempotency guarantee may now double-apply. A register that rises,
    or a newly-declared floor, is a strengthening (safe). Rank via the same
    `_REGISTER_RANK` partial order `_capability_registers` builds the map with.
    """
    from .lower import _REGISTER_RANK  # noqa: PLC0415 - the register partial order
    prev_reg = prev.get("capability_registers") or {}
    new_reg = new.get("capability_registers") or {}
    weakened: list[str] = []
    strengthened: list[str] = []
    for token in prev_reg.keys() & new_reg.keys():
        before = _REGISTER_RANK.get(prev_reg[token], -1)
        after = _REGISTER_RANK.get(new_reg[token], -1)
        if after < before:
            weakened.append(f"register:{token}")
        elif after > before:
            strengthened.append(f"register:{token}")
    for token in prev_reg.keys() - new_reg.keys():
        weakened.append(f"register:{token}")      # a floor removed = weakened
    for token in new_reg.keys() - prev_reg.keys():
        strengthened.append(f"register:{token}")  # a new floor = tightened
    return {"registers_weakened": sorted(weakened),
            "registers_strengthened": sorted(strengthened)}


def _card_direction(before: dict, after: dict) -> str | None:
    """`"widened"` when `after` admits MORE crossings than `before`, `"tightened"`
    when fewer, else None. `unbounded` is the widest ceiling; a bounded count is
    ordered by its integer. Never orders two ceilings it cannot compare."""
    before_unbounded = before.get("kind") == "unbounded"
    after_unbounded = after.get("kind") == "unbounded"
    if after_unbounded and not before_unbounded:
        return "widened"
    if before_unbounded and not after_unbounded:
        return "tightened"
    if before_unbounded and after_unbounded:
        return None
    before_bound, after_bound = before.get("bound"), after.get("bound")
    if before_bound is None or after_bound is None:
        return None
    if after_bound > before_bound:
        return "widened"
    if after_bound < before_bound:
        return "tightened"
    return None


def diff_cardinality(prev: dict, new: dict) -> dict:
    """Diff the per-capability emission `cardinality` ceilings (item 260).

    Each `cardinality[comp].per_capability[token]` is `{bound, kind}`
    (a proved count, or `unbounded`). A ceiling that WIDENS (`<= 3` becomes
    `unbounded`, or a larger bound) admits more crossings per activation - a
    widened cost/authority signal the crossing/reach diff cannot see. A ceiling
    that shrinks is a tightening (safe). Only `(comp, token)` pairs present in
    both are compared; a brand-new crossing is already a `diff_crossings`
    addition.
    """
    def _per_cap(audit: dict) -> dict[tuple[str, str], dict]:
        out: dict[tuple[str, str], dict] = {}
        for comp, entry in (audit.get("cardinality") or {}).items():
            for token, cell in (entry.get("per_capability") or {}).items():
                out[(comp, token)] = cell
        return out

    prev_card = _per_cap(prev)
    new_card = _per_cap(new)
    widened: list[str] = []
    tightened: list[str] = []
    for comp, token in prev_card.keys() & new_card.keys():
        direction = _card_direction(prev_card[(comp, token)],
                                    new_card[(comp, token)])
        if direction == "widened":
            widened.append(f"cardinality:{comp}:{token}")
        elif direction == "tightened":
            tightened.append(f"cardinality:{comp}:{token}")
    return {"cardinality_widened": sorted(widened),
            "cardinality_tightened": sorted(tightened)}


def evaluate(prev: dict, new: dict, accepted: set[str] | None = None,
             accept_all: bool = False) -> dict:
    """The gate decision. `unacknowledged` are the additions that fail.

    exit-code contract: 0 iff `unacknowledged` is empty (a clean or fully
    acknowledged diff); nonzero otherwise.
    """
    accepted = accepted or set()
    delta = diff_crossings(prev, new)
    # item 373: a reach WEAKENING fails the gate the same way a new crossing
    # does — it is the authority axis loosening without a new token appearing.
    # Its `reach-weakened:<name>` token is acknowledged through the same
    # `--accept`/`--accept-all` path as an added crossing.
    gated = delta["added"] + delta["reach_weakened"]
    if accept_all:
        unacknowledged: list[str] = []
    else:
        unacknowledged = [c for c in gated if c not in accepted]
    return {
        "added": delta["added"],
        "removed": delta["removed"],
        "unchanged": delta["unchanged"],
        "reach_weakened": delta["reach_weakened"],
        "reach_tightened": delta["reach_tightened"],
        "acknowledged": sorted(c for c in gated
                               if accept_all or c in accepted),
        "unacknowledged": unacknowledged,
        "widened": bool(unacknowledged),
    }


def render(result: dict, prev_label: str) -> str:
    """Human-readable report of an `evaluate` result."""
    lines: list[str] = []
    added = result["added"]
    weakened = result.get("reach_weakened") or []
    tightened = result.get("reach_tightened") or []
    if not added and not result["removed"] and not weakened and not tightened:
        return (f"authority-drift: clean — the G8 boundary surface is "
                f"unchanged from {prev_label}.")

    acked = set(result["acknowledged"])
    if added:
        lines.append(
            f"authority-drift: {len(added)} new boundary crossing(s) added "
            f"since {prev_label}:")
        lines.append("")
        for crossing in added:
            mark = "  ~ " if crossing in acked else "  + "
            suffix = "  (acknowledged)" if crossing in acked else ""
            lines.append(f"{mark}{crossing}{suffix}")
        lines.append("")
        if any(c in result["unacknowledged"] for c in added):
            lines.append(
                "These WIDEN what the composition reaches outside the system.")
            lines.append(
                "Acknowledge an intended widening with --accept <crossing> "
                "(repeatable) or --accept-all.")
        else:
            lines.append("All additions acknowledged.")
    else:
        lines.append(f"authority-drift: no new crossings since {prev_label}.")

    # item 373: a reach weakening is a loosened bound on a crossing that STAYS —
    # confined -> unconfined, or a bound that moved. It fails the gate like an
    # addition, and is acknowledged through the same `--accept` token.
    if weakened:
        lines.append("")
        lines.append(
            f"reach WEAKENED (a crossing loosened what it is bounded to): "
            f"{len(weakened)}")
        for token in weakened:
            mark = "  ~ " if token in acked else "  + "
            suffix = "  (acknowledged)" if token in acked else ""
            lines.append(f"{mark}{token}{suffix}")
        if any(t in result["unacknowledged"] for t in weakened):
            lines.append(
                "A weakened reach is a widening — acknowledge with "
                "--accept <token> or --accept-all.")

    if tightened:
        lines.append("")
        lines.append(
            f"reach tightened (safe — a bound gained): {len(tightened)}")
        for token in tightened:
            lines.append(f"  - {token}")

    if result["removed"]:
        lines.append("")
        lines.append(
            f"narrowed (safe — authority given up): {len(result['removed'])}")
        for crossing in result["removed"]:
            lines.append(f"  - {crossing}")

    return "\n".join(lines)
