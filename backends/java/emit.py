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
- match       -> Java 21 pattern `switch` expressions (no `default` when the
                 arms already cover the sealed ADT — javac rejects the pair)
- arrows      -> beta-reduced at the call site; there is no functional
                 interface to target for an untyped parameter (see "arrows")
- externs     -> verbatim `@java` bodies
- tests       -> static void methods collected in `REVL_TESTS`
- config      -> plugin constructor parameters (`new XPlugin(url, pool_size)`)
- emit        -> a plain call (the emission marker is a revl-checker concern)
- format      -> `String.format(...)`

The emitted file also contains the host-object runtime (`Pool`/`Map`/`Job`) —
`Pool` is a real bounded connection pool and `Job` a real cancellable
asynchronous unit of work, both defined once for every tier in
backends/python/runtime.py under ".. _pool-job-semantics:" — and `Map` is a
`HashMap<String, V>` with `V` inferred per site from the IR (FR-4 — the
session ledger `Map[Str, List[Msg]]` compiles) —
and applies IR config `default` values through a no-arg plugin constructor.

CLI: `python3 emit.py <ir.json> [> out.java]`.
"""

from __future__ import annotations

import json
import re
import sys

__all__ = ["emit", "EmitError"]

CRATE = "cordis4j"

# item 322 Slice 2: record mode. When True, a witnessed transactional step also
# writes a durable discharge-descriptor to the java WAL sink
# (`revlRecordTransactional`) and the recording preamble (the FileChannel.force
# fsync sink) is emitted. Default False -> byte-identical output (the java
# golden oracle + the selfhost mirror both run with record off, so neither
# shifts). Mirrors backends/go/emit.py's `_RECORD_MODE`.
_RECORD_MODE = False

# item 178(b): lifecycle mode. Set by `emit()` for an ir_version 3 document that
# carries `lifecycle test` blocks (docs/syntax-2.0.md §7.1). Those tests prove
# R1 (every acquired host resource was released) as well as R4 (no provision
# still resolves), so the host runtimes carry a live-resource counter while it
# is on. Default False -> the host runtimes are byte-identical to what they were
# before the driver existed, which is what keeps every java golden (none of
# which carries a lifecycle test) unchanged.
_LIFECYCLE_MODE = False

# The document's declared type names (ir_version 3), so `_java_v3_type` can tell
# a user's `type Row = { .. }` from the host Pool's undeclared query-result row.
# Set by `emit()`; empty for the v1/v2 dialects, which do not use that renderer.
_V3_DECLARED_TYPES: frozenset[str] = frozenset()

# The placeholder a confidential witness is written as in the durable WAL. Must
# equal `revl.taint.REDACTED_SECRET` / `confidential.REDACTED`: it is part of the
# log's on-disk contract, and `revl recover` reads it back to refuse a replay it
# cannot honestly perform (src/revl/recovery.py `_has_redacted_arg`) rather than
# addressing the wrong referent and reporting the miss as a clean rollback.
_REDACTED_SECRET = "<redacted:secret>"

TYPE_MAP = {
    "Str": "String",
    "Int": "long",
    "Int32": "int",
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
        # FR-4: the value parameter is generic — the host Map is
        # `HashMap<String, V>` with V learned per site from the IR.
        "insert": ("void", ["String", "V"]),
        "remove": ("void", ["String"]),
        "get": ("java.util.Optional<V>", ["String"]),
        # Iteration surface (docs/stdlib-2.0.md §Map) — read-only queries.
        "size": ("long", []),
        "keys": ("java.util.List<String>", []),
    },
    "Job": {
        "run": ("Job", ["String"]),
    },
}

# Host builtins whose result is an asynchronous handle. An `await` step over
# one must *join* it (A1: "evaluate expr, await its result, discard the
# value") rather than merely evaluate the call — abandoning the handle leaves
# the job in flight past activation. Rust gets this for free, because its
# `Job::run` is an `async fn` and the step lowers to `.await`; Java has no
# await operator, so the join is explicit.
_HOST_AWAITABLE = {"Job.run"}

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


def _refuse_stream_host(fn: str) -> None:
    """Refuse a `Stream.*` host builtin on this tier (roadmap item 419e).

    `subscribe` already refuses honestly, but the ACQUISITION that opens the
    stream (`let src = effect Stream.source() undo src.close()`) did not:
    `Stream` is not in `_HOST_ROOTS` and has no entry in `_HOST_STUBS`, so the
    emitter rendered `Stream src = Stream.source();` against a class it never
    emits and the file did not compile. Refuse at emit time with a message that
    names the tiers which do lower streams, the way wasm already does."""
    if fn.split(".", 1)[0] == "Stream":
        raise EmitError(
            f"`{fn}` opens a stream; a stream subscription suspends a fiber "
            "and the java blocking-tier lowering (a `BlockingQueue.poll` "
            "interruptible by the cancel signal) is not implemented, so this "
            "tier has no `Stream` runtime class; streams run on py, go and "
            "rust (item 130 §4.6); try `--backend py`"
        )


# Dispatcher conformance (roadmap item 76a). This tier converged to ONE
# expression renderer (`_expr`) covering both IR dialects, so the table below
# has a single entry: every kind the frontend can produce in either position
# must render through it, or be deliberately refused with a named
# tier-limit EmitError — never the "unsupported v3 expression kind"
# fall-through. `arrow` in VALUE position is refused (`_ARROW_VALUE_REFUSAL`):
# a called arrow is beta-reduced by `_v3_call`, so the refusal only ever fires
# where the value has no lowerable home. tests/test_expr_dispatcher_
# conformance.py checks this table against src/revl/lower.py's EXPR_KINDS and
# against the renderer's source. `hole` is refused at the document level by
# the pre-emit walk.
EXPR_DISPATCHERS: dict[str, frozenset[str]] = {
    "renderer": frozenset({
        "adt", "bin", "builtin", "call", "config", "field", "fn", "format",
        "host", "if", "index", "instance-get", "interp", "len", "list", "lit",
        "maplit", "match", "name", "record", "req", "spawn", "un", "var",
    }),
}
EXPR_REFUSED: frozenset[str] = frozenset({
    # functional record update (docs/records.md §6): refused with a named
    # error — "lift it into a helper fn instead"
    "record_update",
    # optional chaining (docs/syntax-2.0.md §3.2): refused with a named
    # error — "unwrap with `match` or `??` for now"
    "optfield", "optcall",
    # an arrow VALUE (a called arrow beta-reduces via `_v3_call`); refused
    # with `_ARROW_VALUE_REFUSAL` — the value has no functional interface to
    # target on this tier
    "arrow",
    "hole",
})


def _is_fn_type(name: object) -> bool:
    """Is this surface type a function type, `(P, ...) -> R`?

    A function type is the one surface spelling that is not `Head[Args]`
    (docs/function-types.md), so it can be recognised without a full parse:
    a leading parenthesised group whose closing paren is followed by `->`.
    """
    if not isinstance(name, str) or not name.strip().startswith("("):
        return False
    name = name.strip()
    depth = 0
    for i, ch in enumerate(name):
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
            if depth == 0:
                return name[i + 1:].lstrip().startswith("->")
    return False


_FN_TYPE_REFUSAL = (
    "a declared function type ({name}) is not lowerable on the Java tier. A "
    "Java lambda needs a *nominal* target type, and the JDK's functional "
    "interfaces are neither generic over arity nor usable with primitives "
    "without boxing: `(Int) -> Int` is `IntUnaryOperator`, `(Int, Str) -> "
    "Bool` has no JDK interface at all, and arity 3+ needs a generated one per "
    "shape. Generating those interfaces is a coherent design; guessing one is "
    "not. Arrows bound to a local `let` and called in the same body still "
    "lower — they are beta-reduced at the call site (see \"arrows\" below) — "
    "so this refusal is only for a function type *written in a declaration*. "
    "See docs/function-types.md."
)


def _reject_fn_type(name: object) -> None:
    if _is_fn_type(name):
        raise EmitError(_FN_TYPE_REFUSAL.format(name=name))


# Java generics cannot hold primitives: `Optional<long>` is not a type. Every
# type argument must be the boxed form, while the same revl type in a plain
# parameter or return position stays primitive.
_BOXED = {
    "long": "Long",
    "int": "Integer",
    "double": "Double",
    "boolean": "Boolean",
    "void": "Void",
}


def _java_type_arg(name: object) -> str:
    """A revl type as a Java *type argument* (boxed where primitive)."""
    rendered = _java_type(name)
    return _BOXED.get(rendered, rendered)


def _java_type(name: object) -> str:
    if not isinstance(name, str) or not name:
        return "Object"
    _reject_fn_type(name)
    if name in TYPE_MAP:
        return TYPE_MAP[name]
    generic = re.match(r"^(\w+)\[(.+)\]$", name)
    if generic:
        head, inner = generic.group(1), generic.group(2)
        if head == "List":
            return f"java.util.List<{_java_type_arg(inner)}>"
        if head == "Opt":
            return f"java.util.Optional<{_java_type_arg(inner)}>"
        if head == "Map":
            k, v = _split_generic(inner)
            return f"java.util.Map<{_java_type_arg(k)}, {_java_type_arg(v)}>"
    # An unrecognised name is opaque in IR v1/v2 (there are no `type`
    # declarations to resolve it against — `examples/user_cache.rvl` names a
    # `Row` that is never declared), so it erases to `Object`. IR v3 declares
    # its types and renders them by name; that is `_java_v3_type`'s job, and
    # the two must never be mixed inside one signature.
    return "Object"


def _split_generic(inner: str) -> list[str]:
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


def _mangle(name: str) -> str:
    """Rename a syntactically-valid identifier that collides with a *Java*
    reserved word (`class`, `new`, `int`, `default`, …) so a valid revl
    identifier that happens to be a Java keyword emits and RUNS instead of
    crashing at emit (roadmap item 165).

    This is the same A3 scheme `_fn_name` applies to top-level callables (and
    `src/revl/lower.py::_safe_name` to revl keywords). It must be a pure
    function of the name — declaration site and use sites agree without a table
    — and ALSO INJECTIVE: two distinct revl identifiers may never land on one
    Java identifier.

    The naive "append `_` while the name is reserved" loop is pure but not
    injective: it sends `double` to `double_` and leaves the equally legal revl
    identifier `double_` alone, so both reach `double_`. Here that breaks
    loudly (javac: "variable double_ is already defined"), but the python tier
    silently CAPTURES on the same shape, so the rule is fixed identically on
    every tier rather than left to a downstream compiler CI does not run.

    The injective rule: escape a name iff the name OR any name reachable from
    it by dropping trailing `_` is reserved, and escape it by exactly ONE `_`.
    Names whose underscore-stripped root is reserved shift up one rung of the
    `kw`/`kw_`/`kw__` ladder (`double` -> `double_`, `double_` -> `double__`),
    which is injective; every other name is returned unchanged and can never
    equal a shifted name, because a shifted name's root is reserved and an
    unchanged name's root is not. The output is never itself reserved: no
    member of `_JAVA_RESERVED` ends in `_`.

    Only a name whose root is reserved can change, so no existing program that
    does not name a Java keyword changes its emitted output. Target keywords
    only; the emitter scaffolding stays rejected in `_ident`."""
    root = name
    while root:
        if root in _JAVA_RESERVED:
            return name + "_"
        if not root.endswith("_"):
            break
        root = root[:-1]
    return name


def _ident(name: object, role: str) -> str:
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise EmitError(f"invalid {role} identifier: {name!r}")
    if name in _EMITTER_RESERVED:
        raise EmitError(f"{role} identifier collides with Java/reserved name: {name!r}")
    return _mangle(name)


def _fn_name(name: object) -> str:
    """The Java identifier for a revl top-level `fn` or extern.

    revl's name space is larger than Java's: `fn double(..)` is a perfectly
    legal revl program, but `double` is a Java keyword. Rejecting it would make
    a portable program unportable for a spelling reason, so rename it with the
    same A3 scheme `src/revl/lower.py::_safe_name` uses for bindings. The
    mapping is a pure function of the name, so declaration sites and call sites
    agree without threading a table around, and it is INJECTIVE by the same
    ladder-shift rule `_mangle` documents above: escape iff the name or any
    name reachable from it by dropping trailing `_` is reserved, by exactly one
    `_`, so `double` -> `double_` and `double_` -> `double__` stay distinct.
    `_check_fn_name_collisions` re-checks the property on the whole callable
    set as a belt-and-braces guard.
    """
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise EmitError(f"invalid function identifier: {name!r}")
    root = name
    while root:
        if root in _JAVA_RESERVED or root in _EMITTER_RESERVED:
            return name + "_"
        if not root.endswith("_"):
            break
        root = root[:-1]
    return name


def _check_fn_name_collisions(functions: list, externs: list) -> None:
    """Reject programs where A3 renaming would merge two distinct callables.

    `_fn_name` is table-free. It used to map both `double` and `double_` onto
    `double_`, and this check was the only thing standing between that and
    wrong code. `_fn_name` is now injective in its own right, so this can no
    longer fire on a keyword rename; it stays as a belt-and-braces assertion
    that the property actually holds over a whole program's callable set, and
    would still catch any future non-injective renaming added on this path.
    """
    seen: dict[str, str] = {}
    for decl in list(functions or []) + list(externs or []):
        original = decl.get("name")
        if not isinstance(original, str) or not _IDENT_RE.match(original):
            continue
        java = _fn_name(original)
        if java in seen and seen[java] != original:
            raise EmitError(
                f"functions {seen[java]!r} and {original!r} both lower to the "
                f"Java name {java!r} after reserved-word renaming; rename one"
            )
        seen[java] = original


def _camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def _zero_java_value(java_type: str) -> str:
    if java_type == "long":
        return "0L"
    if java_type == "int":
        return "0"
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


def _call_label(node: object) -> str:
    """Best-effort human label for a residue record's `crossing`/`attempted`
    fields (docs/design/teardown-contract.md, 'The merged residue schema').
    Baked in as a Java string literal at EMIT TIME — the emitter already
    knows the call's name, so unlike the py reference's bytecode-sniffing
    fallback (`backends/python/runtime.py`, `_named_call_method`) this needs
    no runtime introspection. Never raises: an unrecognized shape degrades to
    a generic label rather than failing emission over a diagnostic string."""
    if not isinstance(node, dict):
        return "?"
    kind = node.get("kind")
    if kind == "fn":
        return str(node.get("name") or "?")
    if kind == "call":
        if "callee" in node:
            callee = node.get("callee") or {}
            return str(callee.get("name") or "?")
        return str(node.get("method") or "?")
    if kind == "host":
        return str(node.get("fn") or "?")
    return "?"


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
    # Int32 bitwise operators (item 366, docs/arithmetic.md). All native on a
    # Java `int` and none trap: `& | ^` are the bit ops; a `<<`/`>>` on `int`
    # masks the shift count to its low 5 bits (JLS 15.19), which is exactly the
    # spec's mod-32 rule, and `>>` is the arithmetic (sign-extending) shift.
    "&": "&", "|": "|", "^": "^", "<<": "<<", ">>": ">>",
}

_V3_ATOMIC_KINDS = {"var", "field", "index", "call", "lit"}
# Kinds that render as a Java postfix-safe primary, so a `.method()` suffix can
# be appended without wrapping them in parentheses. Wider than the set above
# because `_expr` also renders the v1 component dialect (`req`, `config`, ..).
_V3_POSTFIX_SAFE_KINDS = _V3_ATOMIC_KINDS | {"name", "req", "config", "host", "fn"}
_HOST_ROOTS = {"Pool", "Map", "Job"}

# item 416a: host roots this tier emits no runtime class for. `_emit_host_stubs`
# only ever writes a class for a root in `_HOST_STUBS`, so a call on any other
# root lowered to a bare `Stream.source()` naming a type the generated file
# never declares — the emitter was happy and javac was not. That is a SILENT
# EMIT where the design promises a refusal. `subscribe`/`stream-merge` were
# already refused here; a `Stream.source()`-only program was not, so the honest
# refusal covered only half the surface. Refuse the whole root instead.
_UNIMPLEMENTED_HOST_ROOTS = {
    "Stream": (
        "opens a stream, and a stream subscription suspends a fiber. The java "
        "blocking-tier lowering (a `BlockingQueue.poll` interruptible by the "
        "cancel signal, item 130 §4.6) is not implemented and this tier emits "
        "no `Stream` runtime class, so the emitted program would name a type "
        "the generated file never declares: streams run on py, go and rust; "
        "try `--backend py`"
    ),
}


def _refuse_missing_host_root(fn: str) -> None:
    """Refuse a host builtin whose ROOT this tier emits no runtime for, instead
    of emitting a call against a type the generated file does not declare."""
    root = (fn or "").split(".")[0]
    reason = _UNIMPLEMENTED_HOST_ROOTS.get(root)
    if reason is not None:
        raise EmitError(f"`{fn}` {reason}")


def _split_v3_types(inner: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in inner:
        if ch in "[(":
            depth += 1
        elif ch in "])":
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
    _reject_fn_type(name)
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
    if name == "Row" and name not in _V3_DECLARED_TYPES:
        # The host Pool's query-result row: undeclared by the document, handed
        # back by the runtime. `Pool.query` here returns `java.util.List<Object>`,
        # so `List[Row]` has to be exactly that or the emitted interface and its
        # implementation disagree and javac rejects the file. go names the row
        # (`type Row = map[string]string`), rust widens it to `Value`, and this
        # tier's own v1 renderer already widens it to `Object`; v3 was the one
        # renderer that emitted the bare name and produced uncompilable Java.
        return "Object"
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

    def __init__(self, types: dict, functions: list, externs: list,
                 components: list | None = None) -> None:
        self.types = types or {}
        self.function_names = {fn.get("name") for fn in functions or []}
        self.extern_names = {ext.get("name") for ext in externs or []}
        # item 243/318: witnessed externs by name, so a method/activation body's
        # effect step can be recognised as a transactional crossing. Empty for
        # every non-witnessed document, so their emission stays byte-identical.
        self.witnessed = _witnessed_externs(externs)
        # Every component in the document, keyed by name, so a `spawn`
        # acquisition can resolve its target template's config layout (the
        # plugin-constructor argument order) and provided keys (the services to
        # isolate into fresh local realms). Empty for non-spawning documents.
        self.spawn_targets: dict[str, dict] = {
            comp.get("name"): comp for comp in components or []
        }
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
        # Monotonic index handed to the destructure temporary and to a
        # witnessed step's Result/Ok temporaries, so their names are a
        # deterministic property of emission order rather than of object
        # identity. They used to be `id(node)` — a host address — so the SAME
        # IR emitted twice produced two different Java sources
        # (`__revl_destructure_4313623040` vs `__revl_destructure_4391233664`),
        # which is why backends/java/scenarios/crashproof/revl/Components.java
        # had to be exempted from the golden drift check. Same rule and same
        # remedy as item 179 on the reference tier
        # (backends/python/emit.py's `_Lines._destructure_seq`) and as the rust
        # tier's `env.wit_counter`. Kept separate from `_match_counter` so the
        # existing `__revl_ignored_N` numbering is untouched.
        self._gensym_counter = 0
        # local `let`s bound to an arrow literal, in the body being emitted:
        # {binding name: {"arrow": <arrow node>, "captures": {name: snapshot}}}.
        # See `_inline_arrow` for why an arrow has no Java declaration.
        self.arrows: dict[str, dict] = {}

    def new_match_ignored(self) -> str:
        self._match_counter += 1
        return f"__revl_ignored_{self._match_counter}"

    def next_gensym(self) -> int:
        """The next emission-order index for a generated local name."""
        self._gensym_counter += 1
        return self._gensym_counter

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
    # A reference to a top-level callable is resolved before `_ident`, which
    # would reject a keyword-named one (`double`) that `_fn_name` can rename.
    if not (rename and name in rename) and (
        name in ctx.function_names or name in ctx.extern_names
    ):
        return _fn_name(name)
    # a keyword-named local/case is renamed at its *use* the same way `_ident`
    # renamed it at its declaration (item 165)
    mangled = _ident(name, "name")
    if rename and name in rename:
        return rename[name]
    if name in ctx.arrows:
        # an arrow binding has no Java declaration to refer to (see "arrows")
        raise EmitError(_ARROW_VALUE_REFUSAL)
    if name in ctx.case_owners:
        return mangled
    if name == "None":
        return "java.util.Optional.empty()"
    return mangled


def _v3_len(target: str) -> str:
    return f"revlLength({target})"


def _v3_builtin(method: object, target: str, args: list[str],
                recv: str | None = None) -> str:
    """The stdlib surface (docs/stdlib-2.0.md), dispatched through the
    `revl*` static overloads (emitted once per file) — javac resolves the
    receiver's static type, so every (method, Str|List) pair from the spec
    table compiles. The previous inline lowerings picked one type per method
    (`subList` broke Str.slice, `String.concat` broke List.concat) and the
    `instanceof java.util.List` ternaries did not compile for receivers
    statically typed `String`. `recv` carries the receiver's static type only
    where the lowering must dispatch on it (`to_int`: the Int32 widen vs the
    Str parse)."""
    # The total forms (docs/arithmetic.md): same quotient as the faulting
    # operation, but a zero divisor yields Err(reason) instead of throwing —
    # `fail` is refused in a pure fn, so the error travels as a value. The
    # static helpers (emitted by `_emit_checked_div_helpers`) evaluate each
    # operand exactly once and return a typed RevlResult.
    if method in _CHECKED_DIVS:
        return f"{_CHECKED_HELPER[method]}({target}, {args[0]})"
    if method == "length":
        return f"revlLength({target})"
    if method == "push":
        return f"revlPush({target}, {args[0]})"
    if method == "slice":
        return f"revlSlice({target}, {args[0]}, {args[1]})"
    if method == "charAt":
        return f"revlCharAt(String.valueOf({target}), {args[0]})"
    if method == "charCodeAt":
        return f"revlCharCodeAt(String.valueOf({target}), {args[0]})"
    # Codepoint-at-index scan (item 276, docs/stdlib-2.0.md §Str.codepoint_at):
    # the Unicode scalar at code-point index i, via the same code-point-indexed
    # helper as charCodeAt.
    if method == "codepoint_at":
        return f"revlCharCodeAt(String.valueOf({target}), {args[0]})"
    if method == "concat":
        return f"revlConcat({target}, {args[0]})"
    # Integer division and modulo (docs/arithmetic.md). Java `/` truncates and
    # `Math.floorDiv`/`floorMod` give the rest; `floorMod` against |b| is the
    # Euclidean remainder, which is non-negative for either sign of b.
    # Int/Int32 width conversions (docs/arithmetic.md). Widening Int32 -> Int is
    # an explicit `(long)` cast; narrowing Int -> Int32 goes through
    # Math.toIntExact, which throws ArithmeticException out of the int range —
    # the same fault the other Int32 operations give.
    if method == "to_int":
        if recv == "Str":
            # Str.to_int (FR-9, docs/stdlib-2.0.md §Str.to_int): Long.parseLong
            # is total on the ASCII digits (leading `-` allowed) and throws on
            # empty/partial/`+` spellings AND out-of-long-range values; the
            # helper maps the throw to the tier's Opt None (Optional.empty()).
            return f"revlParseInt({target})"
        return f"((long) ({target}))"
    if method == "to_int32":
        return f"Math.toIntExact({target})"
    if method == "div_trunc":
        return f"(({target}) / ({args[0]}))"
    if method == "div_floor":
        return f"Math.floorDiv({target}, {args[0]})"
    if method == "div_euclid":
        return (f"(({args[0]}) > 0 ? Math.floorDiv({target}, {args[0]}) "
                f": -Math.floorDiv({target}, -({args[0]})))")
    if method == "mod":
        return f"Math.floorMod({target}, Math.abs({args[0]}))"
    if method == "indexOf":
        return f"revlIndexOf({target}, {args[0]})"
    if method == "split":
        return f"revlSplit({target}, {args[0]})"
    if method == "join":
        return f"revlJoin({target}, {args[0]})"
    if method == "repeat":
        return f"revlRepeat({target}, {args[0]})"
    # The prefix/suffix probes (FR-6, docs/stdlib-2.0.md §Str.startsWith).
    # A code-point prefix of a string is a UTF-16 prefix (code-point
    # boundaries never split), so the native startsWith/endsWith are exact.
    if method == "startsWith":
        return f"{target}.startsWith({args[0]})"
    if method == "endsWith":
        return f"{target}.endsWith({args[0]})"
    # The Map value type (docs/stdlib-2.0.md §Map): persistent HashMaps —
    # revlMapSet copies before it puts, so the receiver never mutates.
    if method == "set":
        return f"revlMapSet({target}, {args[0]}, {args[1]})"
    if method == "lookup":
        return f"revlMapGet({target}, {args[0]})"
    if method == "has":
        return f"revlMapHas({target}, {args[0]})"
    # The iteration/remove step (docs/stdlib-2.0.md §Map): persistent remove,
    # keys in canonical order via the code-point comparator (Java's
    # String.compareTo is UTF-16 code-unit order, wrong past U+FFFF).
    if method == "size":
        return f"(long) {target}.size()"
    if method == "keys":
        return f"revlMapKeys({target})"
    if method == "remove":
        return f"revlMapRemove({target}, {args[0]})"
    # The rendering builtin (docs/stdlib-2.0.md §Int.to_str): the receiver
    # lowers to a long, and String.valueOf(long) is exact decimal —
    # including Long.MIN_VALUE, no |MIN| detour needed.
    if method == "to_str":
        return f"String.valueOf({target})"
    raise EmitError(f"unknown builtin method {method!r}")


def _stdlib_helper_source() -> list[str]:
    """Every static overload backing the builtin surface, as one flat block.

    Overload resolution replaces runtime `instanceof` dispatch;
    `revlPush`/`revlConcat`/`revlSlice` return copies (persistent,
    docs/stdlib-2.0.md). `_emit_stdlib_helpers` slices this into per-helper
    groups and emits only the ones a document reaches (item 433 F6)."""
    return [
        "// revl stdlib surface (docs/stdlib-2.0.md) — typed static overloads.",
        "// A Str counts and indexes in Unicode code points (docs/strings.md);",
        "// Java's String APIs are UTF-16, so the String overloads go through",
        "// codePointCount/offsetByCodePoints. The List overloads are unchanged.",
        "private static long revlLength(String s) { return s.codePointCount(0, s.length()); }",
        "private static long revlLength(java.util.List<?> xs) { return xs.size(); }",
        "private static long revlLength(Object x) {",
        "    if (x instanceof java.util.List<?>) { return ((java.util.List<?>) x).size(); }",
        "    String s = String.valueOf(x);",
        "    return s.codePointCount(0, s.length());",
        "}",
        "// JS slice semantics: out-of-range bounds clamp, never throw. Bounds",
        "// are code-point offsets for a Str.",
        "private static String revlSlice(String s, long a, long b) {",
        "    int len = s.codePointCount(0, s.length());",
        "    int a2 = (int) Math.min(Math.max(a, 0L), len);",
        "    int b2 = (int) Math.max(Math.min(Math.max(b, 0L), len), a2);",
        "    return s.substring(s.offsetByCodePoints(0, a2), s.offsetByCodePoints(0, b2));",
        "}",
        "private static <T> java.util.List<T> revlSlice(java.util.List<T> xs, long a, long b) {",
        "    int len = xs.size();",
        "    int a2 = (int) Math.min(Math.max(a, 0L), len);",
        "    int b2 = (int) Math.max(Math.min(Math.max(b, 0L), len), a2);",
        "    return java.util.List.copyOf(xs.subList(a2, b2));",
        "}",
        "private static long revlIndexOf(String s, String needle) {",
        "    int i = s.indexOf(needle);",
        "    return i < 0 ? -1 : s.codePointCount(0, i);",
        "}",
        "private static <T> long revlIndexOf(java.util.List<T> xs, T v) { return xs.indexOf(v); }",
        "// charAt/charCodeAt are Str-only; the index and the returned scalar",
        "// are code points, not UTF-16 units (docs/strings.md).",
        "private static String revlCharAt(String s, long i) {",
        "    int len = s.codePointCount(0, s.length());",
        "    if (i < 0 || i >= len) { return \"\"; }",
        "    return new String(Character.toChars(s.codePointAt(s.offsetByCodePoints(0, (int) i))));",
        "}",
        "private static long revlCharCodeAt(String s, long i) {",
        "    return (long) s.codePointAt(s.offsetByCodePoints(0, (int) i));",
        "}",
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
        "// Str.to_int (FR-9, docs/stdlib-2.0.md §Str.to_int): Long.parseLong is",
        "// total on the ASCII digits (leading `-` allowed) and throws on empty,",
        "// partial, `+`-prefixed AND out-of-long-range spellings; the throw is",
        "// mapped to the tier's Opt None (Optional.empty()). parseLong would",
        "// also accept non-ASCII decimal digits (Character.digit), so the",
        "// ASCII gate runs first — revl's spec is ASCII-only.",
        "private static java.util.Optional<Long> revlParseInt(String s) {",
        "    if (s.isEmpty()) { return java.util.Optional.empty(); }",
        "    int i = 0;",
        "    if (s.charAt(0) == '-') {",
        "        i = 1;",
        "        if (s.length() == 1) { return java.util.Optional.empty(); }",
        "    }",
        "    for (; i < s.length(); i++) {",
        "        char c = s.charAt(i);",
        "        if (c < '0' || c > '9') { return java.util.Optional.empty(); }",
        "    }",
        "    try { return java.util.Optional.of(Long.parseLong(s)); }",
        "    catch (NumberFormatException e) { return java.util.Optional.empty(); }",
        "}",
        "// The Map value type (docs/stdlib-2.0.md §Map): persistent maps —",
        "// set copies before it puts; lookup answers the tier's Optional.",
        "private static <V> java.util.Map<String, V> revlMapSet(java.util.Map<String, V> m, String k, V v) {",
        "    java.util.Map<String, V> out = new java.util.HashMap<>(m);",
        "    out.put(k, v);",
        "    return out;",
        "}",
        "private static <V> java.util.Optional<V> revlMapGet(java.util.Map<String, V> m, String k) {",
        "    return java.util.Optional.ofNullable(m.get(k));",
        "}",
        "private static <V> boolean revlMapHas(java.util.Map<String, V> m, String k) {",
        "    return m.containsKey(k);",
        "}",
        "// remove copies before deleting (persistent); keys sorts a copied",
        "// list with a code-point comparator — canonical Str order even for",
        "// supplementary-plane keys, where compareTo would misorder.",
        "private static <V> java.util.Map<String, V> revlMapRemove(java.util.Map<String, V> m, String k) {",
        "    java.util.Map<String, V> out = new java.util.HashMap<>(m);",
        "    out.remove(k);",
        "    return out;",
        "}",
        "private static <V> java.util.List<String> revlMapKeys(java.util.Map<String, V> m) {",
        "    java.util.List<String> ks = new java.util.ArrayList<>(m.keySet());",
        "    ks.sort((a, b) -> {",
        "        int i = 0, j = 0;",
        "        while (i < a.length() && j < b.length()) {",
        "            int ca = a.codePointAt(i), cb = b.codePointAt(j);",
        "            if (ca != cb) { return Integer.compare(ca, cb); }",
        "            i += Character.charCount(ca); j += Character.charCount(cb);",
        "        }",
        "        return Boolean.compare(i >= a.length(), j >= b.length());",
        "    });",
        "    return java.util.List.copyOf(ks);",
        "}",
        "",
    ]


# The leading comment lines of `_stdlib_helper_source` describe the whole
# surface rather than any one helper, so they ride with the block, not a group.
_STDLIB_BLOCK_HEADER_LINES = 4


def _stdlib_helper_groups() -> tuple[list[str], dict[str, list[str]]]:
    """`(block header, helper name -> its lines)`.

    item 433 F6. The helper block used to be all-or-nothing: `_uses_stdlib`
    keyed on the NODE KIND (`builtin`, `len`, a `sized_length` field), never on
    WHICH helper that node needs, so a program reaching one helper carried all
    21 declarations under 16 names. MEASURED on openjdk 26.0.2:
    `examples/java_match.rvl` reaches ZERO of the 16 (its only `builtin` node is
    a `checked_div_trunc`, whose helper lives in the separate
    `_emit_checked_div_helpers` block) and still carried 6049 source bytes /
    6916 CLASS bytes of them, 42 percent of its compiled unit.

    Each helper's own leading comment lines ride with it, so a group that is
    not emitted takes its documentation with it.
    """
    lines = _stdlib_helper_source()
    header = lines[:_STDLIB_BLOCK_HEADER_LINES]
    groups: dict[str, list[str]] = {}
    pending: list[str] = []
    current: str | None = None
    buf: list[str] = []
    depth = 0
    for line in lines[_STDLIB_BLOCK_HEADER_LINES:]:
        text = line.strip()
        if current is None:
            if not text:
                continue
            if text.startswith("//"):
                pending.append(line)
                continue
            match = re.search(r"\b(revl[A-Za-z0-9]*)\s*\(", text)
            if match is None:  # pragma: no cover - the block is all declarations
                raise EmitError(f"unparsable stdlib helper line: {text!r}")
            current = match.group(1)
            buf = [*pending, line]
            pending = []
            depth = text.count("{") - text.count("}")
        else:
            buf.append(line)
            depth += text.count("{") - text.count("}")
        if depth <= 0:
            groups.setdefault(current, []).extend(buf)
            current = None
    return header, groups


def _stdlib_helpers_reached(emitted: list[str]) -> set[str]:
    """The helper names `emitted` actually calls, closed over helper-to-helper
    calls so a selected helper never loses one it needs itself."""
    _, groups = _stdlib_helper_groups()

    def called_in(text: str) -> set[str]:
        return {name for name in groups if re.search(r"\b" + name + r"\s*\(", text)}

    reached = called_in("\n".join(emitted))
    frontier = set(reached)
    while frontier:
        nxt: set[str] = set()
        for name in frontier:
            nxt |= called_in("\n".join(groups[name])) - reached
        reached |= nxt
        frontier = nxt
    return reached


def _emit_stdlib_helpers(names: set[str] | None = None) -> list[str]:
    """The stdlib helper block, restricted to `names` (all of them when None)."""
    header, groups = _stdlib_helper_groups()
    wanted = [name for name in groups if names is None or name in names]
    if not wanted:
        return []
    out = list(header)
    for name in wanted:
        out.extend(groups[name])
    out.append("")
    return out


_EQUALITY_OPS = ("==", "===", "!=", "!==")


def _uses_equality(ir: dict) -> bool:
    """True when any `==`/`!=` appears anywhere in the document, so `revlEq`
    is emitted exactly where it is called (the same gating idiom as
    `_uses_stdlib` and `_uses_checked_div`)."""
    def walk(node) -> bool:
        if isinstance(node, dict):
            if node.get("kind") == "bin" and node.get("op") in _EQUALITY_OPS:
                return True
            return any(walk(value) for value in node.values())
        if isinstance(node, list):
            return any(walk(value) for value in node)
        return False

    return (walk(ir.get("components")) or walk(ir.get("functions"))
            or walk(ir.get("tests")) or walk(ir.get("externs")))


def _emit_eq_helper() -> list[str]:
    """`==` on this tier.

    revl has ONE equality and it is structural (docs/syntax-2.0.md §3.4), with
    exactly one exception: `Float` is IEEE 754 binary64, so `==` on it is the
    IEEE comparison and is NOT reflexive: `NaN != NaN`, and `0.0 == -0.0`
    (docs/arithmetic.md, "Float is IEEE 754 binary64"; "This is not a
    divergence between tiers; every tier agrees").

    Java did diverge. `java.util.Objects.equals` boxes both operands, and
    `Double.equals` compares `doubleToLongBits`, which calls NaN equal to
    itself and negative zero unequal to zero, both the opposite of what
    python, TypeScript, rust and go compute for the same source. So a Double
    pair compares as primitives here, a List/Map/Optional recurses so a Float
    nested inside one keeps the same rule, and every other value keeps the
    structural `Objects.equals` behaviour it already had."""
    return [
        "// revl equality (docs/syntax-2.0.md §3.4): structural, EXCEPT that",
        "// `==` on Float is IEEE 754 (docs/arithmetic.md), so NaN != NaN and",
        "// 0.0 == -0.0. `Objects.equals` boxes to Double, whose `equals`",
        "// compares doubleToLongBits and gets both of those backwards.",
        "private static boolean revlEq(Object a, Object b) {",
        "    if (a instanceof Double && b instanceof Double) {",
        "        return ((Double) a).doubleValue() == ((Double) b).doubleValue();",
        "    }",
        "    if (a instanceof java.util.List<?> && b instanceof java.util.List<?>) {",
        "        java.util.List<?> xs = (java.util.List<?>) a;",
        "        java.util.List<?> ys = (java.util.List<?>) b;",
        "        if (xs.size() != ys.size()) { return false; }",
        "        for (int i = 0; i < xs.size(); i++) {",
        "            if (!revlEq(xs.get(i), ys.get(i))) { return false; }",
        "        }",
        "        return true;",
        "    }",
        "    if (a instanceof java.util.Map<?, ?> && b instanceof java.util.Map<?, ?>) {",
        "        java.util.Map<?, ?> xs = (java.util.Map<?, ?>) a;",
        "        java.util.Map<?, ?> ys = (java.util.Map<?, ?>) b;",
        "        if (xs.size() != ys.size()) { return false; }",
        "        for (java.util.Map.Entry<?, ?> e : xs.entrySet()) {",
        "            if (!ys.containsKey(e.getKey())) { return false; }",
        "            if (!revlEq(e.getValue(), ys.get(e.getKey()))) { return false; }",
        "        }",
        "        return true;",
        "    }",
        "    if (a instanceof java.util.Optional<?> && b instanceof java.util.Optional<?>) {",
        "        java.util.Optional<?> xs = (java.util.Optional<?>) a;",
        "        java.util.Optional<?> ys = (java.util.Optional<?>) b;",
        "        if (xs.isPresent() != ys.isPresent()) { return false; }",
        "        return xs.isEmpty() || revlEq(xs.get(), ys.get());",
        "    }",
        "    return java.util.Objects.equals(a, b);",
        "}",
        "",
    ]


def _uses_stdlib(ir: dict) -> bool:
    """True when any builtin/len node appears anywhere in the document — or a
    sized `.length` field (item 104): a component-position property-form
    `.length` stays a `field` node marked `sized_length` and emits
    `revlLength(...)`, so that helper must be emitted for it too."""
    found = False

    def walk(node) -> None:
        nonlocal found
        if found:
            return
        if isinstance(node, dict):
            if node.get("kind") in ("builtin", "len") or (
                    node.get("kind") == "field" and node.get("sized_length")):
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


def _is_float_expr(node: object) -> bool:
    """Node-local proof that an expression is a `Float`: a Float literal, a `/`
    (true division), a Float-annotated arithmetic node, or a unary minus of
    one — the shared proof the other tiers use (docs/strings.md)."""
    if not isinstance(node, dict):
        return False
    kind = node.get("kind")
    if kind == "lit":
        value = node.get("value")
        return isinstance(value, float) and not isinstance(value, bool)
    if kind == "bin":
        return node.get("op") == "/" or node.get("operands") == "Float"
    if kind == "un":
        return node.get("op") == "-" and _is_float_expr(node.get("operand"))
    return False


def _uses_float_interp(ir: dict) -> bool:
    """True when any `${…}` interpolates a provably-`Float` expression, so the
    canonical Float renderer is emitted only where it is used."""
    found = False

    def walk(node) -> None:
        nonlocal found
        if found:
            return
        if isinstance(node, dict):
            if node.get("kind") == "interp":
                for part in node.get("parts") or []:
                    if (isinstance(part, (list, tuple)) and len(part) == 2
                            and part[0] == "expr" and _is_float_expr(part[1])):
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


def _emit_ftoa_helper() -> list[str]:
    """Canonical Float -> Str: ECMAScript Number::toString (docs/strings.md).
    Double.toString supplies the shortest round-trip digits; this reformats
    them into the ES notation so `${aFloat}` agrees with every other tier."""
    return [
        "// Canonical Float -> Str (docs/strings.md): ECMAScript Number::toString.",
        "private static String revlFtoa(double x) {",
        "    if (Double.isNaN(x)) { return \"NaN\"; }",
        "    if (Double.isInfinite(x)) { return x < 0 ? \"-Infinity\" : \"Infinity\"; }",
        "    if (x == 0.0) { return \"0\"; }",
        "    String sign = x < 0 ? \"-\" : \"\";",
        "    String s = Double.toString(Math.abs(x));",
        "    String mant = s;",
        "    long exp = 0;",
        "    int e = s.indexOf('E');",
        "    if (e >= 0) { mant = s.substring(0, e); exp = Long.parseLong(s.substring(e + 1)); }",
        "    String intpart = mant, frac = \"\";",
        "    int d = mant.indexOf('.');",
        "    if (d >= 0) { intpart = mant.substring(0, d); frac = mant.substring(d + 1); }",
        "    String digits = intpart + frac;",
        "    long point = intpart.length() + exp;",
        "    int lead = 0;",
        "    while (lead + 1 < digits.length() && digits.charAt(lead) == '0') { lead++; point--; }",
        "    digits = digits.substring(lead);",
        "    int end = digits.length();",
        "    while (end > 1 && digits.charAt(end - 1) == '0') { end--; }",
        "    digits = digits.substring(0, end);",
        "    if (digits.equals(\"0\")) { return \"0\"; }",
        "    long k = digits.length();",
        "    long n = point;",
        "    String body;",
        "    if (k <= n && n <= 21) {",
        "        body = digits + \"0\".repeat((int) (n - k));",
        "    } else if (0 < n && n <= 21) {",
        "        int p = (int) n;",
        "        body = digits.substring(0, p) + \".\" + digits.substring(p);",
        "    } else if (-6 < n && n <= 0) {",
        "        body = \"0.\" + \"0\".repeat((int) (-n)) + digits;",
        "    } else {",
        "        long ee = n - 1;",
        "        String m = k > 1 ? digits.substring(0, 1) + \".\" + digits.substring(1) : digits;",
        "        String esign = ee < 0 ? \"-\" : \"+\";",
        "        body = m + \"e\" + esign + Math.abs(ee);",
        "    }",
        "    return sign + body;",
        "}",
    ]


# --------------------------------------------------------------------------
# arrows
#
# revl's checker *enumerates arrows in its unchecked frontier* (see the header
# of src/revl/typecheck.py): an arrow's parameters have no declared type and
# none is inferred, and `infer_ast` returns `None` for the arrow itself. Java
# has no way to spell that. A lambda is not a value with a type of its own —
# it needs a *target type*, i.e. a functional interface with a fixed arity and
# concrete parameter/return types, which is exactly the information that does
# not exist. The two ways out both fail:
#
#   * `java.util.function.Function<Object, Object>` is the type-honest choice,
#     but Java has no arithmetic on `Object`, so the body of even the simplest
#     arrow (`v => v + 1`) stops compiling. Type-honest and unusable.
#   * `Function<Long, Long>` (or a generated `long`-typed interface) compiles
#     that one body by *inventing* an arity and a parameter type the compiler
#     was never given, and silently miscompiles a string or record arrow.
#
# So arrows are **beta-reduced at the call site** instead: `let g = v => …` has
# no Java declaration, and `g(a)` emits the body with `a` substituted for `v`.
# Nothing is invented — javac derives the parameter types from the actual
# arguments, the same conclusion the wasm tier reached for its own reasons
# (backends/wasm/emit.py `_inline_arrow`). The cost is that an arrow can only
# be called, never used as a value; that position raises rather than emitting
# Java that does not compile, because revl has no lowerable function type to
# put it in anyway.
#
# By-value capture (docs/syntax-2.0.md §3.5: "captures are by-value") is
# preserved by snapshotting each captured `var` into a `final` local at the
# binding site — the call site may be reached after the `var` has moved on.

_ARROW_VALUE_REFUSAL = (
    "an arrow value is not lowerable on the Java tier — a Java lambda needs a "
    "target type, and an arrow's parameters are untyped; bind it with `let` "
    "and call it"
)

# expression kinds that are pure and cheap enough to substitute at more than
# one occurrence of a parameter; anything else would be re-evaluated.
_ARROW_REPEATABLE_KINDS = {
    "lit", "var", "name", "config", "req", "field", "index", "un", "bin",
    "if", "len",
}


def _subexprs(node: dict):
    for key, value in node.items():
        if key == "kind":
            continue
        if isinstance(value, dict) and "kind" in value:
            yield value
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and "kind" in item:
                    yield item


def _arrow_repeatable(node: object, ctx: _V3Ctx) -> bool:
    if not isinstance(node, dict):
        return False
    if node.get("kind") == "call":
        # a call to another local arrow is itself beta-reduced, so judge what
        # it will actually expand to (arrow composition stays lowerable)
        nested = _arrow_callee(node.get("callee"), ctx)
        if nested is None:
            return False
        return _arrow_repeatable(nested["arrow"].get("body"), ctx) and all(
            _arrow_repeatable(a, ctx) for a in node.get("args") or [])
    if node.get("kind") not in _ARROW_REPEATABLE_KINDS:
        return False
    return all(_arrow_repeatable(sub, ctx) for sub in _subexprs(node))


def _name_uses(node: object, name: str) -> int:
    if not isinstance(node, dict):
        return 0
    total = 1 if (
        (node.get("kind") == "var" and node.get("name") == name)
        or (node.get("kind") == "name" and node.get("id") == name)
    ) else 0
    return total + sum(_name_uses(sub, name) for sub in _subexprs(node))


def _arrow_callee(callee: object, ctx: _V3Ctx) -> dict | None:
    """The arrow binding this callee names, if any: an arrow literal applied
    on the spot, or a local `let` that was bound to one."""
    if not isinstance(callee, dict):
        return None
    if callee.get("kind") == "arrow":
        return {"arrow": callee, "captures": {}}
    if callee.get("kind") == "var":
        return ctx.arrows.get(callee.get("name"))
    if callee.get("kind") == "name":
        return ctx.arrows.get(callee.get("id"))
    return None


def _bind_local_arrow(
    ctx: _V3Ctx,
    name: str,
    value: object,
    out: list[str],
    pad: str,
    rename: dict[str, str] | None,
    env: "_Env | None",
) -> bool:
    """Handle a `let` whose right-hand side is an arrow, or an alias of one.

    The arrow itself emits no declaration (there is no type to declare it
    with); only the by-value snapshot of each captured mutable `var` does.
    Returns False when this `let` binds an ordinary value.
    """
    alias = _arrow_callee(value, ctx)
    if alias is not None and not (isinstance(value, dict) and value.get("kind") == "arrow"):
        ctx.arrows[name] = alias  # `let h = g` — a second name for one arrow
        return True
    if not (isinstance(value, dict) and value.get("kind") == "arrow"):
        return False
    captures: dict[str, str] = {}
    for captured in value.get("captures") or []:
        _ident(captured, "arrow capture")
        snapshot = f"__revl_capture_{name}_{captured}"
        source = _expr({"kind": "var", "name": captured}, ctx, rename, env)
        out.append(f"{pad}final var {snapshot} = {source};")
        captures[captured] = snapshot
    ctx.arrows[name] = {"arrow": value, "captures": captures}
    return True


def _inline_arrow(
    binding: dict,
    arg_nodes: list,
    ctx: _V3Ctx,
    rename: dict[str, str] | None = None,
    env: "_Env | None" = None,
) -> str:
    arrow = binding["arrow"]
    params = [_ident(p, "arrow parameter") for p in arrow.get("params") or []]
    if len(arg_nodes) != len(params):
        raise EmitError(
            f"arrow expects {len(params)} argument(s), got {len(arg_nodes)}"
        )
    body = arrow.get("body")
    inner = dict(rename or {})
    inner.update(binding.get("captures") or {})
    for param, arg in zip(params, arg_nodes):
        if _name_uses(body, param) > 1 and not _arrow_repeatable(arg, ctx):
            raise EmitError(
                f"arrow parameter {param!r} is used more than once and its "
                f"argument cannot be substituted twice without re-evaluating "
                f"it — bind the argument to a `let` first"
            )
        inner[param] = f"({_expr(arg, ctx, rename, env)})"
    return f"({_expr(body, ctx, inner, env)})"


def _v3_call(
    node: dict,
    ctx: _V3Ctx,
    rename: dict[str, str] | None = None,
    env: _Env | None = None,
) -> str:
    callee = node.get("callee")
    arg_nodes = node.get("args") or []
    # An arrow is only ever *called* on this tier (see `_inline_arrow`), so a
    # call is resolved against the local arrow bindings before anything else.
    arrow = _arrow_callee(callee, ctx)
    if arrow is not None:
        return _inline_arrow(arrow, arg_nodes, ctx, rename, env)
    args = [_expr(a, ctx, rename, env) for a in arg_nodes]
    if isinstance(callee, dict) and callee.get("kind") == "var":
        name = callee.get("name")
        if name in ctx.function_names or name in ctx.extern_names:
            # keyword-named callables are renamed, not rejected (see `_fn_name`)
            return f"{_fn_name(name)}({', '.join(args)})"
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
    if isinstance(callee, dict) and callee.get("kind") == "field":
        # A call whose callee is a member access is a *method invocation*
        # (`Pool.open(..)`, `p.execute(..)`, `Job.run(..)`), not the
        # application of a functional-valued field. Java has no `.field(args)`
        # unification with `.field.apply(args)`, so rendering it through the
        # generic `.apply` fallback below emitted `Pool.open.apply(url, 3L)`,
        # which does not compile ("package Pool does not exist"). The other
        # tiers all lower this to a real method call (ts `p.execute(..)`, rust
        # `p.execute(..)`); this is the Java tier catching up.
        target_node = callee.get("target")
        target = _expr(target_node, ctx, rename, env)
        if not (isinstance(target_node, dict) and target_node.get("kind") in _V3_ATOMIC_KINDS):
            target = f"({target})"
        return f"{target}.{_ident(callee.get('name'), 'method')}({', '.join(args)})"
    callee_s = _expr(callee, ctx, rename, env)
    return f"{callee_s}.apply({', '.join(args)})"


def _expr(
    node: object,
    ctx: "_V3Ctx | None" = None,
    rename: dict[str, str] | None = None,
    env: _Env | None = None,
) -> str:
    """The single expression renderer for the Java backend.

    It covers every IR kind this backend emits — both the v1 *component*
    dialect (`req`, `config`, `host`, and the `target`/`method` shape of
    `call`) and the 2.0 dialect (`callee`/`args` calls, `match`, `adt`,
    `??`, arrows, …). Kinds that exist in both dialects (`call`) dispatch on
    **shape** — the presence of a distinguishing key such as `callee` — never
    on kind alone, so a component-shaped node can never be read as a 2.0 one.

    `ctx` carries the 2.0 environment (declared types, functions/externs, ADT
    case owners, local arrow bindings). Legacy v1 component/method bodies have
    no such environment and call in with `ctx=None`; a throwaway empty context
    stands in for them — those bodies only ever contain v1 kinds plus the
    context-free 2.0 kinds (`bin`, `un`, `field`, `if`, …). `rename` maps a
    binding to its Java spelling (e.g. `x` -> `this.x` for a provider field);
    `env` is the component's requires/provides frame, threaded through for
    callers that need it.
    """
    if ctx is None:
        ctx = _V3Ctx({}, [], [])
    if not isinstance(node, dict) or "kind" not in node:
        raise EmitError(f"malformed v3 expression: {node!r}")
    # An implicit Int -> Float coercion site (docs/arithmetic.md): java used
    # to absorb it behind JLS 5.1.2's long->double widening; the marker makes
    # the conversion explicit in the emitted source.
    if node.get("widen") == "Float":
        inner = {k: v for k, v in node.items() if k != "widen"}
        return f"((double) ({_expr(inner, ctx, rename, env)}))"
    # An Int32 -> Int widening site (docs/arithmetic.md): int -> long is an
    # implicit JLS widening, but the marker makes it explicit so the emitted
    # source reads the same as every other tier's.
    if node.get("widen") == "Int":
        inner = {k: v for k, v in node.items() if k != "widen"}
        return f"((long) ({_expr(inner, ctx, rename, env)}))"
    kind = node["kind"]

    if kind == "lit":
        return _lit(node.get("value"))

    if kind == "var":
        return _v3_var(node, ctx, rename)

    if kind == "adt":
        case = node.get("case")
        _ident(case, "adt case")
        args = ", ".join(_expr(a, ctx, rename, env) for a in node.get("args") or [])
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
        if original in ctx.arrows:
            raise EmitError(_ARROW_VALUE_REFUSAL)
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
        _refuse_missing_host_root(fn)
        host, _, method = fn.partition(".")
        _refuse_stream_host(fn)
        args = ", ".join(
            _expr(a, ctx, rename, env) for a in node.get("args") or []
        )
        return f"{host}.{_host_method(method)}({args})"

    if kind == "call":
        if "callee" in node:
            return _v3_call(node, ctx, rename, env)
        target = node.get("target") or {}
        method = _ident(node.get("method"), "method")
        args = ", ".join(
            _expr(a, ctx, rename, env) for a in node.get("args") or []
        )
        recv = _expr(target, ctx, rename, env)
        return f"{recv}.{method}({args})"

    if kind == "format":
        template = node.get("template") or ""
        args = [_expr(a, ctx, rename, env) for a in node.get("args") or []]
        return _format_java(template, args)

    if kind == "fn":
        fn_name = _fn_name(node.get("name"))
        args = ", ".join(
            _expr(a, ctx, rename, env) for a in node.get("args") or []
        )
        return f"{fn_name}({args})"

    if kind == "bin":
        op = node.get("op")
        if op == "??":
            # `a ?? b` (docs/syntax-2.0.md §3.2): `Opt[T]` is
            # `java.util.Optional<T>` on this tier, so the absent case is
            # `Optional.empty()`. `orElseGet` keeps `b` lazy — `??` must not
            # evaluate its right operand when `a` is present, and `orElse`
            # would evaluate it unconditionally.
            left_opt = _expr(node["left"], ctx, rename, env)
            if node["left"].get("kind") not in _V3_POSTFIX_SAFE_KINDS:
                left_opt = f"({left_opt})"
            right_opt = _expr(node["right"], ctx, rename, env)
            return f"{left_opt}.orElseGet(() -> {right_opt})"
        left = _expr(node["left"], ctx, rename, env)
        right = _expr(node["right"], ctx, rename, env)
        if op in ("==", "==="):
            return f"revlEq({left}, {right})"
        if op in ("!=", "!=="):
            return f"!revlEq({left}, {right})"
        if op == "/" and node.get("operands") in ("Int", "Int32"):
            # `Int / Int` is TRUE division and yields `Float`
            # (docs/arithmetic.md: "§0 governs, syntax revl shares with
            # TypeScript means what TypeScript means"). Java's `/` on two
            # `long`s is INTEGER division, so falling through to the plain
            # operator made `1 / 2` be `0.0` here and `0.5` on python,
            # TypeScript, rust and go. The `/` node already carries `operands`
            # for exactly this reason, so widen both sides first. That is the
            # shape rust emits (`(a) as f64 / (b) as f64`) and go emits
            # (`revlDiv(float64(a), float64(b))`).
            return f"(((double) ({left})) / ((double) ({right})))"
        if op in ("+", "-", "*") and node.get("operands") in ("Int", "Int32"):
            # Int/Int32 overflow traps (docs/arithmetic.md); Math.*Exact throws
            # ArithmeticException, which is exactly the fault we want. Each has
            # an `int` overload as well as a `long` one, so an Int32 operand
            # (a Java `int`) resolves to the 32-bit check with no extra code.
            exact = {"+": "addExact", "-": "subtractExact", "*": "multiplyExact"}[op]
            return f"Math.{exact}({left}, {right})"
        java_op = _JAVA_V3_BIN_OPS.get(op)
        if java_op is None:
            raise EmitError(f"unsupported binary operator {op!r}")
        return f"({left} {java_op} {right})"

    if kind == "un":
        operand = _expr(node.get("operand"), ctx, rename, env)
        if node.get("op") == "!":
            return f"(!{operand})"
        if node.get("op") == "~":
            # Int32 bitwise complement (item 366): Java's `~` on an `int`. A bit
            # op, so it never traps.
            return f"(~{operand})"
        if node.get("op") == "-":
            if node.get("operands") in ("Int", "Int32"):
                # negating Long.MIN / Integer.MIN_VALUE overflows; Math.negateExact
                # (long and int overloads) throws the same ArithmeticException as
                # the other Int/Int32 operations (docs/arithmetic.md)
                return f"Math.negateExact({operand})"
            return f"(-{operand})"
        raise EmitError(f"unsupported unary operator {node.get('op')!r}")

    if kind == "field":
        target_node = node.get("target")
        target = _expr(target_node, ctx, rename, env)
        if not (isinstance(target_node, dict) and target_node.get("kind") in _V3_ATOMIC_KINDS):
            target = f"({target})"
        if node.get("sized_length"):
            # item 104 (cross-tier): property-form `.length` on a sized value in
            # a component position — the code-point (Str) / element (List) count
            # via `_v3_len`, the same as the `len` node, NOT a record member
            # access (which would not resolve on a `String`).
            return _v3_len(target)
        return f"{target}.{_ident(node.get('name'), 'field')}"

    if kind == "index":
        target_node = node.get("target")
        target = _expr(target_node, ctx, rename, env)
        if not (isinstance(target_node, dict) and target_node.get("kind") in _V3_ATOMIC_KINDS):
            target = f"({target})"
        return f"{target}.get((int)({_expr(node['index'], ctx, rename, env)}))"

    if kind == "builtin":
        target_node = node.get("target")
        target = _expr(target_node, ctx, rename, env)
        if not (isinstance(target_node, dict) and target_node.get("kind") in _V3_ATOMIC_KINDS):
            target = f"({target})"
        args = [_expr(a, ctx, rename, env) for a in node.get("args") or []]
        return _v3_builtin(node.get("method"), target, args, node.get("recv"))

    if kind == "len":
        return _v3_len(_expr(node.get("target"), ctx, rename, env))

    if kind == "if":
        return (
            f"({_expr(node['cond'], ctx, rename, env)} ? "
            f"{_expr(node['then'], ctx, rename, env)} : "
            f"{_expr(node['else'], ctx, rename, env)})"
        )

    if kind == "record_update":
        raise EmitError(
            "functional record update `{r | f = e}` is not emitted by the java "
            "backend yet (implemented tiers: python, typescript) — see "
            "docs/records.md §6; lift it into a helper fn instead")

    if kind == "record":
        fields = node.get("fields") or []
        type_name = ctx.record_type_for_fields([k for k, _ in fields])
        spec = ctx.types[type_name]
        by_name = {k: _expr(v, ctx, rename, env) for k, v in fields}
        args = []
        for field_name in spec.get("fields") or {}:
            if field_name not in by_name:
                raise EmitError(f"record literal is missing field {field_name!r}")
            args.append(by_name[field_name])
        return f"new {_ident(type_name, 'type name')}({', '.join(args)})"

    if kind == "list":
        return "java.util.List.of(" + ", ".join(
            _expr(item, ctx, rename, env) for item in node.get("items") or []
        ) + ")"

    if kind == "maplit":
        # `Map.empty()` (docs/stdlib-2.0.md §Map). Inference comes from the
        # surrounding target type (return / declared variable / argument),
        # which covers every typed position — `Map.of()` is a generic method
        # and infers in exactly the poly positions the diamond did.
        #
        # item 433 F8: this used to be `new java.util.HashMap<>()`. Every value
        # -map writer (`revlMapSet`, `revlMapRemove`) COPIES before it mutates,
        # so no caller ever writes through the returned map, and
        # `java.util.Map.of()` is a preallocated singleton. MEASURED on openjdk
        # 26.0.2: 48 B per evaluation that escapes, against 0. (When the map
        # does NOT escape, C2 scalar-replaces the HashMap and both are 0, so
        # the win is exactly on the escaping path.)
        return "java.util.Map.of()"

    if kind == "arrow":
        # reached only in *value* position — a called arrow is beta-reduced by
        # `_v3_call`. There is no functional interface to target here (see the
        # "arrows" note above), and revl has no lowerable function type that
        # would let the value go anywhere useful.
        raise EmitError(_ARROW_VALUE_REFUSAL)

    if kind == "match":
        return _v3_match_expr(node, ctx, rename, env)

    if kind == "interp":
        parts = node.get("parts") or []
        segs: list[str] = []
        for part_kind, value in parts:
            if part_kind == "text":
                segs.append(_string(value))
            elif _is_float_expr(value):
                # A Float renders through the canonical ECMAScript form, not
                # String.valueOf (which gives "1.0E21"/"0.0"/"-0.0"); see
                # docs/strings.md.
                segs.append(f"revlFtoa({_expr(value, ctx, rename, env)})")
            else:  # ["expr", ir_node] — a full expression, stringified
                # item 433 F9: no `String.valueOf` wrapper. Java string
                # concatenation already applies `String.valueOf` to every
                # operand, so the wrapper only built an intermediate `String`
                # (and its `byte[]`) for the surrounding concatenation to copy
                # again; `invokedynamic makeConcatWithConstants` takes the
                # primitive directly. MEASURED on openjdk 26.0.2 over
                # `` `hi ${name} x${n}!` ``: 56 B per interpolation with the
                # wrapper, 32 B without. Parenthesized unless the node is
                # already atomic, so an operand's own precedence cannot leak
                # into the `+` chain.
                rendered = _expr(value, ctx, rename, env)
                if value.get("kind") not in _V3_POSTFIX_SAFE_KINDS:
                    rendered = f"({rendered})"
                segs.append(rendered)
        if not segs:
            return '""'
        # The chain must open in String position or `+` reads as arithmetic.
        if not segs[0].startswith('"'):
            segs.insert(0, '""')
        return " + ".join(segs)

    if kind in ("optfield", "optcall"):
        raise EmitError(
            f"optional chaining (`?.`) is not yet lowerable on the Java tier "
            f"({kind!r}); unwrap with `match` or `??` for now"
        )

    if kind == "spawn":
        return _v3_spawn(node, ctx, rename, env)

    if kind == "instance-get":
        return _v3_instance_get(node, ctx, rename, env)

    if kind in ("subscribe", "stream-merge"):
        # item 130: a stream subscription suspends a fiber. Slice 3 lowered the
        # cancel-channel `select` on go and rust; the java erasure (a
        # `BlockingQueue.poll` interruptible by the cancel signal, design §4.6)
        # is NOT written yet, and this tier's emitter cannot be verified here —
        # no JDK. Refuse honestly rather than emit a subscription whose bracket
        # inverse has never been proven reachable off the teardown thread.
        raise EmitError(
            "a stream subscription suspends a fiber; the java blocking-tier "
            "lowering (a `BlockingQueue.poll` interruptible by the cancel "
            "signal) is not implemented — streams run on py, go and rust "
            "(item 130 §4.6); try `--backend py`"
        )

    raise EmitError(f"unsupported v3 expression kind {kind!r}")


def _v3_instance_get(
    node: dict,
    ctx: _V3Ctx,
    rename: dict[str, str] | None,
    env: "_Env | None",
) -> str:
    """Lower the instance accessor `s.<key>` (docs/design-v2-instances.md,
    "Instance accessor — frozen").

    `target` is a name bound to a `spawn` handle (`RevlSpawnHandle`); `key` is a
    key the spawned component provides. The matching `spawn` isolated that key's
    service into the instance's OWN private local realm (a fork()-isolated child
    `Context`, per-spawn-unique label), and the handle stored that child context.
    Resolving through it — `<handle>.get(<Svc>.class)` — yields THAT instance's
    provision and no other's: only the spawner holding this handle reaches it,
    so a sibling instance (a different realm) and the root cannot
    (supervision-tree addressing). `service` is frozen inline on the node (the
    typing rule's result), so this tier never re-derives it — mirroring how
    `_v3_spawn` reads the realm services and the cordis-py reference
    (backends/python/emit.py + runtime.py `SpawnHandle.get`).
    """
    target = _expr(node.get("target"), ctx, rename, env)
    key = node.get("key")
    if not isinstance(key, str) or not key.isidentifier():
        raise EmitError(f"bad instance-get key {key!r}")
    service = node.get("service")
    if not isinstance(service, str) or not service.isidentifier():
        raise EmitError(f"bad instance-get service {service!r}")
    return f"{target}.get({_ident(service, 'service')}.class)"


def _v3_spawn(
    node: dict,
    ctx: _V3Ctx,
    rename: dict[str, str] | None,
    env: "_Env | None",
) -> str:
    """Lower an instance-parametric `spawn` acquisition (docs/design-v2-instances.md).

    `spawn` is the acquisition of a `let-effect` step, so this renders the
    expression that step binds to the handle. It plugs the target *template* as
    a CHILD instance of the spawner — each key the template provides isolated
    into a FRESH LOCAL realm (a per-spawn-unique label, so two instances of one
    component never collide on a provision) — and returns a `RevlSpawnHandle`
    wrapping that instance's own teardown scope. The step's `undo`
    (`<handle>.dispose()`) reclaims it early, in LIFO order; if it never runs,
    the instance is torn down with the spawner (the parent scope is the safety
    net). Mirrors the rust/cordis (ts) lowering that already landed.
    """
    target = node.get("component")
    if not isinstance(target, str) or not target.isidentifier():
        raise EmitError(f"bad spawn component {target!r}")
    template = ctx.spawn_targets.get(target)
    if template is None:
        raise EmitError(
            f"spawn target {target!r} is not a component in this document"
        )
    cname = _ident(target, "component")
    # Config values in the target's DECLARED order — the order its plugin
    # constructor takes them (`_emit_plugin_ctors`). A field the spawn omits
    # (only possible when it has a default; the lowerer requires the rest) is
    # filled with that default, matching the full-argument constructor.
    supplied = node.get("config") or {}
    ctor_args = []
    for field in template.get("config") or []:
        fname = field.get("name")
        if fname in supplied:
            ctor_args.append(_expr(supplied[fname], ctx, rename, env))
        else:
            ctor_args.append(_config_default_lit(field, _java_v3_type))
    # Each provided key -> the service to isolate into its own fresh local
    # realm at plug time (`Context.isolate(<Svc>.class, <fresh label>)`).
    provides = template.get("provides") or {}
    realm_classes = ", ".join(
        f"{_ident(provides[key], 'service')}.class"
        for key in node.get("realms") or []
    )
    ctx_expr = rename.get("ctx", "ctx") if rename else "ctx"
    return (
        f"RevlSpawnHandle.spawn({ctx_expr}, new {cname}Plugin("
        f"{', '.join(ctor_args)}), new Class<?>[]{{{realm_classes}}})"
    )


def _v3_match_expr(
    node: dict,
    ctx: _V3Ctx,
    rename: dict[str, str] | None = None,
    env: _Env | None = None,
) -> str:
    scrutinee = _expr(node.get("scrutinee"), ctx, rename, env)
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
        # `bind == "_"` (`Some(_) => ..`) has no name to hold the value: it is
        # treated the same as an absent bind (the synthetic `__revl_v` lambda
        # param, unused in the body) rather than emitted as a literal `_`
        # lambda parameter, which `javac --release 21` refuses (`_` is a
        # reserved identifier outside the --enable-preview unnamed-variable
        # feature, JEP 443/456).
        some_bind = (_ident(some_arm.get("bind"), "match bind")
                     if some_arm and some_arm.get("bind") and some_arm.get("bind") != "_"
                     else "__revl_v")
        some_body = _expr((some_arm or wild).get("body"), ctx, rename, env)
        none_body = _expr((none_arm or wild).get("body"), ctx, rename, env)
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
            body = _expr(arm.get("body"), ctx, rename, env)
            if pattern == "_":
                wildcard = f"            default -> {{ yield ({body}); }}"
                continue
            var = ctx.new_match_ignored()
            lines.append(f"            case RevlResult.{pattern}<?, ?> {var} -> {{")
            bind = arm.get("bind")
            # `bind == "_"` (`Ok(_) => ..`) has no name to hold the value;
            # skip the cast-and-bind entirely, same as an absent bind — a
            # literal `_` local (`final var _ = ..;`) is a reserved
            # identifier at `--release 21` without --enable-preview.
            if bind and bind != "_":
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
    covered = {arm.get("pattern") for arm in arms}
    for arm in arms:
        pattern = arm.get("pattern")
        body = _expr(arm.get("body"), ctx, rename, env)
        if pattern == "_":
            wildcard = f"            default -> {{ yield ({body}); }}"
            continue
        case = _ident(pattern, "case name")
        qualified = f"{_ident(ctx.case_owners[case], 'type name')}.{case}" if case in ctx.case_owners else case
        bind = arm.get("bind")
        # `bind == "_"` (`Case(_) => ..`) has no name to hold the value: fall
        # through to the no-bind branch below (an ignored pattern variable),
        # rather than emitting a literal `_` local — a reserved identifier at
        # `--release 21` without --enable-preview (JEP 443/456).
        if bind and bind != "_":
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
    if wildcard is not None:
        lines.append(wildcard)
    elif not _covers_variant(covered, ctx):
        lines.append("            default -> { throw new IllegalArgumentException(\"non-exhaustive match\"); }")
    lines.append("        }")
    return "\n".join(lines)


def _covers_variant(patterns: set, ctx: _V3Ctx) -> bool:
    """Do these arms name every case of one sealed ADT?

    A pattern `switch` over a sealed type that is already total is *complete*
    to javac; the guard arm this emitter used to append unconditionally is
    then at best dead and at worst rejected outright ("switch has both an
    unconditional pattern and a default label"). Omitting it also hands the
    exhaustiveness check to javac, which is where it belongs — revl already
    rejects a non-exhaustive match in `lower.py`, so the guard only ever fired
    for a match the frontend could not see the scrutinee type of, and that
    case still gets it.
    """
    owners = {ctx.case_owners.get(p) for p in patterns}
    if len(owners) != 1 or None in owners:
        return False
    spec = ctx.types.get(owners.pop()) or {}
    return {case.get("name") for case in spec.get("cases") or []} <= patterns


def _adt_binding_type(value: object, ctx: _V3Ctx | None) -> str | None:
    """The sealed-interface name a binding must be declared with when it is
    initialised from an ADT construction, or None.

    revl types `Found(x)` as its *ADT* (`typecheck.py`: an ADT constructor
    returns `case["adt"]`), but the java tier gives each case its own nested
    class, so `var o = new Outcome.Found(x)` freezes `o` at the variant.
    A later `match` on `o` then emits `case Outcome.Missing …` against a
    selector of type `Outcome.Found` — javac: "incompatible types: Found
    cannot be converted to Missing" — and `case Outcome.Found …` becomes an
    unconditional pattern, which may not sit next to a `default` label.
    Naming the interface restores the type revl gave the expression.
    """
    if ctx is None or not isinstance(value, dict) or value.get("kind") != "adt":
        return None
    owner = ctx.case_owners.get(value.get("case"))
    return _ident(owner, "type name") if owner else None


def _let_keyword(node: dict, ctx: _V3Ctx | None = None) -> str:
    """Declaration type for a v3 `let`/`var`.

    An empty list literal has no element type, and `var` would freeze it as
    `List<Object>` — later pushes and the declared return type then fail to
    unify. A raw `java.util.List` keeps javac's erasure in play (unchecked
    warning, correct code); non-empty literals infer their element type.
    """
    value = node.get("value")
    if isinstance(value, dict) and value.get("kind") == "list" and not value.get("items"):
        return "java.util.List"
    adt = _adt_binding_type(value, ctx)
    if adt is not None:
        return adt if node.get("mutable") else f"final {adt}"
    return "var" if node.get("mutable") else "final var"


# item 379 (docs/design/379-break-continue.md): the frame-neutrality invariant is
# enforced whole-IR in the frontend; this is the cheap per-emitter guard. It is
# most load-bearing here: `_emit_setup_stmt` (the activation/setup tier) emits
# loop steps, so a leak into an activation body would otherwise compile silently.
_LOOP_REGISTERING_STEPS = frozenset({
    "effect", "let-effect", "emit", "timer", "approval", "spawn",
})


def _guard_frame_neutral_loop(body) -> None:
    for child in body or []:
        if isinstance(child, dict) and child.get("step") in _LOOP_REGISTERING_STEPS:
            raise EmitError(
                f"frame-neutral loop invariant: a `{child['step']}` step inside a "
                "while/for body (docs/design/379-break-continue.md)")


def _v3_stmt(node: dict, ctx: _V3Ctx, out: list[str], indent: int, *, test_mode: bool = False) -> None:
    pad = "    " * indent
    step = node.get("step")
    if step in ("let", "assign"):
        name = _ident(node.get("name"), "binding")
        raw = node.get("value")
        if step == "let" and _bind_local_arrow(ctx, name, raw, out, pad, None, None):
            return
        ctx.arrows.pop(name, None)  # the name no longer denotes an arrow
        value = _expr(raw, ctx)
        if step == "let":
            out.append(f"{pad}{_let_keyword(node, ctx)} {name} = {value};")
        else:
            out.append(f"{pad}{name} = {value};")
    elif step == "return":
        if node.get("expr") is None:
            out.append(f"{pad}return;")
        else:
            out.append(f"{pad}return {_expr(node['expr'], ctx)};")
    elif step == "if":
        out.append(f"{pad}if ({_expr(node['cond'], ctx)}) {{")
        for child in node.get("then") or []:
            _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        if node.get("else"):
            out.append(f"{pad}}} else {{")
            for child in node["else"]:
                _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        out.append(f"{pad}}}")
    elif step == "while":
        _guard_frame_neutral_loop(node.get("body"))
        out.append(f"{pad}while ({_expr(node['cond'], ctx)}) {{")
        for child in node.get("body") or []:
            _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        out.append(f"{pad}}}")
    elif step == "for":
        _guard_frame_neutral_loop(node.get("body"))
        bind = _ident(node.get("bind"), "loop binding")
        ctx.arrows.pop(bind, None)
        out.append(f"{pad}for (var {bind} : {_expr(node['iterable'], ctx)}) {{")
        for child in node.get("body") or []:
            _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        out.append(f"{pad}}}")
    elif step == "break":
        out.append(f"{pad}break;")
    elif step == "continue":
        out.append(f"{pad}continue;")
    elif step == "let_pattern":
        value = _expr(node.get("value"), ctx)
        tmp = f"__revl_destructure_{ctx.next_gensym()}"
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
        out.append(f"{pad}{_expr(node['expr'], ctx)};")
    elif step == "assert":
        out.append(
            f"{pad}if (!({_expr(node['expr'], ctx)})) "
            f'throw new AssertionError("assertion failed");'
        )
    else:
        raise EmitError(f"unsupported fn statement step {step!r}")


def _uses_builtin_result(ir: dict) -> bool:
    """True if the IR constructs or matches the built-in Result (Ok/Err) — an
    `adt` node typed Result, a match arm on Ok/Err, or a call to one of the
    total division forms (which produce a Result). Emitted RevlResult is
    gated on this so non-Result modules and v1 goldens are unaffected."""
    def walk(node) -> bool:
        if isinstance(node, dict):
            if node.get("kind") == "adt" and str(node.get("type", "")).startswith("Result"):
                return True
            if node.get("kind") == "match" and any(
                a.get("pattern") in ("Ok", "Err") for a in node.get("arms") or []
            ):
                return True
            if node.get("method") in _CHECKED_DIVS:
                return True
            return any(walk(v) for v in node.values())
        if isinstance(node, list):
            return any(walk(v) for v in node)
        return False

    # item 243: a witnessed extern returns `Result[Witness, Error]` and its
    # emitted call site branches on `Ok` to register the transactional entry,
    # so RevlResult must be present even when no surface `match`/`adt` names
    # it (mirrors backends/python/emit.py's identical witnessed gate).
    if any(ext.get("class") == "witnessed" for ext in ir.get("externs") or []):
        return True
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


# The total, value-returning division forms (docs/arithmetic.md): same
# rounding as the faulting operations, Err(reason) at a zero divisor.
_CHECKED_DIVS = ("checked_div_trunc", "checked_div_floor",
                 "checked_div_euclid", "checked_mod")
_CHECKED_HELPER = {
    "checked_div_trunc": "revlCheckedDivTrunc",
    "checked_div_floor": "revlCheckedDivFloor",
    "checked_div_euclid": "revlCheckedDivEuclid",
    "checked_mod": "revlCheckedMod",
}
_DIV_ZERO_MSG = "revl: division by zero"


def _uses_checked_div(ir: dict) -> bool:
    """True if the IR calls one of the total division forms — gates the
    static helpers those calls lower to."""
    def walk(node) -> bool:
        if isinstance(node, dict):
            if node.get("method") in _CHECKED_DIVS:
                return True
            return any(walk(v) for v in node.values())
        if isinstance(node, list):
            return any(walk(v) for v in node)
        return False

    return walk(ir.get("functions")) or walk(ir.get("tests")) or walk(ir.get("components"))


def _emit_checked_div_helpers() -> list[str]:
    """Static helpers backing the total division forms — emitted once per
    file when any `checked_*` node is present. Each evaluates its operands
    exactly once and returns a typed RevlResult."""
    return [
        "// total division forms (docs/arithmetic.md): a zero divisor yields",
        "// Err(reason) instead of the fault the unchecked operations raise.",
        "private static RevlResult<Long, String> revlCheckedDivTrunc(long a, long b) {",
        f'    if (b == 0L) {{ return new RevlResult.Err<>("{_DIV_ZERO_MSG}"); }}',
        '    if (a == Long.MIN_VALUE && b == -1L) { return new RevlResult.Err<>("revl: Int overflow"); }',
        "    return new RevlResult.Ok<>(a / b);",
        "}",
        "private static RevlResult<Long, String> revlCheckedDivFloor(long a, long b) {",
        f'    if (b == 0L) {{ return new RevlResult.Err<>("{_DIV_ZERO_MSG}"); }}',
        '    if (a == Long.MIN_VALUE && b == -1L) { return new RevlResult.Err<>("revl: Int overflow"); }',
        "    long q = a / b;",
        "    if (a % b != 0L && ((a < 0L) != (b < 0L))) { q -= 1L; }",
        "    return new RevlResult.Ok<>(q);",
        "}",
        "private static RevlResult<Long, String> revlCheckedDivEuclid(long a, long b) {",
        f'    if (b == 0L) {{ return new RevlResult.Err<>("{_DIV_ZERO_MSG}"); }}',
        '    if (a == Long.MIN_VALUE && b == -1L) { return new RevlResult.Err<>("revl: Int overflow"); }',
        "    return new RevlResult.Ok<>(b > 0L ? Math.floorDiv(a, b)",
        "                                      : -Math.floorDiv(a, -b));",
        "}",
        "private static RevlResult<Long, String> revlCheckedMod(long a, long b) {",
        f'    if (b == 0L) {{ return new RevlResult.Err<>("{_DIV_ZERO_MSG}"); }}',
        "    return new RevlResult.Ok<>(Math.floorMod(a, Math.abs(b)));",
        "}",
        "",
    ]


# A v3 record/variant-case component whose Java type is one of these is a
# PRIMITIVE: `==` on it is already the language's equality (and for `double`,
# already the IEEE one), so the generated `equals` compares it directly instead
# of boxing it through `revlEq`.
_JAVA_PRIMITIVE_COMPONENTS = frozenset({"long", "int", "double", "boolean"})


def _v3_component_eq(jtype: str, left: str, right: str) -> str:
    """One component of a generated `equals`.

    `double` is compared with the PRIMITIVE `==`, never `Double.equals` /
    `Double.compare`: both of those compare `doubleToLongBits`, which makes
    `NaN` equal to itself and `0.0` unequal to `-0.0`, the exact inversion of
    IEEE 754 that item 433 rider R2 fixed at the top level. Everything else is
    a reference and goes through `revlEq`, so a nested record recurses into
    the `equals` generated below it, and a `Float` buried in a `List`/`Map`/
    `Opt` keeps the same IEEE rule."""
    if jtype in _JAVA_PRIMITIVE_COMPONENTS:
        return f"{left} == {right}"
    return f"revlEq({left}, {right})"


def _v3_component_hash(jtype: str, expr: str) -> str:
    """The `hashCode` term matching `_v3_component_eq`'s comparison.

    `Double.hashCode` hashes `doubleToLongBits`, so it gives `0.0` and `-0.0`
    different hashes even though `==` calls them equal; folding every zero to
    `+0.0` first restores `equal implies same hash`. NaN needs no such care:
    all NaNs canonicalise to one bit pattern there, and a NaN component is
    never equal to anything anyway (see `_emit_v3_value_equality`)."""
    if jtype == "double":
        return f"java.lang.Double.hashCode({expr} == 0.0d ? 0.0d : {expr})"
    if jtype == "long":
        return f"java.lang.Long.hashCode({expr})"
    if jtype == "int":
        return f"java.lang.Integer.hashCode({expr})"
    if jtype == "boolean":
        return f"java.lang.Boolean.hashCode({expr})"
    return f"revlHash({expr})"


def _emit_v3_value_equality(cls: str, components: list, *, tag: str = None) -> list[str]:
    """`equals`/`hashCode` for a v3 record class or sealed-variant case class.

    Without these both kinds inherit `Object.equals`, which is REFERENCE
    IDENTITY, so `{ id: 1 } == { id: 1 }` answered `false` on this tier alone
    while python, TypeScript, rust and go all answered `true` against
    docs/syntax-2.0.md §3.4's single structural equality. `revlEq`'s fallback
    is `java.util.Objects.equals`, which is exactly that inherited method, so
    the helper could not fix this on its own: the structure has to exist on
    the class.

    Both classes are `final`, so `instanceof` is an exact type test and the
    relation stays symmetric. The parameter is `__revl_o`, not the canonical
    `o`, because a record is free to declare a field NAMED `o`: every term
    below spells its operands `this.x`/`__revl_other.x` so a plain `o` would
    still compile, and the point is that the `frontend` job has no javac to
    catch the day that stops being true.

    There is NO `if (this == o) { return true; }` fast path, deliberately.
    revl's `==` on `Float` is IEEE (docs/arithmetic.md), so a component that is
    `NaN` is not equal to itself, and the shortcut would make the answer depend
    on whether the two operands happen to be the same reference. rust, the
    precedent tier, derives `PartialEq`, which is a plain field-by-field `==`
    with no such shortcut, and this matches it.

    The consequence, stated plainly and MEASURED on openjdk 26.0.2 rather
    than argued: a value containing a `NaN` is not equal to itself, so it
    breaks `Object.equals`'s REFLEXIVITY requirement and is not a well-behaved
    hash key. `HashMap`/`HashSet` short-circuit on `key == k`, so a lookup
    with the identical reference still finds it while a structurally identical
    one returns null, and `HashSet` will hold two "equal" NaN values at once.
    `ArrayList` has no such short-circuit: `contains`/`indexOf`/`remove` call
    `equals` directly, so a NaN-carrying value is NOT FOUND IN A LIST IT IS
    LITERALLY IN, same reference and all. That is what "matching every other
    tier" costs on a host whose collections assume an equivalence relation,
    and IEEE equality is not one. Every non-NaN value, including a nested
    record and a `-0.0`/`0.0` pair, round-trips through `HashMap` correctly."""
    lines = ["", "    @Override", "    public boolean equals(Object __revl_o) {"]
    if not components:
        lines.append(f"        return __revl_o instanceof {cls};")
    else:
        lines.append(f"        if (!(__revl_o instanceof {cls})) {{ return false; }}")
        lines.append(f"        {cls} __revl_other = ({cls}) __revl_o;")
        terms = [
            _v3_component_eq(jtype, f"this.{field}", f"__revl_other.{field}")
            for jtype, field in components
        ]
        lines.append(f"        return {' && '.join(terms)};")
    lines.append("    }")
    lines.append("")
    lines.append("    @Override")
    lines.append("    public int hashCode() {")
    # A variant case carries its qualified name into the hash so that two cases
    # of one variant with the same payload do not collide; a record has no such
    # sibling and starts from the conventional 1.
    seed = f'"{tag}".hashCode()' if tag else "1"
    if not components:
        lines.append(f"        return {seed};")
    else:
        lines.append(f"        int h = {seed};")
        for jtype, field in components:
            lines.append(
                f"        h = 31 * h + {_v3_component_hash(jtype, 'this.' + field)};")
        lines.append("        return h;")
    lines.append("    }")
    return lines


def _v3_types_need_value_helpers(types: dict) -> bool:
    """True when some generated `equals`/`hashCode` above will CALL `revlEq` /
    `revlHash`, i.e. some record field or variant payload is a reference type.
    A record of nothing but `Int`/`Float`/`Bool` compares and hashes with
    primitive operators and needs neither helper, so it stays byte-identical
    to what a document with no `==` emitted before. Same gating idiom as
    `_uses_equality`: the helper is emitted exactly where it is called."""
    for spec in (types or {}).values():
        if spec.get("kind") == "record":
            for ftype in (spec.get("fields") or {}).values():
                if _java_v3_type(ftype) not in _JAVA_PRIMITIVE_COMPONENTS:
                    return True
        else:
            for case in spec.get("cases") or []:
                payload = case.get("payload")
                if payload is None:
                    continue
                if _java_v3_type(payload) not in _JAVA_PRIMITIVE_COMPONENTS:
                    return True
    return False


def _emit_hash_helper() -> list[str]:
    """`hashCode` for a value compared by `revlEq`.

    `Objects.hashCode` would disagree with `revlEq` in three places, each one a
    broken `HashMap` waiting to happen: `Double.hashCode` separates `0.0` from
    `-0.0` (which `revlEq` calls equal), and `List`/`Map`/`Optional` delegate
    to the ELEMENT's `hashCode`, so a `Float` nested inside one is hashed by
    `doubleToLongBits` again no matter what `revlEq` does with it. This mirrors
    `revlEq` arm for arm, and hashes each container exactly the way its JDK
    class does apart from swapping in this rule for the elements: `List` is
    `AbstractList`'s 31-fold, `Map` is `AbstractMap`'s sum of `key ^ value`
    (keys hash with the HOST `hashCode`, because `revlEq` matches map keys with
    the host `containsKey`), and `Optional` is its element's hash or 0.

    Not a hypothetical. On openjdk 26.0.2, `Objects.hashCode(List.of(-0.0))`
    and `Objects.hashCode(Map.of("k", -0.0))` both differ from their `0.0`
    twins (`Double.hashCode(-0.0)` is `-2147483648`, `Double.hashCode(0.0)` is
    `0`), so a record holding either would be `equals` to its twin and hash
    apart. With this helper the pair hashes alike and a `HashMap` keyed on one
    finds the other, executed in docs/contract-errata.md's table."""
    return [
        "// The hash that agrees with `revlEq`: 0.0 and -0.0 are one key, and",
        "// a Float nested in a List/Map/Opt is hashed under the same rule.",
        "private static int revlHash(Object a) {",
        "    if (a instanceof Double) {",
        "        double d = ((Double) a).doubleValue();",
        "        return java.lang.Double.hashCode(d == 0.0d ? 0.0d : d);",
        "    }",
        "    if (a instanceof java.util.List<?>) {",
        "        int h = 1;",
        "        for (Object x : (java.util.List<?>) a) { h = 31 * h + revlHash(x); }",
        "        return h;",
        "    }",
        "    if (a instanceof java.util.Map<?, ?>) {",
        "        int h = 0;",
        "        for (java.util.Map.Entry<?, ?> e : ((java.util.Map<?, ?>) a).entrySet()) {",
        "            h += java.util.Objects.hashCode(e.getKey()) ^ revlHash(e.getValue());",
        "        }",
        "        return h;",
        "    }",
        "    if (a instanceof java.util.Optional<?>) {",
        "        java.util.Optional<?> xs = (java.util.Optional<?>) a;",
        "        return xs.isEmpty() ? 0 : revlHash(xs.get());",
        "    }",
        "    return java.util.Objects.hashCode(a);",
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
            components = [
                (_java_v3_type(ftype), _ident(field, "record field"))
                for field, ftype in fields.items()
            ]
            lines.extend(_emit_v3_value_equality(name, components))
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
                    lines.extend(
                        "    " + line if line else line
                        for line in _emit_v3_value_equality(
                            cname, [], tag=f"{name}.{cname}")
                    )
                    lines.append("    }")
                else:
                    ptype = _java_v3_type(payload)
                    lines.append(f"    final class {cname} implements {name} {{")
                    lines.append(f"        public final {ptype} value;")
                    lines.append(
                        f"        public {cname}({ptype} value) {{ this.value = value; }}"
                    )
                    lines.extend(
                        "    " + line if line else line
                        for line in _emit_v3_value_equality(
                            cname, [(ptype, "value")], tag=f"{name}.{cname}")
                    )
                    lines.append("    }")
            lines.append("}")
        lines.append("")
    return lines


# item 378 Stage 5: class-level config seam for document-global config externs.
# Mirrors the py tier's `_REVL_EXTERN_CONFIG` map + fail-loud
# `_revl_extern_config` helper: a mutable static config map, keyed by extern
# name, that a composition driver fills at plug time, and a lookup that THROWS,
# naming the extern, when a required (non-defaulted) field is absent, instead
# of handing the body a null that fails opaquely later. A defaults-only extern
# still resolves to its defaults driver-free. Fully-qualified `java.util.*` so
# the seam adds no import; open-coded joins so it needs no `String.join`.
# Emitted only when a config extern is present, so a no-config program is
# byte-identical.
_JAVA_EXTERN_CONFIG_SCAFFOLD = [
    "static final java.util.Map<String, java.util.Map<String, Object>> "
    "_REVL_EXTERN_CONFIG = new java.util.HashMap<>();",
    "",
    "static java.util.Map<String, Object> _revlExternConfig(",
    "        String name, String[] required, "
    "java.util.Map<String, Object> defaults) {",
    "    java.util.Map<String, Object> out = new java.util.HashMap<>(defaults);",
    "    java.util.Map<String, Object> cfg = _REVL_EXTERN_CONFIG.get(name);",
    "    if (cfg == null) {",
    "        if (required.length > 0) {",
    # item 433 F10: this built the field list with `msg += \", \"` in a loop,
    # justified in a comment by "avoids a dependency on String.join" — which is
    # a method on java.lang.String and needs no import at all. This arm runs
    # immediately before a throw, so the win is nil and the point is that the
    # emitter chose the worse shape for a reason that does not hold. (The
    # `missing` loop below KEEPS its `+=`: unlike this one it runs on every
    # call, and its `+=` executes only for a field that is actually absent, so
    # the happy path allocates nothing. Rewriting it around an ArrayList would
    # allocate one per call and make the hot path worse.)
    "            String msg = String.join(\", \", required);",
    "            throw new RuntimeException(\"config extern `\" + name +",
    "                \"` called before plug-time configuration was installed "
    "(required config: \" +",
    "                msg + \"); configure it through the run driver's config "
    "seam\");",
    "        }",
    "        return out;",
    "    }",
    "    String missing = \"\";",
    "    int n = 0;",
    "    for (String f : required) {",
    "        if (!cfg.containsKey(f)) {",
    "            if (n > 0) missing += \", \";",
    "            missing += f;",
    "            n++;",
    "        }",
    "    }",
    "    if (n > 0) {",
    "        throw new RuntimeException(\"config extern `\" + name +",
    "            \"` called before plug-time configuration was installed "
    "(missing required config: \" +",
    "            missing + \")\");",
    "    }",
    "    out.putAll(cfg);",
    "    return out;",
    "}",
    "",
]


def _java_extern_config_schema(ext: dict) -> tuple[list[str], dict] | None:
    """`(required field names, defaults)` for a config extern, or None."""
    schema = ext.get("config")
    if not schema:
        return None
    required = [f["name"] for f in schema if f.get("default") is None]
    defaults = {f["name"]: f["default"] for f in schema
                if f.get("default") is not None}
    return required, defaults


def _java_extern_config_constants(externs: list) -> list[str]:
    """item 433 F2: the required-field array and the defaults map of every
    config extern, hoisted to `private static final` fields.

    Both are fully determined at emit time. They used to be rebuilt inside the
    call, so `_revlExternConfig("author", new String[]{..}, Map.of(..))`
    allocated the `String[]`, the `ImmutableCollections.MapN` and its backing
    `Object[]` on EVERY call, on top of the `HashMap` copy the resolved map
    genuinely needs. Hoisting removes those three per call and changes nothing
    about WHEN the config store is read, since `_revlExternConfig` still runs
    per call and still reads `_REVL_EXTERN_CONFIG` there."""
    lines: list[str] = []
    for ext in externs:
        schema = _java_extern_config_schema(ext)
        if schema is None:
            continue
        required, defaults = schema
        suffix = _fn_name(ext.get("name"))
        req_lit = "new String[]{%s}" % ", ".join(_string(f) for f in required)
        if defaults:
            pairs = ", ".join(f"{_string(k)}, {_lit(v)}" for k, v in defaults.items())
            def_lit = f"java.util.Map.<String, Object>of({pairs})"
        else:
            def_lit = "java.util.Map.<String, Object>of()"
        lines.append(f"private static final String[] _REVL_CFG_REQUIRED_{suffix} = {req_lit};")
        lines.append(f"private static final java.util.Map<String, Object> "
                     f"_REVL_CFG_DEFAULTS_{suffix} = {def_lit};")
    if lines:
        lines.append("")
    return lines


def _java_extern_config_bind(ext: dict) -> str:
    """The `_revl_config = ...` first-body line for a config extern, or None.
    `_revl_config` is a `java.util.Map<String, Object>`; the verbatim @java body
    reads a field as `(Cast) _revl_config.get("field")`, exactly as the py body
    reads the resolved dict. The schema arguments are the hoisted constants
    `_java_extern_config_constants` emits (item 433 F2)."""
    if _java_extern_config_schema(ext) is None:
        return None
    name = ext.get("name")
    suffix = _fn_name(name)
    return (f"java.util.Map<String, Object> _revl_config = _revlExternConfig("
            f"{_string(name)}, _REVL_CFG_REQUIRED_{suffix}, "
            f"_REVL_CFG_DEFAULTS_{suffix});")


def _emit_v3_externs(externs: list) -> list[str]:
    lines: list[str] = []
    # item 378 Stage 5: emit the config seam once, before the externs, when any
    # extern carries a config schema (byte-identical when none do).
    if any(ext.get("config") for ext in externs):
        lines.extend(_JAVA_EXTERN_CONFIG_SCAFFOLD)
        lines.extend(_java_extern_config_constants(externs))
    for ext in externs:
        name = _fn_name(ext.get("name"))
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
        # item 378 Stage 5: a config extern binds `_revl_config` as the first
        # body line; None for a no-config extern (byte-identical body splice).
        config_bind = _java_extern_config_bind(ext)
        if config_bind:
            lines.append("    " + config_bind)
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
        name = _fn_name(fn.get("name"))
        params = ", ".join(
            f"{_java_v3_type(p.get('type'))} {_ident(p.get('name'), 'parameter name')}"
            for p in fn.get("params") or []
        )
        ret = _java_v3_type(fn.get("returns"))
        ctx.arrows = {}  # arrow bindings are local to one body
        lines.append(f"public static {ret} {name}({params}) {{")
        if not fn.get("body"):
            lines.append("    // (empty body)")
        else:
            for stmt in fn["body"]:
                _v3_stmt(stmt, ctx, lines, 1, test_mode=False)
        lines.append("}")
        lines.append("")
    return lines


def _emit_v3_tests(tests: list, types: dict, functions: list, externs: list,
                   extra_runners: list[str] | None = None) -> list[str]:
    """The document's pure `test` blocks as static methods, plus the
    `REVL_TESTS` roster the JVM runner walks.

    *extra_runners* are already-emitted method names (the lifecycle tests, see
    `_emit_v3_lifecycle_tests`) that join the same roster: one list, so a
    document's lifecycle tests run through exactly the channel its plain tests
    run through and neither can be silently dropped."""
    ctx = _V3Ctx(types, functions, externs)
    lines: list[str] = []
    used: set[str] = set()
    test_names: list[str] = []
    for index, test in enumerate(tests):
        mname = _java_test_method_name(test.get("name"), index, used)
        test_names.append(mname)
        ctx.arrows = {}  # arrow bindings are local to one body
        lines.append(f"public static void {mname}() {{")
        for stmt in test.get("body") or []:
            _v3_stmt(stmt, ctx, lines, 1, test_mode=True)
        lines.append("}")
        lines.append("")
    test_names.extend(extra_runners or [])
    if test_names:
        runners = ", ".join(f"Components::{name}" for name in test_names)
        lines.append(
            f"public static final java.util.List<Runnable> REVL_TESTS = "
            f"java.util.List.of({runners});"
        )
        lines.append("")
    return lines


def _java_lifecycle_method_name(name: object, used: set[str]) -> str:
    """A distinct Java method name for a lifecycle test.

    The `lifecycle` prefix is what keeps it out of `_java_test_method_name`'s
    `test`-prefixed namespace, so a plain `test "x"` and a `lifecycle test "x"`
    in one document never collide.
    """
    raw = name if isinstance(name, str) else str(name)
    base = "lifecycle"
    for part in re.split(r"[^A-Za-z0-9_]+", raw):
        if part:
            base += part[:1].upper() + part[1:]
    candidate, index = base, 1
    while candidate in used:
        candidate = f"{base}_{index}"
        index += 1
    used.add(candidate)
    return candidate


def _emit_lifecycle_no_residue_helper(provisions: str) -> list[str]:
    """The R4 + R1 proof a lifecycle `assert no_residue` runs.

    R4 is read through the *public* Context API, exactly as
    backends/java/placement/RunOnce.java reads it after its LIFO teardown: a
    provided key resolves with `ctx.get(iface)` while its provider is up, and
    once every provide-disposable has run the same `get` must fail. Reading it
    that way (rather than reaching into runtime internals) is what makes the
    check mean the same thing against the in-repo cordis4j stubs and against a
    compiled cordis4j-core (`REVL_CORDIS4J_CLASSES`).

    R1 is the host-resource counter: `undo` that is not the inverse of its
    acquisition (a Pool opened and never closed) leaves it above the baseline
    this test started from. The baseline is per-test, so one test's leak is
    reported against that test and does not smear onto the next one.
    """
    return [
        "// R4 (no provision still resolves) + R1 (every acquired host resource",
        "// was released) — the proof behind a lifecycle `assert no_residue`",
        "// (docs/syntax-2.0.md §7.1, docs/backend-ir.md §Required semantics).",
        "static void revlLifecycleNoResidue(String where, Context root,",
        "        java.util.Map<String, Disposable> loaded, long r1Base,",
        "        Class<?>... provisions) {",
        "    if (!loaded.isEmpty()) {",
        "        throw new AssertionError(where + \": residue — still loaded: \"",
        "            + loaded.keySet() + \" (R4)\");",
        "    }",
        "    for (Class<?> iface : provisions) {",
        "        boolean live;",
        "        try {",
        "            root.get(iface);",
        "            live = true;",
        "        } catch (RuntimeException withdrawn) {",
        "            live = false; // good: nothing answers for this key any more",
        "        }",
        "        if (live) {",
        "            throw new AssertionError(where + \": residue — \"",
        "                + iface.getSimpleName() + \" still resolves after teardown (R4)\");",
        "        }",
        "    }",
        "    long leaked = REVL_LIVE_HOST_RESOURCES.get() - r1Base;",
        "    if (leaked != 0L) {",
        "        throw new AssertionError(where + \": residue — \" + leaked",
        "            + \" host resource(s) never released (R1)\");",
        "    }",
        "}",
        "",
        "// R1 live-resource counter: every host object acquired must be released",
        "// by its `undo`, or the lifecycle `assert no_residue` above fails.",
        "public static final java.util.concurrent.atomic.AtomicLong",
        "    REVL_LIVE_HOST_RESOURCES = new java.util.concurrent.atomic.AtomicLong();",
        "",
        "// Every service this document provides: the keys `assert no_residue`",
        "// requires nothing to answer for once the composition is torn down.",
        "static final Class<?>[] REVL_LIFECYCLE_PROVISIONS =",
        f"    new Class<?>[] {{{provisions}}};",
        "",
    ]


def _emit_v3_lifecycle_tests(tests: list, types: dict, functions: list,
                             externs: list, services: dict,
                             components: list) -> tuple[list[str], list[str]]:
    """`lifecycle test` blocks (syntax-2.0 §7.1) as static methods driving a
    live cordis4j composition (item 178(b); FR-5's java half).

    A lifecycle test is not a pure test unit: it loads components into a live
    context, calls through provision keys, unloads them, and asserts
    residue-freedom by reading the host runtime back. That is exactly the
    round-trip `revl run --backend java --once` already drives
    (backends/java/placement/RunOnce.java); this lowers the same round-trip
    into the tier's own test idiom, so `revl test --backend java` runs it
    beside the document's plain tests.

    Load is `root.plugin(new <Comp>Plugin(<config>))` and unload is the
    Disposable it returns — the real cordis4j load/unload pair the java
    scenarios drive (backends/java/scenarios/RunRealScenarios.java) — and the
    root comes from `Contexts.create()`, so one emitted source runs on the
    in-repo stubs and on a compiled cordis4j-core alike.

    Returns ``(lines, method names)``; the names join `REVL_TESTS`.
    """
    if not services:
        raise EmitError(
            "a lifecycle test loads components and calls through provision "
            "keys, so it needs at least one service in the document to drive; "
            "this document declares none"
        )
    provided: dict[str, str] = {}
    for component in components:
        for key, service in (component.get("provides") or {}).items():
            provided[key] = service
    method_tables = {sname: (svc.get("methods") or {})
                     for sname, svc in services.items()}
    templates = {comp.get("name"): comp for comp in components}
    provisions = ", ".join(
        f"{_ident(sname, 'service')}.class" for sname in sorted(set(provided.values())))

    ctx = _V3Ctx(types, functions, externs, components)
    out = _emit_lifecycle_no_residue_helper(provisions)
    names: list[str] = []
    used: set[str] = set()
    for test in tests:
        mname = _java_lifecycle_method_name(test.get("name"), used)
        names.append(mname)
        ctx.arrows = {}  # arrow bindings are local to one body
        # RAW (not run through `_string`): every use below feeds it into its own
        # single `_string(...)` call, the one place allowed to escape it.
        where = f'lifecycle test "{test.get("name")}"'
        out.append(f"public static void {mname}() {{")
        out.append("    // drives the composition on a live cordis4j context and")
        out.append("    // proves no residue after LIFO teardown (FR-5 / §7.1).")
        out.append("    final Context _revlRoot = Contexts.create();")
        out.append("    final java.util.Map<String, Disposable> _revlLoaded =")
        out.append("        new java.util.LinkedHashMap<>();")
        out.append("    final long _revlR1Base = REVL_LIVE_HOST_RESOURCES.get();")
        for step in test.get("body") or []:
            out.extend(_v3_lifecycle_step(
                step, ctx, where, provided, method_tables, templates))
        out.append("}")
        out.append("")
    return out, names


def _v3_lifecycle_step(step: dict, ctx: _V3Ctx, where: str,
                       provided: dict, method_tables: dict,
                       templates: dict) -> list[str]:
    """One lowered lifecycle step. A step this tier cannot express is refused
    with the tier's standing "not lowerable" wording, which `revl test` reads
    back as a skip-with-reason rather than a tier failure — never a false pass.
    """
    kind = step.get("step")
    if kind == "load":
        name = step["component"]
        template = templates.get(name)
        if template is None:  # pragma: no cover — the lowerer rejects it
            raise EmitError(f"{where}: no component named {name!r}")
        cname = _ident(name, "component")
        supplied = step.get("config") or {}
        # Config values in the component's DECLARED order — the order its
        # plugin constructor takes them (`_emit_plugin_ctors`); an omitted
        # field (only possible when it has a default) takes that default.
        args = []
        for field in template.get("config") or []:
            fname = field.get("name")
            if fname in supplied:
                args.append(_expr(supplied[fname], ctx))
            else:
                args.append(_config_default_lit(field, _java_v3_type))
        return [f"    _revlLoaded.put({_string(name)}, _revlRoot.plugin("
                f"new {cname}Plugin({', '.join(args)})));"]
    if kind == "unload":
        name = step["component"]
        return [
            "    {",
            f"        Disposable _revlFiber = _revlLoaded.remove({_string(name)});",
            "        if (_revlFiber != null) {",
            "            _revlFiber.dispose();",
            "        }",
            "    }",
        ]
    if kind == "call":
        key = step["key"]
        service = provided.get(key)
        if service is None:  # pragma: no cover — the lowerer rejects it
            raise EmitError(f"{where}: no provider for key {key!r}")
        method = (method_tables.get(service) or {}).get(step["method"])
        if method is None:  # pragma: no cover — the lowerer rejects it
            raise EmitError(f"{where}: unknown method {step['method']!r}")
        args = ", ".join(_expr(arg, ctx) for arg in step.get("args") or [])
        # `get` throws when the key is not ACTIVE (R2) — the resolution IS the
        # liveness check, the same read RunOnce's UP proof performs.
        call = (f"_revlRoot.get({_ident(service, 'service')}.class)"
                f".{_ident(step['method'], 'method')}({args})")
        bind = step.get("bind")
        if bind is None:
            return [f"    {call};"]
        return [f"    final var {_ident(bind, 'lifecycle binding')} = {call};"]
    if kind == "assert":
        return [f"    if (!({_expr(step['expr'], ctx)})) {{",
                f"        throw new AssertionError({_string(where + ': assertion failed')});",
                "    }"]
    if kind == "assert_no_residue":
        return [f"    revlLifecycleNoResidue({_string(where)}, _revlRoot, _revlLoaded,",
                "        _revlR1Base, REVL_LIFECYCLE_PROVISIONS);"]
    if kind == "advance":
        # timers (`every`/`after`, item 57) are not lowerable on this tier, so
        # neither is driving the clock coeffect forward
        # (docs/time-coeffect.md) — the same follow-on `revl test`'s timer gate
        # reports for a component that arms one.
        raise EmitError(
            f"{where}: an `advance` step is not lowerable on the {CRATE} tier: "
            f"it drives the clock coeffect, and timers (`every`/`after`) do not "
            f"lower here yet — run it with `revl test --backend py` "
            f"(docs/time-coeffect.md)")
    raise EmitError(  # pragma: no cover — the lowerer emits nothing else
        f"{where}: unknown lifecycle step {kind!r}")


class _Env:
    def __init__(self, component: dict, services: dict):
        self.component = component
        self.services = services
        self.name = component["name"]
        self.reqs: dict[str, str] = dict(component.get("requires") or {})
        self.provides: dict[str, str] = dict(component.get("provides") or {})
        # item 173: routed requires (item 162 `routes` IR): key -> {"realms":
        # [...], "strategy": ...}. A routed key resolves per named realm through
        # an emitted router class, never a single `ctx.get(...)` handle. Empty
        # for every routes-less component.
        self.routes: dict[str, dict] = dict(component.get("routes") or {})


def _format_java(template: str, args: list[str]) -> str:
    """A `format` node (a `${..}` template literal in component position) as a
    Java concatenation chain.

    item 433 F1. This used to render `String.format("[req] %s #%s end", msg, n)`.
    The only conversion this emitter ever produces is `%s`, which is defined as
    `String.valueOf` for every non-`Formattable` argument, and no revl value is
    `Formattable` — so the concatenation is output-identical, including for
    null, and it is what a Java developer writes. MEASURED on openjdk 26.0.2
    over `bench/codegen/java/cases/interp_format`: `String.format` allocates the
    varargs `Object[]`, boxes each primitive, builds a `Formatter` and its
    `StringBuilder` and re-parses the format string into a fresh
    `FormatSpecifier` list on EVERY call; the concatenation compiles to one
    `invokedynamic makeConcatWithConstants` whose linked handle writes the
    primitive straight into the result buffer.

    The chain always opens on a string literal (`""` when the template starts
    with a placeholder), so `+` can never be read as arithmetic. `$$` is a
    literal `$` (A4); `%` needs no escaping any more, which is the
    `UnknownFormatConversionException` hazard on SQL LIKE patterns gone too.
    """
    pieces: list[str] = []          # alternating: literal text, arg index, ...
    i, buf = 0, []
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
                pieces.append(("text", "".join(buf)))
                pieces.append(("arg", int(template[i + 1 : j])))
                buf = []
                i = j
                continue
        buf.append(ch)
        i += 1
    pieces.append(("text", "".join(buf)))

    # Collapse the alternation into a concatenation, dropping empty literal
    # runs except the leading one, which anchors the chain in String position.
    segs: list[str] = []
    for index, (kind, value) in enumerate(pieces):
        if kind == "arg":
            segs.append(args[value] if value < len(args) else _string(""))
        elif value or index == 0:
            segs.append(_string(value))
    return " + ".join(segs)


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
            if method.get("idempotent"):
                # delivery semantics (item 44): safe to re-deliver, so the
                # runtime may auto-retry a transient failure of this emission
                out.append("    /** idempotent: the runtime may auto-retry a transient failure. */")
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
            # v3 top-level functions never lower host calls to a `host` node —
            # `Pool.open(..)`/`Map.new()`/`Job.run(..)` stay a `field` access on
            # a `var` named after the host root (see `_v3_call`). Without this
            # the runtime class was omitted and the emitted method referenced a
            # nonexistent `Pool`, so a host call outside a component body never
            # compiled even after the call itself lowered correctly.
            if node.get("kind") == "field":
                target = node.get("target")
                if (isinstance(target, dict) and target.get("kind") == "var"
                        and target.get("name") in _HOST_ROOTS):
                    used.add(target.get("name"))
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


def _r1_lines(lines: list[str]) -> list[str]:
    """*lines* when the document carries lifecycle tests, nothing otherwise.

    The R1 live-resource accounting exists to answer `assert no_residue`; a
    document without a lifecycle test never reads the counter, so keeping the
    hooks out of its emission leaves every existing golden byte-identical.
    """
    return list(lines) if _LIFECYCLE_MODE else []


def _emit_map_runtime() -> list[str]:
    # FR-4 (FEATURE-REQUESTS.md FR-4 / docs/v2.0-roadmap.md item 77(c)): the
    # host Map is generic over its value type, `V` learned per site from the
    # IR's `insert` calls (the session ledger is `Map[Str, List[Msg]]`). Every
    # site that binds a host Map pins the type at its declaration
    # (`Map<...> store = Map.create();`), so javac never has to infer `V`
    # from a raw `Map.create()`.
    return [
        "// host object runtime (Map) — minimal working Java implementation.",
        "// The value type is generic — each site's `Map.create()` pins `V`",
        "// (FR-4: `Map[Str, List[Msg]]` and friends, not just String).",
        "public static final class Map<V> {",
        "    // item 397: a ConcurrentHashMap, not a bare HashMap. The tier's",
        "    // placement runner serves each bridge connection on its own thread,",
        "    // so the former unsynchronized HashMap was not memory-safe under",
        "    // concurrent put; migrating the backing class makes insert/remove/get",
        "    // thread-safe AND gives insert_if_absent an atomic putIfAbsent.",
        "    private final java.util.concurrent.ConcurrentHashMap<String, V> values =",
        "        new java.util.concurrent.ConcurrentHashMap<>();",
        "    private Map() {}",
    ] + _r1_lines([
        "    // R1 (item 178(b)): a Map is a live host resource until its `undo`",
        "    // drops it. `dropped` makes the release idempotent, so a double",
        "    // teardown cannot drive the counter negative and hide a real leak.",
        "    private boolean dropped = false;",
    ]) + [
        "    // revl `Map.new()` — renamed: `new` is a Java reserved word.",
        "    public static <V> Map<V> create() {",
    ] + _r1_lines([
        "        REVL_LIVE_HOST_RESOURCES.incrementAndGet();",
    ]) + [
        "        return new Map<>();",
        "    }",
        "    public void drop() {",
        "        values.clear();",
    ] + _r1_lines([
        "        if (!dropped) {",
        "            dropped = true;",
        "            REVL_LIVE_HOST_RESOURCES.decrementAndGet();",
        "        }",
    ]) + [
        "    }",
        "    public void insert(String key, V value) {",
        "        values.put(key, value);",
        "    }",
        "    // The atomic compare-and-set (item 397): ConcurrentHashMap.putIfAbsent",
        "    // is a single atomic operation over the test AND the insert. It returns",
        "    // the previous value (null when absent), so a null return means this",
        "    // call inserted. Under N concurrent callers, exactly one sees null.",
        "    // (ConcurrentHashMap forbids null values; revl host inserts never pass",
        "    // null, and `get` already wraps absence in Optional.)",
        "    public boolean insert_if_absent(String key, V value) {",
        "        return values.putIfAbsent(key, value) == null;",
        "    }",
        "    public void remove(String key) {",
        "        values.remove(key);",
        "    }",
        "    public java.util.Optional<V> get(String key) {",
        "        return java.util.Optional.ofNullable(values.get(key));",
        "    }",
        "    // Iteration surface (docs/stdlib-2.0.md §Map): the checker promises",
        "    // `size()`/`keys()` on a host `Map.new()` receiver, and emit lowers",
        "    // both as method calls on this object. `size` is the entry count as a",
        "    // long (revl Int); `keys` yields the keys in ascending canonical Str",
        "    // (code-point) order — String.compareTo is UTF-16 code-unit order, so",
        "    // the inline comparator walks code points to stay canonical past",
        "    // U+FFFF. Read-only queries, no host trace.",
        "    public long size() {",
        "        return values.size();",
        "    }",
        "    public java.util.List<String> keys() {",
        "        java.util.List<String> ks = new java.util.ArrayList<>(values.keySet());",
        "        ks.sort((a, b) -> {",
        "            int i = 0, j = 0;",
        "            while (i < a.length() && j < b.length()) {",
        "                int ca = a.codePointAt(i), cb = b.codePointAt(j);",
        "                if (ca != cb) { return Integer.compare(ca, cb); }",
        "                i += Character.charCount(ca); j += Character.charCount(cb);",
        "            }",
        "            return Boolean.compare(i >= a.length(), j >= b.length());",
        "        });",
        "        return java.util.List.copyOf(ks);",
        "    }",
        "}",
        "",
    ]


def _emit_pool_runtime() -> list[str]:
    """A bounded connection pool over a deterministic in-memory database.

    Semantics are normative and shared across tiers — see
    ``backends/python/runtime.py``, section ``.. _pool-job-semantics:``:
    ``size`` connections numbered 1..size, acquire/release accounting,
    statements borrow a connection for their duration, ``close`` releases what
    is still checked out, and exhaustion / use-after-close raise.
    """
    return [
        "// host object runtime (Pool) — a bounded connection pool over a",
        "// deterministic in-memory database (no driver dependency).  Semantics",
        "// are shared across tiers: backends/python/runtime.py, section",
        "// `.. _pool-job-semantics:`.",
        "public static final class Pool {",
        "    private final String url;",
        "    private final long poolSize;",
        "    private final java.util.ArrayList<Long> idle = new java.util.ArrayList<>();",
        "    private final java.util.ArrayList<Long> checkedOut = new java.util.ArrayList<>();",
        "    private boolean closed = false;",
        "    private Pool(String url, long poolSize) {",
        "        this.url = url;",
        "        this.poolSize = poolSize;",
        "        for (long i = 1L; i <= poolSize; i++) {",
        "            idle.add(i);",
        "        }",
        "    }",
        "    public static Pool open(String url, long poolSize) {",
        "        if (poolSize < 1L) {",
        "            throw new IllegalStateException(",
        "                \"pool size must be an integer >= 1 (got \" + poolSize + \")\");",
        "        }",
    ] + _r1_lines([
        "        // R1 (item 178(b)): an open Pool is a live host resource until",
        "        // `close` returns it. A `undo` that is not the inverse of the",
        "        // acquisition leaves this counter above zero, which is exactly",
        "        // what a lifecycle `assert no_residue` catches.",
        "        REVL_LIVE_HOST_RESOURCES.incrementAndGet();",
    ]) + [
        "        return new Pool(url, poolSize);",
        "    }",
        "    public String url() {",
        "        return url;",
        "    }",
        "    public long capacity() {",
        "        if (closed) {",
        "            return 0L;",
        "        }",
        "        return poolSize;",
        "    }",
        "    public long inUse() {",
        "        return checkedOut.size();",
        "    }",
        "    public long available() {",
        "        return idle.size();",
        "    }",
        "    // Statements borrow silently; only an explicit acquire/release is a",
        "    // traced operation on the tiers that trace.",
        "    private long borrow(String op) {",
        "        if (closed) {",
        "            throw new IllegalStateException(",
        "                \"pool.\" + op + \" after close/drop — use-after-free\");",
        "        }",
        "        if (idle.isEmpty()) {",
        "            throw new IllegalStateException(",
        "                \"pool.\" + op + \" exhausted (size=\" + poolSize",
        "                    + \", in_use=\" + checkedOut.size() + \")\");",
        "        }",
        "        long conn = idle.remove(0);",
        "        checkedOut.add(conn);",
        "        return conn;",
        "    }",
        "    private void giveBack(long conn) {",
        "        checkedOut.remove(Long.valueOf(conn));",
        "        idle.add(conn);",
        "        java.util.Collections.sort(idle);",
        "    }",
        "    public long acquire() {",
        "        return borrow(\"acquire\");",
        "    }",
        "    public void release(long conn) {",
        "        if (closed) {",
        "            throw new IllegalStateException(",
        "                \"pool.release after close/drop — use-after-free\");",
        "        }",
        "        if (!checkedOut.contains(Long.valueOf(conn))) {",
        "            throw new IllegalStateException(",
        "                \"pool.release conn=\" + conn + \" is not checked out\");",
        "        }",
        "        giveBack(conn);",
        "    }",
        "    public void close() {",
        "        if (closed) {",
        "            throw new IllegalStateException(",
        "                \"pool.close after close/drop — use-after-free\");",
        "        }",
        "        checkedOut.clear();",
        "        idle.clear();",
        "        closed = true;",
    ] + _r1_lines([
        "        REVL_LIVE_HOST_RESOURCES.decrementAndGet();",
    ]) + [
        "    }",
        "    public java.util.List<Object> query(String sql) {",
        "        long conn = borrow(\"query\");",
        "        giveBack(conn);",
        "        return java.util.List.of();",
        "    }",
        "    public long execute(String sql) {",
        "        long conn = borrow(\"execute\");",
        "        giveBack(conn);",
        "        return 1L;",
        "    }",
        "}",
        "",
    ]


def _emit_job_runtime() -> list[str]:
    """A cancellable asynchronous unit of work.

    Semantics are normative and shared across tiers — see
    ``backends/python/runtime.py``, section ``.. _pool-job-semantics:``:
    pending -> done after exactly ``TICKS`` cooperative scheduler turns, or
    pending -> cancelled, after which awaiting raises.  No timers, so the
    behaviour is deterministic under test.

    `Job.run(name)` hands back a *handle*, per the v1 contract
    (docs/backend-ir-v1.md, "Host builtins"): an `await` step is only
    meaningful if there is something to join. The old shape returned
    `void`, so the emitted `Job.run("x");` could not be joined even in
    principle and every awaited job stayed in flight past activation.
    `pending()` makes that residue countable.
    """
    return [
        "// host object runtime (Job) — a cancellable asynchronous unit of work.",
        "// Semantics are shared across tiers: backends/python/runtime.py,",
        "// section `.. _pool-job-semantics:`. `Job.run(name) -> handle`;",
        "// an `await` step joins it, so activation leaves nothing pending.",
        "public static final class Job {",
        "    /** scheduler turns of simulated work (same number on every tier) */",
        "    public static final int TICKS = 5;",
        "    private static final java.util.List<Job> HANDLES =",
        "        java.util.Collections.synchronizedList(new java.util.ArrayList<>());",
        "    private final String name;",
        "    private volatile String status = \"pending\";",
        "    private int remaining = TICKS;",
        "    private Job(String name) {",
        "        this.name = name;",
        "    }",
        "    public static Job run(String name) {",
        "        Job job = new Job(name);",
        "        HANDLES.add(job);",
        "        return job;",
        "    }",
        "    public String name() {",
        "        return name;",
        "    }",
        "    public String state() {",
        "        return status;",
        "    }",
        "    /** pending -> cancelled (true); a no-op returning false otherwise. */",
        "    public synchronized boolean cancel() {",
        "        if (!\"pending\".equals(status)) {",
        "            return false;",
        "        }",
        "        status = \"cancelled\";",
        "        return true;",
        "    }",
        "    /** Drive the job: TICKS cooperative turns, then done. */",
        "    public String await() {",
        "        if (\"done\".equals(status)) {",
        "            return name;",
        "        }",
        "        if (\"cancelled\".equals(status)) {",
        "            throw new IllegalStateException(\"job \\\"\" + name + \"\\\" cancelled\");",
        "        }",
        "        while (remaining > 0) {",
        "            Thread.yield();",
        "            if (\"cancelled\".equals(status)) {",
        "                throw new IllegalStateException(\"job \\\"\" + name + \"\\\" cancelled\");",
        "            }",
        "            remaining--;",
        "        }",
        "        status = \"done\";",
        "        return name;",
        "    }",
        "    /** The same job as a future, for a host that wants one. */",
        "    public java.util.concurrent.CompletableFuture<String> toFuture() {",
        "        return java.util.concurrent.CompletableFuture.supplyAsync(this::await);",
        "    }",
        "    /** Handles still in flight — teardown residue, made countable. */",
        "    public static long pending() {",
        "        synchronized (HANDLES) {",
        "            long n = 0L;",
        "            for (Job job : HANDLES) {",
        "                if (\"pending\".equals(job.state())) {",
        "                    n++;",
        "                }",
        "            }",
        "            return n;",
        "        }",
        "    }",
        "    public static void reset() {",
        "        HANDLES.clear();",
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
            # A `spawn` acquisition binds a live-instance handle, not a host
            # resource: its type is the emitted `RevlSpawnHandle`, so a
            # provide-method that captured it can call `.dispose()`.
            if acquire.get("kind") == "spawn":
                return "RevlSpawnHandle"
            return (acquire.get("fn") or "").split(".")[0] or "Object"
    return "Object"


# ---------------------------------------------------------------------------
# FR-4 (FEATURE-REQUESTS.md FR-4 / docs/v2.0-roadmap.md item 77(c)): the host
# Map's value type, learned from the IR.  The frontend types a Map by its
# value parameter — every `store.insert(k, v)` names the map's value type — so
# the emitter carries it into the generic `Map<V>` it emits: provider-struct
# fields become `Map<java.util.List<Msg>>` and the constructor is pinned
# `Map<java.util.List<Msg>> store = Map.create();`.  This is a *tiny* oracle
# for the shapes an `insert` value takes in practice (a parameter, a literal,
# a list, a stdlib result like `push`, a free-fn or required-service call, a
# record literal, an ADT case, a record-field access, a config field).
# Anything it cannot prove stays `None` and the emitter falls back to `Str` —
# the historical surface — so String-valued maps keep emitting byte-identically.
# ---------------------------------------------------------------------------


def _map_value_expr_type(node: object, var_types: dict, env: _Env,
                         functions: list, v3_ctx: _V3Ctx | None) -> str | None:
    """Best-effort *surface* type of an expression for Map value inference."""
    if not isinstance(node, dict):
        return None
    kind = node.get("kind")
    if kind in ("name", "var"):
        return var_types.get(node.get("id") or node.get("name"))
    if kind == "lit":
        value = node.get("value")
        if isinstance(value, bool):
            return "Bool"
        if isinstance(value, int):
            return "Int"
        if isinstance(value, float):
            return "Float"
        if isinstance(value, str):
            return "Str"
        return None
    if kind in ("format", "interp"):
        return "Str"
    if kind == "list":
        items = node.get("items") or []
        if not items:
            return None  # an untyped `[]` pins nothing
        first = _map_value_expr_type(items[0], var_types, env, functions, v3_ctx)
        if first is None:
            return None
        return f"List[{first}]"
    if kind == "builtin":
        method = node.get("method")
        args = node.get("args") or []
        # `push` returns the receiver list extended by its argument, so the
        # result's element type is the argument's type even when the receiver
        # (e.g. a `store.get(k) ?? []` binding) has no known surface type.
        if method == "push" and args:
            elem = _map_value_expr_type(args[0], var_types, env, functions, v3_ctx)
            if elem is not None:
                return f"List[{elem}]"
        if method in ("length", "charCodeAt", "codepoint_at", "indexOf",
                      "to_int"):
            return "Int"
        if method in ("charAt", "join", "repeat", "to_str", "slice"):
            return "Str"
        if method == "split":
            return "List[Str]"
        if method == "concat":
            return _map_value_expr_type(node.get("target"), var_types, env, functions, v3_ctx)
        return None
    if kind == "fn":
        # free-function call: the declared return type
        name = node.get("name")
        for fn in functions or []:
            if fn.get("name") == name:
                return fn.get("returns")
        return None
    if kind == "call":
        # v1-style call: `{target: ..., method: ...}`. A required-service call
        # resolves through the component's requires + the service declaration.
        target = node.get("target") or {}
        if target.get("kind") == "req":
            service_name = (env.component.get("requires") or {}).get(target.get("name"))
            if service_name is not None:
                service = env.services.get(service_name)
                if service is not None:
                    decl = ((service.get("methods") or {})
                            .get(node.get("method") or "") or {})
                    return decl.get("returns")
        # v3-style call: `{callee: {kind: "field", target: ..., name: ...}}`.
        callee = node.get("callee")
        if isinstance(callee, dict) and callee.get("kind") == "field":
            ct = callee.get("target") or {}
            if ct.get("kind") == "req":
                service_name = (env.component.get("requires") or {}).get(ct.get("name"))
                if service_name is not None:
                    service = env.services.get(service_name)
                    if service is not None:
                        decl = ((service.get("methods") or {})
                                .get(callee.get("name") or "") or {})
                        return decl.get("returns")
        return None
    if kind == "record":
        if v3_ctx is None:
            return None
        try:
            return v3_ctx.record_type_for_fields(
                [k for k, _ in node.get("fields") or []])
        except EmitError:
            return None
    if kind == "adt":
        # lowered ADT construction carries its type on the node
        return node.get("type")
    if kind == "field":
        # a record-field access (`msg.content`): resolve through the declared
        # record type of the receiver
        if v3_ctx is None:
            return None
        receiver = _map_value_expr_type(node.get("target"), var_types, env, functions, v3_ctx)
        spec = v3_ctx.types.get(receiver) if receiver is not None else None
        if not (isinstance(spec, dict) and spec.get("kind") == "record"):
            return None
        return (spec.get("fields") or {}).get(node.get("name"))
    if kind == "config":
        fname = node.get("field")
        for f in env.component.get("config") or []:
            if f.get("name") == fname:
                return f.get("type")
        return None
    if kind == "bin" and node.get("op") == "??":
        # `a ?? b` with an unknown left (a host `get`) is circular — the map
        # value type is exactly what we are learning; use the right side only
        # when it types concretely (a literal fallback).
        right = _map_value_expr_type(node.get("right"), var_types, env, functions, v3_ctx)
        if right is not None and "Never" not in right:
            return right
        return None
    return None


# Map verbs that write a value at arg[1]; each pins the host Map's value type V.
# Inferring V from ANY writer (not the literal name "insert") lets a CAS-only
# writer (`insert_if_absent`, item 397) pin a concrete V instead of the Str
# default (item 402).
_MAP_VALUE_WRITERS = ("insert", "insert_if_absent")

# item 397: the compare-and-set host verb whose bound result is a `boolean` and
# whose site-spelled undo is registered only when the CAS actually inserted.
_MAP_CAS_VERBS = ("insert_if_absent",)


def _is_map_cas(acquire) -> bool:
    """Whether a lowered acquisition node is a result-guarded map CAS."""
    return (isinstance(acquire, dict) and acquire.get("kind") == "call"
            and acquire.get("method") in _MAP_CAS_VERBS)


def _map_expr_inserts(node: object, bind: str, var_types: dict, env: _Env,
                      functions: list, v3_ctx: _V3Ctx | None,
                      candidates: list[str]) -> None:
    """Collect candidate value types from any map value-writing call
    (`insert`, `insert_if_absent`, ...) on `bind` anywhere in an expression;
    recurses into sub-expressions. Handles both the v1 call shape
    (`target`/`method`) and the 2.0 shape (`callee` as a field access)."""
    if not isinstance(node, dict):
        return
    if node.get("kind") == "call":
        target = node.get("target") or {}
        if (target.get("kind") in ("name", "var")
                and (target.get("id") or target.get("name")) == bind
                and node.get("method") in _MAP_VALUE_WRITERS):
            args = node.get("args") or []
            if len(args) >= 2:
                t = _map_value_expr_type(args[1], var_types, env, functions, v3_ctx)
                if t is not None and "Never" not in t:
                    candidates.append(t)
        callee = node.get("callee")
        if isinstance(callee, dict) and callee.get("kind") == "field":
            ct = callee.get("target") or {}
            if (ct.get("kind") in ("name", "var")
                    and (ct.get("id") or ct.get("name")) == bind
                    and callee.get("name") in _MAP_VALUE_WRITERS):
                args = node.get("args") or []
                if len(args) >= 2:
                    t = _map_value_expr_type(args[1], var_types, env, functions, v3_ctx)
                    if t is not None and "Never" not in t:
                        candidates.append(t)
        for arg in node.get("args") or []:
            _map_expr_inserts(arg, bind, var_types, env, functions, v3_ctx, candidates)
        _map_expr_inserts(target, bind, var_types, env, functions, v3_ctx, candidates)
        _map_expr_inserts(callee, bind, var_types, env, functions, v3_ctx, candidates)
        return
    for value in node.values():
        if isinstance(value, list):
            for item in value:
                _map_expr_inserts(item, bind, var_types, env, functions, v3_ctx, candidates)
        elif isinstance(value, dict):
            _map_expr_inserts(value, bind, var_types, env, functions, v3_ctx, candidates)


def _map_insert_candidates(step: dict, bind: str, var_types: dict, env: _Env,
                           functions: list, v3_ctx: _V3Ctx | None,
                           candidates: list[str]) -> None:
    """Walk one component-body step (or provide-method body step) for `insert`
    calls on `bind`."""
    kind = step.get("step")
    if kind in ("effect", "let-effect"):
        _map_expr_inserts(step.get("acquire"), bind, var_types, env, functions, v3_ctx, candidates)
        _map_expr_inserts(step.get("undo"), bind, var_types, env, functions, v3_ctx, candidates)
        for nested in step.get("setup") or []:
            _map_insert_candidates(nested, bind, var_types, env, functions, v3_ctx, candidates)
    elif kind == "provide":
        # provide methods type their parameters from the service declaration
        service = env.services.get(step.get("service") or "")
        if service is None:
            return
        for method in step.get("methods") or []:
            mvar = {
                p.get("name"): p.get("type")
                for p in ((service.get("methods") or {})
                          .get(method.get("name") or "", {})).get("params") or []
            }
            for body_step in method.get("body") or []:
                _map_insert_candidates(body_step, bind, mvar, env, functions, v3_ctx, candidates)
    elif kind in ("let", "assign"):
        _map_expr_inserts(step.get("value"), bind, var_types, env, functions, v3_ctx, candidates)
    elif kind == "return":
        _map_expr_inserts(step.get("expr"), bind, var_types, env, functions, v3_ctx, candidates)
    elif kind == "emit":
        _map_expr_inserts(step.get("expr"), bind, var_types, env, functions, v3_ctx, candidates)
        _map_expr_inserts(step.get("compensate"), bind, var_types, env, functions, v3_ctx, candidates)
    elif kind == "if":
        for nested in step.get("then") or []:
            _map_insert_candidates(nested, bind, var_types, env, functions, v3_ctx, candidates)
        for nested in step.get("else") or []:
            _map_insert_candidates(nested, bind, var_types, env, functions, v3_ctx, candidates)
    elif kind == "fail":
        _map_expr_inserts(step.get("message"), bind, var_types, env, functions, v3_ctx, candidates)


def _map_value_surface_type(env: _Env, bind: str, functions: list,
                            v3_ctx: _V3Ctx | None) -> str | None:
    """The revl *surface* value type of a host Map binding, learned from its
    `insert` call sites across the whole component (activation body + every
    provide method). `None` when no site pins a concrete type — the emitter
    then falls back to `Str` (the historical surface)."""
    candidates: list[str] = []
    for step in env.component.get("body") or []:
        _map_insert_candidates(step, bind, {}, env, functions, v3_ctx, candidates)
    if not candidates:
        return None
    distinct: list[str] = []
    for t in candidates:
        if t not in distinct:
            distinct.append(t)
    # A genuinely mixed map cannot be one revl `Map[Str, V]`; the first
    # concrete candidate (document order) is deterministic, and a real
    # conflict surfaces loudly in javac at the mismatched `insert`.
    return distinct[0]


def _component_map_values(env: _Env, functions: list,
                          v3_ctx: _V3Ctx | None) -> dict[str, str]:
    """bind -> revl surface value type for every host Map binding in the
    component, defaulted to `Str` when nothing pins a concrete type."""
    out: dict[str, str] = {}
    for s in env.component.get("body") or []:
        if s.get("step") != "let-effect":
            continue
        acquire = s.get("acquire") or {}
        if acquire.get("kind") != "host" or not (acquire.get("fn") or "").startswith("Map."):
            continue
        surface = _map_value_surface_type(env, s["bind"], functions, v3_ctx)
        out[s["bind"]] = surface or "Str"
    return out


def _bind_decl_type(component: dict, bind: str, render_type,
                    map_values: dict[str, str]) -> str:
    """The Java type of a binding's declaration in the legacy (v1/v2) path:
    the host object's class, or a generic `Map<...>` whose value type comes
    from the IR (FR-4). `render_type` is the signature renderer in use
    (`_java_type` for v1/v2, `_java_v3_type` for v3), which decides how the
    surface value type renders as a Java type argument."""
    host = _host_of(component, bind)
    if host == "Map":
        surface = map_values.get(bind, "Str")
        if render_type is _java_v3_type:
            return f"Map<{_java_v3_type(surface, boxed=True)}>"
        return f"Map<{_java_type_arg(surface)}>"
    return host


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
    if not steps:
        # A genuinely EMPTY body (`fn reset() { }`) is a real no-op, not an
        # unported effect: emit an empty method. The two are distinguishable
        # here because an empty body carries zero steps, whereas an
        # unemittable effectful body carries steps this simple renderer does
        # not handle (it falls through to the honest trap below). A void op
        # becomes a no-op; an empty body on a value-returning op is ill-typed,
        # so it keeps the trap rather than emitting a method that returns
        # nothing.
        if _method_return(env, key, method.get("name")) is None:
            return ""
        return 'throw new UnsupportedOperationException("effectful method body");'
    if len(steps) == 1 and steps[0].get("step") == "return":
        if steps[0].get("expr") is None:
            # bare `return` — a void service operation
            if _method_return(env, key, method.get("name")) is not None:
                raise EmitError(
                    f"{env.name}.{method.get('name')}: bare 'return' in an "
                    f"operation declared to return a value"
                )
            return "return;"
        rename = {b: f"this.{b}" for b in _binds(env.component)}
        # A required service is a field of the provider class, same as a bind.
        rename.update({local: f"this.{local}" for local in env.reqs})
        value = _expr(steps[0]["expr"], None, rename, env)
        # A `void` service operation cannot `return <expr>;` in Java — run
        # the expression for its effect instead.
        if _method_return(env, key, method.get("name")) is None:
            return f"{value};"
        return f"return {value};"
    return 'throw new UnsupportedOperationException("effectful method body");'


_V1_EXPR_KINDS = {"name", "lit", "config", "host", "req", "call", "format"}


def _contains_expr(node: object) -> bool:
    if isinstance(node, dict):
        kind = node.get("kind")
        if kind == "call" and "callee" in node:
            return True
        if kind not in _V1_EXPR_KINDS:
            return True
        return any(_contains_expr(value) for value in node.values())
    if isinstance(node, list):
        return any(_contains_expr(value) for value in node)
    return False


def _reads_config(node: object) -> bool:
    """True if `node` reads a component `config` field anywhere inside it.

    A `config` node is v1-expressible in the ACTIVATION body, because `apply()` is a
    method of `<Comp>Plugin`, which holds the config fields, so a bare
    `prefix` resolves there. It is NOT expressible in a PROVIDE-METHOD body:
    the legacy path puts that body in a separate `<Comp><Key>` provider class
    with no config fields and no reference to the plugin, so the same bare
    `prefix` names nothing and the emitted Java does not compile. The modern
    path threads config through the provider's constructor, so it is the only
    correct home for such a method."""
    if isinstance(node, dict):
        if node.get("kind") == "config":
            return True
        return any(_reads_config(value) for value in node.values())
    if isinstance(node, list):
        return any(_reads_config(value) for value in node)
    return False


def _provider_config_fields(component: dict) -> list[dict]:
    """The component's `config` fields when a PROVIDE METHOD reads one, else
    an empty list.

    `_expr` renders a `config` node as the BARE field name, which resolves in
    `apply()` because that is a method of `<Comp>Plugin` and the plugin holds
    the config fields. A provide-method body lives in the separate
    `<Comp><Key>` provider class, which carried ctx/fx/reqs/binds and no
    config at all, so `config.prefix` emitted a `prefix` that names nothing
    and javac rejected the unit. Threading config into the provider for
    exactly the components that read it there keeps every other component
    emitting byte-for-byte as before (the same gating rule `needs_frame`
    uses)."""
    config_fields = component.get("config") or []
    if not config_fields:
        return []
    for step in component.get("body") or []:
        if step.get("step") != "provide":
            continue
        for method in step.get("methods") or []:
            if _reads_config(method.get("body")):
                return list(config_fields)
    return []


def _component_needs_modern(component: dict) -> bool:
    if component.get("isolate") or component.get("intercept"):
        return True
    # item 173: a routed require needs the modern path — its emitted router
    # class and the per-realm resolution live there. (A routed component's
    # provide method already calls the routed service, so it reaches modern via
    # `_contains_expr` anyway; this makes the routing dependence explicit.)
    if component.get("routes"):
        return True
    for step in component.get("body") or []:
        if step.get("setup"):
            return True
        if step.get("step") in {"if", "fail", "await", "return"}:
            return True
        # An `emit` carrying a `compensate` MUST take the modern path: the
        # simple renderer emits the emission but silently drops its
        # compensation — a lost teardown (G7 residue). The generic
        # `_contains_expr` key-scan below misses a compensate whose expression
        # is a plain call, so guard it explicitly by step shape.
        if step.get("step") == "emit" and step.get("compensate") is not None:
            return True
        if step.get("step") == "provide":
            for method in step.get("methods") or []:
                for stmt in method.get("body") or []:
                    if stmt.get("step") != "return":
                        return True
                    if _contains_expr(stmt.get("expr")):
                        return True
                    # A `config` read is v1-expressible in the activation body
                    # but not in a provide method (see `_reads_config`): the
                    # legacy provider class has no config field, so it emitted
                    # a bare `prefix` that names nothing and does not compile.
                    if _reads_config(stmt.get("expr")):
                        return True
        for key in ("acquire", "undo", "expr", "compensate", "message", "cond", "value"):
            if key in step and _contains_expr(step[key]):
                return True
    return False


def _witnessed_externs(externs: list | None) -> dict[str, dict]:
    """Witnessed externs (item 243) by name, so a call site's `acquire` can
    be recognised as a transactional effect and register its DECLARED
    inverse into the two-phase teardown loop instead of a site-spelled undo.
    Mirrors `backends/python/emit.py`'s `_ComponentEmitter.witnessed` table.
    Empty for every program that declares no witnessed extern, so those
    programs' bracket-only components are unaffected by this slice."""
    return {
        ext["name"]: ext for ext in (externs or [])
        if ext.get("class") == "witnessed"
    }


def _witnessed_extern_for(acquire: object, witnessed: dict[str, dict]) -> dict | None:
    """The witnessed extern descriptor a step's `acquire` calls, or None. A
    component-step acquisition renders as an IR `fn` node (v1/component
    dialect); matching its name against `witnessed` is how the emitter tells
    a transaction from an ordinary bracket (see `_witnessed_externs`)."""
    if not witnessed or not isinstance(acquire, dict):
        return None
    if acquire.get("kind") != "fn":
        return None
    return witnessed.get(acquire.get("name"))


def _component_needs_frame(component: dict, witnessed: dict[str, dict]) -> bool:
    """True if this component registers at least one `transactional` (item
    243) or `compensation` (item 247) teardown entry — the two entry kinds
    beyond the plain `bracket`, per docs/design/teardown-contract.md. Gates
    the RevlFrame two-phase teardown loop so a component that only ever uses
    plain `acquire`/`undo` brackets keeps emitting exactly as before (same
    precedent as `_witnessed_externs`/243 Slice 2a's byte-identical rule,
    extended to compensation): scanned recursively through `if`/`else` and
    into `provide`-method bodies, where a method-time `emit ... compensate`
    or `effect ... undo` shares the SAME per-activation accumulator (the
    provided service instance carries the owning activation's `fx`/`frame`,
    see `_emit_component_modern`)."""

    def step_needs(step: dict) -> bool:
        kind = step.get("step")
        if kind == "emit" and step.get("compensate") is not None:
            return True
        if kind in ("let-effect", "effect"):
            if _witnessed_extern_for(step.get("acquire"), witnessed) is not None:
                return True
        if kind == "if":
            return (any(step_needs(s) for s in step.get("then") or [])
                    or any(step_needs(s) for s in step.get("else") or []))
        if kind == "provide":
            return any(
                step_needs(s)
                for method in step.get("methods") or []
                for s in method.get("body") or []
            )
        return False

    return any(step_needs(step) for step in component.get("body") or [])


def _uses_revl_frame(ir: dict) -> bool:
    """True if any component in the document needs the RevlFrame two-phase
    teardown loop — gates emitting the shared helper class once per file."""
    externs = ir.get("externs") or []
    witnessed = _witnessed_externs(externs)
    return any(
        _component_needs_frame(component, witnessed)
        for component in ir.get("components") or []
    )


def _emit_revl_frame_runtime() -> list[str]:
    """The shared two-phase teardown accumulator (docs/design/teardown-
    contract.md), emitted once per file when any component needs it.

    cordis4j's `Context.EffectScope` (backends/java/stubs/io/cordis4j/core/
    Context.java) is a dumb LIFO of `Disposable`s with no built-in commit/
    abort signal and no per-item failure guard — `dispose()` just pops and
    disposes, propagating whatever the first failing entry throws and
    stopping there. RevlFrame supplies the three things that raw stack does
    not: (1) a commit/abort discriminator (`committed`, flipped once by the
    emitted `apply()` right before its success `return fx;` — strictly
    before any disposal can occur, which is simpler than the py reference's
    generator-ordering trick because Java's `apply()` is synchronous, so
    there is no deferred-yield window to reason about); (2) a guard on every
    bracket/transactional entry so a failing inverse is recorded as residue
    and NEVER stops the remaining Phase-1 replay (continue-and-record, two
    severities: `bracket-fault` is contract-grade, `restore-residue` is
    anticipated); (3) the Phase-2 split for `compensation` entries — they
    must not run inline during fx's native LIFO walk (that walk IS Phase 1),
    so `compensation()`'s disposer only enqueues, and `runPhase2()` — called
    by the emitted `apply()`'s catch block right after `fx.dispose()`
    returns — drains the queue best-effort, bounded by the two `REVL_*`
    budget env vars, after every Phase-1 inverse has already run to
    completion.

    Java per-tier rule (docs/design/teardown-contract.md, 'interruptible
    blocking points only'): `Thread.interrupt` preempts only interruptible
    IO/waits, not arbitrary synchronous host code, so `Future.get(timeout)` +
    `cancel(true)` is a true in-call preemption ONLY when the compensation
    honors the interrupt. When it does not, the task thread keeps running
    DETACHED past the timeout — exactly go's abandon-the-wait shape — and the
    record carries `outcome: unknown`; per the contract, go's concurrency
    caveat then applies on java too (Phase-2 start order is pinned LIFO, but
    two compensations may run concurrently once one has been abandoned).

    The durable WAL discharge-descriptor is emitted SEPARATELY, only under
    `--record` (item 322 Slice 2, `_emit_record_sink`): the recording sink and
    the per-descriptor `revlRecordTransactional` calls ride alongside this frame
    but never appear in the default (byte-identical) output, so this loop itself
    is unchanged whether or not a WAL is being written. `residue` below is the
    in-process record shape the contract specifies, still surfaced by observable
    ordering in the scenario harnesses; the WAL is the crash-durable channel that
    outlives the process (a JVM subprocess writes it to `$REVL_WAL` and fsyncs
    per record), which `revl recover` reads tier-agnostically."""
    return [
        "// docs/design/teardown-contract.md: the shared bracket/transactional/",
        "// compensation two-phase teardown loop (item 243 Slice 2b, item 247).",
        "private static final class RevlFrame {",
        "    private boolean committed = false;",
        "    private final java.util.List<Residue> residue = new java.util.ArrayList<>();",
        "    private final java.util.ArrayDeque<Phase2Task> phase2 = new java.util.ArrayDeque<>();",
        "",
        "    private record Residue(String kind, String crossing, String attempted,",
        "                            boolean attemptedFlag, String error, String outcome) {}",
        "",
        "    private record Phase2Task(String crossing, String attempted, Runnable action) {}",
        "",
        "    void commit() {",
        "        committed = true;",
        "    }",
        "",
        "    /** `bracket` (acquire): replays on every teardown, clean unload and",
        "     * abort alike. Guarded so a failed inverse never skips the remaining",
        "     * Phase-1 entries (contract-grade severity: bracket-fault). */",
        "    Disposable bracket(String crossing, String attempted, Runnable inverse) {",
        "        return () -> {",
        "            try {",
        "                inverse.run();",
        "            } catch (RuntimeException | Error err) {",
        '                residue.add(new Residue("bracket-fault", crossing, attempted,',
        '                        true, String.valueOf(err), "failed"));',
        "            }",
        "        };",
        "    }",
        "",
        "    /** `transactional` (item 243, witnessed): replays the declared inverse",
        "     * ONLY on abort; a committed activation DISCHARGES it (the mutation is",
        "     * the deliverable and persists). Anticipated failure severity:",
        "     * restore-residue. */",
        "    Disposable transactional(String crossing, String attempted, Runnable undo) {",
        "        return () -> {",
        "            if (committed) {",
        "                return;",
        "            }",
        "            try {",
        "                undo.run();",
        "            } catch (RuntimeException | Error err) {",
        '                residue.add(new Residue("restore-residue", crossing, attempted,',
        '                        true, String.valueOf(err), "failed"));',
        "            }",
        "        };",
        "    }",
        "",
        "    /** `transactionalMethod` (item 318): the per-tool-call H1 seam — a",
        "     * witnessed fs mutation fired from a PROVIDE-METHOD body (per request),",
        "     * after activation, whose inverse must outlive the method call and",
        "     * survive until the component/session commits or aborts. On java this",
        "     * is BEHAVIOURALLY IDENTICAL to `transactional`, and deliberately so:",
        "     * the py tier has to PARK a method-registered entry in a separate",
        "     * `_deferred_transactional` list and dispose it inside `drain`, because",
        "     * on py `_committed` only flips at TEARDOWN (drain), so a method entry",
        "     * disposed as an ordinary sibling would observe committed==false on a",
        "     * clean unload and wrongly revert the deliverable. Java has no such",
        "     * window: `committed` flips once at ACTIVATION-END (the emitted",
        "     * apply()'s `frame.commit()` before it returns), strictly before any",
        "     * provide-method can run and before any teardown, so the disposer this",
        "     * returns already observes the settled commit bit when the enclosing",
        "     * component's `fx` disposes it at unload. The ONE soundness rule the",
        "     * emitter must honour is that this entry is tracked into the COMPONENT",
        "     * ACTIVATION `fx` (the provider struct's `this.fx`), never a per-call",
        "     * scope: a per-call scope would dispose it at method-return with",
        "     * committed==true, discharging it and dropping the undo, so a later",
        "     * abort could not revert it (residue). */",
        "    Disposable transactionalMethod(String crossing, String attempted, Runnable undo) {",
        "        return transactional(crossing, attempted, undo);",
        "    }",
        "",
        "    /** Session-level reject seam (item 318; item 245's explicit commit/abort",
        "     * UX is the eventual driver). A component that activated cleanly has",
        "     * already run `commit()`, so a later unload would discharge every",
        "     * transactional entry and PERSIST its mutation. Calling `abort()` before",
        "     * teardown flips the discriminator back to false, so the next `fx`",
        "     * dispose replays every transactional inverse — the activation-body ones",
        "     * AND the per-tool-call method-registered ones — and the mutations",
        "     * revert. Idempotent. */",
        "    void abort() {",
        "        committed = false;",
        "    }",
        "",
        "    /** `compensation` (item 247): abort-only, best-effort, Phase 2. A",
        "     * committed activation DISCHARGES it (never runs). On abort this",
        "     * disposer fires during fx's native Phase-1 walk, interleaved with",
        "     * bracket/transactional entries, so it must not run inline: it",
        "     * enqueues, and `runPhase2()` drains the queue after fx's walk (and",
        "     * therefore every Phase-1 inverse) has returned. */",
        "    Disposable compensation(String crossing, String attempted, Runnable emit) {",
        "        return () -> {",
        "            if (committed) {",
        "                return;",
        "            }",
        "            phase2.addLast(new Phase2Task(crossing, attempted, emit));",
        "        };",
        "    }",
        "",
        "    private void runGuarded(Phase2Task task) {",
        "        try {",
        "            task.action().run();",
        "        } catch (RuntimeException | Error err) {",
        '            residue.add(new Residue("compensation-residue", task.crossing(), task.attempted(),',
        '                    true, String.valueOf(err), "failed"));',
        "        }",
        "    }",
        "",
        "    /** Phase 2: LIFO within the compensation class (entries queue in the",
        "     * order fx's native walk visits them, already reverse-registration",
        "     * order), best-effort, bounded by REVL_COMPENSATION_BUDGET_MS /",
        "     * REVL_COMPENSATION_PER_CALL_MS (default 5000/1000 ms). Never throws:",
        "     * the abort always succeeds. */",
        "    void runPhase2() {",
        "        if (phase2.isEmpty()) {",
        "            return;",
        "        }",
        '        long budgetMs = envMs("REVL_COMPENSATION_BUDGET_MS", 5000L);',
        '        long perCallMs = envMs("REVL_COMPENSATION_PER_CALL_MS", 1000L);',
        "        long deadline = budgetMs <= 0 ? Long.MAX_VALUE",
        "                : System.nanoTime() + budgetMs * 1_000_000L;",
        "        java.util.concurrent.ExecutorService pool = null;",
        "        try {",
        "            while (!phase2.isEmpty()) {",
        "                Phase2Task next = phase2.pollFirst();",
        "                if (budgetMs > 0 && System.nanoTime() >= deadline) {",
        '                    residue.add(new Residue("compensation-residue", next.crossing(), next.attempted(),',
        '                            false, "deadline-expired", "not-attempted"));',
        "                    continue;",
        "                }",
        "                if (perCallMs <= 0) {",
        "                    runGuarded(next);",
        "                    continue;",
        "                }",
        "                if (pool == null) {",
        "                    pool = java.util.concurrent.Executors.newCachedThreadPool(r -> {",
        '                        Thread t = new Thread(r, "revl-compensation");',
        "                        t.setDaemon(true);",
        "                        return t;",
        "                    });",
        "                }",
        "                java.util.concurrent.Future<?> future = pool.submit(() -> runGuarded(next));",
        "                try {",
        "                    future.get(perCallMs, java.util.concurrent.TimeUnit.MILLISECONDS);",
        "                } catch (java.util.concurrent.TimeoutException timeout) {",
        "                    // java per-tier rule: cancel(true) preempts only an",
        "                    // interruptible blocking point. When the compensation",
        "                    // ignores it, the task keeps running DETACHED past this",
        "                    // point (go's abandon-the-wait shape) -> outcome unknown.",
        "                    future.cancel(true);",
        '                    residue.add(new Residue("compensation-residue", next.crossing(), next.attempted(),',
        '                            true, "per-call bound exceeded", "unknown"));',
        "                } catch (InterruptedException interrupted) {",
        "                    Thread.currentThread().interrupt();",
        '                    residue.add(new Residue("compensation-residue", next.crossing(), next.attempted(),',
        '                            true, "teardown thread interrupted", "unknown"));',
        "                } catch (java.util.concurrent.ExecutionException impossible) {",
        "                    // runGuarded() already catches RuntimeException/Error",
        "                    // internally, so the submitted task never actually",
        "                    // throws; this branch only satisfies future.get()'s",
        "                    // checked signature.",
        '                    residue.add(new Residue("compensation-residue", next.crossing(), next.attempted(),',
        '                            true, String.valueOf(impossible.getCause()), "failed"));',
        "                }",
        "            }",
        "        } finally {",
        "            if (pool != null) {",
        "                pool.shutdown();",
        "            }",
        "        }",
        "    }",
        "",
        "    private static long envMs(String name, long fallback) {",
        "        String raw = System.getenv(name);",
        "        if (raw == null || raw.isEmpty()) {",
        "            return fallback;",
        "        }",
        "        try {",
        "            return Long.parseLong(raw.trim());",
        "        } catch (NumberFormatException bad) {",
        "            return fallback;",
        "        }",
        "    }",
        "}",
        "",
        "// item 318: the per-activation Disposable an emitted frame-bearing apply()",
        "// returns instead of the bare `fx` EffectScope. It carries the component's",
        "// `RevlFrame` alongside `fx` so a session-level reject can reach it AFTER a",
        "// clean activation (mirrors the py tier's ctx->Frame reachability, which",
        "// item 245's commit/abort UX drives). `dispose()` runs the native Phase-1",
        "// LIFO walk (`fx.dispose()`) and then drains any Phase-2 compensations the",
        "// walk enqueued — a no-op on a clean unload (committed => every entry",
        "// discharges, nothing enqueues) and the abort drain when `abort()` was",
        "// called first. `abort()` flips the commit discriminator so that dispose",
        "// reverts rather than persists. Existing callers that only `dispose()` the",
        "// returned Disposable are unaffected.",
        "public static final class RevlActivation implements Disposable {",
        "    private final Context.EffectScope fx;",
        "    private final RevlFrame frame;",
        "",
        "    RevlActivation(Context.EffectScope fx, RevlFrame frame) {",
        "        this.fx = fx;",
        "        this.frame = frame;",
        "    }",
        "",
        "    /** Session-level reject: revert this activation's transactional work",
        "     * (activation-body AND per-tool-call) on the next dispose. */",
        "    public void abort() {",
        "        frame.abort();",
        "    }",
        "",
        "    @Override",
        "    public void dispose() {",
        "        fx.dispose();",
        "        frame.runPhase2();",
        "    }",
        "}",
        "",
    ]


# item 322 Slice 2: the durable WAL recording sink emitted into Components when
# `--record` is set and the document carries a teardown frame. The java mirror of
# backends/go/emit.py's `_RECORD_PREAMBLE`: one JSON line per record, fsync'd via
# `FileChannel.force(true)` before the call that wrote it returns, opened from
# `REVL_WAL` (unset -> every record is a no-op, so a non-record composition that
# happens to compile this in is inert). The py JSONL schema, field-for-field:
# `header`, `discharge-descriptor {seq, entry, call:{receiver,method,args},
# origin, witness, idempotency}`, `discharge`, `activation-complete`. Written by
# hand (no JSON dependency); the reader (`revl.wal.read_wal`) parses per line, so
# object field ORDER is irrelevant — only the names/values must match. The three
# `revlRecord*` methods are `public static` so a driver (the crash producer, and
# the stub/real runners on a clean unload) can stamp discharge / the terminal
# marker from outside Components. WAL_GUARANTEE is byte-identical to
# src/revl/wal.py's constant (a header a py tool reads must agree on it).
_RECORD_SINK_SOURCE = r'''
// ---- durable WAL recording sink (item 322 Slice 2, the java host recording channel) ----

private static final String REVL_WAL_GUARANTEE =
        "the WAL records each committed effect's step identity, boundary "
        + "classification and inverse DESCRIPTOR (not its closure). On restart, "
        + "recovery runs the reconstructible boundary inverses newest-first (LIFO); "
        + "in-process inverses are moot (their captured memory died with the "
        + "process) and closure-only boundary inverses are reported as residue, "
        + "never silently claimed to have run.";

private static java.io.FileOutputStream revlWalOut;
private static java.nio.channels.FileChannel revlWalChannel;
private static int revlWalSeq = 0;
private static final java.util.List<Integer> revlWalSeqs = new java.util.ArrayList<>();
private static final Object revlWalLock = new Object();

static {
    revlWalOpen();
}

// Wire the sink to REVL_WAL (unset -> no-op recording) and stamp the header.
// Runs once at class load. A failed open leaves the sink null (recording is
// silently off) rather than crashing a composition that only wanted to run.
private static void revlWalOpen() {
    String path = System.getenv("REVL_WAL");
    if (path == null || path.isEmpty()) {
        return;
    }
    try {
        revlWalOut = new java.io.FileOutputStream(path, true);
        revlWalChannel = revlWalOut.getChannel();
    } catch (java.io.IOException open) {
        revlWalOut = null;
        revlWalChannel = null;
        return;
    }
    revlWalWrite("{\"record\":\"header\",\"walVersion\":1,\"generation\":1,\"guarantee\":"
            + revlWalStr(REVL_WAL_GUARANTEE) + "}");
}

// One durable JSON line: write, flush, and fsync (FileChannel.force(true))
// before returning — the write-ahead discipline the py tier uses, so a record a
// caller saw acknowledged is on disk before the effect it describes may matter.
private static void revlWalWrite(String json) {
    if (revlWalOut == null) {
        return;
    }
    synchronized (revlWalLock) {
        try {
            revlWalOut.write((json + "\n").getBytes(java.nio.charset.StandardCharsets.UTF_8));
            revlWalOut.flush();
            revlWalChannel.force(true);
        } catch (java.io.IOException ignored) {
        }
    }
}

// revlRecordTransactional appends the discharge-descriptor for one witnessed
// transactional inverse: the re-issuable named call {receiver, method, args}
// recover replays LIFO to undo the mutation, plus the forward `origin` it
// reverses. Fsync'd before it returns, so a crash after this call still leaves
// the inverse re-issuable from the log alone.
public static void revlRecordTransactional(String receiver, String method, String[] args) {
    if (revlWalOut == null) {
        return;
    }
    synchronized (revlWalLock) {
        int seq = revlWalSeq++;
        revlWalSeqs.add(seq);
        String call = revlWalCall(receiver, method, args);
        revlWalWrite("{\"record\":\"discharge-descriptor\",\"seq\":" + seq
                + ",\"entry\":\"transactional\",\"call\":" + call
                + ",\"origin\":" + call
                + ",\"witness\":null,\"idempotency\":null}");
    }
}

// revlRecordDischarge writes the commit-path proof that every recorded
// transactional seq COMMITTED, so recover SKIPS it — a committed transaction is
// never rolled back. Called on a clean unload, never on a crash.
public static void revlRecordDischarge() {
    if (revlWalOut == null) {
        return;
    }
    synchronized (revlWalLock) {
        StringBuilder discharged = new StringBuilder("[");
        for (int i = 0; i < revlWalSeqs.size(); i++) {
            if (i > 0) {
                discharged.append(',');
            }
            discharged.append(revlWalSeqs.get(i));
        }
        discharged.append(']');
        revlWalWrite("{\"record\":\"discharge\",\"discharged\":" + discharged + "}");
    }
}

// revlRecordActivationComplete stamps the terminal marker: its presence is the
// whole roll-forward decision, its absence (a crash) is roll-back. Written only
// after a clean unload.
public static void revlRecordActivationComplete() {
    if (revlWalOut == null) {
        return;
    }
    synchronized (revlWalLock) {
        revlWalWrite("{\"record\":\"activation-complete\",\"generation\":1,\"components\":[]}");
    }
}

private static String revlWalCall(String receiver, String method, String[] args) {
    StringBuilder call = new StringBuilder("{\"receiver\":");
    call.append(revlWalStr(receiver)).append(",\"method\":").append(revlWalStr(method))
            .append(",\"args\":[");
    for (int i = 0; i < args.length; i++) {
        if (i > 0) {
            call.append(',');
        }
        call.append(revlWalStr(args[i]));
    }
    call.append("]}");
    return call.toString();
}

// Minimal JSON string escaper (quote/backslash/control chars) — enough for the
// identifiers and stringified witnesses this schema carries, and it never
// depends on a JSON library the emitted module would otherwise not need.
private static String revlWalStr(String value) {
    StringBuilder out = new StringBuilder("\"");
    for (int i = 0; i < value.length(); i++) {
        char c = value.charAt(i);
        switch (c) {
            case '"' -> out.append("\\\"");
            case '\\' -> out.append("\\\\");
            case '\n' -> out.append("\\n");
            case '\r' -> out.append("\\r");
            case '\t' -> out.append("\\t");
            default -> {
                if (c < 0x20) {
                    out.append(String.format("\\u%04x", (int) c));
                } else {
                    out.append(c);
                }
            }
        }
    }
    out.append('"');
    return out.toString();
}
'''


def _emit_record_sink() -> list[str]:
    """The durable WAL recording sink (item 322 Slice 2), emitted into Components
    only in record mode (`--record`) when the document carries a teardown frame.
    Byte-identical default output is preserved by never emitting this otherwise —
    the golden oracle and the selfhost mirror both run with record off."""
    return _RECORD_SINK_SOURCE.strip("\n").split("\n")


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
    if any(t.get("lifecycle") for t in (ir.get("tests") or [])):
        # the lifecycle-test driver mints its own root (item 178(b))
        names.add("Contexts")
    if _ir_uses_component_step(ir, "await"):
        names.add("AsyncPlugin")
    if _ir_uses_component_step(ir, "fail"):
        names.add("CordisException")
    # item 173: an emitted router class throws `CordisException` on an empty
    # live set and on the unreachable tail, with no `fail` step anywhere in the
    # document. Without this arm the emitted unit does not compile ("cannot
    # find symbol: class CordisException"), which is what running `javac` over
    # `bench/codegen/java/cases/router` surfaced.
    if any(component.get("routes") for component in ir.get("components") or []):
        names.add("CordisException")
    return [f"import io.cordis4j.core.{name};" for name in sorted(names)]


def _bind_type(component: dict, bind: str, v3_ctx: _V3Ctx,
               map_values: dict[str, str] | None = None) -> str:
    host = _host_of(component, bind)
    if host == "Map":
        # FR-4: the host Map is generic; the value type is learned from the
        # IR's `insert` sites (defaults to the historical `Str`).
        surface = (map_values or {}).get(bind, "Str")
        return f"Map<{_java_v3_type(surface, boxed=True)}>"
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
    env: _Env, method: dict, v3_ctx: _V3Ctx, *, returns_void: bool = False,
    frame_expr: str | None = None,
) -> list[str]:
    lines: list[str] = []
    v3_ctx.arrows = {}  # arrow bindings are local to one body
    rename = {b: f"this.{b}" for b in _binds(env.component)}
    rename.update({local: f"this.{local}" for local in env.reqs})
    for stmt in method.get("body") or []:
        step = stmt.get("step")
        if step == "return":
            if stmt.get("expr") is None:
                lines.append("return;")
            elif returns_void:
                # `void` methods run the expression for its effect.
                lines.append(f"{_expr(stmt['expr'], v3_ctx, rename, env)};")
                lines.append("return;")
            else:
                lines.append(f"return {_expr(stmt['expr'], v3_ctx, rename, env)};")
        elif step == "effect":
            wit = _witnessed_extern_for(stmt.get("acquire"), v3_ctx.witnessed)
            if wit is not None:
                # item 318: a per-tool-call witnessed fs mutation. Register the
                # extern's declared inverse into the COMPONENT activation frame
                # (this.fx/this.frame), disposed by the component's own unload,
                # not at method-return — the per-tool-call H1 seam.
                _emit_witnessed_step(
                    lines, "", stmt, wit, v3_ctx, env, None, frame_expr,
                    rename=rename, frame_method=True)
            else:
                lines.append(f"{_expr(stmt['acquire'], v3_ctx, rename, env)};")
                _emit_bracket_track(
                    lines, "", stmt["acquire"], stmt["undo"], v3_ctx, env, frame_expr,
                    rename, stmt)
        elif step == "let-effect":
            wit = _witnessed_extern_for(stmt.get("acquire"), v3_ctx.witnessed)
            if wit is not None:
                bind = _ident(stmt["bind"], "binding")
                _emit_witnessed_step(
                    lines, "", stmt, wit, v3_ctx, env, bind, frame_expr,
                    rename=rename, frame_method=True)
            elif _is_map_cas(stmt.get("acquire")):
                # item 397: a method-body host CAS. Bind the atomic `boolean`
                # result and register the site-spelled undo guarded on it — a
                # `false` CAS's inverse is the identity, so teardown never
                # removes the winner's entry. It rides the same per-activation
                # accumulator (`fx`/`frame`) as a bare method-body effect.
                bind = _ident(stmt["bind"], "binding")
                acquire_expr = _expr(stmt["acquire"], v3_ctx, rename, env)
                lines.append(f"boolean {bind} = {acquire_expr};")
                undo_rename = _emit_inverse_pins(
                    lines, "", stmt, "undo", v3_ctx, env, rename)
                undo_expr = _expr(stmt["undo"], v3_ctx, undo_rename, env)
                guarded = f"() -> {{ if ({bind}) {{ {undo_expr}; }} }}"
                if frame_expr is None:
                    lines.append(f"fx.track(Disposables.of({guarded}));")
                else:
                    crossing = _string(_call_label(stmt["acquire"]))
                    attempted = _string(_call_label(stmt["undo"]))
                    lines.append(
                        f"fx.track({frame_expr}.bracket({crossing}, {attempted}, "
                        f"{guarded}));")
            else:
                raise EmitError(
                    "a non-witnessed let-effect is not supported inside a method "
                    "body on the java tier")
        elif step == "emit":
            lines.append(f"{_expr(stmt['expr'], v3_ctx, rename, env)};")
            if stmt.get("compensate") is not None:
                _emit_compensation_track(
                    lines, "", stmt["expr"], stmt["compensate"], v3_ctx, env, frame_expr,
                    _emit_inverse_pins(lines, "", stmt, "compensate",
                                       v3_ctx, env, rename))
        elif step == "await":
            raise EmitError("await steps are not allowed inside method bodies (A1)")
        elif step in ("let", "assign"):
            # a plain value binding inside a method body
            name = _ident(stmt.get("name"), "binding")
            raw = stmt.get("value")
            if step == "let" and _bind_local_arrow(v3_ctx, name, raw, lines, "", rename, env):
                continue
            v3_ctx.arrows.pop(name, None)
            value = _expr(raw, v3_ctx, rename, env)
            decl = _adt_binding_type(raw, v3_ctx) or "var"
            lines.append(f"{decl} {name} = {value};" if step == "let"
                         else f"{name} = {value};")
        elif step == "provide":
            raise EmitError("provide steps are not allowed inside method bodies")
        else:
            raise EmitError(f"unknown step in method body: {step!r}")
    return lines

def _emit_setup_stmt(env: _Env, v3_ctx: _V3Ctx, step: dict, out: list[str], pad: str) -> None:
    kind = step.get("step")
    if kind in ("let", "assign"):
        name = _ident(step.get("name"), "binding")
        value = _expr(step.get("value"), v3_ctx, None, env)
        if kind == "let":
            out.append(f"{pad}{_let_keyword(step, v3_ctx)} {name} = {value};")
        else:
            out.append(f"{pad}{name} = {value};")
    elif kind == "expr":
        out.append(f"{pad}{_expr(step['expr'], v3_ctx, None, env)};")
    elif kind == "if":
        out.append(f"{pad}if ({_expr(step['cond'], v3_ctx, None, env)}) {{")
        for child in step.get("then") or []:
            _emit_setup_stmt(env, v3_ctx, child, out, pad + "    ")
        if step.get("else"):
            out.append(f"{pad}}} else {{")
            for child in step["else"]:
                _emit_setup_stmt(env, v3_ctx, child, out, pad + "    ")
        out.append(f"{pad}}}")
    elif kind == "while":
        _guard_frame_neutral_loop(step.get("body"))
        out.append(f"{pad}while ({_expr(step['cond'], v3_ctx, None, env)}) {{")
        for child in step.get("body") or []:
            _emit_setup_stmt(env, v3_ctx, child, out, pad + "    ")
        out.append(f"{pad}}}")
    elif kind == "for":
        _guard_frame_neutral_loop(step.get("body"))
        bind = _ident(step.get("bind"), "loop binding")
        out.append(f"{pad}for (var {bind} : {_expr(step['iterable'], v3_ctx, None, env)}) {{")
        for child in step.get("body") or []:
            _emit_setup_stmt(env, v3_ctx, child, out, pad + "    ")
        out.append(f"{pad}}}")
    elif kind == "break":
        out.append(f"{pad}break;")
    elif kind == "continue":
        out.append(f"{pad}continue;")
    elif kind == "let_pattern":
        value = _expr(step.get("value"), v3_ctx, None, env)
        tmp = f"__revl_destructure_{v3_ctx.next_gensym()}"
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
            f"{pad}if (!({_expr(step['expr'], v3_ctx, None, env)})) "
            f'throw new AssertionError("assertion failed");'
        )
    else:
        raise EmitError(f"unsupported component setup step {kind!r}")


def _emit_inverse_pins(
    out: list[str], pad: str, step: dict, slot: str,
    v3_ctx: "_V3Ctx", env: "_Env", rename: dict[str, str] | None,
) -> dict[str, str] | None:
    """Snapshot a derived inverse's mutable method-locals BY VALUE, and return the
    `rename` its expression must be rendered under (docs/closures.md;
    `<slot>_captures`, computed by `src/revl/lower.py::_pin_inverse_captures`).

    An `undo` runs at teardown, long after the method returned, so it must hold
    the value its forward effect used -- but a `var` the body reassigns
    afterwards is not effectively final, and javac REFUSES to let a lambda read
    it at all. This tier therefore did not silently undo the wrong thing; it
    emitted a source file that would not compile. Either way the remedy is the
    same one `_inline_arrow` already uses for an arrow's `captures`: a `final`
    copy taken where the effect registers, and the inverse rendered against that
    copy. The copy also restores the effectively-final property, so the shape
    compiles again.

    Returns `rename` unchanged when the lowering found nothing to pin, so a body
    without one emits byte-identically."""
    names = step.get(f"{slot}_captures") or []
    if not names:
        return rename
    inner = dict(rename or {})
    index = v3_ctx.next_gensym()
    for name in names:
        _ident(name, "inverse capture")
        snapshot = f"__revl_inverse_{index}_{_ident(name, 'inverse capture')}"
        out.append(f"{pad}final var {snapshot} = "
                   f"{_expr({'kind': 'var', 'name': name}, v3_ctx, rename, env)};")
        inner[name] = snapshot
    return inner


def _emit_bracket_track(
    out: list[str], pad: str, acquire: dict, undo: dict,
    v3_ctx: _V3Ctx, env: _Env, frame_expr: str | None,
    rename: dict[str, str] | None = None,
    step: dict | None = None,
) -> None:
    """A plain `acquire`/`undo` bracket entry (docs/design/teardown-
    contract.md): replays on every teardown, unchanged. When the owning
    component needs the two-phase loop (`frame_expr` set — it registers at
    least one transactional/compensation entry elsewhere), route the
    inverse through `RevlFrame.bracket` so a failed inverse is recorded as
    residue and never stops the remaining Phase-1 replay (mixed-entry LIFO,
    docs/design/teardown-contract.md exit test 3). A component with ONLY
    plain brackets (`frame_expr` is None) keeps emitting exactly as before."""
    rename = _emit_inverse_pins(out, pad, step or {}, "undo", v3_ctx, env, rename)
    undo_expr = _expr(undo, v3_ctx, rename, env)
    if frame_expr is None:
        out.append(f"{pad}fx.track(Disposables.of(() -> {undo_expr}));")
        return
    crossing = _string(_call_label(acquire))
    attempted = _string(_call_label(undo))
    out.append(
        f"{pad}fx.track({frame_expr}.bracket({crossing}, {attempted}, () -> {undo_expr}));"
    )


def _emit_compensation_track(
    out: list[str], pad: str, forward: dict, compensate: dict,
    v3_ctx: _V3Ctx, env: _Env, frame_expr: str | None,
    rename: dict[str, str] | None = None,
) -> None:
    """An `emit ... compensate` entry (item 247): abort-only, best-effort,
    Phase 2 — never on a clean unload. `frame_expr` is always set here in
    practice (`_component_needs_frame` forces it whenever a `compensate` is
    present); the plain-`Disposables` fallback is defensive only, kept so
    this function never silently drops the compensation if that invariant is
    ever violated (matching the honest-degrade spirit of the contract, not a
    reachable path)."""
    compensate_expr = _expr(compensate, v3_ctx, rename, env)
    if frame_expr is None:  # pragma: no cover — _component_needs_frame forces frame_expr
        out.append(f"{pad}fx.track(Disposables.of(() -> {compensate_expr}));")
        return
    crossing = _string(_call_label(forward))
    attempted = _string(_call_label(compensate))
    out.append(
        f"{pad}fx.track({frame_expr}.compensation({crossing}, {attempted}, "
        f"() -> {compensate_expr}));"
    )


def _emit_witnessed_step(
    out: list[str], pad: str, step: dict, ext: dict,
    v3_ctx: _V3Ctx, env: _Env, bind: str | None, frame_expr: str | None,
    rename: dict[str, str] | None = None, frame_method: bool = False,
) -> None:
    """Emit a witnessed effect (item 243): run the mutation, and on `Ok`
    register the extern's DECLARED inverse as a TRANSACTIONAL entry carrying
    the `Ok` witness. Mirrors `backends/python/emit.py`'s `_witnessed_step`:
    the inverse is the extern's own `undo` (no site-spelled undo — the
    accumulator owns it), bound to the `Ok` payload as `result` (the
    implicit witness binder, docs/design/243-witnessed-externs.md 'Slice 1
    as implemented' #1). On `Err` nothing is registered — a failed mutation
    touched nothing, so it must not schedule a rollback (Ok-conditional).
    `frame_expr` is always set here (a witnessed acquire always forces
    `_component_needs_frame`).

    item 318: when `frame_method` is set the acquisition is inside a
    PROVIDE-METHOD body (the per-tool-call H1 seam). The entry then routes
    through `RevlFrame.transactionalMethod` rather than `.transactional`, and
    the `fx`/`frame` referenced are the provider struct's activation-scope
    fields (`this.fx`/`this.frame`) — the COMPONENT-long accumulator, so the
    inverse outlives the method call and is disposed by the component's own
    unload (commit -> persist, `abort()` -> revert), never at method-return.
    `rename` maps requires/binds to `this.<name>` in method bodies (mirroring
    the bracket path); it is `None` in the activation body."""
    assert frame_expr is not None  # invariant: witnessed acquire => needs_frame
    tag = v3_ctx.next_gensym()
    result_var = f"_revl_wit{tag}"
    ok_var = f"_revl_ok{tag}"
    witness_type = _java_v3_type(ext.get("witness"))
    undo_expr = _expr(ext["undo"], v3_ctx, rename, env)
    crossing = _string(_call_label(step["acquire"]))
    attempted = _string(_call_label(ext["undo"]))
    entry = "transactionalMethod" if frame_method else "transactional"
    out.append(f"{pad}var {result_var} = {_expr(step['acquire'], v3_ctx, rename, env)};")
    out.append(f"{pad}if ({result_var} instanceof RevlResult.Ok<?, ?> {ok_var}) {{")
    out.append(f"{pad}    {witness_type} result = ({witness_type}) {ok_var}.value();")
    if _RECORD_MODE:
        # item 322 Slice 2: the durable exit. At REGISTRATION (this Ok branch
        # runs during apply(), when the mutation happened) write the
        # discharge-descriptor — the re-issuable named call `recover` replays
        # LIFO to undo the mutation — and fsync it, so a crash BEFORE commit is
        # still recoverable from the log alone. `receiver` is the witnessed
        # extern, `method` its declared inverse, and the stringified witness is
        # the referent argument (the go mirror stringifies `result` the same
        # way). Byte-identical default output: emitted only under `--record`.
        receiver = _string(_call_label(step["acquire"]))
        method = _string(_call_label(ext["undo"]))
        # ...unless the author declared the witness position confidential
        # (`Result[Secret[W], E]`, the shape a fallible lease has to use), in
        # which case the descriptor carries the placeholder instead: the WAL is
        # a plaintext file at rest, and a `Secret[T]` declaration authorises
        # disclosure to the declared receiver, never a durable copy. The stamp
        # is the compiler's (`secret_witness`), so the decision is made once,
        # here, at the point that writes the record — never at each reader.
        referent = (_string(_REDACTED_SECRET) if ext.get("secret_witness")
                    else "String.valueOf(result)")
        out.append(
            f"{pad}    revlRecordTransactional({receiver}, {method}, "
            f"new String[]{{{referent}}});"
        )
    out.append(
        f"{pad}    fx.track({frame_expr}.{entry}({crossing}, {attempted}, "
        f"() -> {undo_expr}));"
    )
    out.append(f"{pad}}}")
    if bind is not None:
        out.append(f"{pad}var {bind} = {result_var};")


def _emit_component_stmts(
    component: dict,
    env: _Env,
    v3_ctx: _V3Ctx,
    cname: str,
    out: list[str],
    steps: list[dict],
    pad: str,
    map_values: dict[str, str] | None = None,
    witnessed: dict[str, dict] | None = None,
    frame_expr: str | None = None,
) -> None:
    for step in steps:
        kind = step.get("step")
        if kind in ("let-effect", "effect"):
            for setup in step.get("setup") or []:
                _emit_setup_stmt(env, v3_ctx, setup, out, pad)
            wit = _witnessed_extern_for(step.get("acquire"), witnessed or {})
            bind = _ident(step["bind"], "binding") if kind == "let-effect" else None
            if wit is not None:
                _emit_witnessed_step(out, pad, step, wit, v3_ctx, env, bind, frame_expr)
                continue
            if kind == "let-effect" and _is_map_cas(step.get("acquire")):
                # item 397: a result-declared host CAS binds an atomic
                # `boolean`; guard the site-spelled undo on it so a `false` CAS
                # registers the identity inverse and teardown never removes the
                # winner's entry. Bind `boolean` (not `var`), matching the
                # method-body path, so a later `if (fresh)` in activation code
                # compiles.
                out.append(
                    f"{pad}boolean {bind} = "
                    f"{_expr(step['acquire'], v3_ctx, None, env)};")
                undo_expr = _expr(step["undo"], v3_ctx, None, env)
                guarded = f"() -> {{ if ({bind}) {{ {undo_expr}; }} }}"
                if frame_expr is None:
                    out.append(f"{pad}fx.track(Disposables.of({guarded}));")
                else:
                    crossing = _string(_call_label(step["acquire"]))
                    attempted = _string(_call_label(step["undo"]))
                    out.append(
                        f"{pad}fx.track({frame_expr}.bracket({crossing}, "
                        f"{attempted}, {guarded}));")
                continue
            if kind == "let-effect":
                # FR-4: a host Map binding pins its value type at the
                # declaration (`Map<...> store = Map.create();`) — `var` would
                # infer `Map<Object>` from the generic static factory and the
                # emitted insert/get calls would lose their static types.
                surface = (map_values or {}).get(bind)
                decl = f"Map<{_java_v3_type(surface, boxed=True)}>" if surface else "var"
                out.append(f"{pad}{decl} {bind} = {_expr(step['acquire'], v3_ctx, None, env)};")
            else:
                out.append(f"{pad}{_expr(step['acquire'], v3_ctx, None, env)};")
            _emit_bracket_track(out, pad, step["acquire"], step["undo"], v3_ctx, env,
                                frame_expr, None, step)
        elif kind == "emit":
            out.append(f"{pad}{_expr(step['expr'], v3_ctx, None, env)};")
            if step.get("compensate") is not None:
                _emit_compensation_track(
                    out, pad, step["expr"], step["compensate"], v3_ctx, env, frame_expr)
        elif kind == "provide":
            key = step.get("name")
            service = step.get("service")
            struct = f"{cname}{_camel(key)}"
            ctor_args = ", ".join(
                ["ctx", "fx"] + (["frame"] if frame_expr else [])
                + list(env.reqs) + list(_binds(component))
                + [_ident(f.get("name"), "config field")
                   for f in _provider_config_fields(component)]
            )
            out.append(
                f"{pad}fx.track(ctx.provide(ServiceKey.of({service}.class), "
                f"new {struct}({ctor_args})));"
            )
        elif kind == "if":
            out.append(f"{pad}if ({_expr(step['cond'], v3_ctx, None, env)}) {{")
            _emit_component_stmts(
                component, env, v3_ctx, cname, out, step.get("then") or [], pad + "    ",
                map_values, witnessed, frame_expr,
            )
            if step.get("else"):
                out.append(f"{pad}}} else {{")
                _emit_component_stmts(
                    component, env, v3_ctx, cname, out, step["else"], pad + "    ",
                    map_values, witnessed, frame_expr,
                )
            out.append(f"{pad}}}")
        elif kind == "fail":
            message = _expr(step["message"], v3_ctx, None, env)
            out.append(
                f"{pad}throw new CordisException(String.valueOf({message}));"
            )
            # A `fail` lowers to an unconditional `throw`, so every sibling
            # step after it in THIS block never runs. Unlike go/rust/py —
            # whose targets tolerate dead code after a return/panic/raise —
            # javac hard-rejects an unreachable statement, so drop the
            # post-fail tail rather than emitting it. Nested blocks (an
            # `if`-branch) recurse through their own call, so this only prunes
            # the failing block's own tail, never a sibling branch that is
            # still reachable. (roadmap item 341 / fault-sweep item 125)
            return
        elif kind == "await":
            out.append(f"{pad}{_await_join(step['expr'], env, v3_ctx)};")
        elif kind == "return":
            raise EmitError("return steps are only allowed inside method bodies")
        else:
            _emit_setup_stmt(env, v3_ctx, step, out, pad)




def _await_join(node: dict, env: _Env, v3_ctx: _V3Ctx | None = None) -> str:
    """An A1 `await` step: evaluate, **join** the result, discard the value.

    Emitting the call alone is not an await. `Job.run("x");` starts the job
    and drops the handle on the floor, so activation returns with the job
    still pending — the boundary the paper (§4.3.2) says the runtime may
    divert at never actually closes. Awaiting something that is not a handle
    is the identity, so an expression the emitter cannot see to be
    asynchronous stays a plain expression statement.
    """
    rendered = _expr(node, v3_ctx, None, env)
    if (isinstance(node, dict) and node.get("kind") == "host"
            and (node.get("fn") or "") in _HOST_AWAITABLE):
        return f"{rendered}.await()"
    return rendered


def _emit_java_router_class(env: "_Env", cname: str, key: str, service_name: str,
                            route: dict, services: dict, render_type, out: list[str]) -> None:
    """item 173: the emitted realization of a routed require on cordis4j,
    mirroring src/revl/run.py::_Router and the go/rust backends.

    A per-(component, key) class implementing the required service interface. It
    holds no worker handle — every method re-resolves the live per-realm handle
    off the cordis4j fork's strict single-realm liveness-checked read
    (`ctx.serviceInRealm(<Svc>.class, realm)`, which returns an empty Optional
    for a realm with no ACTIVE provider and — unlike `ctx.get` — never falls
    back to a parent realm). So a withdrawn worker realm drops out of the live
    set and its calls go to the survivors — reactive failover from the emitted
    body. Wired as the component's handle for the routed key, so a provide
    method's `<key>.<op>(..)` forwards through it (G2: one provider downstream).
    """
    struct = f"RevlRouter{cname}{_camel(key)}"
    realms = list(route.get("realms") or [])
    strategy = route.get("strategy") or "round_robin"
    realm_lits = ", ".join(_string(r) for r in realms)
    methods = (services.get(service_name, {}) or {}).get("methods", {}) or {}
    empty_msg = _string(
        f"revl: router for {key} has no live worker (all realms withdrawn)")

    out.append(f"public static final class {struct} implements {service_name} {{")
    out.append("    private final Context ctx;")
    out.append(f"    private final String[] realms = {{{realm_lits}}};")
    # `cursor` and `served` are MUTABLE router state, and the tier's placement
    # runner serves every bridge connection on its own thread
    # (backends/java/placement/PlacementRunner.java: `new Thread(() ->
    # serveConn(ch), "bridge-conn")`), so two requests can select
    # concurrently. The go router already takes `r.mu.Lock()` for the whole of
    # its select; java took nothing, which is the hazard item 397 closed for
    # the host `Map`. `revlSelect` below is `synchronized`, which is the exact
    # mirror of go's whole-function mutex: every read and write of both fields
    # happens inside it.
    out.append("    private int cursor = 0;")
    # item 433 F3: served counts live in a `long[]` indexed by realm position,
    # not a `Map<String, Long>`. The map boxed a `Long` on every call once a
    # count passed the `Long.valueOf` cache at 127, and hashed the realm label
    # to get there. The array indexes straight off the position the selection
    # loop already has in hand.
    out.append(f"    private final long[] served = new long[{len(realms)}];")
    out.append(f"    {struct}(Context ctx) {{ this.ctx = ctx; }}")
    # item 433 F3: ONE resolution per call. This used to call a `revlLive()`
    # that probed EVERY realm, threw away each handle it had just resolved and
    # kept only the label in a fresh `ArrayList`, then scanned that list with
    # `live.contains(cand)` inside the candidate loop (O(realms^2) string
    # comparisons) and called `ctx.serviceInRealm(..)` a SECOND time for the
    # winner. `backends/go/emit.py`'s `_revlLive` already returned the handles
    # alongside the labels, so java was the tier out of step. The selection
    # loop now keeps the handle it probed. The emitter also knows which
    # strategy the route declares, so only that branch is emitted at all —
    # there is no longer a `strategy` field or a per-call `String.equals`
    # against it.
    out.append(f"    private synchronized {service_name} revlSelect() {{")
    out.append("        int n = realms.length;")
    if strategy == "least_loaded":
        out.append(f"        {service_name} chosen = null;")
        out.append("        int best = -1;")
        out.append("        for (int i = 0; i < n; i++) {")
        out.append(f"            java.util.Optional<{service_name}> hit = "
                   f"ctx.serviceInRealm({service_name}.class, realms[i]);")
        out.append("            if (hit.isEmpty()) continue;")
        out.append("            if (best < 0 || served[i] < served[best]) {")
        out.append("                best = i;")
        out.append("                chosen = hit.get();")
        out.append("            }")
        out.append("        }")
        out.append(f"        if (best < 0) throw new CordisException({empty_msg});")
        out.append("        served[best]++;")
        out.append("        return chosen;")
    else:
        out.append("        for (int off = 0; off < n; off++) {")
        out.append("            int at = (cursor + off) % n;")
        out.append(f"            java.util.Optional<{service_name}> hit = "
                   f"ctx.serviceInRealm({service_name}.class, realms[at]);")
        out.append("            if (hit.isPresent()) {")
        out.append("                cursor = (at + 1) % n;")
        out.append("                served[at]++;")
        out.append("                return hit.get();")
        out.append("            }")
        out.append("        }")
        out.append(f"        throw new CordisException({empty_msg});")
    out.append("    }")
    for mname, decl in methods.items():
        jname = _ident(mname, "method")
        params_decl = decl.get("params", []) or []
        params = ", ".join(f"{render_type(p.get('type'))} {_ident(p.get('name'), 'parameter')}"
                           for p in params_decl)
        ret = render_type(decl.get("returns")) if decl.get("returns") else "void"
        args = ", ".join(_ident(p.get("name"), "parameter") for p in params_decl)
        out.append(f"    public {ret} {jname}({params}) {{")
        call = f"revlSelect().{jname}({args})"
        out.append(f"        {'return ' if ret != 'void' else ''}{call};")
        out.append("    }")
    out.append("}")
    out.append("")


def _emit_component_modern(
    component: dict,
    services: dict,
    types: dict | None = None,
    functions: list | None = None,
    externs: list | None = None,
    components: list | None = None,
    *,
    render_type=_java_type,
) -> list[str]:
    env = _Env(component, services)
    v3_ctx = _V3Ctx(types or {}, functions or [], externs or [], components or [])
    # FR-4: bind -> revl surface value type for each host Map binding, so
    # provider fields, constructor parameters and the apply() local all pin
    # `Map<V>` instead of leaving `Map` raw (the session ledger is
    # `Map[Str, List[Msg]]`).
    map_values = _component_map_values(env, functions or [], v3_ctx)
    name = component["name"]
    cname = _ident(name, "component")
    isolate = component.get("isolate") or {}
    intercept = component.get("intercept") or {}
    has_await = any(
        step.get("step") == "await" for step in component.get("body") or []
    )
    plugin_interface = "AsyncPlugin" if has_await else "Plugin"
    # docs/design/teardown-contract.md: a component that registers at least
    # one transactional (witnessed, 243) or compensation (247) entry needs
    # the RevlFrame two-phase loop; a bracket-only component keeps emitting
    # exactly as before (see `_component_needs_frame`).
    witnessed = _witnessed_externs(externs)
    needs_frame = _component_needs_frame(component, witnessed)
    frame_expr = "frame" if needs_frame else None
    provider_config = _provider_config_fields(component)

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
        if needs_frame:
            out.append("    private final RevlFrame frame;")
        for local, service in env.reqs.items():
            out.append(f"    private final {service} {local};")
        for b in _binds(component):
            btype = _bind_type(component, b, v3_ctx, map_values)
            out.append(f"    private final {btype} {b};")
        # A provide method that reads `config.<f>` needs the field HERE, not
        # only on the plugin (see `_provider_config_fields`).
        for f in provider_config:
            out.append(f"    private final {_java_v3_type(f.get('type'))} "
                       f"{_ident(f.get('name'), 'config field')};")
        ctor_params = ", ".join(
            ["Context ctx", "Context.EffectScope fx"]
            + (["RevlFrame frame"] if needs_frame else [])
            + [f"{service} {local}" for local, service in env.reqs.items()]
            + [f"{_bind_type(component, b, v3_ctx, map_values)} {b}" for b in _binds(component)]
            + [f"{_java_v3_type(f.get('type'))} {_ident(f.get('name'), 'config field')}"
               for f in provider_config]
        )
        out.append(f"    {struct}({ctor_params}) {{")
        out.append("        this.ctx = ctx;")
        out.append("        this.fx = fx;")
        if needs_frame:
            out.append("        this.frame = frame;")
        for local in env.reqs:
            out.append(f"        this.{local} = {local};")
        for b in _binds(component):
            out.append(f"        this.{b} = {b};")
        for f in provider_config:
            fname = _ident(f.get("name"), "config field")
            out.append(f"        this.{fname} = {fname};")
        out.append("    }")
        provide = next(
            (s for s in component.get("body") or []
             if s.get("step") == "provide" and s.get("name") == key),
            {"methods": []},
        )
        for method in provide.get("methods") or []:
            mname = _ident(method.get("name"), "method")
            # Provider-method signatures MUST render with the SAME renderer as
            # the service interface this class implements (`render_type`:
            # `_java_type` for IR v1/v2, `_java_v3_type` for v3) — the exact
            # `f(R)`-implemented-by-`f(Object)` contract `_emit_component`
            # documents. The modern path used to hardcode `_java_v3_type`, so a
            # v1/v2 component forced onto this path by a `fail`/`if`/`await`
            # (e.g. the 125 fault sweep injecting a `fail`) rendered an
            # undeclared v1 type literally (`List<Row>`) while its interface
            # erased it to `List<Object>` — a javac "cannot find symbol" (Row)
            # and a signature mismatch. Threading `render_type` here erases it
            # consistently. Byte-identical for v3 (`render_type` IS
            # `_java_v3_type`) and for any v1/v2 program using only declared
            # types (both renderers agree via TYPE_MAP).
            params = ", ".join(
                f"{render_type(_param_type(env, key, mname, p))} {p}"
                for p in method.get("params") or []
            )
            ret = render_type(_method_return(env, key, mname)) if _method_return(env, key, mname) else "void"
            out.append(f"    public {ret} {mname}({params}) {{")
            for line in _method_body_lines(
                env, method, v3_ctx, returns_void=ret == "void", frame_expr=frame_expr,
            ):
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
    if needs_frame:
        # docs/design/teardown-contract.md: one frame per activation, created
        # BEFORE any step runs so every bracket/transactional/compensation
        # registration below shares the same commit/abort discriminator.
        out.append("        RevlFrame frame = new RevlFrame();")
    for local, service in env.reqs.items():
        if local in env.routes:
            # item 173: a routed require resolves per named realm through the
            # emitted router, never a single committed-view `ctx.get`.
            out.append(f"        {service} {local} = "
                       f"new RevlRouter{cname}{_camel(local)}(ctx);")
            continue
        out.append(f"        {service} {local} = ctx.get({service}.class);")
    # A8 self-revert: cordis4j's ctx.effect() scope is NOT owned by the
    # fiber until apply returns it, so a failing activation must dispose
    # the accumulated effects itself before the failure routes to the
    # runtime (verified against the real runtime in RunRealScenarios).
    out.append("        try {")
    body_lines: list[str] = []
    _emit_component_stmts(
        component, env, v3_ctx, cname, body_lines, component.get("body") or [],
        "            ", map_values, witnessed, frame_expr,
    )
    out.extend(body_lines)
    body_steps = component.get("body") or []
    if not any(s.get("step") == "fail" for s in body_steps):
        # An unconditional top-level `fail` lowers to a throw; Java treats any
        # statement after it (here the commit/return tail) as a hard
        # "unreachable statement" error. A `fail` anywhere at the top level of
        # the body — not only as the last step (item 341 / fault-sweep item
        # 125) — makes this tail unreachable, so skip it whenever one is
        # present. A `fail` nested inside an `if`-branch is conditional and
        # does not reach here.
        if needs_frame:
            # Commit path (docs/design/teardown-contract.md): flip the
            # discriminator BEFORE any disposal can occur — activation
            # completed cleanly, so every bracket still runs at teardown and
            # every transactional/compensation entry discharges instead of
            # replaying.
            out.append("            frame.commit();")
        if needs_frame:
            # item 318: return the frame-bearing Disposable so a session-level
            # reject can reach `frame.abort()` after a clean activation, and so
            # the unload drains Phase-2 compensations (a no-op on commit). A
            # bracket-only / non-frame component still returns the bare `fx`.
            out.append("            return new RevlActivation(fx, frame);")
        else:
            out.append("            return fx;")
    out.append("        } catch (RuntimeException | Error failure) {")
    out.append("            fx.dispose();")
    if needs_frame:
        # Abort path: fx.dispose() just ran Phase 1 (every bracket and
        # transactional inverse, LIFO, continue-and-record — compensation
        # disposers only enqueued, they did not run inline). Phase 2 starts
        # only now, after Phase 1 has run to completion.
        out.append("            frame.runPhase2();")
    out.append("            throw failure;")
    out.append("        }")
    out.append("    }")
    out.append("}")
    out.append("")

    # item 173: one router class per routed require, implementing the required
    # service interface by strict per-realm resolution + strategy + failover.
    for rkey, route in env.routes.items():
        _emit_java_router_class(
            env, cname, rkey, env.reqs[rkey], route, services, render_type, out)
    return out



def _emit_component(
    component: dict,
    services: dict,
    types: dict | None = None,
    functions: list | None = None,
    externs: list | None = None,
    components: list | None = None,
    *,
    render_type=_java_type,
) -> list[str]:
    """`render_type` MUST be the renderer that produced the service
    interfaces this component's provider classes implement: `_java_type` for
    IR v1/v2 (`_emit_service_interfaces`), `_java_v3_type` for IR v3
    (`_emit_service_interfaces_v3`). Rendering a signature with one and its
    override with the other is how `f(R)` came to be implemented by
    `f(Object)`, which javac reports as the class not being abstract.
    """
    if _component_needs_modern(component):
        return _emit_component_modern(
            component, services, types, functions, externs, components,
            render_type=render_type)
    env = _Env(component, services)
    # FR-4: bind -> revl surface value type for each host Map binding (the
    # legacy path builds its own lightweight v3 context when the document
    # declares types, so record-valued maps resolve their names).
    v3_ctx = (
        _V3Ctx(types or {}, functions or [], externs or [], components or [])
        if types else None
    )
    map_values = _component_map_values(env, functions or [], v3_ctx)
    name = component["name"]
    cname = _ident(name, "component")
    out: list[str] = []

    for key, service in env.provides.items():
        _ident(key, "provision")
        struct = f"{cname}{_camel(key)}"
        out.append(f"public static final class {struct} implements {service} {{")
        # `requires` bindings are captured exactly like `let-effect` binds:
        # a final field, assigned from the constructor. `apply` resolves them
        # with `ctx.get(...)` into locals of its own, so a method body that
        # reaches one had nothing in scope to name until the provider class
        # held it too (the rust/TypeScript instances of this same bug).
        for local, req_service in env.reqs.items():
            out.append(f"    private final {req_service} {_ident(local, 'requirement')};")
        for b in _binds(component):
            out.append(
                f"    private final {_bind_decl_type(component, b, render_type, map_values)} {b};")
        ctor_args = ", ".join(
            [f"{req_service} {_ident(local, 'requirement')}"
             for local, req_service in env.reqs.items()]
            + [f"{_bind_decl_type(component, b, render_type, map_values)} {b}"
               for b in _binds(component)]
        )
        out.append(f"    {struct}({ctor_args}) {{")
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
                f"{render_type(_param_type(env, key, mname, p))} {p}"
                for p in method.get("params") or []
            )
            ret = render_type(_method_return(env, key, mname)) if _method_return(env, key, mname) else "void"
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
            out.append(
                f"            {_bind_decl_type(component, step['bind'], render_type, map_values)} "
                f"{bind} = {_expr(step['acquire'], None, None, env)};")
            undo = _expr(step['undo'], None, None, env)
            out.append(f"            undos.add(Disposables.of(() -> {undo}));")
            disposers.append(bind)
        elif kind == "effect":
            out.append(f"            {_expr(step['acquire'], None, None, env)};")
            out.append(f"            undos.add(Disposables.of(() -> {_expr(step['undo'], None, None, env)}));")
            disposers.append("effect")
        elif kind == "emit":
            out.append(f"            {_expr(step['expr'], None, None, env)};")
        elif kind == "provide":
            key = step.get("name")
            service = step.get("service")
            struct = f"{cname}{_camel(key)}"
            ctor_args = ", ".join(list(env.reqs) + list(_binds(component)))
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
    if _uses_equality(ir):
        out.extend(["    " + line if line else line for line in _emit_eq_helper()])
    # item 433 F6: reserve the slot, then splice in only the helpers the rest
    # of the unit actually calls once it has all been emitted.
    stdlib_at = len(out)
    if _uses_float_interp(ir):
        out.extend(["    " + line if line else line for line in _emit_ftoa_helper()])
    if _uses_revl_frame(ir):
        out.extend(["    " + line if line else line for line in _emit_revl_frame_runtime()])
    for component in components:
        out.extend(["    " + line if line else line for line in _emit_component(component, ir.get("services") or {})])
    out[stdlib_at:stdlib_at] = [
        "    " + line if line else line
        for line in _emit_stdlib_helpers(_stdlib_helpers_reached(out[stdlib_at:]))
    ]
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
    if _uses_equality(ir):
        out.extend(["    " + line if line else line for line in _emit_eq_helper()])
    # item 433 F6: reserve the slot, then splice in only the helpers the rest
    # of the unit actually calls once it has all been emitted.
    stdlib_at = len(out)
    if _uses_float_interp(ir):
        out.extend(["    " + line if line else line for line in _emit_ftoa_helper()])
    if _uses_revl_frame(ir):
        out.extend(["    " + line if line else line for line in _emit_revl_frame_runtime()])
    for component in components:
        out.extend(["    " + line if line else line for line in _emit_component(component, ir.get("services") or {})])
    out[stdlib_at:stdlib_at] = [
        "    " + line if line else line
        for line in _emit_stdlib_helpers(_stdlib_helpers_reached(out[stdlib_at:]))
    ]
    out.append("}")
    return "\n".join(out).rstrip() + "\n"


def _uses_spawn(ir: dict) -> bool:
    """True when any component body contains a `spawn` acquisition node — gates
    the `RevlSpawnHandle` helper. Non-spawning documents skip it entirely and
    stay byte-identical to the pre-feature output (v1 goldens unaffected)."""
    def walk(node) -> bool:
        if isinstance(node, dict):
            if node.get("kind") == "spawn":
                return True
            return any(walk(value) for value in node.values())
        if isinstance(node, list):
            return any(walk(value) for value in node)
        return False

    return walk(ir.get("components"))


def _uses_instance_get(ir: dict) -> bool:
    """True when any component body reads a provision back off a spawn handle
    (`instance-get`, the instance accessor `s.<key>`). It gates the extra
    context-holding capability on `RevlSpawnHandle`: a spawn-only document keeps
    the pre-accessor handle verbatim, so its output stays byte-identical (the
    accessor is a strict superset added only where a document uses it)."""
    def walk(node) -> bool:
        if isinstance(node, dict):
            if node.get("kind") == "instance-get":
                return True
            return any(walk(value) for value in node.values())
        if isinstance(node, list):
            return any(walk(value) for value in node)
        return False

    return walk(ir.get("components"))


def _emit_spawn_handle(with_get: bool = False) -> list[str]:
    """The value a `spawn` acquisition binds: a live component instance
    (docs/design-v2-instances.md, phase 1), reclaimed by its own `dispose()`.

    When `with_get` (the document uses the instance accessor `s.<key>`), the
    handle also stores the instance's fork()-isolated child `Context` and
    exposes `get(<Svc>.class)`, which resolves a provision through THAT realm —
    the runtime side of `instance-get`. It is gated so a spawn-only document
    keeps the original handle byte-for-byte.

    `spawn` plugs the target template as a CHILD instance of the spawner: each
    key it provides is isolated into a FRESH LOCAL realm (a per-spawn-unique
    label — so two instances of one component coexist without a duplicate-key
    collision, and only the spawner, holding the handle, reaches its instance),
    and the template is applied on that isolated context. The returned handle
    owns the instance's own teardown scope. `dispose()` runs that scope's LIFO
    teardown NOW, independent of the spawner — a request-scoped instance is
    reclaimed when the request ends, not deferred to the component's teardown.
    It is idempotent (the instance disposable is taken exactly once), so the
    spawner's own `undo` inverse — and the parent scope's safety net that stops
    an un-disposed instance outliving its spawner — are harmless no-ops once the
    instance is already gone."""
    lines = [
        "/** A live spawned-component instance (docs/design-v2-instances.md). */",
        "static final class RevlSpawnHandle {",
        "    private static final java.util.concurrent.atomic.AtomicLong SPAWN_SEQ =",
        "        new java.util.concurrent.atomic.AtomicLong();",
        "    private final java.util.concurrent.atomic.AtomicReference<Disposable> instance;",
    ]
    if with_get:
        lines += [
            "    // The instance's fork()-isolated child context — the realm each",
            "    // provided key was isolated into. The accessor `s.<key>` resolves",
            "    // a provision back through it (supervision-tree addressing).",
            "    private final Context ctx;",
            "",
            "    private RevlSpawnHandle(Disposable instance, Context ctx) {",
            "        this.instance = new java.util.concurrent.atomic.AtomicReference<>(instance);",
            "        this.ctx = ctx;",
            "    }",
        ]
    else:
        lines += [
            "",
            "    private RevlSpawnHandle(Disposable instance) {",
            "        this.instance = new java.util.concurrent.atomic.AtomicReference<>(instance);",
            "    }",
        ]
    lines += [
        "",
        "    /** Plug `plugin` as a child instance: each provided service is",
        "     * isolated into a fresh local realm (a per-spawn-unique label, so",
        "     * two instances never collide), then the template is applied on",
        "     * that isolated context. */",
        "    static RevlSpawnHandle spawn(Context ctx, Plugin plugin, Class<?>[] realms) {",
        "        String realm = \"revl$spawn$\" + SPAWN_SEQ.getAndIncrement();",
        "        Context child = ctx;",
        "        for (Class<?> service : realms) {",
        "            child = child.isolate(service, realm);",
        "        }",
    ]
    if with_get:
        lines += [
            "        return new RevlSpawnHandle(plugin.apply(child), child);",
        ]
    else:
        lines += [
            "        return new RevlSpawnHandle(plugin.apply(child));",
        ]
    lines += [
        "    }",
        "",
        "    /** Reclaim the instance now — run its own LIFO teardown. Idempotent. */",
        "    long dispose() {",
        "        Disposable taken = instance.getAndSet(null);",
        "        if (taken != null) {",
        "            taken.dispose();",
        "        }",
        "        return 0L;",
        "    }",
    ]
    if with_get:
        lines += [
            "",
            "    /** Read a provision the instance published, in ITS local realm:",
            "     * `s.<key>` -> `get(<Svc>.class)`. Only the spawner holding this",
            "     * handle reaches it — a sibling (a different realm) and the root",
            "     * cannot (supervision-tree addressing, docs/design-v2-instances.md). */",
            "    <T> T get(Class<T> service) {",
            "        return ctx.get(service);",
            "    }",
        ]
    lines += [
        "}",
        "",
    ]
    return lines


def _emit_v3(ir: dict, package_name: str) -> str:
    services = ir.get("services") or {}
    components = ir.get("components") or []
    types = ir.get("types") or {}
    functions = ir.get("functions") or []
    externs = ir.get("externs") or []
    tests = ir.get("tests") or []
    if not components and not types and not functions and not externs and not tests:
        raise EmitError("IR document has no components, types, functions, externs, or tests")
    _check_fn_name_collisions(functions, externs)

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
    # The generated record/case `equals`/`hashCode` call these too, so a
    # document that declares a type with a reference-typed component needs them
    # even when it never writes `==` itself.
    needs_value_helpers = _v3_types_need_value_helpers(types)
    if _uses_equality(ir) or needs_value_helpers:
        out.extend(["    " + line if line else line for line in _emit_eq_helper()])
    if needs_value_helpers:
        out.extend(["    " + line if line else line for line in _emit_hash_helper()])
    # item 433 F6: reserve the slot, then splice in only the helpers the rest
    # of the unit actually calls once it has all been emitted.
    stdlib_at = len(out)
    if _uses_float_interp(ir):
        out.extend(["    " + line if line else line for line in _emit_ftoa_helper()])
    if _uses_checked_div(ir):
        out.extend(["    " + line if line else line for line in _emit_checked_div_helpers()])
    if externs:
        out.extend(["    " + line if line else line for line in _emit_v3_externs(externs)])
    if functions:
        out.extend(["    " + line if line else line for line in _emit_v3_functions(functions, types, externs)])
    if tests:
        # item 178(b): a `lifecycle test` lowers to the live-composition driver,
        # a plain `test` to a static method; both land on the one `REVL_TESTS`
        # roster so the JVM runner cannot run one kind and miss the other.
        lifecycle = [t for t in tests if t.get("lifecycle")]
        pure = [t for t in tests if not t.get("lifecycle")]
        lifecycle_runners: list[str] = []
        if lifecycle:
            lifecycle_lines, lifecycle_runners = _emit_v3_lifecycle_tests(
                lifecycle, types, functions, externs, services, components)
            out.extend(["    " + line if line else line for line in lifecycle_lines])
        out.extend(["    " + line if line else line
                    for line in _emit_v3_tests(pure, types, functions, externs,
                                               extra_runners=lifecycle_runners)])
    if _uses_spawn(ir):
        out.extend(["    " + line if line else line
                    for line in _emit_spawn_handle(with_get=_uses_instance_get(ir))])
    if _uses_revl_frame(ir):
        out.extend(["    " + line if line else line for line in _emit_revl_frame_runtime()])
        # item 322 Slice 2: the durable WAL sink rides alongside the teardown
        # frame, but ONLY under `--record` — off, this whole block is absent and
        # the output is byte-identical (the golden oracle + selfhost gate).
        if _RECORD_MODE:
            out.extend(["    " + line if line else line for line in _emit_record_sink()])
    for component in components:
        out.extend(["    " + line if line else line for line in _emit_component(
            component, services, types, functions, externs, components,
            render_type=_java_v3_type)])
    out[stdlib_at:stdlib_at] = [
        "    " + line if line else line
        for line in _emit_stdlib_helpers(_stdlib_helpers_reached(out[stdlib_at:]))
    ]
    out.append("}")
    return "\n".join(out).rstrip() + "\n"


# ------------------------------------------------------------ typed holes

def _refuse_holes(ir: dict) -> None:
    """A typed hole is an unmet obligation, not code (docs/holes.md).

    Emitting one would put a placeholder into Java and make javac the
    thing that complains — in its own vocabulary, about a line revl wrote.
    revl already knows the draft is unfinished, so the refusal belongs
    here, before a single character is emitted.
    """
    found: list = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("kind") == "hole":
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for section in ("components", "functions", "tests", "externs"):
        walk(ir.get(section))
    if not found:
        return
    where = ", ".join(
        f"{h.get('file') or '?'}:{h.get('line') or '?'} "
        f"(expects `{h.get('type')}`)" for h in found[:3])
    if len(found) > 3:
        where += f", and {len(found) - 3} more"
    raise EmitError(
        f"refusing to emit Java: this document still has {len(found)} typed "
        f"hole(s) — {where}. A hole type-checks so the surrounding draft can "
        f"be checked, but it has no implementation and there is nothing to "
        f"lower. Fill every hole, then emit (docs/holes.md)."
    )

# A `fault test` is executed by driving a real activation and inspecting the
# runtime's residue afterwards (docs/fault-tests.md).  The cordis4j tier
# has no such driver, so it is refused loudly instead of being dropped on the
# floor: a silently-missing fault test is a guarantee nobody is checking.
def _refuse_fault_tests(ir) -> None:
    fault_tests = (ir or {}).get("fault_tests") or []
    if not fault_tests:
        return
    names = ", ".join(repr(unit.get("name")) for unit in fault_tests)
    raise EmitError(
        f"fault tests do not lower to the cordis4j tier ({names}) — `fault test` runs "
        f"on the python reference tier only (docs/fault-tests.md). Compile "
        f"this document with --backend py, or move the fault tests to a "
        f"module that is not emitted for this tier."
    )

def _refuse_lifecycle_tests(tests: list) -> None:
    """`lifecycle test` blocks (syntax-2.0 §7.1) on the pre-v3 dialects.

    A lifecycle test is not a pure test unit: it loads components into a live
    context, calls through provision keys, unloads them, and asserts
    residue-freedom by reading the host runtime back (R1/R4,
    docs/backend-ir.md). This tier drives that round-trip on ir_version 3
    (`_emit_v3_lifecycle_tests`, item 178(b)); ir_version 1 and 2 have no test
    machinery at all here, so a lifecycle test in one is refused BY NAME rather
    than dropped — a construct silently dropped by one renderer and present in
    another is this project's recurring bug class.
    """
    for test in tests or []:
        if test.get("lifecycle"):
            raise EmitError(
                f"lifecycle test {test.get('name')!r} is not lowerable on the {CRATE} tier "
                f"below ir_version 3: it drives a live composition "
                "(load/call/unload) and asserts R4 residue-freedom by reading the "
                "host runtime back, which this tier does only for ir_version 3 — "
                "run it with `revl test --backend py` (docs/syntax-2.0.md §7.1)"
            )


def _refuse_deferred_emissions(ir: dict) -> None:
    """Roadmap 245 Decision 2 tier gate: a CALL to a `deferred` emission needs a
    session-owner runtime (the deferral queue and the commit verb) this tier does
    not have yet, so refuse it at emit time — surfaced through EmitError, this
    tier's existing refusal channel. The reachability check and the single
    canonical wording live in `revl.session_commit`, shared by all five ownerless
    tiers so six backends do not invent six messages; a declared-but-never-called
    deferred extern emits cleanly (call-site keyed)."""
    try:
        from revl.errors import RevlError
        from revl.session_commit import (
            refuse_approval_on_ownerless_tier,
            refuse_deferred_on_ownerless_tier,
        )
    except ModuleNotFoundError:  # standalone `python3 emit.py` — put src/ on the path
        import pathlib
        import sys as _sys
        src = pathlib.Path(__file__).resolve().parents[2] / "src"
        if src.is_dir() and str(src) not in _sys.path:
            _sys.path.insert(0, str(src))
        from revl.errors import RevlError
        from revl.session_commit import (
            refuse_approval_on_ownerless_tier,
            refuse_deferred_on_ownerless_tier,
        )
    try:
        refuse_deferred_on_ownerless_tier(ir, "java")
        refuse_approval_on_ownerless_tier(ir, "java")
    except RevlError as exc:
        raise EmitError(exc.message) from None


_REVL_SYNC_SUFFIX = "_revl_sync"


def _dedup_colour_erased_poly_externs(ir: dict) -> dict:
    """item 388, stage 6: on a colour-erasing tier (go/rust/java/wasm — suspension
    is not a function colour) a caller-decided-colour extern's two clones — `X`
    (async) and `X_revl_sync` (sync) — emit the SAME blocking host function.
    Collapse them to ONE: drop the sync clone and rewrite its call sites to `X`.

    Detected structurally: a `_revl_sync` extern whose origin twin is present with
    identical `bodies`. A poly extern instantiated in only one colour has no twin,
    so it is emitted unchanged under whatever name survived. Non-destructive (the
    shared IR is also emitted by py/ts, which keep both colours), and a no-op that
    returns the IR untouched when no such pair exists (every existing golden is
    byte-identical)."""
    externs = ir.get("externs") or []
    by_name = {e.get("name"): e for e in externs}
    alias: dict = {}
    kept: list = []
    for e in externs:
        name = e.get("name") or ""
        if name.endswith(_REVL_SYNC_SUFFIX):
            origin = name[: -len(_REVL_SYNC_SUFFIX)]
            twin = by_name.get(origin)
            if twin is not None and twin.get("bodies") == e.get("bodies"):
                alias[name] = origin
                continue
        kept.append(e)
    if not alias:
        return ir

    def _rewrite(node):
        if isinstance(node, dict):
            return {k: (alias[v] if k == "name" and isinstance(v, str)
                        and v in alias else _rewrite(v))
                    for k, v in node.items()}
        if isinstance(node, list):
            return [_rewrite(x) for x in node]
        return node

    ir = dict(ir)
    ir["externs"] = kept
    for key in ("components", "functions", "tests", "prop_tests"):
        if key in ir:
            ir[key] = _rewrite(ir[key])
    return ir


def emit(ir: dict, package_name: str = "revl", record: bool = False) -> str:
    """Emit one Java source file for an IR document (ir_version 1, 2, or 3).

    `record` (item 322 Slice 2) additionally emits the durable WAL recording
    sink and the per-descriptor `revlRecordTransactional` calls at each witnessed
    transactional registration. Default False -> byte-identical to the pre-feature
    output, so the golden oracle and the selfhost mirror (both record off) are
    unaffected. Mirrors backends/go/emit.py's `--record`."""
    if not isinstance(ir, dict):
        raise EmitError("IR document must be a dict")
    ir = _dedup_colour_erased_poly_externs(ir)  # item 388, stage 6
    _refuse_holes(ir)
    _refuse_deferred_emissions(ir)

    _refuse_fault_tests(ir)

    global _RECORD_MODE, _LIFECYCLE_MODE, _V3_DECLARED_TYPES
    saved = _RECORD_MODE
    saved_lifecycle = _LIFECYCLE_MODE
    saved_types = _V3_DECLARED_TYPES
    _RECORD_MODE = record
    _LIFECYCLE_MODE = any(t.get("lifecycle") for t in (ir.get("tests") or []))
    _V3_DECLARED_TYPES = frozenset(ir.get("types") or {})
    try:
        version = ir.get("ir_version")
        if version == 1:
            _refuse_lifecycle_tests(ir.get("tests") or [])
            return _emit_v1(ir, package_name)
        if version == 2:
            _refuse_lifecycle_tests(ir.get("tests") or [])
            return _emit_v2(ir, package_name)
        if version == 3:
            return _emit_v3(ir, package_name)
        raise EmitError(
            f"unsupported ir_version: {version!r} — the Java backend targets "
            f"ir_version 1, 2, and 3"
        )
    finally:
        _RECORD_MODE = saved
        _LIFECYCLE_MODE = saved_lifecycle
        _V3_DECLARED_TYPES = saved_types


def _main(argv: list[str]) -> int:
    # `--record` (item 322 Slice 2) wires the witnessed teardown to a durable
    # WAL sink; an optional package name follows the IR path (default `revl`).
    rest = [a for a in argv[1:] if a != "--record"]
    record = "--record" in argv[1:]
    if len(rest) not in (1, 2):
        print("usage: python3 emit.py <ir.json|-> [package] [--record]", file=sys.stderr)
        return 2
    # `-` reads the IR from stdin. Callers used to pass `/dev/stdin`, which
    # works on macOS and fails on a GitHub runner with `OSError: [Errno 6] No
    # such device or address` — the emitted-code tests were red in CI for that
    # reason alone.
    if rest[0] == "-":
        ir = json.load(sys.stdin)
    else:
        with open(rest[0], "r", encoding="utf-8") as handle:
            ir = json.load(handle)
    package = rest[1] if len(rest) == 2 else "revl"
    sys.stdout.write(emit(ir, package, record=record))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))





