"""Issue #2704: one derivation from a service operation to a tool contract.

A `validated` emission already gets a JSON Schema from its return type, a GBNF
grammar rendered from that same schema and a digest pinning both
(`revl.decode_grammar`). That covers what a model must SEND BACK. It does not
cover what a model may ASK THE PROGRAM TO DO: a tool offered to a model is a
service operation with typed parameters, a capability scope and an effect, and
until now those facts were written once in the service declaration and again,
by hand, in whatever schema the host sent to the model.

This module derives the argument half from the declaration alone:

* :func:`argument_schema` - the JSON Schema of the operation's parameters,
  built by the same `json_schema_for(validated=True)` the return boundary uses,
  so a record or variant argument has exactly the wire form the validator
  accepts. Every parameter is required and the object is closed, matching the
  grammar's language (a grammar that omitted an extra member would not be the
  same language as the schema).
* :func:`tool_contract` - the schema, its GBNF grammar and the JSON-Schema
  dialect, each with the digest form `decode_grammar` already defines. The
  grammar is rendered from the schema object, never from a second walk over the
  surface type, so the two halves cannot drift.
* :func:`refine_arguments` - the value check the grammar cannot make. A grammar
  fixes syntax, not values: `fs.write(path=path)` binds the declared capability
  parameter to the caller's argument, and a call whose value falls outside the
  held capability's cone is refused here even though the grammar accepted the
  string. This is not optional, for the same reason the return boundary keeps
  its post-hoc validator: revl STATES and the provider PERFORMS.

The compiler/provider split is unchanged (design 542 sections 2 and 3): the
contract is data, identical on every tier, and nothing here calls a decoder or
claims the provider enforced the grammar.
"""

from __future__ import annotations

import json

from . import cap_order
from .decode_grammar import (decode_grammar_for, grammar_digest,
                             json_schema_grammar_for)
from .type_schema import (expressibility_reason, fully_expressible,
                          json_schema_for)


class ToolSchemaError(ValueError):
    """An operation named by `revl grammar --tool` cannot be derived: no such
    service, no such operation, or a parameter type with no exact JSON Schema
    (the same gate a `validated` boundary applies to its return type)."""


def _operation(ir: dict, service: str, operation: str) -> dict:
    services = ir.get("services") or {}
    if service not in services:
        known = ", ".join(sorted(services)) or "(none)"
        raise ToolSchemaError(
            f"no service `{service}` in this document (services: {known})")
    methods = services[service].get("methods") or {}
    if operation not in methods:
        known = ", ".join(sorted(methods)) or "(none)"
        raise ToolSchemaError(
            f"service `{service}` has no operation `{operation}` "
            f"(operations: {known})")
    return methods[operation]


def operation_capabilities(ir: dict, service: str, operation: str) -> list[str]:
    """The declared capability spellings of one operation, as the IR stores
    them (canonical, argument-bound parameters unquoted)."""
    return list(_operation(ir, service, operation).get("capabilities") or [])


def argument_schema(ir: dict, service: str, operation: str) -> dict:
    """The JSON Schema of an operation's arguments, derived from the parameter
    types declared on the service operation.

    `validated=True` is the same projection a `validated` return boundary uses:
    a record is a closed object, a variant a discriminated union. A parameter
    type with no exact schema refuses here, by the same gate, rather than
    degrading to a permissive stub a decoder could not respect.
    """
    spec = _operation(ir, service, operation)
    types = ir.get("types") or {}
    properties: dict[str, dict] = {}
    for param in spec.get("params") or []:
        name = param.get("name")
        ptype = param.get("type")
        if not fully_expressible(ptype, types):
            reason = expressibility_reason(ptype, types) or "no exact JSON Schema"
            raise ToolSchemaError(
                f"argument `{name}` of `{service}.{operation}` cannot be a "
                f"tool argument: `{ptype}` {reason}")
        properties[name] = json_schema_for(ptype, types, validated=True)
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def tool_contract(ir: dict, service: str, operation: str) -> dict:
    """The whole tool contract for one operation: the argument schema, the GBNF
    grammar rendered from it, the wire-schema dialect, and their digests.

    The digests use `grammar_digest`, the same identity a return-type grammar
    carries, so a provider may cache a compiled artifact by it and a parameter
    type change necessarily changes the value it names.
    """
    schema = argument_schema(ir, service, operation)
    schema_text = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return {
        "service": service,
        "operation": operation,
        "schema": schema,
        "schema_digest": grammar_digest(schema_text),
        "grammar": decode_grammar_for(schema),
        "json_schema": json_schema_grammar_for(schema),
    }


def _resolved_capabilities(declared: list[str] | tuple[str, ...],
                           arguments: dict,
                           declarations: dict | None) -> list[cap_order.Cap]:
    """Resolve each declared capability against the call's arguments.

    An argument-bound parameter (`fs.write(path=path)`, issue #1985) takes the
    caller's value for that argument; a literal or `config.` symbol is carried
    through unchanged. A binding that names an argument the call does not
    supply is a refused call, not an unconstrained one: the declared destination
    would be missing from the check.
    """
    out: list[cap_order.Cap] = []
    for text in declared or ():
        token = text.split("(", 1)[0]
        declaration = (declarations or {}).get(token)
        cap = cap_order.parse_cap(text, declaration)
        raw: list[tuple[str, object]] = []
        for name, value in cap.params:
            if isinstance(value, cap_order.Symbol) and value.is_arg:
                if value.ref not in arguments:
                    raise ToolSchemaError(
                        f"the declared capability `{text}` binds parameter "
                        f"`{name}` to argument `{value.ref}`, which the call "
                        f"does not supply")
                raw.append((name, arguments[value.ref]))
            else:
                raw.append((name, value))
        out.append(cap_order.make_cap(token, raw, declaration))
    return out


def refine_arguments(declared: list[str] | tuple[str, ...], arguments: dict,
                     held: list[str] | tuple[str, ...],
                     declarations: dict | None = None) -> list[str]:
    """Refuse a call whose arguments put it outside the held capabilities.

    The grammar accepted the argument string; this checks the VALUE. Every
    declared capability of the operation is resolved (an argument-bound
    parameter takes the caller's value) and must be covered by some held
    capability, using the one partial order `cap_order` defines. An empty
    return means the call is within the caller's authority; otherwise each
    string names the capability that is not held and the argument, when one
    bound it.

    `declarations` is the document's `capability -> {name: kind}` map
    (`ir["capability_declarations"]`), which lets a declared resource dimension
    be read back; omitting it leaves only the core parameter registry, exactly
    as `cap_order` documents (fail closed).
    """
    held_caps = []
    for text in held or ():
        token = text.split("(", 1)[0]
        held_caps.append(cap_order.parse_cap(text, (declarations or {}).get(token)))
    arg_bound = declared_symbols(declared, declarations)
    violations: list[str] = []
    for cap in _resolved_capabilities(declared, arguments, declarations):
        if cap_order.covered_by_any(held_caps, cap):
            continue
        bound = [
            f"argument `{arg_bound[(cap.token, name)].ref}` = "
            f"{arguments[arg_bound[(cap.token, name)].ref]!r}"
            for name, _value in cap.params
            if (cap.token, name) in arg_bound
        ]
        detail = f" ({'; '.join(bound)})" if bound else ""
        violations.append(
            f"`{cap.to_str()}`{detail} is not covered by the held capabilities "
            f"({', '.join(held) or 'none'}): the call asks for authority the "
            f"caller does not hold")
    return violations


def declared_symbols(declared: list[str] | tuple[str, ...],
                     declarations: dict | None) -> dict[tuple[str, str],
                                                         object]:
    """The declared (token, parameter) pairs whose value is an argument-bound
    symbol, so `refine_arguments` can name the argument in its refusal without
    re-reading the declaration. Kept separate and pure for that one purpose."""
    out: dict[tuple[str, str], object] = {}
    for text in declared or ():
        token = text.split("(", 1)[0]
        cap = cap_order.parse_cap(text, (declarations or {}).get(token))
        for name, value in cap.params:
            if isinstance(value, cap_order.Symbol) and value.is_arg:
                out[(token, name)] = value
    return out
