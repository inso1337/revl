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

Documented limits (tracked in docs/v2.0-roadmap.md):

- Host objects (`Pool`/`Map`/`Job`) are a minimal real runtime: `Map` is a
  thread-safe `HashMap<String, String>`, `Pool` is a real bounded connection
  pool over a deterministic in-memory database, and `Job::run` returns a real
  cancellable future.  Pool/Job semantics are defined once for every tier in
  backends/python/runtime.py under ".. _pool-job-semantics:".
- Component `await` steps lower to `plugin_async` + `.await`; cordis-rs drives
  the activation future for the fiber (A1).
- Config `default` values are emitted on `impl Default for <Comp>Config`, and
  the plugin body reconstructs its local config through `..Default::default()`.

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
    "Int32": "i32",
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
        "run": ("String", ["String"]),
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
    "a declared function type ({name}) is not lowerable on the Rust tier. "
    "Rust has no single type for \"a callable\": a parameter wants `impl "
    "Fn(..)`, a return wants `impl Fn(..)` tied to one concrete closure, and a "
    "struct field or a `Vec` element wants `Box<dyn Fn(..)>` with an explicit "
    "lifetime — three different lowerings whose choice depends on the position "
    "and on whether the value escapes. revl does not carry that distinction, "
    "so the emitter cannot pick one without guessing. Arrows bound to a local "
    "`let` and called in the same function still lower (rustc infers the "
    "closure type); it is only a function type *written in a declaration* that "
    "is refused. See docs/function-types.md."
)


def _reject_fn_type(name: object) -> None:
    if _is_fn_type(name):
        raise EmitError(_FN_TYPE_REFUSAL.format(name=name))

def _rust_type(name: object, types: dict | None = None) -> str:
    """Surface type -> Rust type. Unknown named types map to cordis `Value`.

    When `types` is supplied (IR v3), user record/variant names are mapped to
    their emitted Rust type names instead of the opaque `Value` fallback.
    """
    if not isinstance(name, str) or not name:
        return "Value"
    _reject_fn_type(name)
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
        if ch in "[(":
            depth += 1
        elif ch in "])":
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
    """A Rust double-quoted string literal, escaped *from code points*.

    The IR stores a `Str` literal as Unicode scalar values (docs/strings.md).
    Rust source is UTF-8 and spells a non-ASCII scalar as `\\u{XXXX}` — it
    rejects the lone-surrogate `\\uXXXX` escapes `json.dumps` emits, which is
    why every non-ASCII literal used to fail to compile on this tier. ASCII is
    byte-identical to the old `json.dumps` output (printable ASCII verbatim;
    `\\n`/`\\r`/`\\t`/`\\"`/`\\\\` the same), so v1 goldens stay frozen.

    A non-`str` input (e.g. a list of requirement names) keeps the old
    `json.dumps` serialization unchanged — only real `Str` values take the
    code-point path.
    """
    if not isinstance(value, str):
        return json.dumps(value)
    out = ['"']
    for ch in value:
        cp = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif 0x20 <= cp <= 0x7E:
            out.append(ch)
        else:
            out.append("\\u{%x}" % cp)
    out.append('"')
    return "".join(out)


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


def _default_for_rust_type(ftype: str) -> str:
    """Return a conservative Rust default expression for a config field type."""
    if ftype == "String":
        return "String::new()"
    if ftype == "i64":
        return "0i64"
    if ftype == "i32":
        return "0i32"
    if ftype == "f64":
        return "0f64"
    if ftype == "bool":
        return "false"
    if ftype == "Vec<u8>":
        return "Vec::new()"
    if ftype == "()":
        return "()"
    return "Default::default()"


def _config_default_lit(value: object, ftype: str) -> str:
    """Render an IR config default (or a type fallback) as a Rust expression."""
    if value is None:
        return _default_for_rust_type(ftype)
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
    raise EmitError(f"unsupported config default value: {value!r}")


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

    def __init__(
        self,
        component: dict,
        services: dict,
        types: dict | None = None,
        functions: list | None = None,
        externs: list | None = None,
        components: list | None = None,
    ):
        self.component = component
        self.services = services
        self.name = component["name"]
        self.reqs: dict[str, str] = dict(component.get("requires") or {})
        self.provides: dict[str, str] = dict(component.get("provides") or {})
        self.types: dict = types or {}
        self.functions: list = functions or []
        self.externs: list = externs or []
        # Every component in the document, so a `spawn` acquisition can resolve
        # its target template's config shape (whether it takes a typed
        # `<Comp>Config` or unit `()`), which the shared expression renderer
        # needs to build the plug-time config value.
        self.components: list = components or []
        self._v3_ctx: _V3Ctx | None = None

    def v3_ctx(self) -> _V3Ctx:
        if self._v3_ctx is None:
            self._v3_ctx = _V3Ctx(self.types, self.functions, self.externs,
                                  self.components)
        return self._v3_ctx


def _expr(node: dict, env: _Env, rename: dict[str, str] | None = None) -> str:
    """Lower a component-body expression via the single Rust expression renderer.

    Component bodies carry a per-scope ``rename`` map (``pool`` -> ``self.pool``
    for captured effects/requirements); the shared type table lives on
    ``env.v3_ctx()``. Both are handed to ``_render_expr``, which is the one
    function that lowers every IR expression kind on this tier.
    """
    return _render_expr(node, env.v3_ctx(), rename or {})


def _format(template: str, args: list[str]) -> str:
    # IR format templates use `$0`/`$1` placeholders and `$$` for a literal `$`
    # (A4). Map them onto Rust `format!` `{0}`/`{1}`. Literal text must have
    # its braces doubled or `format!` reads them as (invalid) format specs.
    def literal(text: list[str]) -> str:
        return "".join(text).replace("{", "{{").replace("}", "}}")

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
                pieces.append(literal(buf) + "{" + template[i + 1 : j] + "}")
                buf = []
                i = j
                continue
        buf.append(ch)
        i += 1
    pieces.append(literal(buf))
    return f"format!({_string(''.join(pieces))}, {', '.join(args)})"


def _emit_service_traits(services: dict, types: dict | None = None) -> list[str]:
    types = types or {}
    out: list[str] = []
    for sname, service in services.items():
        _ident(sname, "service")
        out.append(f"pub trait {sname}: Send + Sync {{")
        for mname, method in (service.get("methods") or {}).items():
            _ident(mname, "method")
            params = ", ".join(
                f"{_ident(p.get('name'), 'parameter')}: {_rust_type(p.get('type'), types)}"
                for p in method.get("params") or []
            )
            ret = _rust_type(method.get("returns"), types) if method.get("returns") else "()"
            if method.get("emission"):
                out.append("    /// emission: crosses the system boundary (DESIGN.md §3.5)")
            out.append(f"    fn {mname}(&self, {params}) -> {ret};")
        out.append("}")
        out.append("")
    return out


def _emit_host_stubs(ir: dict) -> list[str]:
    """Emit the minimal revl host runtime for objects used by this document.

    The objects are intentionally small and dependency-free (std only): ``Map``
    is a real ``String -> String`` map, ``Pool`` is a real bounded connection
    pool over a deterministic in-memory database, and ``Job`` is a real
    cancellable future so component ``await`` steps have genuine async state to
    await.  None of them panic with ``todo!()``.

    Pool/Job semantics are normative and shared across tiers — see
    ``backends/python/runtime.py``, section ``.. _pool-job-semantics:``.  This
    tier reports misuse (exhaustion, use-after-close, awaiting a cancelled
    job) by panicking, because the emitted call sites are infallible.
    """
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
    lifecycle_present = any(t.get("lifecycle") for t in (ir.get("tests") or []))
    if "Map" in used or "Pool" in used or lifecycle_present:
        # R1 live-resource accounting (docs/backend-ir.md §Required semantics,
        # the same pairing the py reference tier's `assert no_residue` checks):
        # every host object acquired must be released by its `undo`, or the
        # lifecycle `assert no_residue` fails. The counter is process-wide, so
        # it is per-test and cross-test safe: a clean test returns to zero.
        out.extend([
            "/// R1 live-resource counter (lifecycle `assert no_residue`).",
            "static REVL_LIVE_HOST_RESOURCES: std::sync::atomic::AtomicI64 =",
            "    std::sync::atomic::AtomicI64::new(0);",
            "",
        ])
    if "Map" in used:
        out.extend(
            [
                "/// revl host object: a small thread-safe string map.",
                "pub struct Map {",
                "    inner: std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, String>>>,",
                "}",
                "impl Map {",
                "    pub fn new() -> Self {",
                "        REVL_LIVE_HOST_RESOURCES.fetch_add(1, std::sync::atomic::Ordering::SeqCst);",
                "        Self {",
                "            inner: std::sync::Arc::new(std::sync::Mutex::new(std::collections::HashMap::new())),",
                "        }",
                "    }",
                "    pub fn drop_(&self) {",
                "        REVL_LIVE_HOST_RESOURCES.fetch_sub(1, std::sync::atomic::Ordering::SeqCst);",
                "        self.inner.lock().unwrap().clear();",
                "    }",
                "    pub fn insert(&self, key: String, value: String) {",
                "        self.inner.lock().unwrap().insert(key, value);",
                "    }",
                "    pub fn remove(&self, key: String) {",
                "        self.inner.lock().unwrap().remove(&key);",
                "    }",
                "    pub fn get(&self, key: String) -> Option<String> {",
                "        self.inner.lock().unwrap().get(&key).cloned()",
                "    }",
                "}",
                "",
            ]
        )
    if "Pool" in used:
        out.extend(
            [
                "/// revl host object: a bounded connection pool over a deterministic",
                "/// in-memory database (no driver dependency).  Semantics are shared",
                "/// across tiers — backends/python/runtime.py, `.. _pool-job-semantics:`:",
                "/// `size` connections numbered 1..size, acquire/release accounting,",
                "/// statements borrow a connection for their duration, `close` releases",
                "/// everything, and exhaustion / use-after-close panic.",
                "pub struct Pool {",
                "    url: String,",
                "    size: i64,",
                "    state: std::sync::Mutex<PoolState>,",
                "}",
                "struct PoolState {",
                "    idle: Vec<i64>,",
                "    in_use: Vec<i64>,",
                "    closed: bool,",
                "}",
                "impl Pool {",
                "    pub fn open(url: String, size: i64) -> Self {",
                "        if size < 1 {",
                "            panic!(\"pool size must be an integer >= 1 (got {})\", size);",
                "        }",
                "        REVL_LIVE_HOST_RESOURCES.fetch_add(1, std::sync::atomic::Ordering::SeqCst);",
                "        Self {",
                "            url,",
                "            size,",
                "            state: std::sync::Mutex::new(PoolState {",
                "                idle: (1..=size).collect(),",
                "                in_use: Vec::new(),",
                "                closed: false,",
                "            }),",
                "        }",
                "    }",
                "    pub fn url(&self) -> String {",
                "        self.url.clone()",
                "    }",
                "    pub fn capacity(&self) -> i64 {",
                "        if self.state.lock().unwrap().closed { 0i64 } else { self.size }",
                "    }",
                "    pub fn in_use(&self) -> i64 {",
                "        self.state.lock().unwrap().in_use.len() as i64",
                "    }",
                "    pub fn available(&self) -> i64 {",
                "        self.state.lock().unwrap().idle.len() as i64",
                "    }",
                "    // NB: never panic while the guard is held — a panic under the",
                "    // lock poisons the Mutex, and every later call would then fail",
                "    // with a PoisonError instead of the intended message (which is",
                "    // not what the other tiers do: there the pool stays usable).",
                "    fn borrow_conn(&self, op: &str) -> i64 {",
                "        let outcome = {",
                "            let mut state = self.state.lock().unwrap();",
                "            if state.closed {",
                "                Err(format!(\"pool.{} after close/drop — use-after-free\", op))",
                "            } else if state.idle.is_empty() {",
                "                Err(format!(",
                "                    \"pool.{} exhausted (size={}, in_use={})\",",
                "                    op,",
                "                    self.size,",
                "                    state.in_use.len()",
                "                ))",
                "            } else {",
                "                let conn = state.idle.remove(0);",
                "                state.in_use.push(conn);",
                "                Ok(conn)",
                "            }",
                "        };",
                "        match outcome {",
                "            Ok(conn) => conn,",
                "            Err(message) => panic!(\"{}\", message),",
                "        }",
                "    }",
                "    fn give_back(&self, conn: i64) {",
                "        let mut state = self.state.lock().unwrap();",
                "        if let Some(pos) = state.in_use.iter().position(|c| *c == conn) {",
                "            state.in_use.remove(pos);",
                "        }",
                "        state.idle.push(conn);",
                "        state.idle.sort_unstable();",
                "    }",
                "    pub fn acquire(&self) -> i64 {",
                "        self.borrow_conn(\"acquire\")",
                "    }",
                "    pub fn release(&self, conn: i64) {",
                "        let outcome = {",
                "            let state = self.state.lock().unwrap();",
                "            if state.closed {",
                "                Err(String::from(\"pool.release after close/drop — use-after-free\"))",
                "            } else if !state.in_use.contains(&conn) {",
                "                Err(format!(\"pool.release conn={} is not checked out\", conn))",
                "            } else {",
                "                Ok(())",
                "            }",
                "        };",
                "        if let Err(message) = outcome {",
                "            panic!(\"{}\", message);",
                "        }",
                "        self.give_back(conn);",
                "    }",
                "    pub fn close(&self) {",
                "        let already_closed = {",
                "            let mut state = self.state.lock().unwrap();",
                "            if state.closed {",
                "                true",
                "            } else {",
                "                state.in_use.clear();",
                "                state.idle.clear();",
                "                state.closed = true;",
                "                false",
                "            }",
                "        };",
                "        if already_closed {",
                "            panic!(\"pool.close after close/drop — use-after-free\");",
                "        }",
                "        REVL_LIVE_HOST_RESOURCES.fetch_sub(1, std::sync::atomic::Ordering::SeqCst);",
                "    }",
                "    pub fn query(&self, _sql: String) -> Vec<Value> {",
                "        let conn = self.borrow_conn(\"query\");",
                "        self.give_back(conn);",
                "        Vec::new()",
                "    }",
                "    pub fn execute(&self, _sql: String) -> i64 {",
                "        let conn = self.borrow_conn(\"execute\");",
                "        self.give_back(conn);",
                "        1i64",
                "    }",
                "}",
                "",
            ]
        )
    if "Job" in used:
        out.extend(
            [
                "/// revl host object: a cancellable asynchronous unit of work.",
                "/// Semantics are shared across tiers — backends/python/runtime.py,",
                "/// `.. _pool-job-semantics:`: a job completes after exactly",
                "/// `Job::TICKS` scheduler turns (no timer, so tests stay deterministic),",
                "/// carries a cancellation token, and panics if awaited after",
                "/// cancellation.  `Job::spawn` hands back the handle (the tier-neutral",
                "/// `Job.run` of the other backends); `Job::run` is the async shorthand",
                "/// the emitted `await Job.run(name)` call site uses.",
                "pub struct Job;",
                "/// 0 = pending, 1 = done, 2 = cancelled",
                "#[derive(Clone)]",
                "pub struct JobToken(std::sync::Arc<std::sync::atomic::AtomicU8>);",
                "impl JobToken {",
                "    /// pending -> cancelled (true); a no-op returning false otherwise.",
                "    pub fn cancel(&self) -> bool {",
                "        self.0",
                "            .compare_exchange(",
                "                0u8,",
                "                2u8,",
                "                std::sync::atomic::Ordering::SeqCst,",
                "                std::sync::atomic::Ordering::SeqCst,",
                "            )",
                "            .is_ok()",
                "    }",
                "    pub fn state(&self) -> &'static str {",
                "        match self.0.load(std::sync::atomic::Ordering::SeqCst) {",
                "            1u8 => \"done\",",
                "            2u8 => \"cancelled\",",
                "            _ => \"pending\",",
                "        }",
                "    }",
                "}",
                "pub struct JobHandle {",
                "    name: String,",
                "    remaining: u32,",
                "    state: std::sync::Arc<std::sync::atomic::AtomicU8>,",
                "}",
                "impl JobHandle {",
                "    pub fn name(&self) -> String {",
                "        self.name.clone()",
                "    }",
                "    pub fn token(&self) -> JobToken {",
                "        JobToken(self.state.clone())",
                "    }",
                "    pub fn state(&self) -> &'static str {",
                "        self.token().state()",
                "    }",
                "    pub fn cancel(&self) -> bool {",
                "        self.token().cancel()",
                "    }",
                "}",
                "impl std::future::Future for JobHandle {",
                "    type Output = String;",
                "    fn poll(",
                "        self: std::pin::Pin<&mut Self>,",
                "        cx: &mut std::task::Context<'_>,",
                "    ) -> std::task::Poll<String> {",
                "        let this = self.get_mut();",
                "        match this.state.load(std::sync::atomic::Ordering::SeqCst) {",
                "            1u8 => return std::task::Poll::Ready(this.name.clone()),",
                "            2u8 => panic!(\"job \\\"{}\\\" cancelled\", this.name),",
                "            _ => {}",
                "        }",
                "        if this.remaining == 0u32 {",
                "            this.state",
                "                .store(1u8, std::sync::atomic::Ordering::SeqCst);",
                "            return std::task::Poll::Ready(this.name.clone());",
                "        }",
                "        this.remaining -= 1u32;",
                "        cx.waker().wake_by_ref();",
                "        std::task::Poll::Pending",
                "    }",
                "}",
                "impl Job {",
                "    /// scheduler turns of simulated work (same number on every tier)",
                "    pub const TICKS: u32 = 5u32;",
                "    /// Schedule a cancellable job.  The handle is the future; take",
                "    /// `handle.token()` first if you need to cancel it from elsewhere.",
                "    pub fn spawn(name: String) -> JobHandle {",
                "        JobHandle {",
                "            name,",
                "            remaining: Self::TICKS,",
                "            state: std::sync::Arc::new(std::sync::atomic::AtomicU8::new(0u8)),",
                "        }",
                "    }",
                "    /// Run a job to completion — the `await Job.run(name)` form emitted",
                "    /// code uses.  On this path a divert cancels by dropping the future;",
                "    /// use `spawn` when the host needs an explicit cancellation token.",
                "    pub async fn run(name: String) -> String {",
                "        Self::spawn(name).await",
                "    }",
                "}",
                "",
            ]
        )
    return out


def _binds(component: dict) -> list[str]:
    return [s["bind"] for s in component.get("body") or [] if s.get("step") == "let-effect"]


def _host_of(component: dict, bind: str) -> str:
    for s in component.get("body") or []:
        if s.get("step") == "let-effect" and s.get("bind") == bind:
            acquire = s.get("acquire") or {}
            # A `spawn` acquisition binds a live-instance handle, not a host
            # resource: the binding's type is the emitted `RevlSpawnHandle`,
            # so a provide-method that captures it can call `.dispose()`.
            if acquire.get("kind") == "spawn":
                return "RevlSpawnHandle"
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


def _pure_method_statements(env: _Env, method: dict, rename: dict) -> str:
    """A pure method body as Rust statements.

    The result is inlined into `fn name(..) -> T { <here> }`, so a plain
    binding is a `let`, and the trailing `return` step keeps its keyword.
    """
    steps = method.get("body") or []
    # The expression-body fast path only applies to `return <expr>`. A bare
    # `return` (void service op, `{"step": "return", "expr": null}`) has no
    # expression to inline and falls through to the statement path below.
    if (
        len(steps) == 1
        and steps[0].get("step") == "return"
        and steps[0].get("expr") is not None
    ):
        return _expr(steps[0]["expr"], env, rename)

    parts: list[str] = []
    scope = dict(rename)
    for step in steps:
        kind = step.get("step")
        if kind == "let":
            name = _ident(step.get("name"), "binding")
            scope.pop(name, None)  # a local shadows an outer rename
            parts.append(f"let {'mut ' if step.get('mutable') else ''}"
                         f"{name} = {_expr(step['value'], env, scope)};")
        elif kind == "assign":
            name = _ident(step.get("name"), "binding")
            parts.append(f"{name} = {_expr(step['value'], env, scope)};")
        elif kind == "return":
            expr = step.get("expr")
            parts.append("return;" if expr is None
                         else f"return {_expr(expr, env, scope)};")
        else:
            raise EmitError(
                f"{env.name}.{method.get('name')}: a pure method body admits "
                f"bindings and a return in the Rust backend, not {kind!r}"
            )
    return " ".join(parts)


def _method_body(env: _Env, method: dict) -> str:
    rename = {b: f"self.{b}" for b in _binds(env.component)}
    rename.update({local: f"self.{local}" for local in env.reqs})
    return _pure_method_statements(env, method, rename)


def _method_body_pure_new(env: _Env, method: dict) -> str:
    return _pure_method_statements(env, method, _method_scope_rename(env))


def _component_has_effectful_methods(component: dict) -> bool:
    for step in component.get("body") or []:
        if step.get("step") != "provide":
            continue
        for method in step.get("methods") or []:
            for body_step in method.get("body") or []:
                if body_step.get("step") in ("effect", "emit"):
                    return True
    return False


def _component_uses_await(component: dict) -> bool:
    return any(
        step.get("step") == "await"
        for step in component.get("body") or []
    )


def _emit_config_struct(component: dict, out: list[str]) -> str:
    """Emit `<Comp>Config` plus its `Default` implementation.

    Returns the Rust type to pass to `plugin_sync`/`plugin_async` (or `()` when
    the component has no config). The `Default` impl carries IR default values;
    required fields fall back to the zero value for their Rust type.
    """
    fields = component.get("config") or []
    if not fields:
        return "()"
    cname = _ident(component.get("name"), "component")
    out.append("#[derive(Clone)]")
    out.append(f"struct {cname}Config {{")
    for field in fields:
        fname = _ident(field.get("name"), "config field")
        out.append(f"    {fname}: {_rust_type(field.get('type'))},")
    out.append("}")
    out.append("")
    out.append(f"impl Default for {cname}Config {{")
    out.append("    fn default() -> Self {")
    out.append("        Self {")
    for field in fields:
        fname = _ident(field.get("name"), "config field")
        ftype = _rust_type(field.get("type"))
        out.append(
            f"            {fname}: {_config_default_lit(field.get('default'), ftype)},"
        )
    out.append("        }")
    out.append("    }")
    out.append("}")
    out.append("")
    return f"{cname}Config"


def _emit_config_application(component: dict, config_ty: str, indent: int) -> list[str]:
    """Reconstruct the plugin config locally using `..Default::default()`.

    `plugin_sync`/`plugin_async` hand the component a fully-populated
    `Arc<CompConfig>`. Rust struct fields are not optional, so the runtime
    cannot observe "missing" fields; emitting the local construction keeps the
    generated component the single place where `<Comp>Config` is built and
    makes `Default` part of that construction.
    """
    if config_ty == "()":
        return []
    fields = component.get("config") or []
    pad = "    " * indent
    lines = [f"{pad}let config = {config_ty} {{"]
    for field in fields:
        fname = _ident(field.get("name"), "config field")
        lines.append(f"{pad}    {fname}: config.{fname}.clone(),")
    lines.append(f"{pad}    ..Default::default()")
    lines.append(f"{pad}}};")
    return lines


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
            # An undo closure is registered for every effect, and for an emit
            # only when it carries a compensate — the `*_undo` clones exist
            # exactly when that closure exists.
            registers_undo = kind == "effect" or step.get("compensate") is not None
            if registers_undo:
                _method_undo_clones(env, method, out, indent)
            acquire = _expr(step.get("acquire") or step.get("expr"), env, acquire_rename)
            out.append(f"{pad}let _ = {acquire};")
            if not registers_undo:
                continue
            undo = (
                _expr(step["compensate"], env, undo_rename)
                if kind == "emit"
                else _expr(step["undo"], env, undo_rename)
            )
            out.append(
                f"{pad}let _ = self.ctx.effect({label}, move || {{ {undo}; Ok(()) }});"
            )
        elif kind in ("let", "assign"):
            name = _ident(step.get("name"), "binding")
            if kind == "let":
                rename.pop(name, None)  # a local shadows an outer rename
                out.append(f"{pad}let {'mut ' if step.get('mutable') else ''}"
                           f"{name} = {_expr(step['value'], env, rename)};")
            else:
                out.append(f"{pad}{name} = {_expr(step['value'], env, rename)};")
        elif kind == "await":
            raise EmitError("await steps are not allowed inside method bodies (A1)")
        else:
            raise EmitError(f"unsupported method body step in Rust backend: {kind!r}")



def _emit_component_new(component: dict, services: dict, ir: dict | None = None) -> list[str]:
    """v2 components + effectful provide-method bodies.

    This is used for any component that needs more than the original v1 spike:
    `isolate`, `intercept`, or a provide method containing `effect`/`emit`.
    """
    ir = ir or {}
    env = _Env(
        component,
        services,
        types=ir.get("types"),
        functions=ir.get("functions"),
        externs=ir.get("externs"),
        components=ir.get("components"),
    )
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

    config_ty = _emit_config_struct(component, out)

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
                f"{p}: {_rust_type(_param_type(env, key, original_mname, p), env.types)}"
                for p in method.get("params") or []
            )
            ret = (_rust_type(_method_return(env, key, original_mname), env.types)
                   if _method_return(env, key, original_mname) else "()")
            # A provide-method body is rendered by the shared expression renderer, which
            # needs the parameter types to tell a string `+` from a numeric one.
            env.v3_ctx().var_types = {
                p: _param_type(env, key, original_mname, p)
                for p in method.get("params") or []
            }
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

    uses_await = _component_uses_await(component)
    plugin_fn = "cordis::plugin_async::<{0}, _, _>" if uses_await else "cordis::plugin_sync::<{0}, _>"
    closure = "        |ctx, config| async move {" if uses_await else "        |ctx, config| {"
    out.append(f"pub fn {snake}() -> cordis::PluginHandle {{")
    out.append(f"    {plugin_fn.format(config_ty)}(")
    out.append(f"        {_string(name)},")
    out.append(f"        cordis::{inject},")
    out.append(closure)
    out.extend(_emit_config_application(component, config_ty, indent=3))
    # Realm isolation is NOT applied here. cordis evaluates a plugin's reactive
    # `Inject` gate against the context the plugin is registered on, before this
    # closure ever runs — so isolating `ctx` inside the body cannot scope the
    # gate, and an isolated `requires kv in realm("t")` would hang Pending
    # forever (the fiber's `meta.isolates` never carries the realm). Isolation
    # is instead applied at plug time via `_revl_isolate_ctx` below, mirroring
    # the python/typescript backends' `plug()` helper. `ctx` is therefore
    # already the isolated context here, so provides/requires resolve in-realm.
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


def _emit_component_auto(component: dict, services: dict, ir: dict | None = None) -> list[str]:
    if (
        not (component.get("isolate") or component.get("intercept"))
        and not _component_has_effectful_methods(component)
    ):
        return _emit_component(component, services, ir)
    return _emit_component_new(component, services, ir)


def _collect_realm_labels(components: list) -> list[str]:
    """Every distinct realm-label string the program isolates on, sorted.

    Sorting makes index assignment a pure function of the label *set*, so the
    same source lowers to the same Isolation values on every build.
    """
    labels: set[str] = set()
    for component in components:
        for realm in (component.get("isolate") or {}).values():
            labels.add(realm)
    return sorted(labels)


def _revl_realm_helper(components: list) -> list[str]:
    """Lower each distinct realm label to a STABLE, DETERMINISTIC Isolation
    from a reserved high region of the u64 scope space.

    cordis-rs mints framework scope labels from a monotonic counter that
    starts at 1 and increments by 1 (`RootInner::scope`,
    cordis-rs-0.3.0/src/context.rs:86-88), so every framework label lands in
    the low range [1, 2^63). We tag realm labels with the top bit set, which
    that counter cannot reach without 2^63 allocations — provably disjoint
    from framework labels (the collision `Isolation::from_raw`'s own docs
    warn about, context.rs:20-25). Distinct labels get distinct indices, so
    equal strings share a realm and no two realms ever collide. Unlike the
    former `DefaultHasher`, this registry is fixed build-to-build and depends
    on no std hashing internals.
    """
    labels = _collect_realm_labels(components)
    lines = [
        "pub fn _revl_realm(label: &str) -> cordis::Isolation {",
        "    // Top bit reserved for realm labels: disjoint from cordis-rs's",
        "    // monotonic scope counter (starts at 1, +1 each isolate()).",
        "    const REVL_REALM_TAG: u64 = 0x8000_0000_0000_0000;",
        "    let index: u64 = match label {",
    ]
    for i, label in enumerate(labels):
        lines.append(f"        {_string(label)} => {i},")
    lines.append(
        '        other => panic!("revl: realm label {other:?} missing from '
        'compile-time registry"),'
    )
    lines.extend(
        [
            "    };",
            "    cordis::Isolation::from_raw(REVL_REALM_TAG | index)",
            "}",
            "",
        ]
    )
    return lines



def _emit_component(component: dict, services: dict, ir: dict | None = None) -> list[str]:
    ir = ir or {}
    env = _Env(
        component,
        services,
        types=ir.get("types"),
        functions=ir.get("functions"),
        externs=ir.get("externs"),
        components=ir.get("components"),
    )
    name = component["name"]
    cname = _ident(name, "component")
    snake = _snake(name)
    out: list[str] = []

    config_ty = _emit_config_struct(component, out)

    for key, service in env.provides.items():
        _ident(key, "provision")
        struct = f"{cname}{_camel(key)}"
        out.append(f"struct {struct} {{")
        for b in _binds(component):
            out.append(f"    {_ident(b, 'binding')}: Arc<{_host_of(component, b)}>,")
        # a provide-method may call a required service, so the provider owns
        # the same bindings the effectful path captures (java does this too)
        for local, req_service in env.reqs.items():
            out.append(f"    {local}: Arc<Box<dyn {req_service}>>,")
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
                f"{p}: {_rust_type(_param_type(env, key, mname, p), env.types)}"
                for p in method.get("params") or []
            )
            ret = (_rust_type(_method_return(env, key, mname), env.types)
                   if _method_return(env, key, mname) else "()")
            # A provide-method body is rendered by the shared expression renderer, which
            # needs the parameter types to tell a string `+` from a numeric one.
            env.v3_ctx().var_types = {
                p: _param_type(env, key, mname, p)
                for p in method.get("params") or []
            }
            out.append(f"    fn {mname}(&self, {params}) -> {ret} {{ {_method_body(env, method)} }}")
        out.append("}")
        out.append("")

    inject = "Inject::none()" if not env.reqs else f"Inject::new({_string(list(env.reqs.keys()))})"
    uses_await = _component_uses_await(component)
    plugin_fn = "cordis::plugin_async::<{0}, _, _>" if uses_await else "cordis::plugin_sync::<{0}, _>"
    closure = "        |ctx, config| async move {" if uses_await else "        |ctx, config| {"
    out.append(f"pub fn {snake}() -> cordis::PluginHandle {{")
    out.append(f"    {plugin_fn.format(config_ty)}(")
    out.append(f"        {_string(name)},")
    out.append(f"        cordis::{inject},")
    out.append(closure)
    out.extend(_emit_config_application(component, config_ty, indent=3))
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


def _emit_setup_value(node: dict, env: _Env) -> str:
    """Lower a setup value.

    revl `Str` is emitted as `String`, and `_render_expr` already lowers every
    string literal to an owned `String::from(..)`, so a setup `let` binding
    never risks being inferred as `&str` and needs no extra coercion here.
    """
    return _expr(node, env)


def _emit_setup_step(step: dict, env: _Env, out: list[str], indent: int) -> None:
    """Lower a pure block-effect setup step (stratum-1)."""
    pad = "    " * indent
    kind = step.get("step")
    if kind == "let":
        name = _ident(step.get("name"), "setup binding")
        out.append(f"{pad}let mut {name} = {_emit_setup_value(step.get('value'), env)};")
    elif kind == "assign":
        name = _ident(step.get("name"), "setup assign")
        out.append(f"{pad}{name} = {_emit_setup_value(step.get('value'), env)};")
    elif kind == "expr":
        out.append(f"{pad}let _ = {_expr(step.get('expr'), env)};")
    elif kind == "if":
        out.append(f"{pad}if {_expr(step.get('cond'), env)} {{")
        for nested in step.get("then") or []:
            _emit_setup_step(nested, env, out, indent + 1)
        if step.get("else"):
            out.append(f"{pad}}} else {{")
            for nested in step.get("else") or []:
                _emit_setup_step(nested, env, out, indent + 1)
        out.append(f"{pad}}}")
    elif kind == "assert":
        out.append(f"{pad}assert!({_expr(step.get('expr'), env)});")
    else:
        raise EmitError(f"unsupported setup step in Rust backend: {kind!r}")


def _emit_step(step: dict, env: _Env, out: list[str], indent: int) -> None:
    pad = "    " * indent
    kind = step.get("step")
    if kind == "let-effect":
        for setup in step.get("setup") or []:
            _emit_setup_step(setup, env, out, indent)
        bind = _ident(step["bind"], "binding")
        acquire = _expr(step["acquire"], env)
        out.append(f"{pad}let {bind} = Arc::new({acquire});")
        undo_name = f"{bind}_undo"
        out.append(f"{pad}let {undo_name} = {bind}.clone();")
        undo_rename = {step["bind"]: undo_name}
        for req in env.reqs:
            req_undo = f"{req}_undo"
            out.append(f"{pad}let {req_undo} = {req}.clone();")
            undo_rename[req] = req_undo
        undo = _expr(step["undo"], env, rename=undo_rename)
        label = _string(env.name + "." + step["bind"] + ".undo")
        out.append(f"{pad}ctx.effect({label}, move || {{ {undo}; Ok(()) }})?;")
    elif kind == "effect":
        for setup in step.get("setup") or []:
            _emit_setup_step(setup, env, out, indent)
        acquire = _expr(step["acquire"], env)
        undo_rename: dict[str, str] = {}
        for req in env.reqs:
            req_undo = f"{req}_undo"
            out.append(f"{pad}let {req_undo} = {req}.clone();")
            undo_rename[req] = req_undo
        undo = _expr(step["undo"], env, rename=undo_rename)
        out.append(f"{pad}let _ = {acquire};")
        label = _string(env.name + ".effect")
        out.append(f"{pad}ctx.effect({label}, move || {{ {undo}; Ok(()) }})?;")
    elif kind == "emit":
        out.append(f"{pad}let _ = {_expr(step['expr'], env)};")
    elif kind == "fail":
        message = _expr(step.get("message"), env)
        out.append(
            f"{pad}return Err(cordis::CordisError::with_message("
            f"cordis::ErrorCode::Plugin, {message}));"
        )
    elif kind == "if":
        out.append(f"{pad}if {_expr(step.get('cond'), env)} {{")
        for nested in step.get("then") or []:
            _emit_step(nested, env, out, indent + 1)
        if step.get("else"):
            out.append(f"{pad}}} else {{")
            for nested in step.get("else") or []:
                _emit_step(nested, env, out, indent + 1)
        out.append(f"{pad}}}")
    elif kind == "await":
        out.append(f"{pad}{_expr(step['expr'], env)}.await;")
    elif kind == "provide":
        key = step.get("name")
        service = step.get("service")
        struct = f"{env.name}{_camel(key)}"
        fields = ", ".join(
            [f"{_ident(b, 'binding')}: {b}.clone()" for b in _binds(env.component)]
            + [f"{local}: {local}.clone()" for local in env.reqs]
        )
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

# Expression kinds that render as a Rust postfix-safe primary, so a `.method()`,
# `.field`, `[index]` or `.unwrap_or_else(..)` suffix can be appended without
# wrapping them in parentheses. The set is the union of both dialects' atomic
# kinds: the 2.0 primaries (`var`/`name`/`field`/`index`/`call`/`lit`) plus the
# component primaries (`req`/`config`/`host`), which never appear in a 2.0 body
# but must stay paren-free when a component `??`/receiver builds on them.
_ATOMIC_KINDS = {
    "var", "name", "field", "index", "call", "lit", "req", "config", "host",
}
_V3_HOST_ROOTS = set(_HOST_STUBS)
_V3_BUILTIN_CONSTRUCTORS = {"Some", "None", "Ok", "Err"}


def _v3_is_str(node: object, ctx: "_V3Ctx") -> bool:
    """Is this expression certainly a `Str`?

    Deliberately conservative — it answers "certainly yes" or "unknown", never
    guesses. A false positive would render a numeric `+` as a `format!` and
    produce a `String` where an `i64` is expected, so the only sources trusted
    here are a string literal, a template, a binding whose declared or
    inferred type is `Str`, and a `+` over those.
    """
    if not isinstance(node, dict):
        return False
    kind = node.get("kind")
    if kind == "lit":
        return isinstance(node.get("value"), str)
    if kind == "interp":
        return True
    if kind in ("name", "var"):
        return ctx.var_types.get(node.get("id") or node.get("name")) == "Str"
    if kind == "bin" and node.get("op") == "+":
        return _v3_is_str(node.get("left"), ctx) or _v3_is_str(node.get("right"), ctx)
    return False


def _v3_infer_type(node: object, ctx: "_V3Ctx") -> str | None:
    """The surface type of an expression when it is knowable, else None."""
    return "Str" if _v3_is_str(node, ctx) else None


def _v3_is_float(node: object) -> bool:
    """Is this expression *syntactically* certain to be a `Float`? A Float
    literal, a `/` (true division), a Float-annotated arithmetic node, or a
    unary minus of one — the same node-local proof the other tiers use so a
    `${aFloat}` routes through the canonical renderer (docs/strings.md)."""
    if not isinstance(node, dict):
        return False
    kind = node.get("kind")
    if kind == "lit":
        value = node.get("value")
        return isinstance(value, float) and not isinstance(value, bool)
    if kind == "bin":
        return node.get("op") == "/" or node.get("operands") == "Float"
    if kind == "un":
        return node.get("op") == "-" and _v3_is_float(node.get("operand"))
    return False


class _V3Ctx:
    """Names visible to lowered IR v3 expression/statement emitters."""

    def __init__(self, types: dict, functions: list, externs: list,
                 components: list | None = None) -> None:
        self.types = types or {}
        self.function_names = {fn.get("name") for fn in functions or []}
        self.extern_names = {ext.get("name") for ext in externs or []}
        # target component name -> whether it declares a typed config struct.
        # A `spawn` acquisition uses this to build the plug-time config value:
        # `<Comp>Config { .. }` when the template has config fields, else `()`.
        self.spawn_target_has_config: dict[str, bool] = {
            comp.get("name"): bool(comp.get("config"))
            for comp in components or []
        }
        # Binding -> surface type, seeded from declared signatures and from
        # `let` right-hand sides. Rust is the only tier that needs this: `+`
        # on strings has ownership rules (`String + &str` only), so the
        # renderer must know when a `+` is a concatenation. See `_v3_is_str`.
        self.var_types: dict[str, str | None] = {}
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



def _render_expr(node: dict, ctx: _V3Ctx, rename: dict[str, str] | None = None) -> str:
    """Lower one IR expression node to a Rust expression.

    This is the *single* expression renderer for the Rust tier. It covers both
    IR dialects a document can mix:

    * the component ("v1") dialect — ``req``/``config``/``host``/``format``/
      ``fn`` and the component-shaped ``call`` (``target``/``method``); and
    * the 2.0 dialect — ``callee``/``args`` calls, ``match``, ``arrow``, ADTs,
      the stdlib ``builtin`` surface, string ``+`` via ``format!``, …

    Where a kind exists in *both* dialects the renderer dispatches on **shape**
    (which distinguishing keys are present), never on kind alone — see ``call``,
    which reads ``callee`` for the 2.0 form and ``target``/``method`` for the
    component form. ``rename`` is the component-capture map (``pool`` ->
    ``self.pool``); a 2.0 body passes no rename, so the map is simply empty
    there. String literals lower to owned ``String`` in every position (revl
    ``Str`` is ``String``), so no dialect-specific literal coercion is needed.
    """
    if not isinstance(node, dict) or "kind" not in node:
        raise EmitError(f"malformed expression: {node!r}")
    # An implicit Int -> Float coercion site (docs/arithmetic.md). This tier
    # is the one that *refused* `ident(3)` with E0308 before the marker
    # existed — the widening was real but invisible; now it is emitted.
    if node.get("widen") == "Float":
        inner = {k: v for k, v in node.items() if k != "widen"}
        return f"({_render_expr(inner, ctx, rename)} as f64)"
    # An Int32 -> Int widening site (docs/arithmetic.md): i32 does not coerce to
    # i64 in Rust (E0308), so the lossless widening is written where the
    # frontend marked it, exactly as the Float case is.
    if node.get("widen") == "Int":
        inner = {k: v for k, v in node.items() if k != "widen"}
        return f"({_render_expr(inner, ctx, rename)} as i64)"
    rename = rename or {}
    kind = node["kind"]

    if kind == "lit":
        return _rust_v3_lit(node)

    if kind == "name":
        # A component capture (`self.pool`) arrives via the rename map already
        # rendered as a Rust expression; otherwise it is a plain identifier.
        original = node.get("id")
        if rename and original in rename:
            return rename[original]
        return _ident(original, "name")

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

    if kind == "req":
        # component dialect: a required capability, possibly a `self.`-capture.
        original = node.get("name")
        if rename and original in rename:
            return rename[original]
        return _ident(original, "requirement")

    if kind == "config":
        # Component guards (`if (config.n < 1) { fail .. }`) reach this renderer
        # through the surrounding `bin`/`un` node, and `_method_body`/setup put a
        # local `let config = <Comp>Config { .. }` in scope, so config reads the
        # same way whether it arrives via the component or the 2.0 path.
        return f"config.{_ident(node.get('field'), 'config field')}.clone()"

    if kind == "host":
        # component dialect: `Pool.open(..)` -> `Pool::open(..)`.
        fn = node.get("fn")  # e.g. "Pool.open"
        host, _, method = fn.partition(".")
        args = ", ".join(_render_expr(a, ctx, rename) for a in node.get("args") or [])
        return f"{host}::{_mname(method)}({args})"

    if kind == "format":
        # component dialect: `$0`/`$1` template -> Rust `format!`.
        template = node.get("template") or ""
        args = [_render_expr(a, ctx, rename) for a in node.get("args") or []]
        return _format(template, args)

    if kind == "fn":
        # component dialect: a free-function call `name(..)`.
        name = _ident(node.get("name"), "function")
        args = ", ".join(_render_expr(a, ctx, rename) for a in node.get("args") or [])
        return f"{name}({args})"

    if kind == "adt":
        # tagged ADT construction: user variants -> `Enum::Case(..)`, built-in
        # Result -> native `Ok(..)`/`Err(..)`. Reuses the constructor logic
        # the call/var paths already use.
        return ctx.constructor(
            node["case"], [_render_expr(a, ctx, rename) for a in node.get("args") or []]
        )

    if kind == "bin":
        if node.get("op") == "??":
            # `a ?? b` (docs/syntax-2.0.md §3.2): `Opt[T]` is `Option<T>` on
            # this tier, so the absent case is `None`. `unwrap_or_else` keeps
            # `b` lazy — `??` must not evaluate its right operand when `a` is
            # present, and `unwrap_or` would evaluate it unconditionally. This
            # is the sole `??` lowering; both dialects funnel through here now.
            left = _render_expr(node["left"], ctx, rename)
            if node["left"].get("kind") not in _ATOMIC_KINDS:
                left = f"({left})"
            return f"{left}.unwrap_or_else(|| {_render_expr(node['right'], ctx, rename)})"
        if node.get("op") == "+" and (
                _v3_is_str(node.get("left"), ctx) or _v3_is_str(node.get("right"), ctx)):
            # Rust's `+` on strings takes `String + &str` only: `&str + String`
            # and `String + String` are both errors, and which one a revl `+`
            # becomes depends on where each side came from. `format!` accepts
            # every combination and always yields `String`, which is what `Str`
            # lowers to. (Both spellings shipped broken until `cargo check` ran
            # over the emitted code — docs/conformance.md.)
            return (f'format!("{{}}{{}}", {_render_expr(node["left"], ctx, rename)}, '
                    f'{_render_expr(node["right"], ctx, rename)})')
        op = _V3_BIN_OPS.get(node.get("op"))
        if op is None:
            raise EmitError(f"unsupported binary operator {node.get('op')!r}")
        left = _render_expr(node["left"], ctx, rename)
        right = _render_expr(node["right"], ctx, rename)
        if node.get("op") in ("+", "-", "*") and node.get("operands") == "Int":
            # Int is bounded 64-bit and overflow TRAPS (docs/arithmetic.md):
            # silent wraparound is the failure mode revl exists to remove, and
            # rust only checks in debug builds by default.
            m = {"+": "checked_add", "-": "checked_sub", "*": "checked_mul"}[node["op"]]
            return (f'({left}).{m}({right}).expect("revl: Int overflow")')
        if node.get("op") in ("+", "-", "*") and node.get("operands") == "Int32":
            # Int32 traps at the i32 edge; rust's own `*`/`+`/`-` only check in
            # debug, so the checked forms make it release-safe too
            # (docs/arithmetic.md).
            m = {"+": "checked_add", "-": "checked_sub", "*": "checked_mul"}[node["op"]]
            return (f'({left}).{m}({right}).expect("revl: Int32 overflow")')
        if node.get("op") == "/" and node.get("operands") in ("Int", "Int32"):
            # `/` is true division and yields Float (docs/arithmetic.md), but
            # rust `/` on two i64 is integer division — it would compute 3 for
            # `7 / 2` and then fail to typecheck against the f64 the checker
            # declared. Widen both sides; IEEE division on f64 follows, so a
            # zero divisor gives +/-inf rather than the i64 panic.
            return f"(({left}) as f64 / ({right}) as f64)"
        return f"({left} {op} {right})"

    if kind == "un":
        operand = _render_expr(node.get("operand"), ctx, rename)
        if node.get("op") == "!":
            return f"(!{operand})"
        if node.get("op") == "-":
            return f"(-{operand})"
        raise EmitError(f"unsupported unary operator {node.get('op')!r}")

    if kind == "call":
        # Shape dispatch: the 2.0 form carries `callee`; the component form
        # carries `target`/`method`. Reading the wrong child would silently emit
        # the wrong receiver, so the presence of `callee` — not the kind — is
        # what selects the form.
        if "callee" in node:
            callee_node = node.get("callee") or {}
            arg_exprs = [_render_expr(a, ctx, rename) for a in node.get("args") or []]
            args = ", ".join(arg_exprs)
            if callee_node.get("kind") == "field":
                target_node = callee_node.get("target") or {}
                method = callee_node.get("name")
                if target_node.get("kind") == "var" and target_node.get("name") in _V3_HOST_ROOTS:
                    return f"{target_node['name']}::{_mname(method)}({args})"
                target = _render_expr(target_node, ctx, rename)
                if target_node.get("kind") not in _ATOMIC_KINDS:
                    target = f"({target})"
                return f"{target}.{_ident(method, 'method')}({args})"
            callee_name = callee_node.get("name") if callee_node.get("kind") == "var" else None
            if callee_name is not None and (
                callee_name in ctx.case_adt or callee_name in _V3_BUILTIN_CONSTRUCTORS
            ):
                return ctx.constructor(callee_name, arg_exprs)
            callee = _render_expr(callee_node, ctx, rename)
            if callee_node.get("kind") not in _ATOMIC_KINDS:
                callee = f"({callee})"
            return f"{callee}({args})"
        # component form: `target.method(args)`.
        target = node.get("target") or {}
        method = _ident(_mname(node.get("method")), "method")
        args = ", ".join(_render_expr(a, ctx, rename) for a in node.get("args") or [])
        if target.get("kind") == "req":
            recv = _ident(target.get("name"), "requirement")
            if rename and target.get("name") in rename:
                recv = rename[target.get("name")]
        else:
            recv = _render_expr(target, ctx, rename)
        return f"{recv}.{method}({args})"

    if kind == "field":
        target_node = node.get("target")
        target = _render_expr(target_node, ctx, rename)
        if target_node.get("kind") not in _ATOMIC_KINDS:
            target = f"({target})"
        return f"{target}.{_ident(node.get('name'), 'field')}"

    if kind == "index":
        target_node = node.get("target")
        target = _render_expr(target_node, ctx, rename)
        if target_node.get("kind") not in _ATOMIC_KINDS:
            target = f"({target})"
        # revl Int is i64; Rust indexing wants usize.
        return f"({target})[({_render_expr(node['index'], ctx, rename)}) as usize].clone()"

    if kind == "if":
        return (
            f"if {_render_expr(node['cond'], ctx, rename)} "
            f"{{ {_render_expr(node['then'], ctx, rename)} }} "
            f"else {{ {_render_expr(node['else'], ctx, rename)} }}"
        )

    if kind == "record_update":
        raise EmitError(
            "functional record update `{r | f = e}` is not emitted by the rust "
            "backend yet (implemented tiers: python, typescript) — see "
            "docs/records.md §6; lift it into a helper fn instead")

    if kind == "record":
        fields = node.get("fields") or []
        type_name = ctx.record_type_for_fields([k for k, _ in fields])
        body = ", ".join(
            f"{_ident(k, 'record field')}: {_render_expr(v, ctx, rename)}" for k, v in fields
        )
        return f"{type_name} {{ {body} }}"

    if kind == "list":
        return ("vec![" + ", ".join(
            _render_expr(item, ctx, rename) for item in node.get("items") or []) + "]")

    if kind == "maplit":
        # `Map.empty()` (docs/stdlib-2.0.md §Map). rustc infers the map's
        # parameters from later use, so an empty literal is context-free.
        return "std::collections::HashMap::new()"

    if kind == "arrow":
        params = ", ".join(_ident(p, "arrow parameter") for p in node.get("params") or [])
        return f"move |{params}| {{ {_render_expr(node['body'], ctx, rename)} }}"

    if kind == "len":
        target = _render_expr(node.get("target"), ctx, rename)
        # Via the helper trait: String::len is bytes, revl length is elements.
        return f"{target}.revl_length()"

    if kind == "builtin":
        target_node = node.get("target")
        target = _render_expr(target_node, ctx, rename)
        if target_node.get("kind") not in _ATOMIC_KINDS:
            target = f"({target})"
        args = [_render_expr(a, ctx, rename) for a in node.get("args") or []]
        return _v3_builtin(node.get("method"), target, args)

    if kind == "match":
        return _v3_match_expr(node, ctx, rename)

    if kind == "interp":
        return _v3_interp(node, ctx, rename)

    if kind in ("optfield", "optcall"):
        raise EmitError(
            f"optional chaining (`?.`) is not yet lowerable on the Rust tier "
            f"({kind!r}); unwrap with `match` or `??` for now"
        )

    if kind == "spawn":
        return _v3_spawn(node, ctx, rename)

    if kind == "instance-get":
        return _v3_instance_get(node, ctx, rename)

    raise EmitError(f"unsupported expression kind {kind!r} in Rust backend")


def _v3_spawn(node: dict, ctx: _V3Ctx, rename: dict[str, str]) -> str:
    """Lower an instance-parametric `spawn` acquisition (docs/design-v2-instances.md).

    `spawn` is the acquisition of a `let-effect` step, so this renders the
    expression the enclosing step wraps in `Arc::new(..)` and binds to the
    handle. It plugs the target *template* as a CHILD FIBER of the spawner —
    each key the template provides isolated into a FRESH LOCAL realm
    (`Context::isolate`, no label -> a distinct `Isolation` per spawn, so two
    instances never collide) — and returns a `RevlSpawnHandle` wrapping that
    fiber. cordis-rs registers the child fiber as a `ctx.plugin()` effect on
    the spawner's frame (registry.rs:501-512), so it is the spawner's own
    nested teardown scope: the safety net that stops an un-disposed instance
    outliving its spawner, while `handle.dispose()` reclaims it earlier.
    """
    target = node.get("component")
    if not isinstance(target, str) or not target.isidentifier():
        raise EmitError(f"bad spawn component {target!r}")
    ctx_expr = rename.get("ctx", "ctx")
    # Each provided key -> its own fresh local realm, applied at plug time (the
    # child fiber's Inject gate resolves against this isolated context, exactly
    # as `_revl_isolate_ctx` does for a statically composed realm placement).
    isolate_chain = ctx_expr
    for key in node.get("realms") or []:
        isolate_chain += f".isolate({_string(key)})"
    if ctx.spawn_target_has_config.get(target):
        cfg_ty = f"{_ident(target, 'component')}Config"
        fields = "".join(
            f"{_ident(k, 'config field')}: {_render_expr(v, ctx, rename)}, "
            for k, v in (node.get("config") or {}).items()
        )
        cfg_expr = f"{cfg_ty} {{ {fields}..Default::default() }}"
    else:
        cfg_expr = "()"
    plug_fn = _snake(target)
    return (
        "{ let __revl_sctx = " + isolate_chain + "; "
        f"let __revl_fiber = __revl_sctx.plugin({plug_fn}(), {cfg_expr}); "
        "RevlSpawnHandle::new(__revl_fiber, __revl_sctx) }"
    )


def _v3_instance_get(node: dict, ctx: _V3Ctx, rename: dict[str, str]) -> str:
    """Lower the instance accessor `s.<key>` (docs/design-v2-instances.md).

    `s : Instance[C]` is a name bound to a `spawn` handle — the emitted
    `RevlSpawnHandle`, which stored the child's own isolated context (the same
    LOCAL realm the matching `spawn` isolated the provided key into). Reading
    `s.<key>` resolves `key` through THAT context, yielding this instance's
    provision and no other's: `RevlSpawnHandle::get` delegates to
    `Context::get_unchecked` (cordis-rs-0.3.0/src/context.rs:358), the
    realm-scoped read. Provisions are stored as `Box<dyn Service>` (the same
    boxing `ctx.provide`/`ctx.require` use), so the type argument mirrors a
    `require`, and the returned `Arc<Box<dyn Service>>` is directly method-
    callable — `s.<key>.method(..)` is the enclosing `call`/`field` node.

    Supervision-tree addressing holds because only the handle holder reaches
    that context: a sibling isolated into a different local realm, and the
    root, resolve `None` (the negative the scenario proves). `service` is the
    frozen inline typing result, so no re-derivation here. The two `expect`s
    are the crash-only contract for a read the frontend already proved sound —
    `key` is a provision of `C` (else a compile error, never emitted) and the
    instance is live when its spawner reads it."""
    handle = _render_expr(node.get("target"), ctx, rename)
    service = node.get("service")
    if not isinstance(service, str) or not service:
        raise EmitError(f"instance-get: bad frozen service type {service!r}")
    key = node.get("key")
    if not isinstance(key, str) or not key.isidentifier():
        raise EmitError(f"instance-get: bad key {key!r}")
    return (
        f"{handle}.get::<Box<dyn {service}>>({_string(key)})"
        '.expect("revl: instance-get resolution failed")'
        '.expect("revl: instance provision absent")'
    )


# The total, value-returning division forms (docs/arithmetic.md): same
# rounding as the faulting operations, Err(reason) at a zero divisor.
_CHECKED_DIVS = ("checked_div_trunc", "checked_div_floor",
                 "checked_div_euclid", "checked_mod")
_DIV_ZERO_MSG = "revl: division by zero"


def _v3_checked_div(method: str, target: str, arg: str) -> str:
    """The total forms (docs/arithmetic.md): same quotient as the faulting
    operation, but a zero divisor yields Err(reason) instead of panicking —
    `fail` is refused in a pure fn, so the error travels as a value. The
    turbofish pins Result<i64, String> so the expression is context-free;
    operands are bound once, since a block evaluates them exactly once."""
    ok = {
        "checked_div_trunc": "a / b",
        "checked_div_floor":
            "{ let q = a / b; if a % b != 0 && ((a < 0) != (b < 0)) { q - 1 } else { q } }",
        "checked_div_euclid": "a.div_euclid(b)",
        "checked_mod": "a.rem_euclid(b).abs()",
    }[method]
    return (f"{{ let a = ({target}); let b = ({arg}); "
            f'if b == 0 {{ Err::<i64, String>("{_DIV_ZERO_MSG}".to_string()) }} '
            f'else {{ Ok::<i64, String>({ok}) }} }}')


def _v3_builtin(method: str, target: str, args: list[str]) -> str:
    """The stdlib surface (docs/stdlib-2.0.md), dispatched via the Revl*Ops
    helper traits so every (method, Str|List) pair from the spec table
    compiles — Rust resolves the receiver type statically."""
    if method == "length":
        return f"{target}.revl_length()"
    if method == "push":
        return f"{target}.revl_push({args[0]})"
    if method == "concat":
        return f"{target}.revl_concat(&{args[0]})"
    if method == "slice":
        return f"{target}.revl_slice({args[0]}, {args[1]})"
    if method == "charAt":
        return f"{{ {target}.chars().nth(({args[0]}) as usize).unwrap().to_string() }}"
    if method == "charCodeAt":
        return f"{{ {target}.chars().nth(({args[0]}) as usize).unwrap() as u32 as i64 }}"
    if method == "indexOf":
        return f"{target}.revl_index_of(&{args[0]})"
    if method == "split":
        return f"{target}.revl_split(&{args[0]})"
    if method == "join":
        return f"{target}.revl_join(&{args[0]})"
    if method == "repeat":
        return f"{target}.revl_repeat({args[0]})"
    # The Map value type (docs/stdlib-2.0.md §Map): a std HashMap, cloned on
    # write. Every revl value type derives Clone on this tier, so the copy
    # is total; `lookup` answers Option<V> (the tier's Opt) via cloned().
    if method == "set":
        return f"{{ let mut c = {target}.clone(); c.insert({args[0]}, {args[1]}); c }}"
    if method == "lookup":
        return f"{target}.get(&{args[0]}).cloned()"
    if method == "has":
        return f"{target}.contains_key(&{args[0]})"
    # The iteration/remove step (docs/stdlib-2.0.md §Map). String: Ord is
    # UTF-8 byte order — the canonical Str order on this tier. keys collects
    # into a Vec<String> (the List[Str] representation) and sorts a copy;
    # remove clones before deleting, so the receiver never mutates.
    if method == "size":
        return f"({target}.len() as i64)"
    if method == "keys":
        return (f"{{ let mut ks: std::vec::Vec<String> = "
                f"{target}.keys().cloned().collect(); ks.sort(); ks }}")
    if method == "remove":
        return f"{{ let mut c = {target}.clone(); c.remove(&{args[0]}); c }}"
    # Integer division and modulo (docs/arithmetic.md). Rust `/` already
    # truncates and carries div_euclid/rem_euclid in std (stable since 1.38);
    # div_floor is spelled out rather than using the much newer
    # `i64::div_floor`, so the emitted crate does not need a recent toolchain.
    # Int/Int32 width conversions (docs/arithmetic.md). Widening Int32 -> Int is
    # a lossless `as i64`; narrowing Int -> Int32 goes through `i32::try_from`,
    # which returns Err out of range — `.expect` turns that into the trap.
    if method == "to_int":
        return f"(({target}) as i64)"
    if method == "to_int32":
        return f'(i32::try_from({target}).expect("revl: Int32 overflow"))'
    if method == "div_trunc":
        return f"(({target}) / ({args[0]}))"
    if method == "div_floor":
        return (f"{{ let (a, b) = (({target}), ({args[0]})); let q = a / b; "
                f"if a % b != 0 && ((a < 0) != (b < 0)) {{ q - 1 }} else {{ q }} }}")
    if method == "div_euclid":
        return f"(({target}).div_euclid({args[0]}))"
    if method == "mod":
        return f"(({target}).rem_euclid({args[0]}).abs())"
    # The total forms (docs/arithmetic.md): same quotient as the faulting
    # operation, but a zero divisor yields Err(reason) instead of panicking —
    # `fail` is refused in a pure fn, so the error travels as a value. The
    # turbofish pins Result<i64, String> so the expression is context-free;
    # operands are bound once, since a block evaluates them exactly once.
    if method in _CHECKED_DIVS:
        return _v3_checked_div(method, target, args[0])
    # The rendering builtin (docs/stdlib-2.0.md §Int.to_str): i64::to_string
    # is exact decimal over the whole range, Int.MIN included, and String is
    # this tier's Str.
    if method == "to_str":
        return f"({target}).to_string()"
    raise EmitError(f"unknown builtin method {method!r}")


def _stdlib_helper_traits() -> list[str]:
    """Emitted once per module when any builtin/len node is present.

    Parity notes: string positions are char-based (matching the Python
    backend, where str indexing is per code point); `revl_index_of` returns
    -1 when absent on both hosts; `revl_push`/`revl_concat` are persistent
    (docs/stdlib-2.0.md).
    """
    return [
        "trait RevlStrOps {",
        "    fn revl_length(&self) -> i64;",
        "    fn revl_slice(&self, a: i64, b: i64) -> String;",
        "    fn revl_index_of(&self, needle: &String) -> i64;",
        "    fn revl_concat(&self, other: &String) -> String;",
        "    fn revl_split(&self, sep: &String) -> Vec<String>;",
        "    fn revl_repeat(&self, n: i64) -> String;",
        "}",
        "impl RevlStrOps for String {",
        "    fn revl_length(&self) -> i64 { self.chars().count() as i64 }",
        "    fn revl_slice(&self, a: i64, b: i64) -> String {",
        "        self.chars().skip(a.max(0) as usize).take((b - a).max(0) as usize).collect()",
        "    }",
        "    fn revl_index_of(&self, needle: &String) -> i64 {",
        "        let hay: Vec<char> = self.chars().collect();",
        "        let nee: Vec<char> = needle.chars().collect();",
        "        if nee.is_empty() { return 0; }",
        "        if nee.len() > hay.len() { return -1; }",
        "        for i in 0..=(hay.len() - nee.len()) {",
        "            if hay[i..i + nee.len()] == nee[..] { return i as i64; }",
        "        }",
        "        -1",
        "    }",
        "    fn revl_concat(&self, other: &String) -> String { format!(\"{}{}\", self, other) }",
        "    fn revl_split(&self, sep: &String) -> Vec<String> {",
        "        if sep.is_empty() {",
        "            self.chars().map(|c| c.to_string()).collect()",
        "        } else {",
        "            self.split(sep.as_str()).map(|s| s.to_string()).collect()",
        "        }",
        "    }",
        "    fn revl_repeat(&self, n: i64) -> String { self.repeat(n.max(0) as usize) }",
        "}",
        "trait RevlStrListOps {",
        "    fn revl_join(&self, sep: &String) -> String;",
        "}",
        "impl RevlStrListOps for Vec<String> {",
        "    fn revl_join(&self, sep: &String) -> String { self.join(sep.as_str()) }",
        "}",
        "trait RevlListOps<T> {",
        "    fn revl_length(&self) -> i64;",
        "    fn revl_slice(&self, a: i64, b: i64) -> Vec<T>;",
        "    fn revl_index_of(&self, needle: &T) -> i64;",
        "    fn revl_concat(&self, other: &Vec<T>) -> Vec<T>;",
        "    fn revl_push(&self, item: T) -> Vec<T>;",
        "}",
        "impl<T: Clone + PartialEq> RevlListOps<T> for Vec<T> {",
        "    fn revl_length(&self) -> i64 { self.len() as i64 }",
        "    fn revl_slice(&self, a: i64, b: i64) -> Vec<T> {",
        "        // JS slice semantics: out-of-range bounds clamp, never panic.",
        "        let len = self.len();",
        "        let a2 = (a.max(0) as usize).min(len);",
        "        let b2 = (b.max(0) as usize).min(len).max(a2);",
        "        self[a2..b2].to_vec()",
        "    }",
        "    fn revl_index_of(&self, needle: &T) -> i64 {",
        "        self.iter().position(|x| x == needle).map(|i| i as i64).unwrap_or(-1)",
        "    }",
        "    fn revl_concat(&self, other: &Vec<T>) -> Vec<T> {",
        "        let mut _v = self.clone(); _v.extend(other.iter().cloned()); _v",
        "    }",
        "    fn revl_push(&self, item: T) -> Vec<T> {",
        "        let mut _v = self.clone(); _v.push(item); _v",
        "    }",
        "}",
        "",
    ]


def _v3_interp(node: dict, ctx: _V3Ctx, rename: dict[str, str] | None = None) -> str:
    parts = node.get("parts") or []
    format_parts: list[str] = []
    args: list[str] = []
    for kind, value in parts:
        if kind == "text":
            format_parts.append(value.replace("{", "{{").replace("}", "}}"))
        elif _v3_is_float(value):
            # A `Float` renders through the canonical ECMAScript form, not
            # Rust's `{}` (`format!("{}", 1e21)` is the full 22-digit expansion
            # and `-0.0` is `-0`); see docs/strings.md.
            format_parts.append("{}")
            args.append(f"revl_ftoa({_render_expr(value, ctx, rename)})")
        else:  # ["expr", ir_node]
            format_parts.append("{}")
            args.append(_render_expr(value, ctx, rename))
    joined = "".join(format_parts)
    if not args:
        return f"format!({_string(joined)})"
    return f"format!({_string(joined)}, {', '.join(args)})"


def _v3_match_expr(node: dict, ctx: _V3Ctx, rename: dict[str, str] | None = None) -> str:
    scrutinee = _render_expr(node.get("scrutinee"), ctx, rename)
    arms = node.get("arms") or []
    lines = [f"match {scrutinee} {{"]
    for arm in arms:
        pattern = ctx.match_pattern(arm)
        body = _render_expr(arm.get("body"), ctx, rename)
        lines.append(f"    {pattern} => {body},")
    if not any(arm.get("pattern") == "_" for arm in arms):
        # lower.py has already checked exhaustiveness for known ADTs.
        lines.append("    _ => unreachable!(),")
    lines.append("}")
    return "\n".join(lines)



def _v3_let_pattern(node: dict, ctx: _V3Ctx, out: list[str], indent: int) -> None:
    pad = "    " * indent
    value = _render_expr(node.get("value"), ctx)
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
        inferred = _v3_infer_type(node.get("value"), ctx)
        value = _render_expr(node.get("value"), ctx)
        if inferred is not None:
            ctx.var_types[node.get("name")] = inferred
        if step == "let":
            keyword = "let mut" if node.get("mutable") else "let"
            out.append(f"{pad}{keyword} {name} = {value};")
        else:
            out.append(f"{pad}{name} = {value};")
    elif step == "return":
        if node.get("expr") is None:
            out.append(f"{pad}return;")
        else:
            out.append(f"{pad}return {_render_expr(node['expr'], ctx)};")
    elif step == "if":
        out.append(f"{pad}if {_render_expr(node['cond'], ctx)} {{")
        for child in node.get("then") or []:
            _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        if node.get("else"):
            out.append(f"{pad}}} else {{")
            for child in node["else"]:
                _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        out.append(f"{pad}}}")
    elif step == "while":
        out.append(f"{pad}while {_render_expr(node['cond'], ctx)} {{")
        for child in node.get("body") or []:
            _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        out.append(f"{pad}}}")
    elif step == "for":
        bind = _ident(node.get("bind"), "loop binding")
        out.append(f"{pad}for {bind} in {_render_expr(node['iterable'], ctx)} {{")
        for child in node.get("body") or []:
            _v3_stmt(child, ctx, out, indent + 1, test_mode=test_mode)
        out.append(f"{pad}}}")
    elif step == "let_pattern":
        _v3_let_pattern(node, ctx, out, indent)
    elif step == "expr":
        out.append(f"{pad}let _ = {_render_expr(node['expr'], ctx)};")
    elif step == "assert":
        out.append(f"{pad}assert!({_render_expr(node['expr'], ctx)});")
    else:
        raise EmitError(f"unsupported fn statement step {step!r}")



def _emit_v3_types(types: dict) -> list[str]:
    # PartialEq is not decoration: revl has one equality and it is
    # structural (syntax-2.0 §3.4), so `{a: 1} == {a: 1}` must compile and
    # be true here as it is on python. Without the derive, rustc refuses
    # the comparison outright (E0369) and legal revl fails on this tier.
    out: list[str] = []
    for name, spec in types.items():
        name = _ident(name, "type name")
        if spec.get("kind") == "record":
            out.append("#[derive(Clone, Debug, PartialEq, serde::Serialize, serde::Deserialize)]")
            out.append(f"pub struct {name} {{")
            for field, ftype in (spec.get("fields") or {}).items():
                out.append(f"    {_ident(field, 'record field')}: {_rust_type(ftype, types)},")
            out.append("}")
        elif spec.get("kind") == "variant":
            out.append("#[derive(Clone, Debug, PartialEq, serde::Serialize, serde::Deserialize)]")
            out.append('#[serde(tag = "$kind", content = "$value")]')
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
        ctx.var_types = {p.get("name"): p.get("type") for p in fn.get("params") or []}
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
        if test.get("lifecycle"):
            # lifecycle tests are emitted by _emit_v3_lifecycle_tests — their
            # body is a script over a live composition, not pure statements
            continue
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


def _emit_v3_lifecycle_tests(tests: list, types: dict, functions: list,
                             externs: list, services: dict,
                             components: list) -> list[str]:
    """`lifecycle test` blocks (syntax-2.0 §7.1) as ``#[test]`` fns driving a
    live cordis-rs context.

    A lifecycle test is a script over a *live* composition: load components
    into a ``cordis::Context``, call through provision keys, unload them LIFO,
    and assert the runtime holds nothing afterwards. It lowers to the tier's
    native test idiom exactly the way a plain ``test`` block does (FR-5):
    ``#[test]`` fns that reuse the generated bridge plumbing — ``_revl_load``
    for loads, typed ``ctx.require`` calls through the service traits for
    calls — and prove ``assert no_residue`` with the same introspection the
    ``run --once`` driver uses (``registry().len() == 0`` and
    ``reflect().services().len() == 0``, docs/backend-ir.md R4).

    The lowerer checks the script statically (every key's provider must be
    loaded, G2 provision disjointness), so a refused call is a compile error
    before any of this runs; here the script becomes Rust.
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
    method_tables = {
        sname: (svc.get("methods") or {})
        for sname, svc in services.items()
    }
    ctx = _V3Ctx(types, functions, externs, components)
    out: list[str] = []
    used: set[str] = set()
    for test in tests:
        if not test.get("lifecycle"):
            continue
        where = f"lifecycle test {_string(test['name'])}"
        base = _snake(test.get("name") or "lifecycle")
        base = re.sub(r"[^A-Za-z0-9_]", "_", base)
        if not base or base[0].isdigit():
            base = "lifecycle_" + base
        name = f"revl_lifecycle_{base}"
        counter = 0
        while name in used:
            counter += 1
            name = f"revl_lifecycle_{base}_{counter}"
        used.add(name)
        out.append("#[test]")
        out.append(f"fn {name}() {{")
        out.append("    // drives the composition on a real cordis-rs context and")
        out.append("    // proves no residue after LIFO teardown (FR-5 / §7.1).")
        out.append("    let root = cordis::Context::new();")
        out.append("    let mut _revl_fibers: Vec<(&str, cordis::Fiber)> = Vec::new();")
        for step in test.get("body") or []:
            kind = step.get("step")
            if kind == "load":
                component = step["component"]
                snake = _snake(component)
                cfg = step.get("config") or {}
                cfg_items = ", ".join(
                    f"{_string(field)}: {_render_expr(value, ctx, {})}"
                    for field, value in cfg.items())
                out.append("    {")
                out.append("        let _cfg = serde_json::json!("
                           f'{{{_string(component)}: {{{cfg_items}}}}});')
                out.append(f'        let _f = _revl_load(&root, {_string(snake)}, &_cfg)'
                           f".expect({_string(where + ': load ' + component)});")
                out.append(f'        _f.wait().expect({_string(where + ": " + component + " did not reach ACTIVE (R2)")});')
                out.append(f"        _revl_fibers.push(({_string(snake)}, _f));")
                out.append("    }")
            elif kind == "unload":
                component = step["component"]
                snake = _snake(component)
                out.append(f'    if let Some(pos) = _revl_fibers.iter().position(|(n, _)| *n == {_string(snake)}) {{')
                out.append("        let (_, _f) = _revl_fibers.remove(pos);")
                out.append(f"        _f.dispose().expect({_string(where + ': unload ' + component)});")
                out.append("    }")
            elif kind == "call":
                key = step["key"]
                service = provided.get(key)
                if service is None:  # pragma: no cover — the lowerer rejects it
                    raise EmitError(f"{where}: no provider for key {key!r}")
                method_name = _mname(step["method"])
                method = (method_tables.get(service) or {}).get(step["method"])
                if method is None:  # pragma: no cover — the lowerer rejects it
                    raise EmitError(f"{where}: unknown method {step['method']!r}")
                args = ", ".join(_render_expr(arg, ctx, {})
                                 for arg in step.get("args") or [])
                call = (f'root.require::<Box<dyn {service}>>({_string(key)})'
                        f".expect({_string(where + ': ' + key + ' is ACTIVE (R2)')})"
                        f".{method_name}({args})")
                bind = step.get("bind")
                if bind is not None:
                    out.append(f"    let {_ident(bind, 'lifecycle binding')} = {call};")
                    if method.get("returns"):
                        ctx.var_types[bind] = method.get("returns")
                else:
                    out.append(f"    let _ = {call};")
            elif kind == "assert":
                out.append(f"    assert!({_render_expr(step['expr'], ctx, {})}, "
                           f"{_string(where + ': assertion failed')});")
            elif kind == "assert_no_residue":
                out.append("    // R4 + R1: the composition must leave the live runtime")
                out.append("    // holding nothing — no plugin, no provided service, no")
                out.append("    // unreleased host resource — the same checks the py")
                out.append("    // reference tier's `assert no_residue` performs and the")
                out.append("    // registry/reflect half of `revl run --once`.")
                out.append("    assert!(root.registry().len() == 0"
                           " && root.reflect().services().len() == 0"
                           " && REVL_LIVE_HOST_RESOURCES.load(std::sync::atomic::Ordering::SeqCst) == 0,")
                out.append(f'            {_string(where + ": residue \u2014 the host runtime still holds state (R4/R1)")});')
            else:  # pragma: no cover — the lowerer emits nothing else
                raise EmitError(f"{where}: unknown lifecycle step {kind!r}")
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
        f"//! Generated by the revl cordis-rs backend (ir_version {version}): do not edit.",
        f"//! Target: {CRATE} 0.3.x (docs.rs/cordis-rs).",
        allow,
        "",
        "use std::sync::Arc;",
        "use cordis::Value;",
        "",
    ]


def _needs_realm_helper(components: list) -> bool:
    return any(component.get("isolate") for component in components)


def _uses_spawn(components: list) -> bool:
    """True when any component acquires an instance via `spawn`."""
    for component in components:
        for step in component.get("body") or []:
            if (step.get("step") == "let-effect"
                    and (step.get("acquire") or {}).get("kind") == "spawn"):
                return True
    return False


def _revl_spawn_handle() -> list[str]:
    """The value a `spawn` acquisition binds: a live component instance, torn
    down by its own `.dispose()` (docs/design-v2-instances.md, phase 1).

    The instance is a CHILD FIBER of its spawner — its own nested teardown
    scope. `dispose()` unloads that fiber, running the instance's LIFO teardown
    NOW, independent of the spawner: a request-scoped instance is reclaimed
    when the request ends, never deferred to the component's teardown. Disposal
    is idempotent (a `Mutex<Option<Fiber>>` taken once), so the spawner's own
    inverse — and cordis-rs's `ctx.plugin()` parent-effect safety net — are
    harmless no-ops once the instance is already gone. `get` reads a provision
    the instance published in ITS local realm: only the holder of this handle
    (the spawner) can reach it; a sibling, isolated into a different local
    realm, cannot (supervision-tree addressing)."""
    return [
        "/// A live spawned-component instance (docs/design-v2-instances.md).",
        "pub struct RevlSpawnHandle {",
        "    fiber: std::sync::Mutex<Option<cordis::Fiber>>,",
        "    ctx: cordis::Context,",
        "}",
        "",
        "impl RevlSpawnHandle {",
        "    pub fn new(fiber: cordis::Fiber, ctx: cordis::Context) -> Self {",
        "        Self { fiber: std::sync::Mutex::new(Some(fiber)), ctx }",
        "    }",
        "    /// Unload the instance's fiber (its LIFO teardown). Idempotent.",
        "    pub fn dispose(&self) -> cordis::Result<()> {",
        "        let taken = self.fiber.lock().unwrap().take();",
        "        if let Some(fiber) = taken { fiber.dispose()?; }",
        "        Ok(())",
        "    }",
        "    /// Read a provision the instance published, in its own local realm.",
        "    pub fn get<T: Send + Sync + 'static>(&self, name: &str)",
        "        -> cordis::Result<Option<std::sync::Arc<T>>> {",
        "        self.ctx.get_unchecked::<T>(name)",
        "    }",
        "    /// The instance's fiber lifecycle state (Active while live).",
        "    pub fn state(&self) -> Option<cordis::FiberState> {",
        "        self.fiber.lock().unwrap().as_ref().map(|f| f.state())",
        "    }",
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


def _uses_float_interp(ir: dict) -> bool:
    """True when any `${…}` template interpolates a provably-`Float`
    expression, so the canonical Float renderer is emitted only then."""
    found = False

    def walk(node) -> None:
        nonlocal found
        if found:
            return
        if isinstance(node, dict):
            if node.get("kind") == "interp":
                for part in node.get("parts") or []:
                    if (isinstance(part, (list, tuple)) and len(part) == 2
                            and part[0] == "expr" and _v3_is_float(part[1])):
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


def _revl_ftoa_helper() -> list[str]:
    """Canonical Float -> Str: ECMAScript Number::toString (docs/strings.md).
    `{:e}` supplies the shortest round-trip digits; this reformats them into
    the ES notation so `${aFloat}` agrees with every other tier."""
    return [
        "fn revl_ftoa(x: f64) -> String {",
        '    if x.is_nan() { return "NaN".to_string(); }',
        "    if x.is_infinite() {",
        '        return (if x < 0.0 { "-Infinity" } else { "Infinity" }).to_string();',
        "    }",
        '    if x == 0.0 { return "0".to_string(); }',
        '    let sign = if x < 0.0 { "-" } else { "" };',
        '    let s = format!("{:e}", x.abs());',
        "    let (mant, exp): (&str, i64) = match s.find('e') {",
        "        Some(i) => (&s[..i], s[i + 1..].parse().unwrap()),",
        "        None => (s.as_str(), 0),",
        "    };",
        "    let (intpart, frac) = match mant.find('.') {",
        "        Some(i) => (&mant[..i], &mant[i + 1..]),",
        '        None => (mant, ""),',
        "    };",
        '    let mut digits = format!("{}{}", intpart, frac);',
        "    let mut point = intpart.len() as i64 + exp;",
        "    let mut lead = 0;",
        "    let b = digits.clone();",
        "    let bb = b.as_bytes();",
        "    while lead + 1 < bb.len() && bb[lead] == b'0' { lead += 1; point -= 1; }",
        "    digits = digits[lead..].to_string();",
        "    while digits.len() > 1 && digits.ends_with('0') { digits.pop(); }",
        '    if digits == "0" { return "0".to_string(); }',
        "    let k = digits.len() as i64;",
        "    let n = point;",
        "    let body = if k <= n && n <= 21 {",
        '        format!("{}{}", digits, "0".repeat((n - k) as usize))',
        "    } else if 0 < n && n <= 21 {",
        "        let p = n as usize;",
        '        format!("{}.{}", &digits[..p], &digits[p..])',
        "    } else if -6 < n && n <= 0 {",
        '        format!("0.{}{}", "0".repeat((-n) as usize), digits)',
        "    } else {",
        "        let e = n - 1;",
        "        let m = if k > 1 {",
        '            format!("{}.{}", &digits[..1], &digits[1..])',
        "        } else {",
        "            digits.clone()",
        "        };",
        '        let (esign, ea) = if e < 0 { ("-", -e) } else { ("+", e) };',
        '        format!("{}e{}{}", m, esign, ea)',
        "    };",
        '    format!("{}{}", sign, body)',
        "}",
    ]


# --------------------------------------------------------------------------
# interop bridge (docs/interop-bridge.md §3): generated per-service proxy +
# stub, so a cordis-rs process can consume/serve a cross-process key. cordis-rs
# services are static traits, so this is codegen (a runtime-generic proxy is
# impossible in Rust, unlike py attribute access or node's `Proxy`).
# --------------------------------------------------------------------------

# Scalar returns get lenient, hand-written JSON conversions (byte-identical to
# the first cut, so the golden holds). Anything else that is not the opaque
# cordis `Value` is marshalled generically through serde (records, ADTs, Map,
# Result, and their Vec/Option nestings, once the emitted types derive
# Serialize/Deserialize). `Value` alone stays unmarshallable: it is opaque.
_SCALAR_OPTION = ("Option<String>", "Option<i64>", "Option<bool>", "Option<f64>")


def _bridge_serde_ok(rtype: str) -> bool:
    """True when `rtype` can cross via serde: not the opaque cordis `Value`."""
    return not re.search(r"\bValue\b", rtype)


def _split_result(rtype: str):
    """`Result<T, E>` -> (T, E), else None. std `Result` serializes
    externally-tagged, so the bridge encodes it to the canonical
    `{"$kind","$value"}` by hand rather than via serde."""
    match = re.match(r"^Result<(.+)>$", rtype.strip())
    if not match:
        return None
    inner, depth = match.group(1), 0
    for i, ch in enumerate(inner):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif ch == "," and depth == 0:
            return inner[:i].strip(), inner[i + 1:].strip()
    return None


def _result_ok_err(value: str, ok_ty: str, err_ty: str) -> str:
    """Rust expr building a std Result from a canonical `{"$kind","$value"}`."""
    payload = f'_r.get("$value").cloned().unwrap_or(serde_json::Value::Null)'
    return (f'{{ let _r = {value}.clone(); '
            f'if _r.get("$kind").and_then(|k| k.as_str()) == Some("Ok") {{ '
            f'Ok(serde_json::from_value::<{ok_ty}>({payload}).expect("bridge: decode Ok")) }} '
            f'else {{ Err(serde_json::from_value::<{err_ty}>({payload}).expect("bridge: decode Err")) }} }}')


def _result_to_json(call: str) -> str:
    """Rust expr encoding a std Result to the canonical `{"$kind","$value"}`."""
    return (f'{{ match {call} {{ '
            f'Ok(_v) => serde_json::json!({{"$kind": "Ok", "$value": serde_json::to_value(&_v).unwrap_or(serde_json::Value::Null)}}), '
            f'Err(_e) => serde_json::json!({{"$kind": "Err", "$value": serde_json::to_value(&_e).unwrap_or(serde_json::Value::Null)}}) }} }}')


def _bridge_arg_ser(name: str, rtype: str) -> str:
    """Serialize a proxy method argument to a serde_json::Value."""
    if rtype in ("String", "i64", "f64", "bool"):
        return f"serde_json::json!({name})"
    if _split_result(rtype):
        return _result_to_json(name)
    if _bridge_serde_ok(rtype):
        return f"serde_json::to_value(&{name}).unwrap_or(serde_json::Value::Null)"
    return "serde_json::Value::Null"  # opaque Value param: not marshalled


def _bridge_arg_extract(index: int, rtype: str) -> str | None:
    """Extract a stub-dispatch argument from `args[index]` at type `rtype`."""
    if rtype == "String":
        return f'args[{index}].as_str().unwrap_or("").to_string()'
    if rtype == "i64":
        return f"args[{index}].as_i64().unwrap_or(0)"
    if rtype == "f64":
        return f"args[{index}].as_f64().unwrap_or(0.0)"
    if rtype == "bool":
        return f"args[{index}].as_bool().unwrap_or(false)"
    result = _split_result(rtype)
    if result:
        return _result_ok_err(f"args[{index}]", result[0], result[1])
    if _bridge_serde_ok(rtype):
        return f'serde_json::from_value::<{rtype}>(args[{index}].clone()).expect("bridge: decode arg")'
    return None  # opaque Value param: this method is not served


def _bridge_ret_deser(value: str, rtype: str) -> str | None:
    """Turn a reply serde_json::Value into the method's Rust return type."""
    table = {
        "i64": f"{value}.as_i64().unwrap_or(0)",
        "String": f'{value}.as_str().unwrap_or("").to_string()',
        "bool": f"{value}.as_bool().unwrap_or(false)",
        "f64": f"{value}.as_f64().unwrap_or(0.0)",
        "()": f"{{ let _ = {value}; }}",
        "Option<String>": f"{value}.as_str().map(|s| s.to_string())",
        "Option<i64>": f"{value}.as_i64()",
        "Option<bool>": f"{value}.as_bool()",
        "Vec<Value>": (f"{value}.as_array().map(|a| a.iter().map(|x| "
                       f"Value::new(x.to_string())).collect()).unwrap_or_default()"),
    }
    if rtype in table:
        return table[rtype]
    result = _split_result(rtype)
    if result:
        return _result_ok_err(value, result[0], result[1])
    if _bridge_serde_ok(rtype):
        return f'serde_json::from_value::<{rtype}>({value}.clone()).expect("bridge: decode return")'
    return None  # opaque cordis Value: not marshallable


def _bridge_ret_ser(call: str, rtype: str) -> str:
    """Serialize a stub method's return value to a serde_json::Value."""
    if rtype in ("i64", "String", "bool", "f64") or rtype in _SCALAR_OPTION:
        return f"serde_json::json!({call})"
    if rtype == "()":
        return f"{{ {call}; serde_json::Value::Null }}"
    if rtype == "Vec<Value>":
        return (f"{{ let _r = {call}; serde_json::json!(_r.iter().map(|v| "
                f"v.downcast::<String>().map(|s| (*s).clone()).unwrap_or_default())"
                f".collect::<Vec<_>>()) }}")
    if _split_result(rtype):
        return _result_to_json(call)
    if _bridge_serde_ok(rtype):
        return f"{{ let _r = {call}; serde_json::to_value(&_r).unwrap_or(serde_json::Value::Null) }}"
    return f"{{ let _ = {call}; serde_json::Value::Null }}"  # opaque Value return


def _emit_bridge(ir: dict) -> list[str]:
    services = ir.get("services") or {}
    components = ir.get("components") or []
    types = ir.get("types") or {}
    if not services:
        return []
    provided: dict[str, str] = {}
    for component in components:
        for key, service in (component.get("provides") or {}).items():
            provided[key] = service

    out: list[str] = [
        "// ---- interop bridge (generated; docs/interop-bridge.md) ----",
        "use std::io::{BufRead, Write};",
        "use std::os::unix::net::UnixStream;",
        "",
        "fn _revl_rpc(socket: &str, key: &str, method: &str, args: Vec<serde_json::Value>) -> serde_json::Value {",
        "    let request = serde_json::json!({ \"key\": key, \"method\": method, \"args\": args });",
        "    let mut stream = None;",
        "    for _ in 0..200 {",
        "        match UnixStream::connect(socket) {",
        "            Ok(s) => { stream = Some(s); break; }",
        "            Err(_) => std::thread::sleep(std::time::Duration::from_millis(50)),",
        "        }",
        "    }",
        "    let stream = stream.expect(\"bridge connect (provider never came up)\");",
        "    let mut writer = stream.try_clone().expect(\"clone stream\");",
        "    let mut line = serde_json::to_string(&request).unwrap();",
        "    line.push('\\n');",
        "    writer.write_all(line.as_bytes()).expect(\"write request\");",
        "    let mut reader = std::io::BufReader::new(stream);",
        "    let mut response = String::new();",
        "    reader.read_line(&mut response).expect(\"read reply\");",
        "    let reply: serde_json::Value = serde_json::from_str(&response).expect(\"parse reply\");",
        "    if !reply[\"ok\"].as_bool().unwrap_or(false) {",
        "        panic!(\"bridge remote error: {}\", reply[\"error\"]);",
        "    }",
        "    reply[\"value\"].clone()",
        "}",
        "",
    ]

    for sname, service in services.items():
        methods = service.get("methods") or {}
        # consumer-side proxy
        out.append(f"pub struct {sname}Proxy {{ pub socket: String, pub key: String }}")
        out.append(f"impl {sname} for {sname}Proxy {{")
        for mname, method in methods.items():
            params = method.get("params") or []
            plist = ", ".join(f"{p['name']}: {_rust_type(p.get('type'), types)}" for p in params)
            ret = _rust_type(method.get("returns"), types) if method.get("returns") else "()"
            argvec = ", ".join(_bridge_arg_ser(p["name"], _rust_type(p.get("type"), types)) for p in params)
            deser = _bridge_ret_deser("_v", ret)
            out.append(f"    fn {_mname(mname)}(&self, {plist}) -> {ret} {{")
            if deser is None:
                out.append(f'        panic!("bridge proxy: unsupported return type for {sname}.{mname}");')
            else:
                out.append(f'        let _v = _revl_rpc(&self.socket, &self.key, "{mname}", vec![{argvec}]);')
                out.append(f"        {deser}")
            out.append("    }")
        out.append("}")
        # provider-side dispatch
        out.append(f"fn _revl_dispatch_{_snake(sname)}(svc: &dyn {sname}, method: &str, "
                   "args: &[serde_json::Value]) -> serde_json::Value {")
        out.append("    match method {")
        for mname, method in methods.items():
            params = method.get("params") or []
            ret = _rust_type(method.get("returns"), types) if method.get("returns") else "()"
            extracts = [_bridge_arg_extract(i, _rust_type(p.get("type"), types)) for i, p in enumerate(params)]
            if any(e is None for e in extracts):
                out.append(f'        "{mname}" => serde_json::Value::Null, // unmarshalled param type')
                continue
            call = f"svc.{_mname(mname)}({', '.join(extracts)})"
            out.append(f'        "{mname}" => {_bridge_ret_ser(call, ret)},')
        out.append("        _ => serde_json::Value::Null,")
        out.append("    }")
        out.append("}")
        out.append("")

    # key -> service name (over provided keys)
    out.append("pub fn _revl_service_of(key: &str) -> Option<&'static str> {")
    out.append("    match key {")
    for key, service in provided.items():
        out.append(f'        "{key}" => Some("{service}"),')
    out.append("        _ => None,")
    out.append("    }")
    out.append("}")
    out.append("")

    # consumer: a plugin that provides `key` via the right proxy
    out.append("pub fn _revl_proxy_plugin(key: &str, service: &str, socket: String) "
               "-> Option<cordis::PluginHandle> {")
    out.append("    let key_string = key.to_string();")
    out.append("    match service {")
    for sname in services:
        out.append(f'        "{sname}" => Some(cordis::plugin_sync::<(), _>(')
        out.append(f'            "{sname}Proxy",')
        out.append("            cordis::Inject::none(),")
        out.append("            move |ctx, _config| {")
        out.append(f"                let proxy: Box<dyn {sname}> = Box::new({sname}Proxy "
                   "{ socket: socket.clone(), key: key_string.clone() });")
        out.append("                ctx.provide(key_string.as_str(), proxy)?;")
        out.append("                Ok(cordis::PluginOutput::none())")
        out.append("            },")
        out.append("        )),")
    out.append("        _ => None,")
    out.append("    }")
    out.append("}")
    out.append("")

    # provider/probe: require a locally-provided key and dispatch to it
    out.append("pub fn _revl_invoke(ctx: &cordis::Context, key: &str, method: &str, "
               "args: &[serde_json::Value]) -> serde_json::Value {")
    out.append("    match key {")
    for key, service in provided.items():
        out.append(f'        "{key}" => match ctx.require::<Box<dyn {service}>>("{key}") {{')
        out.append(f"            Ok(svc) => _revl_dispatch_{_snake(service)}(&**svc, method, args),")
        out.append("            Err(_) => serde_json::Value::Null,")
        out.append("        },")
    out.append("        _ => serde_json::Value::Null,")
    out.append("    }")
    out.append("}")
    out.append("")

    # component name -> plugin handle
    out.append("pub fn plugin_by_name(name: &str) -> Option<cordis::PluginHandle> {")
    out.append("    match name {")
    for component in components:
        snake = _snake(component["name"])
        out.append(f'        "{snake}" => Some({snake}()),')
    out.append("        _ => None,")
    out.append("    }")
    out.append("}")
    out.append("")

    # component name -> context isolated for that component's realm placements.
    # cordis fixes a fiber's isolation scope at plug time (its `Inject` gate is
    # evaluated against this context, before the plugin body runs), so every
    # plug site must isolate the context HERE rather than inside the plugin
    # body. This mirrors the python/typescript `plug()` helper and is what
    # lets an isolated `requires kv in realm("t")` reactively link to an
    # isolated provider in the same realm instead of hanging Pending.
    out.append("pub fn _revl_isolate_ctx(ctx: &cordis::Context, name: &str) -> cordis::Context {")
    out.append("    match name {")
    for component in components:
        isolate = component.get("isolate") or {}
        if not isolate:
            continue
        snake = _snake(component["name"])
        expr = "ctx"
        for key, realm in isolate.items():
            expr += f".isolate_with({_string(key)}, _revl_realm({_string(realm)}))"
        out.append(f'        "{snake}" => {expr},')
    out.append("        _ => ctx.clone(),")
    out.append("    }")
    out.append("}")
    out.append("")

    # component name -> loaded Fiber, building the typed config from the
    # placement spec's `config` object (keyed by component name).
    out.append("pub fn _revl_load(ctx: &cordis::Context, name: &str, config: &serde_json::Value) "
               "-> Option<cordis::Fiber> {")
    out.append("    match name {")
    for component in components:
        snake = _snake(component["name"])
        pascal = component["name"]
        fields = component.get("config") or []
        out.append(f'        "{snake}" => {{')
        # Isolate the registration context so the fiber carries the realm
        # scope its reactive gate and provisions resolve against.
        out.append(f'            let ctx = _revl_isolate_ctx(ctx, "{snake}");')
        if fields:
            out.append(f'            let _c = config.get("{pascal}").cloned().unwrap_or(serde_json::Value::Null);')
            out.append(f"            Some(ctx.plugin({snake}(), {pascal}Config {{")
            for field in fields:
                fname = _ident(field.get("name"), "config field")
                ftype = _rust_type(field.get("type"))
                default = _config_default_lit(field.get("default"), ftype)
                if ftype == "String":
                    out.append(f'                {fname}: _c.get("{fname}").and_then(|v| v.as_str())'
                               f".map(|s| s.to_string()).unwrap_or_else(|| " + default + "),")
                elif ftype == "i64":
                    out.append(f'                {fname}: _c.get("{fname}").and_then(|v| v.as_i64())'
                               f".unwrap_or({default}),")
                elif ftype == "f64":
                    out.append(f'                {fname}: _c.get("{fname}").and_then(|v| v.as_f64())'
                               f".unwrap_or({default}),")
                elif ftype == "bool":
                    out.append(f'                {fname}: _c.get("{fname}").and_then(|v| v.as_bool())'
                               f".unwrap_or({default}),")
                else:
                    out.append(f"                {fname}: {default},")
            out.append("            }))")
        else:
            out.append(f"            Some(ctx.plugin({snake}(), ()))")
        out.append("        },")
    out.append("        _ => None,")
    out.append("    }")
    out.append("}")
    out.append("")
    return out


def _emit_components(ir: dict, components: list) -> list[str]:
    out: list[str] = []
    out.extend(_emit_service_traits(ir.get("services") or {}, ir.get("types") or {}))
    out.extend(_emit_host_stubs(ir))
    if _uses_stdlib(ir):
        out.extend(_stdlib_helper_traits())
    if _uses_float_interp(ir):
        out.extend(_revl_ftoa_helper())
    if _needs_realm_helper(components):
        out.extend(_revl_realm_helper(components))
    if _uses_spawn(components):
        out.extend(_revl_spawn_handle())
    for component in components:
        out.extend(_emit_component_auto(component, ir.get("services") or {}, ir))
    out.extend(_emit_bridge(ir))
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
        pure = [t for t in tests if not t.get("lifecycle")]
        lifecycle = [t for t in tests if t.get("lifecycle")]
        if pure:
            out.extend(_emit_v3_tests(pure, types, functions, externs))
        if lifecycle:
            out.extend(_emit_v3_lifecycle_tests(
                lifecycle, types, functions, externs,
                ir.get("services") or {}, components))
    out.extend(_emit_components(ir, components))
    return "\n".join(out).rstrip() + "\n"


# ------------------------------------------------------------ typed holes

def _refuse_holes(ir: dict) -> None:
    """A typed hole is an unmet obligation, not code (docs/holes.md).

    Emitting one would put a placeholder into Rust and make rustc the
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
        f"refusing to emit Rust: this document still has {len(found)} typed "
        f"hole(s) — {where}. A hole type-checks so the surrounding draft can "
        f"be checked, but it has no implementation and there is nothing to "
        f"lower. Fill every hole, then emit (docs/holes.md)."
    )

# A `fault test` is executed by driving a real activation and inspecting the
# runtime's residue afterwards (docs/fault-tests.md).  The cordis-rs tier
# has no such driver, so it is refused loudly instead of being dropped on the
# floor: a silently-missing fault test is a guarantee nobody is checking.
def _refuse_fault_tests(ir) -> None:
    fault_tests = (ir or {}).get("fault_tests") or []
    if not fault_tests:
        return
    names = ", ".join(repr(unit.get("name")) for unit in fault_tests)
    raise EmitError(
        f"fault tests do not lower to the cordis-rs tier ({names}) — `fault test` runs "
        f"on the python reference tier only (docs/fault-tests.md). Compile "
        f"this document with --backend py, or move the fault tests to a "
        f"module that is not emitted for this tier."
    )


def emit(ir: dict) -> str:
    """Emit one Rust module (crate root) for an IR document."""
    if not isinstance(ir, dict):
        raise EmitError("IR document must be a dict")
    _refuse_holes(ir)

    _refuse_fault_tests(ir)

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
        'serde = { version = "1", features = ["derive"] }\n'
        'serde_json = "1"\n'
    )


def _main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python3 emit.py <ir.json|->", file=sys.stderr)
        return 2
    # `-` reads the IR from stdin. Callers used to pass `/dev/stdin`, which
    # works on macOS and fails on a GitHub runner with `OSError: [Errno 6] No
    # such device or address` — the emitted-code tests were red in CI for that
    # reason alone.
    if argv[1] == "-":
        ir = json.load(sys.stdin)
    else:
        with open(argv[1], "r", encoding="utf-8") as handle:
            ir = json.load(handle)
    sys.stdout.write(emit(ir))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))




