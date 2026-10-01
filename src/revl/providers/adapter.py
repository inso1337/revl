"""The adapter: one bound role, one wire format, one request (issue #1461).

An `Adapter` is what a `model role` is bound to at run time. It turns a
`CompletionRequest` into one HTTP request in its provider's wire format, sends
it through `revl.providers.transport`, and turns the reply into a
`Completion`. It does not decide WHICH role a crossing uses: that is the
program's `route model` table and the crossing's `model.<role>` capability,
checked by `revl.providers.placement` before an adapter is ever handed a call.

The credential is read from the environment at request time, on every
request, and is held only in the local variables of `complete`. No object in
this package stores it, so no `repr`, trace or manifest that serializes one of
them can contain it.
"""

from __future__ import annotations

import os
import time
from dataclasses import replace

from . import wire_anthropic, wire_gemini, wire_openai
from .completion import Completion, CompletionRequest
from .config import Binding
from .transport import ProviderError, redact, request_json

_WIRES = {
    "openai-compatible": wire_openai,
    "anthropic": wire_anthropic,
    "gemini": wire_gemini,
}


def read_credential(env_var: str, environ=None) -> str:
    """The credential in `env_var`, read now. The error names the variable and
    never a value. `environ` is a mapping, a lookup function, or None for
    `os.environ`."""
    if callable(environ):
        value = environ(env_var)
    else:
        value = (os.environ if environ is None else environ).get(env_var)
    if not value:
        raise ProviderError(
            f"the environment variable `{env_var}` is not set (or is empty); "
            f"it is where this role's credential is read from")
    return value


class Adapter:
    """One configured role's client."""

    def __init__(self, binding: Binding, *, environ=None) -> None:
        if binding.provider not in _WIRES:
            raise ValueError(f"no wire format for provider {binding.provider}")
        self.binding = binding
        self._wire = _WIRES[binding.provider]
        # A lookup into the environment, run at request time. It is a closure
        # rather than the mapping itself so that nothing reachable from the
        # adapter's attributes renders the environment: `vars(adapter)` shows a
        # function, not a dict of every variable the process holds.
        def lookup(name, _environ=environ):
            return (os.environ if _environ is None else _environ).get(name)
        self._lookup = lookup

    @property
    def label(self) -> str:
        return f"{self.binding.provider} adapter for role `{self.binding.role}`"

    def __repr__(self) -> str:
        b = self.binding
        return (f"Adapter(role={b.role!r}, provider={b.provider!r}, "
                f"model={b.model!r}, endpoint={b.base_url!r}, "
                f"residence={b.residence!r}, credential_env={b.api_key_env!r})")

    def complete(self, request: CompletionRequest) -> Completion:
        b = self.binding
        credential = (read_credential(b.api_key_env, self._lookup)
                      if b.api_key_env else None)
        secrets = (credential,) if credential else ()
        url, headers, body = self._wire.build(b, request, credential)
        started = time.monotonic()
        raw = request_json(url, body=body, headers=headers, timeout=b.timeout,
                           secrets=secrets, label=self.label)
        try:
            completion = self._wire.parse(raw)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            snippet = redact(str(raw)[:400], secrets)
            raise ProviderError(
                redact(f"{self.label}: unexpected response shape from {url} "
                       f"({type(exc).__name__}: {exc}); body={snippet}",
                       secrets)) from None
        return replace(completion, provider=b.provider,
                       model=completion.model or b.model,
                       latency_seconds=time.monotonic() - started)
