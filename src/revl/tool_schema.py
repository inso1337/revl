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
  dialect, each with the digest form `decode_grammar` already defines, plus the
  operation's declared EFFECT: `emission` and the `capabilities` a scoped
  emission names. The grammar is rendered from the schema object, never from a
  second walk over the surface type, so the two halves cannot drift.

THIS MODULE DERIVES DATA, NOT ENFORCEMENT. It adds no runtime and no call
boundary, so it does not check a call's argument VALUES against a held
capability cone. That check needs the caller's held set, which lives in the
host that dispatches the call; a refinement helper here would be a claim no
caller honours (the fail-open shape this issue was reviewed for), so it was
removed rather than left unwired. The harness's tool surface is the companion
slice that reads this contract and can enforce against its own authority.

A RANGE DIVERGENCE THIS SCHEMA EXPOSES, pre-existing and not introduced here:
JSON Schema `"type": "integer"` is unbounded, so the grammar and schema accept
an integer of any magnitude for an `Int` parameter. revl documents `Int` as
64-bit two's complement, and go, java and rust carry it as i64 (refusing or
faulting outside that range), while python and TypeScript's `bigint` hold
arbitrary precision. A model that emits 2^63 therefore passes this schema on
every tier and only some tiers can run it. Bounding the schema is a wire-format
decision; until then the contract reports a JSON integer with no range,
exactly as the return boundary's schema already does.
"""

from __future__ import annotations

import json

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
    grammar rendered from it, the wire-schema dialect, their digests, and the
    operation's declared effect (`emission`, `capabilities`).

    The digests use `grammar_digest`, the same identity a return-type grammar
    carries, so a provider may cache a compiled artifact by it and a parameter
    type change necessarily changes the value it names. A bare emission carries
    `emission: true` and an empty capability list: "any capability", the IR's
    own spelling of it, is reported as the absence of a scope rather than
    silently dropped.
    """
    spec = _operation(ir, service, operation)
    schema = argument_schema(ir, service, operation)
    schema_text = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return {
        "service": service,
        "operation": operation,
        "schema": schema,
        "schema_digest": grammar_digest(schema_text),
        "grammar": decode_grammar_for(schema),
        "json_schema": json_schema_grammar_for(schema),
        "emission": bool(spec.get("emission")),
        "capabilities": operation_capabilities(ir, service, operation),
    }
