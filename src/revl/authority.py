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
  * ``service -> capability``  the service method declares the token
  * ``secret -> capability``   the secret is bound to it

A role reaches a capability when an admitted path role -> component ->
service -> capability exists. That is a *may* statement about the role's
steering: the audit's component reach is itself a may-analysis, so the graph
is a superset of what any one activation carries. The route, declaration and
binding edges are read off the admitted IR and are exact; the precision of an
edge says which it is, and an opacity (a role that declares no reach) is
recorded rather than guessed.

The graph is deterministic: nodes and edges are sorted by their identity, so
two runs produce byte-identical JSON.
"""

from __future__ import annotations

from .audit_diff import audit_report

# Edge precisions, the same two words every query uses (docs/queries.md §2).
EXACT = "exact"
APPROX = "over-approximation"


# lower._UNDECLARED_NS: an emission with no scoped capability is named by the
# service it is declared on, in its own namespace.
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
        self.edges: dict[tuple[str, str, str], dict] = {}

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
        key = (source, target, kind)
        if key in self.edges:
            return
        record = {"from": source, "to": target, "kind": kind,
                  "precision": precision}
        for name in sorted(attrs):
            if attrs[name] is not None:
                record[name] = attrs[name]
        self.edges[key] = record

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

    A method declaring `emission` with no scoped capability carries the
    lowerer's own undeclared spelling (`svc:<service>`), so the graph names the
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
                caps = [_UNDECLARED_NS + name]
            if caps:
                rows.append((method, sorted(set(caps))))
        if rows:
            out[name] = rows
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
        for crossing in sorted(set(entry.get("emissions") or [])):
            key = crossing.split(".", 1)[0]
            for service in _service_for_key(key, services, components):
                graph.node("service", service)
                graph.edge(comp_id, graph.node("service", service),
                           "crosses", APPROX)

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


def _adjacency(graph: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for edge in graph.get("edges") or []:
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


def _path_precision(graph: dict, path: list[str]) -> str:
    by_pair = {(edge["from"], edge["to"]): edge["precision"]
               for edge in graph.get("edges") or []}
    precisions = {by_pair.get((path[i], path[i + 1]), APPROX)
                  for i in range(len(path) - 1)}
    return APPROX if APPROX in precisions else EXACT


def roles_reaching(graph: dict, capability: str) -> list[dict]:
    """Every model role with an admitted path to `capability`, with a witness.

    One path per role: the shortest, ties broken lexicographically so two runs
    and two compositions with the same shape report the same witness.
    """
    goal = node_id("capability", capability)
    adjacency = _adjacency(graph)
    out = []
    for node in graph.get("nodes") or []:
        if node.get("kind") != "role":
            continue
        path = _shortest_path(adjacency, node_id("role", node["name"]), goal)
        if path is None:
            continue
        out.append({"role": node["name"],
                    "residence": node.get("residence"),
                    "opaque": bool(node.get("opaque")),
                    "path": path,
                    "precision": _path_precision(graph, path)})
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


def diff(previous: dict, current: dict) -> dict:
    """The edges an update adds, removes and keeps.

    The edge identity is `(from, kind, to)`; an attribute change on the same
    edge is not a new authority fact. `widened` is true when the update adds
    any edge, which is the dangerous direction an operator reviews.
    """
    before = {_edge_key(edge): edge for edge in previous.get("edges") or []}
    after = {_edge_key(edge): edge for edge in current.get("edges") or []}
    added = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys())
    return {"added": added, "removed": removed,
            "unchanged": sorted(after.keys() & before.keys()),
            "widened": bool(added)}
