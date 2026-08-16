"""Recursive-descent parser for revl v0.

Grammar (v0 subset — see DESIGN.md §3):

    program    := (service | component)*
    service    := 'service' IDENT '{' methoddecl* '}'
    methoddecl := ['emission'] 'fn' IDENT '(' [tparam (',' tparam)*] ')' ['->' type]
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


@dataclass
class ServiceDecl:
    name: str
    methods: dict[str, MethodDecl]
    line: int


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


@dataclass
class EffectStmt:
    acquire: object
    undo: object
    line: int


@dataclass
class EmitStmt:
    expr: object
    line: int
    compensate: object | None = None


@dataclass
class AwaitStmt:
    expr: object
    line: int


@dataclass
class ReturnStmt:
    expr: object
    line: int


@dataclass
class ProvideMethod:
    name: str
    params: list[str]
    body: list
    line: int


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


@dataclass
class Program:
    filename: str
    services: list[ServiceDecl] = field(default_factory=list)
    components: list[ComponentDecl] = field(default_factory=list)


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
            if self.at("kw", "service"):
                program.services.append(self.service())
            elif self.at("kw", "component"):
                program.components.append(self.component())
            else:
                tok = self.peek()
                raise self.err(tok.line, f"expected `service` or `component`, found {tok.value!r}")
        return program

    def service(self) -> ServiceDecl:
        line = self.expect("kw", "service").line
        name = self.expect("ident").value
        self.expect("{")
        methods: dict[str, MethodDecl] = {}
        while not self.at("}"):
            emission = False
            mline = self.peek().line
            if self.at("kw", "emission"):
                self.next()
                emission = True
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
            methods[mname] = MethodDecl(mname, params, returns, emission, mline)
        self.expect("}")
        return ServiceDecl(name, methods, line)

    def type_(self) -> str:
        base = self.expect("ident", what="a type").value
        if self.at("["):
            self.next()
            inner = [self.type_()]
            while self.at(","):
                self.next()
                inner.append(self.type_())
            self.expect("]")
            return f"{base}[{', '.join(inner)}]"
        return base

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
        if tok.kind == "int":
            return self.next().value
        if tok.kind == "string":
            parts = self.next().value
            if any(kind == "var" for kind, _ in parts):
                raise self.err(tok.line, "config defaults cannot interpolate")
            return "".join(text for _, text in parts)
        if tok.kind == "kw" and tok.value in ("true", "false", "null"):
            self.next()
            return {"true": True, "false": False, "null": None}[tok.value]
        raise self.err(tok.line, f"expected a literal, found {tok.value!r}")

    def stmt(self, in_method: bool):
        tok = self.peek()
        if tok.kind == "kw" and tok.value == "let":
            self.next()
            bind = self.expect("ident").value
            self.expect("=")
            acquire, undo, line = self.effect_form(tok.line)
            return LetEffect(bind, acquire, undo, line)
        if tok.kind == "kw" and tok.value == "effect":
            acquire, undo, line = self.effect_form(tok.line)
            return EffectStmt(acquire, undo, line)
        if tok.kind == "kw" and tok.value == "emit":
            self.next()
            expr = self.expr()
            compensate = None
            if self.at("kw", "compensate"):
                self.next()
                compensate = self.expr()
            return EmitStmt(expr, tok.line, compensate)
        if tok.kind == "kw" and tok.value == "await":
            if in_method:
                raise self.err(
                    tok.line,
                    "`await` is only allowed in a component body",
                    hint="a provide method runs while the component is ACTIVE; iteration "
                         "boundaries (paper §4.3.2) exist only during activation (A1)",
                )
            self.next()
            return AwaitStmt(self.expr(), tok.line)
        if tok.kind == "kw" and tok.value == "return":
            if not in_method:
                raise self.err(tok.line, "`return` is only allowed inside a provide method body")
            self.next()
            return ReturnStmt(self.expr(), tok.line)
        if tok.kind == "kw" and tok.value == "provide":
            if in_method:
                raise self.err(tok.line, "`provide` is not allowed inside a method body")
            return self.provide()
        raise self.err(
            tok.line,
            f"expected a statement (`let`, `effect`, `emit`{', `return`' if in_method else ', `provide`'}), found {tok.value!r}",
            hint="revl bodies contain only effect forms — plain expressions have no effect to record (G6)",
        )

    def effect_form(self, line: int):
        self.expect("kw", "effect")
        acquire = self.expr()
        if not self.at("kw", "undo"):
            head = _describe_expr(acquire)
            raise self.err(
                line,
                f"effect has no `undo` and {head} is not pure",
                hint=f"write `effect {head}(...) undo <expr>`, or mark the call `emit` if it deliberately crosses the system boundary (G4)",
            )
        self.next()
        undo = self.expr()
        return acquire, undo, line

    def provide(self) -> ProvideStmt:
        line = self.expect("kw", "provide").line
        key = self.expect("ident").value
        self.expect("{")
        methods: list[ProvideMethod] = []
        while not self.at("}"):
            mline = self.peek().line
            self.expect("kw", "fn")
            mname = self.expect("ident").value
            self.expect("(")
            params: list[str] = []
            while not self.at(")"):
                params.append(self.expect("ident").value)
                if self.at(","):
                    self.next()
            self.expect(")")
            if self.at("="):
                self.next()
                body = [ReturnStmt(self.expr(), mline)]
            else:
                self.expect("{")
                body = []
                while not self.at("}"):
                    body.append(self.stmt(in_method=True))
                self.expect("}")
            methods.append(ProvideMethod(mname, params, body, mline))
        self.expect("}")
        return ProvideStmt(key, methods, line)

    def expr(self):
        tok = self.peek()
        if tok.kind == "int":
            self.next()
            base = Lit(tok.value, tok.line)
        elif tok.kind == "string":
            self.next()
            parts = tok.value
            if any(kind == "var" for kind, _ in parts):
                base = Interp(parts, tok.line)
            else:
                base = Lit("".join(text for _, text in parts), tok.line)
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
    return "the expression"


def parse_file(path: str) -> Program:
    with open(path, encoding="utf-8") as handle:
        return Parser(handle.read(), path).parse()
