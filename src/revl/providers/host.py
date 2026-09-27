"""The model host: the object a composition's model `requires` key resolves to
(issue #1461).

A provider "is the host object bound to the crossing's require key"
(`docs/design/542-grammar-constrained-decoding.md` section 9.3). For every key
a component requires, that no component provides, and whose service declares
`model.*` operations, `build_hosts` returns one such object. It has one method
per operation, and each method is wired, once, at build time, to the adapter
of the role that operation's `model.<role>` capability names. A call cannot
reach any other adapter: there is no fallback and no runtime choice.

`build_hosts` runs `placement.check_bindings` first and raises
`PlacementRefused` with every refusal, so no host exists for a configuration
the program's placement forbids, however the host was constructed.

HOW ARGUMENTS BECOME A PROMPT
-----------------------------
A parameter named `system` of type `Str` is the system prompt. The remaining
parameters are the user turn: one `Str` parameter is sent as it is, and
anything else is sent as a JSON object of parameter name to value.

HOW A COMPLETION BECOMES A VALUE
--------------------------------
An operation returning `Str` gets the answer text. A `validated` operation
returning any other type gets the answer decoded as JSON, which item 257's
seam then checks against the declared type; text that is not JSON is returned
as text, so the seam refuses it as the wrong shape (and retries under a
declared `retry N`). Nothing here strips code fences or otherwise helps a
reply look valid: that judgement belongs to the validator.
"""

from __future__ import annotations

import asyncio
import json

from .adapter import Adapter
from .completion import Completion, CompletionRequest
from .placement import check_bindings, model_operations
from .transport import ProviderError


class PlacementRefused(Exception):
    """The configuration may not serve the program's model crossings."""

    def __init__(self, refusals) -> None:
        self.refusals = tuple(refusals)
        lines = [f"the provider configuration is refused "
                 f"({len(self.refusals)} problem(s)):"]
        lines += [r.render() for r in self.refusals]
        super().__init__("\n".join(lines))


def _json(value):
    try:
        return json.dumps(value, sort_keys=False, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ProviderError(f"a model argument is not JSON-representable: "
                            f"{exc}") from None


def request_for(op, args) -> CompletionRequest:
    """The single-turn request an operation's arguments make."""
    named = dict(zip([name for name, _ in op.params], args))
    system = None
    types = dict(op.params)
    if types.get("system") == "Str" and "system" in named:
        system = named.pop("system")
    if len(named) == 1:
        (value,) = named.values()
        prompt = value if isinstance(value, str) else _json(value)
    else:
        prompt = _json(named)
    return CompletionRequest(prompt=prompt, system=system)


def value_of(op, completion: Completion):
    """The value a completion becomes, by the operation's declared return."""
    if op.returns in (None, "Str"):
        return completion.text
    try:
        return json.loads(completion.text)
    except (json.JSONDecodeError, TypeError):
        return completion.text


class ModelHost:
    """Base class of the per-key host types `build_hosts` makes. Attribute
    names start with `_revl_` so no revl operation name can shadow one."""

    def __init__(self, key: str, service: str, routes: dict) -> None:
        self._revl_key = key
        self._revl_service = service
        # {method: (op, adapter)}, fixed at build time
        self._revl_routes = routes
        self._revl_last = None

    @property
    def last_completion(self) -> Completion | None:
        """The most recent completion this host returned. Carries usage and
        latency; never a credential."""
        return self._revl_last

    def _revl_serve(self, method: str, args: tuple):
        op, adapter = self._revl_routes[method]
        completion = adapter.complete(request_for(op, args))
        self._revl_last = completion
        return value_of(op, completion)

    def __repr__(self) -> str:
        roles = ", ".join(f"{m}->{op.role}"
                          for m, (op, _) in sorted(self._revl_routes.items()))
        return (f"ModelHost(key={self._revl_key!r}, "
                f"service={self._revl_service!r}, {roles})")


def _method(name: str, is_async: bool):
    if is_async:
        async def call(self, *args):
            return await asyncio.to_thread(self._revl_serve, name, args)
    else:
        def call(self, *args):
            return self._revl_serve(name, args)
    call.__name__ = name
    return call


def build_hosts(ir, placement, config, *, environ=None) -> dict:
    """`{requires key: host}` for every model key of the composition.

    Raises `PlacementRefused` when the configuration may not serve it.
    `environ` is the mapping credentials are read from at request time
    (`os.environ` when None); it is consulted per request, never copied.
    """
    ops = model_operations(ir, placement.roles)
    refusals = check_bindings(placement, config, ops)
    if refusals:
        raise PlacementRefused(refusals)
    adapters: dict = {}
    by_key: dict = {}
    for op in ops:
        if op.role not in adapters:
            adapters[op.role] = Adapter(config.binding(op.role),
                                        environ=environ)
        by_key.setdefault((op.key, op.service), {})[op.method] = (
            op, adapters[op.role])
    hosts = {}
    for (key, service), routes in by_key.items():
        methods = {name: _method(name, op.is_async)
                   for name, (op, _) in routes.items()}
        cls = type(f"ModelHost_{service}", (ModelHost,), methods)
        hosts[key] = cls(key, service, routes)
    return hosts


def missing_credentials(config, environ, roles) -> list:
    """The credential variables the named roles need and `environ` lacks."""
    out = []
    for role in sorted(roles):
        binding = config.binding(role)
        if binding and binding.api_key_env \
                and not environ.get(binding.api_key_env):
            out.append((role, binding.api_key_env))
    return out
