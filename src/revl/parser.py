"""Recursive-descent parser for revl v0.

Grammar (v0 subset — see DESIGN.md §3):

    program    := (service | component)*
    service    := 'service' IDENT '{' methoddecl* '}'
    methoddecl := modifier* 'fn' IDENT '(' [tparam (',' tparam)*] ')' ['->' type]
    modifier   := 'emission' ['[' IDENT (',' IDENT)* ']'] | 'async' | 'commutative' | 'idempotent'
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
    # delivery semantics (docs/delivery-semantics.md, roadmap item 44): the
    # emission may be safely re-delivered — `f(f(x)) == f(x)` on the server.
    # Only an `emission` may claim it; it is the sibling of `commutative` (an
    # algebraic property of the operation) and the precondition for the
    # runtime's auto-retry right.
    idempotent: bool = False
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
    verified: bool = False


@dataclass
class EffectStmt:
    acquire: object
    undo: object
    line: int
    setup: list = field(default_factory=list)
    verified: bool = False


@dataclass
class TimerStmt:
    """`every <n><unit> { <emit>* }` / `after <n><unit> { <emit>* }` — a timer
    (docs/time-coeffect.md, roadmap item 57).

    A timer is a **revertible schedule**: acquiring it registers work with the
    clock coeffect, and its inverse is cancellation, so unloading the component
    (or its enclosing frame) provably cancels its timers — no orphaned interval
    outlives the activation that armed it (the residue probe, item 18, would
    otherwise catch the leak). The body runs at activation-time stratum with
    the component's declared capabilities, so a firing cannot smuggle an
    emission past G4/G8; its reach is audited like any other component reach.

    `mode` is `"every"` (periodic) or `"after"` (one-shot delay); `interval_ms`
    is the resolved delay in milliseconds; `body` is the list of `EmitStmt`s
    the timer runs on each firing."""
    mode: str
    interval_ms: int
    body: list
    line: int


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
class SpawnExpr:
    """`spawn <Component> [with { <field>: <expr>, ... }]` — instantiate a
    component at runtime (docs/design-v2-instances.md). Only legal as the
    acquisition of an effect binding: `let s = effect spawn C with {..} undo
    s.dispose()`. The instance is an acquisition whose inverse is its own
    teardown."""
    component: str
    config: dict  # field name -> expression AST (lowered in the spawner's scope)
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
class HandoffStmt:
    """`handoff <key>: <Type>` — the verified state hand-off of a stateful
    provider (roadmap item 53, the `code_change` gap). `key` is a provided key
    of this component; `state_type` is the declared shape of the provider's
    live state (its effect-created world — a cache's Map, a session store's
    entries). When this component is *replaced*, that state is the value it
    **exports**; when it is the *replacement*, that type is the shape it
    **accepts**. The admission gate proves the two sides are §5-compatible
    before a swap threads the value across — a stateful provider starts warm,
    not cold. See docs/state-handoff.md."""
    key: str
    state_type: str
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
    # explicit type-parameter names from an `extern pure fn id[T](...)` list.
    # externs share the fn signature table, so the same machinery covers them.
    type_params: list[str] = field(default_factory=list)
    # `async` modifier between the classification and `fn` (roadmap item 80,
    # docs/design/async-extern.md §1). Name matches `ServiceMethod.async_`
    # (parser.py:42) and `ProvideMethod.async_` (parser.py:186). Validity
    # (emission-only, no compensate) is checked in lower, not the parser.
    async_: bool = False


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
    source: str = ""  # provenance: the file this fn was parsed from
    # explicit type-parameter names from a `fn id[T](...)` list (roadmap item
    # 6). Empty for the implicit form. Fed into the same tparam machinery the
    # implicit single-uppercase heuristic uses; never reaches the IR.
    type_params: list[str] = field(default_factory=list)


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
class ExprRecordUpdate:
    """`{base | f1 = e1, f2 = e2}` — functional record update.

    Semantics (docs/records.md §2): evaluates `base`, then produces a *fresh*
    value of `base`'s record type with the named fields replaced; `base` is
    untouched, exactly like `Map.set`. The result's type is `base`'s type.
    """
    base: object
    updates: list  # [(field_name, expr)]
    line: int


@dataclass
class ExprBlockArm:
    """A match arm whose body is a statement block: `=> { let x = e; expr }`.

    v1 subset (docs/records.md §4): the block may contain only `let`
    statements (single-assignment, pure) and must end in an expression, whose
    value is the arm's value. Parsed and typechecked; lowering is deferred —
    see docs/records.md §6.
    """
    stmts: list  # [LetStmt]
    tail: object
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
    # `(v: Int) => ...` — the author's parameter annotations, parallel to
    # `params` (None where written bare). The checker *overwrites* this with
    # the types it resolved (from an annotation or from the expected type) and
    # fills `returns`, so lowering can put a real signature in the IR; both
    # stay None/empty exactly when the arrow is still untyped.
    param_types: list = field(default_factory=list)
    returns: str | None = None


@dataclass
class ExprMatch:
    scrutinee: object
    arms: list  # [(pattern_name | "_", bind_name | None, body_expr)]
    line: int


@dataclass
class ExprHole:
    """`hole`, `hole "why"`, `hole[T]`, `hole[T] "why"` — docs/holes.md.

    A placeholder that *has a type* and no implementation. `type` is the
    author's `hole[T]` annotation, if written; `resolved` is the expected
    type the bidirectional checker supplied from context (declared return,
    service signature, parameter). Exactly one of the two must be known by
    the time the hole is lowered — a hole never guesses its own type.
    """
    message: str | None
    type: str | None
    line: int
    resolved: str | None = None

    @property
    def known_type(self) -> str | None:
        return self.type or self.resolved


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
    # `let g: (Int) -> Int = …` — an optional declared type. It is the
    # checking position for the right-hand side (and the only way to give an
    # un-annotated arrow a type without passing it somewhere).
    type: str | None = None


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


# --- v2.0 §7.1: lifecycle test statements ---------------------------------
# Stratum-3 statements, legal only inside a `lifecycle test` body. They are a
# script over a live composition, not pure code, so they are their own node
# set rather than an extension of the pure statement grammar.

@dataclass
class LoadStmt:
    component: str
    config: list[tuple[str, object, int]]   # (field, expr, line)
    line: int


@dataclass
class UnloadStmt:
    component: str
    line: int


@dataclass
class CallStmt:
    key: str
    method: str
    args: list
    bind: str | None
    line: int


@dataclass
class ResidueStmt:
    line: int


@dataclass
class TestDecl:
    name: str
    body: list
    line: int
    lifecycle: bool = False


@dataclass
class FaultTestDecl:
    """`fault test "name" for C { fail at step N  assert no residue }`.

    A declarable L-Raise (A8) experiment: activate `component` with a failure
    injected at one point of its activation body, then assert on what the
    paradigm promises about the wreckage.  See docs/fault-tests.md.
    """

    name: str
    component: str
    config: dict            # component config for the activation under test
    at_step: int | None     # 1-based index into the component's body steps
    at_effect: str | None   # `let effect NAME` binding (resolved to an index)
    asserts: list           # [(kind, line)] — kind from _FAULT_ASSERTS
    line: int


@dataclass
class PropTestDecl:
    """`prop test "name" (a: Int, b: Money) { assert … }` (roadmap item 37).

    A property test: the parameters are *generated inputs*, and the body is a
    pure assertion over them that must hold for every generated value.  The
    generators are DERIVED from the parameter types the checker already knows
    (records, ADTs — every constructor, `Opt`/`List` nesting, the i64 edge
    values); on failure the runner *shrinks* the input to a minimal
    counterexample.  See docs/prop-test.md.

    Precedent: `verified effect` (item 26) is the specific inverse-round-trip
    instance of this general form; `lifecycle`/`fault` set the pattern of a
    modifier/head changing a test's stratum without a new top-level keyword.
    """

    name: str
    params: list[FnParam]   # generated-input surface (name + declared type)
    body: list              # pure statements, at least one `assert`
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
    fault_tests: list[FaultTestDecl] = field(default_factory=list)
    prop_tests: list[PropTestDecl] = field(default_factory=list)
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
            hint = None
            if kind == "ident" and tok.kind == "kw":
                # repeated agent pain: a reserved word used where a NAME is
                # wanted reads as a parser confusion, not a naming mistake
                hint = (f"`{tok.value}` is a reserved keyword — it cannot "
                        "name a field, variable, parameter, or method; "
                        "pick another name")
            raise RevlError(self.filename, tok.line,
                            f"expected {wanted}, found {got}", hint)
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
            elif self.at("ident", "fault") and self.toks[self.pos + 1].kind == "kw" \
                    and self.toks[self.pos + 1].value == "test":
                # `fault` is a *contextual* keyword: it only heads a
                # declaration when immediately followed by `test`, so adding
                # this form cannot break a program that already uses `fault`
                # as an ordinary identifier (and the self-hosted lexer's
                # KEYWORDS table needs no sync).
                program.fault_tests.append(self.fault_test_decl())

            elif self.at("ident", "prop") and self.toks[self.pos + 1].kind == "kw" \
                    and self.toks[self.pos + 1].value == "test":
                # `prop` is a *contextual* keyword: like `fault`, it only heads a
                # declaration when immediately followed by `test`, so adding
                # `prop test` cannot break a program that already uses `prop` as
                # an ordinary identifier, and the self-hosted lexer's KEYWORDS
                # table needs no sync (roadmap item 37).
                program.prop_tests.append(self.prop_test_decl())

            elif self.at("ident", "lifecycle"):
                # contextual keyword: `lifecycle` is a modifier on `test`
                # (syntax-2.0 §7.1). It is deliberately NOT a lexer keyword —
                # the grammar delta is one token in one position, and the
                # self-hosted lexer's token stream stays unchanged.
                nxt = self.toks[self.pos + 1]
                if not (nxt.kind == "kw" and nxt.value == "test"):
                    raise self.err(
                        self.peek().line,
                        "`lifecycle` is a modifier on `test` — expected `test` after it",
                        hint='write `lifecycle test "name" { ... }` (syntax-2.0 §7.1)',
                    )
                self.next()
                program.tests.append(self.test_decl(lifecycle=True))
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
        # Optional `async` modifier between the classification and `fn`,
        # mirroring where service-op modifiers sit (parser.py:895-906). The
        # classification stays first and mandatory, so the "unclassified
        # extern" diagnostic above is untouched. Validity rules (emission-only,
        # no compensate) are enforced in lower (docs/design/async-extern.md §1).
        async_ = False
        if self.at("kw", "async"):
            self.next()
            async_ = True
        self.expect("kw", "fn")
        name = self.expect("ident").value
        type_params = self._type_param_list()
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
        return ExternDecl(name, classification, params, returns, undo, compensate, bodies, public, line,
                          type_params=type_params, async_=async_)

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
            method_idempotent = False
            mline = self.peek().line
            while self.at("kw") and self.peek().value in ("emission", "async", "commutative", "idempotent"):
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
                elif modifier == "commutative":
                    method_commutative = True
                else:
                    method_idempotent = True
            # `idempotent` is a delivery property, and only an emission is
            # delivered: a plain `fn` never crosses the boundary, so it has
            # nothing to re-deliver. Reject the claim rather than silently
            # dropping it (docs/delivery-semantics.md).
            if method_idempotent and not emission:
                raise self.err(
                    mline,
                    "`idempotent` describes how an emission is delivered, so it "
                    "is only meaningful on an `emission` operation",
                    hint="write `emission idempotent fn ...`; a plain `fn` is not "
                         "delivered, so there is nothing to re-deliver",
                )
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
                commutative=method_commutative, idempotent=method_idempotent,
                capabilities=capabilities,
            )
        self.expect("}")
        return ServiceDecl(name, methods, line, commutative=commutative)

    def type_(self) -> str:
        """A type. `(` heads either a function type or a grouped type.

        The function type is spelled `(Int, Str) -> Bool` — syntax-2.0 §2 keeps
        type syntax revl's own, and `->` is already revl's return arrow in
        `fn`, `extern` and service signatures, so a function type reads as the
        signature it is with the parameter names elided. (TS's `(a: number) =>
        boolean` is not adopted: it *requires* parameter names, so the "same
        meaning → same syntax" premise of §0 fails.) See docs/function-types.md.
        """
        if self.at("("):
            line = self.next().line
            inner: list[str] = []
            while not self.at(")"):
                inner.append(self.type_())
                if self.at(","):
                    self.next()
            self.expect(")")
            if self.at("arrow"):
                self.next()
                # the return type is parsed as a full type, so `(Int) -> Str?`
                # is `(Int) -> Opt[Str]` and a right-nested `(Int) -> (Str) ->
                # Bool` associates to the right, as in every ML-family language
                return f"({', '.join(inner)}) -> {self.type_()}"
            if len(inner) != 1:
                raise self.err(
                    line,
                    f"`({', '.join(inner)})` is not a type — revl has no tuples",
                    hint="a parenthesised type group holds exactly one type; "
                         "a function type needs a `-> ReturnType` "
                         "(docs/function-types.md)",
                )
            # a grouped type: `((Int) -> Bool)?` is how an *optional function*
            # is spelled, since a trailing `?` otherwise binds to the return
            return self._type_suffix_tail(inner[0])
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
        return self._type_suffix_tail(rendered)

    def _type_suffix_tail(self, rendered: str) -> str:
        """The `?` tail alone, given an already-rendered type."""
        if self.at("?"):
            self.next()
            rendered = f"Opt[{rendered}]"  # T? sugar (syntax-2.0 §2)
        return rendered

    def _provision_key(self, what: str = "a provision key") -> str:
        """A provision key, optionally namespace-qualified as `ns::key`
        (docs/namespacing.md).

        The `::` separator is two adjacent `:` tokens, so this is a pure
        parser addition — the lexer is untouched. An unqualified key returns
        its bare identifier verbatim, so v1 programs lower byte-for-byte as
        before; a qualified key returns the joined `ns::local` string, which
        is the key's wiring identity (G2 / injection resolution)."""
        first = self.expect("ident", what=what).value
        # `ns::local`: a `:` immediately followed by another `:`. A single
        # `:` here is the ordinary `key: Service` separator and is left alone.
        if self.at(":") and self.toks[self.pos + 1].kind == ":":
            self.next()  # first `:`
            self.next()  # second `:`
            local = self.expect("ident", what="a key after `::`").value
            return f"{first}::{local}"
        return first

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
                local = self._provision_key(what="a requirement or provision key")
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
        if tok.kind in ("int", "float"):
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
            declared = None
            if self.at(":"):
                self.next()
                declared = self.type_()
            self.expect("=")
            # `let x = effect … undo …` binds an acquisition; anything else
            # binds a plain value, so a method can name an intermediate
            # result instead of nesting every call into one expression. A
            # `verified` modifier (syntax-2.0 §7) marks the acquisition for
            # inverse round-trip testing (roadmap item 26).
            verified_effect = False
            if not mutable and self.at("kw", "verified"):
                self.next()
                if not self.at("kw", "effect"):
                    tok2 = self.peek()
                    raise self.err(tok2.line,
                                   f"expected `effect` after `verified`, found {tok2.value!r}",
                                   hint="`verified` marks an effect acquisition for inverse "
                                        "round-trip testing: `let x = verified effect … undo …`")
                verified_effect = True
            if not mutable and self.at("kw", "effect"):
                if declared is not None:
                    # an acquisition binds a *host-valued* object, which the
                    # checker's frontier documents as untyped; accepting an
                    # annotation here and then ignoring it would be a silent lie
                    raise self.err(
                        tok.line,
                        f"`let {bind}: {declared} = effect …` — an acquisition "
                        "binding cannot be annotated",
                        hint="`effect` binds a host-valued object, whose type "
                             "revl does not model (see the frontier in "
                             "src/revl/typecheck.py); drop the annotation",
                    )
                acquire, undo, line, setup = self.effect_form(tok.line)
                return LetEffect(bind, acquire, undo, line, setup, verified_effect)
            if verified_effect:
                tok2 = self.peek()
                raise self.err(tok2.line,
                               f"expected `effect` after `verified`, found {tok2.value!r}",
                               hint="`verified` marks an effect acquisition for inverse "
                                    "round-trip testing: `let x = verified effect … undo …`")
            if not in_method:
                raise self.err(
                    tok.line,
                    f"`{'var' if mutable else 'let'} {bind} = …` binds a plain value, "
                    "which a component activation body has no use for",
                    hint="an activation body records effects: write "
                         f"`let {bind} = effect … undo …`, or move the computation "
                         "into a `fn` (G6)",
                )
            return LetStmt(bind, self.pure_expr(), mutable, tok.line, declared)
        if tok.kind == "kw" and tok.value == "verified":
            # `verified effect … undo …` — the effect is marked for inverse
            # round-trip testing (syntax-2.0 §7, roadmap item 26). `verified`
            # heads a body statement only before `effect`; `verified fn` is a
            # top-level declaration, never a body statement.
            self.next()
            if not self.at("kw", "effect"):
                tok2 = self.peek()
                raise self.err(tok2.line,
                               f"expected `effect` after `verified`, found {tok2.value!r}",
                               hint="inside a body, `verified` marks an effect for inverse "
                                    "round-trip testing: `verified effect … undo …`")
            acquire, undo, line, setup = self.effect_form(tok.line)
            return EffectStmt(acquire, undo, line, setup, verified=True)
        if tok.kind == "kw" and tok.value == "effect":
            acquire, undo, line, setup = self.effect_form(tok.line)
            return EffectStmt(acquire, undo, line, setup)
        if tok.kind == "kw" and tok.value in ("every", "after"):
            if in_method:
                raise self.err(
                    tok.line,
                    f"`{tok.value}` timers are only allowed in a component activation body",
                    hint="a timer is a revertible schedule the activation frame owns; "
                         "arm it in the component body, not a provide method "
                         "(docs/time-coeffect.md)",
                )
            return self.timer()
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
            key = self._provision_key()
            self.expect("kw", "in")
            return IsolateStmt(key, self.realm_label(), tok.line)
        if tok.kind == "kw" and tok.value == "intercept":
            if in_method:
                raise self.err(tok.line, "`intercept` is not allowed inside a method body")
            self.next()
            key = self._provision_key()
            self.expect("kw", "with")
            return InterceptStmt(key, self.record_literal(), tok.line)
        if tok.kind == "kw" and tok.value == "handoff":
            if in_method:
                raise self.err(tok.line, "`handoff` is not allowed inside a method body")
            self.next()
            key = self._provision_key(what="a provided key")
            self.expect(":")
            state_type = self.type_()
            return HandoffStmt(key, state_type, tok.line)
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
        # instance-parametric components: `effect spawn C with {..} undo …`.
        # `spawn` is the one new acquisition form (docs/design-v2-instances.md);
        # its inverse is the instance's own teardown, so an `undo` is required
        # exactly as for any acquisition.
        if self.at("kw", "spawn"):
            acquire = self.spawn_expr()
            if not self.at("kw", "undo"):
                raise self.err(
                    line,
                    "a `spawn` acquisition needs `undo <handle>.dispose()`",
                    hint="an instance is an acquisition whose inverse is its own "
                         "teardown; write `let s = effect spawn "
                         f"{acquire.component} … undo s.dispose()` (G4)",
                )
            self.next()
            undo = self.pure_expr()
            return acquire, undo, line, []
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

    # duration units -> milliseconds. `ms` before `m` and `s` in the lexer's
    # eyes is moot — the unit is a whole ident token here, matched verbatim.
    _DURATION_UNITS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}

    def timer(self) -> "TimerStmt":
        """`every 30s { emit … }` / `after 5m { emit … }` (item 57).

        The delay is `<int><unit>` (e.g. `30s`, `5m`, `250ms`); the body is one
        or more `emit` statements that run at each firing. Kept to emissions in
        this first slice — a firing that acquires long-lived effects, guards, or
        nests further timers is a documented follow-on (docs/time-coeffect.md);
        the audited reach a timer needs is exactly its emissions."""
        kw = self.next()  # `every` | `after`
        mode = kw.value
        num = self.peek()
        if num.kind != "int":
            raise self.err(num.line,
                           f"expected a whole-number delay after `{mode}`, found {num.value!r}",
                           hint=f"a timer delay is `<n><unit>`, e.g. `{mode} 30s {{ … }}` "
                                "(units: ms, s, m, h, d)")
        if num.value <= 0:
            raise self.err(num.line,
                           f"a `{mode}` delay must be positive (found {num.value})",
                           hint="a zero or negative interval has no meaning for a schedule")
        self.next()
        unit_tok = self.peek()
        if unit_tok.kind != "ident" or unit_tok.value not in self._DURATION_UNITS:
            found = unit_tok.value if unit_tok.kind in ("ident", "kw") else repr(unit_tok.value)
            raise self.err(unit_tok.line,
                           f"expected a duration unit after `{mode} {num.value}`, found {found}",
                           hint="units are `ms`, `s`, `m`, `h`, `d` — write the delay with no "
                                f"space, e.g. `{mode} {num.value}s {{ … }}`")
        self.next()
        interval_ms = num.value * self._DURATION_UNITS[unit_tok.value]
        self.expect("{")
        body: list = []
        while not self.at("}"):
            inner = self.stmt(in_method=False)
            if not isinstance(inner, EmitStmt):
                raise self.err(
                    getattr(inner, "line", kw.line),
                    f"a `{mode}` timer body records emissions only",
                    hint="a timer firing runs at activation-time stratum with the "
                         "component's declared capabilities; its body is `emit` "
                         "statement(s). Richer bodies are a documented follow-on "
                         "(docs/time-coeffect.md)")
            if inner.compensate is not None:
                raise self.err(
                    inner.line,
                    "a timer-body `emit` cannot declare `compensate`",
                    hint="a periodic firing is not a one-shot acquisition, so there is "
                         "no single teardown to compensate; the timer's own "
                         "cancellation is its inverse (docs/time-coeffect.md)")
            body.append(inner)
        self.expect("}")
        if not body:
            raise self.err(kw.line,
                           f"an `{mode}` timer body is empty",
                           hint="a timer with no `emit` does nothing; drop it or give it work")
        return TimerStmt(mode, interval_ms, body, kw.line)

    def spawn_expr(self) -> "SpawnExpr":
        """`spawn <Component> [with { field: <expr>, ... }]`."""
        line = self.expect("kw", "spawn").line
        component = self.expect("ident").value
        config: dict = {}
        if self.at("kw", "with"):
            self.next()
            self.expect("{")
            while not self.at("}"):
                fline = self.peek().line
                field = self.expect("ident").value
                if field in config:
                    raise self.err(fline, f"duplicate config field `{field}` in spawn")
                self.expect(":")
                config[field] = self.pure_expr()
                if self.at(","):
                    self.next()
            self.expect("}")
        return SpawnExpr(component, config, line)

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
        if self.at("("):
            # `type Handler = (Int) -> Str`: a variant case is a bare name, so
            # a `(` here can only head a function type. Carried as the sole
            # case name exactly like `type Rows = List[Row]` below, and
            # recognised as a transparent alias in lowering.
            return TypeDecl(name, params, [],
                            [VariantCase(self.type_(), None, line)], line, public)
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

    def _type_param_list(self) -> list[str]:
        """An optional `[T, U]` type-parameter list after a `fn`/`extern` name
        (roadmap item 6). Returns [] when there is no `[`, so the implicit form
        `fn id(x: T)` is unaffected. Names are recorded on the declaration and
        become that function's type parameters in the checker (see
        docs/generics.md). Collision with a declared/builtin type is diagnosed
        later, in the checker, where the whole-program type table is known.

        provide-methods do not take this list: they are not entries in the
        shared fn/extern signature table, so `fn f[...]()` inside a `service`
        still fails at `[` as before."""
        if not self.at("["):
            return []
        bline = self.next().line
        names: list[str] = []
        while not self.at("]"):
            tok = self.expect("ident", what="a type-parameter name")
            if tok.value in names:
                raise self.err(tok.line, f"duplicate type parameter `{tok.value}`")
            names.append(tok.value)
            if self.at(","):
                self.next()
        self.expect("]")
        if not names:
            raise self.err(bline, "an empty type-parameter list `[]` is not allowed",
                           hint="drop the brackets for a non-generic fn, or name "
                                "at least one parameter: `fn id[T](x: T) -> T`")
        return names

    def fn_decl(self, public: bool, verified: bool = False) -> FnDecl:
        line = self.expect("kw", "fn").line
        name = self.expect("ident").value
        type_params = self._type_param_list()
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
        return FnDecl(name, params, returns, body, public, line, verified,
                      source=self.filename, type_params=type_params)

    def test_decl(self, lifecycle: bool = False) -> TestDecl:
        line = self.expect("kw", "test").line
        tok = self.expect("string")
        name = tok.value
        if not name:
            raise self.err(tok.line, "a test name cannot be empty")
        self.expect("{")
        body = []
        while not self.at("}"):
            if lifecycle:
                body.append(self.lifecycle_stmt())
            else:
                self._reject_lifecycle_stmt_here()
                body.append(self.fn_stmt())
        self.expect("}")
        return TestDecl(name, body, line, lifecycle)

    # -- v2.0 §7.1: lifecycle test bodies -----------------------------------

    # `load` / `unload` / `call` are *contextual* statement keywords: they are
    # ordinary identifiers everywhere else in the language, and only a
    # `lifecycle test` body reads them as statements.
    _LIFECYCLE_STMT_WORDS = ("load", "unload", "call")

    def _reject_lifecycle_stmt_here(self) -> None:
        """A lifecycle statement inside a plain `test` (or any pure body) is
        refused by name rather than by a confusing expression-parse error."""
        tok = self.peek()
        nxt = self.toks[self.pos + 1]
        word = None
        if tok.kind == "ident" and tok.value in self._LIFECYCLE_STMT_WORDS and nxt.kind == "ident":
            word = tok.value
        elif tok.kind == "kw" and tok.value == "assert" and nxt.kind == "ident" and nxt.value == "no_residue":
            word = "assert no_residue"
        if word is None:
            return
        raise self.err(
            tok.line,
            f"`{word}` is only allowed in a `lifecycle test` body",
            hint='a plain `test` block is pure (syntax-2.0 §7); write `lifecycle test "name" '
                 "{ ... }` to drive a composition (§7.1)",
        )

    def lifecycle_stmt(self):
        tok = self.peek()
        if tok.kind == "ident" and tok.value == "load":
            self.next()
            component = self.expect("ident", what="a component name").value
            config: list[tuple[str, object, int]] = []
            if self.at("kw", "with"):
                self.next()
                self.expect("{")
                while not self.at("}"):
                    field = self.expect("ident", what="a config field name")
                    self.expect(":")
                    config.append((field.value, self.pure_expr(), field.line))
                    if self.at(","):
                        self.next()
                self.expect("}")
            return LoadStmt(component, config, tok.line)
        if tok.kind == "ident" and tok.value == "unload":
            self.next()
            return UnloadStmt(self.expect("ident", what="a component name").value, tok.line)
        if tok.kind == "kw" and tok.value in ("let", "var"):
            if tok.value == "var":
                raise self.err(tok.line, "`var` has no meaning in a lifecycle test — bindings name "
                                         "the result of a `call` and are single-assignment",
                               hint="use `let`")
            self.next()
            bind = self.expect("ident").value
            self.expect("=")
            return self._call_stmt(bind)
        if tok.kind == "ident" and tok.value == "call":
            return self._call_stmt(None)
        if tok.kind == "kw" and tok.value == "assert":
            self.next()
            nxt = self.peek()
            if nxt.kind == "ident" and nxt.value == "no_residue":
                self.next()
                return ResidueStmt(tok.line)
            # anything else is a pure Bool expression over the test's `let`
            # bindings; an unbound bare word is caught in lowering, where the
            # binding scope is known, and reported as an unknown assertion
            return AssertStmt(self.pure_expr(), tok.line)
        if tok.kind == "ident" and tok.value == "swap":
            # `swap C -> C2` cannot exist: G2 forbids two components in one
            # document from providing the same key, so a replacement provider
            # for a key is not expressible (syntax-2.0 §7.1).
            raise self.err(
                tok.line,
                "there is no `swap` statement",
                hint="two components may not provide the same key in one document (G2), so a "
                     "replacement *provider* is not expressible; a replacement *instance* is "
                     "`unload C` then `load C with { ... }` (syntax-2.0 §7.1)",
            )
        raise self.err(
            tok.line,
            f"expected a lifecycle statement, found {tok.value!r}",
            hint="a lifecycle test body is `load` / `unload` / `call` / `let … = call …` / "
                 "`assert` (syntax-2.0 §7.1)",
        )

    def _call_stmt(self, bind: str | None) -> CallStmt:
        tok = self.peek()
        if not (tok.kind == "ident" and tok.value == "call"):
            raise self.err(tok.line, f"expected `call` after `let {bind} =`, found {tok.value!r}",
                           hint="a lifecycle binding names the result of a service call: "
                                "`let x = call key.op(args)`")
        self.next()
        key = self._provision_key()
        self.expect(".")
        method = self.expect("ident", what="an operation name").value
        self.expect("(")
        args = []
        while not self.at(")"):
            args.append(self.pure_expr())
            if self.at(","):
                self.next()
        self.expect(")")
        return CallStmt(key, method, args, bind, tok.line)

    # -- fault tests (docs/fault-tests.md) ---------------------------------

    def fault_test_decl(self) -> FaultTestDecl:
        """fault test STR for IDENT [with { k: lit, … }] { fail at …  assert … }

        Everything after `fault test` is contextual: `for` and `with` are
        existing keywords; `at`, `step`, `no`, `residue`, `emissions`,
        `inverses`, `lifo`, `failed`, `siblings` and `unaffected` are plain
        identifiers matched by spelling, so no new reserved word lands in the
        language.
        """
        line = self.next().line              # `fault`
        self.expect("kw", "test")
        name_tok = self.expect("string", what="a fault-test name")
        name = name_tok.value
        if not name:
            raise self.err(name_tok.line, "a fault test name cannot be empty")
        self.expect("kw", "for", what="`for <component>` after the fault test name")
        component = self.expect("ident", what="a component name").value

        config: dict = {}
        if self.at("kw", "with"):
            self.next()
            self.expect("{")
            while not self.at("}"):
                fline = self.peek().line
                key = self.expect("ident", what="a config field name").value
                if key in config:
                    raise self.err(fline, f"duplicate config field `{key}` in fault test `{name}`")
                self.expect(":")
                config[key] = self.literal()
                if self.at(","):
                    self.next()
            self.expect("}")

        self.expect("{")
        at_step: int | None = None
        at_effect: str | None = None
        asserts: list = []
        while not self.at("}"):
            tok = self.peek()
            if tok.kind == "kw" and tok.value == "fail":
                if at_step is not None or at_effect is not None:
                    raise self.err(tok.line,
                                   f"fault test `{name}` already has an injection point")
                self.next()
                self._expect_word("at", "`at` after `fail`")
                at_step, at_effect = self._fault_injection_point()
            elif tok.kind == "kw" and tok.value == "assert":
                self.next()
                asserts.append((self._fault_assertion(), tok.line))
            else:
                raise self.err(
                    tok.line,
                    f"expected `fail at …` or `assert …` in a fault test, found {tok.value!r}",
                )
        self.expect("}")
        if at_step is None and at_effect is None:
            raise self.err(line, f"fault test `{name}` has no `fail at …` injection point",
                           hint="a fault test must say where the activation dies, e.g. "
                                "`fail at step 2` or `fail at effect pool`")
        if not asserts:
            raise self.err(line, f"fault test `{name}` asserts nothing",
                           hint="add at least one of `assert failed`, `assert no residue`, "
                                "`assert inverses lifo`, `assert no emissions`, "
                                "`assert siblings unaffected`")
        return FaultTestDecl(name, component, config, at_step, at_effect, asserts, line)

    def _expect_word(self, word: str, what: str) -> None:
        """Consume a contextual keyword spelled as a bare identifier."""
        tok = self.peek()
        if tok.kind != "ident" or tok.value != word:
            got = repr(tok.value) if tok.value is not None else "end of file"
            raise self.err(tok.line, f"expected {what}, found {got}")
        self.next()

    def _fault_injection_point(self) -> tuple:
        tok = self.peek()
        if tok.kind == "ident" and tok.value == "step":
            self.next()
            index = self.expect("int", what="a 1-based body step index")
            if index.value < 1:
                raise self.err(index.line, "`fail at step` is 1-based; step 0 does not exist")
            return index.value, None
        if tok.kind == "kw" and tok.value == "effect":
            self.next()
            named = self.peek()
            if named.kind not in ("ident", "string"):
                raise self.err(named.line,
                               f"expected an effect binding name, found {named.value!r}")
            self.next()
            return None, named.value
        raise self.err(tok.line,
                       f"expected `step <n>` or `effect <name>` after `fail at`, found {tok.value!r}")

    def _fault_assertion(self) -> str:
        tok = self.peek()
        if tok.kind == "ident" and tok.value == "failed":
            self.next()
            return "failed"
        if tok.kind == "ident" and tok.value == "no":
            self.next()
            what = self.peek()
            if what.kind == "ident" and what.value in ("residue", "emissions"):
                self.next()
                return "no-residue" if what.value == "residue" else "no-emissions"
            raise self.err(what.line,
                           f"expected `residue` or `emissions` after `no`, found {what.value!r}")
        if tok.kind == "ident" and tok.value == "inverses":
            self.next()
            self._expect_word("lifo", "`lifo` after `inverses`")
            return "inverses-lifo"
        if tok.kind == "ident" and tok.value == "siblings":
            self.next()
            self._expect_word("unaffected", "`unaffected` after `siblings`")
            return "siblings-unaffected"
        raise self.err(
            tok.line,
            f"unknown fault-test assertion {tok.value!r}",
            hint="fault tests assert on the activation's wreckage, not on values: "
                 "`failed`, `no residue`, `inverses lifo`, `no emissions`, "
                 "`siblings unaffected`",
        )

    # -- property tests (docs/prop-test.md, roadmap item 37) ---------------

    def prop_test_decl(self) -> PropTestDecl:
        """prop test STR (name: Type, …) { assert … }

        `prop` and `test` are matched by spelling/keyword before this is
        entered; the parameter list is exactly a `fn` parameter list (each an
        input to be *generated* from its type), and the body is the same pure
        statement grammar a plain `test` body is — so `assert` reads as it does
        everywhere else, only checked over many generated inputs.
        """
        line = self.next().line              # `prop`
        self.expect("kw", "test")
        name_tok = self.expect("string", what="a prop-test name")
        name = name_tok.value
        if not name:
            raise self.err(name_tok.line, "a prop test name cannot be empty")
        self.expect("(", what="`(` — a prop test's parameters are its generated inputs")
        params: list[FnParam] = []
        seen: set[str] = set()
        while not self.at(")"):
            pline = self.peek().line
            pname = self.expect("ident", what="a parameter name").value
            if pname in seen:
                raise self.err(pline, f"duplicate parameter `{pname}` in prop test `{name}`")
            seen.add(pname)
            self.expect(":", what="`:` and a type — a prop-test parameter is a generated input")
            ptype = self.type_()
            params.append(FnParam(pname, ptype, pline))
            if self.at(","):
                self.next()
        self.expect(")")
        if not params:
            raise self.err(line, f"prop test `{name}` has no parameters",
                           hint="a prop test's parameters are its generated inputs: "
                                'write e.g. `prop test "commutes" (a: Int, b: Int) { … }`')
        self.expect("{")
        body = []
        while not self.at("}"):
            self._reject_lifecycle_stmt_here()
            body.append(self.fn_stmt())
        self.expect("}")
        if not any(isinstance(stmt, AssertStmt) for stmt in body):
            raise self.err(line, f"prop test `{name}` asserts nothing",
                           hint="a prop test states a property to hold for every generated "
                                "input: add at least one `assert <bool expr>`")
        return PropTestDecl(name, params, body, line)

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
            declared = None
            if self.at(":"):
                self.next()
                declared = self.type_()
            self.expect("=")
            return LetStmt(name, self.pure_expr(), mutable, tok.line, declared)
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

    def _hole_expr(self) -> ExprHole:
        """`hole` [`[` Type `]`] [StringLit] — docs/holes.md.

        The type rides in `[...]` rather than after a `:` for two reasons.
        `[]` is already revl's type-application bracket (`List[Row]`,
        §2), so `hole[Db]` reads as a type position on sight; and a `:`
        ascription would be genuinely ambiguous with the TS ternary the
        language admits verbatim (`c ? hole "x" : y` — §3.2). The message
        is a juxtaposed string literal, matching `test "name"`; nothing
        else in the grammar juxtaposes a string, so it cannot be misread.
        """
        line = self.expect("kw", "hole").line
        type_name = None
        if self.at("["):
            self.next()
            type_name = self.type_()
            self.expect("]")
        message = None
        if self.peek().kind == "string":
            message = self.next().value
        return ExprHole(message, type_name, line)

    def _match_expr(self) -> ExprMatch:
        """`match <expr> { arm ("," arm)* [","] "}"` — syntax-2.0 §3.3."""
        line = self.expect("kw", "match").line
        scrutinee = self.pure_expr()
        self.expect("{")
        arms: list = []
        while not self.at("}"):
            pattern, bind = self._match_pattern()
            self.expect("=>")
            if self.at("{") and self._block_arm_ahead():
                body: object = self._match_block_arm(self.peek().line)
            else:
                body = self.pure_expr()
            arms.append((pattern, bind, body))
            if self.at(","):
                self.next()
            else:
                break
        self.expect("}")
        return ExprMatch(scrutinee, arms, line)

    def _record_update_ahead(self) -> bool:
        """Current token is `{` — is this `{base | f = e, …}` (functional
        record update, docs/records.md §1) rather than a record literal?

        A record literal's top level can never contain a bare `|` (field
        values are bracket-balanced), so a depth-1 `|` before the matching
        `}` settles it."""
        i = self.pos + 1  # skip the `{`
        depth = 1
        while i < len(self.toks):
            kind = self.toks[i].kind
            if kind in ("{", "(", "["):
                depth += 1
            elif kind in ("}", ")", "]"):
                depth -= 1
                if depth == 0:
                    return False
            elif kind == "|" and depth == 1:
                return True
            elif kind == "eof":
                return False
            i += 1
        return False

    def _block_arm_ahead(self) -> bool:
        """Current token is `{` right after a match arm's `=>` — statement
        block (docs/records.md §4) or record literal?

        A block arm contains a statement keyword (`let`, `var`) at its top
        level; a record literal starts `ident :`. Either test settles it."""
        i = self.pos + 1  # skip the `{`
        depth = 1
        while i < len(self.toks):
            tok = self.toks[i]
            if tok.kind in ("{", "(", "["):
                depth += 1
            elif tok.kind in ("}", ")", "]"):
                depth -= 1
                if depth == 0:
                    return False
            elif depth == 1 and tok.kind == "kw" and tok.value in ("let", "var"):
                return True
            elif depth == 1 and tok.kind == "ident" and self.toks[i + 1].kind == ":":
                return False
            elif tok.kind == "eof":
                return False
            i += 1
        return False

    def _match_block_arm(self, line: int):
        """`{ let x = e; … ; expr }` — the v1 block-arm subset: only `let`
        statements, then exactly one trailing expression whose value is the
        arm's value."""
        self.expect("{")
        stmts = []
        while self.at("kw", "let") or self.at("kw", "var"):
            stmt = self.fn_stmt()
            if not isinstance(stmt, LetStmt):
                raise self.err(getattr(stmt, "line", line),
                               f"a match block arm may contain only `let` statements "
                               f"(docs/records.md §4), found `{stmt.__class__.__name__}`")
            if stmt.mutable:
                raise self.err(stmt.line,
                               "a match block arm may not declare `var` — arm blocks "
                               "are pure `let` sequences (docs/records.md §4)")
            stmts.append(stmt)
        tail = self.pure_expr()
        self.expect("}")
        return ExprBlockArm(stmts, tail, line)

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
        if tok.kind in ("int", "float"):
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
                return ExprArrow([tok.value], self.pure_expr(), tok.line, [None])
            return ExprVar(tok.value, tok.line)
        if tok.kind == "kw" and tok.value == "hole":
            return self._hole_expr()
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
                param_types: list = []
                while not self.at(")"):
                    params.append(self.expect("ident").value)
                    # `(v: Int) => ...` — an optional per-parameter annotation.
                    # It is what types an arrow that is *not* in checking
                    # position (docs/function-types.md).
                    if self.at(":"):
                        self.next()
                        param_types.append(self.type_())
                    else:
                        param_types.append(None)
                    if self.at(","):
                        self.next()
                self.expect(")")
                self.expect("=>")
                return ExprArrow(params, self.pure_expr(), tok.line, param_types)
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
            if self._record_update_ahead():
                self.next()
                base = self.pure_expr()
                self.expect("|")
                updates = []
                while not self.at("}"):
                    fname = self.expect("ident").value
                    self.expect("=")
                    updates.append((fname, self.pure_expr()))
                    if self.at(","):
                        self.next()
                self.expect("}")
                return ExprRecordUpdate(base, updates, tok.line)
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
        """Current token is `(` — is this `(a, b) => …` / `(a: Int) => …`
        rather than a parenthesised expression?

        A parameter annotation can be an arbitrarily nested type (`(f: (Int)
        -> List[Str]) => …`), so the list is skipped by balancing brackets
        rather than by token shape; what settles it is the `=>` after the
        closing paren, which can follow nothing else. The first two tokens are
        still shape-checked so that a malformed `(a + b) => c` reports as a bad
        expression rather than as a bad parameter list."""
        i = self.pos
        if self.toks[i].kind != "(":
            return False
        head = self.toks[i + 1]
        if head.kind != ")" and not (
            head.kind == "ident" and self.toks[i + 2].kind in (",", ")", ":")
        ):
            return False
        depth = 0
        while i < len(self.toks):
            kind = self.toks[i].kind
            if kind in ("(", "["):
                depth += 1
            elif kind in (")", "]"):
                depth -= 1
                if depth == 0:
                    return self.toks[i + 1].kind == "=>"
            elif kind == "eof":
                return False
            i += 1
        return False

    def provide(self) -> ProvideStmt:
        line = self.expect("kw", "provide").line
        key = self._provision_key()
        self.expect("{")
        methods: list[ProvideMethod] = []
        while not self.at("}"):
            mline = self.peek().line
            async_ = False
            if self.at("kw", "async"):
                self.next()
                async_ = True
            if self.at("kw", "emission"):
                # repeated agent pain (findings-uxprobe R1): purity modifiers
                # died as a bare parser error with no hint
                tok = self.peek()
                raise self.err(
                    tok.line,
                    f"expected fn, found {tok.value!r}",
                    hint="provider methods are plain `fn` — emission-ness is "
                         "inherited from the service declaration (G4 upper "
                         "bound); write `fn <name>(...)`")
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
        if tok.kind in ("int", "float"):
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
    for fn in program.fn_decls:
        fn.source = source
    return program
