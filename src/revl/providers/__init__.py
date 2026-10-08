"""Runtime model provider adapters (issue #1461).

`model role`, `route model`, councils and `validated` crossings are declared and
checked at compile time. This package is what a model crossing reaches at run
time: an adapter per configured role, speaking one of three wire formats over
the standard library's HTTP client.

* `config`     - the provider configuration file, and what it may not contain;
* `placement`  - the program decides which adapter a crossing may use; this
                 checks the configuration against that decision;
* `host`       - the object a model `requires` key resolves to at run time;
* `adapter`    - one role's client; `wire_openai`, `wire_anthropic`,
                 `wire_gemini` and `wire_ollama` are the wire formats;
* `provision`  - a managed role's one load and one unload, shared by every
                 host that routes to it, on the device the model schedule
                 chose (item 515, slice S2);
* `transport`  - the one HTTP client, shared with `bench/`.

`revl run --providers FILE` is the entry point (`bind_for_run`). The design and
the per-provider pages are `docs/model-providers.md` and the three pages it
links.
"""

from __future__ import annotations

import os

from ..errors import RevlError

from .adapter import Adapter, read_credential
from .completion import Completion, CompletionRequest
from .config import (Binding, ProviderConfig, ProviderConfigError,
                     endpoint_residence, load_config, parse_config)
from .host import (ModelHost, PlacementRefused, build_hosts, close_hosts,
                   has_provisions, missing_credentials, open_hosts,
                   provision_residue, provision_summaries)
from .placement import (Placement, Refusal, check_bindings, model_keys,
                        model_operations, placement_of_files,
                        placement_of_program)
from .provision import (ProvisionRefused, Provisions, RoleProvision,
                        gpu_share)
from .transport import ProviderError, redact, request_json

__all__ = [
    "Adapter", "Binding", "Completion", "CompletionRequest", "ModelHost",
    "Placement", "PlacementRefused", "ProviderConfig", "ProviderConfigError",
    "ProviderError", "ProvisionRefused", "Provisions", "Refusal",
    "RoleProvision", "bind_for_run", "build_hosts", "check_bindings",
    "close_hosts", "describe_hosts", "endpoint_residence", "has_provisions",
    "load_config",
    "missing_credentials", "model_keys", "model_operations", "open_hosts",
    "parse_config", "placement_of_files", "placement_of_program",
    "plan_time_residency",
    "provision_residue", "provision_summaries", "read_credential",
    "rebind_problem", "redact",
    "request_json",
]


def bind_for_run(ir, files, config_path, environ=None) -> dict:
    """The model hosts `revl run --providers FILE` provides, checked.

    Refuses (raises) on a malformed file, on any placement refusal, and on a
    credential variable that is not set, so a run whose model crossings cannot
    be served does not boot. The check needs no runtime and makes no request:
    a managed role is loaded later, by `open_hosts`, when the hosts are
    provided.
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


def plan_time_residency(config, wanted: dict) -> dict:
    """`{host: {role: device}}`: what each host already holds, asked of the
    servers at plan time (item 515 / issue #1189 - the acquisition half of
    §11.6 item 1).

    `wanted` is `{host: (role, ...)}` from
    `revl.model_schedule.residency_candidates()`: the candidate roles of the
    arms that wrote `prefer resident`, per host. Only those roles are asked
    about, each through its own binding, so a placement that ranks nothing
    makes no request and a host that ranks nothing is not asked. A role whose
    binding is not managed (or not configured) is skipped: revl does not load
    it, so what a server holds for it is not something this placement can
    reuse.

    The device is the CLASS the server's own report implies, read the way
    `provision.gpu_share` reads the server's own accounting: a member the
    server reports holding entirely in GPU memory is on a `gpu`, one it
    reports holding none of there is on a `cpu`. The class is what a
    ranking reads (a residency is keyed by role and only its keys are
    compared, `model_schedule._Search.order`), and it is also carried into
    the host's spec, so a member whose share the server does not report is
    left OUT rather than guessed at: a guess would be re-derived as a fact by
    the child that verifies the spec.

    Raises `ProviderError` naming the host and the server when a server
    cannot be asked. A host whose arm wrote `prefer resident` is planned
    against what it holds, so a plan that cannot see that is refused rather
    than decided on an assumption.
    """
    held: dict = {}
    for host, roles in sorted(wanted.items()):
        for role in roles:
            binding = config.binding(role)
            if binding is None or not binding.managed:
                continue
            try:
                entry = Adapter(binding).residency()
            except (ProviderError, OSError) as exc:
                raise ProviderError(
                    f"host `{host}`: cannot ask {binding.base_url} what it "
                    f"holds for model role `{role}`. The arm placed here "
                    f"wrote `prefer resident`, so the placement is ranked "
                    f"against what the host holds and cannot be decided "
                    f"without it; start the server, or drop the clause from "
                    f"that arm ({exc})") from None
            if entry is None:
                continue
            share = gpu_share(entry)
            if share == 100:
                held.setdefault(host, {})[role] = "gpu"
            elif share == 0:
                held.setdefault(host, {})[role] = "cpu"
    return held


def rebind_problem(ir, files, config_path, hosts) -> str | None:
    """Why the model hosts bound at boot cannot serve an edited composition,
    or None when they can (issue #1569).

    A `revl run --watch` reload keeps the model hosts, and any member a
    provision loaded, instead of rebuilding them. So an edit is accepted only
    when the configuration, read again and checked against the edited
    program, yields exactly the hosts already bound: the same keys, services,
    operations, roles and bindings. Anything else is refused and the running
    generation is left as it was, because a host checked against the old
    program would otherwise serve the new one unchecked.
    """
    try:
        fresh = bind_for_run(ir, files, config_path)
    except (ProviderConfigError, PlacementRefused, ProviderError, RevlError,
            OSError) as exc:
        return (f"the provider configuration does not admit the edited "
                f"composition: {exc}")
    if _host_shape(fresh) != _host_shape(hosts):
        return ("the edit changes the model hosts the composition needs "
                "(keys, operations, roles or bindings); `--providers` binds "
                "them at boot, so restart the run to rebind them")
    return None


def _host_shape(hosts) -> dict:
    return {key: (host._revl_service,
                  {method: (op, adapter.binding)
                   for method, (op, adapter) in host._revl_routes.items()})
            for key, host in hosts.items()}


def describe_hosts(hosts) -> list:
    """One line per bound operation, for `revl run --plan`. Names the
    credential VARIABLE, never its value."""
    lines = []
    for key, host in sorted(hosts.items()):
        for method, (op, adapter) in sorted(host._revl_routes.items()):
            b = adapter.binding
            cred = (f", credential from ${b.api_key_env}" if b.api_key_env
                    else "")
            load = (f", loaded by revl on the scheduled device of "
                    f"{', '.join(b.device_names())}" if b.managed else "")
            lines.append(f"  {key}.{method} -> role {op.role}: {b.provider} "
                         f"{b.model} at {b.base_url} ({b.residence}{cred}"
                         f"{load})")
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
