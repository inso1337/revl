"""Service ⇄ MCP tool projection.

A revl `service` declaration and an MCP tool definition describe the same
thing: a named, typed, side-effecting operation at a boundary. They differ
in one respect that runs in revl's favour — MCP's behavioural annotations
(`readOnlyHint`, `destructiveHint`) are *hints asserted by the server
author*, whereas revl's `emission` classification is *checked by the
compiler*. Projecting one to the other therefore has a direction of trust:

  revl -> MCP   annotations are **derived** from the checker. A tool
                generated from a non-`emission` operation is read-only
                because the language refused to compile it otherwise.

  MCP -> revl   nothing is vouched for, so everything the manifest does not
                positively assert as read-only becomes an `emission`, and
                the whole imported surface lands in the G8 audit.
"""

from __future__ import annotations

import json
import re

from ..type_schema import (  # noqa: F401 -- re-exported, see type_schema
    _JSON_TYPES,
    _parse_type,
    _variant_arm,
    admits_json_null,
    expressibility_reason,
    fully_expressible,
    has_revl_stub,
    json_schema_for,
)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ---------------------------------------------------------------- revl -> MCP

def tools_from_ir(ir: dict, *, composition: str = "revl") -> list[dict]:
    """Project every *provided* service operation to an MCP tool definition.

    Only provided keys are exposed: a composition's requirements are its
    own business, its provisions are its surface.

    The behavioural annotation comes from the declaration, and the checker
    makes that sound: a service declaration is an *upper bound* on its
    providers' effects (G4 emission propagation), so an operation declared
    plain cannot reach an emission in any provider's body. The walk below
    therefore records *provenance* — which emissions and host code a body
    actually reaches — rather than correcting the declaration.
    """
    services = ir.get("services") or {}
    types = ir.get("types") or {}
    externs = {e["name"]: e for e in ir.get("externs") or []}
    reach = _extern_reachability(ir, externs)
    local_reach = _local_write_reach(ir, services)
    tools: list[dict] = []

    for index, component in enumerate(ir.get("components") or []):
        provided = provided_methods(component)
        for key, service_name in (component.get("provides") or {}).items():
            service = services.get(service_name) or {}
            bodies = provided.get(key) or {}
            for op_name, op in (service.get("methods") or {}).items():
                observed = _method_effects(bodies.get(op_name) or [], component,
                                           services, externs, reach)
                # #2146: the `local` writes the operation reaches by every
                # route, including a required service's provider and a
                # callable handed on as a value
                observed["local_writes"] = set(observed["local_writes"]) \
                    | local_reach.get((index, key, op_name), set())
                observed["writes_host"] = observed["writes_host"] \
                    or bool(observed["local_writes"])
                tools.append(_tool(composition, key, service_name, op_name, op,
                                   component, types, observed))
    return tools


def _tool(composition: str, key: str, service_name: str, op_name: str, op: dict,
          component: dict, types: dict, observed: dict) -> dict:
    # the declaration is authoritative: the checker refuses a provider whose
    # body exceeds it, so `emission` is exactly the operation's contract
    emission = bool(op.get("emission"))
    # a capability-scoped `emission[db]` is a *checked* upper bound on where
    # this operation may reach; `None` is bare `emission` — "any capability"
    scope = op.get("capabilities")
    # a `witnessed[db]` operation is the reversible class: its body may write
    # through a tracked inverse, so it is not read-only — but it is not
    # destructive either, since an abort reverts every write it made. `None` is
    # bare `witnessed`; the key is absent on a plain operation.
    witnessed = op.get("witnessed")
    uses_extern = observed["uses_extern"]
    local_writes = sorted(observed.get("local_writes") or ())
    params = op.get("params") or []
    properties, required = {}, []
    for param in params:
        pname = param.get("name") if isinstance(param, dict) else param
        ptype = param.get("type") if isinstance(param, dict) else None
        properties[pname] = json_schema_for(ptype, types)
        head, _ = _parse_type(ptype)
        if head != "Opt":
            required.append(pname)

    returns = op.get("returns")
    if emission:
        reached = ", ".join(sorted(observed["emissions"])) or "host code"
        behaviour = ("Emission: crosses the system boundary and cannot be reverted "
                     f"(reaches {reached}).")
        if scope:
            behaviour += (" Capability-scoped: the compiler refused any provider "
                          f"emitting outside [{', '.join(scope)}].")
        else:
            behaviour += (" Unscoped: the declaration names no capability, so it "
                          "promises nothing about where the emission goes.")
    elif witnessed is not None:
        behaviour = ("Reversible: the compiler proved every mutation here carries a "
                     "tracked inverse, and refused any provider that reaches an "
                     "emission. A commit settles these writes; an abort reverts them.")
        if witnessed:
            behaviour += (" Capability-scoped: the compiler refused any provider "
                          f"writing outside [{', '.join(witnessed)}].")
        else:
            behaviour += (" Unscoped: the declaration names no capability, so it "
                          "promises nothing about where the writes go.")
    elif local_writes:
        behaviour = ("Local durable write: reaches the `local` extern(s) "
                     f"{', '.join(local_writes)}, which write durable state on this "
                     "host without crossing the system boundary. Nothing reverts "
                     "that write, and the `local` class is the extern author's "
                     "word: the compiler does not inspect the host body. A service "
                     "declaration still bounds what its providers may do, so no "
                     "provider of this operation can reach an emission.")
    else:
        behaviour = ("Read-only: the compiler refused any unreverted mutation here, "
                     "and a service declaration bounds what its providers may do — "
                     "no provider of this operation can reach an emission.")
    if local_writes and (emission or witnessed is not None):
        behaviour += (" It also reaches the `local` extern(s) "
                      f"{', '.join(local_writes)}: a durable write on this host "
                      "that nothing reverts.")
    description = (
        f"{service_name}.{op_name} — provided at key `{key}` by component "
        f"`{component.get('name')}`. " + behaviour
    )

    return {
        "name": f"{composition}.{key}.{op_name}",
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        **({"outputSchema": json_schema_for(returns, types)} if returns else {}),
        "annotations": {
            "title": f"{key}.{op_name}",
            # derived from the checker, not asserted by an author. `readOnlyHint`
            # is a proof that the body mutates NOTHING — a witnessed operation
            # writes (reversibly), so it may not claim it; `destructiveHint`
            # stays false because the checker refuses anything it cannot revert.
            # A reached `local` extern (#2146) writes durably and is not
            # reverted, and the checker does not bound what its host body does
            # (it may delete), so it is neither read-only nor provably
            # non-destructive.
            "readOnlyHint": not emission and witnessed is None and not local_writes,
            "destructiveHint": emission or bool(local_writes),
            "idempotentHint": bool(op.get("commutative")),
            "openWorldHint": uses_extern,
        },
        # the provenance that makes the annotations trustworthy
        "x-revl": {
            "composition": composition,
            "component": component.get("name"),
            "key": key,
            "service": service_name,
            "operation": op_name,
            "classification": ("emission" if emission else
                               "witnessed" if witnessed is not None else "checked"),
            # `["*"]` is bare `emission` — any capability. A named list is an
            # upper bound the checker enforces (docs/capabilities.md).
            "capabilities": list(scope) if scope else (["*"] if emission else []),
            # the same shape for the reversible class: `["*"]` is bare
            # `witnessed`, and `[]` means the operation is plain.
            "witnessed": (list(witnessed) if witnessed else
                          (["*"] if witnessed is not None else [])),
            "async": bool(op.get("async")),
            "commutative": bool(op.get("commutative")),
            "annotationsDerivedFrom": "compiler",
            "effects": {
                # provenance under a declaration the checker holds to an
                # upper bound — a plain operation reaches neither of these
                "reachesEmission": sorted(observed["emissions"]),
                "reachesHostCode": sorted(observed["externs"]),
                # the reached `local` externs (#2146): durable, unreverted,
                # not crossings. Absent when none is reached, so every tool
                # that reaches no `local` extern keeps its bytes.
                **({"reachesLocalWrite": local_writes} if local_writes else {}),
                # the boundaries this body actually crosses — a subset of the
                # declared `capabilities` above, which the checker enforces
                "reachesCapabilities": sorted(observed["capabilities"]),
                "boundedByDeclaration": True,
            },
            "guarantee": (
                f"G4 — an emission bounded to [{', '.join(scope)}]: no provider "
                f"of this operation can reach any other boundary"
                if emission and scope else
                "G4 — an emission is the language's admission of irreversibility"
                if emission else
                f"G4 — a witnessed bound to [{', '.join(witnessed)}]: every "
                f"mutation carries a tracked inverse, and no provider can reach "
                f"an emission"
                if witnessed else
                "G4 — a bare `witnessed` bound: every mutation carries a tracked "
                "inverse, and no provider can reach an emission"
                if witnessed is not None else
                "G4 — every mutation in this operation carries a tracked inverse"
            ),
        },
    }


def provided_methods(component: dict) -> dict[str, dict[str, list]]:
    """The lowered method bodies a component installs, per provide key:
    `{key: {method_name: body}}`. The first `provide` step for a key wins.

    This is the one reader of the lowered `provide` step's shape.
    `revl.shadow_runtime.declared_actions` takes its action names from it
    rather than walking the same steps again."""
    blocks: dict[str, dict[str, list]] = {}
    for step in component.get("body") or []:
        if step.get("step") == "provide":
            blocks.setdefault(step.get("name"), {
                m.get("name"): m.get("body") or []
                for m in step.get("methods") or []})
    return blocks


def _called_fns(node, found: set) -> None:
    """Function names a lowered body calls. Two shapes exist: component
    bodies lower a call to `{kind: fn, name}`, pure fn bodies to
    `{kind: call, callee: {kind: var, name}}`."""
    if isinstance(node, dict):
        if node.get("kind") == "fn" and isinstance(node.get("name"), str):
            found.add(node["name"])
        callee = node.get("callee")
        if node.get("kind") == "call" and isinstance(callee, dict) \
                and callee.get("kind") == "var" and isinstance(callee.get("name"), str):
            found.add(callee["name"])
        for value in node.values():
            _called_fns(value, found)
    elif isinstance(node, list):
        for value in node:
            _called_fns(value, found)


def _local_write_reach(ir: dict, services: dict) -> dict[tuple, set]:
    """#2146: `(component index, provide key, operation)` -> every `local`
    extern the operation's body reaches.

    A service declaration bounds its providers' EMISSIONS, so an operation
    declared plain is read-only with respect to crossings whatever its
    providers do. It does not bound a `local` write, which is not a crossing,
    so the provenance walk has to follow every route one can take:

      * a call or a VALUE reference (`apply(wipe, n)`) to an extern or a pure
        fn, closed by the checker's own fixed point seeded by the `local`
        class (`emission_analysis._emitting_capabilities`), and
      * a call to a required service's operation, which reaches whatever the
        composition's providers of that service reach, to a fixed point."""
    from ..emission_analysis import _calls_in, _emitting_capabilities

    externs = list(ir.get("externs") or [])
    fns = ir.get("functions") or []
    if isinstance(fns, dict):
        fns = list(fns.values())
    by_fn = _emitting_capabilities(list(fns), externs, by_name=True,
                                   classes=("local",))
    components = list(ir.get("components") or [])
    providers: dict[str, list] = {}
    for index, component in enumerate(components):
        for key, service_name in (component.get("provides") or {}).items():
            providers.setdefault(service_name, []).append((index, key))

    local: dict[tuple, set] = {}
    calls: dict[tuple, set] = {}
    for index, component in enumerate(components):
        requires = component.get("requires") or {}
        for key, bodies in provided_methods(component).items():
            for op_name, body in bodies.items():
                called: set = set()
                values: set = set()
                _calls_in(body, called, values=values)
                reached: set = set()
                for name in called | values:
                    reached |= by_fn.get(name) or set()
                local[(index, key, op_name)] = reached - {"*"}
                seams: set = set()

                def walk(node):
                    if isinstance(node, dict):
                        target = node.get("target")
                        if node.get("kind") == "call" and isinstance(target, dict) \
                                and target.get("kind") == "req":
                            seams.add((requires.get(target.get("name")),
                                       node.get("method")))
                        for value in node.values():
                            walk(value)
                    elif isinstance(node, list):
                        for value in node:
                            walk(value)

                walk(body)
                calls[(index, key, op_name)] = seams

    changed = True
    while changed:  # least fixed point over the service seams
        changed = False
        for node, seams in calls.items():
            for service_name, method in seams:
                for pindex, pkey in providers.get(service_name, ()):
                    more = local.get((pindex, pkey, method), set()) - local[node]
                    if more:
                        local[node] |= more
                        changed = True
    return local


def _extern_reachability(ir: dict, externs: dict) -> dict[str, set]:
    """fn name -> the externs it reaches, transitively through other fns."""
    functions = {fn["name"]: fn for fn in ir.get("functions") or []}
    direct: dict[str, set] = {}
    calls: dict[str, set] = {}
    for name, fn in functions.items():
        called: set = set()
        _called_fns(fn.get("body") or [], called)
        direct[name] = {c for c in called if c in externs}
        calls[name] = {c for c in called if c in functions}

    resolved: dict[str, set] = {}

    def resolve(name: str, seen: frozenset = frozenset()) -> set:
        if name in resolved:
            return resolved[name]
        if name in seen:  # recursion: the cycle contributes nothing new
            return set()
        reached = set(direct.get(name, ()))
        for callee in calls.get(name, ()):  # noqa: SIM118 — explicit for clarity
            reached |= resolve(callee, seen | {name})
        resolved[name] = reached
        return reached

    for name in functions:
        resolve(name)
    return resolved


def _method_effects(body: list, component: dict, services: dict,
                    externs: dict, reach: dict[str, set]) -> dict:
    """What a provide-method's *implementation* actually reaches: emissions
    on required services (declared or via `emit` steps), host code, and the
    *capabilities* those crossings name (docs/capabilities.md) — a required
    key for a service emission, the extern itself for host code."""
    emissions: set = set()
    extern_names: set = set()
    capabilities: set = set()

    def walk_expr(node):
        if isinstance(node, dict):
            target = node.get("target")
            if node.get("kind") == "call" and isinstance(target, dict) \
                    and target.get("kind") == "req":
                service_name = (component.get("requires") or {}).get(target.get("name"))
                spec = ((services.get(service_name) or {}).get("methods") or {}) \
                    .get(node.get("method")) or {}
                if spec.get("emission"):
                    emissions.add(f"{target['name']}.{node['method']}")
                    capabilities.add(target["name"])
            if node.get("kind") == "fn":
                name = node.get("name")
                if name in externs:
                    extern_names.add(name)
                extern_names.update(reach.get(name, set()))
            for value in node.values():
                walk_expr(value)
        elif isinstance(node, list):
            for value in node:
                walk_expr(value)

    def walk_steps(steps):
        for step in steps:
            if step.get("step") == "emit":
                expr = step.get("expr") or {}
                target = expr.get("target") or {}
                if target.get("kind") == "req":
                    emissions.add(f"{target.get('name')}.{expr.get('method')}")
                    capabilities.add(target.get("name"))
                else:
                    emissions.add("host emission")
            walk_expr(step)

    walk_steps(body)
    # a host extern that is not `pure` writes to the outside world
    writes_host = any((externs.get(name) or {}).get("class")
                      in ("emission", "acquire", "local")
                      for name in extern_names)
    # a `local` extern (#2146) is a durable write that is not a crossing: no
    # declaration bounds it and nothing reverts it, so an operation that
    # reaches one may not claim to be read-only
    local_writes = {name for name in extern_names
                    if (externs.get(name) or {}).get("class") == "local"}
    # an `emission` extern *is* the boundary, so it names its own capability
    capabilities |= {name for name in extern_names
                     if (externs.get(name) or {}).get("class") == "emission"}
    return {
        "emissions": emissions,
        "externs": extern_names,
        "capabilities": capabilities,
        "uses_extern": bool(extern_names),
        "writes_host": writes_host,
        "local_writes": local_writes,
    }


# ---------------------------------------------------------------- MCP -> revl
#
# ONE classification serves two surfaces: `revl mcp import` renders it as revl
# source, and `revl mcp proxy` (revl.mcp.proxy) loads the same rendering into a
# live session and gates calls with it. The proxy never re-derives a class: a
# runtime observation can only feed a tool name back in through `distrust`,
# which removes a read-only claim before this function reads it.

EFFECT_PLAIN = "plain"
EFFECT_EMISSION = "emission"
EFFECT_WITNESSED = "witnessed"


def parse_undo_specs(specs) -> dict[str, dict]:
    """`TOOL=INVERSE` or `TOOL=INVERSE:result` (the CLI `--undo` spelling) ->
    `{tool: {"tool": inverse, "with": "arguments" | "result"}}`.

    `with` says what the inverse receives: the forward call's own arguments
    (the default, the soft-delete/restore shape), or the forward call's
    `structuredContent` object (`:result`, the "delete returns what it removed"
    shape)."""
    out: dict[str, dict] = {}
    for spec in specs or []:
        tool, sep, inverse = str(spec).partition("=")
        mode = "arguments"
        if inverse.endswith(":result"):
            inverse, mode = inverse[:-len(":result")], "result"
        elif inverse.endswith(":arguments"):
            inverse = inverse[:-len(":arguments")]
        if not sep or not tool or not inverse:
            raise ValueError(f"--undo expects TOOL=INVERSE[:result], got {spec!r}")
        if tool in out:
            raise ValueError(f"--undo names {tool!r} twice")
        out[tool] = {"tool": inverse, "with": mode}
    return out


def _read_only_contradiction(tool: dict, annotations: dict) -> str | None:
    """Why a tool's OWN declaration contradicts its `readOnlyHint: true`, or None.

    MCP defines `destructiveHint` as meaningful only when the tool is NOT
    read-only, so a tool that sets both has declared an effect its read-only
    claim denies. A revl-served upstream (`revl serve --mcp`) also carries the
    checker's own classification under `x-revl`, and an `emission` there is a
    declared crossing."""
    if annotations.get("destructiveHint") is True:
        return "it also declares `destructiveHint: true`"
    provenance = tool.get("x-revl")
    if isinstance(provenance, dict) and provenance.get("classification") == "emission":
        return "its `x-revl` provenance classifies it `emission`"
    return None


def classify_imported_tools(manifest, *, undo: dict | None = None,
                            distrust: dict | None = None,
                            trust_read_only: bool = True) -> list[dict]:
    """Classify each tool of an MCP `tools/list` result. The single source of
    the effect class for both `revl mcp import` and `revl mcp proxy`.

    Trust direction: an MCP annotation is the server author's assertion, so
    **only** an explicit `readOnlyHint: true` avoids `emission`, and only while
    nothing contradicts it:

      * `plain`: the tool claims `readOnlyHint: true` and neither its own
        declaration, the operator, nor an observation (`distrust`) contradicts
        the claim. The claim is still UNCHECKED: revl cannot see into the
        upstream, so `readOnlyClaim` says `unchecked`, never `verified`.
      * `witnessed`: the operator declared an undo for it (`undo`, from
        `--undo TOOL=INVERSE`). A declared undo is the operator's statement
        that the tool mutates and names the tool that reverts it.
      * `emission`: everything else, including an absent or malformed
        annotations block. This is the most restrictive class (class (c)
        under the approval policy: a human yes per call).

    `distrust` maps a tool name to the reason its read-only claim is no longer
    believed (the proxy's runtime observation). Either way the claim is removed
    and the tool falls to `emission` by the rule above, not by a second rule.

    `trust_read_only` decides what an UNCHECKED claim is worth. `True` (the
    default, and what `revl mcp import` uses) admits it as `plain`: the
    importer writes source for a human to review before anything runs.
    `False` (the default of `revl mcp proxy`, which enforces at run time with
    no review step) holds it at `emission` and sets `gated` to say why; the
    claim itself stays `unchecked`, since nothing refuted it either.

    Each entry: `name`, `op` (a unique revl identifier), `effect`,
    `readOnlyClaim` (`none` / `unchecked` / `contradicted` / `observed`),
    `gated` (why an uncontradicted claim is still gated, or None), `reasons`,
    `unclassifiable` (a reason or None), `undo`, `params`, `doc`, `callable`
    (False when the tool has no usable name).
    """
    tools = manifest.get("tools") if isinstance(manifest, dict) else manifest
    tools = tools or []
    undo = dict(undo or {})
    distrust = dict(distrust or {})

    names = [t.get("name") if isinstance(t, dict) else None for t in tools]
    known = {n for n in names if isinstance(n, str) and n}
    for tool_name, spec in undo.items():
        if tool_name not in known:
            raise ValueError(f"--undo names {tool_name!r}, which the server does not list")
        if spec["tool"] not in known:
            raise ValueError(f"--undo {tool_name}={spec['tool']}: the server does not "
                             f"list {spec['tool']!r}")
        if spec["tool"] == tool_name:
            raise ValueError(f"--undo {tool_name}={tool_name}: a tool cannot undo itself")
    inverses = {spec["tool"]: name for name, spec in undo.items()}

    taken: set[str] = set()
    out: list[dict] = []
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            tool = {}
        raw_name = tool.get("name")
        usable = isinstance(raw_name, str) and bool(raw_name)
        name_text = raw_name if usable else ""
        op = _unique_ident(_safe_ident(name_text.rsplit(".", 1)[-1] or name_text), taken)

        reasons: list[str] = []
        unclassifiable = None
        annotations = tool.get("annotations")
        if annotations is None:
            annotations = {}
        elif not isinstance(annotations, dict):
            unclassifiable = "its `annotations` is not an object"
            annotations = {}
        hint = annotations.get("readOnlyHint")
        if hint is not None and not isinstance(hint, bool):
            unclassifiable = f"its `readOnlyHint` is {json.dumps(hint)}, not a boolean"
        if not usable:
            unclassifiable = "it has no usable `name`"
        elif names.count(raw_name) > 1:
            unclassifiable = "the server lists more than one tool under this name"

        claim = "unchecked" if hint is True else "none"
        if claim == "unchecked":
            contradiction = _read_only_contradiction(tool, annotations)
            if contradiction is None and name_text in undo:
                contradiction = "the operator declared an undo for it"
            if contradiction is None and name_text in inverses:
                contradiction = (f"the operator named it as the undo of "
                                 f"{json.dumps(inverses[name_text])}, so it mutates")
            if contradiction is not None:
                claim = "contradicted"
                reasons.append(f"read-only claim contradicted: {contradiction}")
            elif name_text in distrust:
                claim = "observed"
                reasons.append(f"read-only claim contradicted by observed behaviour: "
                               f"{distrust[name_text]}")

        gated = None
        if claim == "unchecked" and not trust_read_only and unclassifiable is None \
                and name_text not in undo:
            gated = "unchecked read-only claim"

        spec = undo.get(name_text)
        if unclassifiable is not None:
            effect = EFFECT_EMISSION
            reasons.append(f"unclassifiable ({unclassifiable}): held at the most "
                           "restrictive class")
            if spec is not None:
                reasons.append("the declared undo is ignored for an unclassifiable tool")
            spec = None
        elif spec is not None:
            effect = EFFECT_WITNESSED
            reasons.append(f"the operator declared {json.dumps(spec['tool'])} as its undo")
        elif gated is not None:
            effect = EFFECT_EMISSION
            reasons.append("gated: unchecked read-only claim. It claims "
                           "`readOnlyHint: true` and revl cannot check the claim, "
                           "so it is not trusted")
        elif claim == "unchecked":
            effect = EFFECT_PLAIN
            reasons.append("claims `readOnlyHint: true`; revl cannot check the claim")
        else:
            effect = EFFECT_EMISSION
            if claim == "none":
                reasons.append("no read-only claim")

        doc = (tool.get("description") or "") if isinstance(tool.get("description"), str) else ""
        doc_lines = doc.strip().splitlines()
        schema = tool.get("inputSchema")
        out.append({
            "name": raw_name if usable else None,
            "index": index,
            "op": op,
            "effect": effect,
            "readOnlyClaim": claim,
            "gated": gated,
            "reasons": reasons,
            "unclassifiable": unclassifiable,
            "undo": dict(spec) if spec is not None else None,
            "params": _params_from_schema(schema if isinstance(schema, dict) else {}),
            "doc": doc_lines[0] if doc_lines else "",
            "callable": usable,
        })
    return out


def _default_host_body(tool: dict, role: str) -> str:
    if role == "settled":
        return "/* the undo is terminal: nothing is left to release */"
    if role == "undo":
        return (f"/* undo MCP tool {json.dumps(tool['name'] or '')} by calling "
                f"{json.dumps(tool['undo']['tool'])} with its {tool['undo']['with']} */")
    return f"/* call MCP tool {json.dumps(tool['name'] or '')} */"


def render_imported_source(tools: list[dict], *, service: str = "Imported",
                           key: str = "imported", backend: str = "ts",
                           host_body=None, signature: str = "typed") -> str:
    """Render classified tools (`classify_imported_tools`) as revl source.

    `signature="typed"` maps each tool's input schema to typed parameters (the
    `revl mcp import` output). `signature="json"` gives every operation one
    `arguments: Str` parameter carrying the JSON arguments object verbatim, so
    a proxy forwards what the client sent without a lossy round trip through
    revl types. `host_body(tool, role)` supplies the text of each host block
    (`role` is `call`, `undo` or `settled`); the default is a comment stub."""
    host_body = host_body or _default_host_body
    ops, methods, externs = [], [], []
    settled_rendered = False
    for tool in tools:
        op = tool["op"]
        effect = tool["effect"]
        if signature == "json":
            params = [("arguments", "Str")]
        else:
            params = tool["params"]
        sig = ", ".join(f"{p}: {t}" for p, t in params)
        call_args = ", ".join(p for p, _ in params)
        extern_name = f"mcp_{op}"

        if tool["doc"]:
            ops.append(f"  // {tool['doc'][:78]}")
        for reason in tool["reasons"]:
            # every upstream name inside a reason is JSON-quoted, so no reason
            # carries a line break out of its comment
            if reason.startswith(("read-only claim", "unclassifiable", "gated")):
                ops.append(f"  // {' '.join(reason.splitlines())}")
        if effect == EFFECT_WITNESSED:
            ops.append(f"  // imported with a declared undo: "
                       f"{json.dumps(tool['undo']['tool'])} reverts it on abort")
            ops.append(f"  emission fn {op}({sig})")
            if not settled_rendered:
                externs.append("extern pure fn settled_mcp() -> Unit\n"
                               f"  = @{backend} {{ {host_body(tool, 'settled')} }}")
                settled_rendered = True
            externs.append(
                f"extern acquire fn undo_{extern_name}(w: Str) -> Unit undo settled_mcp()\n"
                f"  = @{backend} {{ {host_body(tool, 'undo')} }}")
            externs.append(
                f"extern witnessed fn {extern_name}({sig}) -> Result[Str, Str] "
                f"undo undo_{extern_name}(result)\n"
                f"  = @{backend} {{ {host_body(tool, 'call')} }}")
            methods.append(f"    fn {op}({call_args}) {{ effect {extern_name}({call_args}) }}")
            continue

        plain = effect == EFFECT_PLAIN
        if not plain:
            ops.append("  // imported without a verifiable read-only claim")
        ops.append(f"  {'' if plain else 'emission '}fn {op}({sig}) -> Str")
        externs.append(
            f"extern {'pure' if plain else 'emission'} fn {extern_name}({sig}) -> Str\n"
            f"  = @{backend} {{ {host_body(tool, 'call')} }}"
        )
        # an emission crossing carries its `emit` marker at the call site
        # (issue #1437); a witnessed tool is marked by `effect` above
        mark = "" if plain else "emit "
        methods.append(f"    fn {op}({call_args}) = {mark}{extern_name}({call_args})")

    header = (
        "// Generated by `revl mcp import` — an imported MCP surface.\n"
        "//\n"
        "// Nothing here is checked: MCP annotations are assertions by the\n"
        "// server author, so every operation without an explicit\n"
        "// `readOnlyHint: true` is classified `emission` (irreversible) and\n"
        "// appears on the G8 audit surface. Narrow a classification only\n"
        "// after verifying the tool's behaviour yourself.\n"
    )
    body = "\n".join(ops) if ops else "  // (server exposed no tools)"
    return (
        f"{header}\nservice {service} {{\n{body}\n}}\n\n"
        + "\n\n".join(externs)
        + (f"\n\ncomponent {service}Provider provides {key}: {service} {{\n"
           f"  provide {key} {{\n" + "\n".join(methods) + "\n  }\n}\n" if methods else "\n")
    )


def import_tools(manifest: dict, *, service: str = "Imported",
                 key: str = "imported", backend: str = "ts",
                 undo: dict | None = None) -> str:
    """Turn an MCP server's `tools/list` result into revl source: a service
    declaration plus an extern-backed provider skeleton.

    Trust direction: an MCP annotation is the server author's assertion, and
    revl has no way to check it, so **only** an explicit, uncontradicted
    `readOnlyHint: true` avoids `emission`. Everything else (including an
    absent annotations block) is classified irreversible and lands on the G8
    audit surface. `undo` (`parse_undo_specs`) makes a tool `witnessed`: its
    declared inverse runs on abort. See `classify_imported_tools`.
    """
    return render_imported_source(
        classify_imported_tools(manifest, undo=undo),
        service=service, key=key, backend=backend)


def _params_from_schema(schema: dict) -> list[tuple[str, str]]:
    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):
        properties = {}
    required = schema.get("required") or []
    required = set(required) if isinstance(required, list) else set()
    params = []
    taken: set[str] = set()
    for name, spec in properties.items():
        surface = _surface_type(spec if isinstance(spec, dict) else {})
        if name not in required:
            surface = f"Opt[{surface}]"
        params.append((_unique_ident(_safe_ident(name), taken), surface))
    return params


def _surface_type(spec: dict) -> str:
    json_type = spec.get("type")
    if json_type == "string":
        return "Str"
    if json_type == "integer":
        return "Int"
    if json_type == "number":
        return "Float"
    if json_type == "boolean":
        return "Bool"
    if json_type == "array":
        items = spec.get("items")
        return f"List[{_surface_type(items if isinstance(items, dict) else {})}]"
    return "Str"  # unknown/object payloads arrive as text


def _safe_ident(name: str) -> str:
    from ..lexer import KEYWORDS  # noqa: PLC0415 - one keyword list, the lexer's

    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name or "tool")
    if not _IDENT.match(cleaned):
        cleaned = f"t_{cleaned}"
    if cleaned in KEYWORDS:
        # a reserved word cannot name a method or parameter (`type` is a common
        # MCP parameter name); the suffix keeps it readable and unique
        cleaned = f"{cleaned}_"
    return cleaned


def _unique_ident(ident: str, taken: set) -> str:
    """`ident`, or `ident_2`, `ident_3`... when two names sanitize alike."""
    candidate, n = ident, 1
    while candidate in taken:
        n += 1
        candidate = f"{ident}_{n}"
    taken.add(candidate)
    return candidate
