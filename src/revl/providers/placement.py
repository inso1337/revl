"""Which adapter a model crossing may use: decided by the program, checked
against the configuration (issue #1461).

The adapter never chooses. Three things in the PROGRAM decide where a model
crossing goes, and this module checks the configuration against all three
before any adapter is built:

1. **The crossing names its role.** A service operation declared
   `emission[model.<role>]` is placed on `<role>` (`model_route.role_of_crossing`,
   roadmap item 512 slice 4). An operation whose `model.*` token names no
   declared role has no placement, and serving it would mean the adapter layer
   picked one, so it is refused.
2. **The role's residence.** A role declared `on_device` may only be bound to
   an endpoint whose residence is `on_device` (`config.endpoint_residence`: an
   OpenAI-compatible or Ollama server on a loopback host). This is what makes the
   compile-time confidentiality ceiling true at run time: `model_route` refuses
   a `confidential` arm that names an `off_device` role, and this refuses the
   `on_device` role being served from off the device.
3. **The role's reach.** A binding's `reaches` (what the endpoint's model can
   reach on its own) must be covered by the role's declared `reaches [...]`,
   by `cap_order.covers`. An undeclared role reach is `*`, which covers only
   `*`, so a binding that declares any reach against an undeclared role is
   refused.

`check_bindings` returns every refusal rather than stopping at the first, so an
operator fixing a configuration sees the whole list.

WHAT IS NOT CHECKED
-------------------
That the endpoint is what the configuration says. A loopback URL can be a proxy
that forwards to a cloud API; an operator who runs one must say
`residence = "off_device"`. revl sees a URL, not a network path. Roles declared
in a module reached only through `use` are not read by `roles_of_files` (it
parses the root files), so a crossing on such a role is refused as naming no
declared role rather than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import cap_order
from .. import model_council, model_route
from ..errors import RevlError
from ..parser import Parser
from .config import ProviderConfig

CODE = model_route.CODE
DOC = "docs/model-providers.md"


@dataclass(frozen=True)
class Refusal:
    message: str
    hint: str
    role: str | None = None
    code: str = CODE

    def render(self) -> str:
        return f"{self.message} ({self.code})\n  {self.hint}"


@dataclass(frozen=True)
class ModelOp:
    """One operation of a model service a host must serve."""

    key: str
    service: str
    method: str
    capability: str
    role: str | None
    params: tuple
    returns: str | None
    validated: bool
    is_async: bool
    #: the IR's `response_grammar` for a `validated` operation (item 513)
    grammar: dict | None = None
    #: the IR's `response_schema` for a `validated` operation (item 257)
    response_schema: dict | None = None

    @property
    def crossing(self) -> str:
        return f"{self.service}.{self.method}"


@dataclass(frozen=True)
class Placement:
    """The program side of the check: roles and the route table."""

    roles: dict
    routes: dict

    def confidential_routes(self) -> dict:
        """`{role: [(component, action), ...]}` for every arm that places the
        `confidential` origin on the role, directly, as a candidate, or as a
        council member."""
        out: dict = {}
        for component, actions in self.routes.items():
            for action, arms in actions.items():
                placement = (arms or {}).get("confidential")
                if not isinstance(placement, dict):
                    continue
                for role in model_route.reach_of({"confidential": placement}):
                    out.setdefault(role, []).append((component, action))
        return out


def placement_of_program(program) -> Placement:
    councils = model_council.check(program)
    return Placement(model_route.roles(program),
                     model_route.check(program, councils=councils))


def placement_of_files(paths) -> Placement:
    """Roles and routes of the root `.rvl` files, merged. A role declared in
    two root files with different residences is refused."""
    roles: dict = {}
    routes: dict = {}
    for path in paths:
        source = Path(path).read_text(encoding="utf-8")
        found = placement_of_program(Parser(source, str(path)).parse())
        for name, role in found.roles.items():
            prior = roles.get(name)
            if prior is not None and prior.residence != role.residence:
                raise RevlError(
                    str(path), role.line,
                    f"model role `{name}` is declared `{role.residence}` here "
                    f"and `{prior.residence}` in another root file",
                    hint="a role is one declared placement across the "
                         "composition", code=CODE,
                    category=model_route.CATEGORY)
            roles[name] = role
        routes.update(found.routes)
    return Placement(roles, routes)


def _provided_keys(ir) -> set:
    return {key for comp in ir.get("components") or ()
            for key in (comp.get("provides") or {})}


def model_keys(ir) -> dict:
    """`{requires key: service}` for every key a component requires that no
    component in the composition provides and whose service declares at least
    one `model.*` operation. These are the keys a host must serve."""
    services = ir.get("services") or {}
    provided = _provided_keys(ir)
    out: dict = {}
    for comp in ir.get("components") or ():
        for key, service in (comp.get("requires") or {}).items():
            if key in provided or service not in services:
                continue
            methods = (services[service] or {}).get("methods") or {}
            if any(model_route.model_crossing_of(m.get("capabilities"))
                   for m in methods.values()):
                out[key] = service
    return out


def model_operations(ir, roles) -> tuple:
    """Every operation of every model key, with the role its capability names.

    An operation of a model service that is NOT a model crossing is returned
    with `capability == ""`, so the check can refuse it by name: a model host
    cannot serve it."""
    services = ir.get("services") or {}
    ops = []
    for key, service in sorted(model_keys(ir).items()):
        for method, spec in sorted(((services[service] or {}).get("methods")
                                    or {}).items()):
            cap = model_route.model_crossing_of(spec.get("capabilities")) or ""
            ops.append(ModelOp(
                key=key, service=service, method=method, capability=cap,
                role=model_route.role_of_crossing(cap, roles) if cap else None,
                params=tuple((p.get("name"), p.get("type"))
                             for p in spec.get("params") or ()),
                returns=spec.get("returns"),
                validated=bool(spec.get("validated")),
                is_async=bool(spec.get("async")),
                grammar=spec.get("response_grammar"),
                response_schema=spec.get("response_schema"),
            ))
    return tuple(ops)


def _reach_refusal(role, binding) -> Refusal | None:
    try:
        held = [cap_order.parse_stored_cap(t) for t in role.reach_tokens]
        reach = [cap_order.parse_stored_cap(t) for t in binding.reaches]
    except cap_order.CapError as exc:
        return Refusal(
            f"the binding for model role `{role.name}` names a reach that is "
            f"not a capability token: {exc}",
            "write `reaches` as capability tokens, the way a `requires` "
            "clause names them", role.name)
    extra = cap_order.covers_set(held, reach)
    if not extra:
        return None
    declared = (f"declares `reaches [{', '.join(role.reach_tokens)}]`"
                if role.reach_declared else "declares no reach (so `*`)")
    return Refusal(
        f"the endpoint bound to model role `{role.name}` reaches "
        f"{', '.join(f'`{c.to_str()}`' for c in extra)}, which the role "
        f"does not: model role `{role.name}` {declared}",
        f"a binding may not give a role more reach than the program declared "
        f"for it (item 519). Widen the role's `reaches [...]` in the program, "
        f"or bind the role to an endpoint that reaches less. See {DOC}",
        role.name)


def check_bindings(placement: Placement, config: ProviderConfig,
                   ops) -> list:
    """Every reason the configuration may not serve these operations."""
    roles = placement.roles
    refusals: list = []
    confidential = placement.confidential_routes()

    for name, binding in sorted(config.bindings.items()):
        role = roles.get(name)
        if role is None:
            declared = ", ".join(f"`{r}`" for r in sorted(roles)) or "none"
            refusals.append(Refusal(
                f"{config.source} binds model role `{name}`, which the "
                f"program does not declare (declared: {declared})",
                "a binding for an undeclared role is a typo or a stale "
                "configuration; either way the role it meant is unbound. "
                "Rename it to a declared role or remove it", name))
            continue
        if role.residence == "on_device" and binding.residence != "on_device":
            routed = confidential.get(name) or []
            via = ""
            if routed:
                via = (" It receives `confidential` values through " +
                       ", ".join(f"`route model on {a}` in {c}"
                                 for c, a in routed) + ".")
            refusals.append(Refusal(
                f"model role `{name}` is declared `on_device`, and "
                f"{config.source} binds it to {binding.provider} at "
                f"{binding.base_url}, which is `off_device`.{via}",
                f"an on_device role is the placement the program relies on to "
                f"keep a prompt on this machine; serving it from off the "
                f"device would make the compile-time placement false. Bind it "
                f"to an OpenAI-compatible or Ollama server on a loopback address "
                f"(127.0.0.1, ::1, localhost). See {DOC}", name))
        reach = _reach_refusal(role, binding)
        if reach is not None:
            refusals.append(reach)

    for op in ops:
        if not op.capability:
            refusals.append(Refusal(
                f"`{op.crossing}` is an operation of `{op.service}`, which "
                f"the composition requires at `{op.key}` from a model host, "
                f"but it is not a model crossing (no `model.*` capability)",
                "a model host serves completions only. Provide this "
                "operation from a component, or move it to its own service"))
            continue
        if op.role is None:
            refusals.append(Refusal(
                f"`{op.crossing}` declares `{op.capability}`, which names no "
                f"declared model role, so nothing in the program says which "
                f"adapter it may use",
                f"place the crossing on a role: declare `model role <name> "
                f"on_device|off_device` and write `emission[model.<name>]`. "
                f"The adapter layer never picks a placement. See {DOC}"))
            continue
        if config.binding(op.role) is None:
            refusals.append(Refusal(
                f"`{op.crossing}` is placed on model role `{op.role}`, which "
                f"{config.source} does not bind",
                f"add `roles.{op.role}` to the provider configuration. See "
                f"{DOC}", op.role))
        if op.returns not in (None, "Str") and not op.validated:
            refusals.append(Refusal(
                f"`{op.crossing}` returns `{op.returns}` and is not "
                f"`validated`, so a completion would reach the program as a "
                f"`{op.returns}` that nothing checked",
                "declare it `validated` (the response is then checked against "
                "the type on return), or return `Str`", op.role))
    return refusals
