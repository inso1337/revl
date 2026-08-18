"""Composition queries as MCP tools — the primary consumer of `revl.query`.

`revl_audit` hands an agent a dump and leaves it to read. These five tools
hand it an answer to the question it actually has, in a shape it can act on:
a flat list of sites with component/scope/path fields, plus `precision`,
`precisionNote` and `assumptions` so the agent can tell whether it was given
a proof or a may-analysis before it edits anything. The human rendering
(`revl query ...` on the CLI) is the courtesy view of the same structure.

`structuredContent` in the tool result is the query result verbatim; nothing
is reshaped for display on the way out.
"""

from __future__ import annotations

from ..compiler import compile_files, compile_source
from ..diagnostics import report
from ..errors import RevlError
from .. import query as Q

_SOURCE_INPUT = {
    "source": {"type": "string",
               "description": "inline .rvl source for the composition to query "
                              "(never written to disk)"},
    "files": {"type": "array", "items": {"type": "string"},
              "description": "paths to .rvl files (alternative to `source`)"},
    "modules": {"type": "object",
                "description": "in-memory sources for `use` imports, keyed by the "
                               "path the import names"},
}

_PRECISION_NOTE = (
    "Every result carries `precision` (\"exact\" or \"over-approximation\"), "
    "`precisionNote` and `assumptions`. Read them before acting: two of these "
    "queries are proofs over the linked graph and three are may-analyses."
)


def _compile(arguments: dict) -> dict:
    source = arguments.get("source")
    if source is not None:
        return compile_source(source, "<query>.rvl",
                              modules=arguments.get("modules"))
    files = arguments.get("files")
    if not files:
        raise ValueError("provide `source` or `files`")
    return compile_files(list(files))


def _run(arguments: dict, fn, *args, **kwargs) -> dict:
    try:
        ir = _compile(arguments)
    except RevlError as error:
        return report(error)
    except ValueError as error:
        return {"ok": False, "diagnostics": [{
            "severity": "error", "code": "REVL", "category": "usage",
            "message": str(error)}]}
    return fn(ir, *args, **kwargs)


def _require(arguments: dict, field: str):
    value = arguments.get(field)
    if not value:
        return None, {"ok": False, "diagnostics": [{
            "severity": "error", "code": "REVL", "category": "usage",
            "message": f"`{field}` is required"}]}
    return value, None


def _tool_emitters(arguments: dict) -> dict:
    target, err = _require(arguments, "target")
    return err or _run(arguments, Q.emitters, target)


def _tool_withdraw(arguments: dict) -> dict:
    component, err = _require(arguments, "component")
    return err or _run(arguments, Q.withdrawal, component)


def _tool_dependents(arguments: dict) -> dict:
    target, err = _require(arguments, "target")
    return err or _run(arguments, Q.dependents, target)


def _tool_reach(arguments: dict) -> dict:
    component, err = _require(arguments, "component")
    return err or _run(arguments, Q.reach, component)


def _tool_drift(arguments: dict) -> dict:
    service, err = _require(arguments, "service")
    if err:
        return err
    return _run(arguments, Q.drift, service,
                gains=list(arguments.get("gains") or []),
                losses=list(arguments.get("loses") or arguments.get("losses") or []))


_METHOD_LIST = {"type": "array", "items": {"type": "string"}}

QUERY_TOOLS = [
    {
        "name": "revl_query_emitters",
        "description":
            "WHO EMITS TO X? Every component and provide-method whose "
            "irreversible reach includes a provision key, a `key.method`, a "
            "service or an extern — including transitively through pure `fn` "
            "calls and across the service seam into providers. Use before "
            "changing anything that writes to a boundary. " + _PRECISION_NOTE,
        "inputSchema": {
            "type": "object",
            "properties": {
                **_SOURCE_INPUT,
                "target": {"type": "string",
                           "description": "a provision key (`db`), a `key.method` "
                                          "(`db.execute`), a service name "
                                          "(`Database`), or an extern name"},
            },
            "required": ["target"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_emitters,
    },
    {
        "name": "revl_query_withdraw",
        "description":
            "WHAT BREAKS IF I WITHDRAW C? The reactive cascade: components "
            "that inject a provision C provides, then their dependents, with "
            "the LIFO order the runtime would tear them down in and the keys "
            "that stop being provided. EXACT — the linker already resolved "
            "this graph. Use before proposing a revl_swap that replaces or "
            "removes a component.",
        "inputSchema": {
            "type": "object",
            "properties": {**_SOURCE_INPUT,
                           "component": {"type": "string",
                                         "description": "the component to withdraw"}},
            "required": ["component"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_withdraw,
    },
    {
        "name": "revl_query_dependents",
        "description":
            "WHO DEPENDS ON THIS? For a provision key or a service: the "
            "provider, every consumer, which operations each consumer "
            "actually calls (and which of those are emissions), and the "
            "realm each resolution happens in. EXACT.",
        "inputSchema": {
            "type": "object",
            "properties": {**_SOURCE_INPUT,
                           "target": {"type": "string",
                                      "description": "a provision key (`db`) or a "
                                                     "service name (`Database`)"}},
            "required": ["target"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_dependents,
    },
    {
        "name": "revl_query_reach",
        "description":
            "WHAT DOES C REACH? The transitive boundary surface of one "
            "component: its emissions, the host code it touches, its "
            "iteration boundaries, and everything it reaches by calling into "
            "the providers it injects. Over-approximate, and it says so — "
            "`complete: false` plus `unresolvedInjections` marks the case "
            "where a key nothing in this IR provides hides part of the "
            "surface. " + _PRECISION_NOTE,
        "inputSchema": {
            "type": "object",
            "properties": {**_SOURCE_INPUT,
                           "component": {"type": "string"}},
            "required": ["component"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_reach,
    },
    {
        "name": "revl_query_drift",
        "description":
            "WHAT CHANGES IF A SERVICE GAINS OR LOSES A METHOD? Interface "
            "drift: which providers must implement or drop it, and which call "
            "sites stop resolving. Called with no `gains`/`loses` it reports "
            "the current per-method provider and call-site map. EXACT for the "
            "compiled composition — this is the list the admission gate would "
            "flag, not a promise the edit is otherwise safe.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **_SOURCE_INPUT,
                "service": {"type": "string", "description": "the service name"},
                "gains": {**_METHOD_LIST,
                          "description": "methods the service would gain"},
                "loses": {**_METHOD_LIST,
                          "description": "methods the service would lose"},
            },
            "required": ["service"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_drift,
    },
]
