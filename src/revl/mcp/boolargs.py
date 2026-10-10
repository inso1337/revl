"""Boolean arguments checked against the verb's own schema (issue #2239).

Every tool declares its arguments in `inputSchema`, and a property declared
`{"type": "boolean"}` is read by its handler as the Python singleton
(`... is True`, `... is not False`). Nothing checked the type before the
handler ran, so a string `"false"` for `revl_edit`'s `commit` committed the
edit live, and a string `"true"` for `overwrite` read as absent, with a
refusal telling the caller to pass the value it had already passed.

This runs once, at dispatch, before any handler. For every property the
schema declares boolean (at the top level, and inside object properties and
array items wherever the schema declares them):

- a real bool passes unchanged;
- the exact strings `"true"` and `"false"` are canonicalised to the bool
  (terse output is accepted and canonicalised server-side, as issue #1700
  does for source), and the response names every key that was;
- anything else is refused before dispatch, naming the key, the expected
  type and the type received, in the shape of the `revl_verbs` hatch's own
  refusal (`disclosure.py`).

Properties the schema does not declare are left alone: this checks what the
verb promises, not what a handler happens to read.
"""

from __future__ import annotations

_CANONICAL = {"true": True, "false": False}


def _kind(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return f"the string {value!r}"
    return type(value).__name__


def _is_type(schema: dict, name: str) -> bool:
    declared = schema.get("type")
    return declared == name or (isinstance(declared, list) and name in declared)


def _walk(schema: dict, value, path: str, canonical: list):
    """`value` checked against `schema`: the value with its declared booleans
    canonicalised, or raises `ValueError` naming the first one that is not."""
    if not isinstance(schema, dict):
        return value
    if _is_type(schema, "boolean") and not isinstance(value, bool):
        if isinstance(value, str) and value in _CANONICAL:
            canonical.append(path)
            return _CANONICAL[value]
        if not _is_type(schema, "null") or value is not None:
            raise ValueError(f"`{path}` must be a boolean (true or false); "
                             f"got {_kind(value)}")
    properties = schema.get("properties")
    if isinstance(properties, dict) and isinstance(value, dict):
        out = None
        for key, sub in properties.items():
            if key not in value:
                continue
            child = _walk(sub, value[key], f"{path}.{key}" if path else key,
                          canonical)
            if child is not value[key]:
                if out is None:
                    out = dict(value)
                out[key] = child
        return value if out is None else out
    items = schema.get("items")
    if isinstance(items, dict) and isinstance(value, list):
        out = None
        for index, item in enumerate(value):
            child = _walk(items, item, f"{path}[{index}]", canonical)
            if child is not item:
                if out is None:
                    out = list(value)
                out[index] = child
        return value if out is None else out
    return value


def check(schema: dict | None, arguments: dict):
    """`(arguments, canonicalised, refusal)`.

    `arguments` is a copy with every declared boolean that arrived as
    `"true"`/`"false"` replaced by the bool (the caller's dict is never
    mutated); `canonicalised` names those keys in order; `refusal` is the
    message for a declared boolean that is neither, or `""`."""
    canonical: list[str] = []
    if not isinstance(schema, dict) or not isinstance(arguments, dict):
        return arguments, canonical, ""
    try:
        return _walk(schema, arguments, "", canonical), canonical, ""
    except ValueError as error:
        return arguments, [], str(error)


def note(canonicalised: list[str]) -> str:
    """The one line a response carries when it canonicalised a key."""
    keys = ", ".join(f"`{key}`" for key in canonicalised)
    return (f"read the string value of {keys} as the boolean it names; "
            f"send true or false unquoted")
