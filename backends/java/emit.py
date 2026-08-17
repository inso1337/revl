"""revl backend-IR -> Java emitter for the cordis4j runtime.

Target: `cordis4j` (github.com/1na-ko/cordis4j) — the JVM port of Cordis.
`emit(ir) -> str` produces one Java source file (interfaces + plugin classes).

Mapping (DESIGN.md §7, docs/design-v2-realms.md, docs/syntax-2.0.md):

- service     -> `public interface <Name> { <ret> <m>(<params>); }`
- component   -> `public final class <Name>Plugin implements Plugin { apply(ctx) }`
- requires    -> `ctx.get(<Svc>.class)` (manifest load order guarantees the
                 provider is already active)
- provides    -> `ctx.provide(ServiceKey.of(<Svc>.class), new <Impl>(...))`
- effect/undo -> `Context.EffectScope` (`ctx.effect()`) + `fx.track(...)`;
                 pure v1 components keep the byte-identical
                 `Disposables.composite(...)` teardown path
- isolate     -> `ctx = ctx.isolate(<Svc>.class, <realm>)`
- intercept   -> `ctx.intercept(ServiceKey.of(<Svc>.class), <metadata>)`
- types       -> static final record classes / sealed variant interfaces
- functions   -> `public static` methods on `Components`
- match       -> Java 21 pattern `switch` expressions
- externs     -> verbatim `@java` bodies
- tests       -> static void methods collected in `REVL_TESTS`
- config      -> plugin constructor parameters (`new XPlugin(url, pool_size)`)
- emit        -> a plain call (the emission marker is a revl-checker concern)
- format      -> `String.format(...)`

The emitted file also contains the minimal host-object runtime (`Pool`/`Map`/`Job`)
and applies IR config `default` values through a no-arg plugin constructor.

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

# revl host-method names that are Java reserved words must be renamed at both
# the call site and the runtime-class definition (`Map.new()` is a parse
# error — `new` cannot be a method name).
_HOST_METHOD_RENAMES = {"new": "create"}


def _host_method(name: str) -> str:
    return _HOST_METHOD_RENAMES.get(name, name)

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


def _zero_java_value(java_type: str) -> str:
    if java_type == "long":
        return "0L"
    if java_type == "double":
        return "0.0d"
    if java_type == "boolean":
        return "false"
    return "null"


def _config_default_lit(field: dict, java_type_fn) -> str:
    default = field.get("default")
    if default is not None:
        return _lit(default)
    return _zero_java_value(java_type_fn(field.get("type")))


def _emit_plugin_ctors(cname: str, config_fields: list, java_type_fn) -> list[str]:
    lines: list[str] = []
    if config_fields:
        params = ", ".join(
            f"{java_type_fn(f.get('type'))} {_ident(f.get('name'), 'config field')}"
            for f in config_fields
        )
        lines.append(f"    public {cname}Plugin({params}) {{")
        for f in config_fields:
            fname = _ident(f.get("name"), "config field")
            lines.append(f"        this.{fname} = {fname};")
        lines.append("    }")
    lines.append(f"    public {cname}Plugin() {{")
    for f in config_fields:
        fname = _ident(f.get("name"), "config field")
        lines.append(f"        this.{fname} = {_config_default_lit(f, java_type_fn)};")
    lines.append("    }")
    return lines


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

_JAVA_V3_BIN_OPS = {
    "<": "<", ">": ">", "<=": "<=", ">=": ">=",
    "+": "+", "-": "-", "*": "*", "/": "/", "%": "%",
    "&&": "&&", "||": "||",
}

_V3_ATOMIC_KINDS = {"var", "field", "index", "call", "lit"}
_HOST_ROOTS = {"Pool", "Map", "Job"}


def _split_v3_types(inner: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in inner:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current).strip())
    return parts


def _java_v3_type(name: object, *, boxed: bool = False) -> str:
    """IR v3 surface type -> Java type for emitted v3 classes."""
    if name is None or name == "Unit":
        return "void"
    if not isinstance(name, str) or not name.strip():
        return "Object"
    name = name.strip()
    if name in TYPE_MAP:
        java_type = TYPE_MAP[name]
        if not boxed:
            return java_type
        return {
            "long": "java.lang.Long",
            "double": "java.lang.Double",
            "boolean": "java.lang.Boolean",
        }.get(java_type, java_type)
    if "[" in name:
        base = name[: name.index("[")]
        inner = name[name.index("[") + 1 : name.rindex("]")]
        args = _split_v3_types(inner)
        if base == "List" and args:
            return f"java.util.List<{_java_v3_type(args[0], boxed=True)}>"
        if base == "Opt" and args:
            return f"java.util.Optional<{_java_v3_type(args[0], boxed=True)}>"
        if base == "Map" and len(args) == 2:
            return (
                f"java.util.Map<{_java_v3_type(args[0], boxed=True)}, "
                f"{_java_v3_type(args[1], boxed=True)}>"
            )
        if base == "Result" and len(args) == 2:
            return (
                f"RevlResult<{_java_v3_type(args[0], boxed=True)}, "
                f"{_java_v3_type(args[1], boxed=True)}>"
            )
        return base + "<" + ", ".join("Object" for _ in args) + ">"
    return _ident(name, "type name")


def _metadata_lit(value: object) -> str:
    """Interception metadata -> a Java object literal (v2)."""
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
    if isinstance(value, list):
        return "java.util.List.of(" + ", ".join(_metadata_lit(v) for v in value) + ")"
    if isinstance(value, dict):
        entries = ", ".join(
            f"{_string(k)}, {_metadata_lit(v)}" for k, v in value.items()
        )
        return "java.util.Map.of(" + entries + ")"
    raise EmitError(f"unsupported intercept metadata value: {value!r}")


def _java_test_method_name(name: object, index: int, used: set[str]) -> str:
    raw = name if isinstance(name, str) else str(name)
    base = "test"
    for part in re.split(r"[^A-Za-z0-9_]+", raw):
        if part:
            base += part[:1].upper() + part[1:]
    candidate = base
    while candidate in used:
        candidate = f"{base}_{index}"
        index += 1
    used.add(candidate)
    return candidate


class _V3Ctx:
    """Names and type layouts visible to v3 expression emitters."""

    def __init__(self, types: dict, functions: list, externs: list) -> None:
        self.types = types or {}
        self.function_names = {fn.get("name") for fn in functions or []}
        self.extern_names = {ext.get("name") for ext in externs or []}
        self.case_owners: dict[str, str] = {}
        for tname, spec in self.types.items():
            if spec.get("kind") == "variant":
                for case in spec.get("cases") or []:
                    cname = case.get("name")
                    if cname in self.case_owners:
                        raise EmitError(
                            f"duplicate variant case {cname!r} is not portable to "
                            f"the Java backend without a type annotation"
                        )
                    self.case_owners[cname] = tname
        self._match_counter = 0

    def new_match_ignored(self) -> str:
        self._match_counter += 1
        return f"__revl_ignored_{self._match_counter}"

    def record_type_for_fields(self, field_names: list[str]) -> str:
        wanted = set(field_names)
        matches = [
            name
            for name, spec in self.types.items()
            if spec.get("kind") == "record"
            and set((spec.get("fields") or {}).keys()) == wanted
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise EmitError(
                f"record literal with fields {field_names!r} does not match any "
                f"declared record type"
            )
        raise EmitError(
            f"record literal with fields {field_names!r} is ambiguous between "
            f"{', '.join(matches)}"
        )


def _v3_var(node: dict, ctx: _V3Ctx, rename: dict[str, str] | None = None) -> str:
    name = node.get("name")
    _ident(name, "name")
    if rename and name in rename:
        return rename[name]
    if name in ctx.case_owners:
        return name
    if name in ctx.function_names or name in ctx.extern_names:
        return name
    if name == "None":
        return "java.util.Optional.empty()"
    return name


def _v3_len(target: str) -> str:
    return f"revlLength({target})"


def _v3_builtin(method: object, target: str, args: list[str]) -> str:
    """The stdlib surface (docs/stdlib-2.0.md), dispatched through the
    `revl*` static overloads (emitted once per file) — javac resolves the
    receiver's static type, so every (method, Str|List) pair from the spec
    table compiles. The previous inline lowerings picked one type per method
    (`subList` broke Str.slice, `String.concat` broke List.concat) and the
    `instanceof java.util.List` ternaries did not compile for receivers
    statically typed `String`."""
    if method == "length":
        return f"revlLength({target})"
    if method == "push":
        return f"revlPush({target}, {args[0]})"
    if method == "slice":
        return f"revlSlice({target}, {args[0]}, {args[1]})"
    if method == "charAt":
        return f"String.valueOf(String.valueOf({target}).charAt((int)({args[0]})))"
    if method == "charCodeAt":
        return f"(long) String.valueOf({target}).charAt((int)({args[0]}))"
    if method == "concat":
        return f"revlConcat({target}, {args[0]})"
    if method == "indexOf":
        return f"revlIndexOf({target}, {args[0]})"
    if method == "split":
        return f"revlSplit({target}, {args[0]})"
    if method == "join":
        return f"revlJoin({target}, {args[0]})"
    if method == "repeat":
        return f"revlRepeat({target}, {args[0]})"
    raise EmitError(f"unknown builtin method {method!r}")


def _emit_stdlib_helpers() -> list[str]:
    """Static overloads backing the builtin surface — emitted once per file
    when any builtin/len node is present. Overload resolution replaces
    runtime `instanceof` dispatch; `revlPush`/`revlConcat`/`revlSlice`
    return copies (persistent, docs/stdlib-2.0.md)."""
    return [
        "// revl stdlib surface (docs/stdlib-2.0.md) — typed static overloads.",
        "private static long revlLength(String s) { return s.length(); }",
        "private static long revlLength(java.util.List<?> xs) { return xs.size(); }",
        "private static long revlLength(Object x) {",
        "    return (x instanceof java.util.List<?>) ? ((java.util.List<?>) x).size() : String.valueOf(x).length();",
        "}",
        "// JS slice semantics: out-of-range bounds clamp, never throw.",
        "private static String revlSlice(String s, long a, long b) {",
        "    int len = s.length();",
        "    int a2 = (int) Math.min(Math.max(a, 0L), len);",
        "    int b2 = (int) Math.max(Math.min(Math.max(b, 0L), len), a2);",
        "    return s.substring(a2, b2);",
        "}",
        "private static <T> java.util.List<T> revlSlice(java.util.List<T> xs, long a, long b) {",
        "    int len = xs.size();",
        "    int a2 = (int) Math.min(Math.max(a, 0L), len);",
        "    int b2 = (int) Math.max(Math.min(Math.max(b, 0L), len), a2);",
        "    return java.util.List.copyOf(xs.subList(a2, b2));",
        "}",
        "private static long revlIndexOf(String s, String needle) { return s.indexOf(needle); }",
        "private static <T> long revlIndexOf(java.util.List<T> xs, T v) { return xs.indexOf(v); }",
        "private static String revlConcat(String a, String b) { return a.concat(b); }",
        "private static <T> java.util.List<T> revlConcat(java.util.List<T> a, java.util.List<T> b) {",
        "    return java.util.stream.Stream.concat(a.stream(), b.stream()).toList();",
        "}",
        "private static <T> java.util.List<T> revlPush(java.util.List<T> xs, T v) {",
        "    return java.util.stream.Stream.concat(xs.stream(), java.util.stream.Stream.of(v)).toList();",
        "}",
        "// split is pinned to the JS shape: Pattern.quote (literal sep, not",
        "// regex), limit -1 (trailing empties kept), \"\" -> 1-char strings.",
        "private static java.util.List<String> revlSplit(String s, String sep) {",
        "    if (sep.isEmpty()) {",
        "        return s.chars().mapToObj(c -> String.valueOf((char) c)).toList();",
        "    }",
        "    return java.util.List.of(s.split(java.util.regex.Pattern.quote(sep), -1));",
        "}",
        "private static String revlJoin(java.util.List<String> xs, String sep) {",
        "    return String.join(sep, xs);",
        "}",
        "private static String revlRepeat(String s, long n) {",
        "    return s.repeat((int) Math.max(0L, n));",
        "}",
        "",
    ]


def _uses_stdlib(ir: dict) -> bool:
    """True when any builtin/len node appears anywhere in the document."""
    found = False

    def walk(node) -> None:
        nonlocal found
        if found:
            return
        if isinstance(node, dict):
            if node.get("kind") in ("builtin", "len"):
                found = True
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(ir.get("components"))
    walk(ir.get("functions"))
    walk(ir.get("tests"))
    return found


def _v3_call(
    node: dict,
    ctx: _V3Ctx,
    rename: dict[str, str] | None = None,
    env: _Env | None = None,
) -> str:
    callee = node.get("callee")
    args = [_v3_expr(a, ctx, rename, env) for a in node.get("args") or []]
    if isinstance(callee, dict) and callee.get("kind") == "var":
        name = callee.get("name")
        _ident(name, "callable")
        if name in ctx.case_owners:
            variant = _ident(ctx.case_owners[name], "type name")
            return f"new {variant}.{name}({', '.join(args)})"
        if name == "Some":
            if len(args) != 1:
                raise EmitError("Some constructor expects exactly one argument")
            return f"java.util.Optional.of({args[0]})"
        if name == "None":
            if args:
                raise EmitError("None constructor does not take arguments")
            return "java.util.Optional.empty()"
        if name in ("Ok", "Err"):
            raise EmitError(
                f"builtin Result constructor {name!r} is not portable to the Java backend yet"
            )
        return f"{name}({', '.join(args)})"
    callee_s = _v3_expr(callee, ctx, rename, env)
    return f"{callee_s}.apply({', '.join(args)})"


def _v3_expr(
    node: object,
    ctx: _V3Ctx,
    rename: dict[str, str] | None = None,
    env: _Env | None = None,
) -> str:
    if not isinstance(node, dict) or "kind" not in node:
        raise EmitError(f"malformed v3 expression: {node!r}")
    kind = node["kind"]

    if kind == "lit":
        return _lit(node.get("value"))

    if kind == "var":
        return _v3_var(node, ctx, rename)

    if kind == "adt":
        case = node.get("case")
        _ident(case, "adt case")
        args = ", ".join(_v3_expr(a, ctx, rename, env) for a in node.get("args") or [])
        if case in ctx.case_owners:
            variant = _ident(ctx.case_owners[case], "type name")
            return f"new {variant}.{case}({args})"
        if case in ("Ok", "Err"):
            # built-in Result -> the emitted generic sealed RevlResult
            return f"new RevlResult.{case}<>({args})"
        raise EmitError(f"unknown ADT constructor {case!r}")

    if kind == "name":
        original = node.get("id")
        if rename and original in rename:
            return rename[original]
        return _ident(original, "binding")

    if kind == "config":
        return _ident(node.get("field"), "config field")

    if kind == "req":
        original = node.get("name")
        if rename and original in rename:
            return rename[original]
        return _ident(original, "requirement")

    if kind == "host":
        fn = node.get("fn")
        host, _, method = fn.partition(".")
        args = ", ".join(
            _v3_expr(a, ctx, rename, env) for a in node.get("args") or []
        )
        return f"{host}.{_host_method(method)}({args})"

    if kind == "call":
        if "callee" in node:
            return _v3_call(node, ctx, rename, env)
        target = node.get("target") or {}
        method = _ident(node.get("method"), "method")
        args = ", ".join(
            _v3_expr(a, ctx, rename, env) for a in node.get("args") or []
        )
        recv = _v3_expr(target, ctx, rename, env)
        return f"{recv}.{method}({args})"

    if kind == "format":
        template = node.get("template") or ""
        args = [_v3_expr(a, ctx, rename, env) for a in node.get("args") or []]
        return _format_java(template, args)

    if kind == "fn":
        fn_name = _ident(node.get("name"), "function")
        args = ", ".join(
            _v3_expr(a, ctx, rename, env) for a in node.get("args") or []
        )
        return f"{fn_name}({args})"

    if kind == "bin":
        op = node.get("op")
        left = _v3_expr(node["left"], ctx, rename, env)
        right = _v3_expr(node["right"], ctx, rename, env)
        if op in ("==", "==="):
            return f"java.util.Objects.equals({left}, {right})"
        if op in ("!=", "!=="):
            return f"!java.util.Objects.equals({left}, {right})"
        java_op = _JAVA_V3_BIN_OPS.get(op)
        if java_op is None:
            raise EmitError(f"unsupported binary operator {op!r}")
        return f"({left} {java_op} {right})"

    if kind == "un":
        operand = _v3_expr(node.get("operand"), ctx, rename, env)
        if node.get("op") == "!":
            return f"(!{operand})"
        if node.get("op") == "-":
            return f"(-{operand})"
        raise EmitError(f"unsupported unary operator {node.get('op')!r}")

    if kind == "call":
        return _v3_call(node, ctx, rename)

    if kind == "field":
        target_node = node.get("target")
        target = _v3_expr(target_node, ctx, rename, env)
        if not (isinstance(target_node, dict) and target_node.get("kind") in _V3_ATOMIC_KINDS):
            target = f"({target})"
        return f"{target}.{_ident(node.get('name'), 'field')}"

    if kind == "index":
        target_node = node.get("target")
        target = _v3_expr(target_node, ctx, rename, env)
        if not (isinstance(target_node, dict) and target_node.get("kind") in _V3_ATOMIC_KINDS):
            target = f"({target})"
        return f"{target}.get((int)({_v3_expr(node['index'], ctx, rename, env)}))"

    if kind == "builtin":
        target_node = node.get("target")
        target = _v3_expr(target_node, ctx, rename, env)
        if not (isinstance(target_node, dict) and target_node.get("kind") in _V3_ATOMIC_KINDS):
            target = f"({target})"
        args = [_v3_expr(a, ctx, rename, env) for a in node.get("args") or []]
        return _v3_builtin(node.get("method"), target, args)

    if kind == "len":
        return _v3_len(_v3_expr(node.get("target"), ctx, rename, env))

    if kind == "if":
        return (
            f"({_v3_expr(node['cond'], ctx, rename, env)} ? "
            f"{_v3_expr(node['then'], ctx, rename, env)} : "
            f"{_v3_expr(node['else'], ctx, rename, env)})"
        )

    if kind == "record":
        fields = node.get("fields") or []
        type_name = ctx.record_type_for_fields([k for k, _ in fields])
        spec = ctx.types[type_name]
        by_name = {k: _v3_expr(v, ctx, rename, env) for k, v in fields}
        args = []
        for field_name in spec.get("fields") or {}:
            if field_name not in by_name:
                raise EmitError(f"record literal is missing field {field_name!r}")
            args.append(by_name[field_name])
        return f"new {_ident(type_name, 'type name')}({', '.join(args)})"

    if kind == "list":
        return "java.util.List.of(" + ", ".join(
            _v3_expr(item, ctx, rename, env) for item in node.get("items") or []
        ) + ")"

    if kind == "arrow":
        params = ", ".join(_ident(p, "arrow parameter") for p in node.get("params") or [])
        body = _v3_expr(node["body"], ctx, rename, env)
        return f"({params}) -> ({body})"

    if kind == "match":
        return _v3_match_expr(node, ctx, rename, env)

    if kind == "interp":
        parts = node.get("parts") or []
        segs: list[str] = []
        for part_kind, value in parts:
            if part_kind == "text":
                segs.append(_string(value))
            else:  # ["expr", ir_node] — a full expression, stringified
                segs.append(f"String.valueOf({_v3_expr(value, ctx, rename, env)})")
        if not segs:
            return '""'
        return " + ".join(segs)

    if kind in ("optfield", "optcall"):
        raise EmitError(
            f"optional chaining (`?.`) is not yet lowerable on the Java tier "
            f"({kind!r}); unwrap with `match` or `??` for now"
        )

    raise EmitError(f"unsupported v3 expression kind {kind!r}")


def _v3_match_expr(
    node: dict,
    ctx: _V3Ctx,
    rename: dict[str, str] | None = None,
    env: _Env | None = None,
) -> str:
    scrutinee = _v3_expr(node.get("scrutinee"), ctx, rename, env)
    arms = node.get("arms") or []

    # `Some`/`None`/`Ok`/`Err` are built-in only when NOT shadowed by a user
    # variant (the docs' own `type Outcome = Ok(Row) | …` reuses `Ok`).
    def _builtin(p):
        return p not in ctx.case_owners

    # Opt is java.util.Optional (not a sealed type): built-in Some/None lower
    # to map/orElseGet rather than a `switch`.
    if any(arm.get("pattern") in ("Some", "None") and _builtin(arm.get("pattern")) for arm in arms):
        some_arm = next((a for a in arms if a.get("pattern") == "Some"), None)
        none_arm = next((a for a in arms if a.get("pattern") == "None"), None)
        wild = next((a for a in arms if a.get("pattern") == "_"), None)
        some_bind = _ident(some_arm.get("bind"), "match bind") if some_arm and some_arm.get("bind") else "__revl_v"
        some_body = _v3_expr((some_arm or wild).get("body"), ctx, rename, env)
        none_body = _v3_expr((none_arm or wild).get("body"), ctx, rename, env)
        return (f"({scrutinee}).map({some_bind} -> ({some_body}))"
                f".orElseGet(() -> ({none_body}))")
    if any(arm.get("pattern") in ("Ok", "Err") and _builtin(arm.get("pattern")) for arm in arms):
        # built-in Result -> switch over the sealed RevlResult. Wildcard
        # type pattern + a cast of the payload to the arm's declared type
        # (Result's type args aren't recoverable at the pattern site).
        lines = [f"switch ({scrutinee}) {{"]
        wildcard = None
        for arm in arms:
            pattern = arm.get("pattern")
            body = _v3_expr(arm.get("body"), ctx, rename, env)
            if pattern == "_":
                wildcard = f"            default -> {{ yield ({body}); }}"
                continue
            var = ctx.new_match_ignored()
            lines.append(f"            case RevlResult.{pattern}<?, ?> {var} -> {{")
            bind = arm.get("bind")
            if bind:
                bind = _ident(bind, "match bind")
                ptype = _java_v3_type(arm.get("payload_type"), boxed=True)
                lines.append(f"                final var {bind} = ({ptype}) {var}.value();")
            lines.append(f"                yield ({body});")
            lines.append("            }")
        lines.append(wildcard if wildcard is not None
                     else '            default -> { throw new IllegalArgumentException("non-exhaustive match"); }')
        lines.append("        }")
        return "\n".join(lines)

    lines = [f"switch ({scrutinee}) {{"]
    wildcard = None
    for arm in arms:
        pattern = arm.get("pattern")
        body = _v3_expr(arm.get("body"), ctx, rename, env)
        if pattern == "_":
            wildcard = f"            default -> {{ yield ({body}); }}"
            continue
        case = _ident(pattern, "case name")
        qualified = f"{_ident(ctx.case_owners[case], 'type name')}.{case}" if case in ctx.case_owners else case
        bind = arm.get("bind")
        if bind:
            bind = _ident(bind, "match bind")
            case_var = f"__revl_case_{ctx._match_counter + 1}"
            ctx._match_counter += 1
            lines.append(f"            case {qualified} {case_var} -> {{")
            lines.append(f"                final var {bind} = {case_var}.value;")
            lines.append(f"                yield ({body});")
            lines.append("            }")
        else:
            ignored = ctx.new_match_ignored()
            lines.append(f"            case {qualified} {ignored} -> {{")
            lines.append(f"                yield ({body});")
            lines.append("            }")
    if wildcard is None:
        lines.append("            default -> { throw new IllegalArgumentException(\"non-exhaustive match\"); }")
    else:
        lines.append(wildcard)
    lines.append("        }")
    return "\n".join(lines)


def _let_keyword(node: dict) -> str:
    """Declaration type for a v3 `let`/`var`.

    An empty list literal has no element type, and `var` would freeze it as
    `List<Object>` — later pushes and the declared return type then fail to
    unify. A raw `java.util.List` keeps javac's erasure in play (unchecked
    warning, correct code); non-empty literals infer their element type.
    """
    value = node.get("value")
    if isinstance(value, dict) and value.get("kind") == "list" and not value.get("items"):
        return "java.util.List"
    return "var" if node.get("mutable") else "final var"


def _v3_stmt(node: dict, ctx: _V3Ctx, out: list[str], indent: int, *, test_mode: bool = False) -> None:
    pad = "    " * indent
    step = node.get("step")
    if step in ("let", "assign"):
        name = _ident(node.get("name"), "binding")
        value = _v3_expr(node.get("value"), ctx)
        if step == "let":
            out.append(f"{pad}{_let_keyword(node)} {name} = {value};")
        else:
            out.append(f"{pad}{name} = {value};")
    elif step == "return":
        if node.get("expr") is None:
            out.append(f"{pad}return;")
        else:
            out.append(f"{pad}return {_v3_expr(node['expr'], ctx)};")
    elif step == "if":
        out.append(f"{pad}if ({_v3_expr(node['cond'], ctx)}) {{")
        for child in node.get("then") or []:
            _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        if node.get("else"):
            out.append(f"{pad}}} else {{")
            for child in node["else"]:
                _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        out.append(f"{pad}}}")
    elif step == "while":
        out.append(f"{pad}while ({_v3_expr(node['cond'], ctx)}) {{")
        for child in node.get("body") or []:
            _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        out.append(f"{pad}}}")
    elif step == "for":
        bind = _ident(node.get("bind"), "loop binding")
        out.append(f"{pad}for (var {bind} : {_v3_expr(node['iterable'], ctx)}) {{")
        for child in node.get("body") or []:
            _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        out.append(f"{pad}}}")
    elif step == "let_pattern":
        value = _v3_expr(node.get("value"), ctx)
        tmp = f"__revl_destructure_{id(node)}"
        keyword = "var" if node.get("mutable") else "final var"
        out.append(f"{pad}{keyword} {tmp} = {value};")
        names = [_ident(n, "binding") for n in node.get("names") or []]
        if node.get("pattern") == "record":
            for name in names:
                out.append(f"{pad}{keyword} {name} = {tmp}.{name};")
        else:
            for index, name in enumerate(names):
                out.append(f"{pad}{keyword} {name} = {tmp}.get({index});")
            rest = node.get("rest")
            if rest:
                rest = _ident(rest, "binding")
                out.append(
                    f"{pad}{keyword} {rest} = {tmp}.subList({len(names)}, {tmp}.size());"
                )
    elif step == "expr":
        out.append(f"{pad}{_v3_expr(node['expr'], ctx)};")
    elif step == "assert":
        out.append(
            f"{pad}if (!({_v3_expr(node['expr'], ctx)})) "
            f'throw new AssertionError("assertion failed");'
        )
    else:
        raise EmitError(f"unsupported fn statement step {step!r}")


def _uses_builtin_result(ir: dict) -> bool:
    """True if the IR constructs or matches the built-in Result (Ok/Err) — an
    `adt` node typed Result, or a match arm on Ok/Err. Emitted RevlResult is
    gated on this so non-Result modules and v1 goldens are unaffected."""
    def walk(node) -> bool:
        if isinstance(node, dict):
            if node.get("kind") == "adt" and str(node.get("type", "")).startswith("Result"):
                return True
            if node.get("kind") == "match" and any(
                a.get("pattern") in ("Ok", "Err") for a in node.get("arms") or []
            ):
                return True
            return any(walk(v) for v in node.values())
        if isinstance(node, list):
            return any(walk(v) for v in node)
        return False

    return walk(ir.get("functions")) or walk(ir.get("tests")) or walk(ir.get("components"))


def _emit_result_type() -> list[str]:
    """Built-in Result as a generic sealed interface (Java has no native
    Result). Ok/Err are records; construction is `new RevlResult.Ok<>(x)`
    and match is a sealed switch (verified on JDK 21)."""
    return [
        "public sealed interface RevlResult<T, E> permits RevlResult.Ok, RevlResult.Err {",
        "    record Ok<T, E>(T value) implements RevlResult<T, E> {}",
        "    record Err<T, E>(E value) implements RevlResult<T, E> {}",
        "}",
        "",
    ]


def _emit_v3_types(types: dict) -> list[str]:
    lines: list[str] = []
    for name, spec in types.items():
        name = _ident(name, "type name")
        if spec.get("kind") == "record":
            lines.append(f"public static final class {name} {{")
            fields = spec.get("fields") or {}
            for field, ftype in fields.items():
                field = _ident(field, "record field")
                lines.append(f"    public final {_java_v3_type(ftype)} {field};")
            ctor_params = ", ".join(
                f"{_java_v3_type(fields[f])} {_ident(f, 'record field')}"
                for f in fields
            )
            lines.append(f"    public {name}({ctor_params}) {{")
            for field in fields:
                field = _ident(field, "record field")
                lines.append(f"        this.{field} = {field};")
            lines.append("    }")
            lines.append("}")
        else:
            cases = spec.get("cases") or []
            case_names = [_ident(case.get("name"), "case name") for case in cases]
            permits = ", ".join(f"{name}.{case}" for case in case_names)
            lines.append(f"public sealed interface {name} permits {permits} {{")
            for case in cases:
                cname = _ident(case.get("name"), "case name")
                payload = case.get("payload")
                if payload is None:
                    lines.append(f"    final class {cname} implements {name} {{")
                    lines.append(f"        public {cname}() {{}}")
                    lines.append("    }")
                else:
                    ptype = _java_v3_type(payload)
                    lines.append(f"    final class {cname} implements {name} {{")
                    lines.append(f"        public final {ptype} value;")
                    lines.append(
                        f"        public {cname}({ptype} value) {{ this.value = value; }}"
                    )
                    lines.append("    }")
            lines.append("}")
        lines.append("")
    return lines


def _emit_v3_externs(externs: list) -> list[str]:
    lines: list[str] = []
    for ext in externs:
        name = _ident(ext.get("name"), "extern name")
        params = ", ".join(
            f"{_java_v3_type(p.get('type'))} {_ident(p.get('name'), 'extern parameter name')}"
            for p in ext.get("params") or []
        )
        ret = _java_v3_type(ext.get("returns"))
        bodies = ext.get("bodies") or {}
        if "java" not in bodies:
            raise EmitError(
                f"extern `{name}` has no @java body — not portable to this backend "
                f"(available: {', '.join(sorted(bodies)) or 'none'})"
            )
        lines.append(f"public static {ret} {name}({params}) {{")
        body = bodies["java"].strip()
        if body:
            for line in body.splitlines() or [""]:
                lines.append("    " + line)
        else:
            lines.append("    // (empty @java body)")
        lines.append("}")
        lines.append("")
    return lines


def _emit_v3_functions(functions: list, types: dict, externs: list) -> list[str]:
    ctx = _V3Ctx(types, functions, externs)
    lines: list[str] = []
    for fn in functions:
        name = _ident(fn.get("name"), "function name")
        params = ", ".join(
            f"{_java_v3_type(p.get('type'))} {_ident(p.get('name'), 'parameter name')}"
            for p in fn.get("params") or []
        )
        ret = _java_v3_type(fn.get("returns"))
        lines.append(f"public static {ret} {name}({params}) {{")
        if not fn.get("body"):
            lines.append("    // (empty body)")
        else:
            for stmt in fn["body"]:
                _v3_stmt(stmt, ctx, lines, 1, test_mode=False)
        lines.append("}")
        lines.append("")
    return lines


def _emit_v3_tests(tests: list, types: dict, functions: list, externs: list) -> list[str]:
    ctx = _V3Ctx(types, functions, externs)
    lines: list[str] = []
    used: set[str] = set()
    test_names: list[str] = []
    for index, test in enumerate(tests):
        mname = _java_test_method_name(test.get("name"), index, used)
        test_names.append(mname)
        lines.append(f"public static void {mname}() {{")
        for stmt in test.get("body") or []:
            _v3_stmt(stmt, ctx, lines, 1, test_mode=True)
        lines.append("}")
        lines.append("")
    if test_names:
        runners = ", ".join(f"Components::{name}" for name in test_names)
        lines.append(
            f"public static final java.util.List<Runnable> REVL_TESTS = "
            f"java.util.List.of({runners});"
        )
        lines.append("")
    return lines


class _Env:
    def __init__(self, component: dict, services: dict):
        self.component = component
        self.services = services
        self.name = component["name"]
        self.reqs: dict[str, str] = dict(component.get("requires") or {})
        self.provides: dict[str, str] = dict(component.get("provides") or {})


def _expr(node: dict, env: _Env, rename: dict[str, str] | None = None,
          v3_ctx: _V3Ctx | None = None) -> str:
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
        args = ", ".join(_expr(a, env, rename, v3_ctx) for a in node.get("args") or [])
        return f"{host}.{_host_method(method)}({args})"
    if kind == "req":
        original = node.get("name")
        if rename and original in rename:
            return rename[original]
        return _ident(original, "requirement")
    if kind == "call" and "callee" in node:
        return _v3_expr(node, v3_ctx or _V3Ctx({}, [], []), rename, env)
    if kind == "call":
        target = node.get("target") or {}
        method = _ident(node.get("method"), "method")
        args = ", ".join(_expr(a, env, rename, v3_ctx) for a in node.get("args") or [])
        if target.get("kind") == "req":
            recv = _expr(target, env, rename, v3_ctx)
        else:
            recv = _expr(target, env, rename, v3_ctx)
        return f"{recv}.{method}({args})"
    if kind == "format":
        template = node.get("template") or ""
        args = [_expr(a, env, rename, v3_ctx) for a in node.get("args") or []]
        return _format_java(template, args)
    if kind == "fn":
        fn_name = _ident(node.get("name"), "function")
        args = ", ".join(_expr(a, env, rename, v3_ctx) for a in node.get("args") or [])
        return f"{fn_name}({args})"
    if kind in {
        "var", "bin", "un", "field", "index", "if", "record", "list",
        "arrow", "match", "interp", "len", "builtin",
    }:
        return _v3_expr(node, v3_ctx or _V3Ctx({}, [], []), rename, env)
    raise EmitError(f"unsupported expression node in Java backend: {kind!r}")


def _format_java(template: str, args: list[str]) -> str:
    # `$0`/`$1` placeholders -> `%s`; `$$` -> literal `$` (A4). A literal `%`
    # in the template text must become `%%` or String.format throws
    # UnknownFormatConversionException at runtime (SQL LIKE patterns).
    def literal(text: list[str]) -> str:
        return "".join(text).replace("%", "%%")

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
                pieces.append(literal(buf) + "%s")
                buf = []
                i = j
                continue
        buf.append(ch)
        i += 1
    pieces.append(literal(buf))
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

    walk(ir)
    out: list[str] = []
    for host in sorted(used & _HOST_STUBS.keys()):
        if host == "Map":
            out.extend(_emit_map_runtime())
        elif host == "Pool":
            out.extend(_emit_pool_runtime())
        elif host == "Job":
            out.extend(_emit_job_runtime())
    return out


def _emit_map_runtime() -> list[str]:
    return [
        "// host object runtime (Map) — minimal working Java implementation",
        "public static final class Map {",
        "    private final java.util.HashMap<String, String> values = new java.util.HashMap<>();",
        "    private Map() {}",
        "    // revl `Map.new()` — renamed: `new` is a Java reserved word.",
        "    public static Map create() {",
        "        return new Map();",
        "    }",
        "    public void drop() {",
        "        values.clear();",
        "    }",
        "    public void insert(String key, String value) {",
        "        values.put(key, value);",
        "    }",
        "    public void remove(String key) {",
        "        values.remove(key);",
        "    }",
        "    public java.util.Optional<String> get(String key) {",
        "        return java.util.Optional.ofNullable(values.get(key));",
        "    }",
        "}",
        "",
    ]


def _emit_pool_runtime() -> list[str]:
    return [
        "// host object runtime (Pool) — functional placeholder",
        "public static final class Pool {",
        "    private final String url;",
        "    private final long poolSize;",
        "    private Pool(String url, long poolSize) {",
        "        this.url = url;",
        "        this.poolSize = poolSize;",
        "    }",
        "    public static Pool open(String url, long poolSize) {",
        "        return new Pool(url, poolSize);",
        "    }",
        "    public void close() {",
        "        // Placeholder: a real pool would close its connections here.",
        "    }",
        "    public java.util.List<Object> query(String sql) {",
        "        return java.util.List.of();",
        "    }",
        "    public long execute(String sql) {",
        "        return 0L;",
        "    }",
        "}",
        "",
    ]


def _emit_job_runtime() -> list[str]:
    return [
        "// host object runtime (Job) — no-op placeholder",
        "public static final class Job {",
        "    private Job() {}",
        "    public static void run(String name) {",
        "        // Placeholder: a real job runner would execute `name`.",
        "    }",
        "}",
        "",
    ]


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


def _method_body(env: _Env, key: str, method: dict) -> str:
    steps = method.get("body") or []
    if len(steps) == 1 and steps[0].get("step") == "return":
        rename = {b: f"this.{b}" for b in _binds(env.component)}
        value = _expr(steps[0]["expr"], env, rename)
        # A `void` service operation cannot `return <expr>;` in Java — run
        # the expression for its effect instead.
        if _method_return(env, key, method.get("name")) is None:
            return f"{value};"
        return f"return {value};"
    return 'throw new UnsupportedOperationException("effectful method body");'


_V1_EXPR_KINDS = {"name", "lit", "config", "host", "req", "call", "format"}


def _contains_v3_expr(node: object) -> bool:
    if isinstance(node, dict):
        kind = node.get("kind")
        if kind == "call" and "callee" in node:
            return True
        if kind not in _V1_EXPR_KINDS:
            return True
        return any(_contains_v3_expr(value) for value in node.values())
    if isinstance(node, list):
        return any(_contains_v3_expr(value) for value in node)
    return False


def _component_needs_modern(component: dict) -> bool:
    if component.get("isolate") or component.get("intercept"):
        return True
    for step in component.get("body") or []:
        if step.get("setup"):
            return True
        if step.get("step") in {"if", "fail", "await", "return"}:
            return True
        if step.get("step") == "provide":
            for method in step.get("methods") or []:
                for stmt in method.get("body") or []:
                    if stmt.get("step") != "return":
                        return True
                    if _contains_v3_expr(stmt.get("expr")):
                        return True
        for key in ("acquire", "undo", "expr", "compensate", "message", "cond", "value"):
            if key in step and _contains_v3_expr(step[key]):
                return True
    return False


def _body_contains_step(node: object, target: str) -> bool:
    if isinstance(node, dict):
        if node.get("step") == target:
            return True
        return any(_body_contains_step(value, target) for value in node.values())
    if isinstance(node, list):
        return any(_body_contains_step(value, target) for value in node)
    return False


def _ir_uses_component_step(ir: dict, target: str) -> bool:
    return any(
        _body_contains_step(component.get("body"), target)
        for component in ir.get("components") or []
    )


def _core_imports(ir: dict) -> list[str]:
    names = {"Context", "Disposable", "Disposables", "Plugin", "ServiceKey"}
    if _ir_uses_component_step(ir, "await"):
        names.add("AsyncPlugin")
    if _ir_uses_component_step(ir, "fail"):
        names.add("CordisException")
    return [f"import io.cordis4j.core.{name};" for name in sorted(names)]


def _bind_type(component: dict, bind: str, v3_ctx: _V3Ctx) -> str:
    host = _host_of(component, bind)
    if host != "Object":
        return host
    for step in component.get("body") or []:
        if step.get("step") != "let-effect" or step.get("bind") != bind:
            continue
        expr = step.get("acquire") or {}
        kind = expr.get("kind")
        if kind == "record":
            fields = [name for name, _ in expr.get("fields") or []]
            try:
                return v3_ctx.record_type_for_fields(fields)
            except EmitError:
                return "Object"
        if kind == "list":
            return "java.util.List<Object>"
        if kind == "lit":
            value = expr.get("value")
            if isinstance(value, bool):
                return "boolean"
            if isinstance(value, int):
                return "long"
            if isinstance(value, float):
                return "double"
            if isinstance(value, str):
                return "String"
    return "Object"


def _method_body_lines(
    env: _Env, method: dict, v3_ctx: _V3Ctx, *, returns_void: bool = False
) -> list[str]:
    lines: list[str] = []
    rename = {b: f"this.{b}" for b in _binds(env.component)}
    rename.update({local: f"this.{local}" for local in env.reqs})
    for stmt in method.get("body") or []:
        step = stmt.get("step")
        if step == "return":
            if stmt.get("expr") is None:
                lines.append("return;")
            elif returns_void:
                # `void` methods run the expression for its effect.
                lines.append(f"{_expr(stmt['expr'], env, rename, v3_ctx)};")
                lines.append("return;")
            else:
                lines.append(f"return {_expr(stmt['expr'], env, rename, v3_ctx)};")
        elif step == "effect":
            lines.append(f"{_expr(stmt['acquire'], env, rename, v3_ctx)};")
            lines.append(
                f"fx.track(Disposables.of(() -> {_expr(stmt['undo'], env, rename, v3_ctx)}));"
            )
        elif step == "emit":
            lines.append(f"{_expr(stmt['expr'], env, rename, v3_ctx)};")
            if stmt.get("compensate") is not None:
                lines.append(
                    f"fx.track(Disposables.of(() -> "
                    f"{_expr(stmt['compensate'], env, rename, v3_ctx)}));"
                )
        elif step == "await":
            raise EmitError("await steps are not allowed inside method bodies (A1)")
        elif step == "provide":
            raise EmitError("provide steps are not allowed inside method bodies")
        else:
            raise EmitError(f"unknown step in method body: {step!r}")
    return lines

def _emit_setup_stmt(env: _Env, v3_ctx: _V3Ctx, step: dict, out: list[str], pad: str) -> None:
    kind = step.get("step")
    if kind in ("let", "assign"):
        name = _ident(step.get("name"), "binding")
        value = _expr(step.get("value"), env, None, v3_ctx)
        if kind == "let":
            out.append(f"{pad}{_let_keyword(step)} {name} = {value};")
        else:
            out.append(f"{pad}{name} = {value};")
    elif kind == "expr":
        out.append(f"{pad}{_expr(step['expr'], env, None, v3_ctx)};")
    elif kind == "if":
        out.append(f"{pad}if ({_expr(step['cond'], env, None, v3_ctx)}) {{")
        for child in step.get("then") or []:
            _emit_setup_stmt(env, v3_ctx, child, out, pad + "    ")
        if step.get("else"):
            out.append(f"{pad}}} else {{")
            for child in step["else"]:
                _emit_setup_stmt(env, v3_ctx, child, out, pad + "    ")
        out.append(f"{pad}}}")
    elif kind == "while":
        out.append(f"{pad}while ({_expr(step['cond'], env, None, v3_ctx)}) {{")
        for child in step.get("body") or []:
            _emit_setup_stmt(env, v3_ctx, child, out, pad + "    ")
        out.append(f"{pad}}}")
    elif kind == "for":
        bind = _ident(step.get("bind"), "loop binding")
        out.append(f"{pad}for (var {bind} : {_expr(step['iterable'], env, None, v3_ctx)}) {{")
        for child in step.get("body") or []:
            _emit_setup_stmt(env, v3_ctx, child, out, pad + "    ")
        out.append(f"{pad}}}")
    elif kind == "let_pattern":
        value = _expr(step.get("value"), env, None, v3_ctx)
        tmp = f"__revl_destructure_{id(step)}"
        keyword = "var" if step.get("mutable") else "final var"
        out.append(f"{pad}{keyword} {tmp} = {value};")
        names = [_ident(n, "binding") for n in step.get("names") or []]
        if step.get("pattern") == "record":
            for name in names:
                out.append(f"{pad}{keyword} {name} = {tmp}.{name};")
        else:
            for index, name in enumerate(names):
                out.append(f"{pad}{keyword} {name} = {tmp}.get({index});")
            rest = step.get("rest")
            if rest:
                rest = _ident(rest, "binding")
                out.append(
                    f"{pad}{keyword} {rest} = {tmp}.subList({len(names)}, {tmp}.size());"
                )
    elif kind == "assert":
        out.append(
            f"{pad}if (!({_expr(step['expr'], env, None, v3_ctx)})) "
            f'throw new AssertionError("assertion failed");'
        )
    else:
        raise EmitError(f"unsupported component setup step {kind!r}")


def _emit_component_stmts(
    component: dict,
    env: _Env,
    v3_ctx: _V3Ctx,
    cname: str,
    out: list[str],
    steps: list[dict],
    pad: str,
) -> None:
    for step in steps:
        kind = step.get("step")
        if kind in ("let-effect", "effect"):
            for setup in step.get("setup") or []:
                _emit_setup_stmt(env, v3_ctx, setup, out, pad)
            if kind == "let-effect":
                bind = _ident(step["bind"], "binding")
                out.append(f"{pad}var {bind} = {_expr(step['acquire'], env, None, v3_ctx)};")
            else:
                out.append(f"{pad}{_expr(step['acquire'], env, None, v3_ctx)};")
            out.append(
                f"{pad}fx.track(Disposables.of(() -> "
                f"{_expr(step['undo'], env, None, v3_ctx)}));"
            )
        elif kind == "emit":
            out.append(f"{pad}{_expr(step['expr'], env, None, v3_ctx)};")
            if step.get("compensate") is not None:
                out.append(
                    f"{pad}fx.track(Disposables.of(() -> "
                    f"{_expr(step['compensate'], env, None, v3_ctx)}));"
                )
        elif kind == "provide":
            key = step.get("name")
            service = step.get("service")
            struct = f"{cname}{_camel(key)}"
            ctor_args = ", ".join(
                ["ctx", "fx"] + list(env.reqs) + list(_binds(component))
            )
            out.append(
                f"{pad}fx.track(ctx.provide(ServiceKey.of({service}.class), "
                f"new {struct}({ctor_args})));"
            )
        elif kind == "if":
            out.append(f"{pad}if ({_expr(step['cond'], env, None, v3_ctx)}) {{")
            _emit_component_stmts(
                component, env, v3_ctx, cname, out, step.get("then") or [], pad + "    "
            )
            if step.get("else"):
                out.append(f"{pad}}} else {{")
                _emit_component_stmts(
                    component, env, v3_ctx, cname, out, step["else"], pad + "    "
                )
            out.append(f"{pad}}}")
        elif kind == "fail":
            message = _expr(step["message"], env, None, v3_ctx)
            out.append(
                f"{pad}throw new CordisException(String.valueOf({message}));"
            )
        elif kind == "await":
            out.append(f"{pad}{_expr(step['expr'], env, None, v3_ctx)};")
        elif kind == "return":
            raise EmitError("return steps are only allowed inside method bodies")
        else:
            _emit_setup_stmt(env, v3_ctx, step, out, pad)




def _emit_component_modern(
    component: dict,
    services: dict,
    types: dict | None = None,
    functions: list | None = None,
    externs: list | None = None,
) -> list[str]:
    env = _Env(component, services)
    v3_ctx = _V3Ctx(types or {}, functions or [], externs or [])
    name = component["name"]
    cname = _ident(name, "component")
    isolate = component.get("isolate") or {}
    intercept = component.get("intercept") or {}
    has_await = any(
        step.get("step") == "await" for step in component.get("body") or []
    )
    plugin_interface = "AsyncPlugin" if has_await else "Plugin"

    for key in isolate:
        if key not in env.reqs and key not in env.provides:
            raise EmitError(f"{name}: isolate key {key!r} is not declared")
    for key in intercept:
        if key not in env.reqs:
            raise EmitError(f"{name}: intercept key {key!r} is not a requirement")

    out: list[str] = []

    for key, service in env.provides.items():
        _ident(key, "provision")
        struct = f"{cname}{_camel(key)}"
        out.append(f"public static final class {struct} implements {service} {{")
        out.append("    private final Context ctx;")
        out.append("    private final Context.EffectScope fx;")
        for local, service in env.reqs.items():
            out.append(f"    private final {service} {local};")
        for b in _binds(component):
            btype = _bind_type(component, b, v3_ctx)
            out.append(f"    private final {btype} {b};")
        ctor_params = ", ".join(
            ["Context ctx", "Context.EffectScope fx"]
            + [f"{service} {local}" for local, service in env.reqs.items()]
            + [f"{_bind_type(component, b, v3_ctx)} {b}" for b in _binds(component)]
        )
        out.append(f"    {struct}({ctor_params}) {{")
        out.append("        this.ctx = ctx;")
        out.append("        this.fx = fx;")
        for local in env.reqs:
            out.append(f"        this.{local} = {local};")
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
                f"{_java_v3_type(_param_type(env, key, mname, p))} {p}"
                for p in method.get("params") or []
            )
            ret = _java_v3_type(_method_return(env, key, mname)) if _method_return(env, key, mname) else "void"
            out.append(f"    public {ret} {mname}({params}) {{")
            for line in _method_body_lines(env, method, v3_ctx, returns_void=ret == "void"):
                out.append(("        " + line) if line else line)
            out.append("    }")
        out.append("}")
        out.append("")

    config_fields = component.get("config") or []
    out.append(
        f"public static final class {cname}Plugin implements {plugin_interface} {{"
    )
    for f in config_fields:
        fname = _ident(f.get("name"), "config field")
        out.append(f"    private final {_java_v3_type(f.get('type'))} {fname};")
    out.extend(_emit_plugin_ctors(cname, config_fields, _java_v3_type))
    out.append("    @Override")
    if has_await:
        out.append("    public Disposable apply(Context ctx) throws Exception {")
    else:
        out.append("    public Disposable apply(Context ctx) {")
    for key, realm in isolate.items():
        service = env.provides.get(key) or env.reqs[key]
        out.append(f"        ctx = ctx.isolate({service}.class, {_string(realm)});")
    for key, metadata in intercept.items():
        service = env.reqs[key]
        out.append(
            f"        ctx.intercept(ServiceKey.of({service}.class), {_metadata_lit(metadata)});"
        )
    out.append("        Context.EffectScope fx = ctx.effect();")
    for local, service in env.reqs.items():
        out.append(f"        {service} {local} = ctx.get({service}.class);")
    # A8 self-revert: cordis4j's ctx.effect() scope is NOT owned by the
    # fiber until apply returns it, so a failing activation must dispose
    # the accumulated effects itself before the failure routes to the
    # runtime (verified against the real runtime in RunRealScenarios).
    out.append("        try {")
    body_lines: list[str] = []
    _emit_component_stmts(
        component, env, v3_ctx, cname, body_lines, component.get("body") or [], "            "
    )
    out.extend(body_lines)
    body_steps = component.get("body") or []
    if not (body_steps and body_steps[-1].get("step") == "fail"):
        # An unconditional trailing `fail` lowers to a throw; Java treats a
        # statement after it as a hard "unreachable statement" error.
        out.append("            return fx;")
    out.append("        } catch (RuntimeException | Error failure) {")
    out.append("            fx.dispose();")
    out.append("            throw failure;")
    out.append("        }")
    out.append("    }")
    out.append("}")
    out.append("")
    return out



def _emit_component(
    component: dict,
    services: dict,
    types: dict | None = None,
    functions: list | None = None,
    externs: list | None = None,
) -> list[str]:
    if _component_needs_modern(component):
        return _emit_component_modern(component, services, types, functions, externs)
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
            out.append(f"    public {ret} {mname}({params}) {{ {_method_body(env, key, method)} }}")
        out.append("}")
        out.append("")

    config_fields = component.get("config") or []
    out.append(f"public static final class {cname}Plugin implements Plugin {{")
    for f in config_fields:
        fname = _ident(f.get("name"), "config field")
        out.append(f"    private final {_java_type(f.get('type'))} {fname};")
    out.extend(_emit_plugin_ctors(cname, config_fields, _java_type))
    out.append("    @Override")
    out.append("    public Disposable apply(Context ctx) {")
    for local, service in env.reqs.items():
        out.append(f"        {service} {local} = ctx.get({service}.class);")
    # A8 self-revert: undos accumulate as the steps land; if a later step
    # throws mid-activation, the accumulated inverses run (reverse order)
    # before the failure routes to the runtime — cordis4j only owns what
    # `apply` returns (verified in scenarios/RunRealScenarios.java).
    out.append("        java.util.ArrayList<Disposable> undos = new java.util.ArrayList<>();")
    out.append("        try {")
    disposers: list[str] = []
    for step in component.get("body") or []:
        kind = step.get("step")
        if kind == "let-effect":
            bind = _ident(step["bind"], "binding")
            out.append(f"            {_host_of(component, step['bind'])} {bind} = {_expr(step['acquire'], env)};")
            undo = _expr(step["undo"], env)
            out.append(f"            undos.add(Disposables.of(() -> {undo}));")
            disposers.append(bind)
        elif kind == "effect":
            out.append(f"            {_expr(step['acquire'], env)};")
            out.append(f"            undos.add(Disposables.of(() -> {_expr(step['undo'], env)}));")
            disposers.append("effect")
        elif kind == "emit":
            out.append(f"            {_expr(step['expr'], env)};")
        elif kind == "provide":
            key = step.get("name")
            service = step.get("service")
            struct = f"{cname}{_camel(key)}"
            ctor_args = ", ".join(b for b in _binds(component))
            # The provision's disposable joins the teardown list — the
            # modern path tracks it via fx.track(ctx.provide(...)); dropping
            # it would leave the provision registered after unload.
            out.append(
                f"            undos.add(ctx.provide(ServiceKey.of({service}.class), "
                f"new {struct}({ctor_args})));"
            )
            disposers.append(key)
        else:
            raise EmitError(f"unsupported component step in Java backend: {kind!r}")
    if disposers:
        # Real cordis4j Disposables.composite disposes its parts in the
        # given order, so LIFO teardown (G7) means reversing the
        # acquisition-ordered list before handing it over.
        out.append("            java.util.Collections.reverse(undos);")
        out.append("            return Disposables.composite(undos.toArray(new Disposable[0]));")
    else:
        out.append("            return Disposables.none();")
    out.append("        } catch (RuntimeException | Error failure) {")
    out.append("            for (int i = undos.size() - 1; i >= 0; i--) {")
    out.append("                undos.get(i).dispose();")
    out.append("            }")
    out.append("            throw failure;")
    out.append("        }")
    out.append("    }")
    out.append("}")
    out.append("")
    return out


def _emit_service_interfaces_v3(services: dict) -> list[str]:
    out: list[str] = []
    for sname, service in services.items():
        _ident(sname, "service")
        out.append(f"public interface {sname} {{")
        for mname, method in (service.get("methods") or {}).items():
            _ident(mname, "method")
            params = ", ".join(
                f"{_java_v3_type(p.get('type'))} {_ident(p.get('name'), 'parameter')}"
                for p in method.get("params") or []
            )
            ret = _java_v3_type(method.get("returns")) if method.get("returns") else "void"
            out.append(f"    {ret} {mname}({params});")
        out.append("}")
        out.append("")
    return out


def _emit_v1(ir: dict, package_name: str) -> str:
    components = ir.get("components") or []
    if not components:
        raise EmitError("IR document has no components")

    out: list[str] = []
    out.append("// Generated by the revl cordis4j backend (ir_version 1) — do not edit.")
    out.append(f"// Target: {CRATE} (github.com/1na-ko/cordis4j).")
    out.append(f"package {_ident(package_name, 'package')};")
    out.append("")
    out.extend(_core_imports(ir))
    out.append("")
    out.append("public final class Components {")
    out.append("    private Components() {}")
    out.append("")
    out.extend(["    " + line if line else line for line in _emit_service_interfaces(ir.get("services") or {})])
    out.extend(["    " + line if line else line for line in _emit_host_stubs(ir)])
    if _uses_stdlib(ir):
        out.extend(["    " + line if line else line for line in _emit_stdlib_helpers()])
    for component in components:
        out.extend(["    " + line if line else line for line in _emit_component(component, ir.get("services") or {})])
    out.append("}")
    return "\n".join(out).rstrip() + "\n"


def _emit_v2(ir: dict, package_name: str) -> str:
    components = ir.get("components") or []
    if not components:
        raise EmitError("IR document has no components")

    out: list[str] = []
    out.append("// Generated by the revl cordis4j backend (ir_version 2) — do not edit.")
    out.append(f"// Target: {CRATE} (github.com/1na-ko/cordis4j).")
    out.append(f"package {_ident(package_name, 'package')};")
    out.append("")
    out.extend(_core_imports(ir))
    out.append("")
    out.append("public final class Components {")
    out.append("    private Components() {}")
    out.append("")
    out.extend(["    " + line if line else line for line in _emit_service_interfaces(ir.get("services") or {})])
    out.extend(["    " + line if line else line for line in _emit_host_stubs(ir)])
    if _uses_stdlib(ir):
        out.extend(["    " + line if line else line for line in _emit_stdlib_helpers()])
    for component in components:
        out.extend(["    " + line if line else line for line in _emit_component(component, ir.get("services") or {})])
    out.append("}")
    return "\n".join(out).rstrip() + "\n"


def _emit_v3(ir: dict, package_name: str) -> str:
    services = ir.get("services") or {}
    components = ir.get("components") or []
    types = ir.get("types") or {}
    functions = ir.get("functions") or []
    externs = ir.get("externs") or []
    tests = ir.get("tests") or []
    if not components and not types and not functions and not externs and not tests:
        raise EmitError("IR document has no components, types, functions, externs, or tests")

    out: list[str] = []
    out.append("// Generated by the revl cordis4j backend (ir_version 3) — do not edit.")
    out.append(f"// Target: {CRATE} (github.com/1na-ko/cordis4j).")
    out.append(f"package {_ident(package_name, 'package')};")
    out.append("")
    out.extend(_core_imports(ir))
    out.append("")
    out.append("public final class Components {")
    out.append("    private Components() {}")
    out.append("")
    if _uses_builtin_result(ir):
        out.extend(["    " + line if line else line for line in _emit_result_type()])
    if types:
        out.extend(["    " + line if line else line for line in _emit_v3_types(types)])
    if services:
        out.extend(["    " + line if line else line for line in _emit_service_interfaces_v3(services)])
    out.extend(["    " + line if line else line for line in _emit_host_stubs(ir)])
    if _uses_stdlib(ir):
        out.extend(["    " + line if line else line for line in _emit_stdlib_helpers()])
    if externs:
        out.extend(["    " + line if line else line for line in _emit_v3_externs(externs)])
    if functions:
        out.extend(["    " + line if line else line for line in _emit_v3_functions(functions, types, externs)])
    if tests:
        out.extend(["    " + line if line else line for line in _emit_v3_tests(tests, types, functions, externs)])
    for component in components:
        out.extend(["    " + line if line else line for line in _emit_component(component, services, types, functions, externs)])
    out.append("}")
    return "\n".join(out).rstrip() + "\n"


def emit(ir: dict, package_name: str = "revl") -> str:
    """Emit one Java source file for an IR document (ir_version 1, 2, or 3)."""
    if not isinstance(ir, dict):
        raise EmitError("IR document must be a dict")
    version = ir.get("ir_version")
    if version == 1:
        return _emit_v1(ir, package_name)
    if version == 2:
        return _emit_v2(ir, package_name)
    if version == 3:
        return _emit_v3(ir, package_name)
    raise EmitError(
        f"unsupported ir_version: {version!r} — the Java backend targets "
        f"ir_version 1, 2, and 3"
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





