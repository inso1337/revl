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

import os
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

#: revl's built-in value constructors — a synthesized variant case landing on
#: one of these would shadow it, so the collision is reported (mirrors the WIT
#: importer's `_BUILTIN_CASES`).
_BUILTIN_CASES = {"Some", "None", "Ok", "Err"}


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
        deco_at: int | None = None    # offset of the first decorator on this member
        while i < body_end:
            c = self.code[i]
            if c.isspace():
                i += 1
                continue
            if c in ";,":
                mods = []
                deco_at = None
                i += 1
                continue
            if c == "@":
                # A decorator (`@name`, `@ns.name`, `@name(args)`) sits between a
                # method's JSDoc and its `def`; skip it so the underlying method
                # is still recognised, but remember the first one's offset so the
                # JSDoc (attached to the decorator's `@`) still reaches the method.
                if deco_at is None:
                    deco_at = i
                dm = re.match(r"@\s*[A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*",
                              self.code[i:])
                j = i + (dm.end() if dm else 1)
                while j < body_end and self.code[j].isspace():
                    j += 1
                if j < body_end and self.code[j] == "(":
                    j = _match_bracket(self.code, j)
                i = j
                continue
            if c == "{":                       # a stray block: skip it
                i = _match_bracket(self.code, i)
                mods = []
                deco_at = None
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
                jsdoc_at = deco_at if deco_at is not None else m.start()
                method = self._one_method(word, m.start(), j, mods, jsdoc_at)
                if method is not None:
                    methods.append(method)
                # skip the parameter list, an optional return type, and body
                after = _match_bracket(self.code, j)
                i = self._skip_return_and_body(after)
                mods = []
                deco_at = None
                continue
            if word in _METHOD_MODIFIERS:
                mods.append(word)
                i = j
                continue
            # a field: `name = ...` / `name: T` — skip to the next `;`
            mods = []
            deco_at = None
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
                    mods: list[str], jsdoc_at: int | None = None) -> _Method | None:
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
        jsdoc = self.jsdocs.get(start if jsdoc_at is None else jsdoc_at, "")
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

    # -- class chain ------------------------------------------------------
    def _all_classes(self) -> list[dict]:
        """Every `class Name [extends Base]` in the file, with the offset of its
        body `{` and the *bare* base identifier (generics and any `Ns.` prefix
        stripped)."""
        classes: list[dict] = []
        for m in re.finditer(r"\bclass\s+([A-Za-z_$][\w$]*)", self.code):
            try:
                brace = self.code.index("{", m.end())
            except ValueError:
                continue
            header = self.code[m.end():brace]
            # drop generic groups so an `extends` inside `<T extends U>` (a type
            # constraint) is never mistaken for the class's own base clause.
            while True:
                stripped = re.sub(r"<[^<>]*>", "", header)
                if stripped == header:
                    break
                header = stripped
            bm = re.search(r"\bextends\s+([A-Za-z_$][\w$.]*)", header)
            base = bm.group(1).split(".")[-1] if bm else None
            classes.append({"name": m.group(1), "base": base,
                            "start": m.start(), "brace": brace})
        return classes

    def _service_roots(self) -> set[str]:
        """The local names that mean cordis's `Service` base — `Service` itself,
        plus any alias it was imported under (`import { Service as Svc }`)."""
        roots = {"Service"}
        for im in re.finditer(r"import\s*\{([^}]*)\}\s*from\s*['\"][^'\"]*['\"]",
                              self.code):
            for spec in im.group(1).split(","):
                bits = re.split(r"\bas\b", spec)
                if bits[0].strip() == "Service":
                    roots.add(bits[-1].strip())
        return roots

    def _service_class(self) -> tuple[dict | None, list[dict]]:
        """The class to treat as the provided service, found through the *real*
        base chain — so a `Service` subclass reached via a non-`Service`-named
        base (`class Foo extends BaseThing` where `BaseThing extends Service`, or
        an aliased base) is recovered, not just a literal `extends Service`.

        Returns `(target, chain)` where `chain` is the local base classes from
        the target up to (not including) `Service`, nearest first — so inherited
        methods can be merged. `(None, [])` if no Service subclass is present.
        """
        classes = self._all_classes()
        by_name = {c["name"]: c for c in classes}
        roots = self._service_roots()

        def chain_of(cls: dict, seen: frozenset[str]) -> list[dict] | None:
            base = cls["base"]
            if base is None or cls["name"] in seen:
                return None
            if base in roots:
                return []
            parent = by_name.get(base)
            if parent is None:
                # `base` is not a class in this file — it comes from another
                # package. Cordis's convention names every service base `*Service`
                # (`RemoteService`, `TypertRemoteService`, …), so a `*Service`
                # external base is a service root: the subclass's own decorated
                # methods are recovered. A non-`Service`-named package base has
                # nothing to stand behind it, so the chain terminates honestly
                # (`None`) and the class is not treated as a service.
                return [] if base.endswith("Service") else None
            rest = chain_of(parent, seen | {cls["name"]})
            return None if rest is None else [parent, *rest]

        services = [(c, chain_of(c, frozenset())) for c in classes]
        services = [(c, ch) for c, ch in services if ch is not None]
        if not services:
            return None, []
        # Prefer the most-derived class: one no other Service subclass extends.
        bases = {c["base"] for c, _ in services}
        leaves = [(c, ch) for c, ch in services if c["name"] not in bases]
        target, chain = (leaves or services)[0]
        return target, chain

    # -- top-level driver -------------------------------------------------
    def scan(self) -> _Plugin:
        plugin = _Plugin()
        name = self._string_const("name")
        if name:
            plugin.name = name[0]
        self._inject(plugin)
        self._provide_key(plugin)
        self._teardown(plugin)

        # method surface: a `class ... extends Service` (found through the real
        # base chain, so a non-`Service`-named base is still recovered), else a
        # `ctx.<key> = { ... }` object literal.
        cls, chain = self._service_class()
        if cls:
            # the target's own methods, plus any public methods inherited from a
            # local Service base class up the chain (the derived class wins on a
            # name collision — an override, not a duplicate operation).
            merged: dict[str, _Method] = {}
            for ancestor in reversed(chain):        # base-most first
                for meth in self._members(ancestor["brace"]):
                    merged[meth.name] = meth
            for meth in self._members(cls["brace"]):
                merged[meth.name] = meth
            plugin.methods = list(merged.values())
            if cls["base"] in self._service_roots():
                plugin.surface_origin = f"class {cls['name']} extends {cls['base']}"
            elif chain:
                plugin.surface_origin = (
                    f"class {cls['name']} extends {cls['base']} "
                    f"(a Service subclass via {' -> '.join(a['name'] for a in chain)})")
            else:
                # an external base with no local chain: recognised as a service
                # root by the cordis `*Service` naming convention.
                plugin.surface_origin = (
                    f"class {cls['name']} extends {cls['base']} "
                    f"(an external `*Service` base, treated as a Service root)")
            static_provide = re.search(r"\bstatic\s+provide\s*=\s*['\"`]([^'\"`]+)",
                                       self.code[cls["start"]:])
            if static_provide:
                plugin.provide_key = static_provide.group(1)
            if plugin.provide_key is None:
                # a Service subclass whose key we could not read: name it after
                # the class, and say so.
                plugin.provide_key = _snake(cls["name"])
                plugin.provide_line = _line_at(self.raw, cls["start"])
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


_FIELD_MODIFIERS = {"readonly", "public", "private", "protected", "static",
                    "declare", "abstract", "override"}


class _Records:
    """Follows a *named* type — an `interface`, a `type X = { … }` alias, or a
    plain data `class` — to its definition, in the plugin file or a **local**
    import, and transcribes it as a revl record `type`.

    This is the one place the importer reads beyond the single plugin file: a
    nominal parameter/return type is otherwise refused (nothing to stand behind
    the bare name), but a record reachable by following the plugin's own local
    imports *does* have a definition, so it is transcribed rather than refused.
    A non-relative import (a package) is never followed; a name with no findable
    record definition still refuses, exactly as before.
    """

    def __init__(self, source: str, filename: str) -> None:
        self.filename = filename
        self._modules: dict[str, str] = {filename: _decomment(source)[0]}
        self._module_imports: dict[str, dict[str, str]] = {}
        self._ctx: list[str] = []                # module scope stack for lookups
        self._pending: set[str] = set()          # revl names being built (cycles)
        self.decls: dict[str, str] = {}          # revl type name -> declaration
        self.origin: dict[str, str] = {}         # revl type name -> where found
        self.kind: dict[str, str] = {}           # revl type name -> "record"/"variant"

    # -- module loading / import resolution -------------------------------
    def _load(self, path: str) -> str:
        if path not in self._modules:
            try:
                with open(path, encoding="utf-8") as handle:
                    self._modules[path] = _decomment(handle.read())[0]
            except OSError:
                self._modules[path] = ""
        return self._modules[path]

    def _resolve_module(self, from_module: str, spec: str) -> str | None:
        """A relative import `spec` from `from_module` -> an existing file, or
        None (a package import, or an unresolvable path — never followed)."""
        if not spec.startswith(".") or not os.path.isfile(from_module):
            return None
        root = os.path.normpath(os.path.join(os.path.dirname(from_module), spec))
        # A relative import may carry an explicit extension: `./types.ts` (the
        # real source, the NodeNext/DSH spelling), or `./types.js` / `.mjs`
        # (ESM/NodeNext, whose `.js` resolves back to the `.ts` source). Try the
        # path as spelt first, then strip a recognised extension so the usual
        # candidate search finds the module file. `.d.ts` is checked before `.ts`
        # so a declaration file is stripped whole, not down to `types.d`.
        literal = root
        for ext in (".d.ts", ".d.mts", ".d.cts", ".ts", ".tsx", ".mts", ".cts",
                    ".js", ".jsx", ".mjs", ".cjs"):
            if root.endswith(ext):
                root = root[:-len(ext)]
                break
        for cand in (literal, root + ".ts", root + ".tsx", root + ".d.ts",
                     root + ".mts", os.path.join(root, "index.ts")):
            if os.path.isfile(cand):
                return cand
        return None

    def _imports_of(self, module: str) -> dict[str, str]:
        if module not in self._module_imports:
            text = self._modules.get(module, "")
            found: dict[str, str] = {}
            for im in re.finditer(
                    r"import\s*(?:type\s*)?\{([^}]*)\}\s*from\s*['\"]([^'\"]+)['\"]",
                    text):
                target = self._resolve_module(module, im.group(2))
                if target is None:
                    continue
                for spec in im.group(1).split(","):
                    local = re.split(r"\bas\b", spec)[-1].strip()
                    if local:
                        found[local] = target
            self._module_imports[module] = found
        return self._module_imports[module]

    # -- field parsing ----------------------------------------------------
    def _scan_to_sep(self, s: str, i: int) -> int:
        """Index of the next `;`, `,` or newline at bracket depth zero (a member
        boundary); skips balanced `()[]{}<>` and so steps over a method body."""
        depth, n = 0, len(s)
        while i < n:
            c = s[i]
            if c in "([{<":
                depth += 1
            elif c in ")]}>":
                depth -= 1
            elif depth == 0 and (c in ";," or c == "\n"):
                return i
            i += 1
        return n

    def _scan_type_expr(self, s: str, i: int) -> int:
        """Index just past a (possibly multiline) *type-alias body* starting at
        `s[i]`.

        Unlike `_scan_to_sep`, which ends a member at the first newline, this
        reads a whole type expression across lines. DSH formats its unions one
        member per line — with or without a leading `|`:

            type PluginFiberPhase =
              | 'pending'
              | 'loading'
              | null

        so the body must not be cut at the newline after the first member (which
        left `| 'pending'` — a refusal — or, for the leading-`|`-less shape,
        silently collapsed the alias to just its first member). At bracket depth
        zero a newline ends the expression *only* when nothing continues it: the
        line did not end on a binary/opening operator (`|`, `&`, `=`, `<`, `,`,
        `.`, `:`, `?`, `(`, `[`, `{`), and the next non-blank line does not begin
        with `|` or `&`. A `;` at depth zero always ends it. Balanced
        `()[]{}<>` and string literals are stepped over, so an inline object or
        a multiline branded intersection is spanned, not cut."""
        depth, n = 0, len(s)
        last_op = True                 # a leading `|`/`&` is permitted
        while i < n:
            c = s[i]
            if c in "\"'`":            # step over a string-literal type whole
                i += 1
                while i < n and s[i] != c:
                    i += 2 if s[i] == "\\" else 1
                i += 1
                last_op = False
                continue
            if c in "([{<":
                depth += 1
                i += 1
                last_op = True
                continue
            if c in ")]}>":
                if depth > 0:          # ignore a stray `>` (e.g. from `=>`)
                    depth -= 1
                i += 1
                last_op = False
                continue
            if depth == 0 and c == ";":
                return i
            if depth == 0 and c == "\n":
                if last_op:            # this line ended mid-expression
                    i += 1
                    continue
                j = i + 1
                while j < n and s[j] in " \t\r\n":
                    j += 1
                if j < n and s[j] in "|&":   # the next line continues the union
                    i = j
                    continue
                return i
            if not c.isspace():
                last_op = c in "|&=<,.:?([{"
            i += 1
        return n

    def _member_fields(self, body: str) -> list[tuple[str, str, bool]]:
        """`(name, ts_type, optional)` for each `name: T` field of a `{ … }`
        body — methods, index signatures and private/protected members skipped."""
        inner = body[1:-1]
        out: list[tuple[str, str, bool]] = []
        i, n = 0, len(inner)
        mods: list[str] = []
        while i < n:
            c = inner[i]
            if c.isspace():
                i += 1
                continue
            if c in ";,":
                mods = []
                i += 1
                continue
            m = _IDENT.match(inner, i)
            if not m:
                i = self._scan_to_sep(inner, i) + 1
                mods = []
                continue
            word, j = m.group(0), m.end()
            k = j
            while k < n and inner[k].isspace():
                k += 1
            optional = False
            if k < n and inner[k] == "?":
                optional = True
                k += 1
                while k < n and inner[k].isspace():
                    k += 1
            nxt = inner[k] if k < n else ""
            if word in _FIELD_MODIFIERS and not optional and nxt not in ":(?<=":
                mods.append(word)
                i = j
                continue
            if nxt == ":":
                end = self._scan_to_sep(inner, k + 1)
                ts = inner[k + 1:end].strip()
                if ts and "private" not in mods and "protected" not in mods:
                    out.append((word, ts, optional))
                i, mods = end + 1, []
                continue
            # a method, an initializer-only field, or something else: skip it.
            i, mods = self._scan_to_sep(inner, k) + 1, []
        return out

    def _find_def(self, name: str, ctx: str,
                  seen: frozenset[tuple[str, str]] = frozenset()) -> dict | None:
        """Locate `name`'s definition, following local imports. Returns
        `{"module", "fields"}` for a record, `{"module", "alias"}` for a bare
        type alias, or None. `seen` is the set of `(name, module)` already
        visited, so an interface- or import-cycle terminates."""
        if (name, ctx) in seen:
            return None
        seen = seen | {(name, ctx)}
        text = self._modules.get(ctx, "")
        esc = re.escape(name)

        im = re.search(r"(?:export\s+)?(?:declare\s+)?interface\s+" + esc + r"\b",
                       text)
        if im and "{" in text[im.end():]:
            brace = text.index("{", im.end())
            header = text[im.end():brace]
            fields = self._member_fields(text[brace:_match_bracket(text, brace)])
            hm = re.search(r"\bextends\s+([^{]+)", header)
            if hm:                              # flatten inherited interface fields
                inherited: list[tuple[str, str, bool]] = []
                for raw in _split_top(hm.group(1)):
                    base = re.sub(r"<[^>]*>", "", raw).split(".")[-1].strip()
                    parent = base and self._find_def(base, ctx, seen)
                    if parent and "fields" in parent:
                        inherited += parent["fields"]
                fields = inherited + fields
            return {"module": ctx, "fields": fields}

        # `type Name = …`, or a *generic* alias `type Name<T> = …` (e.g. DSH's
        # `type Branded<T> = string & { readonly __brand: T }`). The generic
        # parameters are matched (and dropped): the alias body is resolved as
        # written, and any use of a parameter that survives resolution refuses
        # honestly, exactly like an unmapped nominal.
        tm = re.search(r"(?:export\s+)?type\s+" + esc + r"\s*(?:<[^=]*>\s*)?=\s*",
                       text)
        if tm:
            tail = text[tm.end():].lstrip()
            if tail.startswith("{"):
                base = tm.end() + (len(text[tm.end():]) - len(tail))
                body = text[base:_match_bracket(text, base)]
                return {"module": ctx, "fields": self._member_fields(body)}
            end = self._scan_type_expr(text, tm.end())
            return {"module": ctx, "alias": text[tm.end():end].strip()}

        cm = re.search(r"(?:export\s+)?(?:abstract\s+)?class\s+" + esc + r"\b", text)
        if cm and "{" in text[cm.end():]:
            brace = text.index("{", cm.end())
            if "extends" not in text[cm.end():brace]:   # not a Service subclass
                body = text[brace:_match_bracket(text, brace)]
                return {"module": ctx, "fields": self._member_fields(body)}

        target = self._imports_of(ctx).get(name)
        if target is not None:
            self._load(target)
            return self._find_def(name, target, seen)
        return None

    # -- public: resolve one nominal type ---------------------------------
    def resolve(self, ts_name: str, names: _Names) -> str | None:
        """`ts_name` -> a revl type name (registering its `type` declaration and
        any it depends on), or None if no record definition can be found. An
        *unrecoverable field* raises `Unrecoverable`, so a record with a field
        the importer cannot map refuses the operation rather than half-emitting."""
        ctx = self._ctx[-1] if self._ctx else self.filename
        found = self._find_def(ts_name, ctx)
        if found is None:
            return None
        if "alias" in found:
            self._ctx.append(found["module"])
            try:
                # A *literal-only* union alias (`type PluginFiberPhase =
                # 'pending' | 'loading' | … | null`) is the one union revl can
                # name: the string literals ARE the tags, so it synthesizes a
                # variant named after the alias. Everything else — a single
                # aliased type, or a union that mixes in a non-literal — resolves
                # (or refuses) through the ordinary path.
                variant = self._maybe_literal_union(ts_name, found["alias"], names)
                if variant is not None:
                    return variant
                return _resolve_type(found["alias"], names, self)
            finally:
                self._ctx.pop()

        revl = names.type_name(ts_name)
        if revl in self.decls or revl in self._pending:
            return revl
        self._pending.add(revl)
        self._ctx.append(found["module"])
        try:
            fields: list[str] = []
            for fname, fts, optional in found["fields"]:
                ftype = _resolve_type(fts, names, self)
                if optional and not ftype.startswith("Opt["):
                    ftype = f"Opt[{ftype}]"
                fields.append(f"{_snake(fname)}: {ftype}")
            if not fields:
                return None
            self.decls[revl] = f"type {revl} = {{ {', '.join(fields)} }}"
            self.origin[revl] = f"`{ts_name}` in {found['module']}"
            self.kind[revl] = "record"
        finally:
            self._ctx.pop()
            self._pending.discard(revl)
        return revl

    def resolve_alias_body(self, name: str, names: _Names) -> str | None:
        """Follow a *generic* alias `name` (e.g. `Branded`) to its right-hand
        side and resolve that, so `Branded<'X'>` becomes the alias body's revl
        type. Returns None when `name` is not a reachable alias (a real record,
        or an unknown nominal — left for the caller to refuse)."""
        ctx = self._ctx[-1] if self._ctx else self.filename
        found = self._find_def(name, ctx)
        if found is None or "alias" not in found:
            return None
        self._ctx.append(found["module"])
        try:
            return _resolve_type(found["alias"], names, self)
        finally:
            self._ctx.pop()

    def _maybe_literal_union(self, ts_name: str, body: str,
                             names: _Names) -> str | None:
        """Synthesize a named revl `variant` from a literal-only union alias body,
        registering its declaration. `| null` / `| undefined` members wrap the
        whole variant in `Opt`. Returns the revl type (`Variant` or
        `Opt[Variant]`), or None when the body is not a pure literal union (a
        single member, or a union that mixes literals with a real type) — which
        then resolves or refuses through the ordinary path, unchanged."""
        members = _split_top(body, "|")
        if len(members) < 2:
            return None
        nullish = {"null", "undefined"}
        optional = any(m in nullish for m in members)
        literals = [_string_literal(m) for m in members if m not in nullish]
        # every non-nullish member must be a *string* literal — the tags. A
        # union that mixes a literal with a non-literal type (`'a' | number`) is
        # a genuine sum type with no tag, and stays refused honestly.
        if not literals or any(v is None for v in literals):
            return None

        revl = names.type_name(ts_name)
        if revl not in self.decls and revl not in self._pending:
            cases: list[str] = []
            seen: set[str] = set()
            for value in literals:
                case = _pascal(value)
                if case in seen:                 # two literals that pascal-collide
                    continue
                seen.add(case)
                if case in _BUILTIN_CASES:
                    names.renames.append(
                        f"variant case `{revl}.{case}` (from literal "
                        f"`'{value}'`) shadows revl's built-in `{case}` "
                        "constructor")
                cases.append(case)
            self.decls[revl] = f"type {revl} = {' | '.join(cases)}"
            self.origin[revl] = f"literal union `{ts_name}` in {self._ctx[-1]}"
            self.kind[revl] = "variant"
        return f"Opt[{revl}]" if optional else revl


def _resolve_type(ts: str, names: _Names,
                  records: "_Records | None" = None) -> str:
    """TypeScript type string -> revl surface type, or `Unrecoverable`.

    Every branch either produces a concrete revl type or refuses; nothing is
    guessed. An unknown *nominal* type is refused too — unless `records` can
    follow it to a record/interface definition (in this file or a local import)
    and transcribe it as a revl `type`.
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
    # (A *literal-only* union — the tags-are-the-cases case — is synthesized into
    # a named variant one layer up, in `_Records.resolve`, where the alias name
    # that gives the variant its name is still in hand; by the time a bare body
    # reaches here that name is gone, so a many-membered union refuses.)
    members = _split_top(ts, "|")
    if len(members) > 1:
        nullish = {"null", "undefined"}
        concrete = [m for m in members if m not in nullish]
        if len(concrete) != 1:
            raise Unrecoverable(ts, "a union of several concrete types is a sum "
                                    "type with no tag; revl needs a named "
                                    "`variant`, which this file does not define")
        inner = _resolve_type(concrete[0], names, records)
        return inner if inner.startswith("Opt[") else f"Opt[{inner}]"

    # intersection: revl has no intersection type, but a *branded primitive* —
    # `string & { readonly __brand: T }`, DSH's `Branded<T>` expansion — is, at
    # the value boundary this importer crosses, just its primitive. Drop the
    # brand object(s) and resolve the lone remaining member; anything else (two
    # real types intersected) has no revl spelling and refuses.
    inter = _split_top(ts, "&")
    if len(inter) > 1:
        carriers = [m for m in inter if not m.strip().startswith("{")]
        if len(carriers) == 1:
            return _resolve_type(carriers[0], names, records)
        raise Unrecoverable(ts, "an intersection of concrete types has no revl "
                                "spelling (only a primitive branded by an object "
                                "literal, `string & { __brand }`, is a primitive)")

    if ts.startswith("Promise<") and ts.endswith(">"):
        return _resolve_type(ts[len("Promise<"):-1], names, records)
    if ts.startswith("Array<") and ts.endswith(">"):
        return f"List[{_resolve_type(ts[len('Array<'):-1], names, records)}]"
    if ts.startswith("ReadonlyArray<") and ts.endswith(">"):
        return f"List[{_resolve_type(ts[len('ReadonlyArray<'):-1], names, records)}]"
    if ts.startswith("readonly "):
        ts = ts[len("readonly "):].strip()
    if ts.endswith("[]"):
        return f"List[{_resolve_type(ts[:-2], names, records)}]"
    if ts.startswith("Record<") and ts.endswith(">"):
        args = _split_top(ts[len("Record<"):-1])
        if len(args) == 2:
            return (f"Map[{_resolve_type(args[0], names, records)}, "
                    f"{_resolve_type(args[1], names, records)}]")

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
        # An unknown generic application `Name<...>`. Before refusing, try to
        # follow `Name` to a local *generic alias* and resolve its body: DSH's
        # `Branded<T> = string & { readonly __brand: T }` resolves, through the
        # intersection rule above, to `Str` — principled, not a name special-case.
        gm = re.match(r"([A-Za-z_$][\w$]*)\s*<.*>$", ts)
        if gm and records is not None:
            expanded = records.resolve_alias_body(gm.group(1), names)
            if expanded is not None:
                return expanded
        # Documented heuristic fallback: an unresolvable `Branded<...>` (the
        # alias is out of reach, e.g. it lives in an un-followed package) is a
        # branded string by DSH convention, so it maps to `Str`.
        if gm and gm.group(1) == "Branded":
            return "Str"
        raise Unrecoverable(ts, "a generic type this importer does not map "
                                "(known: `Array`, `Promise`, `Record`)")
    if re.fullmatch(r"[A-Za-z_$][\w$]*", ts):
        if records is not None:
            recovered = records.resolve(ts, names)
            if recovered is not None:
                return recovered
        raise Unrecoverable(ts, f"the nominal type `{ts}` is not defined in this "
                                "file or a local import, and has no built-in revl "
                                "mapping; declare its revl `type` by hand and "
                                "reference it")
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
                 mark_unrecovered: bool, source: str = "") -> None:
        self.plugin = plugin
        self.filename = filename
        self.backend = backend
        self.pure_requested = set(pure)
        self.pure_used: set[str] = set()
        self.mark_unrecovered = mark_unrecovered
        self.names = _Names()
        self.records = _Records(source, filename)
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
        revl = _resolve_type(ts, self.names, self.records)
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
        revl = _resolve_type(ts, self.names, self.records)
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

        for name in self.records.decls:
            kind = self.records.kind.get(name, "record")
            verb = "synthesized from" if kind == "variant" else "transcribed from"
            self.note(f"{kind} type `{name}` {verb} "
                      f"{self.records.origin[name]}")

        parts = [self._header()]
        # named record types followed from local imports, in dependency order
        # (a field's type is registered before the record that uses it).
        parts.extend(self.records.decls.values())
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
                      mark_unrecovered, source=source).emit()


def import_cordis_file(path: str, *, backend: str = "ts", service: str | None = None,
                       pure: object = (), mark_unrecovered: bool = False) -> str:
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    return import_cordis(source, filename=path, backend=backend, service=service,
                         pure=pure, mark_unrecovered=mark_unrecovered)
