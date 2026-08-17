"""revl backend-IR -> Rust emitter for the cordis-rs runtime.

Target: `cordis-rs` 0.3.0 (docs.rs/cordis-rs) — a Rust port of cordis 4.x, the
same runtime model the TypeScript backend targets. `emit(ir) -> str` produces
one Rust module (crate root) from an IR document.

Mapping (DESIGN.md §7 — the backend contract is small):

- service     -> `pub trait <Name>: Send + Sync { fn <m>(&self, ...) -> ... }`
- component   -> `pub fn <snake>() -> PluginHandle` built with
                 `plugin_sync` and `Inject`.
- requires    -> `Inject::new([...])` + `ctx.require::<dyn <Svc>>("key")?`
- provides    -> `impl <Svc> for <Comp><Key> { ... }` +
                 `ctx.provide_arc("key", Arc::new(impl) as Arc<dyn <Svc>>)?`
- effect/undo -> `let x = Arc::new(<acquire>); ctx.effect(label, move || { <undo>; Ok(()) })?;`
- config      -> `#[derive(Clone)] struct <Comp>Config { ... }`, read as `config.<field>`
- emit        -> a plain method call (the emission marker is a revl-checker
                 concern; at runtime it is just a call).
- format      -> `format!(...)`.

Documented spike limits (tracked in docs/v2.0-roadmap.md):

- Host objects (`Pool`/`Map`/`Job`) are emitted as opaque stubs — their real
  Rust forms are host-runtime work, like the wasm tier's "host builtins".
- Provide-method bodies containing `effect`/`emit` statements are stubbed
  `todo!()` (effectful methods need a ctx-carrying design — follow-up). Pure
  delegation bodies are emitted for real.
- Config `default` values are not applied (that belongs in
  `Plugin::validate_config`, which `plugin_sync` does not expose).

CLI: `python3 emit.py <ir.json> [> out.rs]`.
"""

from __future__ import annotations

import json
import re
import sys

__all__ = ["emit", "EmitError"]

CRATE = "cordis-rs"

TYPE_MAP = {
    "Str": "String",
    "Int": "i64",
    "Float": "f64",
    "Bool": "bool",
    "Bytes": "Vec<u8>",
    "Unit": "()",
}

_HOST_STUBS = {
    "Pool": {
        "open": ("Self", ["String", "i64"]),
        "close": ("()", []),
        "query": ("Vec<Value>", ["String"]),
        "execute": ("i64", ["String"]),
    },
    "Map": {
        "new": ("Self", []),
        "drop": ("()", []),
        "insert": ("()", ["String", "String"]),
        "remove": ("()", ["String"]),
        "get": ("Option<String>", ["String"]),
    },
    "Job": {
        "run": ("()", ["String"]),
    },
}

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_RUST_RESERVED = {
    "as", "break", "const", "continue", "crate", "else", "enum", "extern",
    "false", "fn", "for", "if", "impl", "in", "let", "loop", "match", "mod",
    "move", "mut", "pub", "ref", "return", "self", "Self", "static", "struct",
    "super", "trait", "true", "type", "unsafe", "use", "where", "while",
    "async", "await", "dyn",
}
_EMITTER_RESERVED = {"ctx", "config", "root", "plugin"}

# Method names that collide with Rust's `Drop::drop` destructor must be renamed.
_METHOD_RENAMES = {"drop": "drop_"}


def _mname(name: str) -> str:
    return _METHOD_RENAMES.get(name, name)


class EmitError(ValueError):
    """The IR document violates the backend contract."""


def _rust_type(name: object, types: dict | None = None) -> str:
    """Surface type -> Rust type. Unknown named types map to cordis `Value`.

    When `types` is supplied (IR v3), user record/variant names are mapped to
    their emitted Rust type names instead of the opaque `Value` fallback.
    """
    if not isinstance(name, str) or not name:
        return "Value"
    if name in TYPE_MAP:
        return TYPE_MAP[name]
    types = types or {}
    if name in types:
        return _ident(name, "type name")
    generic = re.match(r"^(\w+)\[(.+)\]$", name)
    if generic:
        head, inner = generic.group(1), generic.group(2)
        if head == "List":
            return f"Vec<{_rust_type(inner, types)}>"
        if head == "Opt":
            return f"Option<{_rust_type(inner, types)}>"
        if head == "Map":
            k, v = _split_generic(inner)
            return f"std::collections::HashMap<{_rust_type(k, types)}, {_rust_type(v, types)}>"
        if head == "Result":
            ok, err = _split_generic(inner)
            return f"Result<{_rust_type(ok, types)}, {_rust_type(err, types)}>"
    return "Value"


def _split_generic(inner: str) -> list[str]:
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(inner):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(inner[start:i].strip())
            start = i + 1
    parts.append(inner[start:].strip())
    return parts


def _ident(name: object, role: str) -> str:
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise EmitError(f"invalid {role} identifier: {name!r}")
    if name in _RUST_RESERVED or name in _EMITTER_RESERVED:
        raise EmitError(f"{role} identifier collides with Rust/reserved name: {name!r}")
    return name


def _snake(name: str) -> str:
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0 and (name[i - 1].islower() or name[i - 1].isdigit()):
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def _camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def _string(value: str) -> str:
    return json.dumps(value)


def _rust_lit(node: dict) -> str:
    value = node.get("value")
    if value is None:
        return "None"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, int):
        return f"{value}i64"
    raise EmitError(f"unsupported literal node: {node!r}")


def _rust_v3_lit(node: dict) -> str:
    """v3 literal: strings are owned `String` (revl `Str` is `String`)."""
    value = node.get("value")
    if value is None:
        return "None"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return f"String::from({_string(value)})"
    if isinstance(value, int):
        return f"{value}i64"
    if isinstance(value, float):
        return f"{value}f64"
    raise EmitError(f"unsupported literal node: {node!r}")


def _intercept_json_type(value, base: str, defs: list[str], type_names: dict, counter: list[int]) -> str:
    """Return a concrete Rust type for an intercept metadata value.

    Dictionaries become generated `struct`s (preserving field names) because
    cordis-rs has no serde_json dependency; lists become `Vec<T>`. The generated
    definitions are appended to `defs` so they can be emitted once.
    """
    if isinstance(value, dict):
        if id(value) in type_names:
            return type_names[id(value)]
        counter[0] += 1
        name = f"{base}Intercept{counter[0]}"
        type_names[id(value)] = name
        lines = ["#[derive(Clone)]", f"struct {name} {{"]
        for key, item in value.items():
            field = _ident(key, "intercept field")
            field_type = _intercept_json_type(item, name, defs, type_names, counter)
            lines.append(f"    {field}: {field_type},")
        lines.append("}")
        defs.extend(lines)
        defs.append("")
        return name
    if isinstance(value, list):
        if not value:
            return "Vec<()>"
        item_type = _intercept_json_type(value[0], base, defs, type_names, counter)
        for item in value[1:]:
            if _intercept_json_type(item, base, defs, type_names, counter) != item_type:
                raise EmitError("heterogeneous intercept metadata arrays are not supported")
        return f"Vec<{item_type}>"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "i64"
    if isinstance(value, float):
        return "f64"
    if value is None:
        return "()"
    if isinstance(value, str):
        return "String"
    raise EmitError(f"unsupported intercept metadata value: {value!r}")


def _intercept_json_lit(value, base: str, defs: list[str], type_names: dict, counter: list[int]) -> str:
    if isinstance(value, dict):
        name = _intercept_json_type(value, base, defs, type_names, counter)
        fields = ", ".join(
            f"{_ident(k, 'intercept field')}: {_intercept_json_lit(v, base, defs, type_names, counter)}"
            for k, v in value.items()
        )
        return f"{name} {{ {fields} }}"
    if isinstance(value, list):
        items = ", ".join(
            _intercept_json_lit(item, base, defs, type_names, counter) for item in value
        )
        return f"vec![{items}]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value}i64"
    if isinstance(value, float):
        return f"{value}f64"
    if value is None:
        return "()"
    if isinstance(value, str):
        return f"String::from({_string(value)})"
    raise EmitError(f"unsupported intercept metadata value: {value!r}")


class _Env:
    """Per-component lowering context."""

    def __init__(self, component: dict, services: dict):
        self.component = component
        self.services = services
        self.name = component["name"]
        self.reqs: dict[str, str] = dict(component.get("requires") or {})
        self.provides: dict[str, str] = dict(component.get("provides") or {})


def _expr_arg(node: dict, env: _Env, rename: dict[str, str] | None = None) -> str:
    """Lower a call argument, coercing string literals to `String`."""
    value = _expr(node, env, rename)
    if isinstance(node, dict) and node.get("kind") == "lit" and isinstance(node.get("value"), str):
        return f"String::from({value})"
    return value


def _expr(node: dict, env: _Env, rename: dict[str, str] | None = None) -> str:
    """Lower one IR expression node (v1 component dialect) to a Rust expr."""
    rename = rename or {}
    kind = node.get("kind")
    if kind == "name":
        original = node.get("id")
        if rename and original in rename:
            return rename[original]  # already a Rust expr (e.g. `self.pool`)
        return _ident(original, "binding")
    if kind == "lit":
        return _rust_lit(node)
    if kind == "config":
        field = _ident(node.get("field"), "config field")
        return f"config.{field}.clone()"
    if kind == "host":
        fn = node.get("fn")  # e.g. "Pool.open"
        host, _, method = fn.partition(".")
        args = ", ".join(_expr_arg(a, env, rename) for a in node.get("args") or [])
        return f"{host}::{_mname(method)}({args})"
    if kind == "req":
        original = node.get("name")
        if rename and original in rename:
            return rename[original]
        return _ident(original, "requirement")
    if kind == "call":
        target = node.get("target") or {}
        method = _ident(_mname(node.get("method")), "method")
        args = ", ".join(_expr_arg(a, env, rename) for a in node.get("args") or [])
        if target.get("kind") == "req":
            recv = _ident(target.get("name"), "requirement")
            if rename and target.get("name") in rename:
                recv = rename[target.get("name")]
        else:
            recv = _expr(target, env, rename)
        return f"{recv}.{method}({args})"
    if kind == "format":
        template = node.get("template") or ""
        args = [_expr(a, env, rename) for a in node.get("args") or []]
        return _format(template, args)
    raise EmitError(f"unsupported expression node in Rust backend: {kind!r}")


def _format(template: str, args: list[str]) -> str:
    # IR format templates use `$0`/`$1` placeholders and `$$` for a literal `$`
    # (A4). Map them onto Rust `format!` `{0}`/`{1}`.
    pieces, i, buf = [], 0, []
    while i < len(template):
        ch = template[i]
        if ch == "$":
            if template[i : i + 2] == "$$":
                buf.append("$")
                i += 2
                continue
            j = i + 1
            while j < len(template) and template[j].isdigit():
                j += 1
            if j > i + 1:
                pieces.append("".join(buf) + "{" + template[i + 1 : j] + "}")
                buf = []
                i = j
                continue
        buf.append(ch)
        i += 1
    pieces.append("".join(buf))
    return f"format!({_string(''.join(pieces))}, {', '.join(args)})"


def _emit_service_traits(services: dict) -> list[str]:
    out: list[str] = []
    for sname, service in services.items():
        _ident(sname, "service")
        out.append(f"pub trait {sname}: Send + Sync {{")
        for mname, method in (service.get("methods") or {}).items():
            _ident(mname, "method")
            params = ", ".join(
                f"{_ident(p.get('name'), 'parameter')}: {_rust_type(p.get('type'))}"
                for p in method.get("params") or []
            )
            ret = _rust_type(method.get("returns")) if method.get("returns") else "()"
            if method.get("emission"):
                out.append("    /// emission — crosses the system boundary (DESIGN.md §3.5)")
            out.append(f"    fn {mname}(&self, {params}) -> {ret};")
        out.append("}")
        out.append("")
    return out


def _emit_host_stubs(ir: dict) -> list[str]:
    used: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("kind") == "host":
                used.add((node.get("fn") or "").split(".")[0])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(ir.get("components"))
    walk(ir.get("functions"))
    walk(ir.get("tests"))
    walk(ir.get("types"))
    out: list[str] = []
    for host in sorted(used & _HOST_STUBS.keys()):
        out.append(f"// host object stub ({host}) — real Rust host objects are a follow-up")
        out.append(f"struct {host} {{}}")
        out.append(f"impl {host} {{")
        for mname, (ret, argtypes) in _HOST_STUBS[host].items():
            params = ", ".join(f"_a{i}: {t}" for i, t in enumerate(argtypes))
            is_ctor = ret == "Self"
            ret = host if is_ctor else ret
            recv = "" if is_ctor else "&self, "
            out.append(f"    fn {_mname(mname)}({recv}{params}) -> {ret} {{ todo!() }}")
        out.append("}")
        out.append("")
    return out


def _binds(component: dict) -> list[str]:
    return [s["bind"] for s in component.get("body") or [] if s.get("step") == "let-effect"]


def _host_of(component: dict, bind: str) -> str:
    for s in component.get("body") or []:
        if s.get("step") == "let-effect" and s.get("bind") == bind:
            acquire = s.get("acquire") or {}
            return (acquire.get("fn") or "").split(".")[0] or "Value"
    return "Value"


def _param_type(env: _Env, key: str, mname: str, p: str) -> str:
    service = env.provides[key]
    methods = env.services[service]["methods"]
    for mp in methods.get(mname, {}).get("params", []):
        if mp["name"] == p:
            return mp["type"]
    return "Value"


def _method_return(env: _Env, key: str, mname: str):
    service = env.provides[key]
    return env.services[service]["methods"].get(mname, {}).get("returns")


def _method_body(env: _Env, method: dict) -> str:
    steps = method.get("body") or []
    if len(steps) == 1 and steps[0].get("step") == "return":
        rename = {b: f"self.{b}" for b in _binds(env.component)}
        return _expr(steps[0]["expr"], env, rename)
    return "todo!()"


def _method_body_pure_new(env: _Env, method: dict) -> str:
    steps = method.get("body") or []
    if len(steps) == 1 and steps[0].get("step") == "return":
        return _expr(steps[0]["expr"], env, _method_scope_rename(env))
    return "todo!()"


def _component_has_effectful_methods(component: dict) -> bool:
    for step in component.get("body") or []:
        if step.get("step") != "provide":
            continue
        for method in step.get("methods") or []:
            for body_step in method.get("body") or []:
                if body_step.get("step") in ("effect", "emit"):
                    return True
    return False


def _method_has_effectful_steps(method: dict) -> bool:
    return any(
        body_step.get("step") in ("effect", "emit")
        for body_step in method.get("body") or []
    )


def _method_scope_rename(env: _Env) -> dict[str, str]:
    rename = {b: f"self.{b}" for b in _binds(env.component)}
    for req in env.reqs:
        rename[req] = f"self.{req}"
    return rename


def _method_undo_rename(env: _Env, method: dict) -> dict[str, str]:
    rename = {b: f"{b}_undo" for b in _binds(env.component)}
    for req in env.reqs:
        rename[req] = f"{req}_undo"
    for param in method.get("params") or []:
        rename[param] = f"{param}_undo"
    return rename


def _method_undo_clones(env: _Env, method: dict, out: list[str], indent: int) -> None:
    pad = "    " * indent
    for bind in _binds(env.component):
        out.append(f"{pad}let {bind}_undo = self.{bind}.clone();")
    for req in env.reqs:
        out.append(f"{pad}let {req}_undo = self.{req}.clone();")
    for param in method.get("params") or []:
        out.append(f"{pad}let {param}_undo = {param}.clone();")


def _method_body_lines(env: _Env, method: dict, out: list[str], indent: int) -> None:
    """Lower effect/emit/return steps in a provide-method body.

    Effectful methods get an `Arc<Context>` on the impl struct, so the method
    keeps the plain service-trait signature and effect registration is fire and
    forget (`let _ = self.ctx.effect(...)`), matching the TS backend's
    `ctx.effect(() => ...)` which also cannot propagate a Rust `Result` through
    a non-`Result` method signature.
    """
    pad = "    " * indent
    rename = _method_scope_rename(env)
    for index, step in enumerate(method.get("body") or []):
        kind = step.get("step")
        if kind == "return":
            if step.get("expr") is None:
                out.append(f"{pad}return;")
            else:
                out.append(f"{pad}return {_expr(step['expr'], env, rename)};")
        elif kind in ("effect", "emit"):
            undo_rename = _method_undo_rename(env, method)
            acquire_rename = dict(rename)
            for param in method.get("params") or []:
                acquire_rename[param] = f"{param}.clone()"
            label = _string(f"{env.name}.{method.get('name')}.{kind}.{index}")
            if step.get("compensate") is None:
                _method_undo_clones(env, method, out, indent)
            acquire = _expr(step.get("acquire") or step.get("expr"), env, acquire_rename)
            out.append(f"{pad}let _ = {acquire};")
            if kind == "emit" and step.get("compensate") is None:
                continue
            undo = (
                _expr(step["compensate"], env, undo_rename)
                if kind == "emit"
                else _expr(step["undo"], env, undo_rename)
            )
            out.append(
                f"{pad}let _ = self.ctx.effect({label}, move || {{ {undo}; Ok(()) }});"
            )
        elif kind == "await":
            raise EmitError("await steps are not allowed inside method bodies (A1)")
        else:
            raise EmitError(f"unsupported method body step in Rust backend: {kind!r}")



def _emit_component_new(component: dict, services: dict) -> list[str]:
    """v2 components + effectful provide-method bodies.

    This is used for any component that needs more than the original v1 spike:
    `isolate`, `intercept`, or a provide method containing `effect`/`emit`.
    """
    env = _Env(component, services)
    name = component["name"]
    cname = _ident(name, "component")
    snake = _snake(name)
    isolate = component.get("isolate") or {}
    intercept = component.get("intercept") or {}
    has_effectful = _component_has_effectful_methods(component)

    for local, service in env.reqs.items():
        _ident(local, "requirement")
        if service not in services:
            raise EmitError(f"requirement {local!r} names unknown service {service!r}")
    for key, service in env.provides.items():
        _ident(key, "provision")
        if service not in services:
            raise EmitError(f"provision {key!r} names unknown service {service!r}")
    for key in isolate:
        if key not in env.reqs and key not in env.provides:
            raise EmitError(f"{name}: isolate key {key!r} is not declared")
    for key in intercept:
        if key not in env.reqs:
            raise EmitError(f"{name}: intercept key {key!r} is not a requirement")

    out: list[str] = []

    config_fields = component.get("config") or []
    config_ty = "()" if not config_fields else f"{cname}Config"
    if config_fields:
        out.append("#[derive(Clone)]")
        out.append(f"struct {cname}Config {{")
        for field in config_fields:
            fname = _ident(field.get("name"), "config field")
            out.append(f"    {fname}: {_rust_type(field.get('type'))},")
        out.append("}")
        out.append("")

    for key, service in env.provides.items():
        _ident(key, "provision")
        struct = f"{cname}{_camel(key)}"
        out.append(f"struct {struct} {{")
        for b in _binds(component):
            out.append(f"    {_ident(b, 'binding')}: Arc<{_host_of(component, b)}>,")
        if env.reqs:
            for local, req_service in env.reqs.items():
                out.append(f"    {local}: Arc<Box<dyn {req_service}>>,")
        if has_effectful:
            out.append("    ctx: Arc<cordis::Context>,")
        out.append("}")
        out.append(f"impl {service} for {struct} {{")
        provide = next(
            (s for s in component.get("body") or []
             if s.get("step") == "provide" and s.get("name") == key),
            {"methods": []},
        )
        for method in provide.get("methods") or []:
            original_mname = method.get("name")
            mname = _ident(_mname(original_mname), "method")
            params = ", ".join(
                f"{p}: {_rust_type(_param_type(env, key, original_mname, p))}"
                for p in method.get("params") or []
            )
            ret = (_rust_type(_method_return(env, key, original_mname))
                   if _method_return(env, key, original_mname) else "()")
            if _method_has_effectful_steps(method):
                out.append(f"    fn {mname}(&self, {params}) -> {ret} {{")
                _method_body_lines(env, method, out, indent=2)
                out.append("    }")
            else:
                out.append(f"    fn {mname}(&self, {params}) -> {ret} {{ {_method_body_pure_new(env, method)} }}")
        out.append("}")
        out.append("")


    if intercept:
        inject_parts = []
        for local in env.reqs:
            if local in intercept:
                base = f"{cname}{_camel(local)}"
                defs: list[str] = []
                type_names: dict = {}
                counter = [0]
                meta_type = _intercept_json_type(intercept[local], base, defs, type_names, counter)
                meta_lit = _intercept_json_lit(intercept[local], base, defs, type_names, counter)
                out.extend(defs)
                inject_parts.append(f".require_with({_string(local)}, {meta_lit})")
            else:
                inject_parts.append(f".require({_string(local)})")
        inject = "Inject::none()" + "".join(inject_parts)
    else:
        inject = "Inject::none()" if not env.reqs else f"Inject::new({_string(list(env.reqs.keys()))})"

    out.append(f"pub fn {snake}() -> cordis::PluginHandle {{")
    out.append(f"    cordis::plugin_sync::<{config_ty}, _>(")
    out.append(f"        {_string(name)},")
    out.append(f"        cordis::{inject},")
    out.append("        |ctx, config| {")
    if isolate:
        for key, realm in isolate.items():
            out.append(
                f"            let ctx = ctx.isolate_with({_string(key)}, "
                f"_revl_realm({_string(realm)}));"
            )
    for local, service in env.reqs.items():
        out.append(f"            let {local} = ctx.require::<Box<dyn {service}>>({_string(local)})?;")
    for step in component.get("body") or []:
        if step.get("step") == "provide":
            key = step.get("name")
            service = step.get("service")
            struct = f"{env.name}{_camel(key)}"
            fields = ", ".join(
                [f"{_ident(b, 'binding')}: {b}.clone()" for b in _binds(env.component)]
                + [f"{local}: {local}.clone()" for local in env.reqs]
                + (["ctx: Arc::new(ctx.clone())"] if has_effectful else [])
            )
            out.append(f"            let {key}_box: Box<dyn {service}> = Box::new({struct} {{ {fields} }});")
            out.append(f"            ctx.provide({_string(key)}, {key}_box)?;")
        else:
            _emit_step(step, env, out, indent=3)
    out.append("            Ok(cordis::PluginOutput::none())")
    out.append("        },")
    out.append("    )")
    out.append("}")
    out.append("")
    return out


def _emit_component_auto(component: dict, services: dict) -> list[str]:
    if (
        not (component.get("isolate") or component.get("intercept"))
        and not _component_has_effectful_methods(component)
    ):
        return _emit_component(component, services)
    return _emit_component_new(component, services)


def _revl_realm_helper() -> list[str]:
    return [
        "fn _revl_realm(label: &str) -> cordis::Isolation {",
        "    use std::collections::hash_map::DefaultHasher;",
        "    use std::hash::{Hash, Hasher};",
        "    let mut hasher = DefaultHasher::new();",
        "    label.hash(&mut hasher);",
        "    cordis::Isolation::from_raw(hasher.finish())",
        "}",
        "",
    ]



def _emit_component(component: dict, services: dict) -> list[str]:
    env = _Env(component, services)
    name = component["name"]
    cname = _ident(name, "component")
    snake = _snake(name)
    out: list[str] = []

    config_fields = component.get("config") or []
    config_ty = "()" if not config_fields else f"{cname}Config"
    if config_fields:
        out.append("#[derive(Clone)]")
        out.append(f"struct {cname}Config {{")
        for field in config_fields:
            fname = _ident(field.get("name"), "config field")
            out.append(f"    {fname}: {_rust_type(field.get('type'))},")
        out.append("}")
        out.append("")

    for key, service in env.provides.items():
        _ident(key, "provision")
        struct = f"{cname}{_camel(key)}"
        out.append(f"struct {struct} {{")
        for b in _binds(component):
            out.append(f"    {_ident(b, 'binding')}: Arc<{_host_of(component, b)}>,")
        out.append("}")
        out.append(f"impl {service} for {struct} {{")
        provide = next(
            (s for s in component.get("body") or []
             if s.get("step") == "provide" and s.get("name") == key),
            {"methods": []},
        )
        for method in provide.get("methods") or []:
            mname = _ident(method.get("name"), "method")
            params = ", ".join(
                f"{p}: {_rust_type(_param_type(env, key, mname, p))}"
                for p in method.get("params") or []
            )
            ret = (_rust_type(_method_return(env, key, mname))
                   if _method_return(env, key, mname) else "()")
            out.append(f"    fn {mname}(&self, {params}) -> {ret} {{ {_method_body(env, method)} }}")
        out.append("}")
        out.append("")

    inject = "Inject::none()" if not env.reqs else f"Inject::new({_string(list(env.reqs.keys()))})"
    out.append(f"pub fn {snake}() -> cordis::PluginHandle {{")
    out.append(f"    cordis::plugin_sync::<{config_ty}, _>(")
    out.append(f"        {_string(name)},")
    out.append(f"        cordis::{inject},")
    out.append("        |ctx, config| {")
    for local, service in env.reqs.items():
        out.append(f"            let {local} = ctx.require::<Box<dyn {service}>>({_string(local)})?;")
    for step in component.get("body") or []:
        _emit_step(step, env, out, indent=3)
    out.append("            Ok(cordis::PluginOutput::none())")
    out.append("        },")
    out.append("    )")
    out.append("}")
    out.append("")
    return out


def _emit_step(step: dict, env: _Env, out: list[str], indent: int) -> None:
    pad = "    " * indent
    kind = step.get("step")
    if kind == "let-effect":
        bind = _ident(step["bind"], "binding")
        acquire = _expr(step["acquire"], env)
        out.append(f"{pad}let {bind} = Arc::new({acquire});")
        undo_name = f"{bind}_undo"
        out.append(f"{pad}let {undo_name} = {bind}.clone();")
        undo = _expr(step["undo"], env, rename={step["bind"]: undo_name})
        label = _string(env.name + "." + step["bind"] + ".undo")
        out.append(f"{pad}ctx.effect({label}, move || {{ {undo}; Ok(()) }})?;")
    elif kind == "effect":
        acquire = _expr(step["acquire"], env)
        undo = _expr(step["undo"], env)
        out.append(f"{pad}let _ = {acquire};")
        label = _string(env.name + ".effect")
        out.append(f"{pad}ctx.effect({label}, move || {{ {undo}; Ok(()) }})?;")
    elif kind == "emit":
        out.append(f"{pad}let _ = {_expr(step['expr'], env)};")
    elif kind == "provide":
        key = step.get("name")
        service = step.get("service")
        struct = f"{env.name}{_camel(key)}"
        fields = ", ".join(f"{_ident(b, 'binding')}: {b}.clone()" for b in _binds(env.component))
        out.append(f"{pad}let {key}_box: Box<dyn {service}> = Box::new({struct} {{ {fields} }});")
        out.append(f"{pad}ctx.provide({_string(key)}, {key}_box)?;")
    else:
        raise EmitError(f"unsupported component step in Rust backend: {kind!r}")


_V3_BIN_OPS = {
    "==": "==",
    "===": "==",
    "!=": "!=",
    "!==": "!=",
    "<": "<",
    ">": ">",
    "<=": "<=",
    ">=": ">=",
    "+": "+",
    "-": "-",
    "*": "*",
    "/": "/",
    "%": "%",
    "&&": "&&",
    "||": "||",
}

_V3_ATOMIC_KINDS = {"var", "field", "index", "call", "lit"}
_V3_HOST_ROOTS = set(_HOST_STUBS)
_V3_BUILTIN_CONSTRUCTORS = {"Some", "None", "Ok", "Err"}


class _V3Ctx:
    """Names visible to lowered IR v3 expression/statement emitters."""

    def __init__(self, types: dict, functions: list, externs: list) -> None:
        self.types = types or {}
        self.function_names = {fn.get("name") for fn in functions or []}
        self.extern_names = {ext.get("name") for ext in externs or []}
        self.case_adt: dict[str, str | None] = {}
        self.case_payload: dict[str, str | None] = {}
        self.record_by_fields: dict[tuple[str, ...], str | None] = {}
        for name, spec in self.types.items():
            _ident(name, "type name")
            if spec.get("kind") == "record":
                key = tuple(sorted((spec.get("fields") or {}).keys()))
                self.record_by_fields[key] = name if key not in self.record_by_fields else None
            elif spec.get("kind") == "variant":
                for case in spec.get("cases") or []:
                    cname = case.get("name")
                    payload = case.get("payload")
                    self.case_payload[cname] = payload
                    if cname in self.case_adt:
                        self.case_adt[cname] = None
                    else:
                        self.case_adt[cname] = name

    def record_type_for_fields(self, fields: list[str]) -> str:
        key = tuple(sorted(fields))
        name = self.record_by_fields.get(key)
        if name is None:
            raise EmitError(
                f"cannot infer Rust struct type for record literal with fields "
                f"{sorted(fields)!r} — no unique record has exactly those fields"
            )
        return _ident(name, "type name")

    def record_type_for_names(self, names: list[str]) -> str:
        wanted = set(names)
        candidates = [
            name
            for name, spec in self.types.items()
            if spec.get("kind") == "record" and wanted <= set(spec.get("fields") or {})
        ]
        if len(candidates) != 1:
            raise EmitError(
                f"cannot infer Rust struct type for record destructuring {names!r}"
            )
        return _ident(candidates[0], "type name")

    def constructor(self, name: str, args: list[str]) -> str:
        adt = self.case_adt.get(name)
        if adt is not None:
            payload = self.case_payload.get(name)
            if payload is None:
                if args:
                    raise EmitError(f"`{name}` takes no payload")
                return f"{adt}::{name}"
            if len(args) != 1:
                raise EmitError(f"`{name}` takes exactly one payload argument")
            return f"{adt}::{name}({args[0]})"
        if name == "None":
            if args:
                raise EmitError("`None` takes no arguments")
            return "None"
        if name in ("Some", "Ok", "Err"):
            if len(args) != 1:
                raise EmitError(f"`{name}` takes exactly one argument")
            return f"{name}({args[0]})"
        if name in self.case_adt:
            raise EmitError(f"ambiguous ADT constructor {name!r}")
        raise EmitError(f"unknown ADT constructor {name!r}")

    def match_pattern(self, arm: dict) -> str:
        pattern = arm.get("pattern")
        if pattern == "_":
            return "_"
        bind = arm.get("bind")
        adt = self.case_adt.get(pattern)
        if adt is not None:
            prefix = f"{adt}::{pattern}"
        elif pattern in _V3_BUILTIN_CONSTRUCTORS:
            prefix = pattern
        elif pattern in self.case_adt:
            raise EmitError(f"ambiguous match case {pattern!r}")
        else:
            raise EmitError(f"unknown match case {pattern!r}")
        if bind:
            return f"{prefix}({_ident(bind, 'match bind')})"
        return prefix



def _v3_expr(node: dict, ctx: _V3Ctx) -> str:
    if not isinstance(node, dict) or "kind" not in node:
        raise EmitError(f"malformed v3 expression: {node!r}")
    kind = node["kind"]

    if kind == "lit":
        return _rust_v3_lit(node)

    if kind == "var":
        name = node.get("name")
        _ident(name, "name")
        if name in ctx.case_adt:
            adt = ctx.case_adt.get(name)
            if adt is not None:
                return f"{adt}::{name}"
            raise EmitError(f"ambiguous ADT case name {name!r}")
        if name in _V3_BUILTIN_CONSTRUCTORS:
            return name
        return name

    if kind == "bin":
        op = _V3_BIN_OPS.get(node.get("op"))
        if op is None:
            raise EmitError(f"unsupported binary operator {node.get('op')!r}")
        return f"({_v3_expr(node['left'], ctx)} {op} {_v3_expr(node['right'], ctx)})"

    if kind == "un":
        operand = _v3_expr(node.get("operand"), ctx)
        if node.get("op") == "!":
            return f"(!{operand})"
        if node.get("op") == "-":
            return f"(-{operand})"
        raise EmitError(f"unsupported unary operator {node.get('op')!r}")

    if kind == "call":
        callee_node = node.get("callee") or {}
        arg_exprs = [_v3_expr(a, ctx) for a in node.get("args") or []]
        args = ", ".join(arg_exprs)
        if callee_node.get("kind") == "field":
            target_node = callee_node.get("target") or {}
            method = callee_node.get("name")
            if target_node.get("kind") == "var" and target_node.get("name") in _V3_HOST_ROOTS:
                return f"{target_node['name']}::{_mname(method)}({args})"
            target = _v3_expr(target_node, ctx)
            if target_node.get("kind") not in _V3_ATOMIC_KINDS:
                target = f"({target})"
            return f"{target}.{_ident(method, 'method')}({args})"
        callee_name = callee_node.get("name") if callee_node.get("kind") == "var" else None
        if callee_name is not None and (
            callee_name in ctx.case_adt or callee_name in _V3_BUILTIN_CONSTRUCTORS
        ):
            return ctx.constructor(callee_name, arg_exprs)
        callee = _v3_expr(callee_node, ctx)
        if callee_node.get("kind") not in _V3_ATOMIC_KINDS:
            callee = f"({callee})"
        return f"{callee}({args})"

    if kind == "field":
        target_node = node.get("target")
        target = _v3_expr(target_node, ctx)
        if target_node.get("kind") not in _V3_ATOMIC_KINDS:
            target = f"({target})"
        return f"{target}.{_ident(node.get('name'), 'field')}"

    if kind == "index":
        target_node = node.get("target")
        target = _v3_expr(target_node, ctx)
        if target_node.get("kind") not in _V3_ATOMIC_KINDS:
            target = f"({target})"
        return f"({target})[{_v3_expr(node['index'], ctx)}].clone()"

    if kind == "if":
        return (
            f"if {_v3_expr(node['cond'], ctx)} {{ {_v3_expr(node['then'], ctx)} }} "
            f"else {{ {_v3_expr(node['else'], ctx)} }}"
        )

    if kind == "record":
        fields = node.get("fields") or []
        type_name = ctx.record_type_for_fields([k for k, _ in fields])
        body = ", ".join(
            f"{_ident(k, 'record field')}: {_v3_expr(v, ctx)}" for k, v in fields
        )
        return f"{type_name} {{ {body} }}"

    if kind == "list":
        return "vec![" + ", ".join(_v3_expr(item, ctx) for item in node.get("items") or []) + "]"

    if kind == "arrow":
        params = ", ".join(_ident(p, "arrow parameter") for p in node.get("params") or [])
        return f"move |{params}| {{ {_v3_expr(node['body'], ctx)} }}"

    if kind == "len":
        target = _v3_expr(node.get("target"), ctx)
        return f"({target}.len() as i64)"

    if kind == "builtin":
        target_node = node.get("target")
        target = _v3_expr(target_node, ctx)
        if target_node.get("kind") not in _V3_ATOMIC_KINDS:
            target = f"({target})"
        args = [_v3_expr(a, ctx) for a in node.get("args") or []]
        return _v3_builtin(node.get("method"), target, args)

    if kind == "match":
        return _v3_match_expr(node, ctx)

    if kind == "interp":
        return _v3_interp(node)

    raise EmitError(f"unsupported v3 expression kind {kind!r}")



def _v3_builtin(method: str, target: str, args: list[str]) -> str:
    if method == "length":
        return f"({target}.len() as i64)"
    if method == "push":
        return f"{{ let mut _v = {target}.clone(); _v.push({args[0]}); _v }}"
    if method == "concat":
        return f"{{ let mut _v = {target}.clone(); _v.extend({args[0]}.iter().cloned()); _v }}"
    if method == "slice":
        return f"{{ let _v = {target}.clone(); _v[{args[0]}..{args[1]}].to_vec() }}"
    if method == "charAt":
        return f"{{ {target}.chars().nth({args[0]}).unwrap().to_string() }}"
    if method == "charCodeAt":
        return f"{{ {target}.chars().nth({args[0]}).unwrap() as u32 as i64 }}"
    if method == "indexOf":
        return f"{{ {target}.find({args[0]}).map(|i| i as i64).unwrap_or(-1) }}"
    raise EmitError(f"unknown builtin method {method!r}")


def _v3_interp(node: dict) -> str:
    parts = node.get("parts") or []
    format_parts: list[str] = []
    args: list[str] = []
    for kind, text in parts:
        if kind == "text":
            format_parts.append(text.replace("{", "{{").replace("}", "}}"))
        else:
            format_parts.append("{}")
            args.append(_ident(text, "interpolation"))
    return f"format!({_string(''.join(format_parts))}, {', '.join(args)})"


def _v3_match_expr(node: dict, ctx: _V3Ctx) -> str:
    scrutinee = _v3_expr(node.get("scrutinee"), ctx)
    arms = node.get("arms") or []
    lines = [f"match {scrutinee} {{"]
    for arm in arms:
        pattern = ctx.match_pattern(arm)
        body = _v3_expr(arm.get("body"), ctx)
        lines.append(f"    {pattern} => {body},")
    if not any(arm.get("pattern") == "_" for arm in arms):
        # lower.py has already checked exhaustiveness for known ADTs.
        lines.append("    _ => unreachable!(),")
    lines.append("}")
    return "\n".join(lines)



def _v3_let_pattern(node: dict, ctx: _V3Ctx, out: list[str], indent: int) -> None:
    pad = "    " * indent
    value = _v3_expr(node.get("value"), ctx)
    pattern = node.get("pattern")
    names = [_ident(n, "binding") for n in node.get("names") or []]
    keyword = "let mut" if node.get("mutable") else "let"
    if pattern == "record":
        type_name = ctx.record_type_for_names(node.get("names") or [])
        fields = ", ".join(names)
        out.append(f"{pad}{keyword} {type_name} {{ {fields} }} = {value};")
    elif pattern == "list":
        tmp = f"_revl_list_{indent}_{len(out)}"
        out.append(f"{pad}let {tmp} = {value};")
        for index, name in enumerate(names):
            out.append(f"{pad}{keyword} {name} = {tmp}[{index}].clone();")
        rest = node.get("rest")
        if rest:
            out.append(f"{pad}{keyword} {_ident(rest, 'binding')} = {tmp}[{len(names)}..].to_vec();")
    else:
        raise EmitError(f"unsupported let_pattern kind {pattern!r}")


def _v3_stmt(node: dict, ctx: _V3Ctx, out: list[str], indent: int, *, test_mode: bool = False) -> None:
    pad = "    " * indent
    step = node.get("step")

    if step in ("let", "assign"):
        name = _ident(node.get("name"), "binding")
        value = _v3_expr(node.get("value"), ctx)
        if step == "let":
            keyword = "let mut" if node.get("mutable") else "let"
            out.append(f"{pad}{keyword} {name} = {value};")
        else:
            out.append(f"{pad}{name} = {value};")
    elif step == "return":
        if node.get("expr") is None:
            out.append(f"{pad}return;")
        else:
            out.append(f"{pad}return {_v3_expr(node['expr'], ctx)};")
    elif step == "if":
        out.append(f"{pad}if {_v3_expr(node['cond'], ctx)} {{")
        for child in node.get("then") or []:
            _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        if node.get("else"):
            out.append(f"{pad}}} else {{")
            for child in node["else"]:
                _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        out.append(f"{pad}}}")
    elif step == "while":
        out.append(f"{pad}while {_v3_expr(node['cond'], ctx)} {{")
        for child in node.get("body") or []:
            _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        out.append(f"{pad}}}")
    elif step == "for":
        bind = _ident(node.get("bind"), "loop binding")
        out.append(f"{pad}for {bind} in {_v3_expr(node['iterable'], ctx)} {{")
        for child in node.get("body") or []:
            _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        out.append(f"{pad}}}")
    elif step == "let_pattern":
        _v3_let_pattern(node, ctx, out, indent)
    elif step == "expr":
        out.append(f"{pad}let _ = {_v3_expr(node['expr'], ctx)};")
    elif step == "assert":
        out.append(f"{pad}assert!({_v3_expr(node['expr'], ctx)});")
    else:
        raise EmitError(f"unsupported fn statement step {step!r}")



def _emit_v3_types(types: dict) -> list[str]:
    out: list[str] = []
    for name, spec in types.items():
        name = _ident(name, "type name")
        if spec.get("kind") == "record":
            out.append("#[derive(Clone, Debug)]")
            out.append(f"pub struct {name} {{")
            for field, ftype in (spec.get("fields") or {}).items():
                out.append(f"    {_ident(field, 'record field')}: {_rust_type(ftype, types)},")
            out.append("}")
        elif spec.get("kind") == "variant":
            out.append("#[derive(Clone, Debug)]")
            out.append(f"pub enum {name} {{")
            for case in spec.get("cases") or []:
                cname = _ident(case.get("name"), "case name")
                payload = case.get("payload")
                if payload is None:
                    out.append(f"    {cname},")
                else:
                    out.append(f"    {cname}({_rust_type(payload, types)}),")
            out.append("}")
        else:
            raise EmitError(f"unsupported type kind {spec.get('kind')!r} for {name!r}")
        out.append("")
    return out


def _emit_v3_functions(functions: list, types: dict, externs: list) -> list[str]:
    ctx = _V3Ctx(types, functions, externs)
    out: list[str] = []
    for fn in functions:
        name = _ident(fn.get("name"), "function name")
        params = ", ".join(
            f"{_ident(p.get('name'), 'parameter name')}: {_rust_type(p.get('type'), types)}"
            for p in fn.get("params") or []
        )
        returns = _rust_type(fn.get("returns"), types)
        visibility = "pub " if fn.get("public") else ""
        out.append(f"{visibility}fn {name}({params}) -> {returns} {{")
        if not fn.get("body"):
            out.append("    todo!()")
        else:
            for stmt in fn["body"]:
                _v3_stmt(stmt, ctx, out, 1)
        out.append("}")
        out.append("")
    return out


def _emit_v3_externs(externs: list, types: dict) -> list[str]:
    out: list[str] = []
    for ext in externs:
        name = _ident(ext.get("name"), "extern name")
        params = ", ".join(
            f"{_ident(p.get('name'), 'extern parameter name')}: {_rust_type(p.get('type'), types)}"
            for p in ext.get("params") or []
        )
        returns = _rust_type(ext.get("returns"), types)
        bodies = ext.get("bodies") or {}
        if "rs" not in bodies:
            raise EmitError(
                f"extern `{name}` has no @rs body — not portable to this backend "
                f"(available: {', '.join(sorted(bodies)) or 'none'})"
            )
        out.append(f"fn {name}({params}) -> {returns} {{")
        body = bodies["rs"].strip()
        if body:
            for line in body.splitlines() or [""]:
                out.append("    " + line)
        else:
            out.append("    // (empty @rs body)")
        out.append("}")
        out.append("")
    return out


def _emit_v3_tests(tests: list, types: dict, functions: list, externs: list) -> list[str]:
    ctx = _V3Ctx(types, functions, externs)
    out: list[str] = []
    used: set[str] = set()
    for test in tests:
        base = _snake(test.get("name") or "test")
        base = re.sub(r"[^A-Za-z0-9_]", "_", base)
        if not base or base[0].isdigit():
            base = "test_" + base
        name = base
        counter = 0
        while name in used:
            counter += 1
            name = f"{base}_{counter}"
        used.add(name)
        out.append("#[test]")
        out.append(f"fn {name}() {{")
        if not test.get("body"):
            out.append("    // (empty test body)")
        else:
            for stmt in test["body"]:
                _v3_stmt(stmt, ctx, out, 1, test_mode=True)
        out.append("}")
        out.append("")
    return out



def _module_header(version: int) -> list[str]:
    allow = (
        "#![allow(dead_code, unused_variables)]"
        if version == 1
        else "#![allow(dead_code, unused_variables, unused_mut, unused_imports, unused_parens, unreachable_patterns)]"
    )
    return [
        f"//! Generated by the revl cordis-rs backend (ir_version {version}) — do not edit.",
        f"//! Target: {CRATE} 0.3.x (docs.rs/cordis-rs).",
        allow,
        "",
        "use std::sync::Arc;",
        "use cordis::Value;",
        "",
    ]


def _needs_realm_helper(components: list) -> bool:
    return any(component.get("isolate") for component in components)


def _emit_components(ir: dict, components: list) -> list[str]:
    out: list[str] = []
    out.extend(_emit_service_traits(ir.get("services") or {}))
    out.extend(_emit_host_stubs(ir))
    if _needs_realm_helper(components):
        out.extend(_revl_realm_helper())
    for component in components:
        out.extend(_emit_component_auto(component, ir.get("services") or {}))
    return out


def _emit_v1(ir: dict) -> str:
    components = ir.get("components") or []
    if not components:
        raise EmitError("IR document has no components")
    out = _module_header(1)
    out.extend(_emit_components(ir, components))
    return "\n".join(out).rstrip() + "\n"


def _emit_v2(ir: dict) -> str:
    components = ir.get("components") or []
    if not components:
        raise EmitError("IR document has no components")
    out = _module_header(2)
    out.extend(_emit_components(ir, components))
    return "\n".join(out).rstrip() + "\n"


def _emit_v3(ir: dict) -> str:
    components = ir.get("components") or []
    types = ir.get("types") or {}
    functions = ir.get("functions") or []
    externs = ir.get("externs") or []
    tests = ir.get("tests") or []
    if not components and not types and not functions and not externs and not tests:
        raise EmitError("IR document has no components, types, functions, externs, or tests")
    out = _module_header(3)
    if types:
        out.extend(_emit_v3_types(types))
    if externs:
        out.extend(_emit_v3_externs(externs, types))
    if functions:
        out.extend(_emit_v3_functions(functions, types, externs))
    if tests:
        out.extend(_emit_v3_tests(tests, types, functions, externs))
    out.extend(_emit_components(ir, components))
    return "\n".join(out).rstrip() + "\n"


def emit(ir: dict) -> str:
    """Emit one Rust module (crate root) for an IR document."""
    if not isinstance(ir, dict):
        raise EmitError("IR document must be a dict")
    version = ir.get("ir_version")
    if version == 1:
        return _emit_v1(ir)
    if version == 2:
        return _emit_v2(ir)
    if version == 3:
        return _emit_v3(ir)
    raise EmitError(
        f"unsupported ir_version: {version!r} — the Rust backend targets "
        f"ir_version 1, 2 (realms), and 3 (types/functions/match)"
    )


def cargo_toml(name: str = "revl_components") -> str:
    """A minimal Cargo.toml for the emitted crate."""
    return (
        "[package]\n"
        f'name = "{name}"\n'
        'version = "0.1.0"\n'
        'edition = "2021"\n'
        "\n"
        "[dependencies]\n"
        'cordis = { package = "cordis-rs", version = "0.3" }\n'
    )


def _main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python3 emit.py <ir.json>", file=sys.stderr)
        return 2
    with open(argv[1], "r", encoding="utf-8") as handle:
        ir = json.load(handle)
    sys.stdout.write(emit(ir))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))




