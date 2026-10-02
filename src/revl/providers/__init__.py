"""Runtime model provider adapters (issue #1461).

`model role`, `route model`, councils and `validated` crossings are declared and
checked at compile time. This package is what a model crossing reaches at run
time: an adapter per configured role, speaking one of three wire formats over
the standard library's HTTP client.

* `config`     - the provider configuration file, and what it may not contain;
* `placement`  - the program decides which adapter a crossing may use; this
                 checks the configuration against that decision;
* `host`       - the object a model `requires` key resolves to at run time;
* `adapter`    - one role's client; `wire_openai`, `wire_anthropic` and
                 `wire_gemini` are the three wire formats;
* `transport`  - the one HTTP client, shared with `bench/`.

`revl run --providers FILE` is the entry point (`bind_for_run`). The design and
the per-provider pages are `docs/model-providers.md` and the three pages it
links.
"""

from __future__ import annotations

import os

from .adapter import Adapter, read_credential
from .completion import Completion, CompletionRequest
from .config import (Binding, ProviderConfig, ProviderConfigError,
                     endpoint_residence, load_config, parse_config)
from .host import ModelHost, PlacementRefused, build_hosts, missing_credentials
from .placement import (Placement, Refusal, check_bindings, model_keys,
                        model_operations, placement_of_files,
                        placement_of_program)
from .transport import ProviderError, redact, request_json

__all__ = [
    "Adapter", "Binding", "Completion", "CompletionRequest", "ModelHost",
    "Placement", "PlacementRefused", "ProviderConfig", "ProviderConfigError",
    "ProviderError", "Refusal", "bind_for_run", "build_hosts",
    "check_bindings", "describe_hosts", "endpoint_residence", "load_config",
    "missing_credentials", "model_keys", "model_operations", "parse_config",
    "placement_of_files", "placement_of_program", "read_credential", "redact",
    "request_json",
]


def bind_for_run(ir, files, config_path, environ=None) -> dict:
    """The model hosts `revl run --providers FILE` provides, checked.

    Refuses (raises) on a malformed file, on any placement refusal, and on a
    credential variable that is not set, so a run whose model crossings cannot
    be served does not boot. The check needs no runtime and makes no request.
    """
    config = load_config(config_path)
    placement = placement_of_files(files)
    # `environ` is passed through as given: None makes every adapter read
    # os.environ at request time, rather than hold a reference to it
    hosts = build_hosts(ir, placement, config, environ=environ)
    used = {op.role for host in hosts.values()
            for op, _ in host._revl_routes.values()}
    missing = missing_credentials(
        config, os.environ if environ is None else environ, used)
    if missing:
        raise ProviderError(
            "the provider configuration names credential variable(s) that are "
            "not set: " + ", ".join(f"`{env}` (role `{role}`)"
                                    for role, env in missing))
    return hosts


def describe_hosts(hosts) -> list:
    """One line per bound operation, for `revl run --plan`. Names the
    credential VARIABLE, never its value."""
    lines = []
    for key, host in sorted(hosts.items()):
        for method, (op, adapter) in sorted(host._revl_routes.items()):
            b = adapter.binding
            cred = (f", credential from ${b.api_key_env}" if b.api_key_env
                    else "")
            lines.append(f"  {key}.{method} -> role {op.role}: {b.provider} "
                         f"{b.model} at {b.base_url} ({b.residence}{cred})")
            if op.validated:
                lines.append("    " + structured_line(op, b))
    return lines


def structured_line(op, binding) -> str:
    """How a `validated` operation's grammar reaches its provider, and every
    way the provider's representation falls short of the revl type."""
    from ..decode_grammar import json_schema_grammar_for  # noqa: PLC0415
    from .structured import CLAIMING, representation  # noqa: PLC0415
    mode = binding.structured_output
    if mode == "none":
        return ("structured output: none (the completion is validated on "
                "return only)")
    spec = op.response_schema
    wire = json_schema_grammar_for(spec)["schema"] if spec else None
    gaps = representation(binding.provider, mode,
                          wire if mode != "gbnf" else None)
    claim = "claims the decode" if mode in CLAIMING else "no claim"
    verdict = "exact" if not gaps else "approximates: " + "; ".join(gaps)
    return f"structured output: {mode} ({claim}), {verdict}"
