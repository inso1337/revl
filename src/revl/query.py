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

The same seven-verb surface answers in three time modes (docs/queries.md §9):
against the STATIC IR (the default), against the LIVE session as it stands
after every swap (`live_query`/`as_live` — the verbs run against the session's
post-swap IR, with the envelope's spent hot-swap caveat replaced and the
served-now reality folded in), and against a RECORDED run (`emitted_between`,
`lifetime` — the query-side of item 27's causal traces, reusing the
`why_runtime` lifecycle trace and the `replay` effect timeline verbatim). The
envelope's `precision`/`assumptions` already expressed how the worlds differ,
so a result carries a `mode` and says which world it describes.
"""

from __future__ import annotations

from .lower import SHARED_REALM

# precision vocabulary — every result says which of the two it is
EXACT = "exact"
APPROX = "over-approximation"

# mode vocabulary — every result says WHICH WORLD it describes (docs/queries.md
# §9). One verb surface, three worlds: the static IR, the live session as it
# stands after every swap, and a recorded run's effect timeline. `precision`
# and `assumptions` already carried "hot swap changes the answer" and "only
# components in this IR" — the two facts that a mode changes — so a result can
# say which world it answers for without a second field doing all the work.
MODE_STATIC = "static"          # against the compiled IR (the original three verbs)
MODE_LIVE = "live"              # against the live session's real loaded state, post-swap
MODE_HISTORICAL = "historical"  # against a recorded replay timeline / item-27 trace

# the recorded-world step vocabulary, mirroring backends/python/replay.py's
# KINDS. Named here as *data* the timeline dict already carries, not imported —
# query.py must stay importable without the backend on the path.
_STEP_EMISSION = "emission"
_STEP_PROVISION = "provision"
_STEP_EFFECT = "effect"
_STEP_COMPENSATION = "compensation"
_STEP_BOUNDARY = "boundary"

# item 247 gap 2 (docs/design/247-compensate.md, Decision 2): the THREE audit
# states a boundary crossing can be in, on the same G8 audit surface that today
# tags a crossing compensated-vs-bare. `unresolved` is the third — a
# compensation that was attached AND attempted AND did not land (the runtime's
# `compensation-residue`). "Never silently swallowed" means this state is
# enumerable here, not merely a growing in-memory list.
STATE_BARE = "bare"                 # nothing attached — an honest irreversible crossing
STATE_COMPENSATED = "compensated"   # an offset was attached and ran clean
STATE_UNRESOLVED = "unresolved"     # an offset was attached, attempted, and failed
COMPENSATION_STATES = (STATE_BARE, STATE_COMPENSATED, STATE_UNRESOLVED)

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
# live mode: the swap assumption is not merely dropped — it is *inverted*. A
# static answer warns that a swap would change it; a live answer already stands
# on the far side of every swap applied so far.
_ASSUMPTION_LIVE = (
    "this answers against the live session's actual loaded generation, not a "
    "static IR: it is the composition as it stands after every hot swap applied "
    "so far. The static `swap changes the answer` caveat is therefore spent — "
    "this IS the post-swap world, and re-querying after the next swap is how "
    "you move to the one after it."
)
_ASSUMPTION_LIVE_SERVED = (
    "a provision counts as present only when it is *served right now*: a "
    "declared key whose provider has drifted to an inactive state reads as "
    "absent here (`live.notServedNow`), where the static query — which only "
    "sees the graph, not the running fibers — would still count it."
)
# historical mode: a recorded run is neither a proof about all activations nor
# a prediction. It is a log of one activation that happened.
_ASSUMPTION_RECORDED = (
    "this describes a recorded run, not the static composition or the live "
    "session: every entry is a step the runtime actually took and wrote to the "
    "trace. It is what happened on that one activation — not what may happen "
    "(the static may-analysis) nor what will (the static exact prediction)."
)
_ASSUMPTION_RECORDING_SCOPE = (
    "only what the recording captured is visible: a component loaded without "
    "`record: true`, an activation that never ran, and an effect the recorder "
    "classifies as opaque are all absent from this timeline."
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
                 # item 245: the deferred flag rides the fact so the G8 crossing
                 # surface can tag class (b) without re-deriving it.
                 "deferred": bool((self.externs.get(name) or {}).get("deferred")),
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
            assumptions: list, mode: str = None, **payload) -> dict:
    return {"ok": True, "query": query, "question": question,
            "mode": mode or MODE_STATIC,
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


# ---------------------------------------------------------------- live mode
#
# The same verbs, answered against the live session's real loaded state instead
# of a compiled-from-source IR. The trick is that there is no new machinery:
# the linked graph the static queries read is *also* what the running session
# holds (`session.ir` is the post-swap generation), so `withdrawal(session.ir,
# C)` already answers for the live world. What live mode adds is the envelope
# relabelling — the static `swap changes the answer` caveat is spent (this is
# the post-swap world) — and the one thing only the runtime knows: which
# provisions are *actually served now*, versus merely declared in the graph.


def live_query(ir: dict, verb: str, live_state: dict = None,
               *args, **kwargs) -> dict:
    """Run query `verb` against the live composition `ir` (the session's
    post-swap generation), then relabel the envelope as a LIVE answer.

    `live_state` is what the runtime knows that the graph does not:
    ``{"generation", "servedKeys", "componentStates"}``. It is threaded in by
    the session-bound MCP tool; a caller with only an IR may omit it and get a
    live-labelled answer with no served-key reality folded in."""
    fn = _VERB_FN.get(verb)
    if fn is None:
        return {"ok": False, "query": "live", "error": f"unknown verb: {verb!r}",
                "known": sorted(_VERB_FN)}
    return as_live(fn(ir, *args, **kwargs), live_state)


def as_live(result: dict, live_state: dict = None) -> dict:
    """Relabel a statically computed envelope as a LIVE answer.

    Same verb, same shape — but `mode` becomes ``live``, the spent hot-swap
    assumption is replaced by the live-generation one, and where the query
    speaks of provisions the answer is reconciled against what is *served right
    now*. Everything a static consumer reads still reads; a live consumer gets
    a `live` block on top."""
    if not result.get("ok"):
        return result
    state = live_state or {}
    served = set(state.get("servedKeys") or [])
    out = dict(result)
    out["mode"] = MODE_LIVE

    assumptions = [a for a in (out.get("assumptions") or []) if a != _ASSUMPTION_SWAP]
    assumptions = [_ASSUMPTION_LIVE] + assumptions
    touches_provisions = out.get("query") in (
        "withdrawal", "dependents", "reach", "emitters")
    if touches_provisions and _ASSUMPTION_LIVE_SERVED not in assumptions:
        assumptions.append(_ASSUMPTION_LIVE_SERVED)
    out["assumptions"] = assumptions

    out["live"] = {
        "generation": state.get("generation"),
        "servedKeys": sorted(served),
        "componentStates": dict(state.get("componentStates") or {}),
        "note": "answered against the live composition as it stands now; a "
                "provision is present only if it is actually served.",
    }
    _annotate_live(out, served, state.get("componentStates") or {})
    return out


def _annotate_live(out: dict, served: set, states: dict) -> None:
    """Fold served-now reality into the parts of an answer that name provisions.
    Purely additive: every static field is untouched, new ones say what the
    running fibers add over the graph."""
    query = out.get("query")
    if query == "withdrawal":
        for prov in out.get("provides") or []:
            prov["servedNow"] = prov["key"] in served
        drifted = sorted(p["key"] for p in (out.get("provides") or [])
                         if p["key"] not in served)
        out["live"]["notServedNow"] = drifted
        for item in out.get("cascade") or []:
            item["providerActiveNow"] = states.get(item.get("provider")) == "ACTIVE" \
                if states else None
    elif query == "dependents":
        for key in out.get("keys") or []:
            key["servedNow"] = key["key"] in served
    elif query == "reach":
        injected = out.get("providers") or []
        out["live"]["providersActiveNow"] = {
            name: states.get(name) for name in injected} if states else {}


# ------------------------------------------------------------ historical mode
#
# Queries against a *recorded* run rather than a static IR or the live session.
# This is the query-side of item 27's causal traces (docs/why-runtime.md): the
# lifecycle trace (`why_runtime`, JSONL of load/withdraw + cause) says when a
# component lived, and the recorded effect timeline
# (backends/python/replay.py) says what it did while it lived. Both formats are
# reused verbatim — nothing here invents a parallel recording. The novel part
# is that nobody queries a *verified effect timeline*: "which emissions crossed
# between steps 3 and 7?" is a question you can only ask of a checked run.


def _timelines_of(timeline) -> list:
    """Normalise a recorded replay dict into a list of per-component timeline
    dicts, whatever shape it came in as: a `Recorder.as_dict()`
    (``{"components": [...]}``), a single `Timeline.as_dict()`
    (``{"component", "steps"}``), or a bare list of the latter."""
    if timeline is None:
        return []
    if isinstance(timeline, list):
        return [t for t in timeline if isinstance(t, dict) and "steps" in t]
    if "components" in timeline:
        return [t for t in timeline["components"] if isinstance(t, dict)]
    if "steps" in timeline:
        return [timeline]
    return []


def emitted_between(timeline, frm: int, to: int, component: str = None) -> dict:
    """which emissions crossed between steps X and Y? — over a recorded replay
    timeline. An emission is a one-way boundary crossing (replay.py), so each
    hit is a real crossing the runtime performed in the window, not a reachable
    site. The novel historical query: a windowed read of a verified timeline."""
    timelines = _timelines_of(timeline)
    if not timelines:
        return {"ok": False, "query": "emitted-between",
                "error": "no recorded timeline given — pass a replay recording "
                         "(revl_timeline / `session.timeline()`) to query"}
    if not isinstance(frm, int) or not isinstance(to, int) or frm > to:
        return {"ok": False, "query": "emitted-between",
                "error": f"invalid window [{frm}, {to}]: need integers with from <= to"}

    known = sorted(t.get("component") for t in timelines if t.get("component"))
    if component is not None and component not in known:
        return _unknown("emitted-between", "component", component, known)

    chosen = [t for t in timelines
              if component is None or t.get("component") == component]
    crossings = []
    windowSteps = 0
    for tl in chosen:
        name = tl.get("component")
        for step in tl.get("steps") or []:
            index = step.get("index")
            if index is None or index < frm or index > to:
                continue
            windowSteps += 1
            if step.get("kind") != _STEP_EMISSION:
                continue
            detail = step.get("detail") or {}
            crossings.append({
                "component": name, "step": index, "label": step.get("label"),
                "key": detail.get("key"), "method": detail.get("method"),
                "service": detail.get("service"), "args": detail.get("args"),
                "site": step.get("site"), "source": step.get("source"),
                "compensatedBy": step.get("compensatedBy"),
                "compensated": step.get("compensatedBy") is not None,
            })
    crossings.sort(key=lambda c: (c["step"], c["component"] or "",
                                  c["label"] or ""))
    return _result(
        "emitted-between",
        f"which emissions crossed between steps {frm} and {to}?", EXACT,
        "these are the emission steps the recording actually holds with "
        f"{frm} <= index <= {to}. An emission has no inverse — it is a real "
        "boundary crossing the runtime performed, so this is a fact about the "
        "run, complete and minimal over the window.",
        [_ASSUMPTION_RECORDED, _ASSUMPTION_RECORDING_SCOPE],
        mode=MODE_HISTORICAL,
        window={"from": frm, "to": to},
        component=component,
        emissions=crossings,
        crossings=len(crossings),
        stepsInWindow=windowSteps,
        components=sorted({c["component"] for c in crossings if c["component"]}),
        uncompensated=[c for c in crossings if not c["compensated"]],
    )


def classify_compensation(crossings: list, residue: list = None) -> dict:
    """Partition boundary crossings into the three audit states (item 247 gap 2,
    docs/design/247-compensate.md Decision 2), overlaying the runtime's
    compensation residue on the static/recorded compensated-vs-bare tag.

    `crossings` — dicts each with a `component` and a `compensated` bool (an
    offset was ATTACHED at compile time / in the recording). `residue` — the
    `compensation-residue` records from an abort or `revl recover` (each names
    its `component`; a best-effort offset that raised or was skipped).

    A crossing is:

      * ``bare`` — no offset attached (an honest irreversible crossing);
      * ``compensated`` — an offset attached and no residue accounts for it;
      * ``unresolved`` — an offset attached but a residue record shows it did
        not land.

    Residue is correlated to a compensated crossing by ``component`` (the
    granularity the runtime residue and the crossing record share — the residue
    names the OFFSET call, the crossing names the EMISSION, so a per-crossing
    identity join is not available without threading the WAL seq through the
    recording; that is left as a follow-up). Within a component the residue
    records absorb its compensated crossings newest-first (Phase-2 LIFO). Any
    residue with no compensated crossing to attach to — the common case when the
    caller passes residue without a recording — is surfaced as a standalone
    ``unresolved`` fact, so an operator always SEES it and it is never dropped.

    Pure and byte-inert when ``residue`` is empty: every compensated crossing
    stays ``compensated`` and the ``unresolved`` list is empty."""
    residue = list(residue or [])
    by_component: dict = {}
    for record in residue:
        by_component.setdefault(record.get("component"), []).append(record)

    bare, compensated, unresolved = [], [], []
    for crossing in crossings:
        if not crossing.get("compensated"):
            bare.append({**crossing, "residueState": STATE_BARE})
            continue
        pending = by_component.get(crossing.get("component"))
        if pending:
            record = pending.pop(0)
            unresolved.append({**crossing, "residueState": STATE_UNRESOLVED,
                               "residue": record})
        else:
            compensated.append({**crossing, "residueState": STATE_COMPENSATED})

    # residue with no compensated crossing to attach to — still enumerated, so
    # nothing is silently swallowed (the residue-only surfacing path).
    unattached = [r for group in by_component.values() for r in group]
    for record in unattached:
        unresolved.append({"component": record.get("component"),
                           "compensated": True, "residueState": STATE_UNRESOLVED,
                           "residue": record, "crossing": None})
    return {
        "bare": bare,
        "compensated": compensated,
        "unresolved": unresolved,
        "states": {STATE_BARE: len(bare),
                   STATE_COMPENSATED: len(compensated),
                   STATE_UNRESOLVED: len(unresolved)},
    }


def compensation_audit(timeline=None, residue: list = None,
                       component: str = None) -> dict:
    """The three-state compensation audit (item 247 gap 2). Enumerates every
    recorded emission crossing as ``bare`` / ``compensated`` / ``unresolved``,
    overlaying the runtime `compensation-residue` an abort (or `revl recover`)
    produced. The one place an operator/harness asks "did every offset I owed
    actually land?" — and the `unresolved` list answers when one did not.

    `timeline` is an optional replay recording (as `emitted_between` reads);
    `residue` is the `compensationResidue` list off a session-boundary report.
    Either may be omitted: with only residue, this is the standalone
    unresolved-fact surface (still complete — nothing is swallowed); with only a
    timeline, it degrades to the pre-247 compensated-vs-bare split with an empty
    `unresolved`."""
    crossings = []
    for tl in _timelines_of(timeline):
        name = tl.get("component")
        if component is not None and name != component:
            continue
        for step in tl.get("steps") or []:
            if step.get("kind") != _STEP_EMISSION:
                continue
            detail = step.get("detail") or {}
            crossings.append({
                "component": name, "step": step.get("index"),
                "label": step.get("label"),
                "key": detail.get("key"), "method": detail.get("method"),
                "compensatedBy": step.get("compensatedBy"),
                "compensated": step.get("compensatedBy") is not None,
            })
    partition = classify_compensation(crossings, residue)
    return _result(
        "compensation-audit",
        "which boundary crossings are bare, compensated, or left unresolved?",
        EXACT,
        "a crossing is bare (no offset), compensated (an offset attached and "
        "ran clean), or unresolved (an offset attached but a best-effort "
        "residue shows it did not land). Compensation is not inversion (paper "
        "§6.1): even a compensated crossing already left the system, and an "
        "unresolved one is still out there — this enumerates it so it is never "
        "silently swallowed.",
        [_ASSUMPTION_RECORDED, _ASSUMPTION_RECORDING_SCOPE],
        mode=MODE_HISTORICAL,
        component=component,
        **partition,
        crossings=len(crossings),
        residueCount=partition["states"][STATE_UNRESOLVED],
    )


def lifetime(record: dict, component: str) -> dict:
    """everything this component touched during its life — the historical
    counterpart of `reaches`. Where `reaches` is a may-analysis over the static
    IR, this reads the recorded world: item 27's lifecycle trace for *when* the
    component lived (load -> withdraw, with the cause behind each) and the
    recorded effect timeline for *what* it did while alive.

    `record` is ``{"trace": <why_runtime events / Trace>, "timeline": <replay
    recording>}`` — either half may be absent, but not both."""
    record = record or {}
    trace_in = record.get("trace")
    timeline_in = record.get("timeline")
    if trace_in is None and timeline_in is None:
        return {"ok": False, "query": "lifetime",
                "error": "give a recorded run: `trace` (an item-27 lifecycle "
                         "JSONL / Trace) and/or `timeline` (a replay recording)"}

    from . import why_runtime as wr  # noqa: PLC0415 — read-only reuse of item 27

    life = None
    known_components = set()
    if trace_in is not None:
        trace = trace_in
        if not isinstance(trace, wr.Trace):
            events = (wr.parse_trace(trace) if isinstance(trace, str)
                      else list(trace))
            trace = wr.Trace(events)
        known_components |= set(trace.components())
        transitions = trace.transitions_of(component)
        loaded = next((e for e in transitions if e.get("event") == wr.LOAD), None)
        withdrew = next((e for e in reversed(transitions)
                         if e.get("event") == wr.WITHDRAW), None)
        chain = trace.cause_chain(component,
                                  prefer=wr.WITHDRAW if withdrew else wr.LOAD)
        life = {
            "loaded": _transition_view(loaded),
            "withdrew": _transition_view(withdrew),
            "stillLive": bool(loaded) and withdrew is None,
            "causeChain": [{"component": f.component, "event": f.event,
                            "transition": f.transition, "note": f.note}
                           for f in chain],
        }

    touched = {_STEP_EFFECT: [], _STEP_PROVISION: [], _STEP_EMISSION: [],
               _STEP_COMPENSATION: []}
    emissions = []
    recorded = False
    for tl in _timelines_of(timeline_in):
        known_components.add(tl.get("component"))
        if tl.get("component") != component:
            continue
        recorded = True
        for step in tl.get("steps") or []:
            kind = step.get("kind")
            entry = {"step": step.get("index"), "label": step.get("label"),
                     "site": step.get("site"), "source": step.get("source"),
                     "detail": step.get("detail")}
            if kind in touched:
                touched[kind].append(entry)
            if kind == _STEP_EMISSION:
                detail = step.get("detail") or {}
                emissions.append({
                    "step": step.get("index"),
                    "key": detail.get("key"), "method": detail.get("method"),
                    "service": detail.get("service"), "args": detail.get("args"),
                    "compensated": step.get("compensatedBy") is not None})

    if life is None and not recorded:
        return _unknown("lifetime", "component", component,
                        sorted(c for c in known_components if c))

    return _result(
        "lifetime", f"everything {component} touched during its life", EXACT,
        "the recorded counterpart of `reaches`: not a may-analysis over the "
        "graph but the effects and emissions this component actually produced "
        "on the recorded run, bounded by the lifecycle trace's load and "
        "withdraw. Complete and minimal for that run; silent about runs not "
        "recorded.",
        [_ASSUMPTION_RECORDED, _ASSUMPTION_RECORDING_SCOPE],
        mode=MODE_HISTORICAL,
        component=component,
        life=life,
        touched=touched,
        emissions=emissions,
        counts={kind: len(items) for kind, items in touched.items()},
        recorded=recorded,
    )


def _transition_view(event: dict) -> dict:
    if not event:
        return None
    from . import why_runtime as wr  # noqa: PLC0415
    return {"seq": event.get("seq"), "gen": event.get("gen"),
            "event": event.get("event"), "transition": event.get("transition"),
            "cause": event.get("cause"), "note": wr._cause_note(event.get("cause") or {})}


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

# the same verbs, addressed by the result `query` name — what `live_query`
# dispatches on. Live mode runs these against the session's post-swap IR;
# historical mode adds its own verbs (`emitted-between`, `lifetime`) that only
# a recorded run can answer.
_VERB_FN = {
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

    mode = result.get("mode", MODE_STATIC)
    head = result["question"]
    if mode != MODE_STATIC:
        head += f"   [{mode}]"
    out = [head,
           f"  precision: {result['precision']} — {result['precisionNote']}"]
    if mode == MODE_LIVE and result.get("live"):
        live = result["live"]
        gen = live.get("generation")
        out.append(f"  live: generation {gen if gen is not None else '?'}, "
                   f"served now: {', '.join(live.get('servedKeys') or []) or '—'}")
        if live.get("notServedNow"):
            out.append("  drifted (declared but not served now): "
                       + ", ".join(live["notServedNow"]))
    query = result["query"]

    if query == "emitted-between":
        window = result.get("window") or {}
        out.append(f"\n  window: steps {window.get('from')}..{window.get('to')} "
                   f"({result.get('stepsInWindow', 0)} step(s), "
                   f"{result.get('crossings', 0)} emission crossing(s))")
        if not result["emissions"]:
            out.append("  no emission crossed in this window.")
        for cross in result["emissions"]:
            comp = f"{cross['component']}: " if cross.get("component") else ""
            comp_mark = "  [compensated]" if cross.get("compensated") else ""
            out.append(f"  step {cross['step']}  {comp}"
                       f"{cross.get('key')}.{cross.get('method')}{comp_mark}")
        if result.get("assumptions"):
            out.append("\n  this answer assumes:")
            out.extend(f"    - {line}" for line in result["assumptions"])
        return "\n".join(out)
    if query == "lifetime":
        life = result.get("life")
        if life:
            for phase in ("loaded", "withdrew"):
                ev = life.get(phase)
                if ev:
                    out.append(f"\n  {phase} (seq {ev.get('seq')}): "
                               f"{ev.get('transition')} — {ev.get('note')}")
            if life.get("stillLive"):
                out.append("\n  still live — no withdrawal recorded")
        counts = result.get("counts") or {}
        out.append("\n  touched: "
                   + ", ".join(f"{n} {k}(s)" for k, n in counts.items()) or "nothing")
        for emit in result.get("emissions") or []:
            out.append(f"    emitted {emit.get('key')}.{emit.get('method')} "
                       f"(step {emit.get('step')})"
                       + ("  [compensated]" if emit.get("compensated") else ""))
        if result.get("assumptions"):
            out.append("\n  this answer assumes:")
            out.extend(f"    - {line}" for line in result["assumptions"])
        return "\n".join(out)

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
