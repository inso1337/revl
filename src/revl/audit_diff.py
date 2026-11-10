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

import hashlib
import json
import os

from .cardinality import cardinality
from .distribute import distributability
from .inverse import inverse_class, inverse_rank
from .resources import retention_surface


def audit_report(ir: dict, spec_results: dict | None = None) -> dict:
    """Build the same dict `revl audit --json` emits, from a compiled ir.

    Reuses the one authoritative boundary computation (`_boundary`) rather
    than recomputing the surface a second, divergent way.

    ``spec_results`` is the recorded `function_specs` map from the last
    `revl test --report json` run (issue #2705); absent, every spec lists
    `last_result: "unrun"`, which is what a static audit honestly knows.
    """
    from .boundary import _boundary  # noqa: PLC0415 — lazy, avoids import cycle

    boundary = _boundary(ir)
    manifest = ir.get("manifest") or {}
    declared_externs = [
        {"name": ext["name"], "class": ext.get("class"),
         # issue #2692: the single derived inverse-provenance class, read off
         # this declaration's own facts. Always present (never a missing field):
         # an extern with no provenance fact reads `unknown`. Must match
         # `__main__`'s entry key-for-key.
         "inverse": inverse_class(ext),
         "backends": sorted((ext.get("bodies") or {}).keys()),
         # item 373: carry the reach so the drift gate can read it. Absent unless
         # declared — a bare emission's audit entry is byte-identical to today's.
         **({"reach": ext["reach"]} if ext.get("reach") else {}),
         # item 484: carry the `deferred` modifier, so `revl_audit`'s entry says
         # whether a crossing fires now or is held to the commit prompt. Must
         # match `__main__`'s entry key-for-key. Absent unless declared.
         **({"deferred": True} if ext.get("deferred") else {}),
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
        # item 350: the ENVIRONMENT CONTRACT table — the `boot` component's
        # config schema, which is the exhaustive list of what the host must
        # inject before the composition can boot. NAME, TYPE, requiredness,
        # credential marking and the author-written BOUND only; never a value
        # (values arrive at run time through `--env` and never enter the IR).
        # ADDITIVE and PRESENT ONLY when the composition declares a boot
        # component, so every existing audit surface is byte-identical (the same
        # conditional-presence discipline as `secrets` above). Adding a contract
        # field, or dropping/loosening a field's bound, surfaces as an `env:`
        # crossing the drift gate flags as a widening (see `crossings`).
        **_env_surface(ir),
        # item 308 F10 (docs/design/308-effect-ownership-modes.md, "The honest
        # limitation: retaining externs"): the REPORT-ONLY retention surface -
        # every declared position at which a resource handle leaves revl's
        # sight, so a human can review what B1's flow walk provably cannot see.
        # ADDITIVE and PRESENT ONLY when the composition declares a resource
        # type reaching one, so a handle-free composition's audit surface is
        # byte-identical to before (the same conditional-presence discipline as
        # `parallel_plan` and `secrets` above). A row is a MAY-retain, never a
        # proof of retention, and an ABSENT row is never a proof of the
        # opposite - see `resources.retention_surface`.
        **_retention_surface(ir),
        # issue #1896: each `revl gen-types` file's model document and digest,
        # checked at load. ADDITIVE and present only when one was compiled.
        **_generated_surface(ir),
        # issue #2705: the functions that state an `ensures` spec, with the
        # last recorded result. ADDITIVE and present only when a fn states a
        # spec, so every other composition's audit is byte-identical.
        **_function_specs_surface(ir, spec_results),
    }


def _env_table(ir: dict) -> dict | list:
    """The audit environment-contract table (item 350): one row per field of the
    composition's `boot` component config block, wrapped with the component's
    name, or `[]` when the composition declares no boot component.

    Read statically off the IR. A field's `bound` is AUTHOR-WRITTEN SOURCE (a
    path prefix or an enumerated set), so it is safe to publish here — unlike a
    value, which this table never sees: the host injects values at `revl run
    --env` time, after the audit, and they never enter the IR."""
    boot = next((c for c in (ir.get("components") or []) if c.get("boot")), None)
    if boot is None:
        return []
    rows = []
    for field in boot.get("config") or []:
        rows.append({
            "name": field["name"],
            "type": field.get("type") or "?",
            # a field with no default MUST be injected or the boot refuses
            "required": field.get("default") is None,
            # item 256: a contract field declared `Secret[T]` carries a credential
            "secret": bool(field.get("secret")),
            # None = UNBOUNDED: the host may inject any value of the type, which
            # is exactly as wide as the pre-350 host-written config map. The
            # audit says so explicitly rather than leaving it unsaid.
            "bound": field.get("bound"),
        })
    return {"component": boot["name"], "fields": rows}


def _env_surface(ir: dict) -> dict:
    """The additive `env` audit key, or `{}` when the composition declares no
    `boot` component, so a boot-free composition's audit surface stays
    byte-identical to before."""
    table = _env_table(ir)
    return {"env": table} if table else {}


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


def spec_results_from_report(report: dict) -> dict:
    """The `function_specs` map a `revl test --report json` payload carries.

    `revl audit --spec-results` reads that payload so the audit can list each
    function's spec next to the result of the last recorded run (issue #2705).
    Anything else (no section, an entry with no function, no status) loads as
    absent, which the audit renders as `unrun` rather than inventing a result.
    """
    results: dict[str, dict] = {}
    for row in report.get("function_specs") or []:
        if not isinstance(row, dict):
            continue
        function = row.get("function")
        if not function:
            continue
        entry = {"status": row.get("status") or "unrun",
                 "spec": row.get("spec"),
                 "digest": row.get("digest")}
        if row.get("counterexample"):
            entry["counterexample"] = row["counterexample"]
        results[function] = entry
    return results


def function_spec_digest(ir: dict, unit: dict) -> str:
    """The identity a recorded spec result is bound to (issue #2705).

    A sha256 over the function's lowered signature and body (its `functions`
    entry), the clause, the generated property body and the absolute path of
    the file the function is declared in. `revl test --report json` records it
    and `function_specs_table` recomputes it from the program being audited,
    so a result recorded before the body, the clause or the file changed is
    never attributed to what the audit is showing now.
    """
    function = unit.get("spec_of")
    entry = next((fn for fn in ir.get("functions") or []
                  if fn.get("name") == function), None)
    spec_file = unit.get("spec_file")
    document = {
        "file": os.path.abspath(spec_file) if spec_file else None,
        "function": entry,
        "spec": unit.get("spec") or "",
        "property": unit.get("body"),
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def function_specs_table(ir: dict, results: dict | None = None) -> list:
    """One row per fn that states an `ensures` clause (issue #2705).

    The spec, the parameter types the generated property drew its inputs from,
    and the last result the caller recorded. `unrun` is NOT a failure: the
    audit is a static surface, so a spec that no recorded `revl test` run has
    checked yet is listed with that fact rather than a guess
    (docs/function-specs.md).
    """
    results = results or {}
    rows = []
    for unit in ir.get("prop_tests") or []:
        function = unit.get("spec_of")
        if not function:
            continue
        result = results.get(function) or {}
        # a record from a different spelling of the clause is not this spec's
        # result; report `unrun` rather than attributing another claim's check
        if result.get("spec") is not None and result["spec"] != (unit.get("spec") or ""):
            result = {}
        # a record is this spec's result only when its digest is the one the
        # audited program has now: an edited body, a record from another file
        # with a fn of the same name, or a record with no digest reads `unrun`
        if result and result.get("digest") != function_spec_digest(ir, unit):
            result = {}
        row = {
            "function": function,
            "spec": unit.get("spec") or "",
            "property": unit.get("name"),
            "params": [{"name": p.get("name"), "type": p.get("type")}
                       for p in unit.get("params") or []],
            "last_result": result.get("status") or "unrun",
        }
        if result.get("counterexample"):
            row["counterexample"] = result["counterexample"]
        rows.append(row)
    rows.sort(key=lambda row: row["function"])
    return rows


def _function_specs_surface(ir: dict, results: dict | None = None) -> dict:
    """The additive `function_specs` audit key, or `{}` when no fn states a
    spec, so every other composition's audit surface stays byte-identical."""
    rows = function_specs_table(ir, results)
    return {"function_specs": rows} if rows else {}


def _generated_surface(ir: dict) -> dict:
    """The additive `generated_from` audit key (issue #1896): the model document
    and sha256 behind each generated types file, or `{}` when none was
    compiled, so every other composition's audit is byte-identical."""
    rows = ir.get("generated_from")
    return {"generated_from": [dict(row) for row in rows]} if rows else {}


def _retention_surface(ir: dict) -> dict:
    """The additive `retention` audit key (item 308 F10), or `{}` when the
    composition declares no resource handle reaching a non-inverse callee - so a
    handle-free composition renders byte-identically to before."""
    rows = retention_surface(ir.get("externs"), ir.get("types"),
                             ir.get("services"))
    return {"retention": rows} if rows else {}


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
    externs takes the WEAKEST register. Absent for a token no idempotent extern
    scopes.

    WORST-WINS is the direction 290 §3.2 specifies, and the only direction a
    refusal rule can use: "the effective register of a capability is the WEAKEST
    among the declarations behind it. One bare `declared` inverse beside three
    keyed ones fails a `keyed` floor." A register floor asks whether EVERY
    declaration behind the token meets it, so the map has to carry the floor the
    weakest declaration can support — folding strongest-wins would let one keyed
    declaration carry bare-`declared` siblings on the same token past a `keyed`
    or `strong` floor, which is the fail-open direction in a rule whose only job
    is to refuse. The sibling rule `policy._teardown_violations` grades the same
    declarations worst-wins by construction (it walks every recovery-surface
    entry), so both register rules now read the same convention."""
    from .lower import _REGISTER_RANK  # noqa: PLC0415 — the partial order

    out: dict = {}

    def _record(tokens, register: str) -> None:
        for token in tokens:
            prior = out.get(token)
            if prior is None or _REGISTER_RANK.get(register, -1) \
                    < _REGISTER_RANK.get(prior, -1):
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
        env:<name>:<bound>                a host-injected environment value (item 350)

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
    # item 350: the environment-contract crossings. Like `secret:`, these are
    # composition-level, drawn from the top-level `env` table. The token carries
    # the field's declared BOUND, so three distinct authority changes all land as
    # a token ADDITION the drift gate flags as a widening:
    #   * a NEW contract field         -> a token that was not there before
    #   * a field's bound REMOVED      -> `env:x:under=./data` becomes `env:x:*`
    #   * a bound LOOSENED             -> the fingerprint changes
    # Removing a field, or tightening a bound, drops the old token: a narrowing.
    # The token names the field and the author-written bound, never a value.
    for row in (audit.get("env") or {}).get("fields") or []:
        out.add(f"env:{row['name']}:{_bound_token(row.get('bound'))}")
    return out


def _bound_token(bound) -> str:
    """A contract field's declared bound, as a stable crossing fingerprint.
    `*` means UNBOUNDED — the host may inject any value of the declared type."""
    if not bound:
        return "*"
    if bound.get("kind") == "under":
        return f"under={bound.get('prefix')}"
    values = "|".join(str(v) for v in (bound.get("values") or []))
    return f"in={values}"


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


def diff_inverses(prev: dict, new: dict) -> dict:
    """Diff the derived inverse-provenance class of every extern present in BOTH
    audits (issue #2692).

    Each top-level `externs[]` entry carries `inverse`, one value of the closed
    vocabulary in `inverse.py` (`declared`, `generated`, `compensation`,
    `forward`, `irreversible`, `unknown`), and a `class` (the G4 effect class).
    The diff keys on the extern name; both facts matter because the crossing
    token `host:<component>:<name>` does not change when either moves.

    A class that moves to a WEAKER recovery story (later in `inverse_rank`, e.g.
    `declared -> compensation`, or `compensation -> irreversible`) is a weakened
    inverse: the crossing that could be put back exactly can now only be offset,
    or not recovered at all. It is the same dangerous direction as a reach
    weakening and feeds the gate the same way. A move toward a stronger class is
    a strengthening (safe). Only externs in both audits are compared: a brand-new
    extern is already flagged by its `host:` crossing, and a removed one gave up
    its reach entirely.

    The rank alone misses the worst move: `unknown` is the weakest inverse
    class, so a `pure` read or `local` write (both `unknown`) that becomes an
    `emission` or `witnessed` effect would rank as a strengthening. That is the
    dangerous direction (an effect appeared where there was none) and this diff
    is the only place it shows, so a no-provenance crossing that becomes an
    effectful one is weakened regardless of the rank. The reverse, an effectful
    crossing becoming `pure`, still ranks as a weakening: giving up a reach is
    safe, but the move is reviewable.

    An extern whose entry on EITHER side has no `inverse` is skipped, not read
    as `unknown`: a baseline captured before this field existed did not record a
    class at all, and treating that absence as a class would report drift on a
    crossing that did not change. Absence is not `unknown`; it is not
    comparable.
    """
    prev_inv = {ext["name"]: (ext.get("class"), ext["inverse"])
                for ext in prev.get("externs") or [] if "inverse" in ext}
    new_inv = {ext["name"]: (ext.get("class"), ext["inverse"])
               for ext in new.get("externs") or [] if "inverse" in ext}
    weakened: list[str] = []
    strengthened: list[str] = []
    for name in prev_inv.keys() & new_inv.keys():
        prev_cls, before = prev_inv[name]
        new_cls, after = new_inv[name]
        if before == after and prev_cls == new_cls:
            continue
        if before == "unknown" and new_cls in ("emission", "witnessed"):
            weakened.append(f"inverse:{name}")
        elif inverse_rank(after) < inverse_rank(before):
            weakened.append(f"inverse:{name}")
        elif inverse_rank(after) > inverse_rank(before):
            strengthened.append(f"inverse:{name}")
        # else: the rank did not move and only the effect class did (for
        # example `pure` to `local`, both `unknown`). That is not an
        # inverse-provenance change; the changelog completeness guard still
        # surfaces the class leaf.
    return {"inverse_weakened": sorted(weakened),
            "inverse_strengthened": sorted(strengthened)}


def diff_recovery(prev: dict, new: dict) -> dict:
    """Diff the `recovery_surface` between two audits (the CRITICAL: a dropped
    inverse turns a reversible effect irreversible - item 273's class change).

    Each recovery entry is `{name, kind, register}`; entries are keyed by
    `(name, kind)`. An entry that DISAPPEARS is a dropped recovery (the reversible
    effect can no longer be undone) - the most security-relevant delta a release
    can carry, and invisible to every crossing/reach/interface differ. An entry
    whose idempotency `register` WEAKENS (a lower `_REGISTER_RANK`, or lost
    entirely) is a weakened recovery. A newly-appearing entry is a GAINED recovery
    (safe).

    A name that still has a recovery of SOME kind on the new side is not
    dropped when only its kind moved (`compensation` to `inverse`, or back;
    issue #3182). The effect can still be put back or offset, and which way
    the move goes is the inverse-provenance question `diff_inverses` answers:
    a weaker kind is its `inverse:<name>` token, a stronger one passes. Read
    here as a drop, one kind change gated twice and a strengthening failed.
    A lost kind is suppressed only when `diff_inverses` actually emits an
    `inverse:<name>` token for that name (the class moved) and the name still
    has an entry on the new side. Otherwise it stays dropped: a recovery that
    disappears entirely (even when its class moved, for example
    `compensation` to `irreversible`), an extern with both kinds
    that loses one while its class stays put, and a kind change on an extern
    that lacks `inverse` on either side (a baseline captured before that field
    existed, which `diff_inverses` skips). Across a move the extern's
    `register` is still compared: a new-kind entry whose register is weaker
    than the one the lost kind carried is a weakened recovery, so a move that
    strengthens the class but loses its idempotency floor does not pass as a
    plain strengthening; that entry is reported weakened, not gained.
    Buckets are sorted, so the diff is deterministic over the unhashable
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
    inverses = diff_inverses(prev, new)
    # a move needs both: the class moved, and the name still has an entry.
    moved = {token.split(":", 1)[1]
             for bucket in ("inverse_weakened", "inverse_strengthened")
             for token in inverses[bucket]} & {name for name, _kind in new_r}
    lost_rank: dict[str, int] = {}
    for name, kind in prev_r.keys() - new_r.keys():
        if name in moved:
            rank = _REGISTER_RANK.get(prev_r[(name, kind)], -1)
            lost_rank[name] = max(rank, lost_rank.get(name, -1))
        else:
            dropped.append(f"recovery:{name}:{kind}")
    for name, kind in new_r.keys() - prev_r.keys():
        if (name in lost_rank and
                _REGISTER_RANK.get(new_r[(name, kind)], -1) < lost_rank[name]):
            weakened.append(f"recovery:{name}:{kind}")
        else:
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


# issue #3182: the buckets the drift gate fails on, in report order. Each pair
# is (the `widening_from_audits` key, the differ bucket it reads). This is the
# one gated set: `evaluate` (`revl audit --diff`) and the MCP update path's
# acknowledgement decision both read it, so a widening one of them catches
# cannot pass the other. The safe-direction buckets (removed, tightened,
# strengthened, gained) are reported but never gate.
GATED_BUCKETS: tuple[tuple[str, str], ...] = (
    ("added", "added"),
    ("reachWeakened", "reach_weakened"),
    ("inverseWeakened", "inverse_weakened"),
    ("scopesWidened", "scope_widened"),
    ("backendsAdded", "backends_added"),
    ("recoveryDropped", "recovery_dropped"),
    ("recoveryWeakened", "recovery_weakened"),
    ("registersWeakened", "registers_weakened"),
    ("cardinalityWidened", "cardinality_widened"),
)


def drift(prev: dict, new: dict) -> dict:
    """Every differ's buckets between two audits, merged into one dict.

    The eight differs: the crossing set and its reach, inverse provenance,
    per-emission capability scopes, host backends, recovery inverses,
    capability registers and emission cardinality. Their bucket names are
    disjoint, so the merge loses nothing.
    """
    return {
        **diff_crossings(prev, new),
        **diff_inverses(prev, new),
        **diff_capability_scopes(prev, new),
        **diff_backends(prev, new),
        **diff_recovery(prev, new),
        **diff_registers(prev, new),
        **diff_cardinality(prev, new),
    }


def widening_from_audits(before: dict, after: dict,
                         buckets: dict | None = None) -> dict:
    """Every authority axis that widened between two audits.

    A release can widen any axis while adding no crossing (a new backend host
    body, a scope widened on a stable crossing, a dropped recovery inverse, a
    weakened register floor, a raised emission ceiling), so every gated bucket
    feeds the decision. `buckets` is a `drift` result already computed for the
    same pair.
    """
    found = buckets if buckets is not None else drift(before, after)
    out = {key: list(found[bucket]) for key, bucket in GATED_BUCKETS}
    return {"widened": any(out.values()), **out}


def evaluate(prev: dict, new: dict, accepted: set[str] | None = None,
             accept_all: bool = False) -> dict:
    """The gate decision. `unacknowledged` are the gated tokens (every
    `GATED_BUCKETS` bucket: an added crossing or a weakened or widened
    authority fact) that no `--accept` covers.

    exit-code contract: 0 iff `unacknowledged` is empty (a clean or fully
    acknowledged diff); nonzero otherwise.
    """
    accepted = accepted or set()
    # issue #3182: the gate reads the same buckets the MCP update path does.
    # Before, it read only added crossings, reach weakenings (item 373) and
    # inverse weakenings (#2692), so a new backend host body or a scope widened
    # on a stable crossing printed clean and exited 0. Every gated token is
    # acknowledged through the same `--accept`/`--accept-all` path.
    buckets = drift(prev, new)
    widening = widening_from_audits(prev, new, buckets)
    gated = [token for key, _bucket in GATED_BUCKETS for token in widening[key]]
    if accept_all:
        unacknowledged: list[str] = []
    else:
        unacknowledged = [c for c in gated if c not in accepted]
    return {
        **{bucket: buckets[bucket] for bucket in _REPORTED_BUCKETS},
        "acknowledged": sorted(c for c in gated
                               if accept_all or c in accepted),
        "unacknowledged": unacknowledged,
        "widened": bool(unacknowledged),
    }


# The buckets `evaluate` returns, gated and safe-direction alike, in the order
# `render` prints them.
_REPORTED_BUCKETS: tuple[str, ...] = (
    "added", "removed", "unchanged",
    "reach_weakened", "reach_tightened",
    "inverse_weakened", "inverse_strengthened",
    "scope_widened", "scope_tightened",
    "backends_added", "backends_removed",
    "recovery_dropped", "recovery_weakened", "recovery_added",
    "registers_weakened", "registers_strengthened",
    "cardinality_widened", "cardinality_tightened",
)


def render(result: dict, prev_label: str) -> str:
    """Human-readable report of an `evaluate` result."""
    lines: list[str] = []
    added = result["added"]
    weakened = result.get("reach_weakened") or []
    tightened = result.get("reach_tightened") or []
    inverse_weakened = result.get("inverse_weakened") or []
    inverse_strengthened = result.get("inverse_strengthened") or []
    if not any(result.get(bucket) for bucket in _REPORTED_BUCKETS
               if bucket != "unchanged"):
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

    # issue #2692: an inverse provenance that WEAKENED (declared -> compensation,
    # compensation -> irreversible, ...) is a recovery loss on a crossing that
    # STAYS. It fails the gate like a reach weakening and is acknowledged
    # through the same `--accept` token.
    if inverse_weakened:
        lines.append("")
        lines.append(
            f"inverse provenance WEAKENED (a crossing lost a stronger way "
            f"back): {len(inverse_weakened)}")
        for token in inverse_weakened:
            mark = "  ~ " if token in acked else "  + "
            suffix = "  (acknowledged)" if token in acked else ""
            lines.append(f"{mark}{token}{suffix}")
        if any(t in result["unacknowledged"] for t in inverse_weakened):
            lines.append(
                "A weakened inverse is a widening: acknowledge with "
                "--accept <token> or --accept-all.")

    if inverse_strengthened:
        lines.append("")
        lines.append(
            f"inverse provenance strengthened (safe: a stronger way back): "
            f"{len(inverse_strengthened)}")
        for token in inverse_strengthened:
            lines.append(f"  - {token}")

    # issue #3182: the five axes a crossing-set diff cannot see. Each widened
    # bucket fails the gate like an addition and is acknowledged through the
    # same `--accept` token; each safe bucket is listed only.
    for bucket, title in _WIDENED_SECTIONS:
        _render_gated(lines, result, bucket, title, acked)
    for bucket, title in _SAFE_SECTIONS:
        tokens = result.get(bucket) or []
        if tokens:
            lines.append("")
            lines.append(f"{title}: {len(tokens)}")
            lines.extend(f"  - {token}" for token in tokens)

    if result["removed"]:
        lines.append("")
        lines.append(
            f"narrowed (safe — authority given up): {len(result['removed'])}")
        for crossing in result["removed"]:
            lines.append(f"  - {crossing}")

    return "\n".join(lines)


_WIDENED_SECTIONS: tuple[tuple[str, str], ...] = (
    ("scope_widened", "capability scope WIDENED (a stable emission reaches "
     "more capabilities)"),
    ("backends_added", "host backend ADDED (new host code an extern can run)"),
    ("recovery_dropped", "recovery DROPPED (a reversible effect lost its "
     "inverse)"),
    ("recovery_weakened", "recovery WEAKENED (a recovery entry lost "
     "idempotency strength)"),
    ("registers_weakened", "idempotency register WEAKENED (a capability "
     "floor dropped)"),
    ("cardinality_widened", "emission cardinality WIDENED (more crossings per "
     "activation)"),
)

_SAFE_SECTIONS: tuple[tuple[str, str], ...] = (
    ("scope_tightened", "capability scope tightened (safe)"),
    ("backends_removed", "host backend removed (safe: host code given up)"),
    ("recovery_added", "recovery gained (safe)"),
    ("registers_strengthened", "idempotency register strengthened (safe)"),
    ("cardinality_tightened", "emission cardinality tightened (safe)"),
)


def _render_gated(lines: list[str], result: dict, bucket: str, title: str,
                  acked: set[str]) -> None:
    tokens = result.get(bucket) or []
    if not tokens:
        return
    lines.append("")
    lines.append(f"{title}: {len(tokens)}")
    for token in tokens:
        mark = "  ~ " if token in acked else "  + "
        suffix = "  (acknowledged)" if token in acked else ""
        lines.append(f"{mark}{token}{suffix}")
    if any(t in result["unacknowledged"] for t in tokens):
        lines.append(
            "This is a widening: acknowledge with --accept <token> or "
            "--accept-all.")
