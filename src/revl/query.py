"""Composition queries — ask the audit, don't just read it.

`revl audit` dumps the manifest and the G8 boundary surface. That is the
right shape for a review and the wrong shape for a refactor: an author (or
an agent) editing a live system has a *question*, not a browsing need.

    who emits to `db`?                    -> emitters()
    what breaks if I withdraw PgDatabase? -> withdrawal()
    who depends on `cache` / `Database`?  -> dependents()
    what does UserCache reach?            -> reach()
    what if `Database` loses `execute`?   -> drift()

Every result carries its own precision. Two of these queries are exact —
they read a graph the linker already resolved, where G2 makes each
`(key, realm)` provision unique and G3 makes the graph acyclic — and three
are may-analyses: every site they list is reachable on some path, but a
listed site is not a promise that it runs on every activation. An agent
acting on a result must be able to tell a proof from a guess without
reading the docs, so `precision`, `precisionNote` and `assumptions` are
fields of the result itself, not prose in `docs/queries.md`.

The emission reachability through the pure stratum is *not* recomputed
here: `lower._emitting_fns` is the checker's own least fixed point over the
fn call graph (the same set G4 uses to force `emission` into a service
declaration), and `__main__._extern_reachability` names which externs a fn
reaches. This module consumes both and adds only what neither has: the
inter-component edges (a call on an injected key lands in the provider's
provide-method) and the withdrawal cascade.
"""

from __future__ import annotations

from .lower import SHARED_REALM

# precision vocabulary — every result says which of the two it is
EXACT = "exact"
APPROX = "over-approximation"

_ASSUMPTION_EXTERN = (
    "extern host bodies are opaque: a `pure` or `acquire` extern whose host "
    "code actually crosses the boundary is invisible here. The compiler "
    "enforces how a classification is *used*, not that it is truthful."
)
_ASSUMPTION_SWAP = (
    "the answer is for the composition as linked right now; a hot swap that "
    "replaces a provider changes which body a service call lands in."
)
_ASSUMPTION_SCOPE = (
    "only components present in this IR are considered — ambient components "
    "of a running composition that were not compiled in are not visible."
)
_ASSUMPTION_MAY = (
    "may-analysis: a site is listed when a path to the target exists, not "
    "when one is guaranteed to be taken. Branch arms, `match` arms and arrow "
    "bodies handed to builtins all count as reachable."
)


# ---------------------------------------------------------------- reuse

def _emitting_fn_names(ir: dict) -> set:
    """The checker's own set of fn names whose call reaches an irreversible
    host effect. Imported from `lower` rather than recomputed so this module
    can never disagree with the gate that rejects code."""
    from .lower import _emitting_fns

    return _emitting_fns(list(ir.get("functions") or []), list(ir.get("externs") or []))


def _fn_extern_reach(ir: dict) -> dict:
    """fn name -> externs it transitively reaches. `__main__` owns this walk
    (it is what `revl audit` prints); imported lazily because `__main__`
    imports the rest of the toolchain and this module is also a library."""
    from .__main__ import _extern_reachability

    reach = dict(_extern_reachability(ir))
    reach.pop("__externs__", None)
    return reach


def _called_names(node) -> set:
    """Callable names a lowered node references — `lower`'s own walk, which
    knows both call encodings (component `{kind: fn}`, pure `{kind: call,
    callee: {kind: var}}`) and looks inside arrow bodies."""
    from .lower import _calls_in

    found: set = set()
    _calls_in(node, found)
    return found


# ---------------------------------------------------------------- index


class Composition:
    """Everything the queries read, resolved once: the linked provider graph,
    the scopes (a component's activation body and each provide-method), and
    the direct boundary facts of each scope."""

    def __init__(self, ir: dict) -> None:
        self.ir = ir
        self.manifest = ir.get("manifest") or {}
        self.services = ir.get("services") or {}
        self.externs = {e["name"]: e for e in ir.get("externs") or []}
        self.functions = {f["name"]: f for f in ir.get("functions") or []}
        self.components = {c["name"]: c for c in ir.get("components") or []}
        self.entries = {e["name"]: e for e in self.manifest.get("components") or []}
        self.load_order = list(self.manifest.get("loadOrder") or [])
        self.emitting_fns = _emitting_fn_names(ir)
        self.fn_externs = _fn_extern_reach(ir)

        # provision resolution is per-(key, realm): the same key in two realms
        # is multi-tenancy, not a conflict (docs/design-v2-realms.md)
        self.provider_of: dict[tuple, str] = {}
        for entry in self.entries.values():
            for key in entry.get("provides") or []:
                self.provider_of[(key, self.realm(entry["name"], key))] = entry["name"]

        self.scopes: dict[str, dict] = {}
        self.scopes_of: dict[str, list] = {}
        for name in self.entries:
            self.scopes_of[name] = []
        for comp in ir.get("components") or []:
            for scope in _scopes_of_component(comp):
                self.scopes[scope["id"]] = scope
                self.scopes_of.setdefault(comp["name"], []).append(scope["id"])
                scope["facts"] = self._facts(comp, scope)

        # scope -> [(callee scope id, key, method)] across the service seam
        self.edges: dict[str, list] = {}
        for scope_id, scope in self.scopes.items():
            out = []
            for call in scope["facts"]["calls"]:
                target = self.method_scope(scope["component"], call["key"], call["method"])
                if target is not None:
                    out.append({"scope": target, "key": call["key"],
                                "method": call["method"]})
            self.edges[scope_id] = out

    # -- graph --------------------------------------------------------

    def realm(self, component: str, key: str) -> str:
        entry = self.entries.get(component) or {}
        return (entry.get("isolate") or {}).get(key, SHARED_REALM)

    def provider(self, consumer: str, key: str):
        return self.provider_of.get((key, self.realm(consumer, key)))

    def service_of(self, component: str, key: str):
        comp = self.components.get(component) or {}
        return (comp.get("requires") or {}).get(key) \
            or (comp.get("provides") or {}).get(key)

    def method_scope(self, consumer: str, key: str, method: str):
        provider = self.provider(consumer, key)
        if provider is None:
            return None
        candidate = f"{provider}:{key}.{method}"
        return candidate if candidate in self.scopes else None

    def unresolved_injections(self, component: str) -> list:
        entry = self.entries.get(component) or {}
        return [key for key in entry.get("inject") or []
                if self.provider(component, key) is None]

    # -- per-scope boundary facts -------------------------------------

    def _facts(self, comp: dict, scope: dict) -> dict:
        nodes = scope["nodes"]
        emissions: dict = {}
        calls: dict = {}
        awaits = 0
        compensated = 0

        for node in _walk(nodes):
            if not isinstance(node, dict):
                continue
            if node.get("step") == "await":
                awaits += 1
            if node.get("step") == "emit":
                if node.get("compensate") is not None:
                    compensated += 1
                    for pair in self._service_calls(comp, node.get("expr")):
                        emissions[pair] = True
            target = node.get("target")
            if node.get("kind") == "call" and isinstance(target, dict) \
                    and target.get("kind") == "req":
                key, method = target.get("name"), node.get("method")
                spec = self._method_spec(comp, key, method)
                calls[(key, method)] = bool(spec.get("emission"))
                if spec.get("emission"):
                    emissions.setdefault((key, method), False)

        # host code: externs called directly, plus everything the pure fns
        # this scope calls reach transitively (lower/`__main__` own that walk)
        host: dict = {}
        for name in sorted(_called_names(nodes)):
            if name in self.externs:
                host.setdefault(name, set())
            elif name in self.fn_externs:
                for reached in self.fn_externs[name]:
                    host.setdefault(reached, set()).add(name)

        return {
            "emissions": [
                {"kind": "service", "key": key, "method": method,
                 "service": self.service_of(comp["name"], key),
                 "compensated": flag}
                for (key, method), flag in sorted(emissions.items())
            ],
            "externs": [
                {"kind": "extern", "name": name,
                 "class": (self.externs.get(name) or {}).get("class"),
                 "emission": (self.externs.get(name) or {}).get("class") == "emission",
                 "backends": sorted((self.externs.get(name) or {}).get("bodies") or {}),
                 "through": sorted(through) or None}
                for name, through in sorted(host.items())
            ],
            "calls": [{"key": key, "method": method, "emission": flag}
                      for (key, method), flag in sorted(calls.items())],
            "awaits": awaits,
            "compensated": compensated,
        }

    def _method_spec(self, comp: dict, key, method) -> dict:
        service = (comp.get("requires") or {}).get(key)
        return (((self.services.get(service) or {}).get("methods") or {})
                .get(method) or {})

    def _service_calls(self, comp: dict, node) -> list:
        found = []
        for inner in _walk(node):
            if isinstance(inner, dict) and inner.get("kind") == "call":
                target = inner.get("target")
                if isinstance(target, dict) and target.get("kind") == "req":
                    found.append((target.get("name"), inner.get("method")))
        return found

    # -- transitive closure over the service seam ---------------------

    def closure(self, scope_id: str) -> list:
        """Scopes reachable from `scope_id`, with the shortest call path to
        each. The graph is acyclic (G3) but the visited set stands anyway."""
        seen = {scope_id: []}
        frontier = [(scope_id, [])]
        out = []
        while frontier:
            current, path = frontier.pop(0)
            for edge in self.edges.get(current, []):
                nxt = edge["scope"]
                if nxt in seen:
                    continue
                hop = path + [{"from": current, "key": edge["key"],
                               "method": edge["method"], "to": nxt}]
                seen[nxt] = hop
                out.append({"scope": nxt, "path": hop, "depth": len(hop)})
                frontier.append((nxt, hop))
        return out


def _scopes_of_component(comp: dict) -> list:
    """A component splits into the activation body (its effects, emits and
    awaits) and one scope per provide-method — the two have different
    lifetimes, and "who emits to X" is uninteresting if it cannot tell the
    two apart. `provide` is a top-level-only statement, so the split is a
    partition."""
    name = comp["name"]
    activation = []
    scopes = []
    for step in comp.get("body") or []:
        if isinstance(step, dict) and step.get("step") == "provide":
            activation.append({k: v for k, v in step.items() if k != "methods"})
            for method in step.get("methods") or []:
                scopes.append({
                    "id": f"{name}:{step.get('name')}.{method.get('name')}",
                    "component": name, "kind": "provide-method",
                    "key": step.get("name"), "method": method.get("name"),
                    "label": f"{name}.{step.get('name')}.{method.get('name')}",
                    "nodes": method.get("body") or [],
                })
        else:
            activation.append(step)
    scopes.insert(0, {
        "id": f"{name}:activation", "component": name, "kind": "activation",
        "key": None, "method": None, "label": f"{name} (activation)",
        "nodes": activation,
    })
    return scopes


def _walk(node):
    """Every dict/list node in a lowered tree, root first."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            yield current
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


# ---------------------------------------------------------------- results


def _result(query: str, question: str, precision: str, note: str,
            assumptions: list, **payload) -> dict:
    return {"ok": True, "query": query, "question": question,
            "precision": precision, "precisionNote": note,
            "assumptions": assumptions, **payload}


def _unknown(query: str, what: str, name, known: list) -> dict:
    return {"ok": False, "query": query,
            "error": f"unknown {what}: {name!r}",
            "known": sorted(known)}


def _site(scope: dict) -> dict:
    return {"component": scope["component"], "scope": scope["kind"],
            "key": scope["key"], "method": scope["method"],
            "label": scope["label"], "id": scope["id"]}


def _hops(path: list) -> list:
    return [f"{hop['key']}.{hop['method']}" for hop in path]


# ---------------------------------------------------------------- queries


def emitters(ir: dict, target: str) -> dict:
    """who emits to X? — every component/method whose irreversible reach
    includes a service key, a `key.method`, a service, or an extern,
    directly or through pure `fn` calls and through the service seam."""
    index = Composition(ir)
    interpretations = _interpret(index, target)
    if not interpretations:
        return _unknown("emitters", "target", target, _targets(index))

    matches = _matcher(index, interpretations)
    sites = []
    for scope_id, scope in index.scopes.items():
        for fact in _emission_facts(scope["facts"]):
            if matches(scope["component"], fact):
                sites.append({**_site(scope), "direct": True, "path": [],
                              "distance": 0, "reaches": fact})
        for step in index.closure(scope_id):
            reached = index.scopes[step["scope"]]
            for fact in _emission_facts(reached["facts"]):
                if matches(reached["component"], fact):
                    sites.append({**_site(scope), "direct": False,
                                  "path": _hops(step["path"]),
                                  "via": [hop["to"] for hop in step["path"]],
                                  "distance": step["depth"], "reaches": fact})

    sites.sort(key=lambda s: (s["distance"], s["id"], str(s["reaches"])))
    return _result(
        "emitters", f"who emits to `{target}`?", APPROX,
        "every site listed can reach the target on some path, and no site "
        "that can reach it through a declared `fn`, `extern` or service call "
        "is omitted — but reaching it is not the same as doing it.",
        [_ASSUMPTION_MAY, _ASSUMPTION_EXTERN, _ASSUMPTION_SWAP, _ASSUMPTION_SCOPE],
        target=target, resolved=interpretations, sites=sites,
        components=sorted({s["component"] for s in sites}),
        guarantee="G4 makes a service declaration an upper bound on its "
                  "providers: a method not declared `emission` provably "
                  "reaches none, so the traversal cannot have missed one "
                  "hiding behind a plain `fn`.",
    )


def withdrawal(ir: dict, component: str) -> dict:
    """what breaks if I withdraw C? — the reactive cascade, in the order the
    runtime tears it down."""
    index = Composition(ir)
    if component not in index.entries:
        return _unknown("withdrawal", "component", component, index.entries)

    gone = {component}
    cascade = []
    frontier = [(component, 0)]
    while frontier:
        removed, depth = frontier.pop(0)
        for name, entry in index.entries.items():
            if name in gone:
                continue
            lost = [key for key in entry.get("inject") or []
                    if index.provider(name, key) == removed]
            if not lost:
                continue
            gone.add(name)
            cascade.append({
                "component": name, "depth": depth + 1,
                "lostKeys": sorted(lost), "provider": removed,
                "reason": f"injects {', '.join('`' + k + '`' for k in sorted(lost))}, "
                          f"provided by {removed}",
                "alsoOrphans": sorted(entry.get("provides") or []),
            })
            frontier.append((name, depth + 1))

    order = [name for name in reversed(index.load_order) if name in gone]
    orphaned = []
    for name in sorted(gone):
        for key in (index.entries[name].get("provides") or []):
            orphaned.append({"key": key, "realm": index.realm(name, key),
                             "wasProvidedBy": name})

    return _result(
        "withdrawal", f"what breaks if I withdraw {component}?", EXACT,
        "the provider graph is the one the linker resolved: G2 makes each "
        "`(key, realm)` provision unique, so a lost provision has no "
        "alternative supplier, and G3 makes the graph acyclic, so the "
        "cascade terminates. This is the exact set that loses a provision.",
        [_ASSUMPTION_SCOPE],
        component=component,
        provides=[{"key": key, "realm": index.realm(component, key)}
                  for key in sorted(index.entries[component].get("provides") or [])],
        cascade=cascade,
        withdrawalOrder=order,
        orphanedKeys=sorted(orphaned, key=lambda o: (o["key"], o["realm"])),
        survivors=sorted(set(index.entries) - gone),
        breaks=len(cascade),
    )


def dependents(ir: dict, target: str) -> dict:
    """who depends on service S / provision key k?"""
    index = Composition(ir)
    interpretations = _interpret(index, target, keys_only=True)
    if not interpretations:
        return _unknown("dependents", "target", target, _targets(index))

    wanted = set()
    for item in interpretations:
        if item["kind"] == "service":
            wanted |= {(k, r) for (k, r) in index.provider_of
                       if index.service_of(index.provider_of[(k, r)], k) == item["service"]}
            for name, entry in index.entries.items():
                for key in entry.get("inject") or []:
                    if index.service_of(name, key) == item["service"]:
                        wanted.add((key, index.realm(name, key)))
        else:
            for name, entry in index.entries.items():
                for key in (entry.get("inject") or []) + (entry.get("provides") or []):
                    if key == item["key"]:
                        wanted.add((key, index.realm(name, key)))

    keys = []
    for key, realm in sorted(wanted):
        provider = index.provider_of.get((key, realm))
        consumers = []
        for name, entry in index.entries.items():
            if key not in (entry.get("inject") or []) or index.realm(name, key) != realm:
                continue
            used = sorted({(call["key"], call["method"], call["emission"])
                           for sid in index.scopes_of.get(name, [])
                           for call in index.scopes[sid]["facts"]["calls"]
                           if call["key"] == key})
            consumers.append({
                "component": name,
                "service": index.service_of(name, key),
                "methodsCalled": [{"method": m, "emission": e} for _, m, e in used],
                "sites": sorted({index.scopes[sid]["label"]
                                 for sid in index.scopes_of.get(name, [])
                                 for call in index.scopes[sid]["facts"]["calls"]
                                 if call["key"] == key}),
                "intercept": (index.entries[name].get("intercept") or {}).get(key),
            })
        service = index.service_of(provider, key) if provider else None
        if service is None:  # unresolved key: the consumers still name its type
            service = next((c["service"] for c in consumers if c["service"]), None)
        keys.append({
            "key": key, "realm": realm, "provider": provider,
            "service": service,
            "resolved": provider is not None,
            "consumers": sorted(consumers, key=lambda c: c["component"]),
        })

    return _result(
        "dependents", f"who depends on `{target}`?", EXACT,
        "`requires`/`provides` are declared in the component header (G1) and "
        "resolved by the linker, so the consumer set is a lookup, not an "
        "inference. `methodsCalled` is the may-set of operations actually "
        "referenced in the bodies.",
        [_ASSUMPTION_SCOPE],
        target=target, resolved=interpretations, keys=keys,
        components=sorted({c["component"] for k in keys for c in k["consumers"]}),
    )


def reach(ir: dict, component: str) -> dict:
    """what does C reach? — the transitive boundary surface of one
    component, following calls across the service seam into the provider
    bodies they land in."""
    index = Composition(ir)
    if component not in index.entries:
        return _unknown("reach", "component", component, index.entries)

    surface = {"emissions": [], "externs": [], "iterationBoundaries": 0,
               "compensated": 0}
    reached_components = set()
    for scope_id in index.scopes_of.get(component, []):
        scope = index.scopes[scope_id]
        facts = scope["facts"]
        surface["iterationBoundaries"] += facts["awaits"]
        surface["compensated"] += facts["compensated"]
        for fact in facts["emissions"] + facts["externs"]:
            surface["emissions" if _is_emission(fact) else "externs"].append(
                {**fact, "from": _site(scope), "direct": True, "path": [],
                 "distance": 0})
        for step in index.closure(scope_id):
            other = index.scopes[step["scope"]]
            reached_components.add(other["component"])
            surface["iterationBoundaries"] += other["facts"]["awaits"]
            for fact in other["facts"]["emissions"] + other["facts"]["externs"]:
                surface["emissions" if _is_emission(fact) else "externs"].append(
                    {**fact, "from": _site(scope), "in": _site(other),
                     "direct": False, "path": _hops(step["path"]),
                     "distance": step["depth"]})

    for bucket in ("emissions", "externs"):
        surface[bucket] = _dedupe(surface[bucket])
    reached_components.discard(component)

    unresolved = index.unresolved_injections(component)
    assumptions = [_ASSUMPTION_MAY, _ASSUMPTION_EXTERN, _ASSUMPTION_SWAP,
                   _ASSUMPTION_SCOPE]
    if unresolved:
        # the honest hole: a key nothing in this IR provides is a dynamic
        # boundary — whatever answers it at runtime has a surface we cannot see
        plural = "that key" if len(unresolved) == 1 else "those keys"
        assumptions.append(
            "the reach through " + ", ".join(f"`{k}`" for k in unresolved) +
            f" is NOT included: no component in this IR provides {plural}, so "
            "the callee's own surface is unknown. This result is INCOMPLETE "
            f"for {plural} — it is the one case where the query under-reports.")

    return _result(
        "reach", f"what does {component} reach?", APPROX,
        "a may-analysis over the activation body and every provide-method: "
        "everything listed is reachable, and it is a superset of what any "
        "single activation actually does. Provider *activation* bodies are "
        "not folded in — they are that provider's own surface, reached by "
        "loading it, not by calling it.",
        assumptions,
        component=component,
        surface=surface,
        reachedComponents=sorted(reached_components),
        providers=sorted({p for key in (index.entries[component].get("inject") or [])
                          for p in [index.provider(component, key)] if p}),
        unresolvedInjections=sorted(unresolved),
        complete=not unresolved,
    )


def drift(ir: dict, service: str, gains=(), losses=()) -> dict:
    """what changes if service S gains/loses a method? — which providers
    must change and which call sites are implicated (interface drift)."""
    index = Composition(ir)
    if service not in index.services:
        return _unknown("drift", "service", service, index.services)

    methods = sorted((index.services[service].get("methods") or {}))
    providers = []
    for (key, realm), name in sorted(index.provider_of.items()):
        if index.service_of(name, key) != service:
            continue
        implemented = sorted({index.scopes[sid]["method"]
                              for sid in index.scopes_of.get(name, [])
                              if index.scopes[sid]["key"] == key
                              and index.scopes[sid]["method"]})
        providers.append({"component": name, "key": key, "realm": realm,
                          "implements": implemented})

    call_sites = []
    for scope_id, scope in index.scopes.items():
        for call in scope["facts"]["calls"]:
            if index.service_of(scope["component"], call["key"]) != service:
                continue
            call_sites.append({**_site(scope), "key": call["key"],
                               "method": call["method"],
                               "emission": call["emission"]})

    def _sites(method: str) -> list:
        return [s for s in call_sites if s["method"] == method]

    gained = [{
        "method": method,
        "known": method in methods,
        "providersMustImplement": [p["component"] for p in providers],
        "callSites": _sites(method),
        "note": "every provider of this service must gain an implementation; "
                "the compiler refuses a `provide` block that does not cover "
                "the declaration. Existing call sites are unaffected."
                if method not in methods else
                "`" + method + "` is already declared on this service — a "
                "gain is a no-op unless the signature changes.",
    } for method in gains]

    lost = [{
        "method": method,
        "known": method in methods,
        "emission": bool(((index.services[service].get("methods") or {})
                          .get(method) or {}).get("emission")),
        "providersMustDrop": [p["component"] for p in providers
                              if method in p["implements"]],
        "callSites": _sites(method),
        "note": "each call site above stops resolving and each provider "
                "implementation becomes an operation the service no longer "
                "declares — both are rejected at the admission gate."
                if method in methods else
                "`" + method + "` is not declared on this service; nothing "
                "to lose.",
    } for method in losses]

    return _result(
        "drift", f"what changes if `{service}` gains/loses a method?", EXACT,
        "providers and call sites are declarations and syntactic call nodes, "
        "both enumerable from the IR — this is a complete list of what the "
        "admission gate would flag for the given change, not an estimate. It "
        "is a list of implicated sites, not a prediction that the edit is "
        "safe once they are fixed.",
        [_ASSUMPTION_SCOPE, _ASSUMPTION_SWAP],
        service=service, methods=[
            {"name": name,
             "emission": bool((index.services[service]["methods"][name] or {})
                              .get("emission")),
             "providers": [p["component"] for p in providers
                           if name in p["implements"]],
             "callSites": _sites(name)}
            for name in methods
        ],
        providers=providers, callSites=call_sites,
        gains=gained, losses=lost,
        impacted=sorted({p["component"] for p in providers}
                        | {s["component"] for s in call_sites}),
    )


# ---------------------------------------------------------------- helpers


def _is_emission(fact: dict) -> bool:
    return fact["kind"] == "service" or fact.get("emission") is True


def _emission_facts(facts: dict) -> list:
    return facts["emissions"] + [e for e in facts["externs"] if e["emission"]]


def _dedupe(items: list) -> list:
    seen, out = set(), []
    for item in items:
        mark = (item.get("kind"), item.get("key"), item.get("method"),
                item.get("name"), item.get("direct"), tuple(item.get("path") or ()))
        if mark in seen:
            continue
        seen.add(mark)
        out.append(item)
    return sorted(out, key=lambda i: (i.get("distance", 0), i.get("key") or "",
                                      i.get("name") or "", i.get("method") or ""))


def _targets(index: Composition) -> list:
    keys = {key for (key, _) in index.provider_of}
    for entry in index.entries.values():
        keys |= set(entry.get("inject") or []) | set(entry.get("provides") or [])
    return sorted(keys | set(index.services) | set(index.externs))


def _interpret(index: Composition, target: str, keys_only: bool = False) -> list:
    """A target names a provision key, a `key.method`, a service or an extern.
    Names live in different spaces and could collide, so every reading that
    matches something is returned and the result says which were used."""
    found = []
    keys = {key for (key, _) in index.provider_of}
    for entry in index.entries.values():
        keys |= set(entry.get("inject") or []) | set(entry.get("provides") or [])

    if target in keys:
        found.append({"kind": "key", "key": target,
                      "realms": sorted({r for (k, r) in index.provider_of if k == target})})
    if "." in target:
        key, _, method = target.partition(".")
        if key in keys:
            found.append({"kind": "key.method", "key": key, "method": method})
    if target in index.services:
        found.append({"kind": "service", "service": target,
                      "methods": sorted(index.services[target].get("methods") or {})})
    if not keys_only and target in index.externs:
        found.append({"kind": "extern", "name": target,
                      "class": index.externs[target].get("class")})
    return found


def _matcher(index: Composition, interpretations: list):
    def matches(component: str, fact: dict) -> bool:
        for item in interpretations:
            if item["kind"] == "key" and fact["kind"] == "service" \
                    and fact["key"] == item["key"]:
                return True
            if item["kind"] == "key.method" and fact["kind"] == "service" \
                    and fact["key"] == item["key"] and fact["method"] == item["method"]:
                return True
            if item["kind"] == "service" and fact["kind"] == "service" \
                    and index.service_of(component, fact["key"]) == item["service"]:
                return True
            if item["kind"] == "extern" and fact["kind"] == "extern" \
                    and fact["name"] == item["name"]:
                return True
        return False

    return matches


QUERIES = {
    "emits-to": emitters,
    "withdraw": withdrawal,
    "depends-on": dependents,
    "reaches": reach,
    "drift": drift,
}


# ---------------------------------------------------------------- rendering


def render(result: dict) -> str:
    """Human rendering. The structured result is the product; this is the
    courtesy view — machine first, human second (docs/queries.md §1)."""
    if not result.get("ok"):
        lines = [f"error: {result.get('error')}"]
        if result.get("known"):
            lines.append("  known: " + ", ".join(result["known"]))
        return "\n".join(lines)

    out = [result["question"],
           f"  precision: {result['precision']} — {result['precisionNote']}"]
    query = result["query"]

    if query == "emitters":
        if not result["sites"]:
            out.append("\nnothing emits to it.")
        for site in result["sites"]:
            reaches = site["reaches"]
            what = (f"{reaches['key']}.{reaches['method']}"
                    if reaches["kind"] == "service" else f"{reaches['name']}()")
            how = "directly" if site["direct"] else "via " + " -> ".join(site["path"])
            if reaches.get("through"):
                how += f" (through fn {', '.join(reaches['through'])})"
            comp = "" if reaches.get("compensated") is not True else "  [compensated]"
            out.append(f"\n  {site['label']}")
            out.append(f"    emits {what} {how}{comp}")
    elif query == "withdrawal":
        keys = ", ".join(f"`{p['key']}`" + (f"@{p['realm']}" if p["realm"] else "")
                         for p in result["provides"]) or "—"
        out.append(f"\n  {result['component']} provides {keys}")
        if not result["cascade"]:
            out.append("  nothing depends on it — withdrawal is local.")
        for item in result["cascade"]:
            out.append(f"  {'  ' * item['depth']}-> {item['component']} "
                       f"(depth {item['depth']}): {item['reason']}")
        out.append("\n  withdrawal order (LIFO): "
                   + " -> ".join(result["withdrawalOrder"]))
        if result["orphanedKeys"]:
            out.append("  keys that stop being provided: " + ", ".join(
                o["key"] + (f"@{o['realm']}" if o["realm"] else "")
                + f" (was {o['wasProvidedBy']})" for o in result["orphanedKeys"]))
    elif query == "dependents":
        for key in result["keys"]:
            realm = f" in realm `{key['realm']}`" if key["realm"] else ""
            provider = key["provider"] or "— (unresolved: nothing here provides it)"
            out.append(f"\n  key `{key['key']}`{realm}: {key['service'] or '?'}"
                       f"  provided by {provider}")
            if not key["consumers"]:
                out.append("    consumers: none")
            for consumer in key["consumers"]:
                used = ", ".join(
                    m["method"] + ("!" if m["emission"] else "")
                    for m in consumer["methodsCalled"]) or "—"
                out.append(f"    {consumer['component']}  calls: {used}")
    elif query == "reach":
        surface = result["surface"]
        out.append(f"\n  reaches components: "
                   f"{', '.join(result['reachedComponents']) or '—'}")
        out.append("  emissions:")
        for fact in surface["emissions"] or []:
            what = (f"{fact['key']}.{fact['method']}" if fact["kind"] == "service"
                    else f"{fact['name']}()")
            where = "direct" if fact["direct"] else " -> ".join(fact["path"])
            out.append(f"    {what}  [{where}]")
        if not surface["emissions"]:
            out.append("    none — fully revertible (G8)")
        if surface["externs"]:
            out.append("  host code (non-emission):")
            for fact in surface["externs"]:
                out.append(f"    {fact['name']} ({fact['class']})")
        out.append(f"  iteration boundaries: {surface['iterationBoundaries']}; "
                   f"compensated emissions: {surface['compensated']}")
        if result["unresolvedInjections"]:
            out.append("  INCOMPLETE — unresolved injections: "
                       + ", ".join(result["unresolvedInjections"]))
    elif query == "drift":
        out.append(f"\n  service {result['service']}")
        for method in result["methods"]:
            mark = "emission " if method["emission"] else ""
            out.append(f"    {mark}fn {method['name']}  providers: "
                       f"{', '.join(method['providers']) or '—'}  call sites: "
                       f"{len(method['callSites'])}")
        for gain in result["gains"]:
            out.append(f"\n  + {gain['method']}: must be implemented by "
                       f"{', '.join(gain['providersMustImplement']) or '—'}")
            out.append(f"    {gain['note']}")
        for loss in result["losses"]:
            out.append(f"\n  - {loss['method']}: providers to change: "
                       f"{', '.join(loss['providersMustDrop']) or '—'}")
            for site in loss["callSites"]:
                out.append(f"    breaks call site {site['label']} "
                           f"({site['key']}.{site['method']})")
            out.append(f"    {loss['note']}")

    if result.get("assumptions"):
        out.append("\n  this answer assumes:")
        out.extend(f"    - {line}" for line in result["assumptions"])
    return "\n".join(out)
