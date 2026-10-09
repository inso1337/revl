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

A MANAGED adapter (`provider = "ollama"`, roadmap item 515 slice S2) also
loads and unloads its model. It does not decide when or where: the role's
provision (`revl.providers.provision`) calls `load` once, on the device the
model schedule chose, and `unload` once, when its last consumer releases it.
Until it is loaded, a managed adapter refuses to complete, because a
completion sent to an unloaded model makes the server load it wherever it
likes, which is the placement decision the schedule exists to take.
"""

from __future__ import annotations

import os
import time
from dataclasses import replace

from .completion import Completion, CompletionRequest
from .config import WIRES as _WIRES
from .config import Binding
from .transport import ProviderError, redact, request_json


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
        # managed only: the device the model is loaded on, and its options
        self._device: str | None = None
        self._device_options: dict = {}

    @property
    def label(self) -> str:
        return f"{self.binding.provider} adapter for role `{self.binding.role}`"

    def __repr__(self) -> str:
        b = self.binding
        return (f"Adapter(role={b.role!r}, provider={b.provider!r}, "
                f"model={b.model!r}, endpoint={b.base_url!r}, "
                f"residence={b.residence!r}, credential_env={b.api_key_env!r})")

    @property
    def managed(self) -> bool:
        return self.binding.managed

    @property
    def loaded_on(self) -> str | None:
        """The device the model is loaded on, or None (managed only)."""
        return self._device

    def _credential(self):
        b = self.binding
        credential = (read_credential(b.api_key_env, self._lookup)
                      if b.api_key_env else None)
        return credential, ((credential,) if credential else ())

    def load(self, device: str) -> dict:
        """Load the model on `device` and return the server's reply. The
        provision checks `device` is the scheduled one before calling."""
        if not self.managed:
            raise ProviderError(f"{self.label}: this endpoint manages its own "
                                f"residency; revl loads nothing through it")
        options = self.binding.device_options(device)
        if options is None:
            raise ProviderError(
                f"{self.label}: the binding names no load options for device "
                f"`{device}` (it names {', '.join(self.binding.device_names())})")
        credential, secrets = self._credential()
        url, headers, body = self._wire.load_request(self.binding, credential,
                                                     options)
        started = time.monotonic()
        raw = request_json(url, body=body, headers=headers,
                           timeout=self.binding.timeout, secrets=secrets,
                           label=self.label)
        self._device, self._device_options = device, options
        return {**raw, "revl_load_seconds": time.monotonic() - started}

    def unload(self) -> dict:
        """Take the model out of memory."""
        credential, secrets = self._credential()
        url, headers, body = self._wire.unload_request(self.binding,
                                                       credential)
        self._device, self._device_options = None, {}
        return request_json(url, body=body, headers=headers,
                            timeout=self.binding.timeout, secrets=secrets,
                            label=self.label)

    def residency(self) -> dict | None:
        """What the server reports holding for this model, or None when it
        holds nothing. Asked of the server, never remembered."""
        return self.resident_in(self.residency_report())

    def residency_report(self, timeout: float | None = None) -> dict:
        """The server's whole answer to "what do you hold", undecoded.
        `timeout` overrides the binding's completion timeout, which a caller
        that only asks a question (the plan-time read) caps."""
        credential, secrets = self._credential()
        url, headers = self._wire.residency_request(self.binding, credential)
        return request_json(
            url, headers=headers,
            timeout=self.binding.timeout if timeout is None else timeout,
            secrets=secrets, label=self.label)

    def resident_in(self, raw: dict) -> dict | None:
        """This binding's model's entry in a `residency_report()`, or None."""
        return self._wire.resident_entry(raw, self.binding.model)

    def complete(self, request: CompletionRequest) -> Completion:
        b = self.binding
        credential, secrets = self._credential()
        if self.managed:
            if self._device is None:
                raise ProviderError(
                    f"{self.label}: the model is not loaded, and a completion "
                    f"sent to an unloaded model makes the server load it on a "
                    f"device it picks. Its provision loads it on the device "
                    f"the model schedule chose")
            url, headers, body = self._wire.build(
                b, request, credential, self._device_options)
        else:
            url, headers, body = self._wire.build(b, request, credential)
        started = time.monotonic()
        raw = request_json(url, body=body, headers=headers, timeout=b.timeout,
                           secrets=secrets, label=self.label)
        try:
            completion = self._wire.parse(raw, request)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            snippet = redact(str(raw)[:400], secrets)
            raise ProviderError(
                redact(f"{self.label}: unexpected response shape from {url} "
                       f"({type(exc).__name__}: {exc}); body={snippet}",
                       secrets)) from None
        return replace(completion, provider=b.provider,
                       model=completion.model or b.model,
                       latency_seconds=time.monotonic() - started)
