"""Map a public agent-security suite's tools onto revl services.

The revl arm of the AgentDojo comparison (issue #2683) is only meaningful if the
suite's own tools are what the model is allowed to call. This module is that
mapping, and it is deliberately mechanical: every suite function becomes one
revl service with one operation, ``call``, whose parameters are the function's
parameters in their declared order. The service declaration is the granted
surface an untrusted author is admitted against; nothing else is reachable.

Two properties matter for the comparison:

* The mapping is derived from the suite, not hand-written per suite, so a task
  set cannot quietly gain or lose a tool between the two arms.
* The grant names the service, not the method. A turn that requires a service
  it was not granted is refused by the untrusted-author profile before the
  component is wired, which is the refusal the driver counts.

The host side of the bridge lives in :mod:`bench.agentdojo.bridge`; this module
only writes the declarations and the dispatched extern bodies.
"""

from __future__ import annotations

import enum
import types as _types
import typing
from typing import Any, get_args, get_origin

# The revl types the bridge can lower directly. Any other annotation travels as
# a JSON string, marked in the generated extern so the bridge decodes it before
# the suite sees it; a silently-stringified argument would make the tool call
# disagree with the baseline arm.
_SCALARS = {
    str: "Str",
    int: "Int",
    float: "Float",
    bool: "Bool",
}


class UnsupportedToolType(TypeError):
    """A suite tool parameter the port cannot express as a revl type."""


def revl_type(annotation: Any) -> str:
    """The revl spelling of one AgentDojo parameter annotation."""
    if annotation in _SCALARS:
        return _SCALARS[annotation]
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (list, tuple) and len(args) == 1:
        return f"List[{revl_type(args[0])}]"
    # `str | None` / `Optional[str]`: the tool's own default decides the value
    # when a caller omits it, but the revl call site still has to name the
    # argument, so the declaration keeps it with an Opt.
    if origin is _types.UnionType or origin is typing.Union:
        members = [a for a in args if a is not type(None)]
        if len(members) == 1 and len(args) == 2:
            return f"Opt[{revl_type(members[0])}]"
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        # The suite validates the enum member name from a string, and the
        # model can only write a string, so a string is the honest surface.
        return "Str"
    raise UnsupportedToolType(
        f"tool parameter annotation {annotation!r} has no revl mapping")


def service_name(tool_name: str) -> str:
    return f"Tool_{tool_name}"


def requirement_key(tool_name: str) -> str:
    return f"tool_{tool_name}"


def extern_name(tool_name: str) -> str:
    return f"host_{tool_name}"


def _optional(annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin is not _types.UnionType and origin is not typing.Union:
        return False
    members = [a for a in get_args(annotation) if a is not type(None)]
    return len(members) == 1 and len(get_args(annotation)) == 2


def _param_type(annotation: Any) -> tuple[str, bool]:
    """The revl type for one parameter, and whether it travels as JSON.

    A parameter whose annotation revl cannot name (a list of attachment
    records, for example) travels as a JSON string. The declaration says Str
    because that is the value the turn author writes; the bridge decodes it
    before the suite sees it.
    """
    try:
        return revl_type(annotation), False
    except UnsupportedToolType:
        return ("Opt[Str]" if _optional(annotation) else "Str"), True


def _params(tool: Any) -> list[tuple[str, str, bool]]:
    return [
        (name, *_param_type(field.annotation))
        for name, field in tool.parameters.model_fields.items()
    ]


def declarations(tools: list[Any]) -> dict[str, Any]:
    """The base composition's service block, plus the grant it implies.

    Returns a dict with:
      ``source``       the revl text declaring and providing every tool service
      ``granted``      the service names the untrusted turn may reach
      ``signatures``   ``{tool_name: "call(a: Str, b: Int) -> Str"}`` for prompts
      ``params``       ``{tool_name: [(name, revl_type), ...]}``
    """
    chunks: list[str] = []
    signatures: dict[str, str] = {}
    params: dict[str, list[tuple[str, str]]] = {}
    granted: list[str] = []
    for tool in tools:
        name = tool.name
        svc = service_name(name)
        key = requirement_key(name)
        extern = extern_name(name)
        ps = _params(tool)
        params[name] = [(p, t) for p, t, _ in ps]
        json_params = sorted(p for p, _, is_json in ps if is_json)
        rendered = ", ".join(f"{p}: {t}" for p, t, _ in ps)
        args = ", ".join(p for p, _, _ in ps)
        kwargs = ", ".join(f'"{p}": {p}' for p, _, _ in ps)
        extra = (
            f", json_params={json_params!r}" if json_params else ""
        )
        signatures[name] = f"call({rendered}) -> Str"
        granted.append(svc)
        chunks.append(
            f"service {svc} {{ fn {signatures[name]} }}\n"
            f"extern pure fn {extern}({rendered}) -> Str = @py {{\n"
            f"  import revl_agentdojo_bridge as _b\n"
            f'  return _b.call("{name}", {{{kwargs}}}{extra})\n'
            f"}}\n"
            f"component ToolProvider_{name} provides {key}: {svc} {{\n"
            f"  provide {key} {{ fn call({args}) = {extern}({args}) }}\n"
            f"}}\n"
        )
    return {
        "source": "\n".join(chunks),
        "granted": granted,
        "signatures": signatures,
        "params": params,
    }


def prompt_block(tools: list[Any]) -> str:
    """The declarations a turn author reads: service name, key and signature."""
    decl = declarations(tools)
    lines = [
        "Each tool is a revl service. The service name, the requirement key and "
        "the single operation are exactly:",
        "",
    ]
    for tool in tools:
        name = tool.name
        lines.append(
            f"  requires {requirement_key(name)}: {service_name(name)}  "
            f"| {service_name(name)}.{decl['signatures'][name]}"
        )
    return "\n".join(lines)
