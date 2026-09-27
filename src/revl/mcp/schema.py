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


def admits_json_null(type_name: str | None) -> bool:
    """Does this surface type's JSON derivation accept a bare `null` document?

    Exactly two surface shapes do: `Unit`, which renders as `{"type": "null"}`,
    and any `Opt[_]`, which adds `null` alongside its inner rendering. A variant
    never does at a validated boundary (every case is a tagged object, so a
    nullary case is `{"tag": "Nil"}`, not `null`), a record renders as an object,
    and a `List`/`Map` renders as an array/object.

    This is asked of the SURFACE type, never of the derived schema, because a
    JSON document carries ONE `null` and a derived schema carries ONE nullability
    flag: by the time the nesting has been rendered, the thing that distinguishes
    `Opt[Opt[T]]` from `Opt[T]` is already gone (issue #1263).
    """
    if not type_name:
        return False
    if type_name == "Unit":
        return True
    head, args = _parse_type(type_name)
    return head == "Opt" and bool(args)


def json_schema_for(type_name: str | None, types: dict | None = None,
                    *, validated: bool = False,
                    seen: frozenset = frozenset()) -> dict:
    """Surface type -> JSON Schema. Records expand structurally; unknown
    nominal types degrade to an unconstrained schema carrying the revl name
    (honest about what the projection does not know).

    A payload-carrying variant renders as a DISCRIMINATED union (item 257,
    §3.2): each arm is a closed object keyed by a `tag` pinned with `const` to
    the case name, a payload-bearing case carrying its payload under `value`.
    `additionalProperties: false` plus the `const` tag make the arms mutually
    exclusive, so a validator names the constructor rather than guessing it.

    `validated=True` (the boundary-validation consumer, item 257) renders ALL
    variants tagged, including all-nullary ones (MEDIUM-1): one wire shape, one
    construction path, and adding a payload case never re-encodes the cases
    beside it. `validated=False` (the plain MCP projection) keeps the compact
    `enum` rendering for an enum-shaped (all-nullary) variant; a payload-carrying
    variant is tagged in BOTH modes (it can no longer degrade to the old
    `x-revlType` stub, MEDIUM-2).

    `seen` threads the nominal names currently on the derivation path so the
    renderer TERMINATES on a recursive type: once section 3.2 recurses into
    payloads there is no base case, and a cyclic ADT would otherwise recurse
    forever. On a cycle hit the renderer falls back to the honest `x-revlType`
    stub, which is sound for MCP projection; a `validated` boundary never reaches
    this fallback because `fully_expressible` (below) refuses a cyclic type
    BEFORE any schema is derived, and the derived-dict scan asserts no stub
    survives on an accepted type.
    """
    types = types or {}
    if not type_name:
        return {}
    if type_name in _JSON_TYPES:
        return dict(_JSON_TYPES[type_name])
    head, args = _parse_type(type_name)
    if head == "List" and args:
        return {"type": "array",
                "items": json_schema_for(args[0], types, validated=validated, seen=seen)}
    if head == "Opt" and args:
        # A NULL-AMBIGUOUS `Opt` has no JSON rendering at all (issue #1263).
        # `{**inner, "nullable": True}` folds the outer layer into a flag on the
        # inner schema, so when the inner type already accepts `null` the outer
        # layer vanishes and `Opt[Opt[Str]]` derives the SAME dict as `Opt[Str]`.
        # Renaming the flag does not help: JSON has one `null`, so no document
        # can say which layer produced it. Degrade to the honest stub rather than
        # hand back a schema that claims two distinct types are one. A `validated`
        # boundary never reaches this line, having been refused by
        # `fully_expressible` below.
        if admits_json_null(args[0]):
            return {"x-revlType": type_name}
        inner = json_schema_for(args[0], types, validated=validated, seen=seen)
        return {**inner, "nullable": True} if inner else {"nullable": True}
    if head == "Map" and len(args) == 2:
        return {"type": "object",
                "additionalProperties": json_schema_for(args[1], types,
                                                         validated=validated, seen=seen)}
    if head == "Result" and len(args) == 2:
        return {"oneOf": [json_schema_for(args[0], types, validated=validated, seen=seen),
                          json_schema_for(args[1], types, validated=validated, seen=seen)]}
    # A nominal already on the path is (mutually) recursive: stop rather than
    # recurse forever, returning the honest stub (MCP-only; a validated boundary
    # never gets here, having refused the cycle at the `fully_expressible` gate).
    if type_name in seen:
        return {"x-revlType": type_name}
    spec = types.get(type_name)
    if spec and spec.get("kind") == "record":
        fields = spec.get("fields") or {}
        inner_seen = seen | {type_name}
        return {
            "type": "object",
            "properties": {n: json_schema_for(t, types, validated=validated, seen=inner_seen)
                           for n, t in fields.items()},
            "required": sorted(fields),
        }
    if spec and spec.get("kind") == "variant":
        cases = spec.get("cases") or []
        all_nullary = all(not c.get("payload") for c in cases)
        if all_nullary and not validated:
            return {"enum": [case["name"] for case in cases]}
        inner_seen = seen | {type_name}
        return {"oneOf": [_variant_arm(case, types, validated, inner_seen)
                          for case in cases]}
    return {"x-revlType": type_name}


def _variant_arm(case: dict, types: dict, validated: bool, seen: frozenset) -> dict:
    """One arm of a discriminated union: a closed object whose `tag` is pinned
    with `const` to the case name, plus a `value` carrying the payload schema
    (recursively) when the case has a payload (item 257, §3.2)."""
    name = case.get("name")
    payload = case.get("payload")
    if payload:
        return {
            "type": "object",
            "properties": {"tag": {"const": name},
                           "value": json_schema_for(payload, types,
                                                    validated=validated, seen=seen)},
            "required": ["tag", "value"],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": {"tag": {"const": name}},
        "required": ["tag"],
        "additionalProperties": False,
    }


def fully_expressible(type_name: str | None, types: dict | None = None,
                      seen: frozenset = frozenset()) -> bool:
    """Is a surface type derivable to an EXACT JSON Schema, so a validating
    boundary can trust it (item 257, §3.3)?

    This is a POSITIVE, TOTAL, CYCLE-AWARE predicate over the SURFACE type,
    evaluated BEFORE any schema is derived. It is the gate for a `validated`
    emission: the derive-then-scan sentinel of the first draft is necessary but
    not sufficient (three degradations reach the boundary unsound with no
    `x-revlType` marker, and a cyclic type makes the derivation itself
    non-terminating, a compile-time crash rather than a refusal, §0). Returns
    False (non-expressible, refuse) for:

    - an **unknown nominal** type (a name `types` does not carry);
    - an **untagged `Result[T, E]`** (its derivation is an untagged `oneOf`,
      which cannot name a constructor and, for `T == E`, admits no valid value);
    - a **null-ambiguous `Opt`** (`Opt[Unit]`, `Opt[Opt[U]]`, issue #1263): the
      inner type already accepts `null`, and a JSON document carries one `null`,
      so the outer layer has no document of its own. Both spellings derive the
      same schema and a validator built on it accepts values for the wrong type;
      a caller who needs to tell two absences apart declares a named variant that
      tags each one;
    - a **`Map[K, V]` with `K != Str`** (the mapping drops `K`, so a validator
      cannot enforce the key type; JSON object keys are strings, so `Map[Str, V]`
      is expressible and any non-`Str`-key map is refused);
    - **any type on a cycle** (`type_name in seen`): an inline schema cannot
      express a recursive type and deriving it would not terminate. A later slice
      may lift this via `$ref`/`$defs` (§3.3); Slice 1 refuses it.

    Otherwise it recurses into structure with `type_name` added to `seen`. Since
    `seen` grows on every nominal descent and the set of nominal names is finite,
    the predicate is TOTAL: it terminates on every input, cyclic or not.
    """
    types = types or {}
    if not type_name:
        return False
    if type_name in _JSON_TYPES:
        return True
    head, args = _parse_type(type_name)
    if head == "List" and args:
        return fully_expressible(args[0], types, seen)
    if head == "Opt" and args:
        # a NULL-AMBIGUOUS `Opt` (issue #1263): `Opt[Unit]`, `Opt[Opt[U]]`. Its
        # inner type already accepts `null`, so the outer layer has no JSON
        # document of its own and the derivation erases it.
        if admits_json_null(args[0]):
            return False
        return fully_expressible(args[0], types, seen)
    if head == "Map" and len(args) == 2:
        # JSON object keys are strings: only a `Str` key survives derivation.
        return args[0] == "Str" and fully_expressible(args[1], types, seen)
    if head == "Result" and len(args) == 2:
        # An untagged `oneOf` cannot name its constructor at a validating
        # boundary; a caller who wants a validated two-arm response declares a
        # named tagged variant instead.
        return False
    # a nominal type
    if type_name in seen:  # (mutually) recursive: inline schema cannot express it
        return False
    spec = types.get(type_name)
    if not spec:  # unknown nominal
        return False
    inner_seen = seen | {type_name}
    if spec.get("kind") == "record":
        return all(fully_expressible(t, types, inner_seen)
                   for t in (spec.get("fields") or {}).values())
    if spec.get("kind") == "variant":
        return all(fully_expressible(c["payload"], types, inner_seen)
                   for c in (spec.get("cases") or []) if c.get("payload"))
    return False


def expressibility_reason(type_name: str | None, types: dict | None = None,
                          seen: frozenset = frozenset()) -> str | None:
    """A human explanation of WHY a surface type is not fully expressible, or
    `None` when it is (item 257, §3.3). Mirrors `fully_expressible`'s structure
    to name the first offending position for the compile-time diagnostic; the
    boolean gate remains authoritative, this only phrases the refusal."""
    types = types or {}
    if not type_name:
        return "has no declared type to validate"
    if type_name in _JSON_TYPES:
        return None
    head, args = _parse_type(type_name)
    if head == "List" and args:
        return expressibility_reason(args[0], types, seen)
    if head == "Opt" and args:
        if admits_json_null(args[0]):
            return (f"reaches `{type_name}`, whose inner type `{args[0]}` already "
                    "accepts `null`, so the outer `Opt` has no JSON document of "
                    "its own and both layers derive the same schema (declare a "
                    "named variant that gives each absence a tag)")
        return expressibility_reason(args[0], types, seen)
    if head == "Map" and len(args) == 2:
        if args[0] != "Str":
            return (f"reaches `{type_name}`, whose key type `{args[0]}` is not "
                    "expressible (JSON object keys are strings; use a `Str` key)")
        return expressibility_reason(args[1], types, seen)
    if head == "Result" and len(args) == 2:
        return (f"reaches an untagged `{type_name}`, which cannot name its "
                "constructor at a validating boundary (declare a named tagged "
                "variant instead)")
    if type_name in seen:
        return (f"is recursive (`{type_name}` reaches `{type_name}`), which an "
                "inline schema cannot express (the `$ref`/`$defs` route is a "
                "later slice; §3.3)")
    spec = types.get(type_name)
    if not spec:
        return f"reaches `{type_name}`, which has no JSON-Schema derivation"
    inner_seen = seen | {type_name}
    if spec.get("kind") == "record":
        for t in (spec.get("fields") or {}).values():
            reason = expressibility_reason(t, types, inner_seen)
            if reason is not None:
                return reason
        return None
    if spec.get("kind") == "variant":
        for c in spec.get("cases") or []:
            if c.get("payload"):
                reason = expressibility_reason(c["payload"], types, inner_seen)
                if reason is not None:
                    return reason
        return None
    return f"reaches `{type_name}`, which has no JSON-Schema derivation"


def has_revl_stub(schema: object) -> bool:
    """Whether an `x-revlType` marker survives at any depth of a derived schema.
    Kept only as a DEFENSE-IN-DEPTH assertion (item 257, §3.3): it runs on a type
    `fully_expressible` already accepted, so it never meets a cyclic type and
    never fails to terminate. A firing here means the predicate and the renderer
    have drifted (a renderer bug), caught loudly rather than shipping an
    unconstrained boundary."""
    if isinstance(schema, dict):
        if "x-revlType" in schema:
            return True
        return any(has_revl_stub(v) for v in schema.values())
    if isinstance(schema, list):
        return any(has_revl_stub(v) for v in schema)
    return False


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
        provided = provided_methods(component)
        for key, service_name in (component.get("provides") or {}).items():
            service = services.get(service_name) or {}
            bodies = provided.get(key) or {}
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
    # a capability-scoped `emission[db]` is a *checked* upper bound on where
    # this operation may reach; `None` is bare `emission` — "any capability"
    scope = op.get("capabilities")
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
        if scope:
            behaviour += (" Capability-scoped: the compiler refused any provider "
                          f"emitting outside [{', '.join(scope)}].")
        else:
            behaviour += (" Unscoped: the declaration names no capability, so it "
                          "promises nothing about where the emission goes.")
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
            # `["*"]` is bare `emission` — any capability. A named list is an
            # upper bound the checker enforces (docs/capabilities.md).
            "capabilities": list(scope) if scope else (["*"] if emission else []),
            "async": bool(op.get("async")),
            "commutative": bool(op.get("commutative")),
            "annotationsDerivedFrom": "compiler",
            "effects": {
                # provenance under a declaration the checker holds to an
                # upper bound — a plain operation reaches neither of these
                "reachesEmission": sorted(observed["emissions"]),
                "reachesHostCode": sorted(observed["externs"]),
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
    writes_host = any((externs.get(name) or {}).get("class") in ("emission", "acquire")
                      for name in extern_names)
    # an `emission` extern *is* the boundary, so it names its own capability
    capabilities |= {name for name in extern_names
                     if (externs.get(name) or {}).get("class") == "emission"}
    return {
        "emissions": emissions,
        "externs": extern_names,
        "capabilities": capabilities,
        "uses_extern": bool(extern_names),
        "writes_host": writes_host,
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
