"""The authority graph: which model role can reach which capability.

`revl query authority` (docs/queries.md) answers a question no single query
does today: an operator wants to know *who can reach this capability*, where
"who" includes the model roles a component routes its decisions through. The
audit already proves what each component reaches (the G8 boundary surface)
and the lowering already records every `route model` edge and every secret
binding, so this module joins them into one graph instead of inventing a
second analysis:

  * ``role:<name>``       a declared model role, with its residence
  * ``component:<name>``  a component of the composition
  * ``service:<name>``    a service whose operation a component can cross
  * ``capability:<tok>``  a capability token a crossing carries
  * ``secret:<name>``     a secret bound to a capability

Edges, all directed "can reach":

  * ``role -> component``      the component routes through the role
  * ``component -> service``   the audit says the component can cross it
  * ``component -> capability`` the component itself reaches a host extern
                               (its declared scope, or its own name when it
                               declares none)
  * ``service -> capability``  the service method declares the token
  * ``secret -> capability``   the secret is bound to it

A role reaches a capability when an admitted path role -> component ->
service -> capability (or role -> component -> capability, for a host extern)
exists. That is a *may* statement about the role's steering: the audit's
component reach is itself a may-analysis, so the graph is a superset of what
any one activation carries. The route, declaration and binding edges are read
off the admitted IR and are exact; the precision of an edge says which it is,
and an opacity (a role that declares no reach) is recorded rather than
guessed.

THE UNNAMEABLE BOUNDARY. A bare `emission` (a method whose declaration makes no
promise about where it goes) and a first-class dispatch (an emitting callable
handed on as a value) reach a boundary no token names. The audit and the
checker spell it `*`. The graph keeps it as the ``capability:*`` node, and a
role that reaches it is reported for EVERY target, marked `unnameable`, because
the checker proves the crossing exists and cannot say which capability it
carries. Dropping it would answer "no model role reaches this" for a
capability the component may well emit, which is the one wrong answer an
authority query must not give.

The graph is deterministic: nodes and edges are sorted by their identity, so
two runs produce byte-identical JSON.
"""

from __future__ import annotations

from .audit_diff import audit_report

# Edge precisions, the same two words every query uses (docs/queries.md §2).
EXACT = "exact"
APPROX = "over-approximation"


# The unnameable boundary, spelled the way the audit and the checker spell it
# (boundary._UNKNOWN_DISPATCH, `capabilities[label] == ["*"]`).
UNNAMEABLE = "*"

# lower._UNDECLARED_NS: the checker names an unscoped emission by the service
# it is declared on (`svc:<service>`) in a held set. It is the same unnameable
# boundary, so the graph reads it as `*`.
_UNDECLARED_NS = "svc:"


def node_id(kind: str, name: str) -> str:
    """The stable id of one node, used in witness paths and edge ids."""
    return f"{kind}:{name}"


def _role_node(node: dict) -> dict:
    return {"kind": "role", "name": node["name"],
            "residence": node.get("residence"),
            "reachDeclared": bool(node.get("reachDeclared")),
            "opaque": not bool(node.get("reachDeclared")),
            "reaches": sorted(node.get("reaches") or [])}


class _Graph:
    """Accumulates nodes and edges, then emits them in a stable order."""

    def __init__(self) -> None:
        self.nodes: dict[tuple[str, str], dict] = {}
        self.edges: dict[tuple[str, str, str], dict] = {}  # (from, kind, to)

    def node(self, kind: str, name: str, **attrs) -> str:
        key = (kind, name)
        if key not in self.nodes:
            self.nodes[key] = {"kind": kind, "name": name}
        if kind == "role":
            self.nodes[key].setdefault("reaches", set())
            self.nodes[key]["reaches"] |= set(attrs.get("reaches") or [])
            for field in ("residence",):
                value = attrs.get(field)
                if value is not None and self.nodes[key].get(field) is None:
                    self.nodes[key][field] = value
            if "reachDeclared" in attrs:
                self.nodes[key]["reachDeclared"] = (
                    self.nodes[key].get("reachDeclared")
                    or bool(attrs.get("reachDeclared")))
        return node_id(kind, name)

    def edge(self, source: str, target: str, kind: str, precision: str,
             **attrs) -> None:
        """Add one edge. A second edge with the same identity keeps the first
        one's scalar attributes and unions its list attributes, so a role that
        steers one capability through two components names both."""
        key = (source, kind, target)
        record = self.edges.setdefault(
            key, {"from": source, "to": target, "kind": kind,
                  "precision": precision})
        for name in sorted(attrs):
            value = attrs[name]
            if value is None:
                continue
            if isinstance(value, list):
                record[name] = sorted(set(record.get(name) or []) | set(value))
            else:
                record.setdefault(name, value)

    def document(self) -> dict:
        nodes = [self._render_node(self.nodes[key])
                 for key in sorted(self.nodes)]
        edges = [self.edges[key] for key in sorted(self.edges)]
        return {"nodes": nodes, "edges": edges}

    def _render_node(self, node: dict) -> dict:
        if node["kind"] == "role":
            return _role_node(node)
        return {"kind": node["kind"], "name": node["name"]}


def _service_for_key(key: str, services: dict, components: dict) -> list[str]:
    """The services a provision key can name, in a sorted, deterministic list.

    A crossing from the audit is spelled `key.method`, where `key` is a
    provision key, not necessarily a service name. The service is what the
    component's `requires`/`provides` table maps that key to; a key no
    component maps (an ambient seam) falls back to a service of the same name.
    A key mapped to more than one service is an over-approximation and every
    candidate is kept.
    """
    found: set[str] = set()
    for comp in components.values():
        for table in ("requires", "provides"):
            service = (comp.get(table) or {}).get(key)
            if service in services:
                found.add(service)
    if not found and key in services:
        found.add(key)
    return sorted(found)


def _declared_capabilities(services: dict) -> dict:
    """`service -> [(method, [capability token, ...])]`, deterministic.

    A method declaring `emission` with no scoped capability reaches the
    unnameable boundary, which the audit spells `*`, so the graph names the
    same token the audit and the checker do.
    """
    out: dict[str, list] = {}
    for name in sorted(services):
        methods = (services[name] or {}).get("methods") or {}
        rows = []
        for method in sorted(methods):
            spec = methods[method] or {}
            caps = [str(cap) for cap in (spec.get("capabilities") or [])]
            if spec.get("emission") and not caps:
                caps = [UNNAMEABLE]
            if caps:
                rows.append((method, sorted(set(caps))))
        if rows:
            out[name] = rows
    return out


def _host_reach(report: dict, ir: dict, comp: str) -> list[tuple[str, str]]:
    """`[(token, extern)]` for the host code `comp` reaches, off the audit.

    The token rule is the one `policy.component_reach` already applies: a
    reached extern contributes its declared capability scope when it has one
    and its own name when it does not, and a first-class dispatch contributes
    the unnameable `*`. A `pure` extern is classified as reaching nothing and
    is left out; that classification is trusted exactly as the other queries
    trust it (the opaque-body assumption).
    """
    from .policy import component_reach  # noqa: PLC0415 - lazy, policy is heavy

    classes = {ext["name"]: ext.get("class")
               for ext in ir.get("externs") or []}
    out = []
    for reach in component_reach(report, comp):
        if reach.kind != "host" or classes.get(reach.via) == "pure":
            continue
        out.append((reach.token, reach.via))
    return out


def build_graph(ir: dict) -> dict:
    """Build the authority graph from one admitted composition.

    Reads only the audit report and the IR tables the compiler already writes;
    it never re-derives a reach the checker produced. Returns
    ``{"nodes": [...], "edges": [...]}`` with both lists sorted.
    """
    report = audit_report(ir)
    manifest = report.get("manifest") or {}
    boundary = report.get("boundary") or {}
    services = ir.get("services") or {}
    secrets = report.get("secrets") or []
    components = {comp["name"]: comp
                  for comp in (ir.get("components") or [])}

    graph = _Graph()

    # The composition's own components. The manifest is the linked surface;
    # the IR list keeps a component the manifest elides visible.
    for entry in manifest.get("components") or []:
        name = entry.get("name")
        if name:
            graph.node("component", name)
    for name in components:
        graph.node("component", name)

    # Services and the capability tokens their methods declare.
    declared = _declared_capabilities(services)
    for service in sorted(services):
        graph.node("service", service)
        for method, caps in declared.get(service) or []:
            for cap in caps:
                graph.edge(graph.node("service", service),
                           graph.node("capability", cap),
                           "declares", EXACT, method=method)

    # Component -> service: the audit's own boundary surface, folded through
    # the service seam. `emissions` entries are `key.method`, so the service
    # is resolved through the provision key's declaration.
    for comp_name in sorted(boundary):
        entry = boundary[comp_name] or {}
        comp_id = graph.node("component", comp_name)
        caps_of = entry.get("capabilities") or {}
        for crossing in sorted(set(entry.get("emissions") or [])):
            key = crossing.split(".", 1)[0]
            resolved = _service_for_key(key, services, components)
            for service in resolved:
                graph.node("service", service)
                graph.edge(comp_id, graph.node("service", service),
                           "crosses", APPROX)
            if not resolved:
                # a key nothing in this IR provides: the audit still names
                # what the crossing carries, so the capability is reached
                # directly rather than dropped. Dropping it would answer "no
                # role reaches this" for a crossing the checker enumerated.
                for cap in caps_of.get(crossing) or [UNNAMEABLE]:
                    graph.edge(comp_id, graph.node("capability", cap),
                               "emits", APPROX, via=[crossing])
        # host code the component itself reaches: a scoped extern under its
        # declared capability, an unscoped one under its own name, a
        # first-class dispatch under the unnameable `*`.
        for token, extern in _host_reach(report, ir, comp_name):
            graph.edge(comp_id, graph.node("capability", token),
                       "host", APPROX, externs=[extern])

    # Role -> component: every `route model` edge item 519 already recorded,
    # plus the role's declared reach as node metadata (never as a shortcut:
    # the path must cross the component's own audit surface).
    for row in manifest.get("model_reach") or []:
        role = row.get("role")
        comp = row.get("component")
        if not role or not comp:
            continue
        graph.node("role", role, residence=row.get("residence"),
                   reachDeclared=bool(row.get("reach_declared")),
                   reaches=row.get("reaches") or [])
        for cap in row.get("reaches") or []:
            graph.node("capability", cap)
        graph.edge(graph.node("role", role),
                   graph.node("component", comp), "routes", EXACT,
                   action=row.get("action"), origin=row.get("origin"),
                   crossing=row.get("crossing"))
        # What the checker proved the role can steer the component to: the
        # capabilities the component HOLDS that the role's reach covers
        # (`holds` minus `attenuated`, item 519's own product). A role with no
        # declared reach is the unnameable `*`, which covers nothing, so it may
        # steer everything the component holds. This is the ceiling, not a call
        # site: a component can hold a capability it never emits at the moment,
        # and a role steering it is still a role that can reach it.
        held = row.get("holds") or []
        if row.get("reach_declared"):
            held = [cap for cap in held
                    if cap not in set(row.get("attenuated") or [])]
        for cap in held:
            token = UNNAMEABLE if cap.startswith(_UNDECLARED_NS) else cap
            graph.edge(graph.node("role", role),
                       graph.node("capability", token), "steers", EXACT,
                       components=[comp])

    # Secret -> capability: the manifest-visible binding (name and capability
    # only; the value never enters the IR).
    for row in secrets:
        name = row.get("name")
        cap = row.get("capability")
        if not name or not cap:
            continue
        graph.node("secret", name)
        graph.node("capability", cap)
        graph.edge(graph.node("secret", name),
                   graph.node("capability", cap), "binds", EXACT)

    return graph.document()


def capabilities(graph: dict) -> list[str]:
    """Every capability node's name, sorted."""
    return sorted({node["name"] for node in graph.get("nodes") or []
                   if node.get("kind") == "capability"})


def _adjacency(graph: dict, skip: tuple = ()) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for edge in graph.get("edges") or []:
        if edge["kind"] in skip:
            continue
        out.setdefault(edge["from"], []).append(edge["to"])
    for source in out:
        out[source] = sorted(set(out[source]))
    return out


def _shortest_path(adjacency: dict, start: str, goal: str):
    """The lexicographically smallest shortest path, or None."""
    if start == goal:
        return [start]
    seen = {start}
    queue = [[start]]
    while queue:
        path = queue.pop(0)
        for nxt in adjacency.get(path[-1], ()):
            if nxt in seen:
                continue
            if nxt == goal:
                return path + [nxt]
            seen.add(nxt)
            queue.append(path + [nxt])
    return None


def _edges_by_pair(graph: dict) -> dict:
    return {(edge["from"], edge["to"]): edge
            for edge in graph.get("edges") or []}


def _path_precision(graph: dict, path: list[str]) -> str:
    by_pair = _edges_by_pair(graph)
    precisions = {by_pair[(path[i], path[i + 1])]["precision"]
                  for i in range(len(path) - 1)
                  if (path[i], path[i + 1]) in by_pair}
    return APPROX if APPROX in precisions or not precisions else EXACT


def _hops(graph: dict, path: list[str]) -> list[dict]:
    """The edge records along `path`, so a witness says HOW each step holds
    (which extern, which route) and not only which nodes it visits."""
    by_pair = _edges_by_pair(graph)
    return [by_pair[(path[i], path[i + 1])] for i in range(len(path) - 1)
            if (path[i], path[i + 1]) in by_pair]


def _witness(graphs: list, start: str, goal: str):
    """The first path found, trying each adjacency in order."""
    for adjacency in graphs:
        path = _shortest_path(adjacency, start, goal)
        if path is not None:
            return path
    return None


def roles_reaching(graph: dict, capability: str) -> list[dict]:
    """Every model role with an admitted path to `capability`, with a witness.

    One path per role: the shortest, ties broken lexicographically so two runs
    and two compositions with the same shape report the same witness. A path
    through the audit's own surface (role -> component -> service or host
    extern -> capability) is preferred; a `steers` edge, the capability a
    component holds that the role's reach covers, answers only when no audited
    crossing does, so a role is never left out for a capability its component
    holds but does not currently emit.

    A role with no path to the named capability but a path to the unnameable
    `capability:*` is reported too, with `unnameable: true` and the path to
    `*`: the checker proves a boundary crossing exists on that path and cannot
    say which capability it carries, so "no role reaches it" would be a claim
    nothing proves. A named path wins when a role has both.
    """
    goal = node_id("capability", capability)
    wildcard = node_id("capability", UNNAMEABLE)
    audited = _adjacency(graph, skip=("steers",))
    full = _adjacency(graph)
    out = []
    for node in graph.get("nodes") or []:
        if node.get("kind") != "role":
            continue
        start = node_id("role", node["name"])
        path = _witness([audited, full], start, goal)
        unnameable = False
        if path is None and goal != wildcard:
            path = _witness([audited, full], start, wildcard)
            unnameable = path is not None
        if path is None:
            continue
        entry = {"role": node["name"],
                 "residence": node.get("residence"),
                 "opaque": bool(node.get("opaque")),
                 "path": path,
                 "hops": _hops(graph, path),
                 "precision": _path_precision(graph, path)}
        if unnameable:
            entry["unnameable"] = True
        out.append(entry)
    return out


def secrets_of(graph: dict, capability: str) -> list[str]:
    """The secrets bound to `capability`, sorted."""
    target = node_id("capability", capability)
    return sorted(edge["from"].split(":", 1)[1]
                  for edge in graph.get("edges") or []
                  if edge["from"].startswith("secret:")
                  and edge["to"] == target
                  and edge["kind"] == "binds")


def _edge_key(edge: dict) -> str:
    return f"{edge['from']} --{edge['kind']}--> {edge['to']}"


# precision, ordered by how little the graph proves: a move toward the larger
# number is a loosening, whatever else changed
_PRECISION_RANK = {EXACT: 0, APPROX: 1}


def _changed_edges(before: dict, after: dict) -> list[dict]:
    """Edges present on both sides whose precision moved."""
    out = []
    for key in sorted(before.keys() & after.keys()):
        old, new = before[key]["precision"], after[key]["precision"]
        if old != new:
            out.append({"what": "edge", "id": key, "field": "precision",
                        "before": old, "after": new,
                        "widened": (_PRECISION_RANK.get(new, 1)
                                    > _PRECISION_RANK.get(old, 1))})
    return out


def _changed_roles(previous: dict, current: dict) -> list[dict]:
    """Role attributes that moved on a role present on both sides.

    A role that loses its `reaches [...]` becomes opaque and a role whose
    declared reach grows can steer more: both widen. A role that moves off the
    device widens where its inputs go. The other direction narrows and is
    reported without failing the gate."""
    def roles(graph):
        return {node["name"]: node for node in graph.get("nodes") or []
                if node.get("kind") == "role"}

    before, after = roles(previous), roles(current)
    out = []
    for name in sorted(before.keys() & after.keys()):
        old, new = before[name], after[name]
        ident = node_id("role", name)
        if "opaque" in old and old.get("opaque") != new.get("opaque"):
            out.append({"what": "role", "id": ident, "field": "opaque",
                        "before": old.get("opaque"), "after": new.get("opaque"),
                        "widened": bool(new.get("opaque"))})
        if "reaches" in old and old.get("reaches") != new.get("reaches"):
            gained = sorted(set(new.get("reaches") or [])
                            - set(old.get("reaches") or []))
            out.append({"what": "role", "id": ident, "field": "reaches",
                        "before": old.get("reaches"),
                        "after": new.get("reaches"), "widened": bool(gained)})
        if "residence" in old and old.get("residence") != new.get("residence"):
            out.append({"what": "role", "id": ident, "field": "residence",
                        "before": old.get("residence"),
                        "after": new.get("residence"),
                        "widened": new.get("residence") == "off_device"})
    return out


def diff(previous: dict, current: dict) -> dict:
    """What an update adds, removes and changes in the authority graph.

    Edge identity is `(from, kind, to)`. An edge on both sides is `unchanged`
    unless its precision moved, and a role on both sides is unchanged unless
    its opacity, declared reach or residence moved: those are reported under
    `changed`, because an update can widen authority without adding an edge
    (a role that stops declaring its reach, a proof that weakens from exact to
    a may-analysis, a model that leaves the device). `widened` is true when the
    update adds an edge or any change is a loosening, which is the direction an
    operator reviews.
    """
    before = {_edge_key(edge): edge for edge in previous.get("edges") or []}
    after = {_edge_key(edge): edge for edge in current.get("edges") or []}
    added = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys())
    changed = _changed_edges(before, after) + _changed_roles(previous, current)
    moved = {row["id"] for row in changed if row["what"] == "edge"}
    return {"added": added, "removed": removed,
            "changed": changed,
            "unchanged": sorted((after.keys() & before.keys()) - moved),
            "widened": bool(added) or any(row["widened"] for row in changed)}
