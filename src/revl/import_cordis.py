"""Cordis (TypeScript) plugin -> revl source.

`revl import cordis` is the fourth member of the import codegen family
(docs/v2.0-roadmap.md §14), after `revl mcp import`, `revl import wit` and
`revl import openapi`. It reads a Cordis plugin's `inject`/`provide` surface
and emits revl: a `service` for the service the plugin provides, an `extern`
per operation, and a provider component wiring the two together — so a revl
composition can consume a real Cordis / DeepSeek-Harness plugin as a coeffect.

The honest hard part, and why this importer is not the others
-------------------------------------------------------------

WIT and OpenAPI describe *specified* interfaces: a WIT `func` has a signature,
an OpenAPI operation has a schema. A Cordis plugin is **untyped TypeScript**.
Its service surface is whatever methods a `class ... extends Service` (or a
`ctx.provide(...)` object) happens to expose, and its parameter/return types
are whatever the author bothered to annotate. Recovering that surface — and
saying so loudly where it *cannot* be recovered — is the whole job.

So this importer draws a hard line, the same one the family draws everywhere:

    **A signature is transcribed only from positive evidence — a TypeScript
    type annotation, or a JSDoc `@param`/`@returns` type. A parameter or
    result whose type cannot be recovered is never guessed.** By default the
    importer *refuses* the operation (naming the method, the parameter and the
    line); with `--mark-unrecovered` it emits a loud `// UNRECOVERED` marker in
    place of the method instead, so a partial surface still compiles while
    reading as unmistakably incomplete.

The emission rule is the family's, verbatim
-------------------------------------------

A service declaration is a G4 *upper bound* on every provider of it, so an
under-declared operation silently breaks every consumer's G8 audit. Therefore
every imported operation is `emission` unless something explicitly and
positively asserts otherwise. Two things can:

  * `@revl:pure` in a method's JSDoc — the *plugin author's* claim, the direct
    analogue of WIT's `/// @revl:pure` and MCP's `readOnlyHint: true`;
  * `--pure <Service>.<method>` (or `--pure <method>`) at import time — the
    *importing engineer's* claim.

Both are human claims this compiler cannot verify, and each is recorded next
to the operation it weakened. Everything else is `emission`.

What is recovered vs. refused
-----------------------------

Recovered: the plugin `name`; its `inject` list (array or `{required,
optional}` form); the provided service key (`export const provide`, a plugin
object's `provide:` field, a `Service` subclass's `super(ctx, 'key')` or
`static provide`, or `ctx.provide('key')`); the public methods of a
`class ... extends Service`, or of a `ctx.<key> = { ... }` object literal,
with their TypeScript / JSDoc types.

Refused (rather than guessed): a plugin with no recoverable provided service;
a provided service with no recoverable method surface (a dynamically-built
object this importer cannot read); an operation with an unrecoverable
parameter or result type; a nominal/inline-object type with no revl spelling.
Teardown (`dispose`/`stop`/`ctx.effect`) is *detected and reported* but never
paired to an operation — recovering undo semantics from untyped TS is exactly
the thing this importer will not fake; a resource-owning operation must be
wrapped by hand as `extern acquire fn ... undo ...` (G4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import RevlError
from .lexer import KEYWORDS

# ---------------------------------------------------------------- type mapping

#: TypeScript primitive -> revl surface type. `number` widens to `Float`
#: (TS has one numeric type; revl splits `Int`/`Float`, and `Float` is the
#: safe superset) — the header states the widening rather than hiding it.
#: `bigint` is the one TS spelling that is unambiguously integral.
_PRIMITIVES = {
    "string": "Str",
    "number": "Float",
    "bigint": "Int",
    "boolean": "Bool",
    "true": "Bool",
    "false": "Bool",
    "Uint8Array": "Bytes",
    "Buffer": "Bytes",
    "ArrayBuffer": "Bytes",
}

#: TS spellings of "no value" — a method returning one of these has no revl
#: result type (`-> Unit` is implicit). Note the empty string is *not* here: a
#: missing parameter type is unrecoverable, while a missing return is handled
#: (as Unit-with-a-note) by `_return_type`, so the two never share this table.
_VOIDISH = {"void", "undefined", "never"}

#: types this importer deliberately will not translate, so that a guess never
#: reaches the output. `any`/`object`/`unknown` are the untyped escape hatches;
#: `Function`/`symbol` have no host-crossing revl value.
_UNRECOVERABLE_NAMES = {"any", "unknown", "object", "Function", "symbol"}

#: revl's built-in type names — a plugin type named `Result`/`List` would
#: shadow one, so it is renamed (and the rename is reported in the header).
_BUILTIN_TYPES = {"Str", "Int", "Float", "Bool", "Bytes", "Unit",
                  "List", "Map", "Opt", "Result"}


class Unrecoverable(Exception):
    """A type that has no honest revl spelling from the evidence at hand.

    Carried up to the method boundary, where it becomes either a hard refusal
    (`RevlError`) or a `// UNRECOVERED` marker, per `--mark-unrecovered`.
    """

    def __init__(self, ts_type: str, why: str) -> None:
        super().__init__(why)
        self.ts_type = ts_type
        self.why = why


# --------------------------------------------------------------- name mapping

def _pascal(name: str) -> str:
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", name or "") if p]
    out = "".join(p[0].upper() + p[1:] for p in parts)
    if not out:
        out = "X"
    if not re.match(r"^[A-Za-z]", out):
        out = f"P{out}"
    return out


def _snake(name: str) -> str:
    ident = re.sub(r"[^A-Za-z0-9]+", "_", name or "")
    ident = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", ident)
    ident = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", ident)
    ident = re.sub(r"_+", "_", ident).strip("_").lower()
    if not ident:
        ident = "x"
    if not re.match(r"^[A-Za-z_]", ident):
        ident = f"n_{ident}"
    return f"{ident}_" if ident in KEYWORDS else ident


# ------------------------------------------------------------ the TS scanner

@dataclass
class _Param:
    name: str
    ts_type: str
    optional: bool          # `x?: T` or a `= default`
    line: int


@dataclass
class _Method:
    name: str
    params: list[_Param]
    ret: str
    is_async: bool
    jsdoc: str
    line: int


@dataclass
class _Plugin:
    name: str | None = None
    inject_required: list[str] = field(default_factory=list)
    inject_optional: list[str] = field(default_factory=list)
    provide_key: str | None = None
    provide_line: int = 0
    methods: list[_Method] = field(default_factory=list)
    teardown: list[tuple[str, int]] = field(default_factory=list)  # (what, line)
    surface_origin: str = ""     # how the method surface was found (for the header)


def _line_at(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _decomment(source: str) -> tuple[str, dict[int, str]]:
    """Blank out comments (preserving offsets and line count) and record every
    JSDoc block against the offset of the code token that follows it.

    Offsets are preserved 1:1 — the returned string has the same length as the
    input — so a line number computed on either agrees.
    """
    out: list[str] = []
    jsdocs: dict[int, str] = {}
    pending: str | None = None
    i, n = 0, len(source)

    while i < n:
        c = source[i]
        if c in "\"'`":
            out.append(c)
            i += 1
            while i < n:
                if source[i] == "\\":
                    out.append(source[i])
                    if i + 1 < n:
                        out.append(source[i + 1])
                    i += 2
                    continue
                out.append(source[i])
                if source[i] == c:
                    i += 1
                    break
                i += 1
            continue
        if c == "/" and i + 1 < n and source[i + 1] == "/":
            end = source.find("\n", i)
            end = n if end < 0 else end
            out.append(" " * (end - i))
            i = end
            continue
        if c == "/" and i + 1 < n and source[i + 1] == "*":
            end = source.find("*/", i + 2)
            end = n if end < 0 else end + 2
            block = source[i:end]
            if block.startswith("/**"):
                pending = block
            # keep newlines so line numbers inside the file do not drift
            out.append("".join(ch if ch == "\n" else " " for ch in block))
            i = end
            continue
        if pending is not None and not c.isspace():
            jsdocs[i] = pending
            pending = None
        out.append(c)
        i += 1

    return "".join(out), jsdocs


def _match_bracket(source: str, open_at: int) -> int:
    """Index just past the bracket that closes the one at `open_at`.

    Understands `()`, `{}`, `[]`, `<>` nesting and skips string literals; the
    input is already de-commented.
    """
    pairs = {"(": ")", "{": "}", "[": "]", "<": ">"}
    open_ch = source[open_at]
    close = pairs[open_ch]
    depth, i, n = 0, open_at, len(source)
    while i < n:
        c = source[i]
        if c in "\"'`":
            i += 1
            while i < n and source[i] != c:
                i += 2 if source[i] == "\\" else 1
            i += 1
            continue
        if c == open_ch:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _split_top(text: str, sep: str = ",") -> list[str]:
    """Split on `sep` at bracket depth zero (params, union members, ...)."""
    parts, depth, start = [], 0, 0
    for i, c in enumerate(text):
        if c in "([{<":
            depth += 1
        elif c in ")]}>":
            depth -= 1
        elif c == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def _string_literal(text: str) -> str | None:
    m = re.match(r"""^\s*['"`]([^'"`]*)['"`]""", text)
    return m.group(1) if m else None


_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_METHOD_MODIFIERS = {"public", "private", "protected", "static", "readonly",
                     "abstract", "declare", "override", "async", "get", "set"}

#: Cordis / `Service` lifecycle hooks — teardown, not part of the callable
#: service surface. Detected as teardown evidence and left out of the ops.
_LIFECYCLE = {"start", "stop", "dispose", "fork"}


class _Scanner:
    """Recovers a `_Plugin` from a de-commented Cordis plugin source."""

    def __init__(self, source: str, filename: str) -> None:
        self.filename = filename
        self.raw = source
        self.code, self.jsdocs = _decomment(source)

    # -- plugin-level evidence --------------------------------------------
    def _string_const(self, key: str) -> tuple[str, int] | None:
        """`export const <key> = '...'` or a `<key>: '...'` object field."""
        for pat in (rf"(?:export\s+)?const\s+{key}\s*=\s*",
                    rf"\b{key}\s*:\s*"):
            m = re.search(pat, self.code)
            if m:
                value = _string_literal(self.code[m.end():])
                if value is not None:
                    return value, m.start()
        return None

    def _inject(self, plugin: _Plugin) -> None:
        m = re.search(r"(?:export\s+)?const\s+inject\s*=\s*|\binject\s*:\s*",
                      self.code)
        if not m:
            return
        tail = self.code[m.end():].lstrip()
        base = m.end() + (len(self.code[m.end():]) - len(tail))
        if tail.startswith("["):
            body = self.code[base:_match_bracket(self.code, base)]
            plugin.inject_required = [s for s in
                                      (_string_literal(p) for p in _split_top(body[1:-1]))
                                      if s]
        elif tail.startswith("{"):
            body = self.code[base:_match_bracket(self.code, base)]
            for field_name, target in (("required", plugin.inject_required),
                                       ("optional", plugin.inject_optional)):
                fm = re.search(rf"\b{field_name}\s*:\s*\[", body)
                if fm:
                    arr = body[fm.end() - 1:_match_bracket(body, fm.end() - 1)]
                    target.extend(s for s in
                                  (_string_literal(p) for p in _split_top(arr[1:-1]))
                                  if s)

    def _provide_key(self, plugin: _Plugin) -> None:
        # priority: explicit `provide` const/field, then a Service `super()`
        # or `static provide`, then a `ctx.provide('key')` call.
        for pat in (r"(?:export\s+)?const\s+provide\s*=\s*",
                    r"\bprovide\s*:\s*",
                    r"\bstatic\s+provide\s*=\s*"):
            m = re.search(pat, self.code)
            if m:
                tail = self.code[m.end():].lstrip()
                if tail.startswith("["):
                    base = m.end() + (len(self.code[m.end():]) - len(tail))
                    body = self.code[base:_match_bracket(self.code, base)]
                    first = next((s for s in (_string_literal(p)
                                              for p in _split_top(body[1:-1])) if s), None)
                    if first:
                        plugin.provide_key = first
                        plugin.provide_line = _line_at(self.raw, m.start())
                        return
                else:
                    value = _string_literal(tail)
                    if value:
                        plugin.provide_key = value
                        plugin.provide_line = _line_at(self.raw, m.start())
                        return
        for pat in (r"super\s*\(\s*[A-Za-z_$][\w$]*\s*,\s*['\"`]([^'\"`]+)",
                    r"\bctx\s*\.\s*provide\s*\(\s*['\"`]([^'\"`]+)"):
            m = re.search(pat, self.code)
            if m:
                plugin.provide_key = m.group(1)
                plugin.provide_line = _line_at(self.raw, m.start())
                return

    def _teardown(self, plugin: _Plugin) -> None:
        for pat, what in (
            (r"\bctx\s*\.\s*on\s*\(\s*['\"`]dispose['\"`]", "ctx.on('dispose')"),
            (r"\bctx\s*\.\s*effect\s*\(", "ctx.effect(...)"),
            (r"\[\s*Symbol\s*\.\s*dispose\s*\]", "[Symbol.dispose]"),
            (r"\bstop\s*\(\s*\)\s*[:{]", "stop() teardown"),
        ):
            for m in re.finditer(pat, self.code):
                plugin.teardown.append((what, _line_at(self.raw, m.start())))

    # -- method surface ---------------------------------------------------
    def _members(self, body_open: int) -> list[_Method]:
        """Extract shorthand-method members from a `{ ... }` body.

        Handles a class body and an object literal alike: both spell a method
        as `name(params): ret { ... }`. Arrow-function and `function` fields,
        getters/setters, private (`#x` / `private`) members and the
        constructor are skipped — the constructor is only read for its
        `super(ctx, 'key')` provide key elsewhere.
        """
        body_end = _match_bracket(self.code, body_open) - 1
        methods: list[_Method] = []
        i = body_open + 1
        mods: list[str] = []
        while i < body_end:
            c = self.code[i]
            if c.isspace():
                i += 1
                continue
            if c in ";,":
                mods = []
                i += 1
                continue
            if c == "{":                       # a stray block: skip it
                i = _match_bracket(self.code, i)
                mods = []
                continue
            if c == "#":                        # a private field/method
                i += 1
                continue
            m = _IDENT.match(self.code, i)
            if not m:
                i += 1
                continue
            word = m.group(0)
            j = m.end()
            while j < body_end and self.code[j].isspace():
                j += 1
            nxt = self.code[j] if j < body_end else ""
            # An identifier followed directly by `(` is a method name — a
            # modifier (`get`/`static`/`async`/…) is always followed by a
            # further token, so `get(` is a method literally called `get`.
            if nxt == "(":
                method = self._one_method(word, m.start(), j, mods)
                if method is not None:
                    methods.append(method)
                # skip the parameter list, an optional return type, and body
                after = _match_bracket(self.code, j)
                i = self._skip_return_and_body(after)
                mods = []
                continue
            if word in _METHOD_MODIFIERS:
                mods.append(word)
                i = j
                continue
            # a field: `name = ...` / `name: T` — skip to the next `;`
            mods = []
            semi = self.code.find(";", m.end())
            i = semi + 1 if semi != -1 and semi < body_end else j
        return methods

    def _skip_return_and_body(self, after_params: int) -> int:
        """From just past `)`, skip an optional `: ret` and the `{ ... }` body
        (or a `;` for an overload signature)."""
        i, n = after_params, len(self.code)
        while i < n and self.code[i] not in "{;":
            i += 1
        if i < n and self.code[i] == "{":
            return _match_bracket(self.code, i)
        return i + 1

    def _one_method(self, name: str, start: int, paren_at: int,
                    mods: list[str]) -> _Method | None:
        if name in ("constructor", *_LIFECYCLE) or "private" in mods \
                or "protected" in mods or "get" in mods or "set" in mods:
            return None
        params_src = self.code[paren_at:_match_bracket(self.code, paren_at)]
        after = _match_bracket(self.code, paren_at)
        ret = ""
        rest = self.code[after:].lstrip()
        if rest.startswith(":"):
            k = self.code.index(":", after) + 1
            end, depth = k, 0
            while end < len(self.code):
                ch = self.code[end]
                if depth == 0 and ch in "{;":       # method body / overload end
                    break
                if ch in "([<":
                    depth += 1
                elif ch in ")]>":
                    depth -= 1
                end += 1
            ret = self.code[k:end].strip()
        params = self._params(params_src[1:-1])
        jsdoc = self.jsdocs.get(start, "")
        is_async = "async" in mods or ret.startswith("Promise")
        return _Method(name=name, params=params, ret=ret, is_async=is_async,
                       jsdoc=jsdoc, line=_line_at(self.raw, start))

    def _params(self, src: str) -> list[_Param]:
        params: list[_Param] = []
        for chunk in _split_top(src):
            optional = False
            if "=" in chunk:
                chunk = chunk.split("=", 1)[0].strip()
                optional = True
            name_part, _, type_part = chunk.partition(":")
            name = name_part.strip()
            if name.endswith("?"):
                optional = True
                name = name[:-1].strip()
            name = name.lstrip(".")            # a rest param `...args`
            params.append(_Param(name=name, ts_type=type_part.strip(),
                                 optional=optional, line=0))
        return params

    # -- top-level driver -------------------------------------------------
    def scan(self) -> _Plugin:
        plugin = _Plugin()
        name = self._string_const("name")
        if name:
            plugin.name = name[0]
        self._inject(plugin)
        self._provide_key(plugin)
        self._teardown(plugin)

        # method surface: a `class ... extends Service`, else a
        # `ctx.<key> = { ... }` object literal.
        cls = re.search(r"class\s+([A-Za-z_$][\w$]*)\s+extends\s+"
                        r"(?:[A-Za-z_$][\w$]*\.)*Service\b", self.code)
        if cls:
            brace = self.code.index("{", cls.end())
            plugin.methods = self._members(brace)
            plugin.surface_origin = f"class {cls.group(1)} extends Service"
            static_provide = re.search(r"\bstatic\s+provide\s*=\s*['\"`]([^'\"`]+)",
                                       self.code[cls.start():])
            if static_provide:
                plugin.provide_key = static_provide.group(1)
            if plugin.provide_key is None:
                # a Service subclass whose key we could not read: name it after
                # the class, and say so.
                plugin.provide_key = _snake(cls.group(1))
                plugin.provide_line = _line_at(self.raw, cls.start())
        elif plugin.provide_key:
            obj = re.search(rf"\bctx\s*\.\s*{re.escape(plugin.provide_key)}\s*=\s*\{{",
                            self.code)
            if not obj:
                obj = re.search(r"\bctx\s*\.\s*provide\s*\(\s*['\"`]"
                                + re.escape(plugin.provide_key)
                                + r"['\"`]\s*,\s*\{", self.code)
            if obj:
                brace = self.code.index("{", obj.end() - 1)
                plugin.methods = self._members(brace)
                plugin.surface_origin = f"ctx.{plugin.provide_key} object literal"
        return plugin


# ----------------------------------------------------------------- type map

class _Names:
    """Remembers every type rename so the header can report it."""

    def __init__(self) -> None:
        self.renames: list[str] = []

    def type_name(self, ts: str) -> str:
        name = _pascal(ts)
        if name not in _BUILTIN_TYPES:
            return name
        note = f"type `{ts}` -> `{name}Ty` (`{name}` is a revl built-in)"
        if note not in self.renames:
            self.renames.append(note)
        return f"{name}Ty"


def _resolve_type(ts: str, names: _Names) -> str:
    """TypeScript type string -> revl surface type, or `Unrecoverable`.

    Every branch either produces a concrete revl type or refuses; nothing is
    guessed. An unknown *nominal* type is refused too — this importer reads one
    plugin file and has no definition to stand behind a bare `Foo`.
    """
    ts = ts.strip()
    # strip one layer of redundant parens
    while ts.startswith("(") and _match_bracket(ts, 0) == len(ts):
        ts = ts[1:-1].strip()

    if ts in _VOIDISH:
        return "Unit"
    if ts in _UNRECOVERABLE_NAMES:
        raise Unrecoverable(ts, f"`{ts}` is TypeScript's untyped escape hatch, "
                                "which has no honest revl spelling")

    # union: `T | null` / `T | undefined` -> Opt[T]; anything wider is refused.
    members = _split_top(ts, "|")
    if len(members) > 1:
        nullish = {"null", "undefined"}
        concrete = [m for m in members if m not in nullish]
        if len(concrete) != 1:
            raise Unrecoverable(ts, "a union of several concrete types is a sum "
                                    "type with no tag; revl needs a named "
                                    "`variant`, which this file does not define")
        inner = _resolve_type(concrete[0], names)
        return inner if inner.startswith("Opt[") else f"Opt[{inner}]"

    if ts.startswith("Promise<") and ts.endswith(">"):
        return _resolve_type(ts[len("Promise<"):-1], names)
    if ts.startswith("Array<") and ts.endswith(">"):
        return f"List[{_resolve_type(ts[len('Array<'):-1], names)}]"
    if ts.startswith("ReadonlyArray<") and ts.endswith(">"):
        return f"List[{_resolve_type(ts[len('ReadonlyArray<'):-1], names)}]"
    if ts.startswith("readonly "):
        ts = ts[len("readonly "):].strip()
    if ts.endswith("[]"):
        return f"List[{_resolve_type(ts[:-2], names)}]"
    if ts.startswith("Record<") and ts.endswith(">"):
        args = _split_top(ts[len("Record<"):-1])
        if len(args) == 2:
            return f"Map[{_resolve_type(args[0], names)}, {_resolve_type(args[1], names)}]"

    if ts in _PRIMITIVES:
        return _PRIMITIVES[ts]
    if _string_literal(ts) is not None:
        return "Str"                          # a string-literal type
    if re.fullmatch(r"-?\d+", ts):
        return "Int"                          # a numeric-literal type
    if ts.startswith("{") or ts.startswith("["):
        raise Unrecoverable(ts, "an inline object/tuple type; revl records are "
                                "nominal, so declare a named type and annotate "
                                "with it, or model it as a field of a record")
    if "<" in ts:
        raise Unrecoverable(ts, "a generic type this importer does not map "
                                "(known: `Array`, `Promise`, `Record`)")
    if re.fullmatch(r"[A-Za-z_$][\w$]*", ts):
        raise Unrecoverable(ts, f"the nominal type `{ts}` is not defined in this "
                                "file and has no built-in revl mapping; declare "
                                "its revl `type` by hand and reference it")
    raise Unrecoverable(ts, "no revl spelling for this type")


# ---------------------------------------------------------------- generation

_BACKEND_COMMENT = {"ts": "//", "py": "#", "rust": "//"}


def _jsdoc_pure(jsdoc: str) -> bool:
    return "@revl:pure" in jsdoc


def _jsdoc_type(jsdoc: str, param: str) -> str | None:
    """A JSDoc `@param {T} name` / `@returns {T}` type, if present."""
    if param == "@returns":
        m = re.search(r"@returns?\s*\{([^}]*)\}", jsdoc)
        return m.group(1).strip() if m else None
    m = re.search(r"@param\s*\{([^}]*)\}\s*" + re.escape(param) + r"\b", jsdoc)
    return m.group(1).strip() if m else None


def _jsdoc_summary(jsdoc: str) -> list[str]:
    """The human description lines of a JSDoc block (no `@tag` lines)."""
    out: list[str] = []
    for raw in jsdoc.splitlines():
        text = raw.strip().lstrip("/*").strip()
        text = text.rstrip("*/").strip()
        text = text.lstrip("*").strip()
        if not text or text.startswith("@"):
            continue
        out.append(text)
    return out


class _Generator:
    def __init__(self, plugin: _Plugin, filename: str, backend: str,
                 service: str | None, pure: set[str],
                 mark_unrecovered: bool) -> None:
        self.plugin = plugin
        self.filename = filename
        self.backend = backend
        self.pure_requested = set(pure)
        self.pure_used: set[str] = set()
        self.mark_unrecovered = mark_unrecovered
        self.names = _Names()
        self.notes: list[str] = []
        self.unrecovered: list[str] = []
        self.service = _pascal(service) if service else self._default_service_name()
        self.key = _snake(self.plugin.provide_key or self.service)

    def _default_service_name(self) -> str:
        base = self.plugin.provide_key or self.plugin.name or "Imported"
        name = _pascal(base)
        if name in _BUILTIN_TYPES:
            name = f"{name}Service"
        return name

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    # -- one operation ----------------------------------------------------
    def _param_type(self, method: _Method, param: _Param) -> str:
        ts = param.ts_type or _jsdoc_type(method.jsdoc, param.name) or ""
        if not ts:
            raise Unrecoverable("", f"parameter `{param.name}` has no TypeScript "
                                    "type and no JSDoc `@param` type")
        revl = _resolve_type(ts, self.names)
        if param.optional and not revl.startswith("Opt["):
            revl = f"Opt[{revl}]"
        return revl

    def _return_type(self, method: _Method) -> str | None:
        ts = method.ret or _jsdoc_type(method.jsdoc, "@returns") or ""
        if ts.startswith("Promise<") and ts.endswith(">"):
            ts = ts[len("Promise<"):-1].strip()
        if not ts:
            # A missing return annotation is ambiguous, but unlike a parameter
            # it does not have to be *called* — so it degrades to Unit with a
            # loud note rather than refusing the whole operation.
            self.note(f"`{self.service}.{_snake(method.name)}` has no annotated "
                      "return type; assumed to return nothing (Unit) — annotate "
                      "it to recover a result")
            return None
        if ts in _VOIDISH:
            return None
        revl = _resolve_type(ts, self.names)
        return None if revl == "Unit" else revl

    def _is_pure(self, method: _Method) -> str | None:
        if _jsdoc_pure(method.jsdoc):
            return f"`@revl:pure` in {self.filename}"
        for candidate in (f"{self.service}.{method.name}", method.name):
            if candidate in self.pure_requested:
                self.pure_used.add(candidate)
                return f"`--pure {candidate}` at import time"
        return None

    def _operation(self, method: _Method) -> tuple[list[str], str | None, str | None]:
        """(service-op lines, extern decl, provide-method line). The latter two
        are None when the method is emitted as a `// UNRECOVERED` marker."""
        op = _snake(method.name)
        try:
            sig_parts = [(_snake(p.name), self._param_type(method, p))
                         for p in method.params]
            returns = self._return_type(method)
        except Unrecoverable as bad:
            return self._mark(method, bad)

        sig = ", ".join(f"{n}: {t}" for n, t in sig_parts)
        ret = f" -> {returns}" if returns else ""
        assertion = self._is_pure(method)
        modifier = "" if assertion else "emission "
        # A Promise-returning method is not a revl `async fn`: that keyword is
        # about `await` boundaries in the *provider*, and this provider only
        # forwards to an extern. The asynchrony lives inside the host body,
        # which resolves the Promise before returning the value.
        if method.is_async:
            self.note(f"`{self.service}.{_snake(method.name)}` returns a Promise "
                      "in the plugin; the extern's host body awaits it and "
                      "returns the resolved value (not a revl `async fn`)")

        lines: list[str] = []
        for doc_line in _jsdoc_summary(method.jsdoc):
            lines.append(f"  // {doc_line[:88]}")
        if assertion:
            lines.append(f"  // plain by assertion: {assertion} — the plugin's "
                         "TypeScript makes no such claim")
        else:
            lines.append("  // untyped TS carries no reversibility claim: "
                         "`emission` by default (G8)")
        lines.append(f"  {modifier}fn {op}({sig}){ret}")

        extern = f"cordis_{self.key}_{op}"
        cls = "pure" if assertion else "emission"
        marker = _BACKEND_COMMENT[self.backend]
        target = f"{self.plugin.provide_key or self.key}.{method.name}"
        extern_decl = (
            f"extern {cls} fn {extern}({sig}){ret}\n"
            f"  = @{self.backend} {{ {marker} call the Cordis service method "
            f"`{target}` here }}")
        args = ", ".join(n for n, _ in sig_parts)
        provide = f"    fn {op}({args}) = {extern}({args})"
        return lines, extern_decl, provide

    def _mark(self, method: _Method,
              bad: Unrecoverable) -> tuple[list[str], None, None]:
        where = f"{self.filename}:{method.line}"
        detail = (f"{self.service}.{method.name}: cannot recover a signature — "
                  f"{bad.why} (TS type `{bad.ts_type or 'none'}`, {where})")
        if not self.mark_unrecovered:
            raise RevlError(
                self.filename, method.line,
                f"unrecoverable signature on `{method.name}`: {bad.why}",
                hint=f"the TypeScript type was `{bad.ts_type or 'missing'}`. "
                     "Annotate the method (a TS type or a JSDoc `@param {T}` / "
                     "`@returns {T}`), or re-run with `--mark-unrecovered` to "
                     "emit a loud `// UNRECOVERED` marker in its place instead "
                     "of dropping or guessing it")
        self.unrecovered.append(detail)
        lines = [
            f"  // UNRECOVERED: {self.service}.{method.name} — {bad.why}",
            f"  //   TS type `{bad.ts_type or 'none'}` at {where}; annotate the "
            "plugin and re-import.",
            f"  //   fn {_snake(method.name)}(...)  // signature not emitted: it "
            "would have to be guessed",
        ]
        return lines, None, None

    # -- the file ---------------------------------------------------------
    def emit(self) -> str:
        if self.plugin.provide_key is None:
            raise RevlError(
                self.filename, 0,
                "this Cordis plugin provides no service this importer can find",
                hint="`revl import cordis` wraps a plugin's provided service. "
                     "Expose one with `export const provide = 'key'`, a plugin "
                     "object's `provide:` field, a `class ... extends Service` "
                     "(with `super(ctx, 'key')`), or `ctx.provide('key')` — or "
                     "name it with `--service NAME` if it is built dynamically")
        if not self.plugin.methods:
            raise RevlError(
                self.filename, self.plugin.provide_line,
                f"the service `{self.plugin.provide_key}` exposes no method "
                "surface this importer can read",
                hint="a service surface is recovered from a `class ... extends "
                     "Service`'s methods, or a `ctx.<key> = { ... }` object "
                     "literal. A dynamically-assembled service cannot be read "
                     "statically; declare its `service` by hand")

        op_lines: list[str] = []
        externs: list[str] = []
        provides: list[str] = []
        emitted = 0
        for method in self.plugin.methods:
            lines, extern, provide = self._operation(method)
            op_lines.extend(lines)
            if extern is not None:
                externs.append(extern)
                provides.append(provide)
                emitted += 1

        if emitted == 0:
            raise RevlError(
                self.filename, self.plugin.provide_line,
                f"every operation on `{self.plugin.provide_key}` had an "
                "unrecoverable signature, so no callable surface remains",
                hint="annotate the plugin's methods (TS types or JSDoc) and "
                     "re-import; a service of only `// UNRECOVERED` markers is "
                     "not worth generating")

        unused = sorted(self.pure_requested - self.pure_used)
        if unused:
            raise RevlError(
                self.filename, 0,
                f"--pure named {', '.join(repr(n) for n in unused)}, which is "
                "not a method on this service",
                hint="spell it `<Service>.<method>` or `<method>`, in the "
                     "plugin's own camelCase; a typo would silently leave the "
                     "operation `emission`")

        parts = [self._header()]
        parts.append(f"service {self.service} {{\n" + "\n".join(op_lines) + "\n}")
        parts.extend(externs)
        provider = (
            f"component {self.service}Provider provides {self.key}: {self.service} {{\n"
            f"  provide {self.key} {{\n" + "\n".join(provides) + "\n  }\n}")
        parts.append(provider)
        return "\n\n".join(parts) + "\n"

    def _header(self) -> str:
        lines = [
            "// Generated by `revl import cordis` — do not edit by hand.",
            f"// Source plugin: {self.filename}",
        ]
        if self.plugin.name:
            lines.append(f"// Cordis plugin: {self.plugin.name}")
        if self.plugin.surface_origin:
            lines.append(f"// Service surface: {self.plugin.surface_origin}")
        lines += [
            "//",
            "// This boundary is TRUSTED, NOT CHECKED (G8), and it is trusted harder",
            "// than the rest of the family: a Cordis plugin is untyped TypeScript, so",
            "// its very signatures are recovered, not read off a spec. A service",
            "// declaration is a G4 upper bound on every provider of it, so an",
            "// under-declared operation silently breaks every consumer's audit.",
            "// Therefore every operation below is `emission` unless the plugin's JSDoc",
            "// (`@revl:pure`) or `--pure` at import time explicitly asserted otherwise,",
            "// and each such assertion is a human claim this compiler cannot verify.",
            "//",
            "// A signature is transcribed only from a TypeScript annotation or a JSDoc",
            "// type. Nothing is guessed: an unrecoverable type is either refused or",
            "// left as a loud `// UNRECOVERED` marker (never a plausible-looking fake).",
            "//",
            "// The extern bodies are stubs: the typed boundary is generated, the host",
            "// call into the Cordis service is not. Fill each one in, then",
            "// `revl audit` shows the surface.",
            "//",
            "// TypeScript `number` widens to revl `Float` (revl splits `Int`/`Float`;",
            "// `Float` is the safe superset). Use `bigint` in the plugin for `Int`.",
        ]
        if self.plugin.inject_required or self.plugin.inject_optional:
            lines.append("//")
            lines.append("// The plugin's own coeffect dependencies (its `inject`), which the")
            lines.append("// revl composition consuming this service must satisfy on the Cordis")
            lines.append("// side — surfaced, not wired (their interfaces are not in this file):")
            if self.plugin.inject_required:
                lines.append(f"//   required: {', '.join(self.plugin.inject_required)}")
            if self.plugin.inject_optional:
                lines.append(f"//   optional: {', '.join(self.plugin.inject_optional)}")
        if self.plugin.teardown:
            lines.append("//")
            lines.append("// Teardown detected but NOT paired to any operation — recovering undo")
            lines.append("// semantics from untyped TS is exactly what this importer will not")
            lines.append("// fake. If an operation acquires a resource, wrap it by hand as")
            lines.append("// `extern acquire fn ... undo ...` so its release is a tracked inverse (G4):")
            for what, line in self.plugin.teardown:
                lines.append(f"//   {what} at {self.filename}:{line}")
        for detail in self.unrecovered:
            lines.append(f"// UNRECOVERED: {detail}")
        for note in self.notes + self.names.renames:
            lines.append(f"// note: {note}")
        return "\n".join(lines)


# ----------------------------------------------------------------- public API

def import_cordis(source: str, *, filename: str = "<cordis>", backend: str = "ts",
                  service: str | None = None, pure: object = (),
                  mark_unrecovered: bool = False) -> str:
    """Parse a Cordis plugin `source` and return revl source.

    Raises `RevlError` — naming the construct, its line and the way forward —
    for anything this importer refuses to translate, including (by default) an
    operation whose signature cannot be recovered.
    """
    if backend not in _BACKEND_COMMENT:
        raise RevlError(filename, 0, f"unknown host backend {backend!r}",
                        hint=f"known: {', '.join(sorted(_BACKEND_COMMENT))}. "
                             "There is deliberately no `wasm`: the cordis-wasm "
                             "tier is i32-only, so it cannot host a TS plugin")
    plugin = _Scanner(source, filename).scan()
    return _Generator(plugin, filename, backend, service, set(pure or ()),
                      mark_unrecovered).emit()


def import_cordis_file(path: str, *, backend: str = "ts", service: str | None = None,
                       pure: object = (), mark_unrecovered: bool = False) -> str:
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    return import_cordis(source, filename=path, backend=backend, service=service,
                         pure=pure, mark_unrecovered=mark_unrecovered)
