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


def emit(ir: dict, package: str = "emitted", package_name: str | None = None) -> str:
    # `package_name` is the conformance harness's per-case naming kwarg (the
    # same one the java tier takes); accept it as an alias for `package`.
    if package_name is not None:
        package = package_name
    ver = ir.get("ir_version")
    if ver not in (1, 2):
        raise EmitError("cordis-go backend targets ir_version 1 or 2, got %r" % (ver,))
    for comp in ir.get("components", []):
        if comp.get("spawn") is not None or comp.get("instance") is not None:
            raise EmitError("spawn/instance-parametric IR is out of scope")

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
