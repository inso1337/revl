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
from ..diagnostics import FIXES, GUARANTEES, obligations, report
from ..errors import RevlError
from . import gauntlet as _gauntlet
from .persist import RestoreError
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


def _origin(arguments: dict) -> dict:
    """The admission inputs of a load/swap, kept so the composition can later
    be snapshotted for re-admission (docs/persistence.md)."""
    origin = {}
    for key in ("source", "files", "modules"):
        value = arguments.get(key)
        if value is not None:
            origin[key] = value
    return origin


def _tool_load(arguments: dict) -> dict:
    """Boot a composition in memory (nothing is written to disk)."""
    try:
        ir = _compile(arguments.get("source"), arguments.get("files"),
                      modules=arguments.get("modules"))
    except RevlError as error:
        return report(error)
    try:
        state = SESSION.load(ir, arguments.get("config"),
                             record=bool(arguments.get("record")),
                             origin=_origin(arguments))
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
        state = SESSION.swap(full, origin=_origin(arguments))
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


def _tool_gauntlet(arguments: dict) -> dict:
    """Grade a candidate: run the battery in an isolated scratch session and
    return the verdict dossier. A rejected or misbehaving candidate is graded,
    not thrown, and the live composition is never touched (docs/gauntlet.md)."""
    if arguments.get("source") is None and not arguments.get("files"):
        return _session_error("provide `source` or `files` — the gauntlet "
                              "grades a candidate component")
    return _gauntlet.run(SESSION, arguments)


# -- composition persistence (docs/persistence.md) ------------------------

def _tool_snapshot(_arguments: dict) -> dict:
    """Capture the live composition as re-admittable JSON (sources + manifest).

    This is not a runtime dump: it is the inputs a fresh boot would need to
    put the same composition back through the admission gate."""
    try:
        return {"ok": True, "snapshot": SESSION.snapshot()}
    except SessionError as error:
        return _session_error(str(error))


def _tool_restore(arguments: dict) -> dict:
    """Re-admit a snapshot into an empty session by replaying admission.

    A component the *current* checker rejects fails the restore loudly with
    its diagnostic — a snapshot cannot smuggle a now-rejected component past a
    newer checker. The running system is untouched on failure."""
    snap = arguments.get("snapshot")
    if snap is None:
        return _session_error("`snapshot` (a document from revl_snapshot) is required")
    try:
        return {"ok": True, **SESSION.restore(snap)}
    except RestoreError as error:
        # a rejected re-admission is a *result*: surface the diagnostic, and
        # make clear nothing was loaded
        payload = {"ok": False, "restored": False, "reAdmitted": False,
                   "note": "the component was rejected by the current checker "
                           "and was not loaded — the snapshot cannot bypass "
                           "the admission gate"}
        if error.diagnostic is not None:
            payload["diagnostics"] = [error.diagnostic]
        else:
            payload["diagnostics"] = [{
                "severity": "error", "code": "REVL", "category": "session",
                "message": str(error)}]
        return payload
    except SessionError as error:
        return _session_error(str(error))


# -- backwards replay (docs/replay.md) ------------------------------------

def _tool_timeline(arguments: dict) -> dict:
    try:
        return {"ok": True, **SESSION.timeline(arguments.get("component"))}
    except SessionError as error:
        return _session_error(str(error))


def _tool_inspect_step(arguments: dict) -> dict:
    if "at" not in arguments:
        return _session_error("`at` is required (-1 means 'before every step')")
    try:
        return {"ok": True, **SESSION.inspect_step(arguments.get("component"),
                                                   arguments["at"])}
    except SessionError as error:
        return _session_error(str(error))


def _tool_step_back(arguments: dict) -> dict:
    if "to" not in arguments:
        return _session_error("`to` is required (-1 unwinds everything recorded)")
    try:
        return {"ok": True, **SESSION.step_back(arguments.get("component"),
                                                arguments["to"],
                                                force=bool(arguments.get("force")))}
    except SessionError as error:
        # a refused unwind is a *result*, not a crash: it means the range
        # contains an emission that cannot be undone
        return _session_error(str(error), refused=True)


def _tool_replay_bisect(arguments: dict) -> dict:
    predicate = arguments.get("assert") or arguments.get("predicate")
    if not predicate:
        return _session_error("`assert` is required — a predicate expression "
                              "over the `inspect` view (e.g. \"emissionsSoFar\" "
                              "or \"'db' in activeProvisions\")")
    try:
        return {"ok": True, **SESSION.bisect(arguments.get("component"),
                                             predicate)}
    except SessionError as error:
        return _session_error(str(error))


def _tool_replay_forward(arguments: dict) -> dict:
    if "from" not in arguments:
        return _session_error("`from` is required")
    try:
        return {"ok": True, **SESSION.replay_forward(arguments.get("component"),
                                                     arguments["from"])}
    except SessionError as error:
        return _session_error(str(error))


def _tool_check(arguments: dict) -> dict:
    try:
        ir = _compile(arguments.get("source"), arguments.get("files"),
                      modules=arguments.get("modules"))
    except RevlError as error:
        return report(error)
    # `holes` is the agent's own remaining work on this draft: every
    # placeholder it wrote that still has a type and no implementation
    # (docs/holes.md). `ok: true` with a non-empty `holes` means "checked,
    # and not admissible until these are closed".
    return {"ok": True, **_summary(ir), "boundary": _boundary_of(ir),
            "holes": obligations(ir.get("holes") or [])["holes"]}


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
            emission[db] fn p(a: Str)    // ... only through `db`
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
    # `fixes` is the `revl explain` payload: for every guarantee, the rewrite
    # that satisfies it — so an agent that gets a code back can act without
    # a second round trip
    return {"ok": True, "grammar": _GRAMMAR, "guarantees": GUARANTEES,
            "fixes": FIXES}


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
        "description": "Compile a revl component. Returns the composition summary, "
                       "the G8 boundary and `holes` (open typed-hole obligations, "
                       "each with file, line, expected type and message) on success, "
                       "or structured diagnostics (code, guarantee, expected/actual, "
                       "fix hint) on rejection. A draft with holes compiles; it is "
                       "refused at admission until every hole is filled.",
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
                       "each component can perform, the capabilities each of those "
                       "may cross (`*` = unscoped), which are compensated, its "
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
                                      "description": "per-component config tables"},
                           "record": {"type": "boolean",
                                      "description": "record the effect accumulator so "
                                                     "the composition can be stepped "
                                                     "backwards (revl_timeline / "
                                                     "revl_step_back). Must be set at "
                                                     "load: recording is installed "
                                                     "before activation."}},
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
        "name": "revl_gauntlet",
        "description": "Grade a candidate component instead of merely admitting "
                       "it: run a battery in an ISOLATED scratch session the live "
                       "composition never sees, and return a structured verdict "
                       "dossier. It separates what was PROVED (admission, derived "
                       "teardown), what was TESTED with counts (a real boot/unload "
                       "no-residue lifecycle), and what remains CLAIMED (the "
                       "enumerated G8 extern boundary). Fault-sweep and "
                       "inverse-round-trip sections are present but report "
                       "`pending`. A rejected or faulting candidate is graded, not "
                       "thrown; the running system is untouched either way. "
                       "`ok` reports that a dossier was produced; the grade is in "
                       "`verdict` (admissible | rejected). See docs/gauntlet.md.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **_SOURCE_INPUT,
                "config": {"type": "object",
                           "description": "per-component config for the scratch boot"},
                "replacing": {"type": "array", "items": {"type": "string"},
                              "description": "components withdrawn in this admission"},
            },
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_gauntlet,
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
        "name": "revl_snapshot",
        "description": "Capture the running composition as re-admittable JSON: the "
                       "SOURCES of the currently-admitted components plus the "
                       "manifest and meta. Not a runtime dump — it is the inputs a "
                       "fresh boot needs to put the same composition back through "
                       "the admission gate, so self-evolution survives a restart. "
                       "Pair with revl_restore.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_snapshot,
    },
    {
        "name": "revl_restore",
        "description": "Re-admit a snapshot (from revl_snapshot) into an empty "
                       "session by REPLAYING ADMISSION: the sources are recompiled "
                       "through the same gate a live revl_load runs, never rehydrated "
                       "from the stored manifest. A component the current checker "
                       "rejects fails the restore loudly with its diagnostic — a "
                       "snapshot cannot smuggle a now-rejected component past a newer "
                       "checker. Requires nothing loaded.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "snapshot": {"type": "object",
                             "description": "a document produced by revl_snapshot "
                                            "({sources, manifest, meta})"},
            },
            "required": ["snapshot"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
        "handler": _tool_restore,
    },
    {
        "name": "revl_timeline",
        "description": "The recorded effect accumulator of a running composition: "
                       "every effect step in order, the inverse registered for it, "
                       "and every emission — marked as the one kind of step that "
                       "has no inverse. Requires `revl_load` with `record: true`.",
        "inputSchema": {
            "type": "object",
            "properties": {"component": {"type": "string",
                                         "description": "component name; omit for all"}},
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_timeline,
    },
    {
        "name": "revl_inspect_step",
        "description": "What the composition looks like at step k: which provisions "
                       "are active, which inverses are still accumulated (newest "
                       "first), which have already run, and the emissions that "
                       "happened at or before k.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "component": {"type": "string"},
                "at": {"type": "integer",
                       "description": "step index; -1 means before every step"},
            },
            "required": ["at"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_inspect_step,
    },
    {
        "name": "revl_step_back",
        "description": "Unwind the accumulator to step k by running the registered "
                       "inverses from the top down, newest first — leaving the "
                       "component LIVE, not torn down. Refuses if the range crosses "
                       "an emission with no `compensate` (an emission cannot be "
                       "undone); `force` crosses anyway and reports what was crossed. "
                       "The guarantee is 'the inverses ran in order', never 'state "
                       "was restored'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "component": {"type": "string"},
                "to": {"type": "integer",
                       "description": "unwind down to this step; -1 unwinds everything"},
                "force": {"type": "boolean",
                          "description": "cross uncompensated emissions anyway"},
            },
            "required": ["to"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "handler": _tool_step_back,
    },
    {
        "name": "revl_replay_bisect",
        "description": "git-bisect for an execution: binary-search the recorded "
                       "timeline for the FIRST step at which `assert` flips, in "
                       "log2(N) evaluations instead of N. `assert` is a predicate "
                       "expression over the same `inspect` view — names like "
                       "`activeProvisions`, `emissionsSoFar`, `accumulated`, "
                       "`step`. Read-only: it reconstructs each probed step, "
                       "never mutating the timeline. Returns the found step's "
                       "full record (who ran, what it touched, which realm) and "
                       "the verified/unverified status of the effects on the path "
                       "— today always UNVERIFIED, because `verified effect` "
                       "(roadmap item 26) is not built, so the bisect trusts the "
                       "recorded inverses rather than checking them.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "component": {"type": "string"},
                "assert": {"type": "string",
                           "description": "predicate expression over the inspect "
                                          "view; the first step where it flips is "
                                          "the answer"},
            },
            "required": ["assert"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_replay_bisect,
    },
    {
        "name": "revl_replay_forward",
        "description": "Re-run the tail after step k by re-invoking the service calls "
                       "that produced it — how you re-test after a fix. Activation-body "
                       "steps are reported as not replayable (the body is one generator; "
                       "its head cannot be skipped) rather than faked.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "component": {"type": "string"},
                "from": {"type": "integer", "description": "replay steps after this one"},
            },
            "required": ["from"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "handler": _tool_replay_forward,
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
