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

Who authors what
----------------
The driving agent is NOT a trusted host-code author. That is a decision, and it
is written down here because the code used to decide the opposite by accident:
`_compile` passed no `AdmissionProfile`, so an inline `@py` body handed to
`revl_load`/`revl_check`/`revl_swap` compiled, loaded and RAN arbitrary host
Python (`run.py`'s `exec(compile(source, ...))`) with no operator in the loop.
The item-246 approval policy did not close it either: its classifier reads
DECLARED extern facts, so a body declared `pure` crossed nothing, and an
`emission[notify]` body that also read a `.env` produced a ticket naming only
`notify` — an operator approving a notification while arbitrary I/O rode along.

So the authoring verbs now compile agent-supplied source under the item-329
untrusted-author profile (`admit_profile.AdmissionProfile`), the same defense
`Gate.propose` (item 334) already applies to agent-authored components:

  * the agent may COMPOSE services; it may not DECLARE an `extern`/host block,
    nor REACH one through an imported module (`check_no_extern`,
    `check_no_host_extern_reach`), nor mint its own declassifier;
  * the host code an admitted turn is allowed to use is the OPERATOR's:
    `--provider PATH` modules, exactly `Gate.propose`'s granted-providers map;
  * `--grant SERVICE` turns on the item-329 reach allowlist when the operator
    wants reach bounded too.

Trust is an explicit operator decision that DEFAULTS CLOSED (`AuthoringTrust`
below, `revl mcp serve --author-trust`), never an accident of which verb was
called. An operator who genuinely wants the agent authoring host code passes
`--author-trust trusted`; that mode is honest rather than silent — every
class-(c) ticket raised by a composition carrying agent-authored host bodies
says so, so nobody approves `notify` believing that is all that will run.

`files` arguments are jailed to the operator-sanctioned root(s) (see
`_jail_refusal`): unjailed, they were a filesystem oracle (existence, first
token, line numbers) over every path on the machine, and `revl_restore` +
`revl_snapshot` was a full arbitrary-file READ. A `use` path written INSIDE
transport-carried source is the same path argument by another carrier — the
compile follows it and reads the file — so it is jailed by the same door
(`_transport_use_escapes`, roadmap 425 F2). Imports in a `.rvl` file that was
already on disk inside a sanctioned root are the operator's own composition
layout and resolve unchanged.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, replace

from ..admit_profile import AdmissionProfile
from ..compiler import compile_files, compile_source
from ..diagnostics import FIXES, GUARANTEES, explain, report
from .. import grammar_summary as _grammar_summary
from . import fillspec
from . import edit as _edit
from . import leases as _leases
from . import operator as _operator
from ..errors import RevlError
from . import gauntlet as _gauntlet
from . import quarantine as _quarantine
from . import repair as _repair
from . import ship as _ship
from .persist import RestoreError
from .approval import ApprovalRequired, two_step_payload
from .. import query as Q
from .query_tools import HISTORY_QUERY_TOOLS, LIVE_QUERY_TOOLS, QUERY_TOOLS
from .schema import tools_from_ir
from .session import Session, SessionError

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "revl", "version": "2.0"}


# -------------------------------------------------------- authoring trust
#
# How much this server trusts the AGENT on the other end of the transport, as an
# AUTHOR. Operator-controlled, default closed. See the module docstring for why
# this exists; the mechanism is item 329's `AdmissionProfile`, wired the way
# item 334's `Gate.propose` wires it, not a second one invented here.

@dataclass(frozen=True)
class AuthoringTrust:
    """The operator's answer to "may the agent driving this server author host
    code?", plus the filesystem the agent may name.

    `host_code`   — False (the default, closed): agent-supplied `source`/`files`
                    /`modules` are compiled under the untrusted-author profile,
                    so the turn may compose granted services but may neither
                    declare nor reach an `extern`/host block. True: the operator
                    has explicitly declared the agent a trusted host-code author
                    (`--author-trust trusted`); the compile is then byte-
                    identical to before this existed, and every class-(c) ticket
                    the resulting composition raises SAYS the candidate carries
                    unreviewed host code (`_approval_required`).
    `granted`     — the item-329 reach allowlist: the service names an admitted
                    turn may reach. `None` (default) leaves the allowlist OFF —
                    there is no honest default for "which of the running
                    system's services this agent may reach", so the operator
                    declares it with `--grant` or it is not enforced. A set —
                    even empty — turns it on.
    `providers`   — `{path: source}` of OPERATOR-sanctioned host-code modules,
                    merged under the agent's own `modules` so an untrusted turn
                    can COMPOSE host-backed services without authoring them.
                    This is `Gate.propose`'s granted-providers map, same shape,
                    same rule: reaching their externs directly is still refused
                    by `check_no_host_extern_reach`; only their SERVICES compose.
    `roots`       — the directories a caller-supplied path argument may name.
                    Empty means "the directory the operator started the server
                    in" (see `_file_roots`).
    """

    host_code: bool = False
    granted: frozenset[str] | None = None
    providers: dict[str, str] | None = None
    roots: tuple[str, ...] = ()

    def profile(self) -> AdmissionProfile | None:
        """The admission profile agent-authored source compiles under, or `None`
        when the operator has declared the agent trusted (byte-identical to a
        human's `revl compile`)."""
        if self.host_code:
            return None
        if self.granted is not None:
            return AdmissionProfile.untrusted_author(self.granted)
        # the host-code half of the untrusted-author profile with the reach
        # allowlist left off: `no_extern` (+ its transitive reach sweep) and
        # `no_declassify` need no operator input to be correct, the allowlist
        # does. `taint_strict` rides with them for the same reason it rides in
        # `untrusted_author`: a model-authored turn annotates nothing.
        return replace(AdmissionProfile.untrusted_author(()), granted=None)


# The live trust level. `revl mcp serve` sets it from its flags; a test or an
# embedder sets it with `set_authoring_trust`. Module-global for the same reason
# `SESSION` is: this server serves one agent over one stdio pipe.
AUTHORING = AuthoringTrust()


def set_authoring_trust(**fields) -> AuthoringTrust:
    """Replace the live `AuthoringTrust` (operator-side wiring; the agent has no
    verb that reaches this). Returns the new value."""
    global AUTHORING
    if fields.get("granted") is not None:
        fields["granted"] = frozenset(fields["granted"])
    if "roots" in fields:
        roots = fields.pop("roots")
        if roots is not None:
            fields["roots"] = tuple(os.path.realpath(os.path.abspath(r))
                                    for r in roots)
    AUTHORING = replace(AUTHORING, **fields)
    return AUTHORING


# ------------------------------------------------------------- the path jail
#
# Every caller-supplied path argument is confined to the operator-sanctioned
# root(s). Unjailed, `files` was an ORACLE over the whole filesystem: a syntax
# error names the file and its first token, a missing file says so, and any path
# that happens to be valid revl gives back its full compiled structure — and
# `revl_restore` + `revl_snapshot` handed back a file's entire CONTENT.

_PATH_ARGUMENTS = frozenset({"files", "candidateFiles", "baselineFiles",
                             "traceFile", "registry"})


def _file_roots() -> tuple[str, ...]:
    """The sanctioned roots. Explicit `--root` wins; otherwise the directory the
    operator started the server in — the one directory the operator did sanction
    by launching here. Never empty, and never `/` by default."""
    if AUTHORING.roots:
        return AUTHORING.roots
    return (os.path.realpath(os.getcwd()),)


def _within_roots(path: str, roots: tuple[str, ...]) -> bool:
    """Whether `path` resolves inside one of `roots`. Resolved with `realpath`
    BEFORE the comparison, so `../` traversal and a symlink pointing out of the
    root are both caught, and resolved without stat-ing for existence, so the
    check itself is not the oracle it is closing."""
    real = os.path.realpath(os.path.abspath(path))
    for root in roots:
        if real == root or real.startswith(root + os.sep):
            return True
    return False


def _collect_path_arguments(node, out: list) -> None:
    """Every caller-supplied path anywhere in a tool's arguments, including the
    ones nested inside a `revl_restore` snapshot document (`sources.files`)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _PATH_ARGUMENTS:
                if isinstance(value, str):
                    out.append(value)
                elif isinstance(value, list):
                    out.extend(v for v in value if isinstance(v, str))
            else:
                _collect_path_arguments(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_path_arguments(item, out)


def _escaping_use(path: str) -> bool:
    """Whether a `use` path written in transport-carried source names a file
    outside the importing directory's own tree.

    Purely syntactic, so the check itself opens and stat-s nothing (the whole
    point is that the compile's own resolution was the oracle). Two shapes
    escape: an ABSOLUTE path, and a relative path that normalises to a leading
    `..`. Everything else resolves either under the importer's directory — for
    transport-carried source that is the directory the server was started in,
    the one the operator sanctioned — or, when nothing sits there, through the
    OPERATOR's own search path (`REVL_IMPORT_PATH`, then the installed stdlib),
    which is why `use "stdlib/str.rvl"` keeps working untouched.
    """
    if os.path.isabs(path):
        return True
    normalized = os.path.normpath(path)
    return normalized == ".." or normalized.startswith(".." + os.sep)


def _transport_use_escapes(arguments: dict) -> list[str]:
    """The `use` paths in this call's transport-carried source that leave the
    sanctioned tree (roadmap 425 F2).

    A `use` path IS a caller-supplied path argument; it just rides inside the
    source text rather than beside it, which is how it survived the argument
    jail above. The compile follows it: `_ModuleLoader.resolve_use` joins it to
    the importing directory and reads whatever is there, so
    `revl_check {"source": 'use \"/etc/passwd\" as p', "modules": {...}}`
    reported that file's existence, its first token and its line numbers — the
    same filesystem oracle the argument jail closed, reached through the source
    instead. Item 422's lesson exactly: a guard on the destination that does not
    cover the source is not a guard.

    Scoped to source that ARRIVED OVER THE TRANSPORT, which is the same premise
    `_compile` already runs on: a `.rvl` file on disk inside a sanctioned root
    was put there by a human, and its own `../lib/x.rvl` is the operator's
    composition layout, not an agent's traversal. Those are untouched.

    A path the caller also supplies in-memory (a `modules` key) is exempt: it
    resolves out of the sources map with no disk access at all, so it is not a
    path into the filesystem in the first place.
    """
    sources: list = []
    _collect_sources(arguments, sources)
    if not sources:
        return []
    supplied: set[str] = set()
    _collect_module_keys(arguments, supplied)
    from ..parser import Parser  # noqa: PLC0415

    escapes: list[str] = []
    for text in sources:
        try:
            program = Parser(text, "<candidate>.rvl").parse()
        except Exception:  # noqa: BLE001 — not parseable: the handler reports it
            continue
        for use in program.uses:
            if not _escaping_use(use.path):
                continue
            if os.path.abspath(use.path) in supplied:
                continue
            escapes.append(use.path)
    return escapes


def _collect_module_keys(node, out: set) -> None:
    """The abspaths of every in-memory module this call supplies. Those resolve
    from the sources map, never from disk."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _SOURCE_MAP_ARGUMENTS and isinstance(value, dict):
                out.update(os.path.abspath(k) for k in value)
            else:
                _collect_module_keys(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_module_keys(item, out)


def _jail_refusal(arguments: dict) -> dict | None:
    """The refusal payload when a tool call names a path outside the sanctioned
    roots, or `None` when every path is inside. Fails CLOSED: refused before the
    handler runs, so nothing is opened, stat-ed or compiled, and the message
    discloses only what the caller already sent.

    Two carriers, one jail: a path ARGUMENT (`files`, `traceFile`, ...) and a
    `use` path written inside transport-carried source (`_transport_use_escapes`,
    roadmap 425 F2)."""
    paths: list = []
    _collect_path_arguments(arguments, paths)
    roots = _file_roots()
    escaped = [p for p in paths if not _within_roots(p, roots)]
    imports = _transport_use_escapes(arguments) if not escaped else []
    if not escaped and not imports:
        return None
    allowed = ", ".join(f"`{r}`" for r in roots)
    if escaped:
        named = ", ".join(f"`{p}`" for p in sorted(set(escaped)))
        message = (f"refused: {named} is outside the operator-sanctioned "
                   f"root(s) [{allowed}] — a path argument may not leave them")
        hint = ("a path argument is confined to what the operator sanctioned "
                "when starting this server (`revl mcp serve --root DIR`); "
                "without the jail, `files` reports whether any path on the "
                "machine exists, what its first token is and where it fails, "
                "and a snapshot round-trip returns its content. Send the "
                "candidate as inline `source`, or ask the operator to sanction "
                "the directory")
    else:
        named = ", ".join(f'`use "{p}"`' for p in sorted(set(imports)))
        message = (f"refused: {named} leaves the operator-sanctioned "
                   f"root(s) [{allowed}] — an import in source sent over this "
                   "transport may not name an absolute path or traverse upward")
        hint = ("a `use` path is a path argument like any other: the compile "
                "follows it and reads the file, so an unconfined one reports "
                "whether any path on the machine exists, what its first token "
                "is and where it fails. Send the imported module inline in "
                "`modules`, keyed by the relative path the import names; write "
                "the import relative to the sanctioned directory; or, for an "
                "installed module, use its search-path spelling (`use "
                "\"stdlib/str.rvl\"`). A `.rvl` file already inside a sanctioned "
                "root resolves its own imports unchanged")
    return {"ok": False, "diagnostics": [{
        "severity": "error", "code": "REVL", "category": "admission",
        "message": message, "hint": hint,
    }], "note": "nothing was read, compiled or loaded"}


# ------------------------------------------------ the pre-dispatch source gate
#
# `_compile` carries the FULL untrusted-author profile (declare + transitive
# reach + declassify + the granted allowlist) for the verbs that route through
# it. But it is not the only door into the compiler: `revl_gauntlet`,
# `revl_quarantine`, `revl_repair`, `revl_canary` and `revl_edit` each compile
# through their own module, and `revl_restore` compiles a snapshot's sources. So
# the STRUCTURAL half of the profile — the half that closes the exploit, a
# declared `@py`/`@ts` host body in agent-authored source — runs once, here, over
# every source string in a tool call's arguments, before any handler is
# dispatched. Structural (the parsed AST only), so nothing is lowered or run.
#
# The transitive `check_no_host_extern_reach` half needs the merged module
# closure and therefore still lives in the compiler, on the `_compile` verbs.

_SOURCE_ARGUMENTS = frozenset({"source", "candidate", "baseline", "replacement"})
_SOURCE_MAP_ARGUMENTS = frozenset({"modules", "files_content"})


def _collect_sources(node, out: list) -> None:
    """Every agent-supplied revl source string anywhere in a tool call's
    arguments, including the ones nested in a `revl_restore` snapshot."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _SOURCE_ARGUMENTS and isinstance(value, str):
                out.append(value)
            elif key in _SOURCE_MAP_ARGUMENTS and isinstance(value, dict):
                out.extend(v for v in value.values() if isinstance(v, str))
            else:
                _collect_sources(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_sources(item, out)


def _authoring_refusal(arguments: dict) -> dict | None:
    """The refusal payload when agent-authored source declares host code (or
    mints its own declassifier) under a closed authoring trust, else `None`.

    Parses each source and runs `admit_profile.enforce_source` — the same
    structural check `Gate.propose`'s decision compile runs, before lowering, so
    a host body is refused before it could ever be `exec`'d. Source the parser
    rejects is NOT refused here: a syntax error is the handler's own diagnostic
    to report, and pre-empting it would only make that error message worse."""
    profile = AUTHORING.profile()
    if profile is None:
        return None
    from ..admit_profile import enforce_source  # noqa: PLC0415
    from ..parser import Parser  # noqa: PLC0415

    sources: list = []
    _collect_sources(arguments, sources)
    for text in sources:
        try:
            program = Parser(text, "<candidate>.rvl").parse()
        except Exception:  # noqa: BLE001 — not parseable: the handler reports it
            continue
        try:
            enforce_source([program], profile)
        except RevlError as error:
            payload = report(error)
            payload["authoringTrust"] = "untrusted"
            payload["note"] = ("nothing was compiled, loaded or run — this "
                               "session's authoring trust is closed, so the "
                               "agent may compose granted services but may not "
                               "author host code (`revl mcp serve "
                               "--author-trust`, --provider, --grant)")
            return payload
    return None


# ---------------------------------------------------------------- helpers

# The host bodies the most recent agent-authored compile carried, and the ones
# the RUNNING composition carries. Only ever non-empty under
# `--author-trust trusted` (the untrusted profile refuses the source outright),
# and read by `_approval_required` so a class-(c) ticket cannot understate what
# a yes lets run.
_AUTHORED_HOST_BODIES: list = []
_LIVE_HOST_BODIES: list = []


def _host_bodies(ir: dict) -> list:
    """Every extern in `ir` that carries a verbatim host body, as
    `{extern, classification, backends}`. This is the surface G8 does not check
    inside (item 24) and the item-246 classifier never looks at."""
    out = []
    for extern in ir.get("externs") or []:
        bodies = extern.get("bodies") or {}
        if not bodies:
            continue
        out.append({"extern": extern.get("name"),
                    "classification": extern.get("class"),
                    "backends": sorted(bodies)})
    return sorted(out, key=lambda e: str(e["extern"]))


def compile_under_authoring(source: str | None, files: list[str] | None,
                            manifest: dict | None = None,
                            modules: dict | None = None,
                            replacing: tuple = (),
                            over_the_transport: bool = True) -> dict:
    """Compile inline source or paths through the same entry points the CLI
    uses, so the admission gate is literally the same code.

    THE ONE COMPILER DOOR FOR AGENT-SUPPLIED SOURCE. Every MCP verb that
    compiles something the agent sent over the transport goes through here —
    `revl_check`/`admit`/`plan`/`swap`/`load` via `_compile` below, and
    `revl_gauntlet`/`quarantine`/`repair`/`canary` through their own modules,
    which all call this function. A verb that reaches `compile_source` /
    `compile_files` directly gets NO authoring trust, and a candidate the gate
    refuses is then compiled — and, for the verbs that boot a scratch session,
    RUN — by a sibling verb. `tests/test_mcp_authority_gate.py` holds the guard:
    it reads every compiler call site under `src/revl/mcp/` out of the source
    and fails on one that neither passes `profile=` nor routes through here, so
    the NEXT door cannot be added silently.

    `over_the_transport` says whether the source came from the agent on the far
    end of an MCP session (the default, and the safe direction) or from the
    OPERATOR'S OWN machine. `revl bundle`, `revl canary`, `revl repair`,
    `revl quarantine` and `truc` reuse these modules as a library from the CLI,
    where the human running the command IS the author — the same reason jailed
    `files` compile unprofiled below. Those callers pass
    `over_the_transport=False` explicitly; nothing that reaches a `handle`
    dispatch may.

    Inline source never touches the disk: `compile_source` carries the
    ambient manifest and any in-memory `use` modules itself.

    What the CLI does NOT carry is a trust level: a human running `revl compile`
    IS the author. Here the author is the agent on the other end of the
    transport, so source that ARRIVED over the transport compiles under
    `AUTHORING.profile()` — the item-329 untrusted-author profile unless the
    operator has said otherwise. A refusal is a `RevlError` like any other
    admission refusal, so every caller's `except RevlError: return report(error)`
    already reports it correctly.

    `files` are the exception, and the path jail is what earns it: a `.rvl` file
    inside an operator-sanctioned root was not written by the agent — no MCP verb
    writes to disk, so the only way bytes reach a jailed path is a human putting
    them there. Those compile unprofiled, exactly as `Gate._compile_base` (the
    embedder's own sources) does while `Gate.propose` (the agent's source) does
    not. Inline `source` and `modules` always carry the profile: they arrived
    over the transport, whatever path they claim to sit at.

    With operator-sanctioned `providers` this is item 334's two-compile shape,
    not one compile with a flag, and for `Gate.propose`'s reasons:

      1. the DECISION compile runs the agent's source ALONE under the profile,
         with the providers in scope as `use` modules — so the candidate may
         compose their SERVICES while a direct reach into their host externs is
         still refused across the whole transitive closure;
      2. the COMPOSITION compile then builds what actually loads: the admitted
         candidate plus the providers as CO-ROOTS (a `use`-imported module
         contributes no components), unprofiled, because the candidate already
         passed (1) and the providers are the operator's own trusted host code.

    With no providers configured — the default — there is one compile, the
    decision compile, and its document is what loads.
    """
    # jailed `files` with no transport-carried text are operator-authored.
    inline = source is not None or bool(modules)
    profile = AUTHORING.profile() if inline and over_the_transport else None
    providers = dict(AUTHORING.providers or {})
    # operator-sanctioned modules ride UNDER the agent's own `modules`: an
    # agent-supplied entry can never displace a provider the operator granted.
    merged = dict(providers)
    if modules:
        merged.update({k: v for k, v in modules.items() if k not in merged})

    def _compile_once(prof, co_roots: dict) -> dict:
        if co_roots:
            virtual = {os.path.abspath(p): text for p, text in co_roots.items()}
            paths = list(virtual)
            if source is not None:
                candidate = os.path.abspath("<candidate>.rvl")
                virtual[candidate] = source
                paths.insert(0, candidate)
            elif files:
                paths = list(files) + paths
            else:
                raise ValueError("provide `source` or `files`")
            return compile_files(paths, manifest=manifest, replacing=replacing,
                                 sources=virtual, profile=prof)
        if source is not None:
            return compile_source(source, "<candidate>.rvl", manifest=manifest,
                                  replacing=replacing, modules=merged or None,
                                  profile=prof)
        if files:
            return compile_files(list(files), manifest=manifest,
                                 replacing=replacing, profile=prof)
        raise ValueError("provide `source` or `files`")

    if providers and profile is not None:
        _compile_once(profile, {})              # 1. the decision
        return _compile_once(None, providers)   # 2. what loads
    return _compile_once(profile, providers)


def _compile(source: str | None, files: list[str] | None,
             manifest: dict | None = None, modules: dict | None = None,
             replacing: tuple = ()) -> dict:
    """`compile_under_authoring` for the verbs THIS module dispatches, plus the
    one side effect only they want: remembering the host bodies the compile
    carried, so a class-(c) approval ticket cannot understate what a yes lets
    run. The sibling modules call `compile_under_authoring` directly — their
    scratch compiles must not overwrite what the live session authored."""
    ir = compile_under_authoring(source, files, manifest=manifest,
                                 modules=modules, replacing=replacing)
    over_the_transport = source is not None or bool(modules)
    global _AUTHORED_HOST_BODIES
    _AUTHORED_HOST_BODIES = (_host_bodies(ir)
                             if over_the_transport and AUTHORING.host_code
                             else [])
    return ir


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


# -- operator capabilities (roadmap item 55, docs/operator-capabilities.md) ---

def _refused_by_operator(decision) -> dict:
    """A management verb the bound operator may not call — refused with the
    policy-style why-trace, the running system untouched. This is the acting
    half of the operator profile: the same all-or-nothing refusal admission
    gives, pointed at the management plane instead of a component's reach."""
    from ..why import render as _render_why  # noqa: PLC0415
    return {
        "ok": False,
        "authorized": False,
        "note": "the running composition is untouched — the operator profile "
                "refused this management action",
        "authority": {"operator": decision.operator, "verb": decision.verb,
                      "allowed": False},
        "why": decision.why.to_json() if decision.why is not None else None,
        "diagnostics": [{
            "severity": "error", "code": "REVL", "category": "operator",
            "message": decision.message
                       + ("\n" + _render_why(decision.why) if decision.why else ""),
        }],
    }


def _stamp_authority(payload: dict, decision) -> None:
    """Record *who* on an authorized management action (item 27 audit story):
    stamp the operator identity into the result, and — for a verb that returns
    a causal trace — prepend a trace event naming the operator, so "what
    changed and on whose authority" is one query over the same trace.

    Carried in the mcp layer rather than in `why_runtime`: the operator is a
    property of the session driving the transition, not of the lifecycle
    transition itself, so the runtime trace stays authority-agnostic and this
    stays additive."""
    if not isinstance(payload, dict):
        return
    payload["authority"] = {"operator": decision.operator, "verb": decision.verb,
                            "subjects": list(decision.subjects), "allowed": True}
    trace = payload.get("trace")
    if isinstance(trace, list):
        trace.insert(0, {"channel": "operator", "subject": decision.operator,
                         "detail": f"authorized `{decision.verb}`"
                                   + (f" on {', '.join(decision.subjects)}"
                                      if decision.subjects else "")})


# -- component leases (roadmap item 61, docs/component-leases.md) -------------

def _refused_by_lease(refusal) -> dict:
    """A swap refused because it would replace a component another operator
    leases, under an enforcing policy (item 33). Same untouched-system,
    why-trace shape `_refused_by_operator` gives for the management plane —
    pointed at the workspace instead."""
    from ..why import render as _render_why  # noqa: PLC0415
    return {
        "ok": False,
        "admitted": False,
        "swapped": False,
        "authorized": False,
        "note": "the running composition is untouched — a lease held by another "
                "operator, enforced by policy, refused this replacement",
        "lease": {"component": refusal.component, "heldBy": refusal.heldBy,
                  "expiry": refusal.expiry, "operator": refusal.holder},
        "why": refusal.why.to_json(),
        "diagnostics": [{
            "severity": "error", "code": "REVL", "category": "lease",
            "message": refusal.message + "\n" + _render_why(refusal.why),
        }],
    }


def _record_lease_trace(events: list[dict]) -> None:
    """Ride the lease events into the causal trace (item 27): when a composition
    is loaded, append them to the driver's event stream so `revl_state`'s trace
    — and any trace query over it — carries "who held what lease when" beside
    the lifecycle story."""
    driver = getattr(SESSION, "_driver", None)
    if driver is not None and events:
        driver.events.extend(events)


def _tool_lease(arguments: dict) -> dict:
    """Claim, renew, or release an operator-scoped, TTL-bound lease on a
    component name — the multi-agent workspace primitive (item 61).

    A lease is not a lock: the running component keeps serving. It governs who
    may *replace* it — advisory at plan/swap by default, refused at admission
    under a policy that enforces leases. The holder is the session's operator
    identity (item 55)."""
    action = (arguments.get("action") or "claim").lower()
    component = arguments.get("component")
    if action not in ("claim", "renew", "release"):
        return _session_error(
            f"unknown lease action {action!r} — one of claim, renew, release")
    if not component:
        return _session_error("`component` is required — a component *name* to "
                              "lease (leases govern who may replace it)")
    holder = _leases.holder_identity(SESSION)
    book = SESSION.leases
    try:
        if action == "release":
            released = book.release(component, holder)
        elif action == "renew":
            book.renew(component, holder, arguments.get("ttl"))
        else:
            book.claim(component, holder, arguments.get("ttl"))
    except _leases.LeaseError as error:
        return _session_error(str(error))
    events = book.drain_events()
    _record_lease_trace(events)
    payload = {
        "ok": True,
        "action": action,
        "holder": holder,
        "component": component,
        "leases": book.document(),
        "leaseEvents": events,
    }
    if action == "release":
        payload["released"] = released
    return payload


def _origin(arguments: dict) -> dict:
    """The admission inputs of a load/swap, kept so the composition can later
    be snapshotted for re-admission (docs/persistence.md)."""
    origin = {}
    for key in ("source", "files", "modules"):
        value = arguments.get(key)
        if value is not None:
            origin[key] = value
    return origin


def _remember_live_host_bodies() -> None:
    """Re-read the host bodies the LIVE composition carries, so a later
    class-(c) ticket can name them. Called after every completed tool call, so
    load / swap / edit / restore / undo / rollback / unload are all covered by
    one line rather than five that could drift apart. Empty under a closed
    authoring trust: the profile refused every agent-authored host body, so the
    only bodies that can be live came from the operator's own jailed files and
    there is nothing to warn about."""
    global _LIVE_HOST_BODIES
    ir = getattr(SESSION, "ir", None)
    _LIVE_HOST_BODIES = (_host_bodies(ir)
                         if ir and AUTHORING.host_code else [])


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
    except ApprovalRequired:
        # item 246: the class-(c) ticket two-step is a RESULT, shaped by
        # `handle`. Re-raised past the catch-all below, which used to swallow it
        # into an opaque "ApprovalRequired: ..." internal-fault string — the
        # operator never saw the ticket at all over this verb.
        raise
    except Exception as exc:  # the callee raised — that is a result, not a crash
        return _session_error(f"{type(exc).__name__}: {exc}", raised=True,
                              trace=SESSION.state().get("trace", []))


def _tool_swap(arguments: dict) -> dict:
    """Admit a candidate against what is running, then hot-swap it in. A
    rejected candidate changes nothing — that is the whole point.

    The source may be sent inline (`source`/`files`/`modules`) *or* referred to
    by name: with none of those supplied, swap re-admits the source the server
    already holds for the running composition (the audit's #1 finding — an
    agent that just edited server-side with `revl_edit`, or wants to re-admit
    the running generation, need not re-serialize the whole file). Full inline
    source is still accepted, unchanged.
    """
    if not SESSION.loaded:
        return _session_error("nothing is loaded — call revl_load first")
    replacing = tuple(arguments.get("replacing") or ())

    # component leases (item 61): under a policy that enforces leases, refuse a
    # swap that would replace a component another operator holds — the acting
    # half of the workspace, all-or-nothing like admission, running comp
    # untouched. Advisory-by-default leases never reach here (check_swap returns
    # None unless the policy enforces).
    refusal = _leases.check_swap(SESSION, arguments)
    if refusal is not None:
        return _refused_by_lease(refusal)

    # quarantine tier (item 45): under a policy that declares `quarantine
    # required`, an untrusted candidate must prove itself in the wasm sandbox
    # before it may be swapped into a hosted tier — refused here (running comp
    # untouched) if it did not pass and no operator holds bypass authority.
    # No such policy => gate_swap returns None and the default path pays nothing
    # (docs/quarantine-tier.md).
    quarantined = _quarantine.gate_swap(SESSION, arguments)
    if quarantined is not None:
        return quarantined

    inline = any(arguments.get(k) is not None for k in ("source", "files", "modules"))
    if not inline:
        return _swap_server_side(replacing)

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


def _swap_server_side(replacing: tuple) -> dict:
    """Swap the source the session already holds — no inline source resent."""
    vs = _edit.virtual_source(SESSION)
    if vs.get("source") is None:
        return _session_error(
            "no server-side source to swap by name — this composition was not "
            "loaded from inline source, so there is nothing the session can "
            "re-admit without you passing `source`/`files`")
    try:
        _edit.compile_virtual(vs, manifest=SESSION.ir, replacing=replacing)
    except RevlError as error:
        rejected = report(error)
        rejected["admitted"] = False
        rejected["swapped"] = False
        rejected["note"] = "the running composition is untouched"
        return rejected
    try:
        full = _edit.compile_virtual(vs)
    except RevlError as error:
        rejected = report(error)
        rejected["admitted"] = True
        rejected["swapped"] = False
        rejected["note"] = ("the server-side source admits but is not a complete "
                            "composition on its own")
        return rejected
    try:
        state = SESSION.swap(full, origin=_edit._origin_from(vs))
    except SessionError as error:
        return _session_error(str(error))
    return {"ok": True, "admitted": True, "swapped": True,
            "fromServerSide": True, **_summary(full), **state}


def _tool_edit(arguments: dict) -> dict:
    """Patch the server-side source of the running composition and re-admit —
    deltas, not documents (roadmap item 50, docs/mcp-bridge.md)."""
    try:
        return _edit.apply_edit(SESSION, arguments)
    except _edit.EditError as error:
        return _session_error(str(error), edited=False, swapped=False)
    except SessionError as error:
        return _session_error(str(error))


def _tool_rollback(_arguments: dict) -> dict:
    try:
        return {"ok": True, **SESSION.rollback()}
    except SessionError as error:
        return _session_error(str(error))


def _tool_undo(arguments: dict) -> dict:
    """Return to an earlier generation through the retained history (item 65).

    With no `to`, undoes to generation N−1; `to` names any still-retained
    generation. The undo is admitted through the SAME gate a swap runs: a
    target the current checker rejects is a refusal *result* (the running
    composition is untouched), never a bypass. The dossier — what unloads, what
    state drops, and the interim boundary crossings no undo can un-emit — rides
    along either way (docs/generation-history.md)."""
    if not SESSION.loaded:
        return _session_error("nothing is loaded — call revl_load first")
    try:
        result = SESSION.undo(arguments.get("to"))
    except SessionError as error:
        return _session_error(str(error))
    # a gate refusal is a result, not an error: surface it as ok:False with the
    # diagnostic, matching how revl_restore reports a rejected re-admission.
    if result.get("refused"):
        return {"ok": False, **result}
    return {"ok": True, **result}


def _tool_unload(_arguments: dict) -> dict:
    try:
        return {"ok": True, **SESSION.unload()}
    except SessionError as error:
        return _session_error(str(error))


def _tool_commit(_arguments: dict) -> dict:
    """Step 1 of the two-step session commit (item 245): enumerate the manifest.
    The `summary` is the human's one-line prompt; the `hash` binds the gate
    target. Nothing crosses yet — call revl_commit_confirm with the hash."""
    try:
        return {"ok": True, "manifest": SESSION.commit()}
    except SessionError as error:
        return _session_error(str(error))


def _tool_commit_confirm(arguments: dict) -> dict:
    """Step 2 of the session commit: flush the deferral queue (FIFO), discharge
    the witnessed escrow, mark it durable. A hash that no longer matches the
    live gate target is refused with a fresh manifest (a result, not an error)."""
    manifest_hash = arguments.get("hash")
    if not manifest_hash:
        return _session_error("provide `hash` — the manifest hash revl_commit "
                              "returned, binding exactly what will fire")
    try:
        result = SESSION.commit_confirm(manifest_hash)
    except SessionError as error:
        return _session_error(str(error))
    if result.get("refused"):
        return {"ok": False, **result}
    return {"ok": True, **result}


def _tool_fork(arguments: dict) -> dict:
    """Step 1 of the two-step, hash-bound session fork (item 250): ENUMERATE the
    honest partition of the tail above step `at` and return it with a `hash`.
    Nothing is rewound yet, no branch is minted. The crossed-emission and
    would-cross-on-rewind residue MUST be seen and acknowledged through the hash
    before `revl_fork_confirm` performs the rewind."""
    if "at" not in arguments:
        return _session_error("`at` is required — the step k to fork at "
                              "(-1 rewinds the whole tail)")
    try:
        return {"ok": True, **SESSION.fork(arguments["at"],
                                           arguments.get("component"))}
    except SessionError as error:
        # a refused fork (a KIND_OPAQUE tail, a non-idempotent span, a committed
        # boundary below k) is a RESULT the caller reads, not a crash
        return _session_error(str(error), refused=True)


def _tool_fork_confirm(arguments: dict) -> dict:
    """Step 2 of the session fork (item 250): re-derive the hash, refuse on drift,
    then run the scope-gated rewind to k, drop the parent queue, FREEZE the
    parent, snapshot, and mint the branch. On success the branch becomes the live
    session — the only continuation over the shared, rewound workspace."""
    global SESSION
    fork_hash = arguments.get("hash")
    if not fork_hash:
        return _session_error("provide `hash` — the fork hash revl_fork returned, "
                              "binding exactly the rewound span and residue")
    try:
        result = SESSION.fork_confirm(fork_hash)
    except SessionError as error:
        return _session_error(str(error))
    if result.get("refused"):
        return {"ok": False, **{k: v for k, v in result.items()
                                if k != "branchSession"}}
    # the branch is the live continuation: rebind the server's session to it so
    # every subsequent verb drives the branch, and never serialize the live object
    branch = result.pop("branchSession", None)
    if branch is not None:
        SESSION = branch
    return {"ok": True, **result}


def _approval_required(exc: ApprovalRequired) -> dict:
    """Shape a class-(c) refusal into the ticket two-step response (item 246).
    The call/load/swap did NOT fire: the ticket names what a yes would mean, and
    `revl_approve(hash)` mints the standing approval that lets the identical
    re-issue fire once.

    Under `--author-trust trusted` the ticket also has to stop lying. Its
    `capabilities` / `classCCapabilities` are derived from DECLARED extern facts
    (`approval.ClassMap._classify_direct`) and say nothing about what the host
    BODY does — an `emission[notify]` body that also reads a `.env` yields a
    ticket naming `notify` and nothing else. So when the running (or
    just-compiled) composition carries agent-authored host bodies, the ticket
    carries `unreviewedHostCode` naming them and a warning saying the declared
    capabilities are not a bound on what a yes lets run. The ticket `hash` is
    untouched (the fields land on a COPY, after the hash the outstanding-ticket
    table is keyed by), so approve/consume is byte-identical."""
    ticket = exc.ticket
    bodies = _LIVE_HOST_BODIES or _AUTHORED_HOST_BODIES
    if bodies:
        ticket = dict(ticket)
        ticket["unreviewedHostCode"] = bodies
        ticket["unreviewedHostCodeWarning"] = (
            f"this composition carries {len(bodies)} agent-authored host "
            f"{'body' if len(bodies) == 1 else 'bodies'} that the gate did not "
            f"review (G8, item 24: the gate does not sandbox host code). The "
            f"capabilities above are DECLARED facts, not a bound on what the "
            f"bodies do — approving this crossing also lets that code run "
            f"whatever it contains. This server is running "
            f"`--author-trust trusted`; the default refuses agent-authored "
            f"host code outright.")
    return two_step_payload(
        ticket,
        how_to_approve="Relay the ticket, then call revl_approve with its "
                       "`hash`; the identical re-issue then fires once and "
                       "consumes the approval.")


def _tool_approve(arguments: dict) -> dict:
    """Say YES to a class-(c) crossing (item 246 / roadmap item 344). Two shapes,
    one verb:

      * SINGLE-USE, EXACT-HASH (Slice 1): `hash` alone mints a single-use
        approval bound to that one ticket; the identical re-issue fires once and
        consumes it. An unknown hash is refused by the outstanding-ticket table.
      * SESSION-SCOPED STANDING GRANT (item 344, fork b): a `capability` and/or a
        `uses`/`ttlMs` bound mints a grant keyed by the capability's semantic
        identity that per-call class-(c) crossings consume against — taking the
        shell-escape shape (n repeat crossings) from n prompts to one mint. The
        grant may be named from an outstanding ticket (`hash` + `uses`/`ttlMs`)
        or proactively against a `capability`.

    Gated by the `approve` verb (item 55), so an operator profile scopes who may
    say yes."""
    ticket_hash = arguments.get("hash")
    capability = arguments.get("capability")
    uses = arguments.get("uses")
    ttl_ms = arguments.get("ttlMs")
    # item 344: any of `capability`/`uses`/`ttlMs` selects the standing-grant
    # path; a bare `hash` keeps the Slice-1 single-use behaviour byte-for-byte.
    if capability is not None or uses is not None or ttl_ms is not None:
        try:
            return {"ok": True, **SESSION.mint_standing_grant(
                ticket_hash=ticket_hash, capability=capability,
                uses=uses, ttl_ms=ttl_ms)}
        except SessionError as error:
            return _session_error(str(error))
    if not ticket_hash:
        return _session_error("provide `hash` — the ticket hash from the "
                              "approvalRequired response — or a `capability` "
                              "(+ `uses`/`ttlMs`) to mint a standing grant")
    try:
        return {"ok": True, **SESSION.approve_ticket(ticket_hash)}
    except SessionError as error:
        return _session_error(str(error))


def _tool_revoke(arguments: dict) -> dict:
    """Retire a session-scoped standing grant EARLY (roadmap item 379), the
    symmetric partner of `revl_approve`'s item-344 standing-grant mint. Withdraw
    consent BEFORE the grant's TTL/uses lapse, so the next class-(c) crossing it
    covered prompts again:

      * `capability` revokes EVERY live standing grant for that capability (the
        same key `revl_approve` mints against — a token when scoped by item 343,
        the extern name otherwise);
      * `requestId` revokes one specific grant (the id the mint returned).

    Revoking a capability/id with no live grant is a clean typed no-op
    (`count: 0`), not an error — idempotent. Gated by the `approve` operator verb
    (item 55): withdrawing consent is the same authority as granting it."""
    capability = arguments.get("capability")
    request_id = arguments.get("requestId")
    try:
        return {"ok": True, **SESSION.revoke_standing_grant(
            capability=capability, request_id=request_id)}
    except SessionError as error:
        return _session_error(str(error))


def _tool_distillation_offers(_arguments: dict) -> dict:
    """Fold this session's approval ledger to candidate `AutoApproveRule` offers
    (roadmap item 251). Read-only and PROPOSE-ONLY - it applies no policy, so it
    is ungated. Scoped to the caller's own attributed grants."""
    try:
        return {"ok": True, **SESSION.distillation_offers()}
    except SessionError as error:
        return _session_error(str(error))


def _tool_apply_distillation(arguments: dict) -> dict:
    """Install a distilled offer as a live `AutoApproveRule` (roadmap item 251),
    recording a `distillation-applied` WAL fact with the attribution. Gated by the
    `approve` operator verb (item 55): installing a standing auto-approve is the
    same authority as granting the underlying yeses."""
    offer_id = arguments.get("offerId")
    if not offer_id:
        return _session_error("provide `offerId` - the id from "
                              "`distillation_offers`")
    try:
        return {"ok": True, **SESSION.apply_distillation(offer_id)}
    except SessionError as error:
        return _session_error(str(error))


def _tool_revoke_distillation(arguments: dict) -> dict:
    """Retire an applied distilled rule from the live policy (roadmap item 251),
    recording a `distillation-revoked` WAL fact - the next matching crossing
    prompts again (fail-closed). Gated by the `approve` operator verb (item 55)."""
    rule = arguments.get("rule")
    if not rule:
        return _session_error("provide `rule` - the rule text (or its canonical "
                              "DSL) to revoke")
    try:
        return {"ok": True, **SESSION.revoke_distillation(rule)}
    except SessionError as error:
        return _session_error(str(error))


def _tool_abort(_arguments: dict) -> dict:
    """Abort the session (item 245): drop the deferral queue (never fired),
    replay the witnessed inverses, prove a clean world."""
    try:
        return {"ok": True, **SESSION.abort()}
    except SessionError as error:
        return _session_error(str(error))


def _tool_estop(arguments: dict) -> dict:
    """E-STOP the running composition (item 443, docs/design/443-estop.md).

    Not `revl_abort` with a shorter name: abort is a VERDICT on the work and
    pays for a full two-phase LIFO unwind; estop is the operator's EMERGENCY
    and runs nothing at all. It stops dispatching new crossings, reports what
    was in flight, and leaves every registered entry owed."""
    try:
        return {"ok": True, **SESSION.estop(
            arguments.get("reason") or "operator halt",
            arguments.get("operator") or getattr(
                getattr(SESSION, "operator", None), "token", None))}
    except SessionError as error:
        return _session_error(str(error))


def _tool_estop_report(_arguments: dict) -> dict:
    """Read the E-Stop inventory back without touching the world (item 443)."""
    try:
        return {"ok": True, **SESSION.estop_report()}
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
    dossier = _gauntlet.run(SESSION, arguments)
    # item 290, §4: retain an admissible session dossier so a `mcp requires
    # evidence [gauntlet admissible]` admission can read this operator-run
    # evidence (no attestation root needed — the operator produced it here).
    if hasattr(SESSION, "record_gauntlet"):
        SESSION.record_gauntlet(dossier)
    return dossier


def _tool_quarantine(arguments: dict) -> dict:
    """Quarantine a candidate: grade it with the gauntlet, then compile it to a
    standard wasm component and run its lifecycle + fault battery in wasmtime's
    component-model sandbox — where an escape is a trap, not an incident. Returns
    a report whose `verdict` is passed | trapped | rejected | deferred |
    unavailable, plus the policy admission decision. The live composition is
    never touched (docs/quarantine-tier.md)."""
    if arguments.get("source") is None and not arguments.get("files"):
        return _session_error("provide `source` or `files` — quarantine proves "
                              "a candidate component in the sandbox")
    return _quarantine.run(SESSION, arguments)


def _tool_repair(arguments: dict) -> dict:
    """The repair loop (roadmap item 62, docs/repair-loop.md): from a fault's
    causal trace, run regenerate/reuse -> gauntlet -> policy -> widening-ack ->
    hot-swap unattended, within a self-repair policy, and return the incident
    dossier. A candidate that would WIDEN the composition's outward reach pauses
    for a human ack instead of swapping; an ineligible component halts. The
    running composition is mutated only by the final swap, only when every gate
    is green (the loop orchestrates the landed machinery; it reimplements none)."""
    if not arguments.get("component"):
        return _session_error("`component` is required — the faulting component "
                              "to repair")
    # component leases (item 61): the loop's remediation step calls
    # `Session.swap` itself, so it never reached the check `_tool_swap` runs. A
    # repair is a swap; under a policy that enforces leases it may no more
    # replace a component another operator holds than `revl_swap` may. Skipped
    # for the `apply:false` rehearsal, which swaps nothing.
    if _operator.composed_applies("revl_repair", arguments):
        refusal = _leases.check_swap(
            SESSION, _operator.swap_arguments("revl_repair", arguments))
        if refusal is not None:
            return _refused_by_lease(refusal)
    return _repair.run_repair(SESSION, arguments)


# -- composition persistence (docs/persistence.md) ------------------------

def _tool_snapshot(_arguments: dict) -> dict:
    """Capture the live composition as re-admittable JSON (sources + manifest).

    This is not a runtime dump: it is the inputs a fresh boot would need to
    put the same composition back through the admission gate."""
    try:
        return {"ok": True, "snapshot": SESSION.snapshot()}
    except SessionError as error:
        return _session_error(str(error))


def _restore_authoring_refusal(snap) -> dict | None:
    """The full decision compile for a `revl_restore`, or `None` when the
    snapshot's sources admit under the session's authoring trust. Mirrors
    `persist._recompile`'s inputs exactly (virtual file contents included) so the
    decision is taken over the same text the restore will compile, and adds the
    profile `persist` deliberately does not carry (its other caller is the
    operator's own `--restore`)."""
    profile = AUTHORING.profile()
    if profile is None or not isinstance(snap, dict):
        return None
    sources = snap.get("sources") or {}
    if not isinstance(sources, dict):
        return None
    try:
        source = sources.get("source")
        if source is not None:
            compile_source(source, "<snapshot>.rvl",
                           modules=sources.get("modules") or None, profile=profile)
        elif sources.get("files"):
            virtual = {os.path.abspath(path): text for path, text
                       in (sources.get("files_content") or {}).items()}
            compile_files(list(sources["files"]), sources=virtual or None,
                          profile=profile)
    except RevlError as error:
        payload = report(error)
        payload["restored"] = False
        payload["reAdmitted"] = False
        payload["authoringTrust"] = "untrusted"
        payload["note"] = ("nothing was restored — a snapshot is agent-supplied "
                           "source and admits under the same authoring trust "
                           "`revl_load` does")
        return payload
    return None


def _tool_restore(arguments: dict) -> dict:
    """Re-admit a snapshot into an empty session by replaying admission.

    A component the *current* checker rejects fails the restore loudly with
    its diagnostic — a snapshot cannot smuggle a now-rejected component past a
    newer checker. The running system is untouched on failure.

    The snapshot document is AGENT-SUPPLIED over the transport, so it is
    authored source like any other and gets the same decision compile
    `Gate.propose` runs before it swaps: a snapshot may no more smuggle a host
    body past the authoring trust than `revl_load` may. (The operator's own
    `revl mcp serve --restore` path is not agent-supplied and is unaffected.)"""
    snap = arguments.get("snapshot")
    if snap is None:
        return _session_error("`snapshot` (a document from revl_snapshot) is required")
    refusal = _restore_authoring_refusal(snap)
    if refusal is not None:
        return refusal
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


# -- verified canary (docs/verified-canary.md, roadmap item 59) ------------


def _tool_canary(arguments: dict) -> dict:
    """Progressive delivery for one slice: admit a candidate against the running
    composition, compare its recorded world to the baseline's on the designated
    realm, and prove the revert clean (survivors + residue). Decides; the swap
    acts. Reuses the swap admission gate, realms, replay and erase_report."""
    from . import canary as _canary  # noqa: PLC0415 — keep the module boundary
    baseline = _compile(arguments.get("baseline"), arguments.get("baselineFiles"))
    realm = arguments.get("realm")
    if not realm:
        return {"ok": False, "error": "`realm` (the designated slice) is required"}
    report = _canary.run_canary(
        baseline,
        candidate_files=arguments.get("candidateFiles"),
        candidate_source=arguments.get("candidate"),
        realm=realm,
        provider=arguments.get("provider"),
        promote_to=arguments.get("promoteTo"),
        prove_residue=arguments.get("proveResidue", True),
    )
    return report


# -- session-bound query modes (docs/queries.md §9) ------------------------


def _tool_live_query(arguments: dict) -> dict:
    """The five query verbs, answered against the LIVE session's post-swap
    state instead of a compiled-from-source IR (query.live_query)."""
    if not SESSION.loaded:
        return _session_error("nothing is loaded — a live query answers against "
                              "the running composition; call revl_load first")
    verb = arguments.get("verb")
    if verb not in Q._VERB_FN:
        return _session_error(f"unknown verb {verb!r}; one of "
                              + ", ".join(sorted(Q._VERB_FN)))
    live_state = SESSION.live_state()
    if verb == "drift":
        service = arguments.get("service") or arguments.get("target")
        if not service:
            return _session_error("`service` is required for the drift verb")
        return Q.live_query(
            SESSION.ir, verb, live_state, service,
            gains=list(arguments.get("gains") or []),
            losses=list(arguments.get("loses") or arguments.get("losses") or []))
    arg = arguments.get("target") or arguments.get("component")
    if not arg:
        return _session_error("`target` (emits-to/depends-on) or `component` "
                              "(withdraw/reaches) is required")
    return Q.live_query(SESSION.ir, verb, live_state, arg)


def _tool_history_emitted_between(arguments: dict) -> dict:
    """which emissions crossed between steps X and Y? — over the live session's
    recording, or an inline one (query.emitted_between)."""
    frm, to = arguments.get("from"), arguments.get("to")
    if frm is None or to is None:
        return _session_error("`from` and `to` (step indices) are required")
    timeline = arguments.get("timeline")
    if timeline is None:
        if not SESSION.loaded or SESSION.recorder is None:
            return _session_error("no recorded timeline — revl_load with "
                                  "`record: true`, or pass an inline `timeline`")
        try:
            timeline = SESSION.timeline(None)
        except SessionError as error:
            return _session_error(str(error))
    return Q.emitted_between(timeline, frm, to, arguments.get("component"))


def _tool_history_lifetime(arguments: dict) -> dict:
    """everything a component touched during its life — the recording for the
    effects, an item-27 lifecycle trace for the span (query.lifetime)."""
    component = arguments.get("component")
    if not component:
        return _session_error("`component` is required")
    timeline = arguments.get("timeline")
    if timeline is None and SESSION.loaded and SESSION.recorder is not None:
        try:
            timeline = SESSION.timeline(None)
        except SessionError:
            timeline = None
    trace = arguments.get("trace")
    trace_file = arguments.get("traceFile")
    if trace is None and trace_file:
        from .. import why_runtime as wr
        try:
            trace = wr.read_trace(trace_file)
        except (OSError, ValueError) as error:
            return _session_error(f"cannot read trace {trace_file}: {error}")
    if timeline is None and trace is None:
        return _session_error("no recorded run to read — load with `record: "
                              "true`, or pass `trace`/`traceFile`/`timeline`")
    return Q.lifetime({"trace": trace, "timeline": timeline}, component)


def _tool_check(arguments: dict) -> dict:
    try:
        ir = _compile(arguments.get("source"), arguments.get("files"),
                      modules=arguments.get("modules"))
    except RevlError as error:
        return report(error)
    # `holes` is the agent's own remaining work on this draft: every
    # placeholder it wrote that still has a type and no implementation
    # (docs/holes.md). `ok: true` with a non-empty `holes` means "checked,
    # and not admissible until these are closed". Each obligation carries a
    # `fillSpec` — the expected type, the emission upper bound, the in-scope
    # bindings and the reachable service signatures the checker already knew at
    # that position — so the hole can be filled directly (docs/holes.md §8).
    holes = fillspec.enrich(ir) if ir.get("holes") else []
    return {"ok": True, **_summary(ir), "boundary": _boundary_of(ir),
            "holes": holes}


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
    # Honor `replacing` on the FIRST (and only) compile — mirrors _tool_swap
    # (line 308-311). A provider swap ships the candidate providing a key the
    # named components already provide; excluding those from the ambient lets
    # the candidate legitimately take the key over. Compiling without
    # `replacing` first raised a false G2 ("provided by both") and the second,
    # files-only re-compile never ran (and never fired for `source`
    # candidates at all). A genuine G2 — the candidate colliding with a
    # NON-replaced provider — still surfaces here.
    try:
        ir = _compile(arguments.get("source"), arguments.get("files"),
                      manifest=running, replacing=replacing)
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
    # component leases (item 61): advise — never block — when this swap would
    # replace a component another operator leases. Surfaced so an agent sees
    # the race before it swaps; the plan itself is unchanged.
    payload = {**result, "against": against,
               "note": "nothing was admitted, swapped or written — this is a plan"}
    warnings = _leases.advise_plan(SESSION, arguments)
    if warnings:
        payload["leaseWarnings"] = warnings
    return payload


def _tool_ship(arguments: dict) -> dict:
    """Fuse check -> admit -> plan (-> swap, when `apply`) into one early-exit
    call — the token-saving ship path (docs/token-economy.md, roadmap item 50).

    Instead of the four round-trips the audit's finding #3 measures, an agent
    calls this once: the stages run in order, stop at the first failure (no
    wasted work), and the manifest defaults to the running composition this
    server already holds — so the running IR is not re-sent to admit against
    it. The orchestration lives in `ship.py`; this is the thin wiring that
    hands it the existing per-stage handlers and the live session."""
    return _ship.ship(arguments,
                      check=_tool_check, admit=_tool_admit, plan=_tool_plan,
                      swap=_tool_swap, session=SESSION)


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


# the prose summary lives in grammar_summary.py, alongside the dense
# prompt-pinnable grammar (roadmap item 346) — shared so `revl grammar
# --prompt` (a plain CLI command) never has to import this module's session
# machinery just to print a string.
_GRAMMAR = _grammar_summary.PROSE_GRAMMAR


def _resolve_registry_dir(arguments: dict) -> str:
    """Where the git-backed registry lives: an explicit `registry` argument,
    then $REVL_REGISTRY, then the `registry/` directory shipped in the repo."""
    import os  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    return (arguments.get("registry")
            or os.environ.get("REVL_REGISTRY")
            or str(Path(__file__).resolve().parents[3] / "registry"))


def _tool_resolve(arguments: dict) -> dict:
    """Rank the registry's §5-admissible providers for a need — the read half
    of the component registry (roadmap item 49, docs/registry.md §2). Source and
    manifest ride inline so need -> resolve -> admit is two round-trips."""
    from ..registry import Registry, resolve as registry_resolve  # noqa: PLC0415

    need = arguments.get("need")
    if need is None:
        return _session_error(
            "`need` is required — a `service` declaration (source), a hole's "
            "fill spec (from revl_check), or a service shape object")
    registry_dir = _resolve_registry_dir(arguments)
    from pathlib import Path  # noqa: PLC0415
    if not (Path(registry_dir) / "index.json").exists():
        # §3: absent entirely when no index is configured — not an error.
        return {"ok": True, "query": "resolve", "candidates": [],
                "assumptions": [f"no registry index at {registry_dir}; "
                                "set `registry` or $REVL_REGISTRY"]}
    verify_required = bool(arguments.get("verifyRequired"))
    trusted_publishers = tuple(arguments.get("trustedPublishers") or ())
    # item 296: probe the candidates the §5 filter refused for a SAFE adapter.
    # On by default and reported, never wired - the proposal is source the
    # author commits, and the answer says so.
    adapt = arguments.get("adapt")
    adapt = True if adapt is None else bool(adapt)
    adapt_opt_ins = arguments.get("adaptOptIns") or None
    if adapt_opt_ins is not None and not isinstance(adapt_opt_ins, dict):
        return _session_error(
            "`adaptOptIns` is the author's `adapt` opt-in map `D`, keyed by "
            "method name (the same JSON `revl adapt --adapt` takes), e.g. "
            '{"get": {"return": {"merge": "total"}}}')
    from ..attest import resolve_key  # noqa: PLC0415

    key = None
    if verify_required:
        # a verify-required resolve needs the signer secret to cryptographically
        # check the attestations it gates on: no key is a hard error.
        try:
            key = resolve_key(None)
        except RevlError as error:
            return report(error)
    else:
        # `verifyRequired` stays off by default: most environments configure no
        # signing key, and an agent-facing resolve that errored out without one
        # would be unusable. But whenever a key IS configured, USE it. An
        # attestation nobody checked vouches for nothing, so passing the key is
        # what lets a candidate's evidence count for anything at all; absent, the
        # resolve still runs and the evidence ranks as the unverified claim it
        # is, rather than being read as proof.
        try:
            key = resolve_key(None)
        except RevlError:
            key = None
    # item 290 §5: an optional boundary policy turns on the `wouldBeRefused`
    # marker. It is a PREDICTION for the agent's benefit and gates nothing here —
    # but a policy that fails to LOAD stops the call, because resolving with the
    # prediction silently switched off is the same false clearance the marker
    # exists to prevent.
    policy = None
    policy_path = arguments.get("policy")
    if policy_path:
        from ..policy import load_policy  # noqa: PLC0415
        try:
            policy = load_policy(policy_path)
        except RevlError as error:
            return report(error)
        except OSError as error:
            return _session_error(f"`policy` could not be read: {error}")
    try:
        registry = Registry.from_dir(registry_dir)
        return registry_resolve(registry, need,
                                manifest=arguments.get("manifest"),
                                limit=int(arguments.get("limit", 5)),
                                verify_required=verify_required, key=key,
                                trusted_publishers=trusted_publishers,
                                adapt=adapt, adapt_opt_ins=adapt_opt_ins,
                                policy=policy)
    except RevlError as error:
        return report(error)


def _tool_grammar(_arguments: dict) -> dict:
    # `fixes` is the `revl explain` payload: for every guarantee, the rewrite
    # that satisfies it — so an agent that gets a code back can act without
    # a second round trip
    return {"ok": True, "grammar": _GRAMMAR, "guarantees": GUARANTEES,
            "fixes": FIXES}


# -- the authoring toolbox as MCP tools (roadmap item 345) -------------------
#
# `revl scaffold` / `revl fmt` / `revl explain` were CLI/compiler-only: an
# agent harness had to shell out or reinvent the scaffold-first flow. These
# three handlers are thin wrappers over the same functions the CLI calls
# (`scaffold.py`, `formatter.py`, `diagnostics.explain`) — no new logic, just
# the MCP projection, matching every other tool in this module.

def _tool_scaffold(arguments: dict) -> dict:
    """revl_scaffold: a typed, holed component skeleton from a spec
    (docs/scaffold.md) — the MCP twin of `revl scaffold --json`. Returns the
    skeleton AND every open hole's fillSpec in one call, so an agent scaffolds
    -> fills -> admits without a second round-trip."""
    from ..scaffold import ScaffoldError, build_spec, scaffold_document

    service = arguments.get("service")
    if not service:
        return _session_error("`service` is required")
    try:
        spec = build_spec(
            service=service,
            provides=arguments.get("provides"),
            component=arguments.get("component"),
            requires=arguments.get("requires") or [],
            capabilities=arguments.get("capabilities") or [],
            methods=arguments.get("methods") or [],
            emits=arguments.get("emits") or [],
            config=arguments.get("config") or [],
            effect=arguments.get("effect", True),
            resource_type=arguments.get("resource"),
        )
    except ScaffoldError as error:
        return _session_error(str(error))
    filename = arguments.get("filename") or f"{spec.component}.rvl"
    return scaffold_document(spec, filename)


def _tool_fmt(arguments: dict) -> dict:
    """revl_fmt: canonical formatting (or `migrate: true` for the 1.x `$`
    interpolation rewrite) over inline source — the MCP twin of `revl fmt`,
    taking text in and text out instead of touching the filesystem. Gated by
    the same IR-equivalence proof the CLI runs: a rewrite that would change
    what the compiler sees is refused, never silently written."""
    from ..fmt import migrate_source
    from ..formatter import FormatError, ir_equivalent, format_source

    source = arguments.get("source")
    if source is None:
        return _session_error("`source` is required")
    filename = arguments.get("filename") or "<source>"
    migrate = bool(arguments.get("migrate"))
    warnings: list[str] = []

    if migrate:
        try:
            rewritten, warnings = migrate_source(source, filename)
        except RevlError as error:
            return _session_error(f"cannot migrate: {error}")
    else:
        try:
            rewritten = format_source(source, filename)
        except FormatError as error:
            return _session_error(f"cannot format: {error}")

    gate = ir_equivalent(source, rewritten, filename, token_preserving=not migrate)
    if not gate.admitted:
        return {"ok": False, "admitted": False, "reason": gate.reason,
                "note": "refused — the rewrite would change what the compiler sees"}
    result = {"ok": True, "admitted": True, "formatted": rewritten,
              "changed": rewritten != source, "proof": gate.proof}
    if warnings:
        result["warnings"] = warnings
    return result


def _tool_explain(arguments: dict) -> dict:
    """revl_explain: what a diagnostic code means and how to fix it — the MCP
    twin of `revl explain`, the other half of a structured `revl_check`
    rejection (which already carries the code)."""
    code = arguments.get("code")
    if not code:
        return _session_error("`code` is required")
    return explain(code)


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
        "name": "revl_ship",
        "description": "One intent, one call: fuse check -> admit -> plan into a "
                       "single early-exit request instead of three round-trips "
                       "(the token-saving ship path, docs/token-economy.md). Runs "
                       "the stages in order and STOPS at the first that fails — a "
                       "candidate that does not compile is never admitted, one that "
                       "is not admissible is never planned — and returns one "
                       "consolidated result with a per-stage verdict and `stoppedAt`. "
                       "The running manifest defaults to the composition this server "
                       "holds, so the running IR is not re-sent to admit against it. "
                       "Pass `apply: true` to also hot-swap the candidate in once all "
                       "stages pass, collapsing the whole check->admit->plan->swap "
                       "chain into this one call; without it nothing is mutated.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **_SOURCE_INPUT,
                "manifest": {"type": "object",
                             "description": "compiled IR of the running composition; "
                                            "omit to ship against the loaded session"},
                "replacing": {"type": "array", "items": {"type": "string"},
                              "description": "components withdrawn in this ship"},
                "apply": {"type": "boolean",
                          "description": "when true, hot-swap the candidate in after "
                                         "all stages pass (destructive); default false "
                                         "is the read-only rehearsal"},
            },
        },
        # capable of mutation (apply); annotate like revl_swap so an agent knows
        # it is not purely read-only, even though it defaults to a dry run.
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "handler": _tool_ship,
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
                       "This is the acting half of revl_admit. Source may be sent inline "
                       "(`source`/`files`/`modules`) OR referred to by name: call it with "
                       "NONE of those and swap re-admits the source the server already "
                       "holds for the running composition — so an agent that edited "
                       "server-side with revl_edit, or wants to re-admit the running "
                       "generation, need not re-serialize the whole file.",
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
        "name": "revl_edit",
        "description": "Patch the SERVER-SIDE source of the running composition and "
                       "re-admit — deltas, not documents. Instead of re-sending the "
                       "whole composition to change one line (revl_swap's cost, which "
                       "scales with the running system), send a small structured patch "
                       "against a named buffer the server already holds. Each edit is "
                       "one of: {hole, expr} (fill the typed hole on that source line — "
                       "pairs with revl_check's fillSpec, which reports each hole's "
                       "line); {range: [start, end], replacement} (replace a character "
                       "span); or {anchor, replacement} (replace a literal snippet, no "
                       "offsets). Re-admission runs the SAME gate as revl_swap: a patch "
                       "that breaks a guarantee is refused with its diagnostic and the "
                       "running system is untouched. A patch that compiles clean is "
                       "hot-swapped in; one that still has open holes advances the "
                       "server-side source (so the next edit builds on it) but swaps "
                       "nothing. Returns the admission verdict / holes / diagnostic — "
                       "never the whole source.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "edits": {
                    "type": "array",
                    "description": "the patch: one or more edits applied in order",
                    "items": {
                        "type": "object",
                        "properties": {
                            "hole": {"type": "integer",
                                     "description": "1-based source line of the typed "
                                                    "hole to fill (from a fillSpec)"},
                            "expr": {"type": "string",
                                     "description": "the fill expression (with `hole`)"},
                            "range": {"type": "array", "items": {"type": "integer"},
                                      "description": "[start] or [start, end] character "
                                                     "offsets to replace"},
                            "anchor": {"type": "string",
                                       "description": "literal substring to replace "
                                                      "(no offset arithmetic)"},
                            "replacement": {"type": "string",
                                            "description": "text to substitute (with "
                                                           "`range`/`anchor`)"},
                            "count": {"type": "integer",
                                      "description": "max anchor sites to replace "
                                                     "(omit for all)"},
                        },
                    },
                },
                "target": {"type": "string",
                           "description": "which server-side buffer to edit: omit for the "
                                          "main inline source, or name an in-memory module"},
                "replacing": {"type": "array", "items": {"type": "string"},
                              "description": "components withdrawn in this admission"},
            },
            "required": ["edits"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "handler": _tool_edit,
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
        "name": "revl_quarantine",
        "description": "Quarantine an UNTRUSTED candidate before it may touch a "
                       "hosted tier: grade it with the gauntlet (item 31), then "
                       "compile it to a STANDARD wasm component (the landed "
                       "canonical ABI) and run its lifecycle + fault battery in "
                       "wasmtime's COMPONENT-MODEL SANDBOX — where confinement is "
                       "physical, so a fault that would escape on a hosted tier "
                       "is a TRAP the runtime catches, not an incident. Returns a "
                       "report whose `verdict` is `passed` (proved itself in the "
                       "sandbox — eligible for admission), `trapped` (a probe "
                       "trapped in the sandbox; contained, host untouched — not "
                       "eligible), `rejected` (admission refused; never reached "
                       "the substrate), `deferred` (no Str-surface function — "
                       "records/lists across the boundary are the aggregate "
                       "follow-on) or `unavailable` (wasm-tools/wasmtime absent; "
                       "the grade + lowering still ran). It also reports the "
                       "policy admission decision (`admission`): where a bound "
                       "policy declares `quarantine required` (item 33), a "
                       "candidate is admissible only after it passes — unless the "
                       "session's operator holds `quarantine-bypass` authority "
                       "(item 55). Read-only: the live composition is never "
                       "touched. Str-surface candidates this slice; see "
                       "docs/quarantine-tier.md.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **_SOURCE_INPUT,
                "service": {"type": "string",
                            "description": "WIT interface name to group the "
                                           "candidate's Str-surface functions "
                                           "under (default: the sole declared "
                                           "service, else `Candidate`)"},
                "config": {"type": "object",
                           "description": "per-component config for the gauntlet's "
                                          "scratch boot"},
                "replacing": {"type": "array", "items": {"type": "string"},
                              "description": "components withdrawn in this admission"},
            },
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_quarantine,
    },
    {
        "name": "revl_repair",
        "description": "The repair loop (roadmap item 62): a faulting component "
                       "fixes itself, within policy. Give the fault's causal "
                       "trace (`trace`, item-27 events, or `traceFile`) and a "
                       "regenerated `candidate` (`{source|files|modules}`) — or a "
                       "`need` for the reuse check to find an existing fix (item "
                       "49) — and the loop runs unattended: gauntlet (item 31) -> "
                       "boundary policy (item 33) -> capability-widening ack (item "
                       "21) -> hot-swap (item 23), authorized by the SELF-REPAIR "
                       "POLICY (`selfRepairPolicy`) that says which components may "
                       "self-repair and which capabilities a repair may touch. A "
                       "candidate that would WIDEN what the composition reaches "
                       "outside the system PAUSES for a human ack (status "
                       "awaiting-ack) instead of swapping; an ineligible component "
                       "halts. Returns the INCIDENT DOSSIER: every step (fault, "
                       "why, slice, candidate, verdicts, swap, authority) "
                       "reconstructed from the causal trace alone. The running "
                       "composition is mutated only by the final swap, only when "
                       "every gate is green. `apply:false` plans without swapping. "
                       "See docs/repair-loop.md.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "component": {"type": "string",
                              "description": "the faulting component to repair"},
                "trace": {"type": "array",
                          "description": "the causal trace: item-27 lifecycle "
                                         "event objects (from `revl run --trace`)"},
                "traceFile": {"type": "string",
                              "description": "path to a JSONL causal trace "
                                             "(alternative to inline `trace`)"},
                "predicate": {"type": "string",
                              "description": "a bisect predicate to localize the "
                                             "fault to a step (item 40); needs a "
                                             "session loaded with record:true"},
                "candidate": {"type": "object",
                              "description": "the regenerated repair: "
                                             "{source|files|modules}"},
                "need": {"description": "a need spec for the registry reuse check "
                                        "(item 49) — a `service` decl, a fill "
                                        "spec, or a shape object"},
                "selfRepairPolicy": {
                    "description": "which components may self-repair and which "
                                   "capabilities a repair may touch — a dict "
                                   "({eligible, mayTouch, ackOnWiden}) or DSL "
                                   "text. Absent = closed (nothing self-repairs)."},
                "accept": {"type": "array", "items": {"type": "string"},
                           "description": "widening crossings already acknowledged "
                                          "(item 21 ack tokens)"},
                "apply": {"type": "boolean",
                          "description": "perform the swap (default true); false "
                                         "runs every gate but does not swap"},
                "registry": {"type": "string",
                             "description": "registry dir for the reuse check "
                                            "(default $REVL_REGISTRY)"},
            },
            "required": ["component"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "handler": _tool_repair,
    },
    {
        "name": "revl_rollback",
        "description": "Restore the generation that was running before the last swap.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "handler": _tool_rollback,
    },
    {
        "name": "revl_undo",
        "description": "Return to an earlier generation through the retained "
                       "generation history — the deep version of revl_rollback. "
                       "With no `to`, undoes to generation N−1; `to` names any "
                       "still-retained generation. The undo is itself an ADMITTED, "
                       "gated change: the target's sources are re-admitted through "
                       "the same compile+admission gate a swap runs, so a target the "
                       "current checker rejects is refused (ok:false, with the "
                       "diagnostic) and the running composition is untouched — an "
                       "undo never bypasses the gate. The dossier rides along: what "
                       "unloads, what state drops (item 53's honesty in reverse), and "
                       "the interim boundary crossings that no undo can un-emit "
                       "(compensation is not inversion — paper §6.1). "
                       "See docs/generation-history.md.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "integer",
                       "description": "a retained generation number to return to; "
                                      "omit to undo to the immediately previous "
                                      "generation (N−1)"},
            },
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "handler": _tool_undo,
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
        "name": "revl_commit",
        "description": "Enumerate the session commit MANIFEST (step 1 of 2, item "
                       "245). The session split its actions three ways: witnessed "
                       "mutations ran and revert on abort; DEFERRED emissions were "
                       "queued and have not crossed; immediate emissions already "
                       "fired. This returns what a commit WILL cross — `summary` is "
                       "the one-line prompt ('empty trash: 3 files; send: 1 email'), "
                       "`deferred` the queue, `witnessed` the count about to "
                       "discharge — plus a `hash` binding exactly that. Nothing "
                       "crosses yet; call revl_commit_confirm(hash) to flush.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_commit,
    },
    {
        "name": "revl_commit_confirm",
        "description": "COMMIT the session (step 2 of 2, item 245): flush the "
                       "deferral queue in FIFO order (each deferred emission's host "
                       "body fires once), discharge the witnessed mutations, and "
                       "mark it durable — record order commit-approved, flushed, "
                       "discharge, activation-complete. `hash` must be the one "
                       "revl_commit returned; if the queue or the live composition "
                       "drifted since, the hash mismatches and the commit is "
                       "REFUSED with a fresh manifest (ok:false), so what fires is "
                       "exactly what was approved, never a superset.",
        "inputSchema": {
            "type": "object",
            "properties": {"hash": {"type": "string",
                                    "description": "the manifest hash from revl_commit"}},
            "required": ["hash"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "handler": _tool_commit_confirm,
    },
    {
        "name": "revl_abort",
        "description": "ABORT the session (item 245): DROP the deferral queue "
                       "(nothing fired, nothing to offset — exact by construction), "
                       "replay every witnessed mutation's inverse, and prove a "
                       "clean world. Immediate emissions already out stay out. The "
                       "counterpart to revl_commit_confirm; a session using only "
                       "witnessed and deferred actions aborts residue-free.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "handler": _tool_abort,
    },
    {
        "name": "revl_estop",
        "description": "E-STOP (item 443): the operator's emergency halt. STOP "
                       "DISPATCHING new boundary crossings immediately, run "
                       "NOTHING, and report what was in flight. This is NOT "
                       "revl_abort: abort is a verdict on the work and pays for a "
                       "full two-phase LIFO unwind; estop pays for a latch flip. "
                       "The price is stated, not hidden — every registered entry "
                       "is left STRANDED (owed, never discharged) and every "
                       "acquired handle stays held, so the report says what was "
                       "NOT unwound. The instance is dead afterwards: there is no "
                       "resume, and the way back is `revl recover --wal <file>`. "
                       "Held as an operator authority (verb `estop`) precisely so "
                       "a composition or an agent cannot invoke it on itself.",
        "inputSchema": {"type": "object", "properties": {
            "reason": {"type": "string",
                       "description": "why the button was hit; carried into every "
                                      "residue record and the halt report"},
            "operator": {"type": "string",
                         "description": "the operator token accountable for the "
                                        "halt; defaults to the session's bound "
                                        "operator"}}},
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "handler": _tool_estop,
    },
    {
        "name": "revl_estop_report",
        "description": "Read the item-443 E-Stop inventory back WITHOUT touching "
                       "the world: what was in flight and therefore AMBIGUOUS (at "
                       "most one crossing, outcome unknown), and what was stranded "
                       "— registered, never unwound, still owed. Read-only, and "
                       "never `clean`: an E-Stop leaves residue by design.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
        "handler": _tool_estop_report,
    },
    {
        "name": "revl_fork",
        "description": "Step 1 of the two-step session fork (item 250): ENUMERATE "
                       "what forking at step k would rewind and what it cannot. "
                       "Walks the whole tail above k into an honest, total "
                       "partition — the host-confined witnessed effects and "
                       "provisions that WILL be rewound, the held deferred sends "
                       "that WILL be dropped, the emissions that already CROSSED "
                       "the boundary and cannot be undone, the outbound-scoped "
                       "inverses that WOULD cross on rewind (enumerated, never "
                       "fired), and any step the recorder cannot restore — and "
                       "returns a `hash` binding the rewound span. Nothing is "
                       "rewound yet. Refuses a fork whose tail has a KIND_OPAQUE "
                       "step, a non-idempotent inverse, or a committed boundary "
                       "below k. Call revl_fork_confirm(hash) to perform it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "component": {"type": "string"},
                "at": {"type": "integer",
                       "description": "the step k to fork at; -1 rewinds the whole tail"},
            },
            "required": ["at"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_fork,
    },
    {
        "name": "revl_fork_confirm",
        "description": "Step 2 of the session fork (item 250): PERFORM the fork the "
                       "hash from revl_fork bound. Re-derives the hash and refuses "
                       "on any drift (a fresh report, a result not an error). On "
                       "match it runs the SCOPE-GATED, non-emitting rewind to k "
                       "(host-confined inverses only — an outbound-scoped inverse "
                       "is enumerated, never fired), drops the parent deferral "
                       "queue, FREEZES the parent (retired at k, non-callable), "
                       "snapshots the step-k state, and mints the branch (fresh "
                       "session id + WAL, no approval carry). The branch is then "
                       "the only live continuation over the shared workspace. The "
                       "result carries `lineage`: what the branch inherited "
                       "(composition, generation, IR and source digests, "
                       "capability surface, WAL position) and, listed explicitly, "
                       "what it did NOT (provider versions, seeds and clock, model "
                       "decisions). The same lineage is written durably into the "
                       "branch's own WAL, so `revl branch` / `revl compare` read "
                       "the branch tree back after the process is gone.",
        "inputSchema": {
            "type": "object",
            "properties": {"hash": {"type": "string",
                                    "description": "the fork hash from revl_fork"}},
            "required": ["hash"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "handler": _tool_fork_confirm,
    },
    {
        "name": "revl_approve",
        "description": "Say YES to an outstanding class-(c) crossing (item 246, "
                       "the auto-approve policy). When the approval policy is on, "
                       "a revl_call (or a load/swap whose activation body emits) "
                       "that reaches an IRREVERSIBLE emission with no checked "
                       "inverse does not fire: it returns `approvalRequired` with "
                       "a `ticket`. Relay that ticket to a human, then call this "
                       "with the ticket's `hash` to mint a standing, single-use, "
                       "hash-bound approval; the IDENTICAL re-issue then fires "
                       "once and consumes it. A hash the server never issued is "
                       "refused (the outstanding-ticket table); a swap or edit "
                       "that changes the call's reach closure invalidates a "
                       "standing approval (the candidate hash no longer matches). "
                       "For a REPEAT-shaped session (n class-(c) calls to the same "
                       "capability, e.g. a shell escape), pass `capability` and/or "
                       "`uses`/`ttlMs` INSTEAD of a bare hash to mint a SESSION-"
                       "SCOPED STANDING GRANT (item 344): one mint the n calls then "
                       "auto-approve against, decrementing `uses` and checked "
                       "against the TTL — n prompts become one. Name the grant from "
                       "an outstanding ticket (`hash` + `uses`/`ttlMs`) or "
                       "proactively (`capability` + `uses`/`ttlMs`). "
                       "Gated by the `approve` operator verb (item 55): who may "
                       "say yes is scoped in the same profile grammar as who may "
                       "commit. Class (a) (witnessed-revertible) and (b) (deferred) "
                       "crossings never reach here — they auto-approve.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "hash": {"type": "string",
                         "description": "the ticket hash from the "
                                        "approvalRequired response (single-use "
                                        "approval, or the seed for a standing "
                                        "grant when uses/ttlMs is also given)"},
                "capability": {"type": "string",
                               "description": "item 344: the capability to mint a "
                                              "standing grant for (proactive, or "
                                              "to disambiguate a multi-capability "
                                              "ticket)"},
                "uses": {"type": "integer", "minimum": 1,
                         "description": "item 344: how many class-(c) crossings "
                                        "the standing grant may auto-approve"},
                "ttlMs": {"type": "integer", "minimum": 1,
                          "description": "item 344: how long (ms) the standing "
                                         "grant stays live; checked at the "
                                         "crossing against the session clock"}},
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
        "handler": _tool_approve,
    },
    {
        "name": "revl_revoke",
        "description": "Retire a SESSION-SCOPED STANDING GRANT early (item 379), "
                       "the symmetric partner of revl_approve's item-344 standing "
                       "grant. A grant minted by revl_approve (`capability` + "
                       "`uses`/`ttlMs`) auto-approves repeat class-(c) crossings "
                       "until its uses run out, its TTL lapses, or the session "
                       "ends. This withdraws it BEFORE any of those, effective "
                       "immediately and mid-session: the NEXT class-(c) crossing "
                       "the grant would have covered prompts again (fail-closed). "
                       "Target the SAME key revl_approve minted against — pass "
                       "`capability` to revoke EVERY live grant for it (a token "
                       "like `gateway.send` when the emission is item-343 scoped, "
                       "the extern name otherwise), or `requestId` (the id the "
                       "mint returned) to revoke one specific grant. Revoking a "
                       "capability or id with no live grant is a clean no-op "
                       "(`count: 0`), never an error — idempotent, so a double "
                       "revoke or a stale id is harmless. Gated by the `approve` "
                       "operator verb (item 55): withdrawing consent is the same "
                       "authority as granting it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "capability": {"type": "string",
                               "description": "revoke every live standing grant "
                                              "for this capability (the same "
                                              "token/name key revl_approve mints "
                                              "against)"},
                "requestId": {"type": "string",
                              "description": "revoke one specific grant by the "
                                             "id revl_approve's standing-grant "
                                             "mint returned"}},
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
        "handler": _tool_revoke,
    },
    {
        "name": "revl_distillation_offers",
        "description": "Fold this session's approval ledger to candidate distilled "
                       "auto-approve rules (item 251). The ledger is the item-248 "
                       "stream of human yeses to class-(c) crossings; distillation "
                       "notices the same operator keeps saying yes to the same "
                       "SHAPE of crossing (the resource-scoped capability, realm, "
                       "and taint origins) and writes down the AutoApproveRule that "
                       "would have said yes for them - a rule an operator could "
                       "have typed, checked on the same runtime path. Read-only "
                       "and PROPOSE-ONLY: it applies no policy and is ungated, but "
                       "is scoped to the caller's own attributed grants. Each offer "
                       "carries its rule text, blast radius (the past prompts it "
                       "would have covered, the destinations seen, and the taint "
                       "origins it can NEVER admit), the attributed operator, and "
                       "the sessions it was distilled from. Apply one with "
                       "revl_apply_distillation.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_distillation_offers,
    },
    {
        "name": "revl_apply_distillation",
        "description": "Install a distilled offer as a live AutoApproveRule (item "
                       "251). Writes the rule into the bound policy and records a "
                       "`distillation-applied` WAL fact with the attribution "
                       "(distilledBy - the operator whose repeated yeses it "
                       "encodes; reviewedBy - the operator who applied it; the "
                       "ledger window it was distilled from; appliedAt). The rule "
                       "is bound to the enumerated component set it was reviewed "
                       "against: a component later ENTERING its glob that was not "
                       "in that set suspends the rule and re-offers (fail-closed). "
                       "A distilled `host=X` rule never auto-approves a send to "
                       "another host (the resource scope is enforced by the same "
                       "`covers` order a hand-written rule is), and an admission "
                       "with unknown taint is floored to all origins (never waved "
                       "through). Gated by the `approve` operator verb (item 55): "
                       "installing a standing auto-approve is the same authority as "
                       "granting the underlying yeses.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "offerId": {"type": "string",
                            "description": "the offer id from "
                                           "revl_distillation_offers"}},
            "required": ["offerId"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
        "handler": _tool_apply_distillation,
    },
    {
        "name": "revl_revoke_distillation",
        "description": "Retire an applied distilled AutoApproveRule from the live "
                       "policy (item 251), the symmetric partner of "
                       "revl_apply_distillation. Removes the rule (matched by its "
                       "canonical DSL text), records a `distillation-revoked` WAL "
                       "fact, and the NEXT matching crossing prompts again "
                       "(fail-closed); consume-before-fire already covers any "
                       "in-flight crossing, so there is no orphaned auto-approval "
                       "mid-revoke. Revoking a rule with no live match is a clean "
                       "no-op (`count: 0`). Gated by the `approve` operator verb "
                       "(item 55): withdrawing a standing auto-approve is the same "
                       "authority as installing it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "rule": {"type": "string",
                         "description": "the rule text (or its canonical DSL) to "
                                        "revoke"}},
            "required": ["rule"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
        "handler": _tool_revoke_distillation,
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
        "name": "revl_lease",
        "description": "Claim, renew or release an operator-scoped, TTL-bound "
                       "LEASE on a component NAME — the multi-agent workspace "
                       "primitive. A lease is NOT a lock: the running component "
                       "keeps serving every call. It governs who may REPLACE it "
                       "while you iterate — 'agent B is on UserCache until 14:32'. "
                       "By default a swap that would replace someone else's leased "
                       "component is WARNED at revl_plan/revl_swap but proceeds; "
                       "under a boundary policy that declares `leases enforced` "
                       "(item 33) that swap is REFUSED at admission, the running "
                       "system untouched. The holder is the session's operator "
                       "identity (item 55); active leases show in revl_state; every "
                       "claim/renew/release/expiry rides the causal trace (item 27). "
                       "Leases expire on their TTL, so a walked-away agent never "
                       "wedges the workspace. See docs/component-leases.md.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["claim", "renew", "release"],
                           "description": "claim (or extend) / renew / release; "
                                          "default claim"},
                "component": {"type": "string",
                              "description": "the component name to lease"},
                "ttl": {"type": "number",
                        "description": "lease duration in seconds (claim/renew; "
                                       "default 300)"},
            },
            "required": ["component"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
        "handler": _tool_lease,
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

# the authoring toolbox (roadmap item 345, docs/scaffold.md + docs/holes.md):
# scaffold -> fill -> admit was reachable from the CLI but not from MCP, so a
# harness reinvented generate-whole -> refuse -> regenerate instead. Appended
# additively, like revl_resolve below, so the core verb literal stays owned.
TOOLS.extend([
    {
        "name": "revl_scaffold",
        "description": "Generate a typed, holed component skeleton from a spec "
                       "(docs/scaffold.md) and return it WITH every open hole's "
                       "fillSpec in one call — expected type, capability bound, "
                       "in-scope bindings, reachable services (the same shape "
                       "revl_check adds to `holes`). The scaffold-first flow: "
                       "revl_scaffold -> fill each hole -> revl_check/revl_admit, "
                       "instead of generating a whole component and repairing "
                       "structural errors after the fact.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service": {"type": "string",
                            "description": "the service the component provides"},
                "provides": {"type": "string",
                             "description": "the provision key (default: the "
                                            "service, lowercased)"},
                "component": {"type": "string",
                              "description": "the component name (default: "
                                             "<Service>Provider)"},
                "requires": {"type": "array", "items": {"type": "string"},
                             "description": "injected dependencies, each "
                                            "`KEY[:Service]` (a bare KEY defaults "
                                            "its service to KEY capitalized)"},
                "capabilities": {"type": "array", "items": {"type": "string"},
                                 "description": "boundaries the component may "
                                                "emit through; only a capability "
                                                "whose boundary is `requires`d "
                                                "becomes an emission bound"},
                "methods": {"type": "array", "items": {"type": "string"},
                            "description": "pure service methods, each "
                                           "`'name(p: T) -> R'`"},
                "emits": {"type": "array", "items": {"type": "string"},
                          "description": "emission service methods, each "
                                         "`'name(p: T) -> R'`, bound to the "
                                         "wired capabilities"},
                "config": {"type": "array", "items": {"type": "string"},
                           "description": "component config fields, each "
                                          "`name:Type`"},
                "resource": {"type": "string",
                             "description": "the effect-acquired resource's "
                                            "type (default: <Service>Resource)"},
                "effect": {"type": "boolean",
                           "description": "include the acquire/undo effect "
                                          "block (default true)"},
                "filename": {"type": "string",
                             "description": "filename used when compiling the "
                                            "skeleton for its holes (default: "
                                            "<component>.rvl)"},
            },
            "required": ["service"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_scaffold,
    },
    {
        "name": "revl_fmt",
        "description": "Canonically format (or, with `migrate: true`, rewrite 1.x "
                       "`$` interpolation to 2.0 backtick templates) inline source "
                       "— the MCP twin of `revl fmt`, text in and text out, nothing "
                       "touches disk. The rewrite is proven against the same "
                       "IR-equivalence gate the CLI runs: `admitted: false` means "
                       "the rewrite would change what the compiler sees, and "
                       "`formatted` is NOT returned.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "inline .rvl source"},
                "filename": {"type": "string",
                             "description": "filename for diagnostics (default "
                                            "<source>)"},
                "migrate": {"type": "boolean",
                            "description": "rewrite 1.x `$` interpolation to "
                                           "backtick templates instead of "
                                           "canonical formatting"},
            },
            "required": ["source"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_fmt,
    },
    {
        "name": "revl_explain",
        "description": "What a diagnostic code means and how to fix it — the "
                       "MCP twin of `revl explain`. The other half of a "
                       "structured `revl_check`/`revl_admit` rejection, which "
                       "already carries the code; an unknown code answers with "
                       "the roster of known ones instead of nothing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string",
                         "description": "a diagnostic code, e.g. G4 "
                                        "(case-insensitive)"},
            },
            "required": ["code"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "handler": _tool_explain,
    },
])

# the component registry read path (docs/registry.md, roadmap item 49) —
# appended additively so the tool literal above stays owned by the core verbs
TOOLS.append({
    "name": "revl_resolve",
    "description": "Find a component to IMPORT instead of regenerating one. "
                   "Give the NEED — a `service` declaration (source), a hole's "
                   "fill spec (verbatim from revl_check), or a service shape "
                   "object — and it returns ranked candidates whose provided "
                   "service is §5-compatible with the need, each carrying its "
                   "SOURCE and MANIFEST inline so the next call is revl_admit / "
                   "revl_swap (two round-trips, never a browse session). Matching "
                   "is admission, never text: the same structural-compatibility "
                   "gate a hot-swap runs, pointed at the index — a candidate the "
                   "gate would refuse is not returned. Pass `manifest` (the "
                   "running composition's IR) to upgrade the answer from "
                   "'compatible somewhere' to 'admissible here': a key the "
                   "composition already provides is withheld (G2). Ranking is "
                   "least-authority-first (smallest capability set, then tighter "
                   "interface fit), and then by EVIDENCE QUALITY (item 293): among "
                   "the interface-compatible candidates, one with a fuller fault "
                   "sweep, a valid attestation, a trusted publisher, or an "
                   "inverse-roundtrip pass ranks higher. Each candidate carries an "
                   "`evidence` summary and the winner's `why` names the evidence it "
                   "won on. Interface compatibility is a HARD filter; a missing "
                   "evidence file is `unavailable` (ranked below present-and-valid), "
                   "never read as valid. Set `verifyRequired` (with a signer key in "
                   "$REVL_ATTEST_KEY/$REVL_ATTEST_KEY_FILE) to filter any candidate "
                   "lacking a cryptographically valid attestation. A candidate the "
                   "compatibility filter REFUSED is additionally probed for a safe "
                   "ADAPTER (item 296): when one exists it comes back "
                   "`compatible-with-adapter`, carrying the bridge plan, the "
                   "generated `adapt` source to commit, the chain depth, and the "
                   "wiring rename — PROPOSED, never wired, and ranked below every "
                   "directly compatible candidate at equal authority. Pass "
                   "`adaptOptIns` for the transformations that need an author's "
                   "opt-in (an outcome merge, a non-canonical default); without it "
                   "those pairs come back under `nearMisses` naming the exact "
                   "position and clause. Set `adapt: false` for the direct-only "
                   "answer. Pass `policy` (a boundary policy path) to have each "
                   "candidate the policy's component-scoped EVIDENCE rules "
                   "already refuse come back marked `wouldBeRefused`, so you do "
                   "not pick a top-ranked candidate the gate then bounces — a "
                   "prediction that filters nothing and whose ABSENCE is not an "
                   "admission (`policyPreview.unpredicted` names the rules only "
                   "the gate can decide).",
    "inputSchema": {
        "type": "object",
        "properties": {
            "need": {"description": "a `service` declaration source, a fill spec "
                                    "object, or a service shape object"},
            "manifest": {"type": "object",
                         "description": "the running composition's IR — candidates "
                                        "are additionally checked admissible here"},
            "limit": {"type": "integer",
                      "description": "max candidates to return (default 5)"},
            "registry": {"type": "string",
                         "description": "registry directory (default $REVL_REGISTRY "
                                        "or the repo's registry/)"},
            "verifyRequired": {"type": "boolean",
                               "description": "filter candidates without a "
                                              "cryptographically valid attestation "
                                              "(needs a signer key in the env)"},
            "trustedPublishers": {"type": "array", "items": {"type": "string"},
                                  "description": "publisher ids whose provenance "
                                                 "lifts a candidate in the ranking"},
            "adapt": {"type": "boolean",
                      "description": "probe candidates the §5 filter refused for a "
                                     "SAFE adapter (default true); set false for "
                                     "the direct-only answer"},
            "adaptOptIns": {"type": "object",
                            "description": "the author's `adapt` opt-in map `D`, "
                                           "keyed by method name — the same JSON "
                                           "`revl adapt --adapt` takes, e.g. "
                                           "{\"get\": {\"return\": {\"merge\": "
                                           "\"total\"}}}. Transformations that "
                                           "need an opt-in refuse without it, and "
                                           "the refusal rides out in `nearMisses`"},
            "policy": {"type": "string",
                       "description": "path to a boundary policy: each candidate "
                                      "its component-scoped evidence rules "
                                      "ALREADY refuse comes back carrying "
                                      "`wouldBeRefused` (item 290). A PREDICTION "
                                      "only - it filters nothing and reorders "
                                      "nothing, and its ABSENCE is not an "
                                      "admission; `policyPreview.unpredicted` "
                                      "names the rules only the gate can decide"},
        },
        "required": ["need"],
    },
    "annotations": {"readOnlyHint": True, "destructiveHint": False},
    "handler": _tool_resolve,
})

# verified canary (docs/verified-canary.md, roadmap item 59) — progressive
# delivery: decide one slice on recorded evidence, prove the revert clean.
# Appended additively, like revl_resolve, so the core verb literal stays owned.
TOOLS.append({
    "name": "revl_canary",
    "description": "Progressive delivery with a derived rollback: run a "
                   "successor generation on ONE designated slice (a realm — a "
                   "tenant, a sandbox) while the baseline serves the rest, and "
                   "decide on evidence. Give the running composition (`baseline` "
                   "source or `baselineFiles`), the `candidate` generation of the "
                   "slice's provider (source or `candidateFiles`), and the "
                   "`realm` to canary. Returns: the DIVERGENCE — a replay "
                   "comparison of the two recorded worlds, attributed to the "
                   "exact (component, realm) that produced the first differing "
                   "step, never a metric threshold; the REVERT proof — the "
                   "derived LIFO teardown of the slice with the EXACT `survivors` "
                   "set proving the other N-1 tenants keep every provision (G2) "
                   "and the R4 no-residue proof; and, with `promoteTo`, the "
                   "PROMOTE verdict (the swap admission gate for the remainder). "
                   "It DECIDES — `revl swap` acts. Stateless canary only; a "
                   "stateful one needs item 53 handoff (docs/verified-canary.md).",
    "inputSchema": {
        "type": "object",
        "properties": {
            "baseline": {"type": "string",
                         "description": "the running composition, as source text"},
            "baselineFiles": {"type": "array", "items": {"type": "string"},
                              "description": "the running composition, as .rvl paths"},
            "candidate": {"type": "string",
                          "description": "the successor generation of the slice's "
                                         "provider, as source text"},
            "candidateFiles": {"type": "array", "items": {"type": "string"},
                               "description": "the candidate as .rvl paths (required "
                                              "to also report the promote verdict)"},
            "realm": {"type": "string",
                      "description": "the designated slice — a named realm"},
            "provider": {"type": "string",
                         "description": "the slice's provider component to canary "
                                        "(only needed when the realm serves several)"},
            "promoteTo": {"type": "string",
                          "description": "backend tier to report a promote (= swap "
                                         "the remainder) admission verdict for"},
            "proveResidue": {"type": "boolean",
                             "description": "run the runtime R4 no-residue proof "
                                            "(default true; skipped where cordis "
                                            "is unavailable)"},
        },
        "required": ["realm"],
    },
    "annotations": {"readOnlyHint": True, "destructiveHint": False},
    "handler": _tool_canary,
})

# composition queries (docs/queries.md) — defined next door so this module
# stays the protocol layer and the query surface can grow on its own
TOOLS.extend(QUERY_TOOLS)

# the two session-bound query modes (live / historical) carry their schema
# next door and their handler here, because only this module holds the session
_SESSION_QUERY_HANDLERS = {
    "revl_live_query": _tool_live_query,
    "revl_history_emitted_between": _tool_history_emitted_between,
    "revl_history_lifetime": _tool_history_lifetime,
}
for _schema in LIVE_QUERY_TOOLS + HISTORY_QUERY_TOOLS:
    TOOLS.append({**_schema, "handler": _SESSION_QUERY_HANDLERS[_schema["name"]]})

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
        arguments = params.get("arguments") or {}
        # the path jail and the authoring gate run BEFORE the operator gate and
        # before any handler: a refusal here has read nothing, compiled nothing
        # and run nothing, so neither can be used as an oracle.
        payload = _jail_refusal(arguments) or _authoring_refusal(arguments)
        # operator capabilities (roadmap item 55): gate a mutating management
        # verb against the session's bound operator before it can run. No
        # profile bound -> ungated, today's behaviour unchanged. Skipped when the
        # call is already refused, so a refused path is never even resolved
        # against an operator's grants.
        decision = None if payload is not None \
            else _operator.decide(SESSION, name, arguments)
        if payload is not None:
            pass                    # refused above; the handler never runs
        elif decision.gated and not decision.allowed:
            payload = _refused_by_operator(decision)
        else:
            try:
                payload = handler(arguments)
                _remember_live_host_bodies()
            except ApprovalRequired as exc:
                # item 246: a class-(c) crossing the decision inside Session.call
                # (or the activation gate in load/swap) refused. This is a result,
                # not an error — shape the ticket two-step. Caught before the
                # generic handler so it never reads as an internal fault.
                payload = _approval_required(exc)
            except Exception as exc:  # a tool failure is a result, not a transport error
                payload = {"ok": False, "diagnostics": [{
                    "severity": "error", "code": "REVL", "category": "internal",
                    "message": f"{type(exc).__name__}: {exc}",
                }]}
            if decision.gated and decision.allowed:
                _stamp_authority(payload, decision)
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
