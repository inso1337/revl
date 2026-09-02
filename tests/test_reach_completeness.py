"""Reach-completeness harness - the capstone of the 2026-08-31 security campaign
(roadmap item 414).

Every CRITICAL the review passed found was the same shape: an authority-derivation
fold / gate that visits ONE way authority crosses a boundary and silently misses
another (the spawn/instance-get seam, the transitive module closure, the same-tier
process seam, a resource nested in a record/variant, membership-instead-of-subset
over a worst-class fold). Each fix closed one hole; nothing stopped the NEXT fold
from forgetting the NEXT kind.

This file converts "we keep not finding new holes" into a mechanically-enforced
invariant. It is built on two enumerations and one rule:

  * CROSSING_KINDS - the single source of truth for every way authority can cross
    a boundary in revl. Adding a new crossing kind here forces a decision for every
    surface below (the totality meta-test fails until each surface classifies it).

  * SURFACES - every place that folds reach / authority and must account for the
    kinds in its axis: the G8 `_boundary` audit, `policy.component_reach`, the
    approval `ClassMap` fold, the untrusted-author reach sweep, the distribution-
    seam resource refusal, and the taint origin fold. Each surface classifies EVERY
    crossing kind as either IN_SCOPE (its fold must account for that kind's
    authority) or EXEMPT (with a documented reason - the kind belongs to a different
    axis and is enforced by a different surface).

  * The rule - for every IN_SCOPE (surface, kind) cell the test is DIFFERENTIAL:
    it exercises the kind, reads the surface's authority/refusal set WITH the kind
    and WITHOUT it, and asserts the kind's authority is present in the first and
    absent in the second. That is what makes the assertion load-bearing: if a
    surface stopped visiting the kind, the with-kind and without-kind sets would be
    identical and the cell goes RED. A trivially-passing "assert something in a set"
    cannot survive here - the set has to genuinely DEPEND on the kind.

No single surface visits all six kinds; each has an axis (an emission-capability
enumeration, an admission sweep, a resource-typed seam refusal, an information-flow
fold). The completeness invariant is not "every surface visits every kind" - it is
"every (surface, kind) cell is classified, every kind is covered by at least one
surface, and every IN_SCOPE cell is discriminating." That is the checklist the type
system cannot forget.

If a surface genuinely stops accounting for an IN_SCOPE kind on `main`, the
corresponding differential goes RED - which is the whole point: it is a real
security finding, not a test to relax.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import AdmissionProfile, compile_source  # noqa: E402
from revl.audit_diff import audit_report  # noqa: E402
from revl.distill import blast_radius, class_c_capabilities  # noqa: E402
from revl.distribute import _resource_taint  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.mcp.approval import ClassMap  # noqa: E402
from revl.placement import resource_crossing_refusal  # noqa: E402
from revl.policy import (  # noqa: E402
    TAINT_FOLD_ORIGINS, AutoApproveRule, component_reach,
)


# ===========================================================================
# 1. THE ENUMERATION - the single source of truth for how authority crosses.
# ===========================================================================
#
# Every way authority can cross a boundary in revl. This is the checklist. A
# seventh crossing kind added here (e.g. a new capability channel) fails the
# totality meta-test until every surface below classifies it in_scope or exempt.
CROSSING_KINDS: dict[str, str] = {
    "req": "a `req` service-call emission",
    "spawn": "a spawn / instance-get emission (`s.<key>.<method>()`)",
    "extern": "an imported host-body extern reached transitively through functions",
    "seam": "a same-tier vs cross-tier process seam",
    "nested_res": "a resource nested inside a record / variant / generic",
    "firstclass": "a first-class emitting callable / `*` widening",
}


# ===========================================================================
# 2a. THE REACH ZOO - one composition a `Hub` reaches four crossing kinds
#     through, each introducing a DISTINCT capability token / extern name so a
#     surface's reach set can be probed with the kind present and absent.
# ===========================================================================
#
# Only the capability-enumeration kinds live here (the axis of `_boundary`,
# `component_reach` and the approval `ClassMap`). The process-seam and nested-
# resource kinds are a different axis (the distribution seam) and have their own
# fixtures below.
REACH_KINDS = frozenset({"req", "spawn", "extern", "firstclass"})


def _reach_zoo(kinds: frozenset[str]) -> str:
    """revl source for a `Hub` component that reaches exactly `kinds`.

    Each kind contributes a uniquely-named authority:
      req        -> emission capability token `cap_req`
      spawn      -> emission capability token `cap_spawn` (routed off a spawn handle)
      extern     -> transitively-reached host extern `host_extern`
      firstclass -> an emitting extern `launder_write` handed to a dispatcher
                    (`indirect`), which the fixed point resolves and marks `*`.
    """
    decl: list[str] = []
    hub_req: list[str] = []
    act: list[str] = []      # activation-body steps (the spawn binding)
    meth: list[str] = []     # provide-method steps

    if "extern" in kinds:
        decl += [
            'extern emission[cap_ext] fn host_extern(msg: Str) -> Str = @py { return msg }',
            'fn reach_extern(x: Str) -> Str { return host_extern(x) }',
        ]
        meth.append('let a = reach_extern(x)')
    if "firstclass" in kinds:
        decl += [
            'extern emission fn launder_write(msg: Str) -> Str = @py { return msg }',
            'fn indirect(f: (Str) -> Str, x: Str) -> Str { return f(x) }',
        ]
        meth.append('let b = indirect(launder_write, x)')
    if "req" in kinds:
        decl.append('service ReqSvc { emission[cap_req] fn put(row: Str) -> Int }')
        hub_req.append('requires cap_req: ReqSvc')
        meth.append('emit cap_req.put("r")')
    if "spawn" in kinds:
        decl += [
            'service SpawnStore { emission[cap_spawn] fn w(row: Str) -> Int }',
            'service Worker { emission[cap_spawn] fn run() -> Int }',
            'component Child requires cap_spawn: SpawnStore provides worker: Worker '
            '{ provide worker { fn run() { emit cap_spawn.w("x") return 0 } } }',
        ]
        hub_req.append('requires cap_spawn: SpawnStore')
        act.append('let c = effect spawn Child with { } undo c.dispose()')
        meth.append('emit c.worker.run()')

    decl.append('service Hive { emission fn go(x: Str) -> Str }')
    src = "\n".join(decl) + "\n"
    src += "component Hub " + " ".join(hub_req) + " provides hive: Hive {\n"
    for step in act:
        src += "  " + step + "\n"
    src += "  provide hive {\n    fn go(x) {\n"
    for step in meth:
        src += "      " + step + "\n"
    src += "      return x\n    }\n  }\n}\n"
    return src


def _boundary_reach(kinds: frozenset[str]) -> frozenset[str]:
    """Surface: the G8 `_boundary` audit. Hub's crossed capability tokens — the
    scope of each emission call site, and the token of each host extern it
    reaches (item 247: a scoped extern's DECLARED token, its NAME when
    unscoped, the same `capabilities or (name,)` rule `component_reach` and the
    approval fold use)."""
    audit = audit_report(compile_source(_reach_zoo(kinds), "reach.rvl"))
    stats = audit["boundary"]["Hub"]
    caps = {tok for caps in stats["capabilities"].values() for tok in caps}
    externs = {tok for e in stats["externs"]
               for tok in (e.get("capabilities") or [e["name"]])}
    return frozenset(caps | externs)


def _component_reach(kinds: frozenset[str]) -> frozenset[str]:
    """Surface: `policy.component_reach`. The reached-authority token set."""
    audit = audit_report(compile_source(_reach_zoo(kinds), "reach.rvl"))
    return frozenset(r.token for r in component_reach(audit, "Hub"))


def _approval_reach(kinds: frozenset[str]) -> frozenset[str]:
    """Surface: the approval `ClassMap` fold. The capability set the worst-class
    fold over `hive.go`'s closure derives."""
    ir = compile_source(_reach_zoo(kinds), "reach.rvl")
    reach = ClassMap(ir).classify_call("hive", "go")
    return frozenset(reach["capabilities"]) if reach else frozenset()


# ===========================================================================
# 2b. THE TAINT ZOO - one `Agent` an untrusted origin reaches an emission
#     through five crossing kinds, each carrying a DISTINCT coarse origin so the
#     taint fold's origin set can be probed present/absent per kind.
# ===========================================================================
TAINT_KINDS = frozenset({"req", "spawn", "extern", "nested_res", "firstclass"})


def _taint_zoo(kinds: frozenset[str]) -> str:
    """revl source whose `Agent` reaches an emission with a tainted value through
    exactly `kinds`. Each kind carries a distinct origin:
      req -> web, spawn -> net, extern -> fs, nested_res -> input, firstclass -> model.
    """
    decl: list[str] = []
    hub_req = ""
    act: list[str] = []
    meth: list[str] = []

    if "extern" in kinds:
        # the untrusted SOURCE is reached transitively through a `pub fn` wrapper;
        # its origin must ride the callee signature back to the emitting caller.
        decl += [
            'extern emission[fs] fn read_fs(p: Str) -> Untrusted[Str] = @py { return "" }',
            'fn wrapped(p: Str) -> Untrusted[Str] { return read_fs(p) }',
            'extern emission fn host_sink(s: Str) -> Int = @py { return 0 }',
        ]
        meth += ['let pfs = wrapped(x)', 'emit host_sink(pfs)']
    if "nested_res" in kinds:
        # the tainted value is nested in a record and the WHOLE record is emitted;
        # a fold that stops unioning record fields reads the container clean.
        decl += [
            'extern emission[input] fn read_in(p: Str) -> Untrusted[Str] = @py { return "" }',
            'type Box = { field: Str, tag: Str }',
            'extern emission fn host_box(b: Box) -> Int = @py { return 0 }',
        ]
        meth += ['let pin = emit read_in(x)', 'let bx = { field: pin, tag: "t" }',
                 'emit host_box(bx)']
    if "firstclass" in kinds:
        decl += [
            'extern emission[model] fn ask(p: Str) -> Untrusted[Str] = @py { return "" }',
            'extern emission fn host_out(s: Str) -> Int = @py { return 0 }',
            'fn indirect(f: (Str) -> Str, x: Str) -> Str { return f(x) }',
            'fn ident(s: Str) -> Str { return s }',
        ]
        meth += ['let pmo = emit ask(x)', 'let ym = indirect(ident, pmo)',
                 'emit host_out(ym)']
    if "req" in kinds:
        decl += [
            'extern emission[web] fn fetch_web(u: Str) -> Untrusted[Str] = @py { return "" }',
            'service Sink { emission fn out(s: Str) -> Int }',
        ]
        hub_req = "requires snk: Sink "
        meth += ['let pweb = emit fetch_web(x)', 'emit snk.out(pweb)']
    if "spawn" in kinds:
        decl += [
            'extern emission[net] fn fetch_net(u: Str) -> Untrusted[Str] = @py { return "" }',
            'service WSink { emission fn take(s: Str) -> Int }',
            'extern emission fn host_w(s: Str) -> Int = @py { return 0 }',
            'component Child provides ws: WSink '
            '{ provide ws { fn take(s) { emit host_w(s) return 0 } } }',
        ]
        act.append('let c = effect spawn Child with { } undo c.dispose()')
        meth += ['let pnet = emit fetch_net(x)', 'emit c.ws.take(pnet)']

    decl.append('service Ops { emission fn go(x: Str) -> Int }')
    src = "\n".join(decl) + "\n"
    src += f"component Agent {hub_req}provides ops: Ops {{\n"
    for step in act:
        src += "  " + step + "\n"
    src += "  provide ops {\n    fn go(x) {\n"
    for step in meth:
        src += "      " + step + "\n"
    src += "      return 0\n    }\n  }\n}\n"
    return src


def _taint_reach(kinds: frozenset[str]) -> frozenset[str]:
    """Surface: the taint origin fold (item 249). The set of untrusted origins the
    fold records as reaching an emission in `Agent`."""
    ir = compile_source(_taint_zoo(kinds), "taint.rvl")
    comp = next(c for c in ir["components"] if c["name"] == "Agent")
    return frozenset((comp.get("taint") or {}).get("reaches") or [])


# ===========================================================================
# 2b'. THE SECRET-RAISE ZOO - a capability-bound secret (item 256) refused at
#      each crossing kind. Distinct from the taint origin fold: the secret raise
#      is a REFUSAL at the crossing, not a recorded origin, so its differential is
#      "the crossing raises G-SECRET WITH the key and compiles WITHOUT it" rather
#      than a reach set. The five §4a.2 crossings map onto the enumeration:
#        req        -> the `emit` arm (a `secret` crossing a req emission)
#        extern     -> the plain (non-declared-sink) extern host call
#        firstclass -> the unnameable indirect / `*` callable
#        nested_res -> a `secret` nested in a record, caught at the container's
#                      own crossing
#        spawn      -> the provide-method return across the service / MCP bridge,
#                      exercised through a spawned child's `s.<key>.<method>()`
#      `seam` is the one exempt kind (the taint fold, of which the secret raise is
#      a part, is tier-agnostic - a secret relayed across a cross-tier seam
#      propagates identically via the signature fixed point).
SECRET_KINDS = frozenset({"req", "extern", "firstclass", "nested_res", "spawn"})

_SECRET_PRELUDE = (
    "secret openai_key for model.complete\n"
    "extern emission[model.complete] fn complete(p: Str) -> Str = @py { return p }\n"
    "service Ops { emission fn go(u: Str) -> Int }\n"
)
_SECRET_FC_PRELUDE = (
    "secret openai_key for model.complete\n"
    "extern emission[model.complete] fn complete(p: Str) -> Str = @py { return p }\n"
    "service Ops { emission fn go(cb: (Str) -> Int, u: Str) -> Int }\n"
)


def _secret_zoo(kind: str, tainted: bool) -> str:
    """revl source whose `Agent` crosses a bound key exactly one way (`kind`).

    `tainted` toggles whether the crossed value is the bound key (`complete(u)`,
    whose return is minted `secret`) or a clean parameter (`u`). WITH the key the
    crossing raises G-SECRET; WITHOUT it the identical crossing compiles - the
    discriminating pair that proves the raise genuinely depends on this crossing."""
    crossed = "complete(u)" if tainted else "u"
    if kind == "firstclass":
        return (_SECRET_FC_PRELUDE
                + "component Agent provides ops: Ops {\n  provide ops {\n"
                + "    fn go(cb, u) {\n      let s = " + crossed
                + "\n      let y = cb(s)\n      return 0\n    }\n  }\n}\n")
    if kind == "spawn":
        return (_SECRET_PRELUDE
                + "service Worker { emission fn run(u: Str) -> Str }\n"
                + "component Child provides worker: Worker {\n  provide worker {\n"
                + "    fn run(u) {\n      return " + crossed + "\n    }\n  }\n}\n"
                + "component Agent provides ops: Ops {\n"
                + "  let c = effect spawn Child with { } undo c.dispose()\n"
                + "  provide ops {\n    fn go(u) {\n      let r = emit c.worker.run(u)\n"
                + "      return 0\n    }\n  }\n}\n")
    decl, reqs = "", ""
    body = "      let s = " + crossed + "\n"
    if kind == "req":
        decl = "service Sink { emission fn out(s: Str) -> Int }\n"
        reqs = "requires snk: Sink "
        body += "      emit snk.out(s)\n"
    elif kind == "extern":
        decl = "extern emission fn host_sink(s: Str) -> Int = @py { return 0 }\n"
        body += "      let x = host_sink(s)\n"
    elif kind == "nested_res":
        decl = ("type Box = { key: Str, tag: Str }\n"
                "extern emission fn host_box(b: Box) -> Int = @py { return 0 }\n")
        body += "      let r = { key: s, tag: \"t\" }\n      let x = host_box(r)\n"
    else:
        raise AssertionError(f"unknown secret kind {kind!r}")
    return (_SECRET_PRELUDE + decl + "component Agent " + reqs
            + "provides ops: Ops {\n  provide ops {\n    fn go(u) {\n"
            + body + "      return 0\n    }\n  }\n}\n")


def _secret_raises(kind: str, tainted: bool) -> bool:
    """Surface: the item-256 secret raise. True if the crossing refuses the bound
    key with G-SECRET, False if it compiles."""
    try:
        compile_source(_secret_zoo(kind, tainted), "secret.rvl")
        return False
    except RevlError as exc:
        assert getattr(exc, "code", None) == "G-SECRET", exc
        return True


# ===========================================================================
# 2c. THE UNTRUSTED-AUTHOR fixtures - the reach sweep raises G8 the moment an
#     untrusted turn reaches a host extern, by any door.
# ===========================================================================
_UNTRUSTED_BASE = """
service Kv { fn get(k: Str) -> Str }
component KvProvider provides kv: Kv { provide kv { fn get(k) = "v" } }
"""
_SHELL_TOOL = (
    'pub extern emission fn sh(cmd: Str) -> Str '
    '= @py { import os; os.system(cmd); return "" }\n'
)
_PURE_TOOL = 'pub fn greet(n: Str) -> Str { return "hi" }\n'


def _untrusted_admits(source: str, modules: dict[str, str]) -> bool:
    """Surface: the untrusted-author reach sweep (`check_no_host_extern_reach`).
    True if the turn admits, False if the sweep refuses it with G8."""
    base = compile_source(_UNTRUSTED_BASE, "base.rvl")
    try:
        compile_source(source, "turn.rvl", manifest=base, modules=modules,
                       profile=AdmissionProfile.untrusted_author(set()))
        return True
    except RevlError as exc:
        assert getattr(exc, "code", None) == "G8", exc
        return False


# turn that reaches an imported host extern transitively (through a root helper)
_UNTRUSTED_EXTERN_REACH = """
use "sh.rvl" { sh }
fn relay() -> Str { return sh("id") }
service Pwn { emission fn go() -> Str }
component Pwned provides pwn: Pwn { provide pwn { fn go() = emit relay() } }
"""
# turn that reaches the host extern only by handing it as a first-class value
_UNTRUSTED_FIRSTCLASS_REACH = """
use "sh.rvl" { sh }
fn indirect(f: (Str) -> Str, x: Str) -> Str { return f(x) }
service Pwn { emission fn go() -> Str }
component Pwned provides pwn: Pwn { provide pwn { fn go() = emit indirect(sh, "id") } }
"""
# a turn reaching NO host extern - the without-kind baseline
_UNTRUSTED_CLEAN = """
use "h.rvl" { greet }
service Turn { fn run() -> Str }
component TurnComp provides turn: Turn { provide turn { fn run() = greet("x") } }
"""


# ===========================================================================
# 2d. THE DISTRIBUTION-SEAM fixture - the resource-crossing refusal.
# ===========================================================================
_SEAM_APP = """
type Socket = { fd: Int }
extern pure fn close_sock(h: Int) = @py { return None }
extern acquire fn open_sock() -> Socket undo close_sock(0) = @py { return {"fd": 1} }
type Conn = { sock: Socket }
type Outcome = Live(Socket) | Dead
type ConnG[T] = { sock: T }
type Inner = { a: Int }
type ValRec = { inner: Inner, label: Str }
service TopSvc { async fn top() -> Socket }
service RecSvc { async fn getc() -> Conn }
service VarSvc { async fn getv() -> Outcome }
service GenSvc { async fn getg() -> ConnG[Socket] }
service ValSvc { async fn getp() -> ValRec }
service Ctl { async fn go() -> Str }
component C provides t: TopSvc { provide t { async fn top() = open_sock() } }
"""
# the SAME app but Socket is a plain value type (no `acquire` extern makes it a
# resource) - the without-extern baseline for the taint-base kind.
_SEAM_APP_NO_ACQUIRE = """
type Socket = { fd: Int }
service ValSvc2 { async fn getp() -> Socket }
"""


def _seam_topology(service: str, host_backend: str, consumer_backend: str):
    """A one-key cross-process seam: `w` provides `k: <service>`, `c` requires it."""
    requires = {"c": {"k": service}}
    provides = {"c": {"ctl": "Ctl"}, "w": {"k": service}}
    owner = {"k": "w", "ctl": "c"}
    backends = {"c": consumer_backend, "w": host_backend}
    return requires, provides, owner, backends


def _seam_refuses(ir: dict, service: str, host_backend: str = "py",
                  consumer_backend: str = "py", shared: bool = False) -> bool:
    """Surface: `resource_crossing_refusal`. True if the seam is refused."""
    if shared:
        # a key one process both provides and requires is served in-memory -
        # not a distribution seam, so never refused.
        requires = {"p": {"k": service}}
        provides = {"p": {"k": service}}
        owner = {"k": "p"}
        backends = {"p": "py"}
    else:
        requires, provides, owner, backends = _seam_topology(
            service, host_backend, consumer_backend)
    return resource_crossing_refusal(ir, requires, provides, owner, backends) is not None


# ===========================================================================
# 2d. THE CACHE-APPLICABILITY ZOO (item 310, surface H) - one `Hive.go` declaring
#     `cache capability`, whose PROVIDER CLOSURE reaches an uncacheable crossing
#     through each kind. Distinct from the reach zoo: this fold does not ask what
#     authority a call reaches (a plain emission is exactly what `cache
#     capability` is FOR), it asks whether the reach may be memoized at all - so
#     every kind here contributes an ESCROW-SHAPED crossing (or the `*`
#     widening), each with a uniquely-named token so the visit set is probeable
#     present/absent per kind.
CACHE_KINDS = frozenset({"req", "spawn", "extern", "firstclass"})


def _cache_zoo(kinds: frozenset[str]) -> str:
    """revl source for a `cache capability` seam method whose closure reaches an
    uncacheable crossing through exactly `kinds`:
      extern     -> a `compensate`-declaring emission `c_ext`, reached
                    TRANSITIVELY through the pure fn `reach_c`
      firstclass -> an emitting extern handed to a dispatcher (the `*` widening)
      req        -> a required provider reaching a `deferred` emission `d_req`
      spawn      -> a spawned child reaching the `witnessed` extern `w_spawn`

    With no kind selected the method still crosses the ordinary emission
    `base_read` - the cacheable reach `cache capability` exists for - so the
    without-kind probe is a real composition, not an empty one.
    """
    decl: list[str] = ["type W = { path: Str }", "type E = { msg: Str }",
                       "extern pure fn restore(w: W) -> Unit = @py { pass }"]
    hub_req: list[str] = []
    act: list[str] = []
    meth: list[str] = []

    if "extern" in kinds:
        decl += [
            'extern emission[pay] fn undo_pay() -> Unit = @py { pass }',
            'extern emission[pay] fn c_ext(r: Str) -> Int '
            'compensate undo_pay() = @py { return 0 }',
            'fn reach_c(r: Str) -> Int { return c_ext(r) }',
        ]
        meth.append("let a = reach_c(x)")
    if "firstclass" in kinds:
        decl += [
            'extern emission fn fc_write(msg: Str) -> Str = @py { return msg }',
            'fn indirect(f: (Str) -> Str, x: Str) -> Str { return f(x) }',
        ]
        meth.append("let b = indirect(fc_write, x)")
    if "req" in kinds:
        decl += [
            'extern emission[mail] deferred fn d_req(row: Str) = @py { return }',
            'service ReqSvc { emission fn put(row: Str) -> Int }',
            'component ReqProv provides req_svc: ReqSvc '
            '{ provide req_svc { fn put(row) { emit d_req(row) return 0 } } }',
        ]
        hub_req.append("requires req_svc: ReqSvc")
        meth.append('emit req_svc.put("r")')
    if "spawn" in kinds:
        decl += [
            'extern witnessed[cap_w] fn w_spawn(p: Str) -> Result[W, E] '
            'undo restore(result) = @py { pass }',
            'extern emission fn child_read(x: Str) -> Str = @py { return x }',
            'service Worker { emission fn run(x: Str) -> Str }',
            'component Child provides worker: Worker { provide worker { '
            'fn run(x) { let w = w_spawn(x) return emit child_read(x) } } }',
        ]
        act.append("let c = effect spawn Child with { } undo c.dispose()")
        meth.append("let d = emit c.worker.run(x)")

    decl.append("extern emission[base] fn base_read(x: Str) -> Str "
                "= @py { return x }")
    decl.append("service Hive { emission fn go(x: Str) -> Str cache capability }")
    src = "\n".join(decl) + "\n"
    src += "component Hub " + " ".join(hub_req) + " provides hive: Hive {\n"
    for step in act:
        src += "  " + step + "\n"
    src += "  provide hive {\n    fn go(x) {\n"
    for step in meth:
        src += "      " + step + "\n"
    src += "      return emit base_read(x)\n    }\n  }\n}\n"
    return src


def _cache_applicability_reach(kinds: frozenset[str]) -> frozenset[str]:
    """Surface: the item-310 applicability fold (surface H). The set of crossing
    tokens the fold names as making `hive.go`'s provider closure uncacheable."""
    from revl.mcp.approval import cache_applicability_findings
    from revl.mcp.session import Session
    ir = compile_source(_cache_zoo(kinds), "cache.rvl")
    index = Session._build_cache_index(Session.__new__(Session), ir)
    return frozenset(
        token for _key, _m, _cls, token, _what, _why
        in cache_applicability_findings(ClassMap(ir), index))


# ===========================================================================
# 3. THE MATRIX - every surface classifies every crossing kind.
# ===========================================================================
#
# in_scope: the surface's fold MUST account for this kind's authority (a
#           discriminating differential below proves it does).
# exempt:   the kind belongs to a different axis; the reason names the surface
#           that actually enforces it. An exemption is a documented decision, not
#           a silent gap - the totality meta-test forces one for every cell.
IN_SCOPE = "in_scope"

SURFACES: dict[str, dict[str, str]] = {
    # the G8 `_boundary` audit: a per-component enumeration of emission
    # capabilities and reached host externs.
    "boundary": {
        "req": IN_SCOPE,
        "spawn": IN_SCOPE,      # item 246: the spawn/instance-get seam
        "extern": IN_SCOPE,
        "firstclass": IN_SCOPE,  # item 24: the first-class launder / `*`
        "seam": "the same-tier/cross-tier process seam is the distributability "
                "axis; `_boundary` is a per-component enumeration with no process "
                "or tier notion",
        "nested_res": "`_boundary` enumerates emissions and externs, not resource "
                      "types; a resource-returning emission is still just an "
                      "emission here (the resource seam is `resource_crossing_refusal`)",
    },
    # `policy.component_reach`: folds the `_boundary` table into a reach token set.
    "component_reach": {
        "req": IN_SCOPE,
        "spawn": IN_SCOPE,
        "extern": IN_SCOPE,
        "firstclass": IN_SCOPE,
        "seam": "component_reach folds the per-component `_boundary` table; the "
                "process seam is not on that axis",
        "nested_res": "component_reach reads emission capabilities and host reach, "
                      "not resource types",
    },
    # the approval `ClassMap` worst-authority-class fold.
    "approval": {
        "req": IN_SCOPE,
        "spawn": IN_SCOPE,      # test_spawn_routed_emission_folds_to_class_c
        "extern": IN_SCOPE,
        "firstclass": IN_SCOPE,  # `*` is class-(c) and never approvable
        "seam": "the fold's closure walks realm edges to reach provider scopes, but "
                "the same-tier/cross-tier RESOURCE seam is not an approval-class axis",
        "nested_res": "resource-typing is the distribution-seam axis, not the "
                      "authority-class axis the ClassMap derives",
    },
    # the untrusted-author reach sweep (`check_no_host_extern_reach`).
    "untrusted_author": {
        "extern": IN_SCOPE,      # its entire reason to exist
        "firstclass": IN_SCOPE,  # a host extern handed as a value is caught name-exact
        "req": "an untrusted turn's `req` reach is bounded by the sibling admission "
               "surface `check_allowlist`, not by this host-code sweep",
        "spawn": "a spawn provision call resolves to a service method, invisible to "
                 "the bare-name sweep; a spawned child's authority is bounded by "
                 "capability attenuation (item 66)",
        "seam": "the sweep is tier-agnostic; a process seam changes nothing in the "
                "reached host-name set",
        "nested_res": "the sweep tracks reached callable names, never types; a "
                      "resource wrapped in a record is invisible to it",
    },
    # the distribution-seam resource refusal (`resource_crossing_refusal`).
    "dist_seam": {
        "extern": IN_SCOPE,     # the `acquire` extern is the resource-taint base
        "seam": IN_SCOPE,       # Finding A: tier-agnostic, every cross-process seam
        "nested_res": IN_SCOPE,  # Finding C: record / variant / generic nesting
        "req": "the `req` requires-edge is this surface's enumeration DOMAIN (it is "
               "how a seam is discovered), not an authority the refusal derives",
        "spawn": "a spawn/instance-get carries no requires-edge; a spawned child's "
                 "authority is bounded by capability attenuation, not this refusal",
        "firstclass": "a first-class / `*` emission drives the emission-widening "
                      "report and the live-swap re-point check, not the resource "
                      "refusal, which keys only on resource-typed values",
    },
    # the taint origin fold (item 249).
    "taint": {
        "req": IN_SCOPE,
        "spawn": IN_SCOPE,
        "extern": IN_SCOPE,      # an origin minted behind a transitive `pub fn`
        "nested_res": IN_SCOPE,  # the no-false-clean record/variant union invariant
        "firstclass": IN_SCOPE,  # the `*` over-approximation of an unnameable call
        "seam": "the taint fold is tier-agnostic; a value relayed across a cross-tier "
                "seam propagates identically to a same-process service emit via the "
                "signature fixed point, so the seam is not a distinct taint axis",
    },
    # item 256: the capability-bound secret raise (G-SECRET). The bound key is
    # refused at EVERY crossing kind, with no declassifier - the §4a.4 guardrail
    # against a fold that visits one crossing and misses another. This row asserts
    # the raise fires at all five §4a.2 crossings.
    "secret_raise": {
        "req": IN_SCOPE,        # the `emit` arm
        "extern": IN_SCOPE,     # the plain (non-declared-sink) extern host call
        "firstclass": IN_SCOPE,  # the unnameable indirect / `*` callable
        "nested_res": IN_SCOPE,  # a secret nested in a record, caught at the crossing
        "spawn": IN_SCOPE,      # the provide-method return across the service/MCP bridge
        "seam": "the secret raise is part of the tier-agnostic taint fold; a bound "
                "key relayed across a cross-tier seam propagates identically via "
                "the signature fixed point, so the seam is not a distinct secret "
                "axis (the same reasoning as the `taint` surface's seam exemption)",
    },
    # item 251: the approval-distillation blast-radius fold. It enumerates which
    # crossings a candidate `AutoApproveRule` admits, and an operator approves the
    # rule believing that enumeration is complete (design §3.3). A fold that
    # visited one crossing kind and missed another would UNDERSTATE the blast, so
    # the fold reuses the approval `ClassMap` closure (`class_c_capabilities`)
    # precisely to visit the same kinds that surface does. The row item 274
    # slice 2 reserved.
    "distill_blast_251": {
        "req": IN_SCOPE,
        "spawn": IN_SCOPE,       # the spawn/instance-get seam must be in the blast
        "extern": IN_SCOPE,      # an emission extern reached transitively
        "firstclass": IN_SCOPE,  # the unnameable `*`, class-(c) and never admitted
        "seam": "the blast fold reuses the approval `ClassMap` closure over a "
                "per-component reach; the same-tier/cross-tier process seam is the "
                "distributability axis (`resource_crossing_refusal`), not a "
                "crossing the class-(c) enumeration derives",
        "nested_res": "the fold enumerates class-(c) capabilities and their bound "
                      "resource valuation, not resource TYPES; a resource nested "
                      "in a record is the distribution-seam axis, and the blast "
                      "fold's own resource axis is the host/path/table VALUE cone "
                      "(exercised by the resource-scope cell below), not nesting",
    },
    # item 310 surface H: the cache APPLICABILITY fold - "may this callee's reach
    # be memoized at all". A cache HIT re-delivers a result without re-running the
    # reach that produced it, so a fold that missed one crossing kind would admit
    # a cache that silently skips that kind's escrow registration (or, for the
    # `*` widening, binds an entry to an authority it cannot name). It is a
    # worst-over-reach fold over the SAME `Composition.closure` the approval
    # ClassMap folds, precisely so the two can never disagree about what a cached
    # call reaches.
    "cache_applicability_310": {
        "req": IN_SCOPE,         # an uncacheable crossing behind a requires edge
        "spawn": IN_SCOPE,       # …and behind a spawn handle (no `req` target)
        "extern": IN_SCOPE,      # …reached transitively through a pure fn
        "firstclass": IN_SCOPE,  # the unnameable `*`: an entry cannot be scoped
        "seam": "a `cache`-declaring method split across a PROCESS seam is "
                "refused by `placement.cache_crossing_refusal` (the entry store "
                "is single-process and WAL-ordered); this fold reads one "
                "composition's reach and has no process or tier notion",
        "nested_res": "a resource nested in a record/variant/generic is refused "
                      "at COMPILE by the structural resource-in-entry walk "
                      "(`lower._check_cache_resource`), which reads the method's "
                      "TYPES; this fold reads its reach, not its signature",
    },
}


# ===========================================================================
# 4. META-TESTS - the enumeration and the matrix are complete.
# ===========================================================================

def test_every_surface_classifies_every_crossing_kind():
    """Totality: each surface must classify EVERY crossing kind, and no others.

    This is the checklist the type system cannot forget: add a seventh entry to
    CROSSING_KINDS and every surface fails here until it decides in_scope-or-exempt
    for the new kind. Drop a kind's handling and the surface's own map goes stale."""
    kinds = set(CROSSING_KINDS)
    assert kinds, "the enumeration is non-empty"
    for surface, classification in SURFACES.items():
        assert set(classification) == kinds, (
            f"surface {surface!r} must classify exactly {sorted(kinds)}; "
            f"got {sorted(classification)}")


def test_every_crossing_kind_is_covered_by_some_surface():
    """No crossing kind may be exempt EVERYWHERE - each must be in_scope for at
    least one surface, or it is a kind of boundary crossing nothing derives
    authority from (which would itself be the hole this harness exists to catch)."""
    covered = {
        kind
        for classification in SURFACES.values()
        for kind, verdict in classification.items()
        if verdict == IN_SCOPE
    }
    missing = set(CROSSING_KINDS) - covered
    assert not missing, f"crossing kinds no surface accounts for: {sorted(missing)}"


def test_every_exemption_carries_a_reason():
    """An exemption is a documented decision, never a bare skip: every non-in_scope
    cell must carry a human reason naming the axis / sibling surface that owns it."""
    for surface, classification in SURFACES.items():
        for kind, verdict in classification.items():
            if verdict != IN_SCOPE:
                assert isinstance(verdict, str) and len(verdict) > 20, (
                    f"{surface}.{kind} exemption needs a real reason, got {verdict!r}")


def _in_scope(surface: str) -> frozenset[str]:
    return frozenset(k for k, v in SURFACES[surface].items() if v == IN_SCOPE)


# ===========================================================================
# 5. THE DIFFERENTIALS - each IN_SCOPE cell is discriminating.
# ===========================================================================
#
# For each surface the reach/refusal set is read WITH a kind and WITHOUT it. The
# kind's authority must be present in the first and absent in the second. If the
# surface stopped visiting the kind, the two sets would be equal and the cell goes
# RED - so `test_*_visits` cannot be satisfied by a fold that ignores the kind.

# --- the three capability-enumeration surfaces share the reach zoo ------------
_REACH_SURFACES = {
    "boundary": _boundary_reach,
    "component_reach": _component_reach,
    "approval": _approval_reach,
}


@pytest.mark.parametrize("surface", sorted(_REACH_SURFACES))
def test_reach_surface_visits_every_in_scope_kind(surface):
    probe = _REACH_SURFACES[surface]
    scoped = _in_scope(surface) & REACH_KINDS
    # every in_scope kind of a reach surface is a reach-zoo kind
    assert _in_scope(surface) == scoped, (
        f"{surface} in_scope kinds must all be reach-zoo kinds: "
        f"{sorted(_in_scope(surface) - scoped)} are not")
    full = probe(REACH_KINDS)
    for kind in sorted(scoped):
        without = probe(REACH_KINDS - {kind})
        introduced = full - without
        assert introduced, (
            f"{surface} did not change when {kind!r} ({CROSSING_KINDS[kind]}) was "
            f"removed - the fold does not visit this crossing kind (a security hole)")
        # and the without-kind set must not already contain what the kind introduced
        assert not (introduced & without), (
            f"{surface}: {kind!r}'s authority leaked into the without-kind set")


# --- the taint origin fold ----------------------------------------------------

def test_taint_fold_visits_every_in_scope_kind():
    scoped = _in_scope("taint")
    assert scoped == TAINT_KINDS, (
        f"taint in_scope kinds {sorted(scoped)} must match the taint-zoo kinds "
        f"{sorted(TAINT_KINDS)}")
    full = _taint_reach(TAINT_KINDS)
    # all five distinct origins are present at once
    assert full == frozenset({"web", "net", "fs", "input", "model"}), full
    for kind in sorted(scoped):
        without = _taint_reach(TAINT_KINDS - {kind})
        introduced = full - without
        assert introduced, (
            f"taint fold did not change when {kind!r} ({CROSSING_KINDS[kind]}) was "
            f"removed - a tainted value crossing this way is a false-clean")


# --- the item-256 secret raise ------------------------------------------------

def test_secret_raise_fires_at_every_in_scope_crossing_kind():
    """The §4a.4 guardrail: the bound-key raise fires at ALL five §4a.2 crossings
    - the `emit` arm, the plain extern call, the unnameable indirect / `*`
    callable, the provide-method return across the service/MCP bridge, and a
    secret nested in a record. Each cell is DIFFERENTIAL: the crossing raises
    G-SECRET WITH the key and compiles WITHOUT it, so a fold that stopped visiting
    a crossing (the recurring bug shape) would compile the tainted program and
    turn this RED. This is what makes "refused at every crossing" load-bearing
    rather than a spot check that could silently rot as new crossings are added."""
    scoped = _in_scope("secret_raise")
    assert scoped == SECRET_KINDS, (
        f"secret_raise in_scope kinds {sorted(scoped)} must match the secret-zoo "
        f"kinds {sorted(SECRET_KINDS)}")
    for kind in sorted(scoped):
        assert _secret_raises(kind, tainted=True), (
            f"the bound key was NOT refused at {kind!r} ({CROSSING_KINDS[kind]}) - "
            f"a fold missed this crossing (a security hole: the key can leave)")
        assert not _secret_raises(kind, tainted=False), (
            f"the {kind!r} crossing refused even a CLEAN value - the raise does not "
            f"depend on the bound key (a non-discriminating cell)")


# --- the untrusted-author reach sweep -----------------------------------------

def test_untrusted_author_sweep_visits_every_in_scope_kind():
    scoped = _in_scope("untrusted_author")
    assert scoped == frozenset({"extern", "firstclass"}), sorted(scoped)
    # baseline: a turn reaching NO host extern admits (the without-kind side)
    assert _untrusted_admits(_UNTRUSTED_CLEAN, {"h.rvl": _PURE_TOOL})
    # extern: a transitively-reached host extern is refused
    assert not _untrusted_admits(_UNTRUSTED_EXTERN_REACH, {"sh.rvl": _SHELL_TOOL})
    # firstclass: the host extern reached ONLY as a first-class value is refused too
    assert not _untrusted_admits(_UNTRUSTED_FIRSTCLASS_REACH, {"sh.rvl": _SHELL_TOOL})


# --- the distribution-seam resource refusal -----------------------------------

def test_dist_seam_refusal_visits_every_in_scope_kind():
    scoped = _in_scope("dist_seam")
    assert scoped == frozenset({"extern", "seam", "nested_res"}), sorted(scoped)
    ir = compile_source(_SEAM_APP, "seam.rvl")

    # extern (the `acquire` base): WITH the acquire extern, `Socket` is a resource
    # and its top-level seam is refused; WITHOUT it, `Socket` is a plain value and
    # the same seam admits.
    assert "Socket" in _resource_taint(ir)
    assert _seam_refuses(ir, "TopSvc")
    ir_no_acquire = compile_source(_SEAM_APP_NO_ACQUIRE, "seam0.rvl")
    assert "Socket" not in _resource_taint(ir_no_acquire)
    assert not _seam_refuses(ir_no_acquire, "ValSvc2")

    # seam (Finding A, tier-agnostic): a resource crossing a PROCESS seam is refused
    # - at both a same-tier (py<->py) and a cross-tier (go<->py) seam - while the
    # SAME resource shared WITHIN one process is not.
    assert _seam_refuses(ir, "TopSvc", host_backend="py", consumer_backend="py")
    assert _seam_refuses(ir, "TopSvc", host_backend="go", consumer_backend="py")
    assert not _seam_refuses(ir, "TopSvc", shared=True)

    # nested_res (Finding C): a resource nested in a record, a variant case payload,
    # a two-level nest, and a closed generic arg are all refused; a genuinely
    # value-typed nested record still admits (no over-refusal).
    for service in ("RecSvc", "VarSvc", "GenSvc"):
        assert _seam_refuses(ir, service), service
    assert not _seam_refuses(ir, "ValSvc")


# --- the item-251 blast-radius fold -------------------------------------------

def _blast_251_reach(kinds: frozenset[str]) -> frozenset[str]:
    """Surface: the item-251 blast-radius fold. The class-(c) capability set the
    fold enumerates for `Hub`'s `hive.go` closure, via the SAME approval
    `ClassMap` closure the runtime consent path uses (`class_c_capabilities`) - so
    a fold that stopped visiting a crossing kind would drop that kind's capability
    and the differential would go RED (the blast would silently understate)."""
    ir = compile_source(_reach_zoo(kinds), "reach.rvl")
    return class_c_capabilities(ir, "hive", "go")


def _hub_grant(caps, **extra):
    """One ledger record carrying `Hub`'s class-(c) crossings, the shape the blast
    fold partitions."""
    return {"component": "Hub", "session": "s", "operator": "op", "realm": "",
            "classCCapabilities": sorted(caps), **extra}


def test_distill_blast_251_fold_visits_every_in_scope_kind():
    """Each IN_SCOPE cell is DIFFERENTIAL: the fold's class-(c) enumeration is read
    WITH a crossing kind and WITHOUT it, and the kind's capability must be present
    in the first and absent in the second. A fold that stopped visiting the kind
    would make the two sets equal and this goes RED - the blast-radius understating
    what the rule admits is exactly the CRITICAL shape item 414 exists to catch."""
    scoped = _in_scope("distill_blast_251")
    assert scoped == frozenset({"req", "spawn", "extern", "firstclass"}), sorted(scoped)
    # every in_scope kind is a reach-zoo kind (the fold shares the reach axis).
    assert scoped <= REACH_KINDS, sorted(scoped - REACH_KINDS)
    full = _blast_251_reach(REACH_KINDS)
    for kind in sorted(scoped):
        without = _blast_251_reach(REACH_KINDS - {kind})
        introduced = full - without
        assert introduced, (
            f"the 251 blast fold did not change when {kind!r} "
            f"({CROSSING_KINDS[kind]}) was removed - the fold does not visit this "
            f"crossing kind, so a rule's blast radius would understate it")
        assert not (introduced & without), (
            f"distill_blast_251: {kind!r}'s authority leaked into the without-kind set")

    # and the fold PARTITIONS every enumerated crossing: a rule naming each
    # class-(c) capability covers one grant per capability, so the fold visited
    # each (total == the enumeration size, covered == total).
    wide = AutoApproveRule("Hub", tuple(sorted(full)), realm=None,
                           admitting=TAINT_FOLD_ORIGINS)
    blast = blast_radius(wide, [_hub_grant(full)])
    assert blast.total == len(full) == blast.covered


def test_distill_blast_251_resource_scope_cell():
    """The resource-scope cell (§3.3): the fold operates on the BOUND RESOURCE
    VALUATION, not `argsDigest`. A crossing that carries a `_REGISTRY` resource
    param (host) but reaches the fold WITHOUT its resource scope projected is a
    visible red - a host-scoped rule must NOT count the un-projected (bare)
    crossing as covered, exactly the surface the N1 CRITICAL slipped through when
    the destination lived only in the hash."""
    rule = AutoApproveRule("Hub", ('gateway.send(host="api.stripe.com")',),
                           realm=None, admitting=TAINT_FOLD_ORIGINS)
    projected = _hub_grant(
        ["gateway.send"],
        resourceScopes={"gateway.send": 'gateway.send(host="api.stripe.com")'})
    unprojected = _hub_grant(["gateway.send"])          # no resource scope recorded
    assert blast_radius(rule, [projected]).covered == 1
    # the un-projected crossing reds: it is NOT silently bucketed as a bare match.
    bare = blast_radius(rule, [unprojected])
    assert bare.covered == 0
    assert all(nc.reason == "resource" for nc in bare.not_covered)


def test_distill_blast_251_enforcement_tier_taint_cell():
    """The enforcement-tier taint cell (§3.3, H2): a taint-RELEVANT crossing whose
    admission taint set comes back EMPTY must see the static floor (all five
    origins), so it reds the differential against an untainted rule - proving the
    floor substitution is wired and not defaulting to `{}`."""
    rule = AutoApproveRule("Hub", ("gateway.send",), realm=None,
                           admitting=frozenset())        # admits nothing
    relevant_empty = _hub_grant(["gateway.send"], taintRelevant=True, taintOrigins=[])
    floored = blast_radius(rule, [relevant_empty])
    assert floored.covered == 0                          # empty set floored to five
    assert all(nc.reason == "taint" for nc in floored.not_covered)
    # a genuinely non-taint-relevant crossing is still covered (no over-refusal),
    # so the cell is discriminating rather than a blanket refusal.
    clean = blast_radius(rule, [_hub_grant(["gateway.send"])])
    assert clean.covered == 1


# ===========================================================================
# 6. THE FULL COMPOSITION - one fixture exercises every crossing kind at once,
#    and the surfaces that share an axis agree on what it reaches.
# ===========================================================================

def test_one_composition_exercises_every_crossing_kind():
    """The union of the two zoos plus the seam fixture exercises all six crossing
    kinds - the harness is not asserting over an empty corpus."""
    exercised = REACH_KINDS | TAINT_KINDS | {"seam", "nested_res"}
    assert exercised == set(CROSSING_KINDS), (
        f"the fixtures must exercise every crossing kind; "
        f"missing: {sorted(set(CROSSING_KINDS) - exercised)}")


def test_reach_surfaces_agree_on_the_full_zoo():
    """`_boundary`, `component_reach` and the approval fold all read the same
    composition; each must see the authority every reach-zoo kind introduces (no
    surface silently narrower than the others on their shared axis)."""
    full_boundary = _boundary_reach(REACH_KINDS)
    full_creach = _component_reach(REACH_KINDS)
    full_approval = _approval_reach(REACH_KINDS)
    # the req and spawn capability tokens surface identically on all three
    for token in ("cap_req", "cap_spawn"):
        assert token in full_boundary, token
        assert token in full_creach, token
        assert token in full_approval, token
    # item 247: the transitively-reached host extern is declared
    # `emission[cap_ext]`, so ALL THREE surfaces key it on the declared TOKEN.
    # Before 247 the two enumeration surfaces said `host_extern` while the
    # approval fold said `cap_ext` — the same crossing graded in two namespaces,
    # which is how `capability cap_ext requires register keyed` selected nothing.
    for surface in (full_boundary, full_creach, full_approval):
        assert "cap_ext" in surface
        assert "host_extern" not in surface
    # the extern NAME is not lost: the audit's `externs` table is the host-code
    # enumeration (class, backends, ref provenance) and stays keyed by name.
    audit = audit_report(compile_source(_reach_zoo(REACH_KINDS), "reach.rvl"))
    assert "host_extern" in {e["name"]
                             for e in audit["boundary"]["Hub"]["externs"]}
    # the first-class launder is an UNSCOPED `emission` extern, so its token is
    # its own name on the enumeration axis, and the unnameable `*` on the
    # approval axis (what runs through a dispatcher is not statically boundable)
    assert "launder_write" in full_boundary
    assert "launder_write" in full_creach
    assert "*" in full_approval


# --- item 310 surface H: the cache applicability fold -------------------------

def test_cache_applicability_fold_visits_every_in_scope_kind():
    """Each in_scope kind contributes a uniquely-named uncacheable crossing; the
    fold's visit set must lose exactly that token when the kind is removed. A
    fold that stopped following one seam would answer identically with and
    without it - and would then admit a cache over that seam's crossing."""
    scoped = _in_scope("cache_applicability_310")
    assert scoped == CACHE_KINDS, (
        f"cache in_scope kinds {sorted(scoped)} must match the cache-zoo kinds "
        f"{sorted(CACHE_KINDS)}")
    full = _cache_applicability_reach(CACHE_KINDS)
    for kind in sorted(scoped):
        without = _cache_applicability_reach(CACHE_KINDS - {kind})
        introduced = full - without
        assert introduced, (
            f"the cache applicability fold did not change when {kind!r} "
            f"({CROSSING_KINDS[kind]}) was removed - it does not visit this "
            f"crossing kind, so a cache over it would be admitted")
        assert not (introduced & without), (
            f"cache fold: {kind!r}'s crossing leaked into the without-kind set")


def test_cache_applicability_fold_admits_a_plain_emission_reach():
    """The negative half: with every uncacheable kind removed, the method still
    crosses an ordinary emission (`base_read`) - which is exactly what `cache
    capability` is for - and the fold names nothing."""
    assert _cache_applicability_reach(frozenset()) == frozenset()
