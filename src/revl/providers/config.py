"""Provider configuration: which adapter serves which `model role` (issue #1461).

A `model role` in a revl document is a name and a residence, and nothing else:
"a role is a name bound to a member by configuration. No vendor, no weights
hash, no endpoint appears in a revl document" (`docs/design/531-model-placement.md`
section 5). This module reads that configuration.

    {
      "roles": {
        "local": {"provider": "openai-compatible",
                  "base_url": "http://127.0.0.1:11434/v1",
                  "model": "qwen3:8b"},
        "cloud": {"provider": "anthropic",
                  "model": "claude-sonnet-4-5",
                  "api_key_env": "ANTHROPIC_API_KEY"}
      }
    }

JSON, or TOML with the same shape (`[roles.local]`).

A role bound to `provider = "ollama"` is LOADED and UNLOADED by revl (roadmap
item 515, slice S2): its `devices` table names, per device of the placement's
host, the load options that put the model on that device, and the provision
loads it on the one device the model schedule chose (`revl.providers.provision`).

    [roles.small]
    provider = "ollama"
    base_url = "http://127.0.0.1:11434"
    model = "qwen3:0.6b"
    devices = { cpu0 = { num_gpu = 0 }, gpu0 = {} }

WHAT IS REFUSED HERE, AND WHY
-----------------------------
Everything unknown is refused rather than ignored, because a field that is
silently dropped is a setting the operator believes is in force.

* **A credential in the file.** A field named like one (`api_key`, `token`,
  `authorization`, ...) is refused by name, and the message never repeats its
  value. Credentials are read from the environment variable `api_key_env`
  names, at request time, and live nowhere else.
* **A credential where the variable NAME goes.** `api_key_env` must be an
  environment variable name. A pasted key is not one (it has a `-`, or starts
  with a digit), so it is refused, again without echoing it.
* **A credential in the URL.** `https://user:key@host` and a query string
  (`?key=...`) are both refused: a URL is printed in errors and logs, and a key
  inside one would travel with it.
* **An `on_device` claim the endpoint contradicts.** Only an OpenAI-compatible
  or Ollama endpoint on a loopback address can be `on_device`. A hosted API is always
  `off_device`, whatever its `base_url` says, and a non-loopback host is
  always `off_device`. The operator may DOWNGRADE a loopback endpoint to
  `off_device` (a local proxy that forwards to a cloud API); the reverse is
  refused.
* **A credential over plain HTTP to another machine.** `http://` is accepted
  for a loopback host only when a credential is configured.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import wire_anthropic, wire_gemini, wire_ollama, wire_openai

#: `{provider: wire module}`, in the order a refusal lists them. Each wire
#: module names its own `PROVIDER` and `FIELDS`, so this is the one place the
#: closed vocabulary of formats is assembled; `PROVIDERS`, the per-provider
#: field table and the adapter's dispatch all read it.
WIRES = {w.PROVIDER: w for w in (wire_openai, wire_anthropic, wire_gemini,
                                 wire_ollama)}

#: The wire formats this package speaks. CLOSED, so a typo is a refusal.
PROVIDERS = tuple(WIRES)

#: The providers whose endpoint may be on this device (a loopback server).
LOCAL_PROVIDERS = ("openai-compatible", "ollama")

#: Gemini's two front doors.
GEMINI_APIS = ("google-ai", "vertex")

RESIDENCES = ("on_device", "off_device")

_COMMON_FIELDS = frozenset({
    "provider", "model", "base_url", "api_key_env", "max_tokens",
    "temperature", "timeout", "residence", "reaches",
})
_PROVIDER_FIELDS = {provider: wire.FIELDS for provider, wire in WIRES.items()}

#: Field names that can only mean "the credential itself". Refused by name.
_CREDENTIAL_FIELDS = frozenset({
    "api_key", "apikey", "key", "token", "access_token", "bearer",
    "authorization", "password", "secret", "credential", "credentials",
})

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_GEMINI_MODEL = re.compile(r"[A-Za-z0-9._-]+\Z")
_GCP_NAME = re.compile(r"[a-z0-9-]+\Z")

DEFAULT_MAX_TOKENS = 1024
DEFAULT_TIMEOUT = 120.0
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"

_DEFAULT_BASE = {
    "anthropic": "https://api.anthropic.com",
    ("gemini", "google-ai"): "https://generativelanguage.googleapis.com/v1beta",
}


class ProviderConfigError(ValueError):
    """The provider configuration is malformed. The message names the field
    and never quotes a value that could be a credential."""


@dataclass(frozen=True)
class Binding:
    """One role's adapter, as configured. Holds the NAME of the credential's
    environment variable and never the credential."""

    role: str
    provider: str
    model: str
    base_url: str
    api_key_env: str | None = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = 0.0
    timeout: float = DEFAULT_TIMEOUT
    #: the EFFECTIVE residence: derived from the endpoint, or the operator's
    #: downgrade of it. Never wider than what the endpoint allows.
    residence: str = "off_device"
    #: what the endpoint's model can reach on its own (provider-side tools),
    #: as capability tokens. Empty for a plain completion endpoint.
    reaches: tuple = ()
    api: str | None = None
    project: str | None = None
    location: str | None = None
    anthropic_version: str = DEFAULT_ANTHROPIC_VERSION
    #: `ollama` only: `((device name, ((option, value), ...)), ...)`, the load
    #: options per device the provision may load this role on. Empty for
    #: every other provider, whose endpoint manages its own residency.
    devices: tuple = ()

    @property
    def endpoint(self) -> str:
        return self.base_url

    @property
    def managed(self) -> bool:
        """Whether revl loads and unloads this role's model (slice S2)."""
        return self.provider == "ollama"

    def device_names(self) -> tuple:
        return tuple(name for name, _ in self.devices)

    def device_options(self, device: str) -> dict | None:
        """The load options for `device`, or None when the binding does not
        name it (and so cannot load the model there)."""
        for name, options in self.devices:
            if name == device:
                return dict(options)
        return None


@dataclass(frozen=True)
class ProviderConfig:
    bindings: dict = field(default_factory=dict)
    source: str = "<config>"

    def binding(self, role: str) -> Binding | None:
        return self.bindings.get(role)


def is_loopback(host: str | None) -> bool:
    """Whether `host` names this machine. `localhost` and the loopback ranges
    only: a LAN address is another machine, so a prompt sent there has left
    the device."""
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def endpoint_residence(provider: str, base_url: str) -> str:
    """Where a call to this endpoint runs, as far as revl can tell from the
    configuration alone.

    `on_device` only for an OpenAI-compatible or Ollama server on a loopback
    host. The
    Anthropic and Gemini wire formats are hosted APIs, so they are
    `off_device` even when `base_url` points at a loopback proxy: the proxy's
    upstream is the provider, and the prompt leaves the device through it.
    """
    if provider not in LOCAL_PROVIDERS:
        return "off_device"
    return "on_device" if is_loopback(urlsplit(base_url).hostname) \
        else "off_device"


def _refuse(where: str, message: str) -> ProviderConfigError:
    return ProviderConfigError(f"{where}: {message}")


def _check_url(where: str, url: str, credential: bool) -> str:
    if not isinstance(url, str) or not url:
        raise _refuse(where, "`base_url` must be a non-empty URL string")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise _refuse(where, "`base_url` must be an http:// or https:// URL "
                             "with a host")
    if parts.username is not None or parts.password is not None:
        raise _refuse(where, "`base_url` carries user info. Credentials are "
                             "read from the variable `api_key_env` names, "
                             "never from the URL, because a URL is printed in "
                             "errors")
    if parts.query or parts.fragment:
        raise _refuse(where, "`base_url` carries a query string or fragment. "
                             "A key passed as `?key=...` would travel with "
                             "every error that prints the URL; name the "
                             "variable in `api_key_env` instead")
    if credential and parts.scheme == "http" \
            and not is_loopback(parts.hostname):
        raise _refuse(where, "a credential is configured and `base_url` is "
                             "plain http:// to a host that is not this "
                             "machine; the key would cross the network in "
                             "clear. Use https://")
    return url.rstrip("/")


def _number(where: str, name: str, value, kind, minimum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _refuse(where, f"`{name}` must be a number")
    if kind is int and not isinstance(value, int):
        raise _refuse(where, f"`{name}` must be an integer")
    if value < minimum:
        raise _refuse(where, f"`{name}` must be at least {minimum}")
    return kind(value)


def _binding(role: str, entry, source: str) -> Binding:
    where = f"{source}: role `{role}`"
    if not isinstance(entry, dict):
        raise _refuse(where, "must be a table of fields")
    for name in entry:
        if str(name).lower() in _CREDENTIAL_FIELDS:
            raise _refuse(where, f"field `{name}` looks like a credential, and "
                                 f"a credential is never read from this file. "
                                 f"Put it in an environment variable and name "
                                 f"that variable in `api_key_env`")
    provider = entry.get("provider")
    if provider not in PROVIDERS:
        raise _refuse(where, f"`provider` must be one of "
                             f"{', '.join(PROVIDERS)}")
    unknown = sorted(set(entry) - _COMMON_FIELDS - _PROVIDER_FIELDS[provider])
    if unknown:
        raise _refuse(where, f"unknown field(s) {', '.join(unknown)} for "
                             f"provider {provider}; an ignored field would be a "
                             f"setting you believe is in force")
    model = entry.get("model")
    if not isinstance(model, str) or not model:
        raise _refuse(where, "`model` must be a non-empty string")

    env = entry.get("api_key_env")
    if env is not None and (not isinstance(env, str)
                            or not _ENV_NAME.match(env)):
        # never echo the value: the commonest way to get here is pasting the
        # key itself where its variable's name goes
        raise _refuse(where, "`api_key_env` must be the NAME of an environment "
                             "variable (letters, digits and `_`); the value "
                             "given is not one, and it is not repeated here in "
                             "case it is the credential itself")
    if provider in ("anthropic", "gemini") and env is None:
        raise _refuse(where, f"provider {provider} is a hosted API and needs "
                             f"`api_key_env`, the name of the environment "
                             f"variable holding its credential")

    api = project = location = None
    if provider == "gemini":
        api = entry.get("api", "google-ai")
        if api not in GEMINI_APIS:
            raise _refuse(where, f"`api` must be one of {', '.join(GEMINI_APIS)}")
        if not _GEMINI_MODEL.match(model.removeprefix("models/")):
            raise _refuse(where, "a Gemini `model` goes into the request path, "
                                 "so it may hold only letters, digits, `.`, "
                                 "`_` and `-`")
        if api == "vertex":
            project, location = entry.get("project"), entry.get("location")
            for name, value in (("project", project), ("location", location)):
                if not isinstance(value, str) or not _GCP_NAME.match(value):
                    raise _refuse(where, f"Vertex needs `{name}`, lower-case "
                                         f"letters, digits and `-`")
        elif "project" in entry or "location" in entry:
            raise _refuse(where, "`project` and `location` are Vertex fields; "
                                 "set `api = \"vertex\"` or drop them")

    base = entry.get("base_url")
    if base is None:
        if provider == "ollama":
            raise _refuse(where, "`base_url` is required for an Ollama server "
                                 "(its root, for example "
                                 "http://127.0.0.1:11434)")
        if provider == "openai-compatible":
            raise _refuse(where, "`base_url` is required for an "
                                 "OpenAI-compatible endpoint (for example "
                                 "http://127.0.0.1:11434/v1)")
        if provider == "gemini" and api == "vertex":
            base = f"https://{location}-aiplatform.googleapis.com/v1"
        else:
            base = _DEFAULT_BASE[provider if provider == "anthropic"
                                 else (provider, api)]
    base = _check_url(where, base, credential=env is not None)

    derived = endpoint_residence(provider, base)
    residence = entry.get("residence", derived)
    if residence not in RESIDENCES:
        raise _refuse(where, f"`residence` must be one of "
                             f"{', '.join(RESIDENCES)}")
    if residence == "on_device" and derived == "off_device":
        reason = ("a hosted API" if provider not in LOCAL_PROVIDERS
                  else "not on a loopback address")
        raise _refuse(where, f"`residence` says on_device, but {base} is "
                             f"{reason}, so a prompt sent there leaves the "
                             f"device. The residence of an endpoint can be "
                             f"narrowed by configuration, never widened")

    reaches = entry.get("reaches", [])
    if not isinstance(reaches, list) or not all(
            isinstance(t, str) and t for t in reaches):
        raise _refuse(where, "`reaches` must be a list of capability tokens")

    return Binding(
        role=role, provider=provider, model=model, base_url=base,
        api_key_env=env,
        max_tokens=_number(where, "max_tokens",
                           entry.get("max_tokens", DEFAULT_MAX_TOKENS), int, 1),
        temperature=_number(where, "temperature",
                            entry.get("temperature", 0.0), float, 0.0),
        timeout=_number(where, "timeout",
                        entry.get("timeout", DEFAULT_TIMEOUT), float, 0.001),
        residence=residence, reaches=tuple(reaches), api=api,
        project=project, location=location,
        anthropic_version=entry.get("anthropic_version",
                                    DEFAULT_ANTHROPIC_VERSION),
        devices=(_devices(where, entry.get("devices"))
                 if provider == "ollama" else ()),
    )


def _devices(where: str, table) -> tuple:
    """An `ollama` binding's `devices`: a non-empty table of device name to
    load options. Refused rather than defaulted: a binding with no device
    table has no device it can be loaded on, and loading it where the server
    likes is the "any free device" answer the model schedule exists to
    refuse."""
    allowed = wire_ollama.DEVICE_OPTIONS
    if not isinstance(table, dict) or not table:
        raise _refuse(where, "`devices` is required for provider ollama: a "
                             "table of the placement's device names (for "
                             "example `cpu0`, `gpu0`) to the load options that "
                             "put the model on each. revl loads the model only "
                             "on the device the model schedule chose, so a "
                             "binding with no devices can be loaded nowhere")
    out = []
    for name, options in table.items():
        if not isinstance(name, str) or not _ENV_NAME.match(name):
            raise _refuse(where, "a `devices` name must be a device name of "
                                 "the placement (letters, digits and `_`)")
        if not isinstance(options, dict):
            raise _refuse(where, f"`devices.{name}` must be a table of load "
                                 f"options ({', '.join(allowed)})")
        unknown = sorted(set(options) - set(allowed))
        if unknown:
            raise _refuse(where, f"`devices.{name}` has unknown option(s) "
                                 f"{', '.join(unknown)}; the load options are "
                                 f"{', '.join(allowed)}")
        for option, value in options.items():
            _number(where, f"devices.{name}.{option}", value, int, 0)
        out.append((name, tuple(sorted(options.items()))))
    return tuple(out)


def parse_config(data, source: str = "<config>") -> ProviderConfig:
    """Validate a decoded configuration document."""
    if not isinstance(data, dict):
        raise _refuse(source, "the provider configuration must be a table")
    unknown = sorted(set(data) - {"roles"})
    if unknown:
        raise _refuse(source, f"unknown top-level field(s) "
                              f"{', '.join(unknown)}; the file holds `roles`")
    roles = data.get("roles")
    if not isinstance(roles, dict) or not roles:
        raise _refuse(source, "`roles` must be a non-empty table of role name "
                              "to adapter")
    return ProviderConfig(
        {str(name): _binding(str(name), entry, source)
         for name, entry in roles.items()},
        source)


def load_config(path) -> ProviderConfig:
    """Read a JSON or TOML provider configuration file."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".toml":
        import tomllib  # noqa: PLC0415 - stdlib, only needed for TOML
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise _refuse(str(path), f"not valid TOML: {exc}") from None
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _refuse(str(path), f"not valid JSON: {exc}") from None
    return parse_config(data, str(path))
