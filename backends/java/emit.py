"""revl backend-IR -> Java emitter for the cordis4j runtime.

Target: `cordis4j` (github.com/1na-ko/cordis4j) — the JVM port of Cordis.
`emit(ir) -> str` produces one Java source file (interfaces + plugin classes).

Mapping (DESIGN.md §7 — the backend contract is small):

- service     -> `public interface <Name> { <ret> <m>(<params>); }`
- component   -> `public final class <Name>Plugin implements Plugin { apply(ctx) }`
- requires    -> `ctx.get(<Svc>.class)` (manifest load order guarantees the
                 provider is already active; `ctx.inject(...)` is the reactive
                 refinement)
- provides    -> `ctx.provide(ServiceKey.of(<Svc>.class), new <Impl>(...))`
- effect/undo -> acquired value + `Disposables.of(() -> <undo>)`, composed via
                 `Disposables.composite(...)` as the plugin's teardown
- config      -> plugin constructor parameters (`new XPlugin(url, pool_size)`)
- emit        -> a plain call (the emission marker is a revl-checker concern)
- format      -> `String.format(...)`

Documented spike limits (tracked in docs/v2.0-roadmap.md):

- ir_version 1 only; v2/v3 rejected with a clear error.
- Host objects (`Pool`/`Map`/`Job`) are opaque stubs that throw
  `UnsupportedOperationException` (host-runtime work, as in the wasm tier).
- Effectful provide-method bodies are stubbed; pure delegations are real.
- Config `default` values are not applied (host loader territory).

CLI: `python3 emit.py <ir.json> [> out.java]`.
"""

from __future__ import annotations

import json
import re
import sys

__all__ = ["emit", "EmitError"]

CRATE = "cordis4j"

TYPE_MAP = {
    "Str": "String",
    "Int": "long",
    "Float": "double",
    "Bool": "boolean",
    "Bytes": "byte[]",
    "Unit": "void",
}

_HOST_STUBS = {
    "Pool": {
        "open": ("Self", ["String", "long"]),
        "close": ("void", []),
        "query": ("java.util.List<Object>", ["String"]),
        "execute": ("long", ["String"]),
    },
    "Map": {
        "new": ("Self", []),
        "drop": ("void", []),
        "insert": ("void", ["String", "String"]),
        "remove": ("void", ["String"]),
        "get": ("java.util.Optional<String>", ["String"]),
    },
    "Job": {
        "run": ("void", ["String"]),
    },
}

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_JAVA_RESERVED = {
    "abstract", "assert", "boolean", "break", "byte", "case", "catch", "char",
    "class", "const", "continue", "default", "do", "double", "else", "enum",
    "extends", "final", "finally", "float", "for", "goto", "if", "implements",
    "import", "instanceof", "int", "interface", "long", "native", "new",
    "package", "private", "protected", "public", "return", "short", "static",
    "strictfp", "super", "switch", "synchronized", "this", "throw", "throws",
    "transient", "try", "void", "volatile", "while", "var", "record", "yield",
}
_EMITTER_RESERVED = {"ctx", "config", "root"}


class EmitError(ValueError):
    """The IR document violates the backend contract."""


def _java_type(name: object) -> str:
    if not isinstance(name, str) or not name:
        return "Object"
    if name in TYPE_MAP:
        return TYPE_MAP[name]
    generic = re.match(r"^(\w+)\[(.+)\]$", name)
    if generic:
        head, inner = generic.group(1), generic.group(2)
        if head == "List":
            return f"java.util.List<{_java_type(inner)}>"
        if head == "Opt":
            return f"java.util.Optional<{_java_type(inner)}>"
        if head == "Map":
            k, v = _split_generic(inner)
            return f"java.util.Map<{_java_type(k)}, {_java_type(v)}>"
    return "Object"


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
    if name in _JAVA_RESERVED or name in _EMITTER_RESERVED:
        raise EmitError(f"{role} identifier collides with Java/reserved name: {name!r}")
    return name


def _camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def _string(value: str) -> str:
    return json.dumps(value)


def _lit(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, int):
        return f"{value}L"
    if isinstance(value, float):
        return f"{value}d"
    raise EmitError(f"unsupported literal: {value!r}")


class _Env:
    def __init__(self, component: dict, services: dict):
        self.component = component
        self.services = services
        self.name = component["name"]
        self.reqs: dict[str, str] = dict(component.get("requires") or {})
        self.provides: dict[str, str] = dict(component.get("provides") or {})


def _expr(node: dict, env: _Env, rename: dict[str, str] | None = None) -> str:
    rename = rename or {}
    kind = node.get("kind")
    if kind == "name":
        original = node.get("id")
        if rename and original in rename:
            return rename[original]
        return _ident(original, "binding")
    if kind == "lit":
        return _lit(node.get("value"))
    if kind == "config":
        return _ident(node.get("field"), "config field")
    if kind == "host":
        fn = node.get("fn")
        host, _, method = fn.partition(".")
        args = ", ".join(_expr(a, env, rename) for a in node.get("args") or [])
        return f"{host}.{method}({args})"
    if kind == "req":
        return _ident(node.get("name"), "requirement")
    if kind == "call":
        target = node.get("target") or {}
        method = _ident(node.get("method"), "method")
        args = ", ".join(_expr(a, env, rename) for a in node.get("args") or [])
        if target.get("kind") == "req":
            recv = _ident(target.get("name"), "requirement")
        else:
            recv = _expr(target, env, rename)
        return f"{recv}.{method}({args})"
    if kind == "format":
        template = node.get("template") or ""
        args = [_expr(a, env, rename) for a in node.get("args") or []]
        return _format_java(template, args)
    raise EmitError(f"unsupported expression node in Java backend: {kind!r}")


def _format_java(template: str, args: list[str]) -> str:
    # `$0`/`$1` placeholders -> `%s`; `$$` -> literal `$` (A4).
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
                pieces.append("".join(buf) + "%s")
                buf = []
                i = j
                continue
        buf.append(ch)
        i += 1
    pieces.append("".join(buf))
    return f"String.format({_string(''.join(pieces))}, {', '.join(args)})"


def _emit_service_interfaces(services: dict) -> list[str]:
    out: list[str] = []
    for sname, service in services.items():
        _ident(sname, "service")
        out.append(f"public interface {sname} {{")
        for mname, method in (service.get("methods") or {}).items():
            _ident(mname, "method")
            params = ", ".join(
                f"{_java_type(p.get('type'))} {_ident(p.get('name'), 'parameter')}"
                for p in method.get("params") or []
            )
            ret = _java_type(method.get("returns")) if method.get("returns") else "void"
            out.append(f"    {ret} {mname}({params});")
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
        out.append(f"// host object stub ({host}) — real Java host objects are a follow-up")
        out.append(f"public static final class {host} {{")
        for mname, (ret, argtypes) in _HOST_STUBS[host].items():
            params = ", ".join(f"{t} _a{i}" for i, t in enumerate(argtypes))
            is_ctor = ret == "Self"
            ret = host if is_ctor else ret
            mod = "static " if is_ctor else ""
            out.append(
                f"    {mod}{ret} {mname}({params}) "
                f"{{ throw new UnsupportedOperationException(\"host object {host}\"); }}"
            )
        out.append("}")
        out.append("")
    return out


def _binds(component: dict) -> list[str]:
    return [s["bind"] for s in component.get("body") or [] if s.get("step") == "let-effect"]


def _host_of(component: dict, bind: str) -> str:
    for s in component.get("body") or []:
        if s.get("step") == "let-effect" and s.get("bind") == bind:
            acquire = s.get("acquire") or {}
            return (acquire.get("fn") or "").split(".")[0] or "Object"
    return "Object"


def _param_type(env: _Env, key: str, mname: str, p: str) -> str:
    service = env.provides[key]
    for mp in env.services[service]["methods"].get(mname, {}).get("params", []):
        if mp["name"] == p:
            return mp["type"]
    return "Object"


def _method_return(env: _Env, key: str, mname: str):
    service = env.provides[key]
    return env.services[service]["methods"].get(mname, {}).get("returns")


def _method_body(env: _Env, method: dict) -> str:
    steps = method.get("body") or []
    if len(steps) == 1 and steps[0].get("step") == "return":
        rename = {b: f"this.{b}" for b in _binds(env.component)}
        return f"return {_expr(steps[0]['expr'], env, rename)};"
    return 'throw new UnsupportedOperationException("effectful method body");'


def _emit_component(component: dict, services: dict) -> list[str]:
    env = _Env(component, services)
    name = component["name"]
    cname = _ident(name, "component")
    out: list[str] = []

    for key, service in env.provides.items():
        _ident(key, "provision")
        struct = f"{cname}{_camel(key)}"
        out.append(f"public static final class {struct} implements {service} {{")
        for b in _binds(component):
            out.append(f"    private final {_host_of(component, b)} {b};")
        ctor_args = ", ".join(f"{_host_of(component, b)} {b}" for b in _binds(component))
        out.append(f"    {struct}({ctor_args}) {{")
        for b in _binds(component):
            out.append(f"        this.{b} = {b};")
        out.append("    }")
        provide = next(
            (s for s in component.get("body") or []
             if s.get("step") == "provide" and s.get("name") == key),
            {"methods": []},
        )
        for method in provide.get("methods") or []:
            mname = _ident(method.get("name"), "method")
            params = ", ".join(
                f"{_java_type(_param_type(env, key, mname, p))} {p}"
                for p in method.get("params") or []
            )
            ret = _java_type(_method_return(env, key, mname)) if _method_return(env, key, mname) else "void"
            out.append(f"    public {ret} {mname}({params}) {{ {_method_body(env, method)} }}")
        out.append("}")
        out.append("")

    config_fields = component.get("config") or []
    ctor_params = ", ".join(
        f"{_java_type(f.get('type'))} {_ident(f.get('name'), 'config field')}"
        for f in config_fields
    )
    out.append(f"public static final class {cname}Plugin implements Plugin {{")
    for f in config_fields:
        fname = _ident(f.get("name"), "config field")
        out.append(f"    private final {_java_type(f.get('type'))} {fname};")
    out.append(f"    public {cname}Plugin({ctor_params}) {{")
    for f in config_fields:
        fname = _ident(f.get("name"), "config field")
        out.append(f"        this.{fname} = {fname};")
    out.append("    }")
    out.append("    @Override")
    out.append("    public Disposable apply(Context ctx) {")
    for local, service in env.reqs.items():
        out.append(f"        {service} {local} = ctx.get({service}.class);")
    disposers: list[str] = []
    for step in component.get("body") or []:
        kind = step.get("step")
        if kind == "let-effect":
            bind = _ident(step["bind"], "binding")
            out.append(f"        {_host_of(component, step['bind'])} {bind} = {_expr(step['acquire'], env)};")
            undo = _expr(step["undo"], env)
            disposers.append(f"            Disposables.of(() -> {undo})")
        elif kind == "effect":
            out.append(f"        {_expr(step['acquire'], env)};")
            disposers.append(f"            Disposables.of(() -> {_expr(step['undo'], env)})")
        elif kind == "emit":
            out.append(f"        {_expr(step['expr'], env)};")
        elif kind == "provide":
            key = step.get("name")
            service = step.get("service")
            struct = f"{cname}{_camel(key)}"
            ctor_args = ", ".join(b for b in _binds(component))
            out.append(f"        ctx.provide(ServiceKey.of({service}.class), new {struct}({ctor_args}));")
        else:
            raise EmitError(f"unsupported component step in Java backend: {kind!r}")
    if disposers:
        out.append("        return Disposables.composite(")
        out.append(",\n".join(disposers))
        out.append("        );")
    else:
        out.append("        return Disposables.none();")
    out.append("    }")
    out.append("}")
    out.append("")
    return out


def emit(ir: dict, package_name: str = "revl") -> str:
    """Emit one Java source file for an IR document (ir_version 1)."""
    if not isinstance(ir, dict):
        raise EmitError("IR document must be a dict")
    version = ir.get("ir_version")
    if version != 1:
        raise EmitError(
            f"unsupported ir_version: {version!r} — the Java backend targets "
            f"ir_version 1 (v3 types/functions are a follow-up)"
        )
    components = ir.get("components") or []
    if not components:
        raise EmitError("IR document has no components")

    out: list[str] = []
    out.append("// Generated by the revl cordis4j backend (ir_version 1) — do not edit.")
    out.append(f"// Target: {CRATE} (github.com/1na-ko/cordis4j).")
    out.append(f"package {_ident(package_name, 'package')};")
    out.append("")
    out.append("import io.cordis4j.core.Context;")
    out.append("import io.cordis4j.core.Disposable;")
    out.append("import io.cordis4j.core.Disposables;")
    out.append("import io.cordis4j.core.Plugin;")
    out.append("import io.cordis4j.core.ServiceKey;")
    out.append("")
    out.append("public final class Components {")
    out.append("    private Components() {}")
    out.append("")
    out.extend(["    " + line if line else line for line in _emit_service_interfaces(ir.get("services") or {})])
    out.extend(["    " + line if line else line for line in _emit_host_stubs(ir)])
    for component in components:
        out.extend(["    " + line if line else line for line in _emit_component(component, ir.get("services") or {})])
    out.append("}")
    return "\n".join(out).rstrip() + "\n"


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





