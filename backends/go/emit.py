"""revl -> cordis-go backend.

Emits idiomatic Go targeting **github.com/0xdenny218/stc-go** (pinned in
backends/go/scenarios/go.mod) — a Go implementation of the same
spatiotemporal-composability paradigm revl compiles for. `emit(ir) -> str`
produces one Go source file (package `emitted`).

Backend contract (DESIGN.md §7), mapped to stc-go:

  | revl                    | Go (stc-go)                                            |
  |-------------------------|--------------------------------------------------------|
  | service                 | `type <Name> interface { <M>(…) … }`                   |
  | component               | `func <Name>(cfg) stc.Component` (Apply closure)       |
  | requires k: S           | `Inject: []stc.Key{_key_k}` + `stc.Service[S](ctx, …)` |
  | provides k: S           | `impl <Comp>_<k>` + `ctx.Provide(_key_k, S(impl))`     |
  | effect E undo U         | `ctx.Effect(func() stc.Inverse { E; return func()…U })`|
  | isolate k in realm("R") | load-site `ctx.Isolate(_key_k, _revlRealm("R"))`       |
  | emit                    | plain method call                                      |
  | format                  | `fmt.Sprintf(…)`                                        |

Realm placement is applied to the load-target context (the emitted
`Load<Name>` helper), NOT inside the Apply body — isolating inside Apply runs
after stc-go's Inject gate has already evaluated on the un-isolated context,
which strands a realm-scoped consumer in Pending forever. This mirrors the
same fix carried in the Rust backend (`_revl_realm`).

Scope: ir_version 1 and 2 components (effect/inverse, config+defaults,
provide/inject, provide-method bodies, isolate, intercept). v3 pure
functions and spawn/instance-parametric IR are out of scope.
"""

from __future__ import annotations

import json
import re
import sys


class EmitError(ValueError):
    pass


# --------------------------------------------------------------------------
# identifiers / types
# --------------------------------------------------------------------------

def _camel(name: str) -> str:
    """snake_or_lower -> UpperCamel (exported Go identifier)."""
    parts = str(name).replace("-", "_").split("_")
    return "".join(p[:1].upper() + p[1:] for p in parts if p)


def _lower_camel(name: str) -> str:
    c = _camel(name)
    return c[:1].lower() + c[1:] if c else c


def _key_var(name: str) -> str:
    return "_key" + _camel(name)


def _realm_helper_name() -> str:
    return "_revlRealm"


_PRIM = {
    "Str": "string",
    "Int": "int",
    "Float": "float64",
    "Bool": "bool",
    "Unit": "",
}


def _go_type(t) -> str:
    """Map a revl type name to a Go type (value position)."""
    if t is None:
        return ""
    t = str(t).strip()
    if t in _PRIM:
        return _PRIM[t]
    if t.startswith("List[") and t.endswith("]"):
        return "[]" + _go_type(t[5:-1])
    if t.startswith("Opt[") and t.endswith("]"):
        # value position: Opt lowers to the bare type; the (T, bool) tuple
        # only appears in a method's return signature (see _go_return).
        return _go_type(t[4:-1])
    if t == "Row":
        return "Row"
    # Unknown / user type: pass through as an exported identifier.
    return _camel(t)


def _go_return(t):
    """Return-position lowering. Opt[T] -> '(T, bool)'; Unit -> ''."""
    if t is None:
        return ""
    t = str(t).strip()
    if t.startswith("Opt[") and t.endswith("]"):
        return "(%s, bool)" % _go_type(t[4:-1])
    return _go_type(t)


# --------------------------------------------------------------------------
# expression rendering
# --------------------------------------------------------------------------

class _Env:
    """Renders name references. `receiver` is '' at Apply top-level (bare
    locals) or 's' inside a provide-impl method (struct fields)."""

    def __init__(self, binds, reqs, config_fields, params=None, receiver=""):
        self.binds = set(binds)
        self.reqs = set(reqs)
        self.config_fields = set(config_fields)
        self.params = set(params or [])
        self.receiver = receiver

    def name_ref(self, ident: str) -> str:
        if ident in self.params:
            return _safe_local(ident)
        if ident in self.binds:
            return ("s." if self.receiver else "") + _bind_field(ident)
        if ident in self.reqs:
            return ("s." if self.receiver else "") + _req_field(ident)
        # Fall back to a bare local (e.g. a let bind not tracked).
        return _safe_local(ident)

    def config_ref(self, field: str) -> str:
        base = "s.cfg" if self.receiver else "cfg"
        return "%s.%s" % (base, _camel(field))

    def req_ref(self, name: str) -> str:
        return ("s." if self.receiver else "") + _req_field(name)

    def ctx_ref(self) -> str:
        return "s.ctx" if self.receiver else "ctx"


_GO_KEYWORDS = {"type", "range", "func", "map", "chan", "select", "go",
                "defer", "return", "var", "const", "package", "import"}


def _safe_local(name: str) -> str:
    n = str(name)
    return n + "_" if n in _GO_KEYWORDS else n


def _bind_field(name: str) -> str:
    return _lower_camel(name)


def _req_field(name: str) -> str:
    return _lower_camel(name)


def _expr(node, env: _Env) -> str:
    if not isinstance(node, dict):
        raise EmitError("expr must be an object: %r" % (node,))
    kind = node.get("kind")
    if kind == "name":
        return env.name_ref(node["id"])
    if kind == "config":
        return env.config_ref(node["field"])
    if kind == "req":
        return env.req_ref(node["name"])
    if kind == "host":
        fn = node["fn"]  # e.g. "Pool.open" -> PoolOpen
        recv, _, meth = fn.partition(".")
        go = _camel(recv) + _camel(meth)
        args = ", ".join(_expr(a, env) for a in node.get("args", []))
        return "%s(%s)" % (go, args)
    if kind == "call":
        target = _expr(node["target"], env)
        meth = _camel(node["method"])
        args = ", ".join(_expr(a, env) for a in node.get("args", []))
        return "%s.%s(%s)" % (target, meth, args)
    if kind == "format":
        return _format(node["template"], [_expr(a, env) for a in node.get("args", [])])
    if kind == "str":
        return _go_string(node.get("value", ""))
    if kind == "int":
        return str(int(node.get("value", 0)))
    if kind == "bool":
        return "true" if node.get("value") else "false"
    if kind == "lit":
        return _go_literal(node.get("value"))
    raise EmitError("unsupported expr kind: %r" % (kind,))


def _format(template: str, args: list[str]) -> str:
    """revl format template with $0,$1 placeholders -> fmt.Sprintf."""
    out = []
    i = 0
    used = 0
    while i < len(template):
        c = template[i]
        if c == "$" and i + 1 < len(template) and template[i + 1].isdigit():
            j = i + 1
            while j < len(template) and template[j].isdigit():
                j += 1
            out.append("%v")
            used += 1
            i = j
            continue
        if c == "%":
            out.append("%%")
        else:
            out.append(c)
        i += 1
    fmt_str = _go_string("".join(out))
    if not args:
        return "fmt.Sprintf(%s)" % fmt_str
    return "fmt.Sprintf(%s, %s)" % (fmt_str, ", ".join(args))


def _go_string(s: str) -> str:
    return json.dumps(str(s))


# --------------------------------------------------------------------------
# services
# --------------------------------------------------------------------------

def _emit_services(services: dict) -> list[str]:
    out = []
    for sname, sdef in services.items():
        out.append("// service %s" % sname)
        out.append("type %s interface {" % _camel(sname))
        for mname, m in sdef.get("methods", {}).items():
            params = ", ".join(
                "%s %s" % (_safe_local(p["name"]), _go_type(p["type"]))
                for p in m.get("params", [])
            )
            ret = _go_return(m.get("returns"))
            sig = "\t%s(%s)" % (_camel(mname), params)
            if ret:
                sig += " " + ret
            out.append(sig)
        out.append("}")
        out.append("")
    return out


# --------------------------------------------------------------------------
# provide-impl methods
# --------------------------------------------------------------------------

def _collect_refs(body, binds_out, reqs_out):
    """Walk a step/expr tree, recording referenced binds/reqs (best effort;
    over-capture is harmless)."""
    if isinstance(body, dict):
        k = body.get("kind")
        if k == "req":
            reqs_out.add(body["name"])
        if body.get("step") is not None or k is not None:
            for v in body.values():
                _collect_refs(v, binds_out, reqs_out)
    elif isinstance(body, list):
        for x in body:
            _collect_refs(x, binds_out, reqs_out)


def _method_returns_value(m) -> bool:
    return bool(m.get("params") is not None) and _go_return(_method_ret(m)) != ""


def _method_ret(m):
    # provide-block method params are names only; the return type comes from
    # the service declaration, resolved by the caller.
    return m.get("_ret")


def _emit_provide_impl(comp_name, prov_name, service_name, methods, services,
                       binds, reqs, has_config, out):
    """Emit the impl struct + methods for one `provide` block."""
    struct = "%s_%s" % (comp_name, prov_name)
    svc = services.get(service_name, {})
    svc_methods = svc.get("methods", {})

    # struct fields: ctx + config + every bind + every req (over-capture ok).
    out.append("type %s struct {" % struct)
    out.append("\tctx *stc.Context")
    if has_config:
        out.append("\tcfg %sConfig" % _camel(comp_name))
    for b in binds:
        out.append("\t%s *%s" % (_bind_field(b), _host_of_bind(b)))
    for r in reqs:
        out.append("\t%s %s" % (_req_field(r), _camel(_service_of_req(r, services, reqs_map=None))))
    out.append("}")
    out.append("")

    for m in methods:
        mname = m["name"]
        decl = svc_methods.get(mname, {})
        params_decl = decl.get("params", [])
        ret = _go_return(decl.get("returns"))
        # map param names to declared types
        ptypes = {p["name"]: p["type"] for p in params_decl}
        go_params = ", ".join(
            "%s %s" % (_safe_local(pn), _go_type(ptypes.get(pn, "any")))
            for pn in m.get("params", [])
        )
        sig = "func (s *%s) %s(%s)" % (struct, _camel(mname), go_params)
        if ret:
            sig += " " + ret
        sig += " {"
        out.append(sig)
        env = _Env(binds, reqs, _config_fields_flag(has_config),
                   params=m.get("params", []), receiver="s")
        _emit_method_body(m.get("body", []), env, out, 1)
        out.append("}")
        out.append("")


def _config_fields_flag(has_config):
    # placeholder: config field membership isn't needed for ref detection,
    # config refs are explicit ('config' kind). Return empty set.
    return set()


def _emit_method_body(body, env: _Env, out, indent):
    pad = "\t" * indent
    for step in body:
        s = step.get("step")
        if s == "return":
            out.append("%sreturn %s" % (pad, _expr(step["expr"], env)))
        elif s == "effect":
            _emit_effect_step(step, env, out, indent)
        elif s == "emit":
            out.append("%s%s" % (pad, _expr(step["expr"], env)))
        elif s == "let-effect":
            raise EmitError("let-effect not allowed inside a provide method")
        else:
            raise EmitError("unsupported method step: %r" % (s,))


def _emit_effect_step(step, env: _Env, out, indent):
    """A bare `effect ... undo ...` step (inside Apply body or a method)."""
    pad = "\t" * indent
    acquire = _expr(step["acquire"], env)
    undo = step.get("undo")
    ctx = env.ctx_ref()
    out.append("%s%s.Effect(func() stc.Inverse {" % (pad, ctx))
    out.append("%s\t%s" % (pad, acquire))
    if undo is not None:
        out.append("%s\treturn func() error { %s; return nil }" % (pad, _expr(undo, env)))
    else:
        out.append("%s\treturn nil" % pad)
    out.append("%s})" % pad)


# --------------------------------------------------------------------------
# host binding types
# --------------------------------------------------------------------------

_BIND_HOST = {}  # bind name -> host type (populated per component)


def _host_of_bind(bind):
    return _BIND_HOST.get(bind, "any")


_REQ_SERVICE = {}  # req name -> service type


def _service_of_req(name, services, reqs_map):
    return _REQ_SERVICE.get(name, "any")


# --------------------------------------------------------------------------
# components
# --------------------------------------------------------------------------

def _emit_config_struct(comp, out) -> bool:
    cfg = comp.get("config", [])
    if not cfg:
        return False
    name = _camel(comp["name"])
    out.append("type %sConfig struct {" % name)
    for f in cfg:
        out.append("\t%s %s" % (_camel(f["name"]), _go_type(f["type"])))
    out.append("}")
    out.append("")
    # Defaults constructor.
    out.append("func Default%sConfig() %sConfig {" % (name, name))
    out.append("\treturn %sConfig{" % name)
    for f in cfg:
        if f.get("default") is not None:
            out.append("\t\t%s: %s," % (_camel(f["name"]), _default_lit(f["default"], f["type"])))
    out.append("\t}")
    out.append("}")
    out.append("")
    return True


def _default_lit(value, t):
    gt = _go_type(t)
    if gt == "string":
        return _go_string(value)
    if gt == "bool":
        return "true" if value else "false"
    return str(value)


def _emit_component(comp, services, out):
    global _BIND_HOST, _REQ_SERVICE
    name = comp["name"]
    cname = _camel(name)
    requires = comp.get("requires", {}) or {}
    provides = comp.get("provides", {}) or {}
    body = comp.get("body", []) or []

    # per-component maps
    _REQ_SERVICE = dict(requires)
    _BIND_HOST = {}
    for step in body:
        if step.get("step") == "let-effect":
            _BIND_HOST[step["bind"]] = _host_type_of_acquire(step["acquire"])

    binds = [s["bind"] for s in body if s.get("step") == "let-effect"]
    reqs = list(requires.keys())
    has_config = bool(comp.get("config"))

    # config struct
    _emit_config_struct(comp, out)

    # component constructor
    if has_config:
        out.append("func %s(cfg %sConfig) stc.Component {" % (cname, cname))
    else:
        out.append("func %s() stc.Component {" % cname)
    out.append("\treturn stc.Component{")
    out.append("\t\tName: %s," % _go_string(name))
    if reqs:
        out.append("\t\tInject: []stc.Key{%s}," % ", ".join(_key_var(r) for r in reqs))
    if provides:
        out.append("\t\tProvide: []stc.Key{%s}," % ", ".join(_key_var(p) for p in provides))
    out.append("\t\tApply: func(ctx *stc.Context) (stc.Inverse, error) {")

    env = _Env(binds, reqs, set(), receiver="")

    # requires -> Service resolution
    for rname, svc in requires.items():
        out.append("\t\t\t%s, err := stc.Service[%s](ctx, %s)" %
                    (_req_field(rname), _camel(svc), _key_var(rname)))
        out.append("\t\t\tif err != nil {")
        out.append("\t\t\t\treturn nil, err")
        out.append("\t\t\t}")

    # component body steps (let-effect, effect, provide)
    for step in body:
        _emit_component_step(comp, step, services, env, out)

    # underscore-guard binds and reqs so unused locals never break the build.
    for b in binds:
        out.append("\t\t\t_ = %s" % _bind_field(b))
    for r in reqs:
        out.append("\t\t\t_ = %s" % _req_field(r))
    if has_config:
        out.append("\t\t\t_ = cfg")

    out.append("\t\t\treturn nil, nil")
    out.append("\t\t},")
    out.append("\t}")
    out.append("}")
    out.append("")

    # provide-impl structs/methods
    for step in body:
        if step.get("step") == "provide":
            svc = step["service"]
            _emit_provide_impl(cname, step["name"], svc, step.get("methods", []),
                               services, binds, reqs, has_config, out)


def _host_type_of_acquire(acquire):
    if acquire.get("kind") == "host":
        recv = acquire["fn"].split(".")[0]
        return _camel(recv)
    return "any"


def _emit_component_step(comp, step, services, env: _Env, out):
    s = step.get("step")
    cname = _camel(comp["name"])
    if s == "let-effect":
        bind = step["bind"]
        host = _host_type_of_acquire(step["acquire"])
        acquire = _expr(step["acquire"], env)
        undo = step.get("undo")
        out.append("\t\t\tvar %s *%s" % (_bind_field(bind), host))
        out.append("\t\t\tif err := ctx.Effect(func() stc.Inverse {")
        out.append("\t\t\t\t%s = %s" % (_bind_field(bind), acquire))
        if undo is not None:
            out.append("\t\t\t\treturn func() error { %s; return nil }" % _expr(undo, env))
        else:
            out.append("\t\t\t\treturn nil")
        out.append("\t\t\t}); err != nil {")
        out.append("\t\t\t\treturn nil, err")
        out.append("\t\t\t}")
    elif s == "effect":
        # bare effect step at component top level
        acquire = _expr(step["acquire"], env)
        undo = step.get("undo")
        out.append("\t\t\tif err := ctx.Effect(func() stc.Inverse {")
        out.append("\t\t\t\t%s" % acquire)
        if undo is not None:
            out.append("\t\t\t\treturn func() error { %s; return nil }" % _expr(undo, env))
        else:
            out.append("\t\t\t\treturn nil")
        out.append("\t\t\t}); err != nil {")
        out.append("\t\t\t\treturn nil, err")
        out.append("\t\t\t}")
    elif s == "provide":
        pname = step["name"]
        svc = step["service"]
        struct = "%s_%s" % (cname, pname)
        # resolve method return types from service decl for the impl emit
        for m in step.get("methods", []):
            decl = services.get(svc, {}).get("methods", {}).get(m["name"], {})
            m["_ret"] = decl.get("returns")
        # build the impl value, wiring ctx + config + binds + reqs
        fields = ["ctx: ctx"]
        if comp.get("config"):
            fields.append("cfg: cfg")
        for b in [x["bind"] for x in comp.get("body", []) if x.get("step") == "let-effect"]:
            fields.append("%s: %s" % (_bind_field(b), _bind_field(b)))
        for r in (comp.get("requires", {}) or {}).keys():
            fields.append("%s: %s" % (_req_field(r), _req_field(r)))
        out.append("\t\t\t_impl%s := &%s{%s}" % (_camel(pname), struct, ", ".join(fields)))
        out.append("\t\t\tif _, err := ctx.Provide(%s, %s(_impl%s)); err != nil {" %
                   (_key_var(pname), _camel(svc), _camel(pname)))
        out.append("\t\t\t\treturn nil, err")
        out.append("\t\t\t}")
    elif s == "intercept":
        # handled at load site (metadata); no-op in Apply
        pass
    else:
        raise EmitError("unsupported component step: %r" % (s,))


# --------------------------------------------------------------------------
# keys, realm helper, load helpers
# --------------------------------------------------------------------------

def _emit_keys(ir, out):
    seen = {}
    for comp in ir.get("components", []):
        for name, svc in (comp.get("provides", {}) or {}).items():
            seen[name] = svc
        for name, svc in (comp.get("requires", {}) or {}).items():
            seen.setdefault(name, svc)
    for name, svc in seen.items():
        out.append("var %s = stc.NewKey[%s](%s)" % (_key_var(name), _camel(svc), _go_string(name)))
    if seen:
        out.append("")


def _emit_realm_helper(ir, out):
    # emit only if any component isolates a key
    if not any(c.get("isolate") for c in ir.get("components", [])):
        return
    out.append("var (")
    out.append("\t_revlRealmMu sync.Mutex")
    out.append("\t_revlRealmBy = map[string]*stc.Realm{}")
    out.append(")")
    out.append("")
    out.append("// %s interns a realm by name under the root realm so every load" % _realm_helper_name())
    out.append("// site naming the same realm shares the same *stc.Realm (provKey is")
    out.append("// keyed by pointer, not name).")
    out.append("func %s(name string) *stc.Realm {" % _realm_helper_name())
    out.append("\t_revlRealmMu.Lock()")
    out.append("\tdefer _revlRealmMu.Unlock()")
    out.append("\tif r, ok := _revlRealmBy[name]; ok {")
    out.append("\t\treturn r")
    out.append("\t}")
    out.append("\tr := stc.NewRealm(stc.RootRealm(), name)")
    out.append("\t_revlRealmBy[name] = r")
    out.append("\treturn r")
    out.append("}")
    out.append("")


def _emit_load_helpers(ir, out):
    for comp in ir.get("components", []):
        name = comp["name"]
        cname = _camel(name)
        isolate = comp.get("isolate") or {}
        intercept = comp.get("intercept") or {}
        has_config = bool(comp.get("config"))
        sig_arg = "target *stc.Context"
        call_arg = "cfg"
        if has_config:
            out.append("// Load%s isolates the load-target per the component's realm"
                       " placement, then loads it." % cname)
            out.append("func Load%s(target *stc.Context, cfg %sConfig) *stc.Fiber {" % (cname, cname))
        else:
            out.append("func Load%s(target *stc.Context) *stc.Fiber {" % cname)
        if isolate or intercept:
            out.append("\tctx := target.Child()")
            for key, realm in isolate.items():
                out.append("\tctx.Isolate(%s, %s(%s))" %
                           (_key_var(key), _realm_helper_name(), _go_string(realm)))
            for key, meta in intercept.items():
                out.append("\tctx.Intercept(%s, %s)" % (_key_var(key), _go_literal(meta)))
            target_ctx = "ctx"
        else:
            target_ctx = "target"
        if has_config:
            out.append("\treturn %s.Load(%s(cfg))" % (target_ctx, cname))
        else:
            out.append("\treturn %s.Load(%s())" % (target_ctx, cname))
        out.append("}")
        out.append("")


def _go_literal(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return _go_string(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, list):
        return "[]any{%s}" % ", ".join(_go_literal(x) for x in v)
    if isinstance(v, dict):
        return "map[string]any{%s}" % ", ".join(
            "%s: %s" % (_go_string(k), _go_literal(val)) for k, val in v.items())
    return "nil"


# --------------------------------------------------------------------------
# module header + host runtime
# --------------------------------------------------------------------------

def _needs_sync(ir) -> bool:
    return any(c.get("isolate") for c in ir.get("components", [])) or True


_HOST_RUNTIME = r'''// ---- host runtime (minimal, recording) --------------------------------
// A deterministic in-memory stand-in for revl host objects, instrumented so
// scenarios can assert the exact effect/undo order of emitted code.

var _hostMu sync.Mutex
var _hostLog []string

func hostRecord(op string) {
	_hostMu.Lock()
	_hostLog = append(_hostLog, op)
	_hostMu.Unlock()
}

// HostMarks returns an ordered snapshot of host operations.
func HostMarks() []string {
	_hostMu.Lock()
	defer _hostMu.Unlock()
	out := make([]string, len(_hostLog))
	copy(out, _hostLog)
	return out
}

// HostReset clears the host op log (call between scenarios).
func HostReset() {
	_hostMu.Lock()
	_hostLog = nil
	_hostMu.Unlock()
}

// Row is a query result row.
type Row = map[string]string

// Pool is a deterministic in-memory connection pool.
type Pool struct {
	url  string
	size int
}

func PoolOpen(url string, size int) *Pool {
	hostRecord("pool.open")
	return &Pool{url: url, size: size}
}
func (p *Pool) Close()               { hostRecord("pool.close") }
func (p *Pool) Query(sql string) []Row {
	hostRecord("pool.query:" + sql)
	return nil
}
func (p *Pool) Execute(sql string) int {
	hostRecord("pool.execute:" + sql)
	return 0
}

// Map is a thread-safe string map.
type Map struct {
	mu sync.Mutex
	m  map[string]string
}

func MapNew() *Map {
	hostRecord("map.new")
	return &Map{m: map[string]string{}}
}
func (m *Map) Drop() { hostRecord("map.drop") }
func (m *Map) Insert(k, v string) {
	m.mu.Lock()
	m.m[k] = v
	m.mu.Unlock()
}
func (m *Map) Remove(k string) {
	m.mu.Lock()
	delete(m.m, k)
	m.mu.Unlock()
}
func (m *Map) Get(k string) (string, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	v, ok := m.m[k]
	return v, ok
}
'''


# ==========================================================================
# ir_version 3 — the pure / typed-core tier (docs/syntax-2.0.md).
#
# v3 is PURE: no stc-go runtime. It lowers to ordinary Go, so `go build`
# + `go test` IS the execution gate. Structure mirrors backends/rust/emit.py
# (the closest compiled/typed analog) in Go idioms:
#
#   * records            -> Go structs (fields keep their revl names, unexported)
#   * user ADTs/enums    -> a sealed-interface + case-struct tagging, and
#                           `match` -> a Go type switch (mirrors the JAVA tier's
#                           sealed variants; Go has no native sum type)
#   * built-in Opt/Result-> generic sealed interfaces RevlOpt[T]/RevlResult[T,E]
#   * stdlib builtins    -> typed helpers, generics for the List overloads
#   * `test` blocks      -> func TestXxx(t *testing.T) asserting computed values
#
# Reserved-name safety, fn-type parsing and generic-arg splitting are local to
# this section so the v1/v2 path (which must stay byte-identical) is untouched.
# ==========================================================================

_GO_RESERVED = {
    "break", "case", "chan", "const", "continue", "default", "defer", "else",
    "fallthrough", "for", "func", "go", "goto", "if", "import", "interface",
    "map", "package", "range", "return", "select", "struct", "switch", "type",
    "var", "nil", "true", "false", "iota",
}

_V3_PRIM = {
    "Int": "int64",
    "Str": "string",
    "Bool": "bool",
    "Float": "float64",
    "Bytes": "[]byte",
}


def _v3_ident(name, role: str) -> str:
    if not isinstance(name, str) or not name:
        raise EmitError(f"invalid {role} identifier: {name!r}")
    if name in _GO_RESERVED:
        return name + "_"
    return name


def _v3_split_generic(inner: str) -> list[str]:
    """Split `A, B` at top-level commas (depth-aware for nested generics)."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(inner):
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(inner[start:i].strip())
            start = i + 1
    parts.append(inner[start:].strip())
    return parts


def _v3_split_fn_type(name: str):
    """`(A, B) -> C` -> (["A","B"], "C") or None when not a function type."""
    if "->" not in name:
        return None
    depth = 0
    for i in range(len(name) - 1):
        ch = name[i]
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        elif ch == "-" and name[i + 1] == ">" and depth == 0:
            params_src = name[:i].strip()
            returns = name[i + 2:].strip()
            if not (params_src.startswith("(") and params_src.endswith(")")):
                return None
            inner = params_src[1:-1].strip()
            params = [p for p in _v3_split_generic(inner)] if inner else []
            return params, returns
    return None


def _go_v3_type(t, types: dict) -> str:
    """Surface type -> Go type for the v3 tier."""
    if t is None or t == "" or t == "Unit":
        return ""
    t = str(t).strip()
    if t in _V3_PRIM:
        return _V3_PRIM[t]
    fn = _v3_split_fn_type(t)
    if fn is not None:
        params, returns = fn
        rendered = ", ".join(_go_v3_type(p, types) for p in params)
        ret = _go_v3_type(returns, types)
        return f"func({rendered}) {ret}".rstrip()
    if "[" in t and t.endswith("]"):
        head = t[: t.index("[")]
        inner = t[t.index("[") + 1: -1]
        if head == "List":
            return "[]" + _go_v3_type(inner, types)
        if head == "Opt":
            return f"RevlOpt[{_go_v3_type(inner, types)}]"
        if head == "Result":
            ok, err = _v3_split_generic(inner)
            return f"RevlResult[{_go_v3_type(ok, types)}, {_go_v3_type(err, types)}]"
        if head == "Map":
            k, v = _v3_split_generic(inner)
            return f"map[{_go_v3_type(k, types)}]{_go_v3_type(v, types)}"
    if isinstance(types, dict) and t in types:
        return _v3_ident(t, "type name")
    # Unknown named type: pass through as an identifier.
    return _v3_ident(t, "type name")


class _V3GoCtx:
    """Names and layouts visible to the v3 Go expression/statement emitters."""

    def __init__(self, types: dict, functions: list, externs: list) -> None:
        self.types = types or {}
        self.var_types: dict[str, str | None] = {}
        self.function_ret: dict[str, str | None] = {
            fn.get("name"): fn.get("returns") for fn in functions or []
        }
        self.extern_ret: dict[str, str | None] = {
            ex.get("name"): ex.get("returns") for ex in externs or []
        }
        self.case_adt: dict[str, str | None] = {}
        self.case_payload: dict[str, str | None] = {}
        self.record_by_fields: dict[tuple, str | None] = {}
        self.ret_type: str | None = None
        # feature flags set during rendering / used at module assembly
        self.needs_fmt = False
        self.used_stdlib = False
        for name, spec in self.types.items():
            if spec.get("kind") == "record":
                key = tuple(sorted((spec.get("fields") or {}).keys()))
                self.record_by_fields[key] = (
                    name if key not in self.record_by_fields else None
                )
            elif spec.get("kind") == "variant":
                for case in spec.get("cases") or []:
                    cname = case.get("name")
                    self.case_payload[cname] = case.get("payload")
                    self.case_adt[cname] = (
                        None if cname in self.case_adt else name
                    )

    def record_type_for_fields(self, fields: list) -> str:
        key = tuple(sorted(fields))
        name = self.record_by_fields.get(key)
        if name is None:
            raise EmitError(
                "cannot infer Go struct type for record literal with fields "
                f"{sorted(fields)!r} — no unique record has exactly those fields"
            )
        return _v3_ident(name, "type name")


def _v3_builtin_ret_type(method, recv_type):
    if method in ("length", "indexOf", "charCodeAt"):
        return "Int"
    if method in ("charAt", "repeat", "join"):
        return "Str"
    if method == "split":
        return "List[Str]"
    if method in ("slice", "concat", "push"):
        return recv_type
    return None


def _go_v3_infer_type(node, ctx: _V3GoCtx):
    """Surface type of an expression when knowable, else None."""
    if not isinstance(node, dict):
        return None
    kind = node.get("kind")
    if kind == "lit":
        v = node.get("value")
        if isinstance(v, bool):
            return "Bool"
        if isinstance(v, int):
            return "Int"
        if isinstance(v, float):
            return "Float"
        if isinstance(v, str):
            return "Str"
        return None
    if kind in ("var", "name"):
        return ctx.var_types.get(node.get("name") or node.get("id"))
    if kind == "interp":
        return "Str"
    if kind == "field":
        tt = _go_v3_infer_type(node.get("target"), ctx)
        spec = ctx.types.get(tt) if isinstance(tt, str) else None
        if spec and spec.get("kind") == "record":
            return (spec.get("fields") or {}).get(node.get("name"))
        return None
    if kind == "index":
        tt = _go_v3_infer_type(node.get("target"), ctx)
        if isinstance(tt, str) and tt.startswith("List[") and tt.endswith("]"):
            return tt[5:-1]
        return None
    if kind == "builtin":
        return _v3_builtin_ret_type(
            node.get("method"), _go_v3_infer_type(node.get("target"), ctx)
        )
    if kind == "call":
        callee = node.get("callee") or {}
        if callee.get("kind") == "var":
            nm = callee.get("name")
            return ctx.function_ret.get(nm) or ctx.extern_ret.get(nm)
        return None
    if kind == "bin":
        op = node.get("op")
        if op in ("==", "===", "!=", "!==", "<", ">", "<=", ">=", "&&", "||"):
            return "Bool"
        if op == "+":
            lt = _go_v3_infer_type(node.get("left"), ctx)
            rt = _go_v3_infer_type(node.get("right"), ctx)
            if lt == "Str" or rt == "Str":
                return "Str"
            return lt or rt
        if op in ("-", "*", "/", "%"):
            return "Int"
        return None
    if kind == "un":
        if node.get("op") == "!":
            return "Bool"
        return _go_v3_infer_type(node.get("operand"), ctx)
    if kind == "if":
        return (_go_v3_infer_type(node.get("then"), ctx)
                or _go_v3_infer_type(node.get("else"), ctx))
    if kind == "record":
        try:
            return ctx.record_type_for_fields([f[0] for f in node.get("fields") or []])
        except EmitError:
            return None
    if kind == "adt":
        return ctx.case_adt.get(node.get("case"))
    return None


_V3_GO_BIN_OPS = {
    "==": "==", "===": "==", "!=": "!=", "!==": "!=",
    "<": "<", ">": ">", "<=": "<=", ">=": ">=",
    "+": "+", "-": "-", "*": "*", "/": "/", "%": "%", "&&": "&&", "||": "||",
}

_V3_GO_ATOMIC = {"var", "name", "lit", "call", "field", "index", "builtin"}


def _go_v3_lit(node: dict) -> str:
    value = node.get("value")
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "nil"
    if isinstance(value, str):
        return _go_string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    raise EmitError(f"unsupported v3 literal: {node!r}")


def _go_v3_construct(ctx: _V3GoCtx, case: str, arg_renders: list, expected):
    """Render an ADT/Opt/Result construction from a case name + rendered args."""
    adt = ctx.case_adt.get(case)
    if adt is not None:
        # user variant (monomorphic): `<Variant><Case>{...}`
        payload = ctx.case_payload.get(case)
        struct = f"{_v3_ident(adt, 'type name')}{case}"
        if payload is None:
            if arg_renders:
                raise EmitError(f"variant case `{case}` takes no payload")
            return f"{struct}{{}}"
        if len(arg_renders) != 1:
            raise EmitError(f"variant case `{case}` takes exactly one payload")
        return f"{struct}{{Value: {arg_renders[0]}}}"
    # built-in Opt / Result — need the type arguments from the expected type.
    exp = (expected or "").strip() if isinstance(expected, str) else ""
    if case in ("Some", "None"):
        inner = exp[4:-1] if exp.startswith("Opt[") and exp.endswith("]") else "any"
        got = _go_v3_type(inner, ctx.types) or "any"
        if case == "None":
            if arg_renders:
                raise EmitError("`None` takes no arguments")
            return f"RevlNone[{got}]{{}}"
        return f"RevlSome[{got}]{{Value: {arg_renders[0]}}}"
    if case in ("Ok", "Err"):
        if exp.startswith("Result[") and exp.endswith("]"):
            ok, err = _v3_split_generic(exp[7:-1])
        else:
            ok, err = "any", "any"
        got_ok = _go_v3_type(ok, ctx.types) or "any"
        got_err = _go_v3_type(err, ctx.types) or "any"
        payload = arg_renders[0] if arg_renders else ""
        if case == "Ok":
            return f"RevlOk[{got_ok}, {got_err}]{{Value: {payload}}}"
        return f"RevlErr[{got_ok}, {got_err}]{{Value: {payload}}}"
    raise EmitError(f"unknown ADT constructor {case!r}")


def _go_v3_expr(node, ctx: _V3GoCtx, expected=None) -> str:
    if not isinstance(node, dict) or "kind" not in node:
        raise EmitError(f"malformed v3 expression: {node!r}")
    kind = node["kind"]

    if kind == "lit":
        return _go_v3_lit(node)

    if kind in ("var", "name"):
        name = node.get("name") or node.get("id")
        if name in ctx.case_adt and not (node.get("args") is not None):
            # bare nullary variant reference is not expected in the fixtures;
            # constructions arrive as `adt`. Fall through to a plain ident.
            pass
        return _v3_ident(name, "name")

    if kind == "adt":
        args = [_go_v3_expr(a, ctx) for a in node.get("args") or []]
        return _go_v3_construct(ctx, node["case"], args, expected)

    if kind == "bin":
        op = node.get("op")
        if op == "??":
            ctx.used_stdlib = True  # revlOptOr lives in the opt preamble
            left = _go_v3_expr(node["left"], ctx)
            right = _go_v3_expr(node["right"], ctx)
            return f"revlOptOr({left}, {right})"
        go_op = _V3_GO_BIN_OPS.get(op)
        if go_op is None:
            raise EmitError(f"unsupported v3 binary operator {op!r}")
        return f"({_go_v3_expr(node['left'], ctx)} {go_op} {_go_v3_expr(node['right'], ctx)})"

    if kind == "un":
        operand = _go_v3_expr(node.get("operand"), ctx)
        if node.get("op") == "!":
            return f"(!{operand})"
        if node.get("op") == "-":
            return f"(-{operand})"
        raise EmitError(f"unsupported v3 unary operator {node.get('op')!r}")

    if kind == "call":
        callee = node.get("callee") or {}
        arg_renders = [_go_v3_expr(a, ctx) for a in node.get("args") or []]
        if callee.get("kind") == "var" and (
            callee.get("name") in ctx.case_adt
            or callee.get("name") in ("Some", "None", "Ok", "Err")
        ):
            return _go_v3_construct(ctx, callee.get("name"), arg_renders, expected)
        callee_src = _go_v3_expr(callee, ctx)
        return f"{callee_src}({', '.join(arg_renders)})"

    if kind == "field":
        target_node = node.get("target")
        target = _go_v3_expr(target_node, ctx)
        if target_node.get("kind") not in _V3_GO_ATOMIC:
            target = f"({target})"
        return f"{target}.{_v3_ident(node.get('name'), 'field')}"

    if kind == "index":
        target_node = node.get("target")
        target = _go_v3_expr(target_node, ctx)
        if target_node.get("kind") not in _V3_GO_ATOMIC:
            target = f"({target})"
        return f"{target}[{_go_v3_expr(node.get('index'), ctx)}]"

    if kind == "record":
        fields = node.get("fields") or []
        type_name = ctx.record_type_for_fields([k for k, _ in fields])
        body = ", ".join(
            f"{_v3_ident(k, 'record field')}: {_go_v3_expr(v, ctx)}" for k, v in fields
        )
        return f"{type_name}{{{body}}}"

    if kind == "list":
        elem = None
        if isinstance(expected, str) and expected.startswith("List[") and expected.endswith("]"):
            elem = expected[5:-1]
        items = node.get("items") or []
        if elem is None and items:
            elem = _go_v3_infer_type(items[0], ctx)
        go_elem = _go_v3_type(elem, ctx.types) if elem else "any"
        rendered = ", ".join(_go_v3_expr(it, ctx) for it in items)
        return f"[]{go_elem}{{{rendered}}}"

    if kind == "arrow":
        names = node.get("params") or []
        declared = list(node.get("param_types") or [])
        declared += [None] * (len(names) - len(declared))
        params = ", ".join(
            f"{_v3_ident(p, 'arrow parameter')} {_go_v3_type(t, ctx.types) or 'any'}"
            for p, t in zip(names, declared)
        )
        body = _go_v3_expr(node.get("body"), ctx)
        body_t = _go_v3_type(_go_v3_infer_type(node.get("body"), ctx), ctx.types)
        ret = f" {body_t}" if body_t else ""
        return f"func({params}){ret} {{ return {body} }}"

    if kind == "if":
        exp_t = _go_v3_type(expected, ctx.types) if expected else _go_v3_type(
            _go_v3_infer_type(node.get("then"), ctx), ctx.types)
        exp_t = exp_t or "any"
        cond = _go_v3_expr(node.get("cond"), ctx)
        then = _go_v3_expr(node.get("then"), ctx, expected)
        els = _go_v3_expr(node.get("else"), ctx, expected)
        return f"func() {exp_t} {{ if {cond} {{ return {then} }}; return {els} }}()"

    if kind == "builtin":
        target_node = node.get("target")
        target = _go_v3_expr(target_node, ctx)
        if target_node.get("kind") not in _V3_GO_ATOMIC:
            target = f"({target})"
        args = [_go_v3_expr(a, ctx) for a in node.get("args") or []]
        return _go_v3_builtin(ctx, node.get("method"), target_node, target, args)

    if kind == "interp":
        return _go_v3_interp(node, ctx)

    if kind == "match":
        return _go_v3_match(node, ctx, expected)

    if kind == "optfield":
        return _go_v3_optchain(node, ctx, field=node.get("name"))

    if kind == "optcall":
        return _go_v3_optchain(node, ctx, method=node.get("method"),
                               args=node.get("args") or [])

    raise EmitError(f"unsupported v3 expression kind {kind!r} in Go backend")


def _go_v3_builtin(ctx, method, target_node, target, args):
    ctx.used_stdlib = True
    rt = _go_v3_infer_type(target_node, ctx)
    is_str = (rt == "Str")
    if method == "length":
        return f"revlStrLen({target})" if is_str else f"revlListLen({target})"
    if method == "push":
        return f"revlListPush({target}, {args[0]})"
    if method == "concat":
        return (f"revlStrConcat({target}, {args[0]})" if is_str
                else f"revlListConcat({target}, {args[0]})")
    if method == "slice":
        return (f"revlStrSlice({target}, {args[0]}, {args[1]})" if is_str
                else f"revlListSlice({target}, {args[0]}, {args[1]})")
    if method == "indexOf":
        return (f"revlStrIndexOf({target}, {args[0]})" if is_str
                else f"revlListIndexOf({target}, {args[0]})")
    if method == "split":
        return f"revlStrSplit({target}, {args[0]})"
    if method == "join":
        return f"revlJoin({target}, {args[0]})"
    if method == "repeat":
        return f"revlStrRepeat({target}, {args[0]})"
    if method == "charAt":
        return f"revlStrCharAt({target}, {args[0]})"
    if method == "charCodeAt":
        return f"revlStrCharCodeAt({target}, {args[0]})"
    raise EmitError(f"unknown v3 builtin method {method!r}")


def _go_v3_interp(node: dict, ctx: _V3GoCtx) -> str:
    ctx.needs_fmt = True
    fmt_parts: list[str] = []
    args: list[str] = []
    for part_kind, value in node.get("parts") or []:
        if part_kind == "text":
            fmt_parts.append(str(value).replace("%", "%%"))
        else:
            fmt_parts.append("%v")
            args.append(_go_v3_expr(value, ctx))
    joined = "".join(fmt_parts)
    if not args:
        return _go_string(joined)
    return f"fmt.Sprintf({_go_string(joined)}, {', '.join(args)})"


def _go_v3_optchain(node, ctx: _V3GoCtx, *, field=None, method=None, args=None):
    """`?.field` / `?.method(..)` -> revlOptMap over the Opt payload."""
    ctx.used_stdlib = True
    target_node = node.get("target")
    target = _go_v3_expr(target_node, ctx)
    tt = _go_v3_infer_type(target_node, ctx)
    if not (isinstance(tt, str) and tt.startswith("Opt[") and tt.endswith("]")):
        raise EmitError(
            f"optional chaining requires an Opt[..] receiver, got {tt!r}"
        )
    payload = tt[4:-1]
    go_payload = _go_v3_type(payload, ctx.types)
    # infer the closure's return type so Go can infer revlOptMap's B.
    if field is not None:
        spec = ctx.types.get(payload)
        ret_surface = None
        if spec and spec.get("kind") == "record":
            ret_surface = (spec.get("fields") or {}).get(field)
        body = f"_x.{_v3_ident(field, 'field')}"
    else:
        arg_renders = [_go_v3_expr(a, ctx) for a in args or []]
        # optcall receiver payload becomes _x; render the builtin against it.
        ret_surface = _v3_builtin_ret_type(method, payload)
        body = _go_v3_builtin(
            ctx, method, {"kind": "var", "name": "__optx"}, "_x", arg_renders
        )
    go_ret = _go_v3_type(ret_surface, ctx.types) if ret_surface else "any"
    return (f"revlOptMap({target}, func(_x {go_payload}) {go_ret} "
            f"{{ return {body} }})")


def _go_v3_match(node: dict, ctx: _V3GoCtx, expected) -> str:
    scrut_node = node.get("scrutinee")
    scrutinee = _go_v3_expr(scrut_node, ctx)
    st = _go_v3_infer_type(scrut_node, ctx)
    arms = node.get("arms") or []
    exp_t = _go_v3_type(expected, ctx.types) if expected else None
    if not exp_t:
        for arm in arms:
            exp_t = _go_v3_type(_go_v3_infer_type(arm.get("body"), ctx), ctx.types)
            if exp_t:
                break
    exp_t = exp_t or "any"

    # classify the scrutinee's ADT family
    is_opt = isinstance(st, str) and st.startswith("Opt[") and st.endswith("]")
    is_result = isinstance(st, str) and st.startswith("Result[") and st.endswith("]")
    opt_inner = st[4:-1] if is_opt else None
    res_ok = res_err = None
    if is_result:
        res_ok, res_err = _v3_split_generic(st[7:-1])

    lines = [f"func() {exp_t} {{"]
    lines.append(f"\tswitch _m := {scrutinee}.(type) {{")
    has_wild = False
    for arm in arms:
        pattern = arm.get("pattern")
        bind = arm.get("bind")
        if pattern == "_":
            has_wild = True
            lines.append("\tdefault:")
            lines.append("\t\t_ = _m")
            body = _go_v3_expr(arm.get("body"), ctx, expected)
            lines.append(f"\t\treturn {body}")
            continue
        # determine the case struct type and the bound payload's surface type
        if is_opt:
            got = _go_v3_type(opt_inner, ctx.types)
            case_type = (f"RevlSome[{got}]" if pattern == "Some"
                         else f"RevlNone[{got}]")
            payload_surface = opt_inner if pattern == "Some" else None
        elif is_result:
            got_ok = _go_v3_type(res_ok, ctx.types)
            got_err = _go_v3_type(res_err, ctx.types)
            case_type = (f"RevlOk[{got_ok}, {got_err}]" if pattern == "Ok"
                         else f"RevlErr[{got_ok}, {got_err}]")
            payload_surface = res_ok if pattern == "Ok" else res_err
        else:
            adt = st if isinstance(st, str) else ctx.case_adt.get(pattern)
            if adt is None:
                raise EmitError(f"cannot resolve match case {pattern!r}")
            case_type = f"{_v3_ident(adt, 'type name')}{pattern}"
            payload_surface = ctx.case_payload.get(pattern)
        lines.append(f"\tcase {case_type}:")
        if bind:
            ctx.var_types[bind] = payload_surface
            lines.append(f"\t\t{_v3_ident(bind, 'match bind')} := _m.Value")
        else:
            lines.append("\t\t_ = _m")
        body = _go_v3_expr(arm.get("body"), ctx, expected)
        lines.append(f"\t\treturn {body}")
    if not has_wild:
        lines.append("\tdefault:")
        lines.append("\t\t_ = _m")
        lines.append('\t\tpanic("unreachable: non-exhaustive match")')
    lines.append("\t}")
    lines.append("}()")
    return "\n".join(lines)


def _go_v3_stmt(node: dict, ctx: _V3GoCtx, out: list, indent: int, *, t_name=None) -> None:
    pad = "\t" * indent
    step = node.get("step")
    if step == "let":
        name = _v3_ident(node.get("name"), "binding")
        inferred = _go_v3_infer_type(node.get("value"), ctx)
        if inferred is not None:
            ctx.var_types[node.get("name")] = inferred
        value = _go_v3_expr(node.get("value"), ctx, inferred)
        out.append(f"{pad}{name} := {value}")
        out.append(f"{pad}_ = {name}")
    elif step == "assign":
        name = _v3_ident(node.get("name"), "binding")
        value = _go_v3_expr(node.get("value"), ctx, ctx.var_types.get(node.get("name")))
        out.append(f"{pad}{name} = {value}")
    elif step == "return":
        if node.get("expr") is None:
            out.append(f"{pad}return")
        else:
            out.append(f"{pad}return {_go_v3_expr(node['expr'], ctx, ctx.ret_type)}")
    elif step == "if":
        out.append(f"{pad}if {_go_v3_expr(node['cond'], ctx)} {{")
        for child in node.get("then") or []:
            _go_v3_stmt(child, ctx, out, indent + 1, t_name=t_name)
        if node.get("else"):
            out.append(f"{pad}}} else {{")
            for child in node["else"]:
                _go_v3_stmt(child, ctx, out, indent + 1, t_name=t_name)
        out.append(f"{pad}}}")
    elif step == "while":
        out.append(f"{pad}for {_go_v3_expr(node['cond'], ctx)} {{")
        for child in node.get("body") or []:
            _go_v3_stmt(child, ctx, out, indent + 1, t_name=t_name)
        out.append(f"{pad}}}")
    elif step == "for":
        bind = _v3_ident(node.get("bind"), "loop binding")
        it_node = node.get("iterable")
        it_t = _go_v3_infer_type(it_node, ctx)
        if isinstance(it_t, str) and it_t.startswith("List[") and it_t.endswith("]"):
            ctx.var_types[node.get("bind")] = it_t[5:-1]
        out.append(f"{pad}for _, {bind} := range {_go_v3_expr(it_node, ctx)} {{")
        out.append(f"{pad}\t_ = {bind}")
        for child in node.get("body") or []:
            _go_v3_stmt(child, ctx, out, indent + 1, t_name=t_name)
        out.append(f"{pad}}}")
    elif step == "expr":
        out.append(f"{pad}_ = {_go_v3_expr(node['expr'], ctx)}")
    elif step == "assert":
        expr = _go_v3_expr(node["expr"], ctx)
        tn = t_name or "t"
        out.append(f"{pad}if !({expr}) {{")
        out.append(f'{pad}\t{tn}.Fatalf("assertion failed: %s", {_go_string(expr)})')
        out.append(f"{pad}}}")
    else:
        raise EmitError(f"unsupported v3 statement step {step!r}")


def _go_v3_test_name(name, used: set) -> str:
    raw = name if isinstance(name, str) else str(name)
    base = "Test"
    for part in re.split(r"[^A-Za-z0-9]+", raw):
        if part:
            base += part[:1].upper() + part[1:]
    if base == "Test":
        base = "TestCase"
    candidate = base
    i = 1
    while candidate in used:
        i += 1
        candidate = f"{base}{i}"
    used.add(candidate)
    return candidate


def _emit_v3_go_types(types: dict) -> list[str]:
    out: list[str] = []
    for name, spec in types.items():
        gname = _v3_ident(name, "type name")
        if spec.get("kind") == "record":
            out.append(f"type {gname} struct {{")
            for field, ftype in (spec.get("fields") or {}).items():
                out.append(f"\t{_v3_ident(field, 'record field')} {_go_v3_type(ftype, types)}")
            out.append("}")
            out.append("")
        elif spec.get("kind") == "variant":
            marker = f"is{gname}"
            out.append(f"// {gname} is a sealed sum type (revl ADT); {marker}() seals it.")
            out.append(f"type {gname} interface {{ {marker}() }}")
            for case in spec.get("cases") or []:
                cname = case.get("name")
                cstruct = f"{gname}{cname}"
                payload = case.get("payload")
                if payload is None:
                    out.append(f"type {cstruct} struct{{}}")
                else:
                    out.append(f"type {cstruct} struct {{ Value {_go_v3_type(payload, types)} }}")
                out.append(f"func ({cstruct}) {marker}() {{}}")
            out.append("")
        else:
            raise EmitError(f"unsupported v3 type kind {spec.get('kind')!r} for {name!r}")
    return out


def _emit_v3_go_externs(externs: list, ctx: _V3GoCtx) -> list[str]:
    out: list[str] = []
    for ext in externs:
        name = _v3_ident(ext.get("name"), "extern name")
        params = ", ".join(
            f"{_v3_ident(p.get('name'), 'extern parameter')} {_go_v3_type(p.get('type'), ctx.types)}"
            for p in ext.get("params") or []
        )
        ret = _go_v3_type(ext.get("returns"), ctx.types)
        sig_ret = f" {ret}" if ret else ""
        bodies = ext.get("bodies") or {}
        # Require a native @go body — like the rust (@rs) and java (@java) tiers.
        # Never fall back to another tier's body: emitting @ts text as Go only
        # "works" for trivial expressions and would silently ship broken Go for
        # anything else. A missing @go body is a portability boundary, not a gap.
        if "go" not in bodies:
            raise EmitError(
                f"extern `{name}` has no @go body — not portable to this backend "
                f"(available: {', '.join(sorted(bodies)) or 'none'})"
            )
        body = bodies["go"].strip()
        out.append(f"func {name}({params}){sig_ret} {{")
        if body:
            for line in body.splitlines():
                out.append("\t" + line.rstrip())
        else:
            out.append("\t// (empty extern body)")
        out.append("}")
        out.append("")
    return out


def _emit_v3_go_functions(functions: list, ctx: _V3GoCtx) -> list[str]:
    out: list[str] = []
    for fn in functions:
        name = _v3_ident(fn.get("name"), "function name")
        ctx.var_types = {p.get("name"): p.get("type") for p in fn.get("params") or []}
        ctx.ret_type = fn.get("returns")
        params = ", ".join(
            f"{_v3_ident(p.get('name'), 'parameter')} {_go_v3_type(p.get('type'), ctx.types)}"
            for p in fn.get("params") or []
        )
        ret = _go_v3_type(fn.get("returns"), ctx.types)
        sig_ret = f" {ret}" if ret else ""
        out.append(f"func {name}({params}){sig_ret} {{")
        for stmt in fn.get("body") or []:
            _go_v3_stmt(stmt, ctx, out, 1)
        out.append("}")
        out.append("")
    return out


def _emit_v3_go_tests(tests: list, ctx: _V3GoCtx) -> list[str]:
    out: list[str] = []
    used: set = set()
    for test in tests:
        tname = _go_v3_test_name(test.get("name"), used)
        ctx.var_types = {}
        ctx.ret_type = None
        # `revlT` (not `t`) so a user binding named `t` can't shadow it.
        out.append(f"func {tname}(revlT *testing.T) {{")
        for stmt in test.get("body") or []:
            _go_v3_stmt(stmt, ctx, out, 1, t_name="revlT")
        out.append("}")
        out.append("")
    return out


# The pure-tier runtime preamble. Groups are emitted only when used, but every
# helper here is an ordinary package-level declaration — Go never errors on an
# unused func/type, only on unused imports (which is why the group flags gate
# the `import` block, not the helpers).
_V3_OPT_PREAMBLE = '''// ---- built-in Opt as a generic sealed interface -----------------------
type RevlOpt[T any] interface{ isRevlOpt() }
type RevlSome[T any] struct{ Value T }

func (RevlSome[T]) isRevlOpt() {}

type RevlNone[T any] struct{}

func (RevlNone[T]) isRevlOpt() {}

func revlOptMap[A any, B any](o RevlOpt[A], f func(A) B) RevlOpt[B] {
\tif s, ok := o.(RevlSome[A]); ok {
\t\treturn RevlSome[B]{Value: f(s.Value)}
\t}
\treturn RevlNone[B]{}
}

func revlOptOr[T any](o RevlOpt[T], d T) T {
\tif s, ok := o.(RevlSome[T]); ok {
\t\treturn s.Value
\t}
\treturn d
}
'''

_V3_RESULT_PREAMBLE = '''// ---- built-in Result as a generic sealed interface --------------------
type RevlResult[T any, E any] interface{ isRevlResult() }
type RevlOk[T any, E any] struct{ Value T }

func (RevlOk[T, E]) isRevlResult() {}

type RevlErr[T any, E any] struct{ Value E }

func (RevlErr[T, E]) isRevlResult() {}
'''

_V3_STDLIB_PREAMBLE = '''// ---- stdlib builtins (docs/stdlib-2.0.md); positions are code-point based
func revlStrLen(s string) int64 { return int64(utf8.RuneCountInString(s)) }

func revlStrSlice(s string, a, b int64) string {
\tr := []rune(s)
\tn := int64(len(r))
\tif a < 0 {
\t\ta = 0
\t}
\tif a > n {
\t\ta = n
\t}
\tif b > n {
\t\tb = n
\t}
\tif b < a {
\t\tb = a
\t}
\treturn string(r[a:b])
}

func revlStrIndexOf(s, sub string) int64 {
\trs := []rune(s)
\trn := []rune(sub)
\tif len(rn) == 0 {
\t\treturn 0
\t}
\tif len(rn) > len(rs) {
\t\treturn -1
\t}
\tfor i := 0; i+len(rn) <= len(rs); i++ {
\t\tok := true
\t\tfor j := range rn {
\t\t\tif rs[i+j] != rn[j] {
\t\t\t\tok = false
\t\t\t\tbreak
\t\t\t}
\t\t}
\t\tif ok {
\t\t\treturn int64(i)
\t\t}
\t}
\treturn -1
}

func revlStrConcat(a, b string) string { return a + b }

func revlStrSplit(s, sep string) []string {
\tif sep == "" {
\t\tr := []rune(s)
\t\tout := make([]string, len(r))
\t\tfor i, c := range r {
\t\t\tout[i] = string(c)
\t\t}
\t\treturn out
\t}
\treturn strings.Split(s, sep)
}

func revlStrRepeat(s string, n int64) string {
\tif n < 0 {
\t\tn = 0
\t}
\treturn strings.Repeat(s, int(n))
}

func revlStrCharAt(s string, i int64) string {
\tr := []rune(s)
\treturn string(r[i])
}

func revlStrCharCodeAt(s string, i int64) int64 {
\tr := []rune(s)
\treturn int64(r[i])
}

func revlJoin(xs []string, sep string) string { return strings.Join(xs, sep) }

func revlListLen[T any](xs []T) int64 { return int64(len(xs)) }

func revlListSlice[T any](xs []T, a, b int64) []T {
\tn := int64(len(xs))
\tif a < 0 {
\t\ta = 0
\t}
\tif a > n {
\t\ta = n
\t}
\tif b > n {
\t\tb = n
\t}
\tif b < a {
\t\tb = a
\t}
\tout := make([]T, b-a)
\tcopy(out, xs[a:b])
\treturn out
}

func revlListConcat[T any](a, b []T) []T {
\tout := make([]T, 0, len(a)+len(b))
\tout = append(out, a...)
\tout = append(out, b...)
\treturn out
}

func revlListPush[T any](xs []T, x T) []T {
\tout := make([]T, 0, len(xs)+1)
\tout = append(out, xs...)
\tout = append(out, x)
\treturn out
}

func revlListIndexOf[T comparable](xs []T, x T) int64 {
\tfor i, v := range xs {
\t\tif v == x {
\t\t\treturn int64(i)
\t\t}
\t}
\treturn -1
}
'''


def _emit_v3_go(ir: dict, package: str) -> str:
    types = ir.get("types") or {}
    functions = ir.get("functions") or []
    externs = ir.get("externs") or []
    tests = ir.get("tests") or []
    if not types and not functions and not externs and not tests:
        raise EmitError(
            "v3 IR document has no types, functions, externs, or tests to emit"
        )

    ctx = _V3GoCtx(types, functions, externs)
    # Render bodies first so feature flags (fmt / stdlib) settle before imports.
    body: list[str] = []
    if types:
        body.extend(_emit_v3_go_types(types))
    if externs:
        body.extend(_emit_v3_go_externs(externs, ctx))
    if functions:
        body.extend(_emit_v3_go_functions(functions, ctx))
    if tests:
        body.extend(_emit_v3_go_tests(tests, ctx))

    blob = json.dumps({
        "types": types, "functions": functions,
        "externs": externs, "tests": tests,
    })
    used_opt = ("Opt[" in blob) or ('"optfield"' in blob) or ('"optcall"' in blob)
    used_result = "Result[" in blob

    imports: list[str] = []
    if tests:
        imports.append('\t"testing"')
    if ctx.needs_fmt:
        imports.append('\t"fmt"')
    if ctx.used_stdlib:
        imports.append('\t"strings"')
        imports.append('\t"unicode/utf8"')

    out: list[str] = []
    out.append("// Code generated by backends/go/emit.py — DO NOT EDIT.")
    out.append("// revl -> cordis-go (ir_version 3, pure typed-core tier): ordinary Go.")
    out.append(f"package {package}")
    out.append("")
    if imports:
        out.append("import (")
        out.extend(sorted(imports))
        out.append(")")
        out.append("")
    if used_opt:
        out.append(_V3_OPT_PREAMBLE)
    if used_result:
        out.append(_V3_RESULT_PREAMBLE)
    if ctx.used_stdlib:
        out.append(_V3_STDLIB_PREAMBLE)
    out.extend(body)
    return "\n".join(out).rstrip() + "\n"


def emit(ir: dict, package: str = "emitted", package_name: str | None = None) -> str:
    # `package_name` is the conformance harness's per-case naming kwarg (the
    # same one the java tier takes); accept it as an alias for `package`.
    if package_name is not None:
        package = package_name
    ver = ir.get("ir_version")
    if ver not in (1, 2, 3):
        raise EmitError("cordis-go backend targets ir_version 1, 2 or 3, got %r" % (ver,))
    # spawn / instance-parametric IR lives under v3 too, but is a separate
    # feature that is out of scope here — reject only that node, not v3 itself.
    for comp in ir.get("components", []):
        if comp.get("spawn") is not None or comp.get("instance") is not None:
            raise EmitError("spawn/instance-parametric IR is out of scope")

    if ver == 3:
        return _emit_v3_go(ir, package)

    out: list[str] = []
    out.append("// Code generated by backends/go/emit.py — DO NOT EDIT.")
    out.append("// revl -> cordis-go, targeting github.com/0xdenny218/stc-go.")
    out.append("package %s" % package)
    out.append("")
    out.append("import (")
    out.append('\t"fmt"')
    out.append('\t"sync"')
    out.append("")
    out.append('\tstc "github.com/0xdenny218/stc-go"')
    out.append(")")
    out.append("")
    out.append("var _ = fmt.Sprintf")
    out.append("")
    out.append(_HOST_RUNTIME)

    out.extend(_emit_services(ir.get("services", {})))
    _emit_keys(ir, out)
    _emit_realm_helper(ir, out)

    for comp in ir.get("components", []):
        _emit_component(comp, ir.get("services", {}), out)

    _emit_load_helpers(ir, out)

    return "\n".join(out) + "\n"


def main(argv):
    if len(argv) < 2:
        print("usage: emit.py <ir.json> [package]", file=sys.stderr)
        return 2
    package = argv[2] if len(argv) > 2 else "emitted"
    with open(argv[1], encoding="utf-8") as f:
        ir = json.load(f)
    sys.stdout.write(emit(ir, package))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
