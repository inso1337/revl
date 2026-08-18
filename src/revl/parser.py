"""Recursive-descent parser for revl v0.

Grammar (v0 subset — see DESIGN.md §3):

    program    := (service | component)*
    service    := 'service' IDENT '{' methoddecl* '}'
    methoddecl := modifier* 'fn' IDENT '(' [tparam (',' tparam)*] ')' ['->' type]
    modifier   := 'emission' ['[' IDENT (',' IDENT)* ']'] | 'async' | 'commutative'
    tparam     := IDENT ':' type
    type       := IDENT ['[' type (',' type)* ']']
    component  := 'component' IDENT ['requires' binds] ['provides' binds] '{' body '}'
    binds      := IDENT ':' IDENT (',' IDENT ':' IDENT)*
    body       := (configdecl | stmt)*
    configdecl := 'config' '{' cfield (',' cfield)* [','] '}'
    stmt       := 'let' IDENT '=' effectform | effectform | 'emit' expr | provide
    effectform := 'effect' expr 'undo' expr          # G4: undo is not optional
    provide    := 'provide' IDENT '{' pmethod* '}'
    pmethod    := 'fn' IDENT '(' [IDENT (',' IDENT)*] ')' ('=' expr | '{' mstmt* '}')
    mstmt      := effectform | 'emit' expr | 'return' expr
    expr       := postfix
    postfix    := primary ('.' IDENT ['(' [expr (',' expr)*] ')'])*
    primary    := IDENT | INT | STRING | 'true' | 'false' | 'null'
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import RevlError
from .lexer import Token, lex


# ---------------------------------------------------------------- AST

@dataclass
class MethodDecl:
    name: str
    params: list[tuple[str, str]]  # (name, type)
    returns: str | None
    emission: bool
    line: int
    async_: bool = False       # `async fn` service operation (§5)
    commutative: bool = False  # Def. 39 order-independence opt-in
    # capability-scoped emission (docs/capabilities.md): `emission[db, log]`
    # bounds *where* a provider may emit. `None` is bare `emission` — "any
    # capability" — which is what every pre-capability source means, so
    # existing programs keep their meaning.
    capabilities: tuple[str, ...] | None = None


@dataclass
class ServiceDecl:
    name: str
    methods: dict[str, MethodDecl]
    line: int
    commutative: bool = False  # service-wide order-independence opt-in


@dataclass
class ConfigField:
    name: str
    type: str
    default: object
    line: int


@dataclass
class PostfixOp:
    name: str
    args: list | None  # None = field access, list = call
    line: int


@dataclass
class Postfix:
    head: str
    ops: list[PostfixOp]
    line: int


@dataclass
class Lit:
    value: object
    line: int


@dataclass
class Interp:
    parts: list[tuple[str, str]]
    line: int


@dataclass
class LetEffect:
    bind: str
    acquire: object
    undo: object
    line: int
    setup: list = field(default_factory=list)


@dataclass
class EffectStmt:
    acquire: object
    undo: object
    line: int
    setup: list = field(default_factory=list)


@dataclass
class FailStmt:
    message: object
    line: int


@dataclass
class EmitStmt:
    expr: object
    line: int
    compensate: object | None = None


@dataclass
class EmitExpr:
    """`emit <call>` in value position — the value of an irreversible call.

    The marker still sits at the call site (G4's point); it no longer forces
    the value to be discarded, which an emission that *returns* data (an LLM
    completion, an HTTP GET) obviously needs."""
    expr: object
    line: int


@dataclass
class AwaitStmt:
    expr: object
    line: int


@dataclass
class ReturnStmt:
    expr: object
    line: int


@dataclass
class IsolateStmt:
    key: str
    realm: str
    line: int


@dataclass
class InterceptStmt:
    key: str
    metadata: dict
    line: int


@dataclass
class ProvideMethod:
    name: str
    params: list[str]
    body: list
    line: int
    async_: bool = False  # `async fn` provide-method declaration (§5)
    # optional per-param type annotations (parallel to `params`, None where
    # omitted): `fn query(sql: Str) = ...`. The service is the source of
    # truth (A6); an annotation, if present, is checked against it.
    param_types: list = field(default_factory=list)
    returns: str | None = None  # optional `-> T` annotation, checked vs service


@dataclass
class ProvideStmt:
    key: str
    methods: list[ProvideMethod]
    line: int


@dataclass
class ComponentDecl:
    name: str
    config: list[ConfigField]
    requires: list[tuple[str, str, int]]  # (local, service, line)
    provides: list[tuple[str, str, int]]  # (key, service, line)
    body: list
    line: int
    source: str = ""  # provenance: the file this component was parsed from


# --- v2.0: types & pure functions (docs/syntax-2.0.md §2–§3) ----------------

@dataclass
class RecordField:
    name: str
    type: str
    line: int


@dataclass
class VariantCase:
    name: str
    payload: str | None
    line: int


@dataclass
class TypeDecl:
    name: str
    params: list[str]
    fields: list[RecordField]
    cases: list[VariantCase]
    line: int
    public: bool = False


@dataclass
class UseDecl:
    path: str
    names: list[str] | None  # None = module namespace import
    alias: str | None
    line: int


@dataclass
class HostBody:
    backend: str
    text: str
    line: int


@dataclass
class ExternDecl:
    name: str
    classification: str  # 'pure' | 'acquire' | 'emission'
    params: list[FnParam]
    returns: str | None
    undo: object | None
    compensate: object | None
    bodies: list[HostBody]
    public: bool
    line: int


@dataclass
class FnParam:
    name: str
    type: str
    line: int


@dataclass
class FnDecl:
    name: str
    params: list[FnParam]
    returns: str | None
    body: list
    public: bool
    line: int
    verified: bool = False


# pure-expression AST (§3.2 — the TS-subset stratum)

@dataclass
class ExprLit:
    value: object
    line: int


@dataclass
class ExprVar:
    name: str
    line: int


@dataclass
class ExprBin:
    op: str
    left: object
    right: object
    line: int


@dataclass
class ExprUn:
    op: str
    operand: object
    line: int


@dataclass
class ExprCall:
    callee: object
    args: list
    line: int


@dataclass
class ExprField:
    target: object
    name: str
    line: int


@dataclass
class ExprIndex:
    target: object
    index: object
    line: int


@dataclass
class ExprOptField:
    target: object
    name: str
    line: int


@dataclass
class ExprOptCall:
    target: object
    method: str
    args: list
    line: int


@dataclass
class ExprIf:
    cond: object
    then: object
    otherwise: object
    line: int


@dataclass
class ExprRecord:
    fields: list
    line: int


@dataclass
class ExprList:
    items: list
    line: int


@dataclass
class ExprArrow:
    params: list[str]
    body: object
    line: int


@dataclass
class ExprMatch:
    scrutinee: object
    arms: list  # [(pattern_name | "_", bind_name | None, body_expr)]
    line: int


# fn-body statements

@dataclass
class RecordPattern:
    fields: list[str]
    line: int


@dataclass
class ListPattern:
    binds: list[str]
    rest: str | None
    line: int


@dataclass
class LetPatternStmt:
    pattern: RecordPattern | ListPattern
    value: object
    mutable: bool
    line: int


@dataclass
class LetStmt:
    name: str
    value: object
    mutable: bool
    line: int


@dataclass
class AssignStmt:
    name: str
    value: object
    line: int
    op: str = "="


@dataclass
class WhileStmt:
    cond: object
    body: list
    line: int


@dataclass
class ForStmt:
    bind: str
    iterable: object
    body: list
    line: int


@dataclass
class IfStmt:
    cond: object
    then: list
    otherwise: list | None
    line: int


@dataclass
class ExprStmt:
    expr: object
    line: int


@dataclass
class AssertStmt:
    expr: object
    line: int


@dataclass
class TestDecl:
    name: str
    body: list
    line: int


@dataclass
class Program:
    filename: str
    services: list[ServiceDecl] = field(default_factory=list)
    components: list[ComponentDecl] = field(default_factory=list)
    type_decls: list[TypeDecl] = field(default_factory=list)
    fn_decls: list[FnDecl] = field(default_factory=list)
    uses: list[UseDecl] = field(default_factory=list)
    externs: list[ExternDecl] = field(default_factory=list)
    tests: list[TestDecl] = field(default_factory=list)
    # Set by compile_files so the checker can resolve module-private vs
    # imported names without merging all files into one global namespace.
    fn_scopes: dict[int, set[str]] = field(default_factory=dict)
    fn_alias_scopes: dict[int, dict[str, set[str]]] = field(default_factory=dict)
    # Provenance for non-component declarations: id(decl) -> source file.
    # Components carry their own `source`; fns, externs and services do not,
    # and a why-trace needs a file for every hop it names (why.py). Keyed by
    # identity like `fn_scopes`, so merging modules preserves it for free.
    decl_files: dict[int, str] = field(default_factory=dict)


# `===` -> `==`, `!==` -> `!=`: both spellings, one meaning (structural,
# no coercion); the formatter story is canonicalization, the IR never
# carries the triple form
_CANONICAL_OPS = {"===": "==", "!==": "!="}


# ---------------------------------------------------------------- parser

class Parser:
    def __init__(self, source: str, filename: str):
        self.filename = filename
        self.toks = lex(source, filename)
        self.pos = 0

    # -- token helpers

    def peek(self) -> Token:
        return self.toks[self.pos]

    def next(self) -> Token:
        tok = self.toks[self.pos]
        self.pos += 1
        return tok

    def at(self, kind: str, value=None) -> bool:
        tok = self.peek()
        return tok.kind == kind and (value is None or tok.value == value)

    def expect(self, kind: str, value=None, what: str | None = None) -> Token:
        tok = self.peek()
        if not self.at(kind, value):
            wanted = what or (value if value is not None else kind)
            got = repr(tok.value) if tok.value is not None else "end of file"
            raise RevlError(self.filename, tok.line, f"expected {wanted}, found {got}")
        return self.next()

    def err(self, line: int, message: str, hint: str | None = None) -> RevlError:
        return RevlError(self.filename, line, message, hint)

    # -- productions

    def parse(self) -> Program:
        program = Program(self.filename)
        while not self.at("eof"):
            if self.at("kw", "use"):
                program.uses.append(self.use_decl())
            elif self.at("kw", "service"):
                program.services.append(self.service(commutative=False))
            elif self.at("kw", "commutative"):
                self.next()
                if not self.at("kw", "service"):
                    tok = self.peek()
                    raise self.err(tok.line, f"expected `service` after `commutative`, found {tok.value!r}")
                program.services.append(self.service(commutative=True))
            elif self.at("kw", "component"):
                program.components.append(self.component())
            elif self.at("kw", "type"):
                program.type_decls.append(self.type_decl(False))
            elif self.at("kw", "pub"):
                self.next()
                verified = False
                if self.at("kw", "verified"):
                    self.next()
                    verified = True
                commutative = False
                if self.at("kw", "commutative"):
                    self.next()
                    commutative = True
                if self.at("kw", "fn"):
                    if commutative:
                        tok = self.peek()
                        raise self.err(tok.line, f"expected `service` after `commutative`, found {tok.value!r}")
                    program.fn_decls.append(self.fn_decl(True, verified))
                elif self.at("kw", "type"):
                    if commutative:
                        tok = self.peek()
                        raise self.err(tok.line, f"expected `service` after `commutative`, found {tok.value!r}")
                    program.type_decls.append(self.type_decl(True))
                elif self.at("kw", "service"):
                    # services are interfaces and are pub by default; a `pub`
                    # prefix is accepted as documentation but adds nothing
                    program.services.append(self.service(commutative=commutative))
                elif self.at("kw", "extern"):
                    if commutative:
                        tok = self.peek()
                        raise self.err(tok.line, f"expected `service` after `commutative`, found {tok.value!r}")
                    program.externs.append(self.extern_decl(True))
                elif self.at("kw", "component"):
                    tok = self.peek()
                    raise self.err(
                        tok.line,
                        "components are never `pub` — they are composed through the manifest, not imported",
                    )
                else:
                    tok = self.peek()
                    if commutative:
                        raise self.err(tok.line, f"expected `service` after `commutative`, found {tok.value!r}")
                    raise self.err(tok.line, f"expected `fn`, `type`, `service`, or `extern` after `pub`, found {tok.value!r}")
            elif self.at("kw", "verified"):
                self.next()
                if self.at("kw", "fn"):
                    program.fn_decls.append(self.fn_decl(False, True))
                else:
                    tok = self.peek()
                    raise self.err(tok.line, f"expected `fn` after `verified`, found {tok.value!r}")
            elif self.at("kw", "extern"):
                program.externs.append(self.extern_decl(False))
            elif self.at("kw", "fn"):
                program.fn_decls.append(self.fn_decl(False))
            elif self.at("kw", "test"):
                program.tests.append(self.test_decl())
            else:
                tok = self.peek()
                raise self.err(tok.line, f"expected a top-level declaration, found {tok.value!r}")
        return program

    def use_decl(self) -> UseDecl:
        line = self.expect("kw", "use").line
        path_tok = self.expect("string", what="a module path string")
        path = path_tok.value
        if not path:
            raise self.err(line, "`use` path cannot be empty")
        names = None
        alias = None
        if self.at("{"):
            self.next()
            names = []
            while not self.at("}"):
                names.append(self.expect("ident").value)
                if self.at(","):
                    self.next()
            self.expect("}")
        elif self.at("kw", "as"):
            self.next()
            alias = self.expect("ident").value
        else:
            tok = self.peek()
            raise self.err(tok.line, f"expected `{{` or `as` after `use` path, found {tok.value!r}")
        return UseDecl(path, names, alias, line)

    def extern_decl(self, public: bool) -> ExternDecl:
        line = self.expect("kw", "extern").line
        if not self.at("kw") or self.peek().value not in ("pure", "acquire", "emission"):
            tok = self.peek()
            raise self.err(
                line,
                "unclassified extern — expected `pure`, `acquire`, or `emission` after `extern`",
                hint="classification is mandatory: `pure` has no observable effect, "
                     "`acquire` must declare `undo`, and `emission` may declare `compensate`",
            )
        classification = self.next().value
        self.expect("kw", "fn")
        name = self.expect("ident").value
        self.expect("(")
        params: list[FnParam] = []
        while not self.at(")"):
            pline = self.peek().line
            pname = self.expect("ident").value
            self.expect(":")
            ptype = self.type_()
            params.append(FnParam(pname, ptype, pline))
            if self.at(","):
                self.next()
        self.expect(")")
        returns = None
        if self.at("arrow"):
            self.next()
            returns = self.type_()
        undo = None
        compensate = None
        if self.at("kw", "undo"):
            self.next()
            undo = self.pure_expr()
        if self.at("kw", "compensate"):
            self.next()
            compensate = self.pure_expr()
        bodies: list[HostBody] = []
        while self.at("="):
            self.next()
            host_tok = self.expect("hostbody", what="`@backend { ... }` host body")
            backend, text = host_tok.value
            bodies.append(HostBody(backend, text, host_tok.line))
        if not bodies:
            raise self.err(line, f"extern `{name}` must declare at least one `@backend {{ ... }}` body")
        return ExternDecl(name, classification, params, returns, undo, compensate, bodies, public, line)

    def _capability_list(self) -> tuple[str, ...]:
        """`[a, b]` after `emission` — the boundaries this operation may cross.

        Names are wiring names, not types: a requirement/provision key or an
        `emission` extern (docs/capabilities.md). They are not resolved here —
        a service is written before its providers exist — the G4 check in
        lower.py compares them against what a provider's body actually reaches.
        """
        line = self.expect("[").line
        names: list[str] = []
        while not self.at("]"):
            names.append(self.expect("ident", what="a capability name").value)
            if self.at(","):
                self.next()
        self.expect("]")
        if not names:
            raise self.err(
                line,
                "`emission[]` names no capability, so it forbids every emission",
                hint="an operation that may not emit is a plain `fn` — drop the "
                     "`emission` modifier instead (G4)",
            )
        seen: set[str] = set()
        for cap in names:
            if cap in seen:
                raise self.err(line, f"duplicate capability `{cap}` in `emission[...]`")
            seen.add(cap)
        return tuple(names)

    def service(self, commutative: bool = False) -> ServiceDecl:
        line = self.expect("kw", "service").line
        name = self.expect("ident").value
        self.expect("{")
        methods: dict[str, MethodDecl] = {}
        while not self.at("}"):
            emission = False
            capabilities: tuple[str, ...] | None = None
            async_ = False
            method_commutative = False
            mline = self.peek().line
            while self.at("kw") and self.peek().value in ("emission", "async", "commutative"):
                modifier = self.next().value
                if modifier == "emission":
                    emission = True
                    # `emission[db, log]`: the bracket is revl's existing
                    # parameterisation bracket (`List[Row]`, `Map[Str, Int]`),
                    # so this reads as "emission, parameterised by the
                    # boundaries it may cross". Bare `emission` stays "any".
                    if self.at("["):
                        capabilities = self._capability_list()
                elif modifier == "async":
                    async_ = True
                else:
                    method_commutative = True
            self.expect("kw", "fn")
            mname = self.expect("ident").value
            self.expect("(")
            params: list[tuple[str, str]] = []
            while not self.at(")"):
                pname = self.expect("ident").value
                self.expect(":")
                params.append((pname, self.type_()))
                if self.at(","):
                    self.next()
            self.expect(")")
            returns = None
            if self.at("arrow"):
                self.next()
                returns = self.type_()
            if mname in methods:
                raise self.err(mline, f"duplicate method `{mname}` in service {name}")
            methods[mname] = MethodDecl(
                mname, params, returns, emission, mline, async_=async_,
                commutative=method_commutative, capabilities=capabilities,
            )
        self.expect("}")
        return ServiceDecl(name, methods, line, commutative=commutative)

    def type_(self) -> str:
        return self._type_suffix(self.expect("ident", what="a type").value)

    def _type_suffix(self, base: str) -> str:
        """The `[...]` / `?` tail of a type, given its head.

        Split out of `type_` so `type X = List[Row]` can decide *after* reading
        the head that what follows is a type application rather than a variant
        case (a case is a bare name with an optional parenthesised payload, so
        `[` or `?` here is unambiguous)."""
        if self.at("["):
            self.next()
            inner = [self.type_()]
            while self.at(","):
                self.next()
                inner.append(self.type_())
            self.expect("]")
            rendered = f"{base}[{', '.join(inner)}]"
        else:
            rendered = base
        if self.at("?"):
            self.next()
            rendered = f"Opt[{rendered}]"  # T? sugar (syntax-2.0 §2)
        return rendered

    def component(self) -> ComponentDecl:
        line = self.expect("kw", "component").line
        name = self.expect("ident").value
        requires: list[tuple[str, str, int]] = []
        provides: list[tuple[str, str, int]] = []
        while self.at("kw", "requires") or self.at("kw", "provides"):
            kw = self.next().value
            target = requires if kw == "requires" else provides
            while True:
                bline = self.peek().line
                local = self.expect("ident").value
                self.expect(":")
                svc = self.expect("ident").value
                target.append((local, svc, bline))
                # a comma continues the same clause; `requires`/`provides`/`{` end it
                if self.at(","):
                    self.next()
                else:
                    break
        self.expect("{")
        config: list[ConfigField] = []
        body: list = []
        while not self.at("}"):
            if self.at("kw", "config"):
                if config:
                    raise self.err(self.peek().line, f"duplicate `config` block in component {name}")
                config = self.config_block()
            else:
                body.append(self.stmt(in_method=False))
        self.expect("}")
        return ComponentDecl(name, config, requires, provides, body, line)

    def config_block(self) -> list[ConfigField]:
        self.expect("kw", "config")
        self.expect("{")
        fields: list[ConfigField] = []
        while not self.at("}"):
            fline = self.peek().line
            fname = self.expect("ident").value
            self.expect(":")
            ftype = self.type_()
            default = None
            if self.at("="):
                self.next()
                default = self.literal()
            fields.append(ConfigField(fname, ftype, default, fline))
            if self.at(","):
                self.next()
        self.expect("}")
        return fields

    def literal(self):
        tok = self.peek()
        if tok.kind == "-" and self.toks[self.pos + 1].kind == "int":
            self.next()
            return -self.next().value
        if tok.kind == "int":
            return self.next().value
        if tok.kind == "string":
            return self.next().value
        if tok.kind == "template":
            raise self.err(tok.line, "config defaults cannot interpolate")
        if tok.kind == "kw" and tok.value in ("true", "false", "null"):
            self.next()
            return {"true": True, "false": False, "null": None}[tok.value]
        raise self.err(tok.line, f"expected a literal, found {tok.value!r}")

    def stmt(self, in_method: bool, in_async_method: bool = False):
        tok = self.peek()
        # `y = expr` — assignment to a `var` bound earlier in this method
        if in_method and tok.kind == "ident" and self.toks[self.pos + 1].kind == "=":
            self.next()
            self.next()
            return AssignStmt(tok.value, self.pure_expr(), tok.line)
        if tok.kind == "kw" and tok.value in ("let", "var"):
            mutable = tok.value == "var"
            self.next()
            bind = self.expect("ident").value
            self.expect("=")
            # `let x = effect … undo …` binds an acquisition; anything else
            # binds a plain value, so a method can name an intermediate
            # result instead of nesting every call into one expression
            if not mutable and self.at("kw", "effect"):
                acquire, undo, line, setup = self.effect_form(tok.line)
                return LetEffect(bind, acquire, undo, line, setup)
            if not in_method:
                raise self.err(
                    tok.line,
                    f"`{'var' if mutable else 'let'} {bind} = …` binds a plain value, "
                    "which a component activation body has no use for",
                    hint="an activation body records effects: write "
                         f"`let {bind} = effect … undo …`, or move the computation "
                         "into a `fn` (G6)",
                )
            return LetStmt(bind, self.pure_expr(), mutable, tok.line)
        if tok.kind == "kw" and tok.value == "effect":
            acquire, undo, line, setup = self.effect_form(tok.line)
            return EffectStmt(acquire, undo, line, setup)
        if tok.kind == "kw" and tok.value == "fail":
            if in_method:
                raise self.err(
                    tok.line,
                    "`fail` is only allowed in a component activation body (A8)",
                    hint="provide-method bodies run while the component is ACTIVE; "
                         "deliberate L-Raise is an activation-time transition",
                )
            self.next()
            return FailStmt(self.pure_expr(), tok.line)
        if tok.kind == "kw" and tok.value == "if":
            if in_method:
                raise self.err(
                    tok.line,
                    "`if` guards are only allowed in a component activation body",
                    hint="provide-method bodies run while the component is ACTIVE; "
                         "use a pure `if` expression in the method value instead (G6)",
                )
            return self.component_if()
        if tok.kind == "kw" and tok.value == "emit":
            self.next()
            expr = self.pure_expr()
            compensate = None
            if self.at("kw", "compensate"):
                self.next()
                compensate = self.pure_expr()
            return EmitStmt(expr, tok.line, compensate)
        if tok.kind == "kw" and tok.value == "await":
            if in_method and not in_async_method:
                raise self.err(
                    tok.line,
                    "`await` is only allowed in a component body",
                    hint="a provide method runs while the component is ACTIVE; iteration "
                         "boundaries (paper §4.3.2) exist only during activation (A1). "
                         "Declare the operation `async fn` to `await` a host async value in "
                         "a provide method (services 2.0, §5)",
                )
            self.next()
            return AwaitStmt(self.pure_expr(), tok.line)
        if tok.kind == "kw" and tok.value == "return":
            if not in_method:
                raise self.err(tok.line, "`return` is only allowed inside a provide method body")
            self.next()
            # a void operation returns nothing: `fn f(x) { return }`
            if self.at("}"):
                return ReturnStmt(None, tok.line)
            return ReturnStmt(self.pure_expr(), tok.line)
        if tok.kind == "kw" and tok.value == "isolate":
            if in_method:
                raise self.err(tok.line, "`isolate` is not allowed inside a method body")
            self.next()
            key = self.expect("ident").value
            self.expect("kw", "in")
            return IsolateStmt(key, self.realm_label(), tok.line)
        if tok.kind == "kw" and tok.value == "intercept":
            if in_method:
                raise self.err(tok.line, "`intercept` is not allowed inside a method body")
            self.next()
            key = self.expect("ident").value
            self.expect("kw", "with")
            return InterceptStmt(key, self.record_literal(), tok.line)
        if tok.kind == "kw" and tok.value == "provide":
            if in_method:
                raise self.err(tok.line, "`provide` is not allowed inside a method body")
            return self.provide()
        raise self.err(
            tok.line,
            f"expected a statement (`let`, `effect`, `emit`, `fail`, `if`{', `return`' if in_method else ', `provide`'}), found {tok.value!r}",
            hint="revl bodies contain only effect forms — plain expressions have no effect to record (G6)",
        )

    def effect_form(self, line: int):
        self.expect("kw", "effect")
        setup: list = []
        if self.at("{"):
            self.next()
            stmts = []
            while not self.at("}"):
                if self.at("kw", "fail"):
                    raise self.err(
                        self.peek().line,
                        "`fail` is not allowed in an effect block setup (G6)",
                        hint="effect block bodies are stratum-1 pure code plus a final acquisition; "
                             "deliberate L-Raise is a component activation statement",
                    )
                stmts.append(self.fn_stmt())
            self.expect("}")
            if not stmts or not isinstance(stmts[-1], ExprStmt):
                raise self.err(
                    line,
                    "an effect block must end with the acquisition expression",
                    hint="the final expression is the acquired value; earlier statements are pure setup (G6)",
                )
            acquire = stmts[-1].expr
            setup = stmts[:-1]
        else:
            acquire = self.pure_expr()
        if not self.at("kw", "undo"):
            head = _describe_expr(acquire)
            raise self.err(
                line,
                f"effect has no `undo` and {head} is not pure",
                hint=f"write `effect {head}(...) undo <expr>`, or mark the call `emit` if it deliberately crosses the system boundary (G4)",
            )
        self.next()
        undo = self.pure_expr()
        return acquire, undo, line, setup

    def component_if(self) -> IfStmt:
        line = self.expect("kw", "if").line
        self.expect("(")
        cond = self.pure_expr()
        self.expect(")")
        if self.at("{"):
            then = self.component_guard_block()
        else:
            then = [self.stmt(in_method=False)]
        if not then:
            raise self.err(line, "component `if` guard cannot be empty",
                           hint="a guard exists to decide a deliberate L-Raise (A8)")
        otherwise = None
        if self.at("kw", "else"):
            self.next()
            if self.at("kw", "if"):
                otherwise = [self.component_if()]
            elif self.at("{"):
                otherwise = self.component_guard_block()
            else:
                otherwise = [self.stmt(in_method=False)]
            if not otherwise:
                raise self.err(line, "component `else` guard cannot be empty",
                               hint="a guard exists to decide a deliberate L-Raise (A8)")
        return IfStmt(cond, then, otherwise, line)

    def component_guard_block(self) -> list:
        self.expect("{")
        stmts = []
        while not self.at("}"):
            stmts.append(self.stmt(in_method=False))
        self.expect("}")
        return stmts

    def realm_label(self) -> str:
        """`realm("<label>")` — static string literals only (v2)."""
        line = self.expect("kw", "realm").line
        self.expect("(")
        tok = self.peek()
        if tok.kind != "string":
            raise self.err(
                line,
                "dynamic realm labels are not supported — a realm is a static string literal",
                hint="config is unknown at link and admission time, so the linker could "
                     "neither prove nor refute a collision between config-derived realms "
                     "(G2 would be unsound); dynamic realms await instance-parametric "
                     "components (docs/design-v2-realms.md)",
            )
        self.next()
        label = tok.value
        if not label:
            raise self.err(line, "a realm label cannot be empty")
        self.expect(")")
        return label

    def record_literal(self) -> dict:
        """`{ field: literal | [literal, ...], ... }` — static metadata (v2)."""
        self.expect("{")
        record: dict = {}
        while not self.at("}"):
            fline = self.peek().line
            field_name = self.expect("ident").value
            if field_name in record:
                raise self.err(fline, f"duplicate metadata field `{field_name}`")
            self.expect(":")
            if self.at("["):
                self.next()
                values = []
                while not self.at("]"):
                    values.append(self.literal())
                    if self.at(","):
                        self.next()
                self.expect("]")
                record[field_name] = values
            else:
                record[field_name] = self.literal()
            if self.at(","):
                self.next()
        self.expect("}")
        return record

    # -- v2.0: type & function declarations (syntax-2.0 §2–§3) ----------------

    def type_decl(self, public: bool = False) -> TypeDecl:
        line = self.expect("kw", "type").line
        name = self.expect("ident").value
        params: list[str] = []
        if self.at("["):
            self.next()
            while not self.at("]"):
                params.append(self.expect("ident").value)
                if self.at(","):
                    self.next()
            self.expect("]")
        self.expect("=")
        if self.at("{"):
            self.next()
            fields: list[RecordField] = []
            while not self.at("}"):
                fline = self.peek().line
                fname = self.expect("ident").value
                self.expect(":")
                ftype = self.type_()
                fields.append(RecordField(fname, ftype, fline))
                if self.at(","):
                    self.next()
            self.expect("}")
            return TypeDecl(name, params, fields, [], line, public)
        cases: list[VariantCase] = []
        while True:
            cline = self.peek().line
            cname = self.expect("ident").value
            if not cases and (self.at("[") or self.at("?")):
                # `type Rows = List[Row]` / `type MaybeRow = Row?`: a variant
                # case is a bare name with an optional parenthesised payload,
                # so a `[` or `?` here can only be a type application — this is
                # an alias right-hand side. It is carried as the sole case name
                # (a name no case could otherwise have) and recognised as an
                # alias in lowering, where the type table is known.
                rendered = self._type_suffix(cname)
                if self.at("|"):
                    raise self.err(
                        self.peek().line,
                        "revl has no union types — `|` separates the cases of a "
                        f"variant, and `{rendered}` is a type, not a case",
                        hint="declare the alternatives as named cases "
                             "(`type T = Hit(Row) | Missing`), or alias a single "
                             "type (`type Rows = List[Row]`) — syntax-2.0 §2",
                    )
                return TypeDecl(name, params, [],
                                [VariantCase(rendered, None, cline)], line, public)
            payload = None
            if self.at("("):
                self.next()
                payload = self.type_()
                self.expect(")")
            cases.append(VariantCase(cname, payload, cline))
            if self.at("|"):
                self.next()
            else:
                break
        return TypeDecl(name, params, [], cases, line, public)

    def fn_decl(self, public: bool, verified: bool = False) -> FnDecl:
        line = self.expect("kw", "fn").line
        name = self.expect("ident").value
        self.expect("(")
        params: list[FnParam] = []
        while not self.at(")"):
            pline = self.peek().line
            pname = self.expect("ident").value
            self.expect(":")
            ptype = self.type_()
            params.append(FnParam(pname, ptype, pline))
            if self.at(","):
                self.next()
        self.expect(")")
        returns = None
        if self.at("arrow"):
            self.next()
            returns = self.type_()
        self.expect("{")
        body = []
        while not self.at("}"):
            body.append(self.fn_stmt())
        self.expect("}")
        return FnDecl(name, params, returns, body, public, line, verified)

    def test_decl(self) -> TestDecl:
        line = self.expect("kw", "test").line
        tok = self.expect("string")
        name = tok.value
        if not name:
            raise self.err(tok.line, "a test name cannot be empty")
        self.expect("{")
        body = []
        while not self.at("}"):
            body.append(self.fn_stmt())
        self.expect("}")
        return TestDecl(name, body, line)

    def fn_stmt(self):
        tok = self.peek()
        if tok.kind == "kw" and tok.value == "fail":
            raise self.err(
                tok.line,
                "`fail` is only allowed in a component activation body (A8)",
                hint="pure functions and tests return `Result` values; deliberate L-Raise "
                     "is a component activation transition",
            )
        if tok.kind == "kw" and tok.value in ("let", "var"):
            mutable = tok.value == "var"
            self.next()
            if self.at("{"):
                pattern = self._record_pattern()
                self.expect("=")
                return LetPatternStmt(pattern, self.pure_expr(), mutable, tok.line)
            if self.at("["):
                pattern = self._list_pattern()
                self.expect("=")
                return LetPatternStmt(pattern, self.pure_expr(), mutable, tok.line)
            name = self.expect("ident").value
            self.expect("=")
            return LetStmt(name, self.pure_expr(), mutable, tok.line)
        if tok.kind == "kw" and tok.value == "return":
            self.next()
            value = None if self.at("}") else self.pure_expr()
            return ReturnStmt(value, tok.line)
        if tok.kind == "kw" and tok.value == "if":
            return self.if_stmt()
        if tok.kind == "kw" and tok.value == "while":
            return self.while_stmt()
        if tok.kind == "kw" and tok.value == "for":
            return self.for_stmt()
        if tok.kind == "kw" and tok.value == "assert":
            self.next()
            return AssertStmt(self.pure_expr(), tok.line)
        if tok.kind == "ident" and self._assign_ahead():
            self.next()
            op = "="
            if self.at("="):
                self.next()
            else:
                op = self.next().value + "="
                self.next()
            return AssignStmt(tok.value, self.pure_expr(), tok.line, op)
        return ExprStmt(self.pure_expr(), tok.line)

    def _assign_ahead(self) -> bool:
        """True when the token after the current identifier starts `=` or a
        compound assignment (`+=`, `-=`, `*=`, `/=`, `%=`)."""
        nxt = self.toks[self.pos + 1]
        if nxt.kind == "=":
            return True
        if nxt.kind in ("+", "-", "*", "/", "%"):
            return self.toks[self.pos + 2].kind == "="
        return False

    def _record_pattern(self) -> RecordPattern:
        line = self.expect("{").line
        fields: list[str] = []
        while not self.at("}"):
            fields.append(self.expect("ident").value)
            if self.at(","):
                self.next()
        self.expect("}")
        return RecordPattern(fields, line)

    def _list_pattern(self) -> ListPattern:
        line = self.expect("[").line
        binds: list[str] = []
        rest: str | None = None
        while not self.at("]"):
            if self._rest_pattern_ahead():
                self.next()
                self.next()
                self.next()
                rest = self.expect("ident").value
                break
            binds.append(self.expect("ident").value)
            if self.at(","):
                self.next()
        self.expect("]")
        return ListPattern(binds, rest, line)

    def _rest_pattern_ahead(self) -> bool:
        return (
            self.at(".")
            and self.toks[self.pos + 1].kind == "."
            and self.toks[self.pos + 2].kind == "."
        )

    def while_stmt(self) -> WhileStmt:
        line = self.expect("kw", "while").line
        self.expect("(")
        cond = self.pure_expr()
        self.expect(")")
        body = self.block() if self.at("{") else [self.fn_stmt()]
        return WhileStmt(cond, body, line)

    def for_stmt(self) -> ForStmt:
        line = self.expect("kw", "for").line
        self.expect("(")
        bind = self.expect("ident").value
        self.expect("kw", "of")
        iterable = self.pure_expr()
        self.expect(")")
        body = self.block() if self.at("{") else [self.fn_stmt()]
        return ForStmt(bind, iterable, body, line)

    def if_stmt(self) -> IfStmt:
        line = self.expect("kw", "if").line
        self.expect("(")
        cond = self.pure_expr()
        self.expect(")")
        then = self.block() if self.at("{") else [self.fn_stmt()]
        otherwise = None
        if self.at("kw", "else"):
            self.next()
            if self.at("kw", "if"):
                otherwise = [self.if_stmt()]
            else:
                otherwise = self.block() if self.at("{") else [self.fn_stmt()]
        return IfStmt(cond, then, otherwise, line)

    def block(self) -> list:
        self.expect("{")
        stmts = []
        while not self.at("}"):
            stmts.append(self.fn_stmt())
        self.expect("}")
        return stmts

    # pure expressions — precedence climbing (§3.2)

    def _parse_template_parts(self, raw_parts, line: int):
        """Turn lexer template parts into ("text", str) / ("expr", ast): each
        `${...}` body is re-parsed as a full pure expression (§3.2)."""
        parts = []
        for kind, value in raw_parts:
            if kind == "text":
                parts.append(("text", value))
                continue
            sub = Parser(value, self.filename)
            expr = sub.pure_expr()
            if not sub.at("eof"):
                extra = sub.peek()
                raise self.err(line,
                               f"unexpected {extra.value!r} in `${{...}}` interpolation",
                               hint="an interpolation holds one expression")
            parts.append(("expr", expr))
        return parts

    def pure_expr(self):
        return self._ternary()

    def _ternary(self):
        cond = self._or()
        if self.at("?"):
            self.next()
            then = self._ternary()
            self.expect(":")
            otherwise = self._ternary()
            return ExprIf(cond, then, otherwise, cond.line)
        return cond

    def _or(self):
        return self._bin(self._nullish, ("||",))

    def _nullish(self):
        # `??` is a right-associative binary operator typed against Opt[T]
        # (typecheck._binop_type). TS forbids mixing `??` with `&&`/`||`
        # without parentheses; we enforce the same (§0: no silently different
        # meaning) via _reject_nullish_mix.
        left = self._and()
        if self.at("??"):
            line = self.next().line
            right = self._nullish()
            self._reject_nullish_mix("??", left, line)
            self._reject_nullish_mix("??", right, line)
            return ExprBin("??", left, right, left.line)
        return left

    @staticmethod
    def _is_unparen_bin(node, ops: tuple) -> bool:
        return (isinstance(node, ExprBin) and node.op in ops
                and not getattr(node, "_paren", False))

    def _reject_nullish_mix(self, op: str, operand, line: int) -> None:
        """`??` cannot be adjacent to `&&`/`||` without parentheses, and vice
        versa (matches TS, which makes the unparenthesized form a syntax
        error)."""
        if op == "??" and self._is_unparen_bin(operand, ("&&", "||")):
            raise self.err(
                line,
                f"`??` cannot be mixed with `{operand.op}` without parentheses",
                hint=f"write `(a {operand.op} b) ?? c` or `a ?? (b {operand.op} c)` "
                     "to say which you mean",
            )
        if op in ("&&", "||") and self._is_unparen_bin(operand, ("??",)):
            raise self.err(
                line,
                f"`{op}` cannot be mixed with `??` without parentheses",
                hint=f"write `(a ?? b) {op} c` or `a {op} (b ?? c)` to say which you mean",
            )

    def _and(self):
        return self._bin(self._eq, ("&&",))

    def _eq(self):
        return self._bin(self._cmp, ("==", "===", "!=", "!=="))

    def _cmp(self):
        return self._bin(self._add, ("<", ">", "<=", ">="))

    def _add(self):
        return self._bin(self._mul, ("+", "-"))

    def _mul(self):
        return self._bin(self._unary, ("*", "/", "%"))

    def _bin(self, operand, ops):
        left = operand()
        while True:
            op = None
            for candidate in ops:
                if self.at(candidate):
                    op = candidate
                    break
            if op is None:
                return left
            op_line = self.next().line
            right = operand()
            canonical = _CANONICAL_OPS.get(op, op)
            if canonical in ("&&", "||"):
                self._reject_nullish_mix(canonical, left, op_line)
                self._reject_nullish_mix(canonical, right, op_line)
            # `===`/`!==` are accepted spellings of the ONE structural
            # equality (syntax-2.0 §3.2): canonicalized here so the IR
            # carries a single operator and no backend can diverge
            left = ExprBin(canonical, left, right, left.line)

    def _unary(self):
        tok = self.peek()
        if tok.kind in ("!", "-"):
            self.next()
            return ExprUn(tok.kind, self._unary(), tok.line)
        if tok.kind == "kw" and tok.value == "emit":
            self.next()
            return EmitExpr(self._unary(), tok.line)
        return self._postfix()

    def _postfix(self):
        node = self._primary()
        # Once an optional access (`?.`) is taken, only another `?.` may
        # follow: a plain `.field`/`(...)`/`[i]` applied to a possibly-None
        # result would not short-circuit (`a?.b.c` runs `.c` on None). We
        # reject that rather than emit wrong runtime behavior — chain with
        # `?.` (`a?.b?.c`) or unwrap the optional first.
        optional = False
        while True:
            if self.at("."):
                if optional:
                    raise self._optional_chain_error()
                self.next()
                node = ExprField(node, self.expect("ident").value, node.line)
            elif self.at("?."):
                # `expr?.name` / `expr?.name(args)`: short-circuit on Opt-None.
                # Modelled as an ExprOptField / ExprOptCall so lowering can
                # emit a conditional and typing can flow Opt into inner types.
                self.next()
                name = self.expect("ident").value
                if self.at("("):
                    self.next()
                    args = []
                    while not self.at(")"):
                        args.append(self.pure_expr())
                        if self.at(","):
                            self.next()
                    self.expect(")")
                    node = ExprOptCall(node, name, args, node.line)
                else:
                    node = ExprOptField(node, name, node.line)
                optional = True
            elif self.at("("):
                if optional:
                    raise self._optional_chain_error()
                self.next()
                args = []
                while not self.at(")"):
                    args.append(self.pure_expr())
                    if self.at(","):
                        self.next()
                self.expect(")")
                node = ExprCall(node, args, node.line)
            elif self.at("["):
                if optional:
                    raise self._optional_chain_error()
                self.next()
                index = self.pure_expr()
                self.expect("]")
                node = ExprIndex(node, index, node.line)
            else:
                break
        return node

    def _optional_chain_error(self) -> RevlError:
        tok = self.peek()
        return self.err(
            tok.line,
            "an optional access `?.` can only be followed by another `?.` — "
            "a plain `.field`, call, or index after `?.` would not short-circuit",
            hint="chain with `?.` (`a?.b?.c`), or unwrap the optional first with "
                 "`match` or `??` before the next access",
        )

    def _match_expr(self) -> ExprMatch:
        """`match <expr> { arm ("," arm)* [","] "}"` — syntax-2.0 §3.3."""
        line = self.expect("kw", "match").line
        scrutinee = self.pure_expr()
        self.expect("{")
        arms: list = []
        while not self.at("}"):
            pattern, bind = self._match_pattern()
            self.expect("=>")
            body = self.pure_expr()
            arms.append((pattern, bind, body))
            if self.at(","):
                self.next()
            else:
                break
        self.expect("}")
        return ExprMatch(scrutinee, arms, line)

    def _match_pattern(self):
        tok = self.peek()
        if tok.kind != "ident":
            raise self.err(tok.line, f"expected a match pattern (case name or `_`), found {tok.value!r}")
        self.next()
        if tok.value == "_":
            return "_", None
        if not self.at("("):
            return tok.value, None
        self.next()
        bind = self.expect("ident").value
        self.expect(")")
        return tok.value, bind

    def _primary(self):
        tok = self.peek()
        if tok.kind == "int":
            self.next()
            return ExprLit(tok.value, tok.line)
        if tok.kind == "string":
            self.next()
            return ExprLit(tok.value, tok.line)
        if tok.kind == "template":
            self.next()
            return Interp(self._parse_template_parts(tok.value, tok.line), tok.line)
        if tok.kind == "kw" and tok.value in ("true", "false", "null"):
            self.next()
            return ExprLit({"true": True, "false": False, "null": None}[tok.value], tok.line)
        if tok.kind == "ident":
            self.next()
            if self.at("=>"):
                self.next()
                return ExprArrow([tok.value], self.pure_expr(), tok.line)
            return ExprVar(tok.value, tok.line)
        if tok.kind == "kw" and tok.value == "config":
            # `config` is a keyword for the declaration block, but in pure
            # expression position it heads `config.<field>` access (component
            # effect blocks and guard conditions).
            self.next()
            return ExprVar(tok.value, tok.line)
        if tok.kind == "(":
            if self._arrow_params_ahead():
                self.next()
                params = []
                while not self.at(")"):
                    params.append(self.expect("ident").value)
                    if self.at(","):
                        self.next()
                self.expect(")")
                self.expect("=>")
                return ExprArrow(params, self.pure_expr(), tok.line)
            self.next()
            node = self.pure_expr()
            self.expect(")")
            # mark the group so `??`/`&&`/`||` mixing checks treat it as
            # explicitly parenthesized (e.g. `(a ?? b) || c` is allowed)
            try:
                node._paren = True
            except AttributeError:
                pass
            return node
        if tok.kind == "{":
            self.next()
            fields = []
            while not self.at("}"):
                fname = self.expect("ident").value
                self.expect(":")
                fexpr = self.pure_expr()
                fields.append((fname, fexpr))
                if self.at(","):
                    self.next()
            self.expect("}")
            return ExprRecord(fields, tok.line)
        if tok.kind == "[":
            self.next()
            items = []
            while not self.at("]"):
                items.append(self.pure_expr())
                if self.at(","):
                    self.next()
            self.expect("]")
            return ExprList(items, tok.line)
        if tok.kind == "kw" and tok.value == "match":
            return self._match_expr()
        raise self.err(tok.line, f"expected an expression, found {tok.value!r}")

    def _arrow_params_ahead(self) -> bool:
        # current token is '(' — is this `(a, b) => ...` rather than `(expr)`?
        i = self.pos
        if self.toks[i].kind != "(":
            return False
        i += 1
        if self.toks[i].kind == ")":
            i += 1
        else:
            while True:
                if self.toks[i].kind != "ident":
                    return False
                i += 1
                if self.toks[i].kind == ",":
                    i += 1
                    continue
                break
            if self.toks[i].kind != ")":
                return False
            i += 1
        return self.toks[i].kind == "=>"

    def provide(self) -> ProvideStmt:
        line = self.expect("kw", "provide").line
        key = self.expect("ident").value
        self.expect("{")
        methods: list[ProvideMethod] = []
        while not self.at("}"):
            mline = self.peek().line
            async_ = False
            if self.at("kw", "async"):
                self.next()
                async_ = True
            self.expect("kw", "fn")
            mname = self.expect("ident").value
            self.expect("(")
            params: list[str] = []
            param_types: list = []
            while not self.at(")"):
                params.append(self.expect("ident").value)
                # optional `: Type` annotation — models write these on
                # autopilot from the `fn` stratum; accept and (later) check
                # them against the service signature (A6)
                if self.at(":"):
                    self.next()
                    param_types.append(self.type_())
                else:
                    param_types.append(None)
                if self.at(","):
                    self.next()
            self.expect(")")
            # optional `-> T` return annotation — models write the full `fn`
            # signature; accept and check it against the service (A6)
            returns = None
            if self.at("arrow"):
                self.next()
                returns = self.type_()
            if self.at("="):
                self.next()
                body = [ReturnStmt(self.pure_expr(), mline)]
            else:
                self.expect("{")
                body = []
                while not self.at("}"):
                    body.append(self.stmt(in_method=True, in_async_method=async_))
                self.expect("}")
            methods.append(ProvideMethod(mname, params, body, mline, async_=async_,
                                         param_types=param_types, returns=returns))
        self.expect("}")
        return ProvideStmt(key, methods, line)

    def expr(self):
        tok = self.peek()
        if tok.kind == "int":
            self.next()
            base = Lit(tok.value, tok.line)
        elif tok.kind == "string":
            self.next()
            base = Lit(tok.value, tok.line)
        elif tok.kind == "template":
            self.next()
            base = Interp(self._parse_template_parts(tok.value, tok.line), tok.line)
        elif tok.kind == "kw" and tok.value in ("true", "false", "null"):
            self.next()
            base = Lit({"true": True, "false": False, "null": None}[tok.value], tok.line)
        elif tok.kind == "ident" or (tok.kind == "kw" and tok.value == "config"):
            # `config` is a keyword for the declaration block, but in
            # expression position it heads `config.<field>` access
            self.next()
            base = Postfix(tok.value, [], tok.line)
        else:
            raise self.err(tok.line, f"expected an expression, found {tok.value!r}")

        while self.at("."):
            self.next()
            op_tok = self.expect("ident")
            args = None
            if self.at("("):
                self.next()
                args = []
                while not self.at(")"):
                    args.append(self.expr())
                    if self.at(","):
                        self.next()
                self.expect(")")
            if not isinstance(base, Postfix):
                raise self.err(op_tok.line, "method calls on literals are not supported in v0")
            base.ops.append(PostfixOp(op_tok.value, args, op_tok.line))
        return base


def _describe_expr(expr) -> str:
    if isinstance(expr, Postfix):
        return "`" + ".".join([expr.head] + [op.name for op in expr.ops]) + "`"
    if isinstance(expr, Lit):
        return repr(expr.value)
    path = _dotted_path(expr)
    if path:
        return f"`{path}`"
    if isinstance(expr, ExprLit):
        return repr(expr.value)
    return "the expression"


def _dotted_path(expr) -> str | None:
    """Render ExprCall/ExprField/ExprVar chains as `a.b.c` for diagnostics."""
    if isinstance(expr, ExprCall):
        return _dotted_path(expr.callee)
    if isinstance(expr, ExprField):
        target = _dotted_path(expr.target)
        return f"{target}.{expr.name}" if target else None
    if isinstance(expr, ExprVar):
        return expr.name
    return None


def parse_file(path: str) -> Program:
    import os

    with open(path, encoding="utf-8") as handle:
        program = Parser(handle.read(), path).parse()
    # provenance is recorded relative to the invocation cwd so IR documents
    # stay machine-independent when compiled from the project root
    source = os.path.relpath(path)
    for component in program.components:
        component.source = source
    for decl in (*program.fn_decls, *program.externs, *program.services):
        program.decl_files[id(decl)] = source
    return program
