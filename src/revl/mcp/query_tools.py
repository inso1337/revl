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


# --------------------------------------------------------------------------
# Session-bound query modes (docs/queries.md §9). The static tools above answer
# against a compiled-from-source IR. These two families answer against the OTHER
# two worlds: the live session as it stands after every swap, and a recorded
# run. They carry no `handler` here — the handlers live in `server.py`, which
# owns the session — but their schemas belong with the query surface. The
# envelope is the same one the static tools use; `mode` says which world.
# --------------------------------------------------------------------------

_VERBS = ["emits-to", "withdraw", "depends-on", "reaches", "drift"]

LIVE_QUERY_TOOLS = [
    {
        "name": "revl_live_query",
        "description":
            "THE QUERY SURFACE, ANSWERED AGAINST THE LIVE SESSION. Runs one of "
            "the five verbs (`verb`: emits-to | withdraw | depends-on | reaches "
            "| drift) against the composition CURRENTLY LOADED — the generation "
            "as it stands after every revl_swap, not a static IR. The result is "
            "the same envelope, with `mode: live`: the static \"a hot swap would "
            "change this\" caveat is spent (this IS the post-swap world), and a "
            "`live` block adds what only the runtime knows — which provisions are "
            "actually SERVED right now, so a key whose provider has drifted to an "
            "inactive state reads as absent (`live.notServedNow`). Requires "
            "revl_load. Use this, not revl_query_*, once a composition is running.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "verb": {"type": "string", "enum": _VERBS,
                         "description": "which of the five query verbs to run"},
                "target": {"type": "string",
                           "description": "the verb's argument for emits-to / "
                                          "depends-on (a key, key.method, service "
                                          "or extern)"},
                "component": {"type": "string",
                              "description": "the verb's argument for withdraw / "
                                             "reaches (a component name)"},
                "service": {"type": "string",
                            "description": "the service, for drift"},
                "gains": {**_METHOD_LIST,
                          "description": "drift: methods the service would gain"},
                "loses": {**_METHOD_LIST,
                          "description": "drift: methods the service would lose"},
            },
            "required": ["verb"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
]

HISTORY_QUERY_TOOLS = [
    {
        "name": "revl_history_emitted_between",
        "description":
            "WHICH EMISSIONS CROSSED BETWEEN STEPS X AND Y? A windowed read of a "
            "RECORDED run's effect timeline (revl_load with `record: true`, then "
            "revl_timeline). An emission is a one-way boundary crossing, so each "
            "hit is a real crossing the runtime performed in [from, to] — not a "
            "reachable site. EXACT for the recorded world; `mode: historical`. "
            "The query nobody could ask before: a windowed read of a *verified* "
            "effect timeline.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from": {"type": "integer", "description": "first step index (inclusive)"},
                "to": {"type": "integer", "description": "last step index (inclusive)"},
                "component": {"type": "string",
                              "description": "restrict to one component; omit for all"},
                "timeline": {"type": "object",
                             "description": "an inline replay recording to query "
                                            "instead of the live session's"},
            },
            "required": ["from", "to"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "revl_history_lifetime",
        "description":
            "EVERYTHING THIS COMPONENT TOUCHED DURING ITS LIFE. The recorded "
            "counterpart of revl_query_reach: not a may-analysis over the graph "
            "but the effects and emissions the component ACTUALLY produced on a "
            "recorded run, bounded by item 27's lifecycle trace (when it loaded, "
            "when it withdrew, and why). Reads the live session's recording for "
            "the effects and, if given, a `revl run --trace` JSONL for the "
            "lifecycle. `mode: historical`, EXACT for that run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "component": {"type": "string"},
                "trace": {"type": "array", "items": {"type": "object"},
                          "description": "inline item-27 lifecycle events (a "
                                         "why_runtime JSONL parsed to objects)"},
                "traceFile": {"type": "string",
                              "description": "path to a `revl run --trace` JSONL file"},
                "timeline": {"type": "object",
                             "description": "an inline replay recording to use "
                                            "instead of the live session's"},
            },
            "required": ["component"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
]
