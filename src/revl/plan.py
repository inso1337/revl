"""`revl plan` — a dry run for the admission gate.

`compile_files(files, manifest=running_ir)` answers one question: *may this
be admitted?* A plan answers the next one: *and then what?* It reports the
delta a swap would produce — provisions gained and withdrawn, which running
components deactivate as a consequence, the order their inverses would
replay in, and how the composition's irreversible reach (G8) changes —
**without applying any of it**.

Nothing here mutates: no file is written, no session is touched, and the
only compilation performed is the same read-only one the gate already does.

What a plan is made of
----------------------
The delta is derived from two composition manifests: the *running* one that
came in, and the *resulting* one the linker produces when the candidate is
admitted against it. Both are built by `lower._link`; this module diffs
them. G2 (provision disjointness) and G3 (acyclic dependencies) are not
re-checked here — if they would fail, the gate raises and the plan reports
the rejection.

Degradation (the brief's "report the rejection *and* as much of the delta
as it can still compute"). `basis` names how much of the plan is real:

  admitted    the gate accepted; every field below is compiler-derived
  standalone  the gate rejected, but the candidate compiles on its own, so
              the delta is structural (what the composition *would* look
              like) while admissibility is already known to be false
  parsed      the candidate does not compile at all; component headers were
              recovered from the AST, so provisions and cascade are shapes,
              not checked facts
  none        not even parseable — only the diagnostics are meaningful

Guaranteed vs predicted
-----------------------
Provisions, replacements, interface drift and the emission surface are
*compiler-derived*: they are read off the same manifests the gate uses.
The reactive cascade and teardown order are *predicted*: they apply the
runtime contract (R2 reactive resolution, R3 withdrawal ordering, G7 LIFO
teardown) to the manifest graph. A real runtime can still land elsewhere —
a component whose body fails during activation, or config that changes what
it acquires, is not visible in a manifest. `plan()["predicted"]` says so in
the payload, and the CLI prints it.
"""

from __future__ import annotations

import os

from .compiler import compile_files, compile_source
from .diagnostics import classify
from .errors import RevlError
from .lower import SHARED_REALM

__all__ = ["plan", "render"]


# ---------------------------------------------------------------- plumbing

def _compile(source: str | None, files: list[str] | None,
             modules: dict[str, str] | None = None,
             manifest: dict | None = None,
             replacing: tuple[str, ...] = ()) -> dict:
    """Dispatch to the same entry points the CLI and the MCP bridge use, so
    a plan is computed against the literal admission gate."""
    if source is not None:
        return compile_source(source, "<candidate>.rvl", manifest=manifest,
                              replacing=replacing, modules=modules)
    if not files:
        raise ValueError("provide `source` or `files`")
    return compile_files(list(files), manifest=manifest, replacing=replacing,
                         sources={os.path.abspath(p): t
                                  for p, t in (modules or {}).items()} or None)


def _boundary_of(ir: dict) -> dict:
    """`revl audit`'s G8 walk. Imported lazily: `__main__` imports this
    module, so a module-level import would be circular."""
    from .boundary import _boundary  # noqa: PLC0415

    return _boundary(ir)


def _realm(entry: dict, key: str) -> str:
    return (entry.get("isolate") or {}).get(key, SHARED_REALM)


def _realm_name(realm: str) -> str | None:
    return None if realm == SHARED_REALM else realm


def _key_services(ir: dict | None) -> dict[tuple[str, str], str]:
    """(component, key) -> service name, from a lowered IR's components.

    A manifest entry only lists provision *keys*; the service each key
    carries lives on the lowered component. When the caller handed us a
    manifest without bodies, the service is simply unknown.
    """
    out: dict[tuple[str, str], str] = {}
    for comp in (ir or {}).get("components") or []:
        for key, service in (comp.get("provides") or {}).items():
            out[(comp.get("name"), key)] = service
    return out


def _parsed_entries(source: str | None, files: list[str] | None,
                    modules: dict[str, str] | None) -> tuple[list[dict], dict]:
    """Last-resort recovery: component headers straight from the AST, in
    manifest-entry shape. `isolate`/`intercept` are prelude *statements*, so
    they are not recovered here — realms fall back to the shared realm."""
    from .parser import Parser, parse_file  # noqa: PLC0415

    programs = []
    if source is not None:
        programs.append(Parser(source, "<candidate>.rvl").parse())
    for path in files or []:
        text = (modules or {}).get(path)
        programs.append(Parser(text, path).parse() if text is not None
                        else parse_file(path))

    entries: list[dict] = []
    services: dict[tuple[str, str], str] = {}
    for program in programs:
        for comp in program.components:
            entries.append({
                "name": comp.name,
                "file": comp.source or program.filename,
                "inject": sorted({local for local, _, _ in comp.requires}),
                "provides": sorted({key for key, _, _ in comp.provides}),
            })
            for key, service, _ in comp.provides:
                services[(comp.name, key)] = service
    return entries, services


# ---------------------------------------------------------------- the plan

def _merge_resulting_ir(running_ir: dict | None, candidate_ir: dict,
                        dropped: set) -> dict:
    """The full resulting composition, bodies and all — running survivors
    (minus what this admission drops) plus the newly compiled components.

    `plan()` never needs this, but `revl apply` does: to load an added or
    replaced component against a live session it needs that component's
    *body*, and to leave the survivors reflected in the composition it needs
    theirs. This is the same shape `lower._link` produces; the linker already
    validated it, so this only reassembles the pieces the gate handed back.
    """
    running_ir = running_ir or {}
    cand_names = {c["name"] for c in candidate_ir.get("components") or []}
    components = [c for c in running_ir.get("components") or []
                  if c["name"] not in dropped and c["name"] not in cand_names]
    components += list(candidate_ir.get("components") or [])
    externs = {e["name"]: e for e in running_ir.get("externs") or []}
    externs.update({e["name"]: e for e in candidate_ir.get("externs") or []})
    return {
        "ir_version": candidate_ir.get("ir_version") or running_ir.get("ir_version"),
        "components": components,
        "services": {**(running_ir.get("services") or {}),
                     **(candidate_ir.get("services") or {})},
        "functions": {**(running_ir.get("functions") or {}),
                      **(candidate_ir.get("functions") or {})},
        "externs": list(externs.values()),
        "manifest": candidate_ir.get("manifest") or running_ir.get("manifest") or {},
    }


def plan(source: str | None = None, files: list[str] | None = None,
         manifest: dict | None = None, modules: dict[str, str] | None = None,
         replacing: tuple[str, ...] = (), include_ir: bool = False) -> dict:
    """What admitting this candidate would do to the running composition.

    `manifest` is a compiled IR document of what is running (or its
    `manifest` + `services`); omit it and the plan describes a cold start,
    where every provision is a gain. `replacing` names components withdrawn
    in the same admission (renames), exactly as `compile_files` takes it.

    With `include_ir=True` (and only on the admitted path) the result also
    carries `resultingIR` — the full resulting composition, bodies included —
    which is what `revl apply` executes against a live session (docs/apply.md).

    Returns a structured dict; `render()` turns it into the CLI's prose.
    """
    replacing = tuple(replacing or ())
    running_ir = manifest if isinstance(manifest, dict) else None
    running_manifest = (running_ir or {}).get("manifest", running_ir or {}) or {}
    before_entries = [dict(e) for e in running_manifest.get("components") or []]
    before_order = list(running_manifest.get("loadOrder") or [])
    before_by_name = {e["name"]: e for e in before_entries}

    # -- run the gate (read-only) ------------------------------------------
    diagnostics: list[dict] = []
    candidate_ir: dict | None = None
    parsed_entries: list[dict] | None = None
    parsed_services: dict = {}
    notes: list[str] = []

    def _add(error: RevlError, origin: str) -> None:
        """Record a rejection once. `origin` matters: only `admission` is the
        real gate. A `standalone` failure comes from re-compiling the
        candidate *without* the running composition in scope, so it can name
        an ambient service as unknown — an artifact of the fallback, not a
        problem with the candidate. Labelling beats hiding.

        A multi-refusal compile raises a `RevlErrors` carrier (item 386); its
        `.errors` list is iterated so `plan()` on a candidate with several
        independent defects surfaces all of them, not just the first. The dedup
        key here EXCLUDES filename by design (the gate abspaths, the standalone
        compile does not — same rejection, two filenames), so it stays as-is."""
        for one in (getattr(error, "errors", None) or [error]):
            record = classify(one)
            # the same rejection reaches us under two filenames (the gate
            # abspaths, the standalone compile does not) — compare on the rest
            if any(d["code"] == record["code"] and d["line"] == record["line"]
                   and d["message"] == record["message"] for d in diagnostics):
                continue
            record["from"] = origin
            if origin == "standalone":
                record["note"] = ("seen while compiling the candidate on its own, "
                                  "without the running composition's services in "
                                  "scope — it may not be a real defect")
            diagnostics.append(record)

    try:
        candidate_ir = _compile(source, files, modules, manifest=running_ir,
                                replacing=replacing)
        basis, admissible = "admitted", True
    except RevlError as error:
        admissible = False
        _add(error, "admission")
        basis = "none"
        if running_ir is not None:
            # the gate refused; does the candidate at least stand alone?
            try:
                candidate_ir = _compile(source, files, modules)
                basis = "standalone"
            except RevlError as second:
                _add(second, "standalone")
        if candidate_ir is None:
            try:
                parsed_entries, parsed_services = _parsed_entries(source, files, modules)
                basis = "parsed"
            except RevlError as third:
                _add(third, "parse")
            except OSError:
                # an unreadable file already produced the gate's diagnostic
                pass

    # -- the resulting composition -----------------------------------------
    if candidate_ir is not None:
        candidate_names = [c["name"] for c in candidate_ir.get("components") or []]
        wanted = set(candidate_names)
        candidate_entries = [
            dict(e) for e in (candidate_ir.get("manifest") or {}).get("components") or []
            if e.get("name") in wanted
        ]
    elif parsed_entries is not None:
        candidate_entries = parsed_entries
        candidate_names = [e["name"] for e in candidate_entries]
    else:
        candidate_entries, candidate_names = [], []

    dropped = set(replacing) | set(candidate_names)
    for name in sorted(set(replacing) - set(before_by_name)):
        notes.append(f"`replacing` names `{name}`, which is not in the running "
                     f"composition — it is ignored")
    # same shape `lower._link` builds: ambient minus what this admission
    # drops, then the newly compiled components.
    after_entries = [e for e in before_entries if e["name"] not in dropped] + candidate_entries
    after_by_name = {e["name"]: e for e in after_entries}
    after_order = (list((candidate_ir.get("manifest") or {}).get("loadOrder") or [])
                   if basis == "admitted" else None)

    services_by_key = {**_key_services(running_ir), **_key_services(candidate_ir),
                       **parsed_services}

    def _service(component: str, key: str) -> str | None:
        return services_by_key.get((component, key))

    def _providers(entries: list[dict]) -> dict[tuple[str, str], str]:
        out: dict[tuple[str, str], str] = {}
        for entry in entries:
            for key in entry.get("provides") or []:
                # a G2 conflict would have been rejected by the gate; on the
                # degraded paths, first writer wins and the diagnostic says why
                out.setdefault((key, _realm(entry, key)), entry["name"])
        return out

    before_providers = _providers(before_entries)
    after_providers = _providers(after_entries)

    # -- provisions ---------------------------------------------------------
    def _provision(key: str, realm: str, provider: str) -> dict:
        record = {"key": key, "service": _service(provider, key), "provider": provider}
        if _realm_name(realm) is not None:
            record["realm"] = realm
        return record

    gained, withdrawn_provisions, rebound_provisions, retained = [], [], [], []
    for (key, realm), provider in sorted(after_providers.items()):
        prior = before_providers.get((key, realm))
        if prior is None:
            gained.append(_provision(key, realm, provider))
        elif prior != provider or prior in dropped:
            record = _provision(key, realm, provider)
            record["from"] = prior
            record["to"] = provider
            record.pop("provider")
            rebound_provisions.append(record)
        else:
            retained.append(_provision(key, realm, provider))
    for (key, realm), provider in sorted(before_providers.items()):
        if (key, realm) not in after_providers:
            withdrawn_provisions.append(_provision(key, realm, provider))

    # -- components ---------------------------------------------------------
    replaced = []
    for entry in candidate_entries:
        prior = before_by_name.get(entry["name"])
        if prior is None:
            continue
        replaced.append({
            "name": entry["name"],
            "file": {"before": prior.get("file") or None, "after": entry.get("file") or None},
            "provides": {
                "before": list(prior.get("provides") or []),
                "after": list(entry.get("provides") or []),
                "added": sorted(set(entry.get("provides") or []) - set(prior.get("provides") or [])),
                "removed": sorted(set(prior.get("provides") or []) - set(entry.get("provides") or [])),
            },
            "requires": {
                "before": list(prior.get("inject") or []),
                "after": list(entry.get("inject") or []),
                "added": sorted(set(entry.get("inject") or []) - set(prior.get("inject") or [])),
                "removed": sorted(set(prior.get("inject") or []) - set(entry.get("inject") or [])),
            },
        })
    replaced_names = {r["name"] for r in replaced}
    added_names = [e["name"] for e in candidate_entries if e["name"] not in before_by_name]
    gone_names = sorted(n for n in before_by_name if n not in after_by_name)

    # -- the reactive cascade (predicted: R2/R3) ----------------------------
    # A running component that survives the admission is still disturbed if
    # a key it requires changes hands (deactivate + reactivate against the
    # replacement, R2) or loses its provider outright (deactivate, stays
    # PENDING). Both propagate: a disturbed provider disturbs its consumers.
    survivors = [e for e in after_entries
                 if e["name"] in before_by_name and e["name"] not in replaced_names]
    diverted: dict[str, dict] = {}
    rebound: dict[str, dict] = {}
    activated: dict[str, dict] = {}
    inactive = set(gone_names)
    restarted = set(replaced_names)

    changed = True
    while changed:
        changed = False
        for entry in survivors:
            name = entry["name"]
            if name in diverted:
                continue
            lost, upstream, moved, met = [], [], [], []
            for key in entry.get("inject") or []:
                realm = _realm(entry, key)
                prior = before_providers.get((key, realm))
                now = after_providers.get((key, realm))
                if now is not None and now in inactive:
                    # a provider that cannot itself activate provides nothing;
                    # if the key was already unmet, this changes nothing
                    if prior is not None:
                        upstream.append(key)
                elif prior is not None and now is None:
                    lost.append(key)
                elif prior is None and now is not None:
                    met.append(key)
                elif prior is not None and (now != prior or now in restarted
                                            or now in rebound):
                    moved.append(key)
            if lost or upstream:
                reason = ("a required provision is withdrawn and nothing replaces it"
                          if lost else
                          "the provider of a required key is itself diverted "
                          "(cascade)")
                diverted[name] = {
                    "name": name, "keys": sorted(lost + upstream),
                    "withdrawnKeys": sorted(lost), "upstreamKeys": sorted(upstream),
                    "reason": f"{reason} — the component deactivates and stays PENDING",
                }
                rebound.pop(name, None)
                activated.pop(name, None)
                if name not in inactive:
                    inactive.add(name)
                    changed = True
                continue
            if moved and name not in rebound:
                rebound[name] = {
                    "name": name, "keys": sorted(moved),
                    "reason": "a required provision changes provider — the component "
                              "deactivates and reactivates against the replacement (R2)",
                }
                restarted.add(name)
                changed = True
            if met and name not in activated and name not in rebound:
                activated[name] = {
                    "name": name, "keys": sorted(met),
                    "reason": "a previously unmet requirement is now provided — "
                              "the component can activate",
                }
                changed = True

    # -- teardown order (predicted: G7 LIFO, R3 consumers before providers) -
    torn_down = set(gone_names) | replaced_names | set(diverted) | set(rebound)
    ordered = before_order or [e["name"] for e in before_entries]
    teardown = [name for name in reversed(ordered) if name in torn_down]
    teardown += sorted(n for n in torn_down if n not in set(ordered))

    # -- emission surface (G8) ----------------------------------------------
    surface = _emission_surface(running_ir, candidate_ir, before_by_name,
                                after_by_name, replaced_names)

    # -- interface drift -----------------------------------------------------
    drift = _interface_drift(running_ir, candidate_ir, dropped)

    if basis != "admitted":
        notes.append("the admission gate rejected this candidate — nothing below "
                     "would happen; the delta is what it *would* have been")
    if basis == "parsed":
        notes.append("the candidate does not compile, so component headers were "
                     "recovered from the AST: realms and provision services may be "
                     "incomplete")
    if diverted:
        notes.append("a diverted component stays in the composition but cannot run; "
                     "its emission surface is unreachable while it is PENDING")

    result = {
        "ok": basis != "none",
        "admissible": admissible,
        "basis": basis,
        "diagnostics": diagnostics,
        "running": {
            "components": sorted(before_by_name),
            "loadOrder": before_order,
            "provisions": [_provision(key, realm, provider)
                           for (key, realm), provider in sorted(before_providers.items())],
        },
        "candidate": {"components": candidate_names, "replacing": list(replacing)},
        "resulting": {"components": sorted(after_by_name), "loadOrder": after_order},
        "provisions": {
            "gained": gained,
            "withdrawn": withdrawn_provisions,
            "rebound": rebound_provisions,
            "retained": retained,
        },
        "components": {
            "added": added_names,
            "replaced": replaced,
            "withdrawn": gone_names,
            "unchanged": sorted(n for n in after_by_name
                                if n in before_by_name and n not in replaced_names),
        },
        "cascade": {
            "withdrawn": gone_names,
            "diverted": [diverted[n] for n in sorted(diverted)],
            "rebound": [rebound[n] for n in sorted(rebound)],
            "activated": [activated[n] for n in sorted(activated)],
            "unaffected": sorted(e["name"] for e in survivors
                                 if e["name"] not in diverted and e["name"] not in rebound),
        },
        "teardownOrder": teardown,
        "emissionSurface": surface,
        "interfaceDrift": drift,
        "notes": notes,
        "guaranteed": [
            "admissibility (`admissible`) — the gate was actually run",
            "provisions gained/withdrawn/rebound, replacements and interface drift "
            "— read off the composition manifests the linker built",
            "the emission surface — `revl audit`'s own G8 walk over the same IR",
        ],
        "predicted": [
            "the reactive cascade (diverted/rebound/activated) — the runtime "
            "contract R2/R3 applied to the manifest graph, not an observation",
            "the teardown order — derived LIFO (G7) over the running load order; "
            "the *inverses* each component replays depend on what it acquired at "
            "runtime and are not visible in a manifest",
        ],
    }
    # `revl apply` needs the resulting composition's bodies to execute the
    # plan; only the admitted path has them, and only when asked (the plan's
    # prose output never carries an IR).
    if include_ir and basis == "admitted" and candidate_ir is not None:
        result["resultingIR"] = _merge_resulting_ir(running_ir, candidate_ir, dropped)
    return result


def _emission_surface(running_ir: dict | None, candidate_ir: dict | None,
                      before_by_name: dict, after_by_name: dict,
                      replaced_names: set) -> dict:
    """Irreversible reach the composition gains and loses (G8).

    Both halves come from `revl audit`'s `_boundary`; retained running
    components keep their existing entry, new and replaced ones take the
    candidate's. Withdrawn components drop off the surface entirely.
    """
    # a caller may hand us `{manifest, services}` instead of a full IR; then
    # there are no component bodies to walk and the *before* surface is
    # simply unknown. Say so rather than reporting a false gain.
    before: dict = {}
    bodies_available = bool((running_ir or {}).get("components"))
    if bodies_available:
        before = _boundary_of(running_ir)
    unavailable_before = bool(before_by_name) and not bodies_available

    candidate = _boundary_of(candidate_ir) if candidate_ir else {}

    after: dict = {}
    for name in after_by_name:
        if name in candidate:
            after[name] = candidate[name]
        elif name in before:
            after[name] = before[name]
    # a replaced component's *new* body is the candidate's
    for name in replaced_names & set(candidate):
        after[name] = candidate[name]

    def _reach(boundary: dict) -> tuple[set, set]:
        emissions = {f"{comp}.{e}" for comp, stats in boundary.items()
                     for e in stats.get("emissions") or []}
        host = {f"{comp} -> {ext['name']}" for comp, stats in boundary.items()
                for ext in stats.get("externs") or []}
        return emissions, host

    def _totals(boundary: dict) -> dict:
        emissions = sum(len(s.get("emissions") or []) for s in boundary.values())
        compensated = sum(s.get("compensated") or 0 for s in boundary.values())
        return {
            "emissionSites": emissions,
            "compensated": compensated,
            "iterationBoundaries": sum(s.get("awaits") or 0 for s in boundary.values()),
            "hostCalls": sum(len(s.get("externs") or []) for s in boundary.values()),
            "components": len(boundary),
        }

    before_emissions, before_host = _reach(before)
    after_emissions, after_host = _reach(after)

    changed = {}
    for name in sorted(set(before) | set(after)):
        if before.get(name) != after.get(name):
            changed[name] = {"before": before.get(name), "after": after.get(name)}

    surface = {
        "basis": "unavailable" if unavailable_before else "computed",
        "gained": {
            "emissions": sorted(after_emissions - before_emissions),
            "hostCode": sorted(after_host - before_host),
        },
        "withdrawn": {
            "emissions": sorted(before_emissions - after_emissions),
            "hostCode": sorted(before_host - after_host),
        },
        "before": _totals(before),
        "after": _totals(after),
        "byComponent": changed,
    }
    if unavailable_before:
        surface["note"] = ("the running manifest carries no component bodies, so "
                           "the *before* surface is unknown — only the candidate's "
                           "own contribution is reported")
    return surface


def _interface_drift(running_ir: dict | None, candidate_ir: dict | None,
                     dropped: frozenset | set = frozenset()) -> list[dict]:
    """Services the candidate redeclares *incompatibly*.

    This previews exactly what the admission gate would refuse
    (`lower._service_compatible`, docs/service-compat.md): a method removal,
    a parameter narrowing (or arity change), a return widening, an emission
    appearance or capability widening — each relative to the running
    components that actually touch the interface. A *compatible* evolution
    (a method added, a parameter widened, a return narrowed, an emission
    dropped) is not drift and is not reported; neither is a change to a
    service nothing running consumes or provides.

    Both tables are lowered by the same code, so the check runs the gate's
    own relation over `lower._service_from_ir` projections of the two IRs.
    """
    if not running_ir or not candidate_ir:
        return []
    running = running_ir.get("services") or {}
    candidate = candidate_ir.get("services") or {}
    # `check_and_lower` merges ambient services into the candidate's table
    # verbatim, so on the admitted path every shared name compares equal and
    # this list is empty by construction. It only fills in on the standalone
    # path, which is exactly where the author needs it.
    from .lower import (_service_compatible, _service_from_ir,
                        _service_touchers)
    # the same ambient view the gate itself sees: running components minus
    # what this admission drops, plus the key -> service map read off the
    # running document's `components` (whose `provides` is {key: service}).
    running_manifest = running_ir.get("manifest", running_ir) or {}
    key_service: dict = {}
    for comp in running_ir.get("components") or []:
        provides = comp.get("provides")
        if isinstance(provides, dict):
            key_service.update(provides)
    ambient = {
        "services": running,
        "components": [e for e in running_manifest.get("components") or []
                       if e.get("name") not in dropped],
        "provision_services": key_service,
    }
    drift = []
    for name in sorted(set(running) & set(candidate)):
        old = _service_from_ir(name, running[name] or {})
        new = _service_from_ir(name, candidate[name] or {})
        touch = _service_touchers(name, ambient)
        # a retained provider (or an unresolved key that might be one) pins
        # the interface to identity — exactly as the gate decides it
        incompatible = _service_compatible(new, old,
                                           bool(touch.providers)
                                           or touch.unresolved)
        if incompatible is None:
            continue
        if not (touch.consumers or touch.providers or touch.unresolved):
            continue  # nothing running touches it: the gate admits any change
        drift.append({
            "service": name,
            "method": incompatible.method,
            "kind": incompatible.kind,
            "reason": incompatible.reason,
            "consumers": list(touch.consumers),
            "providers": list(touch.providers),
        })
    return drift


# ---------------------------------------------------------------- rendering

def _bullet(items: list[str], empty: str = "—") -> str:
    return ", ".join(items) if items else empty


def render(result: dict) -> str:
    """The human-readable plan the CLI prints."""
    out: list[str] = []
    verdict = "ADMISSIBLE" if result["admissible"] else "REJECTED"
    out.append(f"plan: {verdict}   (basis: {result['basis']})")

    for diagnostic in result["diagnostics"]:
        where = f"{diagnostic.get('file')}:{diagnostic.get('line')}"
        label = ("rejection" if diagnostic.get("from") in (None, "admission")
                 else "also")
        out.append(f"\n{label} [{diagnostic.get('code')}] {where}")
        out.append(f"  {diagnostic.get('message')}")
        if diagnostic.get("guarantee"):
            out.append(f"  guarantee: {diagnostic['guarantee']}")
        if diagnostic.get("hint"):
            out.append(f"  hint: {diagnostic['hint']}")
        if diagnostic.get("note"):
            out.append(f"  note: {diagnostic['note']}")

    running = result["running"]
    resulting = result["resulting"]
    out.append(f"\nrunning:   {_bullet(running['components'], '(nothing)')}")
    if running["loadOrder"]:
        out.append(f"  load order: {' -> '.join(running['loadOrder'])}")
    out.append(f"resulting: {_bullet(resulting['components'], '(nothing)')}")
    if resulting["loadOrder"]:
        out.append(f"  load order: {' -> '.join(resulting['loadOrder'])}")

    comps = result["components"]
    out.append("\ncomponents")
    out.append(f"  added:     {_bullet(comps['added'])}")
    out.append(f"  replaced:  {_bullet([r['name'] for r in comps['replaced']])}")
    for entry in comps["replaced"]:
        detail = []
        for label, block in (("provides", entry["provides"]), ("requires", entry["requires"])):
            if block["added"]:
                detail.append(f"+{label} {', '.join(block['added'])}")
            if block["removed"]:
                detail.append(f"-{label} {', '.join(block['removed'])}")
        out.append(f"    {entry['name']}: {'; '.join(detail) or 'same interface'}")
    out.append(f"  withdrawn: {_bullet(comps['withdrawn'])}")

    def _render_provisions(label: str, records: list[dict]) -> None:
        if not records:
            return
        out.append(f"  {label}:")
        for record in records:
            realm = f" in realm `{record['realm']}`" if record.get("realm") else ""
            service = record.get("service") or "?"
            if "from" in record:
                # same name on both sides is the implicit-replacement case:
                # a new instance of the component takes the provision over
                move = (f"{record['to']} (replaced)" if record["from"] == record["to"]
                        else f"{record['from']} -> {record['to']}")
                out.append(f"    {record['key']}: {service}{realm}  {move}")
            else:
                out.append(f"    {record['key']}: {service}{realm}  "
                           f"({record['provider']})")

    provisions = result["provisions"]
    out.append("\nprovisions")
    _render_provisions("gained", provisions["gained"])
    _render_provisions("withdrawn", provisions["withdrawn"])
    _render_provisions("rebound", provisions["rebound"])
    if not (provisions["gained"] or provisions["withdrawn"] or provisions["rebound"]):
        out.append("  no change")

    cascade = result["cascade"]
    out.append("\nreactive cascade (predicted — R2/R3)")
    if not (cascade["withdrawn"] or cascade["diverted"] or cascade["rebound"]
            or cascade["activated"]):
        out.append("  nothing running is disturbed")
    for name in cascade["withdrawn"]:
        out.append(f"  withdrawn  {name}  leaves the composition")
    for entry in cascade["diverted"]:
        out.append(f"  DIVERTED   {entry['name']}  ({', '.join(entry['keys'])})")
        out.append(f"             {entry['reason']}")
    for entry in cascade["rebound"]:
        out.append(f"  rebound    {entry['name']}  ({', '.join(entry['keys'])})")
    for entry in cascade["activated"]:
        out.append(f"  activated  {entry['name']}  ({', '.join(entry['keys'])})")
    if cascade["unaffected"]:
        out.append(f"  unaffected {_bullet(cascade['unaffected'])}")

    out.append("\nteardown order (predicted — LIFO, consumers before providers)")
    out.append(f"  {' -> '.join(result['teardownOrder']) or '(nothing tears down)'}")

    surface = result["emissionSurface"]
    out.append("\nemission surface (G8)")
    if surface.get("note"):
        out.append(f"  note: {surface['note']}")
    for label in ("gained", "withdrawn"):
        block = surface[label]
        if block["emissions"]:
            out.append(f"  {label} emissions: {', '.join(block['emissions'])}")
        if block["hostCode"]:
            out.append(f"  {label} host code: {', '.join(block['hostCode'])}")
    if not any(surface[l][k] for l in ("gained", "withdrawn")
               for k in ("emissions", "hostCode")):
        out.append("  unchanged — the composition's irreversible reach is the same")
    before, after = surface["before"], surface["after"]
    out.append(f"  totals: emission sites {before['emissionSites']} -> "
               f"{after['emissionSites']} ({before['compensated']} -> "
               f"{after['compensated']} compensated); iteration boundaries "
               f"{before['iterationBoundaries']} -> {after['iterationBoundaries']}; "
               f"host calls {before['hostCalls']} -> {after['hostCalls']}")

    if result["interfaceDrift"]:
        out.append("\ninterface drift (would be refused)")
        for entry in result["interfaceDrift"]:
            method = f", method `{entry['method']}`" if entry.get("method") else ""
            out.append(f"  service {entry['service']}{method} — {entry['kind']}")
            out.append(f"    {entry['reason']}")
            affected = list(entry["consumers"]) + list(entry["providers"])
            if affected:
                out.append(f"    running components affected: {', '.join(affected)}")

    if result["notes"]:
        out.append("\nnotes")
        for note in result["notes"]:
            out.append(f"  - {note}")

    out.append("\nthis is a plan: nothing was compiled to disk, admitted or swapped.")
    out.append("  guaranteed: " + "; ".join(result["guaranteed"]))
    out.append("  predicted:  " + "; ".join(result["predicted"]))
    return "\n".join(out)
