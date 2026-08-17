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


def _rust_type(name: object) -> str:
    """Surface type -> Rust type. Unknown named types map to cordis `Value`."""
    if not isinstance(name, str) or not name:
        return "Value"
    if name in TYPE_MAP:
        return TYPE_MAP[name]
    generic = re.match(r"^(\w+)\[(.+)\]$", name)
    if generic:
        head, inner = generic.group(1), generic.group(2)
        if head == "List":
            return f"Vec<{_rust_type(inner)}>"
        if head == "Opt":
            return f"Option<{_rust_type(inner)}>"
        if head == "Map":
            k, v = _split_generic(inner)
            return f"std::collections::HashMap<{_rust_type(k)}, {_rust_type(v)}>"
        if head == "Result":
            ok, err = _split_generic(inner)
            return f"Result<{_rust_type(ok)}, {_rust_type(err)}>"
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


class _Env:
    """Per-component lowering context."""

    def __init__(self, component: dict, services: dict):
        self.component = component
        self.services = services
        self.name = component["name"]
        self.reqs: dict[str, str] = dict(component.get("requires") or {})
        self.provides: dict[str, str] = dict(component.get("provides") or {})


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
        args = ", ".join(_expr(a, env, rename) for a in node.get("args") or [])
        return f"{host}::{_mname(method)}({args})"
    if kind == "req":
        return _ident(node.get("name"), "requirement")
    if kind == "call":
        target = node.get("target") or {}
        method = _ident(_mname(node.get("method")), "method")
        args = ", ".join(_expr(a, env, rename) for a in node.get("args") or [])
        if target.get("kind") == "req":
            recv = _ident(target.get("name"), "requirement")
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


def emit(ir: dict) -> str:
    """Emit one Rust module (crate root) for an IR document."""
    if not isinstance(ir, dict):
        raise EmitError("IR document must be a dict")
    version = ir.get("ir_version")
    if version != 1:
        raise EmitError(
            f"unsupported ir_version: {version!r} — the Rust backend targets "
            f"ir_version 1 (v3 types/functions are a follow-up)"
        )
    components = ir.get("components") or []
    if not components:
        raise EmitError("IR document has no components")

    out: list[str] = []
    out.append("//! Generated by the revl cordis-rs backend (ir_version 1) — do not edit.")
    out.append(f"//! Target: {CRATE} 0.3.x (docs.rs/cordis-rs).")
    out.append("#![allow(dead_code, unused_variables)]")
    out.append("")
    out.append("use std::sync::Arc;")
    out.append("use cordis::Value;")
    out.append("")
    out.extend(_emit_service_traits(ir.get("services") or {}))
    out.extend(_emit_host_stubs(ir))
    for component in components:
        out.extend(_emit_component(component, ir.get("services") or {}))
    return "\n".join(out).rstrip() + "\n"


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




