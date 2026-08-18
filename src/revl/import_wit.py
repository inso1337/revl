"""WIT (WebAssembly Interface Types) -> revl source.

`revl import wit` is the second member of the import codegen family
(docs/v2.0-roadmap.md §4), after `revl mcp import`. It reads a `.wit` file
and emits revl: a `service` per WIT interface, revl `type` declarations for
the WIT data types, an `extern` per operation, and a provider component that
wires the two together.

Trust direction — the same as MCP's, and for the same reason.  A WIT world
describes *shape*: names, parameters, results. It says nothing about whether
an operation can be undone. revl's `emission` is not a shape claim, it is a
semantic one, and a service declaration is an **upper bound** on every
provider of that service (G4): an operation declared plain may never reach an
irreversible effect, in any provider, ever. Under-declaring one silently
breaks every consumer's G8 audit.

So the rule this importer adopts, mirroring `revl mcp import` (where only an
explicit `readOnlyHint: true` avoids `emission`):

    **Every imported operation is `emission` unless something explicitly and
    positively asserts otherwise.** WIT has no such assertion, so the two
    ways to make one are both out-of-band and both recorded in the generated
    header as human claims:

      * `/// @revl:pure` on the WIT function — the *interface author's*
        assertion, the direct analogue of `readOnlyHint: true`;
      * `--pure <interface>.<func>` at import time — the *importing
        engineer's* assertion.

    A pure-asserted operation is emitted as a plain `fn` backed by an
    `extern pure fn`, so the generated source is honest under G4: a plain
    operation reaches nothing irreversible. Everything else is
    `emission fn` backed by `extern emission fn`, and lands on the G8
    audit surface.

What this importer refuses (rather than emitting something wrong): resources
and handles (`borrow`/`own`), `stream`/`future`, `flags`, `tuple`, function
types as values, named/multiple results, inline interfaces in a world,
`include`, nested package blocks, and `use` from a package this file does not
define. Each refusal names the construct, its line, and the way forward.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import RevlError
from .lexer import KEYWORDS

# ---------------------------------------------------------------- type mapping

#: WIT primitive -> revl surface type. `u64` is mapped to `Int` on the
#: roadmap's instruction; values above 2^63-1 do not survive the trip, which
#: the generated header states rather than hides. `char` widens to `Str`
#: (revl has no character type) — safe inbound, lossy to validate outbound.
_PRIMITIVES = {
    "string": "Str",
    "char": "Str",
    "bool": "Bool",
    "s8": "Int", "s16": "Int", "s32": "Int", "s64": "Int",
    "u8": "Int", "u16": "Int", "u32": "Int", "u64": "Int",
    "f32": "Float", "f64": "Float",
    "float32": "Float", "float64": "Float",  # pre-0.2 spellings
}

#: revl's built-in type names — a WIT type named `result` or `list` would
#: shadow one, so it is renamed (and the rename is reported in the header).
_BUILTIN_TYPES = {"Str", "Int", "Float", "Bool", "Bytes", "Unit",
                  "List", "Map", "Opt", "Result"}

#: revl's built-in value constructors — a WIT variant case that lands on one
#: of these shadows it inside the generated module, which is reported.
_BUILTIN_CASES = {"Ok", "Err", "Some", "None"}

#: constructs that are refused by name, with the way forward
_REFUSED_TYPES = {
    "tuple": ("tuple<...>",
              "revl has no tuple type; declare a WIT `record` with named "
              "fields and re-import"),
    "borrow": ("borrow<T> (a resource handle)",
               "revl values are copies with no identity, so a borrowed handle "
               "has no honest revl spelling; expose the data the handle stands "
               "for as a `record`, or wrap the resource by hand as an "
               "`extern acquire fn ... undo ...`"),
    "own": ("own<T> (an owned resource handle)",
            "same as `borrow`: wrap the resource by hand as an "
            "`extern acquire fn ... undo ...` so its release is a tracked "
            "inverse (G4)"),
    "stream": ("stream<T>",
               "a stream is an open-ended effect, not a value; model it as a "
               "service whose operations are `emission`, written by hand"),
    "future": ("future<T>",
               "revl spells asynchrony as `async fn` on a service operation; "
               "declare that operation by hand"),
    "func": ("a `func` type used as a value (a callback)",
             "revl has no host-crossing function values; invert the callback "
             "into a service the host requires"),
}


# ------------------------------------------------------------------- the lexer

@dataclass
class _Tok:
    kind: str          # 'ident' | 'punct' | 'version' | 'eof'
    value: str
    line: int
    escaped: bool = False   # written `%name`: never a WIT keyword


_IDENT_START = re.compile(r"[A-Za-z]")
_VERSION = re.compile(r"[0-9A-Za-z.+-]+")
_PUNCT = set("{}()<>,:;=./@*_")


def _lex(source: str, filename: str) -> tuple[list[_Tok], dict[int, list[str]]]:
    """Tokens, plus the doc-comment lines attached to each token index.

    WIT doc comments (`///` and `/** */`) carry the `@revl:pure` marker and
    the descriptions copied into the generated source, so they are kept
    rather than skipped.
    """
    tokens: list[_Tok] = []
    docs: dict[int, list[str]] = {}
    pending: list[str] = []
    i, line, n = 0, 1, len(source)

    def attach() -> None:
        if pending:
            docs[len(tokens)] = list(pending)
            pending.clear()

    while i < n:
        c = source[i]
        if c == "\n":
            line += 1
            i += 1
        elif c.isspace():
            i += 1
        elif source.startswith("///", i):
            end = source.find("\n", i)
            end = n if end < 0 else end
            pending.append(source[i + 3:end].strip())
            i = end
        elif source.startswith("//", i):
            end = source.find("\n", i)
            i = n if end < 0 else end
        elif source.startswith("/*", i):
            doc = source.startswith("/**", i)
            depth, start, i = 1, i + 2, i + 2
            while i < n and depth:
                if source.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif source.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    line += 1 if source[i] == "\n" else 0
                    i += 1
            if depth:
                raise RevlError(filename, line, "unterminated block comment")
            if doc:
                body = source[start:i - 2]
                pending.extend(part.strip().lstrip("*").strip()
                               for part in body.splitlines() if part.strip())
        elif source.startswith("->", i):
            attach()
            tokens.append(_Tok("punct", "->", line))
            i += 2
        elif c == "%" or _IDENT_START.match(c):
            start = i
            i += 1 if c != "%" else 1
            while i < n and (source[i].isalnum()
                             or (source[i] == "-" and i + 1 < n and source[i + 1].isalnum())):
                i += 1
            attach()
            tokens.append(_Tok("ident", source[start:i].lstrip("%"), line,
                               escaped=c == "%"))
        elif c in _PUNCT:
            attach()
            tokens.append(_Tok("punct", c, line))
            i += 1
            if c == "@":
                match = _VERSION.match(source, i)
                if match:
                    tokens.append(_Tok("version", match.group(0), line))
                    i = match.end()
        else:
            raise RevlError(filename, line,
                            f"unexpected character {c!r} in WIT source",
                            hint="`revl import wit` reads the WIT grammar "
                                 "(interfaces, worlds, funcs, records, "
                                 "variants, enums, type aliases)")
    attach()
    tokens.append(_Tok("eof", "", line))
    return tokens, docs


# --------------------------------------------------------------- the WIT model

@dataclass
class _Func:
    name: str
    params: list[tuple[str, tuple]]
    result: tuple | None
    docs: list[str]
    line: int


@dataclass
class _TypeDef:
    name: str
    kind: str                    # 'record' | 'variant' | 'enum' | 'alias'
    payload: object              # fields / cases / alias target
    owner: str                   # the interface or world it was declared in
    line: int


@dataclass
class _Interface:
    name: str
    line: int
    funcs: list[_Func] = field(default_factory=list)
    docs: list[str] = field(default_factory=list)


@dataclass
class _World:
    name: str
    line: int
    exports: list[_Func] = field(default_factory=list)
    imports: list[_Func] = field(default_factory=list)
    refs: list[tuple[str, str, bool]] = field(default_factory=list)  # dir, name, local


@dataclass
class _Document:
    package: str | None = None
    types: dict[str, _TypeDef] = field(default_factory=dict)
    interfaces: list[_Interface] = field(default_factory=list)
    worlds: list[_World] = field(default_factory=list)


# ------------------------------------------------------------------ the parser

class _Parser:
    def __init__(self, source: str, filename: str) -> None:
        self.filename = filename
        self.tokens, self.docs = _lex(source, filename)
        self.pos = 0

    # -- token plumbing ----------------------------------------------------
    def peek(self, ahead: int = 0) -> _Tok:
        return self.tokens[min(self.pos + ahead, len(self.tokens) - 1)]

    def next(self) -> _Tok:
        tok = self.peek()
        if tok.kind != "eof":
            self.pos += 1
        return tok

    def at(self, kind: str, value: str | None = None) -> bool:
        tok = self.peek()
        return tok.kind == kind and (value is None or tok.value == value)

    def take_docs(self) -> list[str]:
        return list(self.docs.get(self.pos, ()))

    def expect(self, kind: str, value: str | None = None, what: str | None = None) -> _Tok:
        if not self.at(kind, value):
            tok = self.peek()
            found = "end of file" if tok.kind == "eof" else repr(tok.value)
            raise RevlError(self.filename, tok.line,
                            f"expected {what or value or kind}, found {found}")
        return self.next()

    def refuse(self, line: int, what: str, hint: str) -> RevlError:
        return RevlError(self.filename, line,
                         f"unsupported WIT construct: {what}", hint=hint)

    # -- entry point -------------------------------------------------------
    def parse(self) -> _Document:
        doc = _Document()
        if self.at("ident", "package"):
            doc.package = self.package_decl()
        while not self.at("eof"):
            docs = self.take_docs()
            tok = self.peek()
            if tok.kind != "ident":
                raise RevlError(self.filename, tok.line,
                                f"expected a top-level WIT item, found {tok.value!r}",
                                hint="top level holds `package`, `use`, `interface`, "
                                     "`world`, and type definitions")
            if tok.value == "interface":
                doc.interfaces.append(self.interface(docs, doc))
            elif tok.value == "world":
                doc.worlds.append(self.world(doc))
            elif tok.value == "use":
                self.use_item("<file>")
            elif tok.value == "package":
                raise self.refuse(tok.line, "a second `package` declaration",
                                  "one WIT file declares at most one package; "
                                  "split the packages into separate files")
            else:
                self.type_def(doc, owner="<file>")
        return doc

    def package_decl(self) -> str:
        self.next()                                   # `package`
        parts = [self.expect("ident", what="a package namespace").value]
        while self.at("punct", ":") or self.at("punct", "/"):
            parts.append(self.next().value)
            parts.append(self.expect("ident", what="a package name").value)
        name = "".join(parts)
        if self.at("punct", "@"):
            self.next()
            name += "@" + self.expect("version", what="a package version").value
        if self.at("punct", "{"):
            raise self.refuse(self.peek().line, "a nested `package { ... }` block",
                              "give the file a single top-level `package ...;` "
                              "declaration and split nested packages into their "
                              "own files")
        self.expect("punct", ";")
        return name

    # -- items -------------------------------------------------------------
    def interface(self, docs: list[str], doc: _Document) -> _Interface:
        line = self.next().line                       # `interface`
        name = self.expect("ident", what="an interface name").value
        iface = _Interface(name, line, docs=docs)
        self.expect("punct", "{")
        while not self.at("punct", "}"):
            item_docs = self.take_docs()
            tok = self.peek()
            if tok.kind == "eof":
                raise RevlError(self.filename, tok.line,
                                f"unterminated `interface {name}`")
            if tok.value == "use":
                self.use_item(name)
                continue
            if (not tok.escaped
                    and tok.value in ("record", "variant", "enum", "flags",
                                      "type", "resource")
                    and self.peek(1).kind == "ident"):
                self.type_def(doc, owner=name)
                continue
            iface.funcs.append(self.func_item(item_docs))
        self.expect("punct", "}")
        return iface

    def world(self, doc: _Document) -> _World:
        line = self.next().line                       # `world`
        name = self.expect("ident", what="a world name").value
        world = _World(name, line)
        self.expect("punct", "{")
        while not self.at("punct", "}"):
            item_docs = self.take_docs()
            tok = self.peek()
            if tok.kind == "eof":
                raise RevlError(self.filename, tok.line, f"unterminated `world {name}`")
            if tok.value == "include":
                raise self.refuse(tok.line, f"`include` in world `{name}`",
                                  "world inclusion needs a package resolver; "
                                  "flatten the world by listing its items "
                                  "explicitly, then re-import")
            if tok.value in ("import", "export"):
                self.world_item(world, self.next().value, item_docs)
                continue
            if tok.value == "use":
                self.use_item(name)
                continue
            self.type_def(doc, owner=name)
        self.expect("punct", "}")
        return world

    def world_item(self, world: _World, direction: str, docs: list[str]) -> None:
        name_tok = self.expect("ident", what="an import/export name")
        # `import wasi:io/streams@0.2.0;` — a reference to an interface, here or
        # elsewhere. `:` also starts `import name: func(...)`, so the package
        # form is recognised by the `/` or `:` that follows the next name.
        is_ref = (
            self.at("punct", ";") or self.at("punct", "/")
            or (self.at("punct", ":") and self.peek(2).kind == "punct"
                and self.peek(2).value in ("/", ":"))
        )
        if is_ref:
            parts = [name_tok.value]
            local = True
            while self.at("punct", ":") or self.at("punct", "/"):
                local = False
                parts.append(self.next().value)
                parts.append(self.expect("ident", what="an interface name").value)
            if self.at("punct", "@"):
                self.next()
                parts.append("@" + self.expect("version", what="a version").value)
            self.expect("punct", ";")
            world.refs.append((direction, "".join(parts), local))
            return
        self.expect("punct", ":")
        if self.at("ident", "interface"):
            raise self.refuse(self.peek().line,
                              f"an inline `interface` in world `{world.name}`",
                              "lift it to a top-level `interface <name> { ... }` and "
                              f"reference it from the world as "
                              f"`{direction} {name_tok.value};`")
        func = self.func_item(docs, name=name_tok.value, line=name_tok.line)
        (world.exports if direction == "export" else world.imports).append(func)

    def use_item(self, owner: str) -> None:
        line = self.next().line                       # `use`
        first = self.expect("ident", what="an interface name").value
        if self.at("punct", ":") or self.at("punct", "/"):
            raise self.refuse(line, f"`use` from another package (`{first}...`)",
                              "`revl import wit` resolves names inside one file "
                              "only; copy the types you need into this file, or "
                              "import the other package separately")
        while self.at("punct", "."):
            self.next()
            if self.at("punct", "{"):
                break
            self.expect("ident", what="a type name")
        if self.at("punct", "{"):
            self.next()
            while not self.at("punct", "}"):
                self.expect("ident", what="a type name")
                if self.at("ident", "as"):
                    self.next()
                    self.expect("ident", what="an alias name")
                if self.at("punct", ","):
                    self.next()
            self.expect("punct", "}")
        self.expect("punct", ";")
        # nothing to record: revl types share one module namespace, so a
        # locally-defined type is already in scope wherever it is used.

    def func_item(self, docs: list[str], name: str | None = None,
                  line: int | None = None) -> _Func:
        if name is None:
            tok = self.expect("ident", what="a function name")
            name, line = tok.value, tok.line
            self.expect("punct", ":")
        if self.at("ident", "async"):
            raise self.refuse(self.peek().line, f"`async func` (`{name}`)",
                              "revl spells asynchrony as `async fn` on a service "
                              "operation; declare this operation by hand")
        self.expect("ident", "func", what="`func`")
        params = self.params()
        result = None
        if self.at("punct", "->"):
            arrow = self.next().line
            if self.at("punct", "("):
                raise self.refuse(arrow, f"a named/multiple result list on `{name}`",
                                  "revl operations return one value; declare a WIT "
                                  "`record` holding the results and return that")
            result = self.type_ref()
        self.expect("punct", ";")
        return _Func(name, params, result, docs, line or 0)

    def params(self) -> list[tuple[str, tuple]]:
        self.expect("punct", "(")
        params: list[tuple[str, tuple]] = []
        while not self.at("punct", ")"):
            pname = self.expect("ident", what="a parameter name").value
            self.expect("punct", ":")
            params.append((pname, self.type_ref()))
            if self.at("punct", ","):
                self.next()
        self.expect("punct", ")")
        return params

    # -- types -------------------------------------------------------------
    def type_def(self, doc: _Document, owner: str) -> None:
        tok = self.peek()
        keyword = tok.value
        if keyword == "resource":
            raise self.refuse(tok.line, f"`resource {self.peek(1).value}`",
                              "a resource is a live handle with identity; revl "
                              "values are copies without identity, so there is no "
                              "faithful mapping. Wrap the resource by hand: "
                              "`extern acquire fn open(...) undo close(...)` plus "
                              "`emission` operations, so its lifetime is a tracked "
                              "inverse (G4)")
        if keyword == "flags":
            raise self.refuse(tok.line, f"`flags {self.peek(1).value}`",
                              "revl has no bit-set type; declare a WIT `record` of "
                              "`bool` fields, or a `list` of an `enum`, and re-import")
        if keyword not in ("record", "variant", "enum", "type"):
            raise RevlError(self.filename, tok.line,
                            f"expected a WIT type definition, found {keyword!r}",
                            hint="supported: `record`, `variant`, `enum`, and "
                                 "`type <name> = <type>;` aliases")
        self.next()
        name_tok = self.expect("ident", what=f"a name after `{keyword}`")
        name, line = name_tok.value, name_tok.line

        if keyword == "type":
            self.expect("punct", "=")
            target = self.type_ref()
            self.expect("punct", ";")
            self._register(doc, _TypeDef(name, "alias", target, owner, line))
            return
        if keyword == "record":
            self.expect("punct", "{")
            fields: list[tuple[str, tuple]] = []
            while not self.at("punct", "}"):
                fname = self.expect("ident", what="a field name").value
                self.expect("punct", ":")
                fields.append((fname, self.type_ref()))
                if self.at("punct", ","):
                    self.next()
            self.expect("punct", "}")
            self._register(doc, _TypeDef(name, "record", fields, owner, line))
            return
        # variant / enum
        self.expect("punct", "{")
        cases: list[tuple[str, tuple | None]] = []
        while not self.at("punct", "}"):
            cname = self.expect("ident", what="a case name").value
            payload = None
            if self.at("punct", "("):
                if keyword == "enum":
                    raise self.refuse(self.peek().line,
                                      f"a payload on enum case `{name}.{cname}`",
                                      "a WIT `enum` case carries no payload; use "
                                      "`variant` if the case needs one")
                self.next()
                payload = self.type_ref()
                self.expect("punct", ")")
            cases.append((cname, payload))
            if self.at("punct", ","):
                self.next()
        self.expect("punct", "}")
        self._register(doc, _TypeDef(name, "variant", cases, owner, line))

    def _register(self, doc: _Document, definition: _TypeDef) -> None:
        existing = doc.types.get(definition.name)
        if existing is None:
            doc.types[definition.name] = definition
            return
        if (existing.kind, existing.payload) == (definition.kind, definition.payload):
            return                                    # same shape twice: harmless
        raise RevlError(
            self.filename, definition.line,
            f"WIT type `{definition.name}` is defined twice with different shapes "
            f"(in `{existing.owner}` at line {existing.line}, and in "
            f"`{definition.owner}` here)",
            hint="revl types share one module namespace, so the two definitions "
                 "would collide; rename one of them in the WIT and re-import",
        )

    def type_ref(self) -> tuple:
        tok = self.peek()
        if tok.kind == "punct" and tok.value == "_":
            self.next()
            return ("unit",)
        name_tok = self.expect("ident", what="a type")
        name, line = name_tok.value, name_tok.line
        if name_tok.escaped:          # `%result` names a user type, not the builtin
            return ("named", name, line)
        if name in _REFUSED_TYPES:
            what, hint = _REFUSED_TYPES[name]
            raise self.refuse(line, what, hint)
        if name in ("list", "option"):
            self.expect("punct", "<")
            inner = self.type_ref()
            self.expect("punct", ">")
            return ("list" if name == "list" else "option", inner)
        if name == "result":
            if not self.at("punct", "<"):
                return ("result", ("unit",), ("unit",))
            self.next()
            ok = self.type_ref()
            err: tuple = ("unit",)
            if self.at("punct", ","):
                self.next()
                err = self.type_ref()
            self.expect("punct", ">")
            return ("result", ok, err)
        if self.at("punct", "<"):
            raise self.refuse(line, f"a generic WIT type `{name}<...>`",
                              "supported generics are `list`, `option`, and "
                              "`result`")
        if name in _PRIMITIVES:
            return ("prim", name)
        return ("named", name, line)


# --------------------------------------------------------------- name mapping

def _pascal(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in name.split("-") if part)


def _snake(name: str) -> str:
    ident = re.sub(r"[^A-Za-z0-9_]", "_", name.replace("-", "_"))
    if not re.match(r"^[A-Za-z_]", ident):
        ident = f"w_{ident}"
    return f"{ident}_" if ident in KEYWORDS else ident


class _Names:
    """WIT names -> revl names, remembering every rename so the generated
    header can report it (a silent rename is a trap for the reader)."""

    def __init__(self) -> None:
        self.renames: list[str] = []

    def type_name(self, wit: str) -> str:
        name = _pascal(wit)
        if name not in _BUILTIN_TYPES:
            return name
        note = f"type `{wit}` -> `{name}Ty` (`{name}` is a revl built-in)"
        if note not in self.renames:
            self.renames.append(note)
        return f"{name}Ty"


# ----------------------------------------------------------------- generation

_BACKEND_COMMENT = {"wasm": ";;", "ts": "//", "rust": "//", "py": "#"}


class _Generator:
    def __init__(self, doc: _Document, filename: str, backend: str,
                 pure: set[str]) -> None:
        self.doc = doc
        self.filename = filename
        self.backend = backend
        self.pure_requested = pure
        self.pure_used: set[str] = set()
        self.names = _Names()
        self.notes: list[str] = []
        self.services: dict[str, int] = {}

    # -- types -------------------------------------------------------------
    def resolve(self, ref: tuple, line: int, seen: tuple = ()) -> str:
        kind = ref[0]
        if kind == "unit":
            return "Unit"
        if kind == "prim":
            return _PRIMITIVES[ref[1]]
        if kind == "list":
            return f"List[{self.resolve(ref[1], line, seen)}]"
        if kind == "option":
            inner = self.resolve(ref[1], line, seen)
            if inner.startswith("Opt["):
                raise RevlError(
                    self.filename, line, "unsupported WIT construct: `option<option<T>>`",
                    hint="revl's `Opt[T]` does not nest — `Opt[Opt[T]]` cannot "
                         "distinguish the two absences; declare a WIT `variant` "
                         "that names both cases explicitly")
            return f"Opt[{inner}]"
        if kind == "result":
            return (f"Result[{self.resolve(ref[1], line, seen)}, "
                    f"{self.resolve(ref[2], line, seen)}]")
        name, ref_line = ref[1], ref[2]
        spec = self.doc.types.get(name)
        if spec is None:
            raise RevlError(
                self.filename, ref_line, f"unknown WIT type `{name}`",
                hint="it is not defined in this file. `revl import wit` resolves "
                     "names inside one file only — copy the definition in, or "
                     "define it before use")
        if spec.kind != "alias":
            return self.names.type_name(name)
        if name in seen:
            chain = " -> ".join((*seen, name))
            raise RevlError(self.filename, spec.line,
                            f"circular WIT type alias: {chain}",
                            hint="break the cycle by declaring a `record` or "
                                 "`variant` at one point in it")
        return self.resolve(spec.payload, line, (*seen, name))

    def type_decls(self) -> list[str]:
        out: list[str] = []
        for name, spec in self.doc.types.items():
            revl = self.names.type_name(name)
            if spec.kind == "alias":
                target = self.resolve(spec.payload, spec.line, (name,))
                # `type X = Y` in revl declares a one-case *variant*, not an
                # alias, so an alias is inlined at its use sites instead.
                self.notes.append(
                    f"WIT `type {name} = ...` is inlined as `{target}` "
                    "(revl has no type alias: `type X = Y` would declare a variant)")
                continue
            if spec.kind == "record":
                fields = ", ".join(f"{_snake(fname)}: {self.resolve(ftype, spec.line)}"
                                   for fname, ftype in spec.payload)
                out.append(f"type {revl} = {{ {fields} }}")
                continue
            rendered = []
            for cname, payload in spec.payload:
                case = _pascal(cname)
                if case in _BUILTIN_CASES:
                    self.notes.append(
                        f"`{revl}.{case}` shadows revl's built-in `{case}` "
                        f"constructor inside this module — rename `{cname}` in the "
                        "WIT if you mean to write `Opt`/`Result` values here too")
                rendered.append(
                    case + (f"({self.resolve(payload, spec.line)})" if payload else ""))
            out.append(f"type {revl} = {' | '.join(rendered)}")
        return out

    # -- operations --------------------------------------------------------
    def _is_pure(self, iface: str, func: _Func) -> str | None:
        """The assertion that lets this operation be a plain `fn`, or None."""
        if any("@revl:pure" in doc for doc in func.docs):
            return f"`/// @revl:pure` in {self.filename}"
        for candidate in (f"{iface}.{func.name}", func.name):
            if candidate in self.pure_requested:
                self.pure_used.add(candidate)
                return f"`--pure {candidate}` at import time"
        return None

    def _host_comment(self, text: str) -> str:
        marker = _BACKEND_COMMENT[self.backend]
        return f"{marker} {text.replace('{', '(').replace('}', ')')}"

    def operations(self, iface_name: str, funcs: list[_Func], *,
                   extern_prefix: str, with_bodies: bool) -> tuple[list[str], list[str], list[str]]:
        """(service operation lines, extern declarations, provide methods)."""
        ops: list[str] = []
        externs: list[str] = []
        methods: list[str] = []
        for func in funcs:
            op = _snake(func.name)
            sig = ", ".join(f"{_snake(pname)}: {self.resolve(ptype, func.line)}"
                            for pname, ptype in func.params)
            returns = (f" -> {self.resolve(func.result, func.line)}"
                       if func.result is not None else "")
            assertion = self._is_pure(iface_name, func)

            for doc in func.docs:
                if doc and "@revl:pure" not in doc:
                    ops.append(f"  // {doc[:88]}")
            if assertion:
                ops.append(f"  // plain by assertion: {assertion} — WIT itself "
                           "makes no such claim")
            else:
                ops.append("  // WIT carries no reversibility claim: `emission` "
                           "by default (G8)")
            ops.append(f"  {'' if assertion else 'emission '}fn {op}({sig}){returns}")

            if not with_bodies:
                continue
            extern = f"{extern_prefix}{op}"
            target = (f'{self.doc.package or "<package>"}/{iface_name}#{func.name}')
            externs.append(
                f"extern {'pure' if assertion else 'emission'} fn {extern}({sig}){returns}\n"
                f"  = @{self.backend} {{ "
                f"{self._host_comment(f'call the WIT export {target} here')} }}"
            )
            args = ", ".join(_snake(pname) for pname, _ in func.params)
            methods.append(f"    fn {op}({args}) = {extern}({args})")
        return ops, externs, methods

    def _service_name(self, wit_name: str, line: int, suffix: str = "") -> str:
        name = self.names.type_name(wit_name) + suffix
        if name in self.services:
            raise RevlError(
                self.filename, line,
                f"two WIT items both generate the revl service `{name}`",
                hint=f"the first is at line {self.services[name]}; rename one of "
                     "them in the WIT (interfaces and worlds share revl's one "
                     "service namespace)")
        self.services[name] = line
        return name

    def block(self, wit_name: str, line: int, funcs: list[_Func], *,
              suffix: str = "", provided: bool = True,
              preamble: list[str] | None = None) -> list[str]:
        if not funcs:
            return []
        service = self._service_name(wit_name, line, suffix)
        key = _snake(wit_name) + ("_imports" if suffix else "")
        ops, externs, methods = self.operations(
            wit_name, funcs,
            extern_prefix=f"wit_{_snake(wit_name)}_",
            with_bodies=provided)
        out: list[str] = []
        head = "\n".join(preamble or ())
        out.append((head + "\n" if head else "")
                   + f"service {service} {{\n" + "\n".join(ops) + "\n}")
        if not provided:
            return out
        out.extend(externs)
        out.append(
            f"component {service}Provider provides {key}: {service} {{\n"
            f"  provide {key} {{\n" + "\n".join(methods) + "\n  }\n}")
        return out

    # -- the whole file ----------------------------------------------------
    def emit(self) -> str:
        blocks: list[str] = []
        types = self.type_decls()

        for iface in self.doc.interfaces:
            preamble = [f"// interface {iface.name}"] + \
                       [f"// {line[:88]}" for line in iface.docs if line]
            blocks.extend(self.block(iface.name, iface.line, iface.funcs,
                                     preamble=preamble))

        for world in self.doc.worlds:
            for direction, name, local in world.refs:
                where = "defined in this file (see above)" if local \
                    else "defined in another package — not imported"
                self.notes.append(
                    f"world `{world.name}` {direction}s `{name}`: {where}")
            blocks.extend(self.block(
                world.name, world.line, world.exports,
                preamble=[f"// world {world.name} — its inline exports "
                          "(what a revl consumer may call)"]))
            blocks.extend(self.block(
                world.name, world.line, world.imports, suffix="Imports",
                provided=False,
                preamble=[f"// world {world.name} — its inline imports: what the "
                          "component expects to be *given*.",
                          "// Declaration only; a revl component must provide this "
                          "key. `emission` is the safe",
                          "// direction here — a declaration is an upper bound, so "
                          "a provider may do less (G4)."]))

        unused = sorted(self.pure_requested - self.pure_used)
        if unused:
            raise RevlError(
                self.filename, 0,
                f"--pure named {', '.join(repr(n) for n in unused)}, which is not a "
                "function in this WIT",
                hint="spell it `<interface>.<func>` or `<func>`, in the WIT's own "
                     "kebab-case; a typo here would silently leave the operation "
                     "`emission`")

        if not blocks:
            raise RevlError(self.filename, 0,
                            "this WIT file declares no functions to import",
                            hint="`revl import wit` generates a service per "
                                 "interface; a file of type definitions alone has "
                                 "no operations to project")

        parts = [self.header()]
        if types:
            parts.append("\n".join(types))
        parts.extend(blocks)
        return "\n\n".join(parts) + "\n"

    def header(self) -> str:
        lines = [
            "// Generated by `revl import wit` — do not edit by hand.",
            f"// Source WIT: {self.filename}",
        ]
        if self.doc.package:
            lines.append(f"// WIT package: {self.doc.package}")
        lines += [
            "//",
            "// This boundary is TRUSTED, NOT CHECKED (G8). WIT describes shape —",
            "// names, parameters, results — and says nothing about whether an",
            "// operation can be undone. revl's `emission` is a semantic claim, and a",
            "// service declaration is an upper bound on every provider of it (G4),",
            "// so an under-declared operation silently breaks every consumer's",
            "// audit. Therefore: every operation below is `emission` unless something",
            "// explicitly asserted otherwise (`/// @revl:pure` in the WIT, or",
            "// `--pure` at import time), and each such assertion is a human claim",
            "// this compiler cannot verify. Narrow a classification only after",
            "// reading the implementation on the other side.",
            "//",
            "// The extern bodies are stubs: the typed boundary is generated, the host",
            "// call is not. Fill each one in, then `revl audit` shows the surface.",
            "//",
            f"// Number mapping is lossy in one direction: WIT `u64`/`s64` become revl",
            "// `Int`, so a u64 above 2^63-1 does not survive the trip; `char` widens",
            "// to `Str` (revl has no character type).",
        ]
        if self.backend == "wasm":
            lines += [
                "//",
                "// The extern bodies are tagged `@wasm` because that is where a WIT",
                "// world lives — but the cordis-wasm tier is i32-only today, so a",
                "// service carrying `Str`/records/lists does not lower there yet.",
                "// Re-import with `--backend ts|py|rust` to target a hosted tier.",
            ]
        for note in self.notes + self.names.renames:
            lines.append(f"// note: {note}")
        return "\n".join(lines)


# --------------------------------------------------------------- public API

def import_wit(source: str, *, filename: str = "<wit>", backend: str = "wasm",
               pure: object = ()) -> str:
    """Parse WIT `source` and return revl source.

    Raises `RevlError` — naming the construct, its line, and the way
    forward — for any WIT this importer refuses to translate.
    """
    if backend not in _BACKEND_COMMENT:
        raise RevlError(filename, 0, f"unknown host backend {backend!r}",
                        hint=f"known: {', '.join(sorted(_BACKEND_COMMENT))}")
    document = _Parser(source, filename).parse()
    return _Generator(document, filename, backend, set(pure or ())).emit()


def import_wit_file(path: str, *, backend: str = "wasm", pure: object = ()) -> str:
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    return import_wit(source, filename=path, backend=backend, pure=pure)
