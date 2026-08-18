"""The revl compiler as an MCP server (stdio, JSON-RPC 2.0).

An agent driving a running revl system gets a typed protocol instead of
filesystem access: every mutation it proposes goes through the same
admission gate a human's `revl compile` does, and every rejection comes
back as a structured diagnostic naming the guarantee it violated.

Tools
  revl_check       compile a candidate component (source text or files)
  revl_admit       check a candidate *against a running composition*
  revl_plan        the delta a swap would produce, without applying it
  revl_audit       manifest + G8 boundary surface of a composition
  revl_tools       project a composition's provided services to MCP tools
  revl_grammar     the language surface, small enough to put in a prompt
  revl_query_*     ask the composition a question instead of reading a dump
                   (emitters / withdraw / dependents / reach / drift —
                   docs/queries.md, defined in query_tools.py)

Transport is newline-delimited JSON-RPC on stdin/stdout (the MCP stdio
convention); no third-party dependency, consistent with the rest of the
toolchain.
"""

from __future__ import annotations

import json
import sys

from ..compiler import compile_files, compile_source
from ..diagnostics import GUARANTEES, report
from ..errors import RevlError
from .query_tools import QUERY_TOOLS
from .schema import tools_from_ir
from .session import Session, SessionError

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "revl", "version": "2.0"}


# ---------------------------------------------------------------- helpers

def _compile(source: str | None, files: list[str] | None,
             manifest: dict | None = None, modules: dict | None = None,
             replacing: tuple = ()) -> dict:
    """Compile inline source or paths through the same entry points the CLI
    uses, so the admission gate is literally the same code.

    Inline source never touches the disk: `compile_source` carries the
    ambient manifest and any in-memory `use` modules itself.
    """
    if source is not None:
        return compile_source(source, "<candidate>.rvl", manifest=manifest,
                              replacing=replacing, modules=modules)
    if not files:
        raise ValueError("provide `source` or `files`")
    return compile_files(list(files), manifest=manifest, replacing=replacing)


def _summary(ir: dict) -> dict:
    manifest = ir.get("manifest") or {}
    return {
        "irVersion": ir.get("ir_version"),
        "loadOrder": manifest.get("loadOrder") or [],
        "components": [
            {
                "name": entry.get("name"),
                "requires": entry.get("inject") or [],
                "provides": entry.get("provides") or [],
            }
            for entry in manifest.get("components") or []
        ],
        "services": sorted((ir.get("services") or {}).keys()),
    }


def _boundary_of(ir: dict) -> dict:
    # the CLI owns the G8 walk; import lazily so the module works whether
    # revl is running as `python -m revl` or imported as a library
    from ..__main__ import _boundary

    return _boundary(ir)


# ---------------------------------------------------------------- tools

SESSION = Session()


def _session_error(message: str, **extra) -> dict:
    return {"ok": False, "diagnostics": [{
        "severity": "error", "code": "REVL", "category": "session",
        "message": message,
    }], **extra}


def _tool_load(arguments: dict) -> dict:
    """Boot a composition in memory (nothing is written to disk)."""
    try:
        ir = _compile(arguments.get("source"), arguments.get("files"),
                      modules=arguments.get("modules"))
    except RevlError as error:
        return report(error)
    try:
        state = SESSION.load(ir, arguments.get("config"))
    except SessionError as error:
        return _session_error(str(error))
    return {"ok": True, **_summary(ir), **state}


def _tool_call(arguments: dict) -> dict:
    """Invoke a provided service operation on the running composition."""
    key, method = arguments.get("key"), arguments.get("method")
    if not key or not method:
        return _session_error("`key` and `method` are required")
    try:
        return {"ok": True, **SESSION.call(key, method, arguments.get("args") or [])}
    except SessionError as error:
        return _session_error(str(error))
    except Exception as exc:  # the callee raised — that is a result, not a crash
        return _session_error(f"{type(exc).__name__}: {exc}", raised=True,
                              trace=SESSION.state().get("trace", []))


def _tool_swap(arguments: dict) -> dict:
    """Admit a candidate against what is running, then hot-swap it in. A
    rejected candidate changes nothing — that is the whole point."""
    if not SESSION.loaded:
        return _session_error("nothing is loaded — call revl_load first")
    replacing = tuple(arguments.get("replacing") or ())
    try:
        _compile(arguments.get("source"), arguments.get("files"),
                 manifest=SESSION.ir, modules=arguments.get("modules"),
                 replacing=replacing)
    except RevlError as error:
        rejected = report(error)
        rejected["admitted"] = False
        rejected["swapped"] = False
        rejected["note"] = "the running composition is untouched"
        return rejected

    # admitted: recompile the whole composition so the swap is a full
    # generation (the same shape `revl run --watch` reloads)
    try:
        full = _compile(arguments.get("source"), arguments.get("files"),
                        modules=arguments.get("modules"))
    except RevlError as error:
        rejected = report(error)
        rejected["admitted"] = True
        rejected["swapped"] = False
        rejected["note"] = ("the candidate is admissible against the running "
                            "composition, but is not a complete composition on "
                            "its own — pass the full source set to swap")
        return rejected
    try:
        state = SESSION.swap(full)
    except SessionError as error:
        return _session_error(str(error))
    return {"ok": True, "admitted": True, "swapped": True, **_summary(full), **state}


def _tool_rollback(_arguments: dict) -> dict:
    try:
        return {"ok": True, **SESSION.rollback()}
    except SessionError as error:
        return _session_error(str(error))


def _tool_unload(_arguments: dict) -> dict:
    try:
        return {"ok": True, **SESSION.unload()}
    except SessionError as error:
        return _session_error(str(error))


def _tool_state(_arguments: dict) -> dict:
    return {"ok": True, **SESSION.state(drain=True)}


def _tool_check(arguments: dict) -> dict:
    try:
        ir = _compile(arguments.get("source"), arguments.get("files"),
                      modules=arguments.get("modules"))
    except RevlError as error:
        return report(error)
    return {"ok": True, **_summary(ir), "boundary": _boundary_of(ir)}


def _tool_admit(arguments: dict) -> dict:
    running = arguments.get("manifest")
    if running is None:
        return {"ok": False, "diagnostics": [{
            "severity": "error", "code": "REVL", "category": "usage",
            "message": "`manifest` (a compiled IR document of the running "
                       "composition) is required — admission is checked "
                       "against what is already loaded",
        }]}
    replacing = tuple(arguments.get("replacing") or ())
    try:
        ir = _compile(arguments.get("source"), arguments.get("files"),
                      manifest=running)
    except RevlError as error:
        rejected = report(error)
        rejected["admitted"] = False
        return rejected
    if replacing:
        try:
            ir = compile_files(list(arguments.get("files") or []),
                               manifest=running, replacing=replacing) \
                if arguments.get("files") else ir
        except RevlError as error:
            rejected = report(error)
            rejected["admitted"] = False
            return rejected
    return {
        "ok": True,
        "admitted": True,
        "note": "the candidate links against the running composition; "
                "G2/G3 hold across both and no interface drifted",
        **_summary(ir),
        "boundary": _boundary_of(ir),
    }


def _tool_plan(arguments: dict) -> dict:
    """Dry-run a swap: report the delta without applying it.

    Design decision: when no `manifest` is supplied and this server holds a
    live composition, the plan runs against *that*. `revl_plan` then reads
    as the rehearsal for `revl_swap` with the identical arguments — the
    agent does not have to round-trip the running IR through its context to
    ask "what would this do?". An explicit `manifest` always wins, and the
    `against` field says which was used.
    """
    from ..plan import plan as build_plan  # noqa: PLC0415 — lazy, mirrors _boundary_of

    running = arguments.get("manifest")
    against = "manifest"
    if running is None and SESSION.loaded:
        running, against = SESSION.ir, "session"
    elif running is None:
        against = "nothing (cold start — every provision is a gain)"

    result = build_plan(
        source=arguments.get("source"),
        files=arguments.get("files"),
        manifest=running,
        modules=arguments.get("modules"),
        replacing=tuple(arguments.get("replacing") or ()),
    )
    return {**result, "against": against,
            "note": "nothing was admitted, swapped or written — this is a plan"}


def _tool_audit(arguments: dict) -> dict:
    try:
        ir = _compile(arguments.get("source"), arguments.get("files"))
    except RevlError as error:
        return report(error)
    return {
        "ok": True,
        "manifest": ir.get("manifest") or {},
        "boundary": _boundary_of(ir),
        "guarantees": GUARANTEES,
    }


def _tool_tools(arguments: dict) -> dict:
    try:
        ir = _compile(arguments.get("source"), arguments.get("files"))
    except RevlError as error:
        return report(error)
    composition = arguments.get("composition") or "revl"
    return {
        "ok": True,
        "tools": tools_from_ir(ir, composition=composition),
        "note": "annotations are derived from the compiler: readOnlyHint is "
                "true only where the checker refused unreverted mutation",
    }


_GRAMMAR = """\
revl 2.0 — surface summary (full spec: docs/syntax-2.0.md)

service S { fn f(a: Str) -> Int          // checked operation
            emission fn g(a: Str) -> Int // crosses the boundary
            async fn h() -> Str }

component C requires k: S provides j: T {
  config { field: Int = 3 }
  isolate k in realm("tenant")        // optional realm placement (prelude)
  let r = effect acquire() undo r.release()
  await Job.run("work")               // iteration boundary (divert point)
  emit k.g("x") compensate k.g("undo")
  fail "reason"                       // deliberate L-Raise
  provide j { fn m(a) = pure_fn(a) }
}

type Row = { id: Int, name: Str }      // record
type Outcome = Ok(Row) | NotFound      // ADT; match is exhaustive
pub fn f(xs: List[Row]) -> Int {       // pure stratum (TS-subset exprs)
  var n = 0
  for (x of xs) { if (x.id > 0) n += 1 }
  return n
}
extern pure fn sha(d: Bytes) -> Str = @ts { ... } = @py { ... }
test "name" { assert f([]) == 0 }

Rules that reject code: mutation needs `undo` or `emit` (G4); reads must be
declared (G1); no cycles or duplicate providers (G2/G3); teardown cannot
register effects (G5); expressions are pure (G6); `null` has no type —
absence is Opt[T]; declared types are checked at every boundary.
"""


def _tool_grammar(_arguments: dict) -> dict:
    return {"ok": True, "grammar": _GRAMMAR, "guarantees": GUARANTEES}


_SOURCE_INPUT = {
    "source": {"type": "string",
               "description": "inline .rvl source (use this for a generated component; "
                              "it is never written to disk)"},
    "files": {"type": "array", "items": {"type": "string"},
              "description": "paths to .rvl files (alternative to `source`)"},
    "modules": {"type": "object",
                "description": "in-memory sources for `use` imports, keyed by the path "
                               "the import names — so a multi-module candidate can be "
                               "checked and loaded without touching the filesystem"},
}

TOOLS = [
    {
        "name": "revl_check",
        "description": "Compile a revl component. Returns the composition summary "
                       "and G8 boundary on success, or structured diagnostics "
                       "(code, guarantee, expected/actual, fix hint) on rejection.",
        "inputSchema": {"type": "object", "properties": dict(_SOURCE_INPUT)},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_check,
    },
    {
        "name": "revl_admit",
        "description": "Check a candidate component against a RUNNING composition "
                       "(the admission gate): ambient services are in scope, G2/G3 "
                       "span both, and interface drift is refused. Use before "
                       "hot-swapping generated code into a live system.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **_SOURCE_INPUT,
                "manifest": {"type": "object",
                             "description": "the compiled IR of the running composition"},
                "replacing": {"type": "array", "items": {"type": "string"},
                              "description": "components being withdrawn in this admission"},
            },
            "required": ["manifest"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_admit,
    },
    {
        "name": "revl_plan",
        "description": "Dry run for admission: what a swap WOULD do, without doing "
                       "it. Reports provisions gained and withdrawn, which running "
                       "components divert or reactivate as a consequence, the LIFO "
                       "teardown order, how the composition's irreversible reach "
                       "(G8) changes, and any interface drift. A rejected candidate "
                       "is explained, not thrown. Defaults to the composition this "
                       "server has loaded, so it is the rehearsal for revl_swap.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **_SOURCE_INPUT,
                "manifest": {"type": "object",
                             "description": "compiled IR of the running composition; "
                                            "omit to plan against the loaded session "
                                            "(or against nothing, for a cold start)"},
                "replacing": {"type": "array", "items": {"type": "string"},
                              "description": "components withdrawn in this admission"},
            },
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_plan,
    },
    {
        "name": "revl_audit",
        "description": "The G8 boundary surface of a composition: which emissions "
                       "each component can perform, which are compensated, its "
                       "iteration boundaries, and the host code it reaches.",
        "inputSchema": {"type": "object", "properties": dict(_SOURCE_INPUT)},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_audit,
    },
    {
        "name": "revl_tools",
        "description": "Project a composition's provided services to MCP tool "
                       "definitions whose behavioural annotations are derived from "
                       "the compiler rather than asserted by an author.",
        "inputSchema": {
            "type": "object",
            "properties": {**_SOURCE_INPUT,
                           "composition": {"type": "string",
                                           "description": "tool-name prefix"}},
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_tools,
    },
    {
        "name": "revl_load",
        "description": "Boot a composition IN MEMORY and hold it live. Nothing is "
                       "written to disk, so a draft component can be run and tested "
                       "before it exists as a file. Returns fiber states, provided "
                       "keys and the lifecycle trace.",
        "inputSchema": {
            "type": "object",
            "properties": {**_SOURCE_INPUT,
                           "config": {"type": "object",
                                      "description": "per-component config tables"}},
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
        "handler": _tool_load,
    },
    {
        "name": "revl_call",
        "description": "Invoke a provided service operation on the running composition "
                       "— how you test a component you just loaded. Returns the result "
                       "and the trace it produced.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "provided key, e.g. `cache`"},
                "method": {"type": "string", "description": "operation name"},
                "args": {"type": "array", "description": "positional arguments"},
            },
            "required": ["key", "method"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
        "handler": _tool_call,
    },
    {
        "name": "revl_swap",
        "description": "Admit a candidate against the RUNNING composition and hot-swap "
                       "it in. A rejected candidate leaves the running system untouched. "
                       "This is the acting half of revl_admit.",
        "inputSchema": {
            "type": "object",
            "properties": {**_SOURCE_INPUT,
                           "replacing": {"type": "array", "items": {"type": "string"},
                                         "description": "components withdrawn in this swap"}},
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "handler": _tool_swap,
    },
    {
        "name": "revl_rollback",
        "description": "Restore the generation that was running before the last swap.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "handler": _tool_rollback,
    },
    {
        "name": "revl_unload",
        "description": "Tear the composition down and report the residue checks "
                       "(registry, provisions, effects, listeners) — prove a component "
                       "leaves nothing behind before you commit it to disk.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "handler": _tool_unload,
    },
    {
        "name": "revl_state",
        "description": "What is loaded right now: fiber states, provided keys, whether "
                       "a rollback is available, and the trace since the last call.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_state,
    },
    {
        "name": "revl_grammar",
        "description": "The revl surface syntax and the rules that reject code — "
                       "small enough to keep in context while generating.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_grammar,
    },
]

# composition queries (docs/queries.md) — defined next door so this module
# stays the protocol layer and the query surface can grow on its own
TOOLS.extend(QUERY_TOOLS)

_HANDLERS = {tool["name"]: tool["handler"] for tool in TOOLS}
_ADVERTISED = [{k: v for k, v in tool.items() if k != "handler"} for tool in TOOLS]


# ---------------------------------------------------------------- protocol

def handle(message: dict) -> dict | None:
    """One JSON-RPC request -> one response (or None for a notification)."""
    method = message.get("method")
    request_id = message.get("id")

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": "Compile revl components before proposing them; use "
                            "revl_admit against the running manifest before a swap, "
                            "and revl_plan to see what that swap would do first.",
        }
    elif method == "tools/list":
        result = {"tools": _ADVERTISED}
    elif method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        handler = _HANDLERS.get(name)
        if handler is None:
            return _error(request_id, -32602, f"unknown tool: {name}")
        try:
            payload = handler(params.get("arguments") or {})
        except Exception as exc:  # a tool failure is a result, not a transport error
            payload = {"ok": False, "diagnostics": [{
                "severity": "error", "code": "REVL", "category": "internal",
                "message": f"{type(exc).__name__}: {exc}",
            }]}
        result = {
            "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
            "isError": not payload.get("ok", False),
            "structuredContent": payload,
        }
    elif method in ("notifications/initialized", "initialized"):
        return None
    elif method == "ping":
        result = {}
    else:
        if request_id is None:
            return None
        return _error(request_id, -32601, f"method not found: {method}")

    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def serve(stdin=None, stdout=None) -> int:
    """Read newline-delimited JSON-RPC from stdin until EOF."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            stdout.write(json.dumps(_error(None, -32700, "parse error")) + "\n")
            stdout.flush()
            continue
        response = handle(message)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
    return 0
