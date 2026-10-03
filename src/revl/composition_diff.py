"""`revl diff` — the semantic composition diff (roadmap item 123).

This is the PR-review tool for agent-generated compositions. Given two
generations of a composition (before/after a change — two compiled IR
documents, or two sources), it reports the **IR-level structural delta**, not
a textual one: which components were added / removed / changed, which
emissions each component gained or lost, which provide/require dependencies
were introduced or broken. The output speaks in guarantees —

    component `Front` gained emission `cache.put`
    provider of key `db` changed from `PgDatabase` to `MysqlDatabase`
    `Auth` now requires `session`

— never "line 14 changed".

Design: this builds *on top of* `audit_diff`, it does not reinvent the drift
relation. The authority axis — the per-component emission and reached-host
surface — is exactly `audit_diff.diff_crossings` over `audit_diff.crossings`.
`diff` adds the two axes the authority gate does not model: the **membership**
axis (components added/removed/changed) and the **wiring** axis (the
provide/require dependency edges of the composition graph).
"""

from __future__ import annotations

import json

from .audit_diff import audit_report, diff_crossings
from .lower import SHARED_REALM


def load_composition(path: str) -> dict:
    """Load one side of the diff as a compiled IR document.

    Accepts either a compiled IR / interchange JSON document (the output of
    `revl compile -o` or `revl audit --json`) or a `.rvl` source file, which is
    compiled on the spot. Detection is by content: a JSON object carrying an
    `ir_version`, `components`, `manifest`, or `boundary` key is taken as an
    already-compiled document; anything else is compiled as source.
    """
    from .compiler import compile_files  # noqa: PLC0415 — lazy, avoids cycle

    if path.endswith(".rvl"):
        return compile_files([path])
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return compile_files([path])
    if isinstance(doc, dict) and (
            "ir_version" in doc or "components" in doc
            or "manifest" in doc or "boundary" in doc):
        return doc
    return compile_files([path])


def _components(ir: dict) -> dict[str, dict]:
    """Name -> the component's IR record (with `requires`/`provides` dicts).

    A compiled IR document (`revl compile -o`) carries the full key->service
    wiring; an audit/interchange document carries only the manifest, whose
    entries name the keys but not the providing service. Both are accepted:
    the manifest is the fallback so a degraded input still diffs membership
    and edges, just without provider-service identities.
    """
    comps = ir.get("components")
    if comps:
        out: dict[str, dict] = {}
        for comp in comps:
            out[comp["name"]] = {
                "requires": dict(comp.get("requires") or {}),
                "provides": dict(comp.get("provides") or {}),
                "isolate": dict(comp.get("isolate") or {}),
            }
        return out
    # fallback: manifest entries (keys only, service identity unknown)
    out = {}
    for entry in ((ir.get("manifest") or {}).get("components") or []):
        out[entry["name"]] = {
            "requires": {k: None for k in entry.get("inject") or []},
            "provides": {k: None for k in entry.get("provides") or []},
            "isolate": dict(entry.get("isolate") or {}),
        }
    return out


def _realm(rec: dict, key: str) -> str:
    """The realm one component reaches `key` in: its `isolate` for the key, or
    the shared realm. The same rule `query.Composition.realm` applies, so a
    provider is matched to exactly the consumers `revl_query` matches it to."""
    return rec["isolate"].get(key, SHARED_REALM)


def _record(key: str, realm: str, **fields) -> dict:
    """A provider or edge record. `realm` appears only for a realm-scoped key,
    so a single-realm composition's delta is the same as before."""
    out = {"key": key}
    if realm != SHARED_REALM:
        out["realm"] = realm
    out.update(fields)
    return out


def _in_realm(record: dict) -> str:
    """` in realm `b`` for a realm-scoped record, else nothing."""
    return f" in realm `{record['realm']}`" if record.get("realm") else ""


def facts(ir: dict) -> dict:
    """The three guarantee surfaces of one composition, in a shape set-diffable
    against another generation's facts.

        components   name -> {requires:{key:service}, provides:{key:service},
                              isolate:{key:realm}}
        providers    (key, realm) -> {service, component}  (the DI wiring)
        require_edges set of (component, key, realm)

    Keyed by realm (issue #1848): two realms may each provide `db`, and a
    consumer is satisfied only by the provider in ITS realm. Keyed by key
    alone, the second provider overwrote the first, and removing realm B's
    provider left B's consumers reading as satisfied while A still provided
    the key.
        audit        the `audit_diff.audit_report` — reused verbatim for the
                     authority (emission / reached-host) surface
    """
    comps = _components(ir)
    providers: dict[tuple[str, str], dict] = {}
    require_edges: set[tuple[str, str, str]] = set()
    for name, rec in comps.items():
        for key, service in rec["provides"].items():
            providers[(key, _realm(rec, key))] = {"service": service, "component": name}
        for key in rec["requires"]:
            require_edges.add((name, key, _realm(rec, key)))
    return {
        "components": comps,
        "providers": providers,
        "require_edges": require_edges,
        "audit": audit_report(ir),
    }


def _emission_scope(audit: dict, component: str, label: str) -> list[str]:
    """The capability scope declared for one emission label, e.g. `["bus"]`;
    `["*"]` (an unscoped emission) is reported as no scope."""
    caps = ((audit.get("boundary") or {}).get(component) or {}).get("capabilities") or {}
    scope = caps.get(label)
    if not scope or scope == ["*"]:
        return []
    return scope


def _split(token: str) -> tuple[str, str, str]:
    """`emit:Comp:label` / `host:Comp:name` -> (kind, component, label)."""
    kind, component, label = token.split(":", 2)
    return kind, component, label


def diff(before: dict, after: dict) -> dict:
    """The whole-composition structural delta between two compiled IRs.

    Reuses `audit_diff.diff_crossings` for the authority surface; adds the
    membership (components) and wiring (providers / require edges) surfaces.
    """
    bf, af = facts(before), facts(after)

    before_names = set(bf["components"])
    after_names = set(af["components"])
    added_c = sorted(after_names - before_names)
    removed_c = sorted(before_names - after_names)
    common_c = before_names & after_names

    # authority surface — the reused drift relation
    cross = diff_crossings(bf["audit"], af["audit"])

    # providers (the DI wiring), keyed by (DI key, realm)
    prov_added, prov_removed, prov_changed = [], [], []
    for key, realm in sorted(set(af["providers"]) - set(bf["providers"])):
        prov_added.append(_record(key, realm, **af["providers"][(key, realm)]))
    for key, realm in sorted(set(bf["providers"]) - set(af["providers"])):
        prov_removed.append(_record(key, realm, **bf["providers"][(key, realm)]))
    for key, realm in sorted(set(bf["providers"]) & set(af["providers"])):
        b, a = bf["providers"][(key, realm)], af["providers"][(key, realm)]
        # a provider *swap* is a change of the concrete providing component
        # (the DI wiring), or of the service interface it satisfies
        if (b["component"], b["service"]) != (a["component"], a["service"]):
            prov_changed.append(_record(
                key, realm,
                **{"from": b["component"], "to": a["component"],
                   "from_service": b["service"], "to_service": a["service"]}))

    # require edges (the depends-on graph)
    req_added = sorted(af["require_edges"] - bf["require_edges"])
    req_removed = sorted(bf["require_edges"] - af["require_edges"])

    # a require edge is *broken* when the key has no provider IN ITS REALM in
    # that generation; a NEWLY broken edge (satisfiable before, dangling now) is
    # the dependency the change quietly severed.
    def _broken(f: dict) -> set[tuple[str, str, str]]:
        provided = set(f["providers"])
        return {(c, k, r) for (c, k, r) in f["require_edges"] if (k, r) not in provided}

    broken_after = _broken(af)
    newly_broken = sorted(broken_after - _broken(bf))

    # a component present in both generations is *changed* if any of its three
    # surfaces moved
    changed_c = []
    for name in sorted(common_c):
        touched = any(_split(t)[1] == name for t in cross["added"] + cross["removed"])
        req_moved = any(c == name for (c, _k, _r) in req_added + req_removed)
        prov_moved = (
            any(p["component"] == name for p in prov_added + prov_removed)
            or any(p["from"] == name or p["to"] == name
                   for p in prov_changed))
        if touched or req_moved or prov_moved:
            changed_c.append(name)

    delta = {
        "components": {"added": added_c, "removed": removed_c, "changed": changed_c},
        "providers": {"added": prov_added, "removed": prov_removed,
                      "changed": prov_changed},
        "requires": {
            "added": [_edge(c, k, r) for (c, k, r) in req_added],
            "removed": [_edge(c, k, r) for (c, k, r) in req_removed],
            "broken": [_edge(c, k, r) for (c, k, r) in newly_broken],
        },
        "crossings": {"added": cross["added"], "removed": cross["removed"]},
    }
    delta["guarantees"] = _guarantees(delta, bf, af)
    delta["changed"] = _nonempty(delta)
    return delta


def _edge(component: str, key: str, realm: str) -> dict:
    out = {"component": component, "key": key}
    if realm != SHARED_REALM:
        out["realm"] = realm
    return out


def _nonempty(delta: dict) -> bool:
    c = delta["components"]
    p = delta["providers"]
    r = delta["requires"]
    return bool(c["added"] or c["removed"] or c["changed"]
                or p["added"] or p["removed"] or p["changed"]
                or r["added"] or r["removed"]
                or delta["crossings"]["added"] or delta["crossings"]["removed"])


def _guarantees(delta: dict, bf: dict, af: dict) -> list[str]:
    """The human/agent-readable guarantee sentences — the delta stated as
    changes to what the composition promises, not as text edits."""
    added_c = set(delta["components"]["added"])
    removed_c = set(delta["components"]["removed"])
    out: list[str] = []

    for name in delta["components"]["added"]:
        out.append(f"component `{name}` added")
    for name in delta["components"]["removed"]:
        out.append(f"component `{name}` removed")

    # authority surface — suppress per-crossing lines for whole components that
    # were added/removed (the membership line already says it); keep the detail
    # for components that merely changed.
    for token in delta["crossings"]["added"]:
        kind, comp, label = _split(token)
        if comp in added_c:
            continue
        if kind == "emit":
            scope = _emission_scope(af["audit"], comp, label)
            tail = f" [{', '.join(scope)}]" if scope else ""
            out.append(f"component `{comp}` gained emission `{label}`{tail}")
        else:
            out.append(f"component `{comp}` now reaches host code `{label}`")
    for token in delta["crossings"]["removed"]:
        kind, comp, label = _split(token)
        if comp in removed_c:
            continue
        if kind == "emit":
            out.append(f"component `{comp}` lost emission `{label}`")
        else:
            out.append(f"component `{comp}` no longer reaches host code `{label}`")

    for p in delta["providers"]["changed"]:
        if p["from"] != p["to"]:
            out.append(f"provider of key `{p['key']}`{_in_realm(p)} changed from "
                       f"`{p['from']}` to `{p['to']}`")
        else:
            out.append(f"provider of key `{p['key']}`{_in_realm(p)} (`{p['from']}`) now "
                       f"satisfies service `{p['to_service']}` "
                       f"(was `{p['from_service']}`)")
    for p in delta["providers"]["added"]:
        svc = p.get("service")
        via = f" by `{svc}`" if svc else ""
        out.append(f"key `{p['key']}`{_in_realm(p)} is now provided{via} "
                   f"(component `{p['component']}`)")
    for p in delta["providers"]["removed"]:
        svc = p.get("service")
        was = f" (was `{svc}`)" if svc else ""
        out.append(f"key `{p['key']}`{_in_realm(p)} is no longer provided{was}")

    for e in delta["requires"]["added"]:
        if e["component"] in added_c:
            continue
        out.append(f"`{e['component']}` now requires `{e['key']}`")
    for e in delta["requires"]["removed"]:
        if e["component"] in removed_c:
            continue
        out.append(f"`{e['component']}` no longer requires `{e['key']}`")
    for e in delta["requires"]["broken"]:
        out.append(f"`{e['component']}` requires `{e['key']}`{_in_realm(e)} — "
                   f"no provider in the composition (broken dependency)")

    return out


def render(delta: dict, before_label: str, after_label: str) -> str:
    """Human-readable report of a `diff` result."""
    if not delta["changed"]:
        return (f"composition diff: no structural change "
                f"({before_label} -> {after_label}).")

    lines = [f"composition diff: {before_label} -> {after_label}", ""]

    c = delta["components"]
    if c["added"] or c["removed"] or c["changed"]:
        lines.append("components:")
        for name in c["added"]:
            lines.append(f"  + {name}  (added)")
        for name in c["removed"]:
            lines.append(f"  - {name}  (removed)")
        for name in c["changed"]:
            lines.append(f"  ~ {name}  (changed)")
        lines.append("")

    # the membership sentences are already shown in the components block above
    membership = {f"component `{n}` added" for n in c["added"]}
    membership |= {f"component `{n}` removed" for n in c["removed"]}
    for guarantee in delta["guarantees"]:
        if guarantee in membership:
            continue
        lines.append(f"  {guarantee}")

    return "\n".join(lines).rstrip()
