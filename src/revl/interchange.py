"""The versioned composition-interchange format (DESIGN §10, roadmap item 28).

`revl audit --json` already emits the manifest + G8 boundary surface as data.
This module is the small missing piece that turns that data into a *stable,
documented, versioned interchange format*: the verdict a revl gate produces —
what each component provides, requires, and reaches — that another tool can
consume WITHOUT running revl.

Three things live here and nowhere else:

  * `INTERCHANGE_VERSION` / `INTERCHANGE_KIND` — the format's identity,
  * `stamp()` — the one place a raw audit dict becomes a versioned document,
  * `validate()` — a dependency-free check that a document satisfies the
    published JSON Schema (`schema/revl-interchange-v1.schema.json`), so the
    schema is executable, not just prose.

Compatibility promise (also in docs/interchange-format.md):

  `schema_version` is MAJOR.MINOR. A consumer written for MAJOR.m keeps
  reading every MAJOR.n with n >= m: within a MAJOR, changes are *additive*
  only — new optional members appear, existing members keep their name,
  meaning, and shape. A MAJOR bump is the only thing that may remove or
  re-shape a member. Consumers should therefore ignore unknown members
  (the schema is `additionalProperties: true` at every level for exactly this
  reason) and gate strictly on MAJOR.

Composability with the gauntlet dossier (item 31): the envelope reserves an
optional `gauntlet` member. A dossier's proved/tested/claimed/pending
sections already speak the manifest+boundary vocabulary defined by this
schema, so a dossier drops into the same document without a schema change —
`stamp(report, gauntlet=dossier)` produces one interchange document that
carries both the composition's audit and a candidate's graded verdict.
"""

from __future__ import annotations

import json
from pathlib import Path

# MAJOR.MINOR. Bump MINOR for an additive change (a new optional member);
# bump MAJOR only for a breaking one (a removed or re-shaped member).
INTERCHANGE_VERSION = "1.0"

# Self-identifying tag: lets a consumer tell an interchange document apart
# from any other JSON without sniffing its shape.
INTERCHANGE_KIND = "revl.interchange"

# The published JSON Schema, checked in beside the sources so the format is
# consumable by tools in any language, not just this one.
SCHEMA_PATH = (Path(__file__).resolve().parent.parent.parent
               / "schema" / "revl-interchange-v1.schema.json")


def stamp(report: dict, *, gauntlet: dict | None = None) -> dict:
    """Turn a raw audit report into a versioned interchange document.

    `report` is exactly the dict `revl audit --json` assembles
    (`manifest` / `boundary` / `externs` / `distributability`). This adds the
    `schema_version` and `kind` header additively and, when given, carries a
    gauntlet dossier in the reserved `gauntlet` member. The input is not
    mutated.
    """
    document = {"schema_version": INTERCHANGE_VERSION, "kind": INTERCHANGE_KIND}
    document.update(report)
    if gauntlet is not None:
        document["gauntlet"] = gauntlet
    return document


def load_schema() -> dict:
    """The published JSON Schema, parsed."""
    with open(SCHEMA_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def validate(document: dict, schema: dict | None = None) -> list[str]:
    """Check `document` against the interchange schema. Returns a list of
    human-readable error strings; an empty list means the document is valid.

    Prefers `jsonschema` when it is importable (full Draft 2020-12 coverage);
    otherwise falls back to a small built-in validator that supports the
    subset of keywords the published schema uses (type, properties, required,
    additionalProperties, patternProperties, items, enum, minimum, $ref to a
    local `#/$defs/...`). No third-party dependency is required to consume the
    format.
    """
    schema = schema if schema is not None else load_schema()
    try:  # pragma: no cover - exercised only where jsonschema is installed
        import jsonschema  # noqa: PLC0415

        validator = jsonschema.Draft202012Validator(schema)
        return [f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
                for e in sorted(validator.iter_errors(document),
                                key=lambda e: list(e.path))]
    except ModuleNotFoundError:
        errors: list[str] = []
        _check(document, schema, schema, "<root>", errors)
        return errors


# --------------------------------------------------------------- fallback

def _resolve(ref: str, root: dict) -> dict:
    """Resolve a local `#/a/b` JSON pointer against the schema root."""
    node: object = root
    for part in ref.lstrip("#/").split("/"):
        if not part:
            continue
        node = node[part]  # type: ignore[index]
    assert isinstance(node, dict)
    return node


_JSON_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _type_ok(value: object, expected: str) -> bool:
    if expected == "integer":
        # bool is an int subclass in Python; JSON integer must not be a bool
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    return isinstance(value, _JSON_TYPES[expected])


def _check(value: object, schema: dict, root: dict, path: str,
           errors: list[str]) -> None:
    """Append a message to `errors` for every way `value` violates `schema`."""
    if "$ref" in schema:
        _check(value, _resolve(schema["$ref"], root), root, path, errors)
        return

    types = schema.get("type")
    if types is not None:
        allowed = [types] if isinstance(types, str) else list(types)
        if not any(_type_ok(value, t) for t in allowed):
            errors.append(f"{path}: expected type {'|'.join(allowed)}, "
                          f"got {type(value).__name__}")
            return  # further keyword checks assume the type held

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")

    if "pattern" in schema and isinstance(value, str):
        import re  # noqa: PLC0415

        if not re.search(schema["pattern"], value):
            errors.append(f"{path}: {value!r} does not match /{schema['pattern']}/")

    if "minimum" in schema and isinstance(value, (int, float)) \
            and not isinstance(value, bool):
        if value < schema["minimum"]:
            errors.append(f"{path}: {value} < minimum {schema['minimum']}")

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required property {key!r}")
        props = schema.get("properties", {})
        pattern_props = schema.get("patternProperties", {})
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            child = f"{path}/{key}"
            if key in props:
                _check(item, props[key], root, child, errors)
            else:
                matched = False
                if pattern_props:
                    import re  # noqa: PLC0415

                    for pat, sub in pattern_props.items():
                        if re.search(pat, key):
                            _check(item, sub, root, child, errors)
                            matched = True
                if not matched:
                    if isinstance(additional, dict):
                        _check(item, additional, root, child, errors)
                    elif additional is False:
                        errors.append(f"{child}: unexpected property {key!r}")

    elif isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _check(item, item_schema, root, f"{path}[{index}]", errors)
