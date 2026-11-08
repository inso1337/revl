"""The host half of the AgentDojo tool bridge.

The generated base composition (see :mod:`bench.agentdojo.services`) declares a
revl service per suite tool. Each service operation dispatches to one extern
body, and every extern body calls :func:`call` here. The runtime therefore
executes the suite's own tool, against the suite's own environment, exactly as
the baseline arm's tool executor does; the only difference is that the call is
reached through an admitted revl component.

The current runtime and environment are process globals, set by the pipeline
immediately before it calls the admitted turn. That is sound here because the
driver runs one task at a time, and the bridge also records every call so the
driver can present the same trace to AgentDojo's task checks that a native
pipeline would have left.
"""

from __future__ import annotations

import json
from typing import Any

_STATE: dict[str, Any] = {"runtime": None, "env": None, "calls": []}


def install(runtime: Any, env: Any) -> None:
    """Point the bridge at the runtime and environment of the running task."""
    _STATE["runtime"] = runtime
    _STATE["env"] = env
    _STATE["calls"] = []


def taken() -> list[dict[str, Any]]:
    """Every tool call the admitted turns have made since :func:`install`."""
    return list(_STATE["calls"])


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return str(value)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def call(name: str, kwargs: dict[str, Any], json_params: tuple[str, ...] = ()) -> str:
    """Dispatch one revl service call to the suite tool it names.

    Returns a JSON object so the turn can read the result as a ``Str``. A tool
    error is returned as data (the ``error`` member) rather than raised: the
    repair message the model sees is the tool's own answer, exactly as a
    pipeline would have shown it.
    """
    runtime = _STATE["runtime"]
    env = _STATE["env"]
    if runtime is None:
        return json.dumps({"result": None, "error": "bridge was not installed"})
    args = {str(k): _plain(v) for k, v in kwargs.items()}
    for param in json_params:
        if isinstance(args.get(param), str):
            try:
                args[param] = json.loads(args[param])
            except json.JSONDecodeError:
                pass
    _STATE["calls"].append({"function": name, "args": args})
    result, error = runtime.run_function(env, name, args)
    payload = {"result": _jsonable(result), "error": error}
    _STATE["calls"][-1]["result"] = payload["result"]
    _STATE["calls"][-1]["error"] = error
    return json.dumps(payload, default=str)
