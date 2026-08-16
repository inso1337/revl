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


# fn-body statements

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
class Program:
    filename: str
    services: list[ServiceDecl] = field(default_factory=list)
    components: list[ComponentDecl] = field(default_factory=list)
    type_decls: list[TypeDecl] = field(default_factory=list)
    fn_decls: list[FnDecl] = field(default_factory=list)


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
            elif self.at("kw", "type"):
                program.type_decls.append(self.type_decl())
            elif self.at("kw", "pub"):
                self.next()
                if self.at("kw", "fn"):
                    program.fn_decls.append(self.fn_decl(True))
                else:
                    tok = self.peek()
                    raise self.err(tok.line, f"expected `fn` after `pub`, found {tok.value!r}")
            elif self.at("kw", "fn"):
                program.fn_decls.append(self.fn_decl(False))
            else:
                tok = self.peek()
                raise self.err(tok.line, f"expected a top-level declaration, found {tok.value!r}")
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

    def realm_label(self) -> str:
        """`realm("<label>")` — static string literals only (v2)."""
        line = self.expect("kw", "realm").line
        self.expect("(")
        tok = self.peek()
        if tok.kind != "string" or any(kind == "var" for kind, _ in tok.value):
            raise self.err(
                line,
                "dynamic realm labels are not supported — a realm is a static string literal",
                hint="config is unknown at link and admission time, so the linker could "
                     "neither prove nor refute a collision between config-derived realms "
                     "(G2 would be unsound); dynamic realms await instance-parametric "
                     "components (docs/design-v2-realms.md)",
            )
        self.next()
        label = "".join(text for _, text in tok.value)
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

    def type_decl(self) -> TypeDecl:
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
            return TypeDecl(name, params, fields, [], line)
        cases: list[VariantCase] = []
        while True:
            cline = self.peek().line
            cname = self.expect("ident").value
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
        return TypeDecl(name, params, [], cases, line)

    def fn_decl(self, public: bool) -> FnDecl:
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
        return FnDecl(name, params, returns, body, public, line)

    def fn_stmt(self):
        tok = self.peek()
        if tok.kind == "kw" and tok.value == "let":
            self.next()
            name = self.expect("ident").value
            self.expect("=")
            return LetStmt(name, self.pure_expr(), False, tok.line)
        if tok.kind == "kw" and tok.value == "var":
            self.next()
            name = self.expect("ident").value
            self.expect("=")
            return LetStmt(name, self.pure_expr(), True, tok.line)
        if tok.kind == "kw" and tok.value == "return":
            self.next()
            value = None if self.at("}") else self.pure_expr()
            return ReturnStmt(value, tok.line)
        if tok.kind == "kw" and tok.value == "if":
            return self.if_stmt()
        if tok.kind == "ident" and self.toks[self.pos + 1].kind == "=":
            self.next()
            self.next()
            return AssignStmt(tok.value, self.pure_expr(), tok.line)
        return ExprStmt(self.pure_expr(), tok.line)

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
        return self._bin(self._and, ("||",))

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
            self.next()
            right = operand()
            left = ExprBin(op, left, right, left.line)

    def _unary(self):
        tok = self.peek()
        if tok.kind in ("!", "-"):
            self.next()
            return ExprUn(tok.kind, self._unary(), tok.line)
        return self._postfix()

    def _postfix(self):
        node = self._primary()
        while True:
            if self.at("."):
                self.next()
                node = ExprField(node, self.expect("ident").value, node.line)
            elif self.at("("):
                self.next()
                args = []
                while not self.at(")"):
                    args.append(self.pure_expr())
                    if self.at(","):
                        self.next()
                self.expect(")")
                node = ExprCall(node, args, node.line)
            elif self.at("["):
                self.next()
                index = self.pure_expr()
                self.expect("]")
                node = ExprIndex(node, index, node.line)
            else:
                break
        return node

    def _primary(self):
        tok = self.peek()
        if tok.kind == "int":
            self.next()
            return ExprLit(tok.value, tok.line)
        if tok.kind == "string":
            self.next()
            parts = tok.value
            if any(kind == "var" for kind, _ in parts):
                return Interp(parts, tok.line)
            return ExprLit("".join(text for _, text in parts), tok.line)
        if tok.kind == "kw" and tok.value in ("true", "false", "null"):
            self.next()
            return ExprLit({"true": True, "false": False, "null": None}[tok.value], tok.line)
        if tok.kind == "ident":
            self.next()
            if self.at("=>"):
                self.next()
                return ExprArrow([tok.value], self.pure_expr(), tok.line)
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
    import os

    with open(path, encoding="utf-8") as handle:
        program = Parser(handle.read(), path).parse()
    # provenance is recorded relative to the invocation cwd so IR documents
    # stay machine-independent when compiled from the project root
    source = os.path.relpath(path)
    for component in program.components:
        component.source = source
    return program
