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

# surface type -> JSON Schema fragment
_JSON_TYPES = {
    "Str": {"type": "string"},
    "Int": {"type": "integer"},
    "Float": {"type": "number"},
    "Bool": {"type": "boolean"},
    "Bytes": {"type": "string", "contentEncoding": "base64"},
    "Unit": {"type": "null"},
}

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parse_type(name: str | None):
    if not name:
        return None, []
    if "[" not in name or not name.endswith("]"):
        return name, []
    head, _, rest = name.partition("[")
    inner, args, depth, start = rest[:-1], [], 0, 0
    for i, ch in enumerate(inner):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif ch == "," and depth == 0:
            args.append(inner[start:i].strip())
            start = i + 1
    args.append(inner[start:].strip())
    return head, args


def json_schema_for(type_name: str | None, types: dict | None = None) -> dict:
    """Surface type -> JSON Schema. Records expand structurally; unknown
    nominal types degrade to an unconstrained schema carrying the revl name
    (honest about what the projection does not know)."""
    types = types or {}
    if not type_name:
        return {}
    if type_name in _JSON_TYPES:
        return dict(_JSON_TYPES[type_name])
    head, args = _parse_type(type_name)
    if head == "List" and args:
        return {"type": "array", "items": json_schema_for(args[0], types)}
    if head == "Opt" and args:
        inner = json_schema_for(args[0], types)
        return {**inner, "nullable": True} if inner else {"nullable": True}
    if head == "Map" and len(args) == 2:
        return {"type": "object", "additionalProperties": json_schema_for(args[1], types)}
    if head == "Result" and len(args) == 2:
        return {"oneOf": [json_schema_for(args[0], types), json_schema_for(args[1], types)]}
    spec = types.get(type_name)
    if spec and spec.get("kind") == "record":
        fields = spec.get("fields") or {}
        return {
            "type": "object",
            "properties": {n: json_schema_for(t, types) for n, t in fields.items()},
            "required": sorted(fields),
        }
    if spec and spec.get("kind") == "variant":
        return {"enum": [case["name"] for case in spec.get("cases") or []]} \
            if all(not c.get("payload") for c in spec.get("cases") or []) \
            else {"x-revlType": type_name}
    return {"x-revlType": type_name}


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
    tools: list[dict] = []

    for component in ir.get("components") or []:
        for key, service_name in (component.get("provides") or {}).items():
            service = services.get(service_name) or {}
            bodies = _provide_methods(component, key)
            for op_name, op in (service.get("methods") or {}).items():
                observed = _method_effects(bodies.get(op_name) or [], component,
                                           services, externs, reach)
                tools.append(_tool(composition, key, service_name, op_name, op,
                                   component, types, observed))
    return tools


def _tool(composition: str, key: str, service_name: str, op_name: str, op: dict,
          component: dict, types: dict, observed: dict) -> dict:
    # the declaration is authoritative: the checker refuses a provider whose
    # body exceeds it, so `emission` is exactly the operation's contract
    emission = bool(op.get("emission"))
    uses_extern = observed["uses_extern"]
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
    else:
        behaviour = ("Read-only: the compiler refused any unreverted mutation here, "
                     "and a service declaration bounds what its providers may do — "
                     "no provider of this operation can reach an emission.")
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
            # derived from the checker, not asserted by an author:
            "readOnlyHint": not emission,
            "destructiveHint": emission,
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
            "classification": "emission" if emission else "checked",
            "async": bool(op.get("async")),
            "commutative": bool(op.get("commutative")),
            "annotationsDerivedFrom": "compiler",
            "effects": {
                # provenance under a declaration the checker holds to an
                # upper bound — a plain operation reaches neither of these
                "reachesEmission": sorted(observed["emissions"]),
                "reachesHostCode": sorted(observed["externs"]),
                "boundedByDeclaration": True,
            },
            "guarantee": (
                "G4 — an emission is the language's admission of irreversibility"
                if emission else
                "G4 — every mutation in this operation carries a tracked inverse"
            ),
        },
    }


def _provide_methods(component: dict, key: str) -> dict[str, list]:
    """The lowered method bodies a component installs at `key`."""
    for step in component.get("body") or []:
        if step.get("step") == "provide" and step.get("name") == key:
            return {m.get("name"): m.get("body") or []
                    for m in step.get("methods") or []}
    return {}


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
    on required services (declared or via `emit` steps) and host code."""
    emissions: set = set()
    extern_names: set = set()

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
                else:
                    emissions.add("host emission")
            walk_expr(step)

    walk_steps(body)
    # a host extern that is not `pure` writes to the outside world
    writes_host = any((externs.get(name) or {}).get("class") in ("emission", "acquire")
                      for name in extern_names)
    return {
        "emissions": emissions,
        "externs": extern_names,
        "uses_extern": bool(extern_names),
        "writes_host": writes_host,
    }


# ---------------------------------------------------------------- MCP -> revl

def import_tools(manifest: dict, *, service: str = "Imported",
                 key: str = "imported", backend: str = "ts") -> str:
    """Turn an MCP server's `tools/list` result into revl source: a service
    declaration plus an extern-backed provider skeleton.

    Trust direction: an MCP annotation is the server author's assertion, and
    revl has no way to check it, so **only** an explicit `readOnlyHint: true`
    avoids `emission` — everything else (including an absent annotations
    block) is classified irreversible and lands on the G8 audit surface.
    """
    tools = manifest.get("tools") if isinstance(manifest, dict) else manifest
    tools = tools or []

    ops, methods, externs = [], [], []
    for tool in tools:
        raw_name = tool.get("name") or ""
        op = _safe_ident(raw_name.rsplit(".", 1)[-1] or raw_name)
        annotations = tool.get("annotations") or {}
        read_only = annotations.get("readOnlyHint") is True
        params = _params_from_schema(tool.get("inputSchema") or {})
        returns = "Str"  # MCP content is text unless the server says otherwise
        sig = ", ".join(f"{p}: {t}" for p, t in params)

        doc = (tool.get("description") or "").strip().splitlines()
        if doc:
            ops.append(f"  // {doc[0][:78]}")
        if not read_only:
            ops.append(f"  // imported without a verifiable read-only claim")
        ops.append(f"  {'' if read_only else 'emission '}fn {op}({sig}) -> {returns}")

        extern_name = f"mcp_{op}"
        externs.append(
            f"extern {'pure' if read_only else 'emission'} fn {extern_name}({sig}) -> {returns}\n"
            f"  = @{backend} {{ /* call MCP tool {json.dumps(raw_name)} */ }}"
        )
        call_args = ", ".join(p for p, _ in params)
        methods.append(f"    fn {op}({call_args}) = {extern_name}({call_args})")

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


def _params_from_schema(schema: dict) -> list[tuple[str, str]]:
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    params = []
    for name, spec in properties.items():
        surface = _surface_type(spec)
        if name not in required:
            surface = f"Opt[{surface}]"
        params.append((_safe_ident(name), surface))
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
        return f"List[{_surface_type(spec.get('items') or {})}]"
    return "Str"  # unknown/object payloads arrive as text


def _safe_ident(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name or "tool")
    if not _IDENT.match(cleaned):
        cleaned = f"t_{cleaned}"
    return cleaned
