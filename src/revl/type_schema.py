"""Surface type -> JSON Schema, the half of the MCP tool projection the compiler
itself needs.

`revl.lower` checks whether a boundary type is expressible as JSON Schema and
renders the schema (`fully_expressible`, `expressibility_reason`,
`json_schema_for`), and `revl.decode_grammar` reads the same type parser. They
used to import it from `revl.mcp.schema`, which put the `revl.mcp` package on
the compiler's import graph and, through it, the MCP server (issue #1780). This
module has no dependency at all, so the compiler reaches it without reaching
`revl.mcp`. `revl.mcp.schema` re-exports every name defined here, unchanged,
for the tool projection and its importers.
"""

from __future__ import annotations

# surface type -> JSON Schema fragment
_JSON_TYPES = {
    "Str": {"type": "string"},
    "Int": {"type": "integer"},
    "Float": {"type": "number"},
    "Bool": {"type": "boolean"},
    "Bytes": {"type": "string", "contentEncoding": "base64"},
    "Unit": {"type": "null"},
}


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
