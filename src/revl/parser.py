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
    # taint declassification slot (roadmap item 249, Slice C): the origins a
    # provider of this operation may `endorse[<origin>]` in its body. Declared on
    # the service operation because a provide method is a plain `fn` that inherits
    # its authority (emission-ness, and now declassification rights) from the
    # service declaration. Empty unless the operation declares an endorse slot.
    endorse_origins: frozenset = field(default_factory=frozenset)


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
    # item 131: the surface carried `effect await …` — the acquisition suspends
    # a fiber and its landed result (not the in-flight value) is bound. The
    # admission pass (lower.py) proves the acquire actually reaches a suspension
    # source; the emitters prefix the acquisition with the tier's await marker.
    is_async: bool = False


@dataclass
class EffectStmt:
    acquire: object
    undo: object
    line: int
    setup: list = field(default_factory=list)
    verified: bool = False
    # item 131: the surface carried `effect await …` (see LetEffect.is_async).
    is_async: bool = False


@dataclass
class LeaseAcquire:
    """The acquisition head of `effect lease <cap> [ttl <dur>] [uses <n>]` (item
    294 Slice 2, docs/design/294-parameterized-capabilities.md).

    Not an ordinary `acquire` expression: a lease acquisition is a class-(c)-
    GATED, ticket-mediated mint of a standing grant over `capability`'s cone,
    bounded by `ttl_ms` and/or `uses`. It rides the same `effect … undo …`
    stratum (the undo is `l.revoke()`, riding the LIFO teardown) but lowers to a
    reserved runtime acquisition, so it travels as this dedicated node rather
    than as a call the emitter would treat as a host body. `capability` is the
    canonical `(T, P)` spelling `cap_order` produces (a bare or parameterized
    token). At least one of `ttl_ms`/`uses` bounds the grant; an unbounded lease
    is refused at parse (consent stays bounded, the item-344 rule)."""

    capability: str
    ttl_ms: object            # int milliseconds, or None
    uses: object              # int count, or None
    line: int


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
    # item 246, Slice 2: the `with <e>` clause threading an `Approval[C]` value to
    # the crossing. `e` is a pure expression evaluating to a value of type
    # `Approval[C']`; the checker (lower._lower_provide) proves `C` is within
    # `C'`'s scope. None when the crossing carries no approval edge.
    approval: object | None = None
    # item 131: the surface carried `await emit …` — the emission crosses the
    # boundary through a suspending (async) operation and the step awaits it.
    # The boundary marker sits outermost (`await emit`, not `emit await`) because
    # the step is an iteration boundary whose payload is an emission (design §2).
    # Kept last so the positional `EmitStmt(expr, line, compensate, approval)`
    # construction is unchanged.
    is_async: bool = False


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
class ApprovalExpr:
    """`await approval[C] { field: expr, ... }` — the only producer of a value of
    type `Approval[C]` (item 246, Decision 3). It suspends until the operator
    grants or refuses; the fields are the human's evidence, rendered in the prompt
    and carried into the ledger and the WAL. `Approval[C]` has no constructor, so
    an approval cannot be forged in-language."""
    capability: str          # the capability token C (e.g. "payment")
    fields: object           # [(name, exprAST), ...] the human's evidence
    line: int


@dataclass
class LetApprovalStmt:
    """`let a = await approval[C] { fields }` — bind an `Approval[C]` value in a
    component activation body (item 246). The suspension is acquisition-shaped,
    so it lives beside `let x = effect …` rather than in the plain-value stratum
    the activation body otherwise forbids."""
    bind: str
    request: ApprovalExpr
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
class RouteStmt:
    """`isolate <key> in realms("w1","w2","w3") strategy(round_robin)` — the
    multi-realm bind (roadmap item 162). The plural of `isolate <key> in
    realm(<name>)`: one required key is bound across N named realms, and an
    optional `strategy` names how the runtime router distributes across them.
    This is the CONSUMPTION side of the load-balancer pattern (item 161's
    Router consumes it); it records the binding + strategy for the runtime
    router and does not itself route. `strategy` is `None` when omitted."""
    key: str
    realms: list[str]
    strategy: str | None
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
    # item 396 option A: provenance when this body was SPLICED from an external
    # host-code file (`= @backend file "path"`). Both stay `None` for an inline
    # `@backend { ... }` body, so a program that uses no file form lowers to a
    # byte-identical IR. Set by the compiler's body-file resolver, which reads
    # the file under the jail and replaces the HostBodyFile node below with a
    # resolved HostBody carrying the spliced (normalized) text plus these:
    #   source_path — the root-relative path as written, for `revl audit`
    #   sha256      — sha256 of the RAW file bytes read, so two compiles of the
    #                 same tree are byte-comparable and `revl verify` can re-hash
    source_path: str | None = None
    sha256: str | None = None


@dataclass
class HostBodyFile:
    """item 396 option A: an extern body that references an external host-code
    file, `= @backend file "path"`. The parser is IO-free and records only this
    node; the compiler layer (`_ModuleLoader`) resolves and reads the file under
    the jail right after parse, then REPLACES this node with a resolved
    `HostBody`, so nothing downstream (lower, emit, audit) ever sees this shape.
    """
    backend: str
    path: str   # the path string exactly as written, resolved by the compiler
    line: int


@dataclass
class HostRef:
    """item 396 option B: an extern body that IMPORTS a host symbol from an
    external host MODULE, `= @backend ref sym from "path"`. Unlike a
    `HostBodyFile` (option A, a compile-time text splice), this is NOT replaced:
    the referenced file is a normal host module and the emitter generates a LAZY
    import THUNK that imports the symbol at the extern's FIRST CALL (never at
    module top — a module-top import would run host code at artifact LOAD,
    outside every classification/approval/witness gate).

    The parser is IO-free and records the symbol and the written path. The
    compiler's ref resolver (`hostref.resolve_refs`) checks the file EXISTS under
    the ROOT-tree jail, pins a content hash of its bytes, and records the
    root-relative path (the emitted import specifier is derived from it). The
    node then flows THROUGH lower into the additive `refs` IR key, so an extern
    that uses no ref lowers to a byte-identical IR.
    """
    backend: str
    symbol: str        # the host identifier imported from the referenced module
    path: str          # the path string exactly as written
    line: int
    # set by the compiler's ref resolver (hostref.resolve_refs):
    rel_path: str | None = None   # resolved path RELATIVE TO THE ROOT tree
    sha256: str | None = None     # sha256 of the resolved file's raw bytes
    # item 410: the ROOT KIND this ref resolved against. `"stdlib"` when the
    # DECLARING module is install-origin (resolved through the item-319 search
    # path or realpath-contained in `stdlib_root()`), so the ref jails to the
    # install tree and the runner picks the install root at deploy. `None`
    # (absent in the IR) for a user-origin ref — every existing ref IR stays
    # byte-identical.
    root_kind: str | None = None


@dataclass
class ExternDecl:
    name: str
    classification: str  # 'pure' | 'acquire' | 'emission' | 'witnessed'
    params: list[FnParam]
    returns: str | None
    undo: object | None
    compensate: object | None
    # item 396: a body element is a resolved inline `HostBody`; before the
    # compiler's body-file resolver runs, an unresolved `HostBodyFile` (option A,
    # replaced by a `HostBody`); or a `HostRef` (option B), which flows through
    # unchanged into the `refs` IR key.
    bodies: list["HostBody | HostBodyFile | HostRef"]
    public: bool
    line: int
    # capability scope of a boundary-crossing extern (docs/design/243-witnessed-externs.md):
    # `witnessed[fs]` bounds *where* the reversible mutation crosses, exactly as
    # `emission[db]` bounds an emission. Empty for the unscoped/inapplicable
    # classifications. Read by the emission analysis (same authority namespace)
    # and carried onto the IR node so the audit surface names the boundary.
    capabilities: tuple[str, ...] = ()
    # explicit type-parameter names from an `extern pure fn id[T](...)` list.
    # externs share the fn signature table, so the same machinery covers them.
    type_params: list[str] = field(default_factory=list)
    # `async` modifier between the classification and `fn` (roadmap item 80,
    # docs/design/async-extern.md §1). Name matches `ServiceMethod.async_`
    # (parser.py:42) and `ProvideMethod.async_` (parser.py:186). Validity
    # (emission-only, no compensate) is checked in lower, not the parser.
    async_: bool = False
    # `deferred` modifier between the classification and `fn` (roadmap item 245,
    # docs/design/245-session-commit.md, Decision 2). Sits in the same modifier
    # slot as `async`: `extern emission[mail] deferred fn send(...)`. A deferred
    # emission does not fire at the call site — it enqueues onto the session's
    # deferral queue and returns Unit; the session commit flushes it (or an abort
    # drops it). Validity (emission-only, Unit-returning, no compensate, not
    # async) is checked in lower, not the parser.
    deferred: bool = False
    # item 388: the caller-decided colour marker, spelled `fn|async` in the
    # `async`/`deferred` slot (`extern emission fn|async engine_run(...)`). A poly
    # extern fixes NO colour at its declaration: one authored host body serves a
    # sync `def`/`function` clone at a sync call site and an `async def`/`async
    # function` clone at an async call site, monomorphized per call-site colour
    # (the EXTERN analog of item 342's arrow monomorphization,
    # docs/design/388-caller-decided-extern-colour.md, option a). Validity
    # (emission-only, not `deferred`, not combined with a fixed `async`, no
    # `compensate`, and the per-backend `await`-keyword lint) is checked in lower,
    # not the parser. `False` for every extern, so a non-poly extern is
    # byte-identical.
    colour_poly: bool = False
    # item 246: the declaration-owned `requires approval` clause, for first-party
    # code that knows its boundary is sensitive: `extern emission[production.payment]
    # fn charge(...) requires approval`. A crossing that reaches this extern must
    # carry a covering `with e` edge or admission refuses at lowering, holding even
    # with no policy file (Decision 3, floor-and-acknowledgment).
    requires_approval: bool = False
    # item 373: the reach clause `emission(confined: <param>)`, sibling to the
    # `[caps]` capability scope. Where `[caps]` names the boundary TOKEN the
    # crossing joins, `reach` names what the crossing is BOUNDED to — the
    # confinement a reviewer needs to see. `None` is a bare emission (no reach,
    # "unconfined"), which keeps the IR byte-identical. Otherwise a
    # `(kind, target)` pair: `kind` is `"confined"` (the only reach kind today)
    # and `target` names the PARAMETER carrying the confinement region. Only an
    # `emission` extern may carry it, and the target must name a real parameter
    # (both checked in lower) — so a weakened reach (confined -> unconfined) is a
    # reviewable diff, not a buried host-body comment.
    reach: tuple[str, str] | None = None
    # item 379 / option (b) of docs/design/378-sync-extern-service-reach.md: a
    # typed `config { field: T = default, ... }` block, the same shape a
    # `ComponentDecl` carries (parser.py:284), letting a document-global extern
    # read STATIC configuration resolved once at plug time instead of ambient
    # env vars. Empty for every extern that declares no `config` clause, so
    # their parse/IR/goldens stay byte-identical.
    config: list[ConfigField] = field(default_factory=list)


@dataclass
class FnParam:
    name: str
    type: str
    line: int
    # optional default value (roadmap item 187): `fn f(a: Int, b: Int = 0)`.
    # A pure expression, evaluated at the call site when the argument is
    # omitted (call-site resolution — the emitters never see a defaulted
    # parameter, only a fully-supplied argument list). `None` for a parameter
    # without a default. Only `fn_decl` parses defaults; extern (`=` opens a
    # host body) and prop-test (generated inputs) parameter lists do not.
    default: object | None = None


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
    # taint declassification slot (roadmap item 249, Slice C): the origins this
    # fn is DECLARED to be allowed to `endorse[<origin>]` in its body. An
    # `endorse[o]` whose origin is not in this set is refused at admission, so a
    # downgrade must appear in the enclosing declaration — never ambient. Empty
    # for a fn that declares no endorse slot (byte-identical to before).
    endorse_origins: frozenset = field(default_factory=frozenset)


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
class ExprEndorse:
    """`endorse[<origin>](<value>, reason = "...")` [`with <appr>`] — the scoped,
    reasoned taint declassifier (roadmap item 249, Slice C).

    Supersedes Slice A's ambient `endorse(v)`. Three properties, each on the node:
    `origin` names the coarse taint class being downgraded (and must appear in the
    enclosing declaration's endorse slot); `reason` is the mandatory audit string
    that lands in the boundary table's declassify record; `approval` is the name
    of an `Approval[declassify.<origin>]` value threaded through `with`, so an
    endorse under a `capability declassify.<origin> requires approval` policy rule
    is covered (item 246 surface). It is identity on its argument's base type and
    is spliced out of the IR after the taint verdict, so no emitter sees it."""
    origin: str
    expr: object
    reason: str
    line: int
    approval: str | None = None


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
    """A match arm whose body is a statement block: `=> { stmt* ; expr }`.

    The block accepts the same statement set a normal fn/block body accepts
    (`let`, `var`, `while`, `if`, `for`, assignments, ...) and ends in an
    expression whose value is the arm's value (docs/records.md §4). Lowering
    lambda-lifts the block into a synthetic helper fn, so the imperative
    statements run where the value is destructured and the IR arm body stays an
    expression (a call). `return` is not a block-arm statement — the arm yields
    its final expression, not an early return from the enclosing function.
    """
    stmts: list  # block statements preceding the tail (any fn-body statement)
    tail: object
    line: int


@dataclass
class ExprList:
    items: list
    line: int


# ------------------------------------------------------ item 383: list transforms
# The receiver-first functional transforms `xs.map(f)` / `xs.filter(p)` /
# `xs.reduce(init, f)` are PURE SUGAR for the generic free functions
# `list_map` / `list_filter` / `list_reduce` in stdlib/list.rvl — the same
# desugar-to-a-plain-CALL shape as item 189's Value dot-accessors, except the
# free function is generic and takes a function-value argument (items 92/342).
#
# The redirect happens BEFORE typing and lowering (`desugar_list_transform` is
# called from typecheck.py and lower.py at the method-call site), so the
# existing generic-call + arrow-argument inference does all the work. This is
# deliberately NOT a `_BUILTIN_SIG` row: no builtin-method signature can express
# `map`'s result `List[<f's return>]` (the return type must be solved from the
# arrow), so the sugar is a syntactic redirect to a real generic `fn`, not a
# builtin. Receiver-first: the receiver becomes the leading argument, preserving
# the written argument order (`xs.reduce(init, f)` -> `list_reduce(xs, init, f)`).
LIST_TRANSFORMS = {
    "map": "list_map",
    "filter": "list_filter",
    "reduce": "list_reduce",
}


def desugar_list_transform(expr):
    """If `expr` is a `recv.map(...)` / `.filter(...)` / `.reduce(...)` call,
    return the equivalent `list_map(recv, ...)` free-function ExprCall; else
    return None. The receiver is threaded in as the leading argument."""
    callee = expr.callee
    if not isinstance(callee, ExprField):
        return None
    free = LIST_TRANSFORMS.get(callee.name)
    if free is None:
        return None
    return ExprCall(ExprVar(free, callee.line),
                    [callee.target, *expr.args], expr.line)


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


@dataclass
class BreakStmt:
    """`break` — leave the innermost enclosing `while`/`for` (item 379,
    docs/design/379-break-continue.md). Bare, no label, no value; valid only
    inside a loop body in the fn statement grammar."""
    line: int


@dataclass
class ContinueStmt:
    """`continue` — skip to the innermost enclosing loop's next iteration
    (item 379). Bare, no label, no value; valid only inside a loop body in
    the fn statement grammar."""
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
class AbortStmt:
    """`abort` — drive the enclosing session frame's 245 abort (roadmap item
    377). Marks every live activation frame aborting, replays the witnessed
    inverses (LIFO), and drops the deferral queue, exactly as
    `revl.mcp.session.Session.abort` does (docs/design/245-session-commit.md).

    This is what makes H1's flagship proof expressible in-language: perform
    witnessed mutations, `abort`, then `assert no_residue` — the witnessed
    effects revert and the workspace is left byte-identical to before, with no
    host Python in the loop (F-H1.7). Like a session abort it tears the live
    composition down, so it names the end of the driven composition."""

    line: int


@dataclass
class AdvanceStmt:
    """`advance <n><unit>` — drive the clock coeffect (item 57) forward inside a
    `lifecycle test`. The clock never moves on its own, so this is the only way
    a test can exercise a timer's *firing* — a firing is a deterministic
    timeline step (`fires on the 3rd tick`), not a wall-clock race
    (item 102, docs/time-coeffect.md)."""

    ms: int
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

# item 384: foreign statement/declaration keywords that LEX as identifiers
# (none is a revl keyword) and so land in a statement- or declaration-dispatch
# position where they are already an error — but a cryptic one (`expected a
# top-level declaration, found 'def'`). Redirected to the revl spelling. The
# check runs only at those error sites, which no valid program reaches with
# these spellings, so it is false-positive-free (a fn/binding legitimately
# NAMED `def`/`throw` is parsed in name position, never reaching the dispatch).
# (message, hint) pairs; kept in sync with lower._FOREIGN_NAME_REDIRECTS.
_FOREIGN_STMT_KEYWORDS = {
    "def": ("revl has no `def`",
            "a function is declared with `fn` (syntax-2.0 §3.1)"),
    "throw": ("revl has no `throw`",
              "a pure function returns a `Result`; a component activation body "
              "signals failure with `fail` (syntax-2.0 §3.3, §4b.5)"),
    "elif": ("revl has no `elif`",
             "chain conditionals with `else if` (syntax-2.0 §3.2)"),
    "lambda": ("revl has no `lambda`",
               "an anonymous function is an arrow `x => …` (syntax-2.0 §3.2)"),
    "const": ("revl has no `const`",
              "use `let` (single-assignment) or `var` (mutable) (syntax-2.0 §3.5)"),
    "print": ("revl has no `print`",
              "pure code has no I/O — emit output through a service effect "
              "(syntax-2.0 §4)"),
}


# ---------------------------------------------------------------- parser

class Parser:
    def __init__(self, source: str, filename: str):
        self.filename = filename
        self.toks = lex(source, filename)
        self.pos = 0
        # When set, the next `_bor` call does not consume a top-level `|` — it
        # is the functional-record-update separator `{base | f = e}`, not the
        # bitwise-OR operator (item 366). The flag is cleared the moment it is
        # read so a *parenthesised* `(a | b)` inside the base still lexes as
        # bitwise OR.
        self._suppress_bor = False
        # item 379: bare `break`/`continue` are valid only inside a `while`/`for`
        # body. `_loop_depth` counts the enclosing loops of the statement being
        # parsed (parse-state, like `_suppress_bor`), incremented around each
        # loop's body parse. A match block arm is lambda-lifted into a separate
        # helper fn at lowering (src/revl/lower.py `_lift_block_arm`), so a
        # `break` written there would land in a fn with no loop; entering an arm
        # resets the depth to 0 so such a `break` is refused, and `_in_block_arm`
        # switches the refusal to the block-arm voice (C1,
        # docs/design/379-break-continue.md).
        self._loop_depth = 0
        self._in_block_arm = False

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

    # -- item 157: `;` as an optional statement separator/terminator

    def _skip_semis(self) -> None:
        """`;` is an *optional* statement separator/terminator (item 157). It
        carries no meaning of its own: statements may be separated by a newline
        (as always) OR by `;`, and a leading, trailing, or repeated `;` — a lone
        `;` or `;;` being an empty statement — is a harmless no-op. Runs of them
        are skipped wherever statements are listed, so a program written with no
        `;` tokenises and parses to the exact same AST as before."""
        while self.at(";"):
            self.next()

    # -- item 158: cordis-domain nouns as CONTEXTUAL keywords in name position

    # `realm`, `intercept`, `isolate`, `in`, `with` are reserved where the
    # grammar wants the keyword (a statement/clause head: `isolate k in r`,
    # `intercept k with {…}`, `spawn C with {…}`, a `realm { … }` label). In a
    # position where only a NAME is grammatically possible — a record/param
    # field name, or a `.field` access — none of the five can head a clause, so
    # all five relax to ordinary identifiers there without ambiguity. (`in` and
    # `with` never *lead* anything: they are always the tail of an `isolate`/
    # `intercept`/`spawn` form, so their keyword role is untouched here.)
    _CONTEXTUAL_NOUNS = frozenset({"realm", "intercept", "isolate", "in", "with"})

    def _is_name_tok(self, tok: Token) -> bool:
        return tok.kind == "ident" or (
            tok.kind == "kw" and tok.value in self._CONTEXTUAL_NOUNS)

    def _name(self, what: str | None = None) -> str:
        """A NAME in a position where no keyword is grammatically possible
        (field name, parameter name, `.field` access). Accepts an ordinary
        `ident`, or one of the contextual cordis-domain nouns (item 158). A
        genuinely reserved keyword (`type`, `fn`, …) falls through to
        `expect("ident", …)` unchanged, so its "expected ident/…"-plus-"reserved
        keyword" diagnostic is byte-for-byte what it was before this broadening.
        `what` is forwarded verbatim so each call site keeps its exact prior
        wording (`None` → the default "expected ident")."""
        tok = self.peek()
        if self._is_name_tok(tok):
            return self.next().value
        return self.expect("ident", what=what).value

    # -- item 237: cordis-domain nouns as record FIELD names

    # A record FIELD position — a record-literal key (`{k: e}`), a functional
    # record-update field (`{base | k = e}`), or a record-type field
    # (`type T = {k: τ}`) — always has its name immediately followed by `:` or
    # `=`. None of the cordis-domain nouns can head a clause in that spot, so on
    # top of the item-158 five (`realm`/`intercept`/`isolate`/`in`/`with`, which
    # already relax via `_name`) the three component-grammar heads
    # `component`/`config`/`requires` (the nouns self-host dogfood item 234 hit)
    # also relax to ordinary field names here. They stay genuine keywords
    # everywhere they can LEAD a form — `component`/`config` as decl/body heads,
    # `requires` as a component clause head — because none of those are field
    # positions and none reach this helper. (Parameter position keeps refusing
    # them: see item 158 and `_name`.)
    _RECORD_KEY_NOUNS = _CONTEXTUAL_NOUNS | frozenset(
        {"component", "config", "requires"})

    def _record_key_name(self, what: str | None = None) -> str:
        """A NAME in record-FIELD position, broadening `_name` (item 158) with
        the three component-grammar nouns (item 237). A field name is always
        followed by `:` or `=`, so no keyword reading is grammatically possible.
        Any other reserved keyword (`type`, `fn`, …) still falls through to
        `expect("ident", …)` with its diagnostic byte-for-byte unchanged."""
        tok = self.peek()
        if tok.kind == "ident" or (
                tok.kind == "kw" and tok.value in self._RECORD_KEY_NOUNS):
            return self.next().value
        return self.expect("ident", what=what).value

    # -- productions

    def parse(self) -> Program:
        try:
            return self._parse_program()
        except RevlError as e:
            better = self._maybe_stray_backtick_error(e)
            if better is not None:
                raise better from None
            raise

    def _maybe_stray_backtick_error(self, e: RevlError) -> "RevlError | None":
        """Item 365: a stray backtick inside a host `//`/`/*` comment closes a
        backtick template early; the template's tail then reparses as revl and
        the raw parse error names whatever identifier the tail happens to hold,
        far from the real mistake. When the lexer flagged such a close (see
        `lexer._closing_backtick_is_stray`) at or before the failing line, point
        the diagnostic back at the template boundary instead — the item-70 move
        of naming the construct that swallowed the tokens.

        Only ever called AFTER a parse has already failed, so it can reword a
        genuine error but never reject accepted source (additivity)."""
        best = None
        for tok in self.toks:
            if tok.kind != "template":
                continue
            span = getattr(tok, "stray_backtick", None)
            if span is None:
                continue
            start_line, close_line = span
            # nearest suspect template whose stray close is at or before the
            # point the parse gave out.
            if e.line is not None and close_line > e.line:
                continue
            if best is None or close_line > best[1]:
                best = span
        if best is None:
            return None
        start_line, close_line = best
        opened = (f"opened on line {start_line}"
                  if start_line != close_line else "on this line")
        return RevlError(
            self.filename, close_line,
            f"a stray backtick closed the template {opened} early: this "
            "backtick sits inside the host comment or string but revl read it "
            "as the template's end",
            hint="revl templates have no backtick escape; embed a literal "
                 "backtick with an interpolation, `` ${\"`\"} ``, or move the "
                 "template's closing backtick to where the template really ends",
        )

    def _parse_program(self) -> Program:
        program = Program(self.filename)
        while True:
            self._skip_semis()
            if self.at("eof"):
                break
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
                # item 249 Slice C: an `endorse[<origin>]` declaration slot may
                # sit before `fn`, exactly where `emission[cap]` sits on an
                # extern — it names the taint classes the body may declassify.
                endorse_origins = self._endorse_slot()
                if self.at("kw", "fn"):
                    if commutative:
                        tok = self.peek()
                        raise self.err(tok.line, f"expected `service` after `commutative`, found {tok.value!r}")
                    program.fn_decls.append(self.fn_decl(True, verified, endorse_origins))
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
                endorse_origins = self._endorse_slot()  # item 249 Slice C
                if self.at("kw", "fn"):
                    program.fn_decls.append(self.fn_decl(False, True, endorse_origins))
                else:
                    tok = self.peek()
                    raise self.err(tok.line, f"expected `fn` after `verified`, found {tok.value!r}")
            elif self.at("kw", "extern"):
                program.externs.append(self.extern_decl(False))
            elif self.at("ident", "endorse"):
                # item 249 Slice C: a top-level `endorse[<origin>] [verified] fn`
                # — the declassification slot before `fn`, mirroring `emission`.
                endorse_origins = self._endorse_slot()
                verified = False
                if self.at("kw", "verified"):
                    self.next()
                    verified = True
                if not self.at("kw", "fn"):
                    tok = self.peek()
                    raise self.err(tok.line, f"expected `fn` after `endorse[...]`, found {tok.value!r}",
                                   hint="the `endorse[<origin>]` slot names what a `fn` body "
                                        "may declassify — it can only precede a `fn` (item 249)")
                program.fn_decls.append(self.fn_decl(False, verified, endorse_origins))
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
                self._reject_foreign_keyword(tok)  # item 384
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
        # `pure`/`acquire`/`emission` are reserved keywords; `witnessed` (item
        # 243) is a CONTEXTUAL keyword recognised only in this classification
        # slot, so the self-hosted lexer's KEYWORDS set needs no sync and no
        # program that used `witnessed` as an ordinary name is broken.
        if self.peek().value not in ("pure", "acquire", "emission", "witnessed") \
                or (not self.at("kw") and not self.at("ident", "witnessed")):
            raise self.err(
                line,
                "unclassified extern — expected `pure`, `acquire`, `emission`, or "
                "`witnessed` after `extern`",
                hint="classification is mandatory: `pure` has no observable effect, "
                     "`acquire` must declare `undo`, `emission` may declare `compensate`, "
                     "and `witnessed` is a reversible mutation whose declared `undo` the "
                     "accumulator auto-registers (docs/design/243-witnessed-externs.md)",
            )
        classification = self.next().value
        # Capability scope, `witnessed[fs]` / `emission[gateway.send]`
        # (docs/design/243-witnessed-externs.md, item 343). A witnessed mutation
        # and an emission are capability-scoped alike — the bracket is revl's
        # parameterisation bracket — and both join the same authority namespace.
        # The declared token, not the extern NAME, is the emission's capability,
        # so a `capability C requires approval` rule and a standing grant target
        # the crossing by token (item 344). `pure`/`acquire` cross no boundary,
        # so the parse is still refused on them and the surface stays honest.
        capabilities: tuple[str, ...] = ()
        if self.at("["):
            if classification not in ("witnessed", "emission"):
                raise self.err(
                    self.peek().line,
                    f"`{classification}` extern takes no capability scope",
                    hint="only a `witnessed[caps]` or `emission[caps]` extern is "
                         "capability-scoped (docs/design/243-witnessed-externs.md, "
                         f"item 343); write `{classification} fn ...` without the "
                         "bracket",
                )
            capabilities = self._capability_list(kind=classification)
        # Optional reach clause `(confined: <param>)` (item 373), a sibling of
        # the `[caps]` scope above. It sits right after the classification (and
        # its optional bracket) and before the `async`/`deferred` modifiers.
        # `(` is not otherwise legal here — the next token is `async`/`deferred`
        # /`fn` — so the peek is unambiguous. Structural parse only; the
        # "emission-only" and "target names a parameter" rules are enforced in
        # lower, next to the sibling classification checks, with honest messages.
        reach: tuple[str, str] | None = None
        if self.at("("):
            reach = self._reach_clause()
        # Optional `async` modifier between the classification and `fn`,
        # mirroring where service-op modifiers sit (parser.py:895-906). The
        # classification stays first and mandatory, so the "unclassified
        # extern" diagnostic above is untouched. Validity rules (emission-only,
        # no compensate) are enforced in lower (docs/design/async-extern.md §1).
        # `async` and `deferred` modifiers, in either order, between the
        # classification (and its optional capability scope) and `fn`. `async`
        # is a reserved keyword; `deferred` (item 245) is a CONTEXTUAL keyword
        # recognised only in this modifier slot, so the lexer's KEYWORDS set
        # needs no sync and no program that used `deferred` as an ordinary name
        # is broken (the same discipline `witnessed` uses above). Their pairwise
        # validity (`deferred` is emission-only and async-exclusive) is enforced
        # in lower with honest messages, not the parser.
        async_ = False
        deferred = False
        while True:
            if self.at("kw", "async") and not async_:
                self.next()
                async_ = True
            elif self.at("ident", "deferred") and not deferred:
                self.next()
                deferred = True
            else:
                break
        self.expect("kw", "fn")
        # item 388: the caller-decided colour marker `fn|async`. It sits right
        # after `fn` (not in the pre-`fn` modifier slot) and reads as "either
        # colour": one authored body, colour decided at each call site. `async`
        # is a reserved keyword, so the lexer needs no change; a bare `fn` is
        # byte-identical. Validity (emission-only, not `deferred`, not also a
        # fixed `async`) is enforced in lower with honest messages.
        colour_poly = False
        if self.at("|"):
            self.next()
            self.expect("kw", "async",
                        what="`async` after `fn|` — the caller-decided colour "
                             "marker `fn|async` (item 388)")
            colour_poly = True
        name = self.expect("ident").value
        type_params = self._type_param_list()
        self.expect("(")
        params: list[FnParam] = []
        while not self.at(")"):
            pline = self.peek().line
            pname = self._name()
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
        # item 246: the declaration-owned `requires approval` clause (a contextual
        # `approval` after the `requires` keyword, so the keyword set is untouched).
        requires_approval = False
        if self.at("kw", "requires"):
            self.next()
            self.expect("ident", "approval",
                        what="`approval` after `requires` (item 246)")
            requires_approval = True
        # item 379: an optional typed `config { ... }` block, reusing the same
        # `config_block()` a component uses (parser.py:1352-1365). It sits after
        # the teardown/approval clauses and before the `= @backend` bodies, the
        # last declaration-level clause. `config` is already a reserved keyword
        # (recognised in `component`), so the lexer needs no change and an
        # extern with no block is byte-identical.
        config: list[ConfigField] = []
        if self.at("kw", "config"):
            config = self.config_block()
        bodies: list[HostBody | HostBodyFile | HostRef] = []
        while self.at("="):
            self.next()
            if self.at("hostbody"):
                host_tok = self.next()
                backend, text = host_tok.value
                bodies.append(HostBody(backend, text, host_tok.line))
            elif self.at("@"):
                # item 396: the two file/module body forms. When `@py` is NOT
                # followed by `{`, the lexer emits a plain `@` token and re-lexes
                # the backend word as an ordinary identifier (lexer.py:322-324),
                # so both spellings need ZERO lexer change. `file`/`ref`/`from`
                # are CONTEXTUAL keywords recognised only in this slot (the
                # discipline `witnessed`/`deferred`/`confined` use), so the
                # KEYWORDS set is untouched and no program using them as names is
                # broken. The parser stays IO-free: it records the path (and, for
                # a ref, the symbol) and the compiler reads it, jailed.
                at_line = self.next().line
                backend = self.expect(
                    "ident",
                    what="a backend name after `@` (e.g. `@py file ...` or "
                         "`@py ref sym from ...`)").value
                if self.at("ident", "ref"):
                    # option B: `= @backend ref sym from "path"`
                    self.next()
                    sym_tok = self.expect(
                        "ident",
                        what="a host symbol name after `ref` (the identifier to "
                             "import from the referenced module, item 396)")
                    self.expect(
                        "ident", "from",
                        what='`from` — the host-import form `@backend ref sym '
                             'from "path"` (item 396)')
                    path_tok = self.expect(
                        "string", what="a host-module file path string")
                    bodies.append(HostRef(backend, sym_tok.value,
                                          path_tok.value, at_line))
                else:
                    # option A: `= @backend file "path"`
                    self.expect(
                        "ident", "file",
                        what='`file` — the external host-body form `@backend '
                             'file "path"`, or `ref sym from "path"` — the '
                             'host-import form (item 396)')
                    path_tok = self.expect("string",
                                           what="a host-body file path string")
                    bodies.append(HostBodyFile(backend, path_tok.value, at_line))
            else:
                tok = self.peek()
                raise self.err(
                    tok.line,
                    f"expected a `@backend {{ ... }}` host body, a `@backend "
                    f'file "path"` reference, or a `@backend ref sym from '
                    f'"path"` import after `=`, found {tok.value!r}')
        if not bodies:
            raise self.err(line, f"extern `{name}` must declare at least one `@backend {{ ... }}` body")
        return ExternDecl(name, classification, params, returns, undo, compensate, bodies, public, line,
                          capabilities=capabilities, type_params=type_params, async_=async_,
                          deferred=deferred, requires_approval=requires_approval,
                          reach=reach, config=config, colour_poly=colour_poly)

    def _reach_clause(self) -> tuple[str, str]:
        """`(confined: <param>)` after an emission classification — item 373.

        The reach clause names what an emission crossing is BOUNDED to. `confined`
        is a CONTEXTUAL keyword recognised only in this slot (the discipline
        `witnessed`/`deferred` use), so the lexer's KEYWORDS set needs no sync and
        no program that used `confined` as an ordinary name is broken. The target
        is a bare ident naming the parameter that carries the confinement region;
        it is not resolved here (params are parsed just below) — lower checks it
        against the actual parameter list. `confined` is the only reach kind for
        now; the shape leaves room for more (the returned kind is carried through).
        """
        self.expect("(")
        kind = self.expect("ident", "confined",
                           what="`confined` — the reach kind (item 373)").value
        self.expect(":", what="`:` after the reach kind")
        target = self.expect("ident", what="the parameter the crossing is "
                                            "confined to").value
        self.expect(")")
        return (kind, target)

    def _capability_list(self, kind: str = "emission") -> tuple[str, ...]:
        """`[a, b]` after `emission`/`witnessed` — the boundaries this operation
        may cross.

        Names are wiring names, not types: a requirement/provision key or an
        `emission` extern (docs/capabilities.md). They are not resolved here —
        a service is written before its providers exist — the G4 check in
        lower.py compares them against what a provider's body actually reaches.

        An entry may be a realm-style dotted token (`gateway.send`,
        `production.payment`) so an emission scope names the same tokens the
        item-246 `Approval[C]` / item-33 policy grammar do (item 343). A bare
        ident is the single-segment case, so every pre-343 `witnessed[fs]` list
        parses to the same tokens byte-for-byte.
        """
        line = self.expect("[").line
        names: list[str] = []
        while not self.at("]"):
            parts = [self.expect("ident", what="a capability name").value]
            while self.at("."):
                self.next()
                parts.append(self.expect("ident").value)
            names.append(self._capability_params(".".join(parts)))
            if self.at(","):
                self.next()
        self.expect("]")
        if not names:
            raise self.err(
                line,
                f"`{kind}[]` names no capability, so it forbids every crossing",
                hint="an operation that may not cross a boundary is a plain `fn` — "
                     f"drop the `{kind}` modifier instead (G4)",
            )
        seen: set[str] = set()
        for cap in names:
            if cap in seen:
                raise self.err(line, f"duplicate capability `{cap}` in `{kind}[...]`")
            seen.add(cap)
        return tuple(names)

    def _capability_params(self, token: str) -> str:
        """An optional parenthesized literal parameter list on a dotted
        capability token (item 294): `fs.write(path="/data/incoming")`,
        `db.read(table="orders")`, `model.complete(calls=3)`. A bare token with
        no `(` returns unchanged (byte-identical to every pre-294 token), so the
        extension is purely additive.

        Values are STATIC literals (a string or a non-negative integer). The
        parse funnels through `cap_order.make_cap`, the ONE canonical point: it
        validates against the CLOSED parameter registry (an unknown name like
        `pth=` refuses HERE, at parse, never silently inert), canonicalizes a
        path value (trailing slash dropped, `.`/`..`/`//`/`"/"` refused), refuses
        a parameter list on `*` and duplicate keys, and returns the canonical
        `(T, P)`, stored as its canonical spelling so the fold re-reads it at a
        single point."""
        if not self.at("("):
            return token
        from . import cap_order  # noqa: PLC0415 - lazy, avoids an import cycle
        line = self.next().line          # consume `(`
        raw: list[tuple[str, object]] = []
        while not self.at(")"):
            name = self.expect("ident", what="a capability parameter name").value
            self.expect("=", what="`=` after a capability parameter name")
            vtok = self.peek()
            if vtok.kind in ("string", "int"):
                self.next()
                value: object = vtok.value
            else:
                raise self.err(
                    vtok.line,
                    "a capability parameter value must be a string or integer "
                    f"literal, found {vtok.value!r}",
                    hint='write `path="/data/incoming"` or `calls=10`; a '
                         "per-instance value (`config.job_root`) is item 294 "
                         "Slice 2")
            raw.append((name, value))
            if self.at(","):
                self.next()
        self.expect(")")
        try:
            return cap_order.make_cap(token, raw).to_str()
        except cap_order.CapError as exc:
            raise self.err(line, str(exc), hint=exc.hint) from exc

    def _capability_token(self, what: str = "a capability token") -> str:
        """One capability token in an `Approval[C]` type or an `await
        approval[C]` form (item 246). A token names a boundary, not a type, so it
        is a string literal (`"production.payment"`), a dotted-ident path
        (`production.payment`), or a bare ident (`payment`) — never `*`, since an
        unnameable reach can never be approved into (Decision 1, the `*` row)."""
        tok = self.peek()
        if tok.kind == "string":
            self.next()
            token = tok.value
        else:
            parts = [self.expect("ident", what=what).value]
            while self.at("."):
                self.next()
                parts.append(self.expect("ident").value)
            token = ".".join(parts)
        if token == "*":
            # item 246, the `*` row (Decision 1): an unnameable reach can never be
            # proven reversible, so no approval shape can name it — no
            # `await approval["*"]`, no `Approval[*]`. A `*` crossing receives only
            # the per-call ticket, every time.
            raise self.err(
                tok.line,
                "`*` is not an approvable capability — an unnameable reach cannot "
                "be approved into (item 246, the `*` row)",
                hint="a bare `emission` reach is class (c) and receives the "
                     "per-call ticket, not a typed `Approval`")
        return token

    def _endorse_slot(self) -> frozenset:
        """Consume zero or more `endorse[<origin>[, <origin>...]]` declaration
        modifiers (roadmap item 249, Slice C) and return the declared origin set.

        The slot is spelled in the same bracket style as `emission[cap]`, so a
        declaration reads its declassification rights the way it reads its
        emission scope. `endorse` is not a reserved keyword (it is an ident whose
        expression form is intercepted in `_primary`), so the modifier is matched
        on the ident, not a kw."""
        origins: set[str] = set()
        while self.at("ident", "endorse"):
            self.next()
            self.expect("[", what="`[<origin>]` after `endorse` (the taint class "
                             "this declaration may declassify, item 249)")
            origins.add(self.expect("ident").value)
            while self.at(","):
                self.next()
                origins.add(self.expect("ident").value)
            self.expect("]")
        return frozenset(origins)

    def service(self, commutative: bool = False) -> ServiceDecl:
        line = self.expect("kw", "service").line
        name = self.expect("ident").value
        self.expect("{")
        methods: dict[str, MethodDecl] = {}
        while not self.at("}"):
            emission = False
            capabilities: tuple[str, ...] | None = None
            endorse_origins: frozenset = frozenset()
            async_ = False
            method_commutative = False
            method_idempotent = False
            mline = self.peek().line
            while (self.at("kw") and self.peek().value in ("emission", "async", "commutative", "idempotent")) \
                    or self.at("ident", "endorse"):
                if self.at("ident", "endorse"):
                    endorse_origins = endorse_origins | self._endorse_slot()
                    continue
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
                pname = self._name()
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
                capabilities=capabilities, endorse_origins=endorse_origins,
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
        # item 246: `Approval[C]` carries a capability TOKEN, not a type — the
        # bracket argument may be a string literal or a dotted path, which the
        # ordinary type-application tail (which expects a type) cannot read.
        if base == "Approval" and self.at("["):
            self.next()
            token = self._capability_token()
            self.expect("]")
            return self._type_suffix_tail(f"Approval[{token}]")
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
        while True:
            self._skip_semis()
            if self.at("}"):
                break
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
            fname = self._name()
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
                acquire, undo, line, setup, is_async = self.effect_form(tok.line)
                return LetEffect(bind, acquire, undo, line, setup, verified_effect,
                                 is_async)
            # item 246: `let a = await approval[C] { fields }` — an acquisition-
            # shaped suspension that yields an `Approval[C]`. Allowed in the
            # activation body exactly where `let x = effect …` is (both bind a
            # value the plain-value stratum otherwise refuses here).
            if not mutable and self.at("kw", "await") \
                    and self.toks[self.pos + 1].kind == "ident" \
                    and self.toks[self.pos + 1].value == "approval":
                if in_method:
                    raise self.err(
                        tok.line,
                        "`await approval[C]` is only allowed in a component "
                        "activation body, not a provide method",
                        hint="mint the approval in the activation body and thread "
                             "it to the crossing there; a provide method runs "
                             "while the component is ACTIVE (item 246)")
                if declared is not None:
                    raise self.err(
                        tok.line,
                        f"`let {bind}: {declared} = await approval[…]` — the "
                        f"binding's type is `Approval[C]`, fixed by the "
                        f"capability, so it cannot be annotated",
                        hint="drop the annotation; `await approval[C]` already "
                             "names the capability the value carries")
                request = self._await_approval_expr()
                return LetApprovalStmt(bind, request, tok.line)
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
            acquire, undo, line, setup, is_async = self.effect_form(tok.line)
            return EffectStmt(acquire, undo, line, setup, verified=True,
                              is_async=is_async)
        if tok.kind == "kw" and tok.value == "effect":
            acquire, undo, line, setup, is_async = self.effect_form(tok.line)
            return EffectStmt(acquire, undo, line, setup, is_async=is_async)
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
            return self._emit_stmt(tok.line, is_async=False)
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
            # item 131: `await emit <call> [compensate <e>]` — an AWAITED
            # emission step. The boundary marker sits outermost (design §2):
            # the step is an iteration boundary whose payload is an emission.
            # `await` on a provide-method emission is not this form — that path
            # is refused above unless the method is `async fn`.
            if not in_method and self.at("kw", "emit"):
                self.next()
                return self._emit_stmt(tok.line, is_async=True)
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
            # `realms(...)` (plural, item 162) binds the key across N realms with
            # an optional routing strategy; `realm(...)` (singular) is unchanged.
            # `realms`/`strategy` are ordinary identifiers (NOT reserved words) —
            # they head a clause only in this exact `isolate <key> in …` position,
            # so a program using either as a name stays valid and the reference
            # KEYWORDS set (and the selfhosted lexer that mirrors it) is untouched.
            if self.at("ident", "realms"):
                realms, strategy = self.realms_route()
                return RouteStmt(key, realms, strategy, tok.line)
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
        if tok.kind == "kw" and tok.value in ("break", "continue"):
            # item 379: `break`/`continue` are loop control, and the
            # activation/provide-method grammar has no loop form (loops live
            # only in the fn statement grammar). Redirect in the block-arm
            # voice `_refuse_block_arm_stmt` uses for loops themselves.
            raise self.err(
                tok.line,
                f"`{tok.value}` is not valid here: activation and provide-method "
                "bodies have no loops",
                hint="iterate in a module `fn` (the only grammar with `while`/"
                     "`for`, and thus `break`/`continue`) and call it from here",
            )
        self._reject_foreign_keyword(tok)  # item 384
        raise self.err(
            tok.line,
            f"expected a statement (`let`, `effect`, `emit`, `fail`, `if`{', `return`' if in_method else ', `provide`'}), found {tok.value!r}",
            hint="revl bodies contain only effect forms — plain expressions have no effect to record (G6)",
        )

    def _emit_stmt(self, line: int, *, is_async: bool) -> "EmitStmt":
        """Parse an `emit` statement body (the `emit` keyword already consumed).

        Shared by the plain `emit <call> [compensate …] [with …]` step and the
        item-131 awaited `await emit <call> [compensate …]` step; `is_async`
        records which spelling headed it so the admission pass (lower.py) can
        enforce the exact `await`/async pairing and the emitters can prefix the
        tier's await marker."""
        expr = self.pure_expr()
        compensate = None
        if self.at("kw", "compensate"):
            self.next()
            compensate = self.pure_expr()
        # item 246: `emit <call> with <e>` threads an `Approval[C]` to the
        # crossing. The clause is the explicit dataflow that turns
        # "unreachable without approval" into a type check (Decision 3).
        approval = None
        if self.at("kw", "with"):
            self.next()
            approval = self.pure_expr()
        return EmitStmt(expr, line, compensate, approval, is_async)

    def effect_form(self, line: int):
        self.expect("kw", "effect")
        # item 131: `effect await <expr> undo <expr>` — an ASYNC acquisition.
        # The `await` is a divert boundary (paper §4.3.2): the fiber suspends
        # during the call and the LANDED result is bound, not the in-flight
        # value. The block form `effect { …setup…; acq }` keeps its stratum-1
        # interior and does NOT take `await` (design §2 fence) — hoist the
        # preparation into a module fn, which async-colors under item 90, and
        # write `effect await prepped_open(cfg) undo …`.
        is_async = False
        if self.at("kw", "await"):
            self.next()
            is_async = True
            if self.at("{"):
                raise self.err(
                    line,
                    "`effect await { … }` — the block effect form does not take "
                    "`await` (item 131)",
                    hint="hoist the preparation into a module fn (it async-colors "
                         "under item 90) and write `effect await prepped_open(cfg) "
                         "undo …`; awaits interleaved with pure setup steps reopen "
                         "the acquisition-atomicity argument (design §4 clause 1)")
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
            return acquire, undo, line, [], is_async
        # capability leases: `effect lease fs.write(path="/tmp") ttl 10m undo
        # l.revoke()` (item 294 Slice 2). A lease is a ticket-gated acquisition of
        # a standing grant over the capability's cone; its inverse is its own
        # revoke, so an `undo` is required exactly as for `spawn`. The acquired
        # value is a lease handle, and `undo l.revoke()` retires the grant on the
        # LIFO teardown. `lease` is a CONTEXTUAL keyword, not reserved: the lease
        # form is `effect lease <ident…>` (the capability's first segment is an
        # ident), so `effect lease()`, `effect lease.m()`, and any other use of a
        # binding named `lease` stay ordinary acquisition expressions. `await`
        # does not combine with a lease (it is gated at load, not a suspension).
        if self.at("ident", "lease") and self.toks[self.pos + 1].kind == "ident":
            if is_async:
                raise self.err(
                    line,
                    "`effect await lease …` — a lease acquisition is gated at "
                    "load, not an await suspension source (item 294)",
                    hint="write `let l = effect lease <cap> ttl <dur> undo "
                         "l.revoke()` without `await`")
            self.next()                       # consume `lease`
            acquire = self._lease_acquire(line)
            if not self.at("kw", "undo"):
                raise self.err(
                    line,
                    "a `lease` acquisition needs `undo l.revoke()`",
                    hint="a lease is an acquisition whose inverse is its own "
                         "revoke; write `let l = effect lease "
                         f"{acquire.capability} … undo l.revoke()` (G4, item 294)")
            self.next()
            undo = self.pure_expr()
            return acquire, undo, line, [], is_async
        setup: list = []
        if self.at("{"):
            self.next()
            stmts = []
            while True:
                self._skip_semis()
                if self.at("}"):
                    break
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
            # witnessed-inverse externs (item 243, docs/design/243-witnessed-externs.md):
            # a witnessed call's inverse is the extern's own DECLARED `undo`,
            # auto-registered by the teardown accumulator on the `Ok` branch —
            # there is no site-spelled undo, so the grammar admits its omission
            # for a bare-name call. Whether the callee is ACTUALLY a witnessed
            # extern is not decidable here: `use` imports are resolved in a
            # later pass (compiler.py's `_ModuleLoader`), one file at a time,
            # so an imported witnessed extern's classification is unknown at
            # this point even though a same-file one's is (item 315 — the
            # per-file `_witnessed_names` set this replaced could see only
            # same-file `extern` decls, so an imported `write`/`rm` etc. was
            # hard-refused here before module resolution ever ran). Rather
            # than duplicate resolution, the parser admits every bare-name
            # call missing its `undo` and defers the real gate to lower.py's
            # `_lower_effect_step`, which runs on the MERGED program — after
            # imports are resolved — and refuses there if the callee turns
            # out not to be witnessed after all (same message, same G4 code).
            # Anything that cannot possibly BE an extern call by bare name (a
            # dotted path, a literal, a binary op, …) is refused immediately,
            # exactly as before — no import can turn `Pool.open(...)` into a
            # witnessed call, so there is nothing to defer.
            if isinstance(acquire, ExprCall) and isinstance(acquire.callee, ExprVar):
                return acquire, None, line, setup, is_async
            head = _describe_expr(acquire)
            raise self.err(
                line,
                f"effect has no `undo` and {head} is not pure",
                hint=f"write `effect {head}(...) undo <expr>`, or mark the call `emit` if it deliberately crosses the system boundary (G4)",
            )
        self.next()
        undo = self.pure_expr()
        return acquire, undo, line, setup, is_async

    def _lease_acquire(self, line: int) -> "LeaseAcquire":
        """`<cap> [ttl <n><unit>] [uses <n>]` after `effect lease` (item 294).

        The capability is the standard parameterized token grammar (a bare or
        parameterized dotted token, funneled through `cap_order` at
        `_capability_params`, so `*` and unregistered parameters refuse exactly as
        in an `emission[...]` scope). `ttl`/`uses` are bare-ident qualifiers (not
        reserved words); order-free, each at most once. At least one must bound the
        grant — an unbounded lease is refused here, the item-344 mandatory-bound
        rule enforced at the source instead of only at mint."""
        captok = self.peek()
        if captok.value == "*":
            raise self.err(captok.line,
                           "`*` is not a leasable capability — an unnameable "
                           "reach cannot be granted (item 294, the `*` row)",
                           hint="name the boundary the lease grants")
        parts = [self.expect("ident", what="a capability token after "
                             "`effect lease`").value]
        while self.at("."):
            self.next()
            parts.append(self.expect("ident").value)
        capability = self._capability_params(".".join(parts))
        ttl_ms = None
        uses = None
        while self.at("ident", "ttl") or self.at("ident", "uses"):
            qual = self.next().value
            if qual == "ttl":
                if ttl_ms is not None:
                    raise self.err(line, "duplicate `ttl` on a lease")
                ttl_ms = self._duration_ms()
            else:
                if uses is not None:
                    raise self.err(line, "duplicate `uses` on a lease")
                ntok = self.peek()
                if ntok.kind != "int" or ntok.value < 1:
                    raise self.err(
                        ntok.line,
                        f"`uses` on a lease needs a positive whole count, found "
                        f"{ntok.value!r}",
                        hint="write `uses 3` — the number of class-(c) crossings "
                             "the lease may auto-approve")
                self.next()
                uses = ntok.value
        if ttl_ms is None and uses is None:
            raise self.err(
                line,
                "a lease must be bounded — give `ttl <dur>` and/or `uses <n>`",
                hint="an unbounded lease would convert prompt-per-call into "
                     "prompt-never forever; consent stays bounded (item 344/294)")
        return LeaseAcquire(capability, ttl_ms, uses, line)

    def _duration_ms(self) -> int:
        """`<n><unit>` (e.g. `10m`, `30s`, `250ms`) -> milliseconds, reusing the
        timer duration units. The unit is a bare ident token following the count."""
        num = self.peek()
        if num.kind != "int" or num.value < 1:
            raise self.err(num.line,
                           f"expected a positive whole-number duration, found "
                           f"{num.value!r}",
                           hint="a duration is `<n><unit>`, e.g. `10m` "
                                "(units: ms, s, m, h, d)")
        self.next()
        unit = self.peek()
        if unit.kind != "ident" or unit.value not in self._DURATION_UNITS:
            raise self.err(unit.line,
                           f"expected a duration unit (ms, s, m, h, d), found "
                           f"{unit.value!r}")
        self.next()
        return num.value * self._DURATION_UNITS[unit.value]

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
        while True:
            self._skip_semis()
            if self.at("}"):
                break
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
                field = self._name()
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
        while True:
            self._skip_semis()
            if self.at("}"):
                break
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

    def realms_route(self) -> tuple[list[str], str | None]:
        """`realms("w1", "w2", ...) [strategy(<ident>)]` — the multi-realm bind
        (item 162), the plural of `realm("<label>")`. Same static-string-literal
        rule as `realm_label` (a realm is not config-derived, else G2 would be
        unsound). Returns the ordered realm list and the strategy name (or
        `None`). The order is preserved as written — a router's rotation is
        defined over the list in declaration order."""
        line = self.expect("ident", "realms").line
        self.expect("(")
        if self.at(")"):
            raise self.err(
                line,
                "`realms(...)` needs at least one realm label",
                hint="the multi-realm bind names the realms to route across; for a single "
                     "realm use `realm(\"<label>\")` (singular)",
            )
        realms: list[str] = []
        while True:
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
            if label in realms:
                raise self.err(
                    line,
                    f"realm `{label}` is listed twice in `realms(...)` — routing a key to "
                    f"the same realm twice is meaningless",
                    hint="each realm in the list is a distinct routing target; remove the "
                         "duplicate (G2 already keeps one provider per (key, realm))",
                )
            realms.append(label)
            if self.at(","):
                self.next()
                # allow a trailing comma before `)`
                if self.at(")"):
                    break
            else:
                break
        self.expect(")")
        strategy: str | None = None
        if self.at("ident", "strategy"):
            self.next()
            self.expect("(")
            strategy = self.expect("ident", what="a strategy name").value
            self.expect(")")
        return realms, strategy

    def record_literal(self) -> dict:
        """`{ field: literal | [literal, ...], ... }` — static metadata (v2)."""
        self.expect("{")
        record: dict = {}
        while not self.at("}"):
            fline = self.peek().line
            field_name = self._record_key_name()
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
                fname = self._record_key_name()
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

    def fn_decl(self, public: bool, verified: bool = False,
                endorse_origins: frozenset = frozenset()) -> FnDecl:
        line = self.expect("kw", "fn").line
        name = self.expect("ident").value
        type_params = self._type_param_list()
        self.expect("(")
        params: list[FnParam] = []
        seen_default = False
        while not self.at(")"):
            pline = self.peek().line
            pname = self._name()
            self.expect(":")
            ptype = self.type_()
            # optional default value (roadmap item 187): `= <pure expr>`. A
            # `fn` body opens with `{`, never `=`, so the `=` here is
            # unambiguous. Defaults must be trailing — a required parameter
            # after a defaulted one has no positional slot a caller could
            # reach (revl has no keyword arguments).
            default = None
            if self.at("="):
                self.next()
                default = self.pure_expr()
                seen_default = True
            elif seen_default:
                raise self.err(pline,
                               f"parameter `{pname}` has no default but follows a "
                               "defaulted parameter",
                               hint="once a parameter declares a default, every "
                                    "parameter after it must too — a required "
                                    "parameter after a defaulted one has no call "
                                    "position, since revl calls are positional")
            params.append(FnParam(pname, ptype, pline, default))
            if self.at(","):
                self.next()
        self.expect(")")
        returns = None
        if self.at("arrow"):
            self.next()
            returns = self.type_()
        self.expect("{")
        body = []
        while True:
            self._skip_semis()
            if self.at("}"):
                break
            body.append(self.fn_stmt())
        self.expect("}")
        return FnDecl(name, params, returns, body, public, line, verified,
                      source=self.filename, type_params=type_params,
                      endorse_origins=endorse_origins)

    def test_decl(self, lifecycle: bool = False) -> TestDecl:
        line = self.expect("kw", "test").line
        tok = self.expect("string")
        name = tok.value
        if not name:
            raise self.err(tok.line, "a test name cannot be empty")
        self.expect("{")
        body = []
        while True:
            self._skip_semis()
            if self.at("}"):
                break
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
    _LIFECYCLE_STMT_WORDS = ("load", "unload", "call", "advance")

    def _reject_lifecycle_stmt_here(self) -> None:
        """A lifecycle statement inside a plain `test` (or any pure body) is
        refused by name rather than by a confusing expression-parse error."""
        tok = self.peek()
        nxt = self.toks[self.pos + 1]
        word = None
        # `advance` is followed by a duration (`advance 30s`), so its lookahead
        # is an int; the others name a component/key and are followed by an ident.
        nxt_ok = nxt.kind == "ident" or (tok.value == "advance" and nxt.kind == "int")
        if tok.kind == "ident" and tok.value in self._LIFECYCLE_STMT_WORDS and nxt_ok:
            word = tok.value
        elif tok.kind == "ident" and tok.value == "abort" and nxt.kind in (
                "}", ";", "kw", "ident"):
            # `abort` (item 377) takes no operand, so it is a bare statement: it
            # is the lifecycle word only when it stands alone (next token ends
            # the body or begins another statement), never when it heads an
            # expression (`abort.foo()`, `abort(x)`, `abort + 1`) that merely
            # uses the name — those keep working.
            word = "abort"
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
                    fline = self.peek().line
                    field = self._name("a config field name")
                    self.expect(":")
                    config.append((field, self.pure_expr(), fline))
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
        if tok.kind == "ident" and tok.value == "advance":
            self.next()
            ms = self._advance_duration_ms()
            return AdvanceStmt(ms, tok.line)
        if tok.kind == "ident" and tok.value == "abort":
            self.next()
            return AbortStmt(tok.line)
        if tok.kind == "kw" and tok.value == "assert":
            self.next()
            nxt = self.peek()
            if nxt.kind == "ident" and nxt.value == "no_residue":
                self.next()
                return ResidueStmt(tok.line)
            if nxt.kind == "ident" and nxt.value == "call":
                # `assert call key.op(...) ...` (roadmap item 407). A witnessed
                # `call` is the effectful driver of the composition (it is
                # recorded for teardown and residue checking), while an `assert`
                # is a pure observation over the test's `let` bindings. Letting
                # the call be evaluated inside the assertion would hide a
                # witnessed effect from the timeline the checker walks, so it
                # stays a statement. Redirect to the one-line hoist rather than
                # letting `pure_expr` read `call` as a bare variable and fail
                # further along with an opaque "expected a lifecycle statement".
                raise self.err(
                    tok.line,
                    "a witnessed `call` cannot be evaluated inside an `assert`: "
                    "the call is an effect, the assert is a pure observation",
                    hint="hoist the call to its own step, then assert over the "
                         f"binding: `let result = {self._peek_call_suggestion()}` "
                         "then `assert result == ...` (syntax-2.0 §7.1)",
                )
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
                 "`advance` / `abort` / `assert` (syntax-2.0 §7.1)",
        )

    def _advance_duration_ms(self) -> int:
        """Parse the `<n><unit>` after `advance` (item 102) into milliseconds,
        reusing item 57's duration units. `advance` is the only lifecycle
        statement that moves the clock coeffect, so a timer's firing becomes an
        assertable timeline step (docs/time-coeffect.md §advance)."""
        num = self.peek()
        if num.kind != "int":
            raise self.err(num.line,
                           f"expected a whole-number duration after `advance`, found {num.value!r}",
                           hint="an advance is `<n><unit>`, e.g. `advance 30s` (units: ms, s, m, h, d)")
        if num.value <= 0:
            raise self.err(num.line,
                           f"an `advance` duration must be positive (found {num.value})",
                           hint="advancing the clock by zero fires nothing; give it a real span")
        self.next()
        unit_tok = self.peek()
        if unit_tok.kind != "ident" or unit_tok.value not in self._DURATION_UNITS:
            found = unit_tok.value if unit_tok.kind in ("ident", "kw") else repr(unit_tok.value)
            raise self.err(unit_tok.line,
                           f"expected a duration unit after `advance {num.value}`, found {found}",
                           hint="units are `ms`, `s`, `m`, `h`, `d` — write the advance with no "
                                f"space, e.g. `advance {num.value}s`")
        self.next()
        return num.value * self._DURATION_UNITS[unit_tok.value]

    def _peek_call_suggestion(self) -> str:
        """Best-effort `call key.op(...)` reconstruction for the item-407
        redirect, read WITHOUT consuming tokens. Falls back to a generic shape
        when the tokens after `call` do not look like `key.op(`."""
        toks = self.toks
        i = self.pos  # points at the `call` token
        if not (i < len(toks) and toks[i].kind == "ident" and toks[i].value == "call"):
            return "call key.op(...)"
        key_tok = toks[i + 1] if i + 1 < len(toks) else None
        dot_tok = toks[i + 2] if i + 2 < len(toks) else None
        op_tok = toks[i + 3] if i + 3 < len(toks) else None
        if (key_tok is not None and key_tok.kind == "ident"
                and dot_tok is not None and dot_tok.kind == "."
                and op_tok is not None and op_tok.kind == "ident"):
            return f"call {key_tok.value}.{op_tok.value}(...)"
        return "call key.op(...)"

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
                key = self._name("a config field name")
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
        while True:
            self._skip_semis()
            if self.at("}"):
                break
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
            pname = self._name("a parameter name")
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
        while True:
            self._skip_semis()
            if self.at("}"):
                break
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
        if tok.kind == "kw" and tok.value in ("break", "continue"):
            return self._loop_control_stmt(tok)
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

    def _loop_control_stmt(self, tok):
        """`break` / `continue` — bare loop control (item 379). Valid only
        inside a `while`/`for` body (`_loop_depth > 0`). Outside a loop it
        replaces today's misleading G1 "`break` is not declared" with a
        redirect in the item-384 voice; inside a match block arm (where the
        depth has been reset to 0) it names the lambda-lift that makes the
        `break` unlandable (C1, docs/design/379-break-continue.md)."""
        self.next()
        if self._loop_depth == 0:
            if self._in_block_arm:
                raise self.err(
                    tok.line,
                    f"`{tok.value}` cannot leave a match block arm "
                    "(docs/records.md §4)",
                    hint=f"a block arm is lifted into its own function, so a "
                         f"`{tok.value}` here has no enclosing loop to target; "
                         f"put the loop and its `{tok.value}` in a module `fn` "
                         f"the arm calls",
                )
            raise self.err(
                tok.line,
                f"`{tok.value}` is only valid inside a `while` or `for` body",
                hint=("return early or restructure the loop"
                      if tok.value == "break"
                      else "move the skip into the loop's header condition, or "
                           "restructure the loop"),
            )
        return (BreakStmt(tok.line) if tok.value == "break"
                else ContinueStmt(tok.line))

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
            fields.append(self._name())
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
        self._loop_depth += 1
        try:
            body = self.block() if self.at("{") else [self.fn_stmt()]
        finally:
            self._loop_depth -= 1
        return WhileStmt(cond, body, line)

    def for_stmt(self) -> ForStmt:
        line = self.expect("kw", "for").line
        self.expect("(")
        # item 384 (pairs with 379): revl's only loop header is the TS for-of
        # `for (x of xs)`. A C-style `for (let i = 0; i < n; i += 1)` opens
        # with a `let`/`var` keyword (or a bare `i = …`/`;`) where the bind
        # name is expected, and used to report the cryptic `expected ident,
        # found 'let'`. Redirect to for-of / `while` instead.
        if self.at("kw", "let") or self.at("kw", "var"):
            raise self.err(
                self.peek().line,
                "revl has no C-style `for (init; cond; step)` loop",
                hint="iterate with `for (x of xs)`, or count with a `var` and a "
                     "`while (cond)` loop (syntax-2.0 §3.5)",
            )
        bind = self.expect("ident").value
        # A bare C-style header without `let` (`for (i = 0; …)`) reaches here
        # with `=`/`;` where `of` is expected — same redirect.
        if self.at("=") or self.at(";"):
            raise self.err(
                self.peek().line,
                "revl has no C-style `for (init; cond; step)` loop",
                hint="iterate with `for (x of xs)`, or count with a `var` and a "
                     "`while (cond)` loop (syntax-2.0 §3.5)",
            )
        # `for (x in xs)` (Python / JS enumerate-keys) walks the wrong thing;
        # revl's `of` walks elements. Redirect the `in` keyword to `of`.
        if self.at("kw", "in"):
            raise self.err(
                self.peek().line,
                "revl iterates elements with `for (x of xs)`, not `for (x in xs)`",
                hint="`of` binds each element; revl has no key-enumerating "
                     "`in` loop (syntax-2.0 §3.5)",
            )
        self.expect("kw", "of")
        iterable = self.pure_expr()
        self.expect(")")
        self._loop_depth += 1
        try:
            body = self.block() if self.at("{") else [self.fn_stmt()]
        finally:
            self._loop_depth -= 1
        return ForStmt(bind, iterable, body, line)

    def if_stmt(self) -> IfStmt:
        line = self.expect("kw", "if").line
        self.expect("(")
        cond = self.pure_expr()
        self.expect(")")
        then = self.block() if self.at("{") else [self.fn_stmt()]
        # item 384: `elif` (Python) lexes as an identifier, so after the `then`
        # block it lands as a bare call-expression statement and reports a
        # cryptic error deep in the following `{...}`. Catch it at the natural
        # `else`-position and redirect to `else if`.
        if self.at("ident", "elif"):
            raise self.err(
                self.peek().line,
                "revl has no `elif`",
                hint="chain conditionals with `else if` (syntax-2.0 §3.2)",
            )
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
        while True:
            self._skip_semis()
            if self.at("}"):
                break
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
        # item 384: `a if c else b` is the Python conditional expression. An
        # `if` immediately following a parsed expression is never valid revl
        # (its `if` *statement* always opens `if (`), so an `if` here that is
        # NOT followed by `(` is the Python postfix-if. Redirect to `c ? a : b`
        # instead of the cryptic `expected (, found '<cond>'`. The `(`
        # exclusion keeps a real `if` statement on the next line — reached as a
        # separate statement, not within this expression — untouched.
        if (self.at("kw", "if")
                and self.pos + 1 < len(self.toks)
                and self.toks[self.pos + 1].kind != "("):
            raise self.err(
                self.peek().line,
                "revl has no Python-style `a if c else b` conditional expression",
                hint="revl's conditional expression is `c ? a : b` — the "
                     "condition comes first (syntax-2.0 §3.2)",
            )
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
        return self._bin(self._bor, ("&&",))

    # Bitwise `| ^ &` sit between `&&` and equality, in C/TypeScript order
    # (loosest `|`, then `^`, then `&`), so `a & b == c` parses as
    # `a & (b == c)` exactly as it does in TS — §0 keeps shared syntax meaning
    # what TS means by it (item 366, docs/arithmetic.md).
    def _bor(self):
        # `_suppress_bor` marks the one context where a top-level `|` is a
        # record-update separator, not the operator; clear it on read so it
        # suppresses only this leftmost `|` and nested `(a | b)` still parse.
        if self._suppress_bor:
            self._suppress_bor = False
            return self._bxor()
        return self._bin(self._bxor, ("|",))

    def _bxor(self):
        return self._bin(self._band, ("^",))

    def _band(self):
        return self._bin(self._eq, ("&",))

    def _eq(self):
        return self._bin(self._cmp, ("==", "===", "!=", "!=="))

    def _cmp(self):
        return self._bin(self._shift, ("<", ">", "<=", ">="))

    # The Int32 shifts bind tighter than the relational operators and looser
    # than additive — C/TypeScript order, so `a + b << c` is `(a + b) << c`
    # (item 366).
    def _shift(self):
        return self._bin(self._add, ("<<", ">>"))

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
        if tok.kind in ("!", "-", "~"):
            # `~` is the Int32 bitwise complement, grouped with the other
            # prefix unaries (item 366, docs/arithmetic.md).
            self.next()
            return ExprUn(tok.kind, self._unary(), tok.line)
        if tok.kind == "kw" and tok.value == "emit":
            self.next()
            return EmitExpr(self._unary(), tok.line)
        return self._postfix()

    def _await_approval_expr(self) -> ApprovalExpr:
        """`await approval[C] { field: expr, ... }` — parsed only as the RHS of a
        `let` binding (item 246). Not a general expression: an approval is an
        acquisition-shaped suspension, bound once and threaded by `with`."""
        line = self.expect("kw", "await").line
        self.expect("ident", "approval")
        self.expect("[")
        capability = self._capability_token("a capability token in `approval[...]`")
        self.expect("]")
        fields: list[tuple[str, object]] = []
        if self.at("{"):
            self.next()
            while not self.at("}"):
                fname = self._record_key_name()
                self.expect(":")
                fields.append((fname, self.pure_expr()))
                if self.at(","):
                    self.next()
            self.expect("}")
        return ApprovalExpr(capability, fields, line)

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
                node = ExprField(node, self._record_key_name(), node.line)
            elif self.at("?."):
                # `expr?.name` / `expr?.name(args)`: short-circuit on Opt-None.
                # Modelled as an ExprOptField / ExprOptCall so lowering can
                # emit a conditional and typing can flow Opt into inner types.
                self.next()
                name = self._record_key_name()
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
                    # item 384: `f(k=v)` is a Python keyword argument — revl
                    # calls are positional. Redirect before the stray `=`
                    # reports `expected ), found '='`.
                    if self.at("="):
                        raise self.err(
                            self.peek().line,
                            "revl has no keyword arguments — `f(k=v)` is not a call",
                            hint="pass arguments positionally, e.g. `f(v)` "
                                 "(syntax-2.0 §3.1)",
                        )
                    if self.at(","):
                        self.next()
                self.expect(")")
                node = ExprCall(node, args, node.line)
            elif self.at("["):
                if optional:
                    raise self._optional_chain_error()
                self.next()
                index = self.pure_expr()
                # item 384: `xs[a:b]` is Python slice syntax — revl indexes a
                # single element and slices with a method. Redirect the `:`
                # before it reports `expected ], found ':'`.
                if self.at(":"):
                    raise self.err(
                        self.peek().line,
                        "revl has no slice syntax `xs[a:b]`",
                        hint="take a sublist with `xs.slice(a, b)`; `[i]` indexes "
                             "one element (docs/stdlib-2.0.md)",
                    )
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

    def _endorse_expr(self) -> ExprEndorse:
        """`endorse[<origin>](<value>, reason = "...")` [`with <appr>`] — the
        scoped, reasoned declassifier (item 249, Slice C).

        The ambient single-argument `endorse(v)` of Slice A is superseded: it is
        refused here with a migration hint, so a downgrade is always scoped and
        reasoned. The `reason` string is mandatory (it lands in the audit's
        declassify record); an optional `with <appr>` threads an approval."""
        line = self.next().line  # consume `endorse`
        if not self.at("["):
            raise self.err(
                line,
                "the ambient `endorse(v)` is superseded — a declassification must "
                "name the taint class it downgrades and carry a reason",
                hint="write `endorse[<origin>](v, reason = \"...\")` (e.g. "
                     "`endorse[web](page, reason = \"operator-reviewed\")`), and "
                     "declare the slot on the enclosing `fn`/operation "
                     "(`endorse[web] fn ...`) (item 249, Slice C)")
        self.next()  # `[`
        origin = self.expect("ident", what="the taint class `endorse[<origin>]` "
                             "downgrades (e.g. `web`, `net`, `fs`)").value
        self.expect("]")
        self.expect("(", what="`(value, reason = \"...\")` after `endorse[<origin>]`")
        value = self.pure_expr()
        reason = None
        if self.at(","):
            self.next()
            self.expect("ident", "reason",
                        what="`reason = \"...\"` — the mandatory audit reason for "
                             "an `endorse` (item 249, Slice C)")
            self.expect("=")
            rtok = self.peek()
            if rtok.kind != "string":
                raise self.err(rtok.line,
                               f"`endorse` reason must be a string literal, found "
                               f"{rtok.value!r}",
                               hint="e.g. `reason = \"operator-reviewed template\"`")
            reason = self.next().value
        self.expect(")")
        approval = None
        if self.at("kw", "with"):
            self.next()
            approval = self.expect("ident", what="the `Approval[declassify.<origin>]` "
                                   "value to thread through `with` (item 249/246)").value
        if reason is None:
            raise self.err(
                line,
                "an `endorse` must carry a reason",
                hint="write `endorse[<origin>](v, reason = \"...\")` — the reason "
                     "is recorded on the audit's declassify surface (item 249)")
        return ExprEndorse(origin, value, reason, line, approval)

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

    def _if_expr(self) -> ExprIf:
        """`if (cond) { then } else { otherwise }` in EXPRESSION position — a
        block-bodied conditional whose value is the taken branch's final
        expression (item 196). It is the block-bodied twin of the ternary
        `cond ? then : otherwise` and lowers to the very same ExprIf node, so
        both branches are required (an expression-if needs an `else`) and must
        agree in type, which the checker enforces on ExprIf exactly as it does
        for the ternary. Statement-position `if` (no `else`, side-effecting
        body) is dispatched in `fn_stmt`/`component_if` before `_primary` is
        ever reached, so it keeps its existing semantics untouched."""
        line = self.expect("kw", "if").line
        self.expect("(")
        cond = self.pure_expr()
        self.expect(")")
        then = self._if_branch_expr()
        if not self.at("kw", "else"):
            tok = self.peek()
            raise self.err(
                tok.line,
                f"an `if` used as an expression needs an `else`, found {tok.value!r}",
                hint="an expression must produce a value on every path; write "
                     "`if (c) { a } else { b }` (or use a statement `if` for a "
                     "side-effecting body)",
            )
        self.next()
        otherwise = self._if_branch_expr()
        return ExprIf(cond, then, otherwise, line)

    def _if_branch_expr(self):
        """A `{ expr }` branch of an expression-position `if`: one value
        expression wrapped in braces. Keeping the branch a single expression
        (not a statement block) is what makes the whole `if` carry the same
        node shape as a ternary operand, so no backend needs new emit support."""
        self.expect("{")
        self._skip_semis()
        value = self.pure_expr()
        self._skip_semis()
        self.expect("}")
        return value

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
        block (docs/records.md §4) or record value?

        The only `{...}` an arm body can otherwise be is a record: a record
        *literal* always opens `ident :` (or is the empty `{}`), and a record
        *update* opens `base | ...`. Anything else — a `let`/`var`/`if`/`while`/
        `for`/assignment, or a bare tail expression — is a statement block."""
        first = self.toks[self.pos + 1]  # token after the `{`
        if first.kind == "}":
            return False  # empty record literal
        if self._record_update_ahead():
            return False  # `{ base | f = e }`
        if first.kind == "ident" and self.toks[self.pos + 2].kind == ":":
            return False  # `{ field: value, ... }`
        return True

    def _match_block_arm(self, line: int):
        """`{ stmt* tail }` — a statement-block arm (docs/records.md §4).

        The block is parsed like a normal fn/block body: any fn-body statement
        may precede the trailing expression, whose value is the arm's value.
        `return` is rejected here — the arm yields its final expression, and
        lowering lambda-lifts the block into a helper fn where a `return` would
        silently mean something else (early return from the helper, not the
        enclosing function)."""
        self.expect("{")
        # C1 (item 379): the block is lambda-lifted into a helper fn at
        # lowering, so a `break`/`continue` targeting a loop *outside* the arm
        # would land in a loopless fn. Reset the loop-depth context to 0 for the
        # arm body so such a jump is refused at parse time (in the block-arm
        # voice); a loop written *inside* the arm restores a positive depth for
        # its own `break` (docs/design/379-break-continue.md).
        saved_depth, saved_in_arm = self._loop_depth, self._in_block_arm
        self._loop_depth, self._in_block_arm = 0, True
        stmts = []
        try:
            while True:
                self._skip_semis()
                if self.at("}"):
                    break
                stmts.append(self.fn_stmt())
        finally:
            self._loop_depth, self._in_block_arm = saved_depth, saved_in_arm
        self.expect("}")
        if not stmts:
            raise self.err(line,
                           "a match block arm must end in an expression (its "
                           "value) (docs/records.md §4)")
        for stmt in stmts:
            if isinstance(stmt, ReturnStmt):
                raise self.err(stmt.line,
                               "a match block arm yields its final expression, not "
                               "`return` (docs/records.md §4)")
        tail = stmts[-1]
        if not isinstance(tail, ExprStmt):
            raise self.err(getattr(tail, "line", line),
                           "a match block arm must end in an expression (its "
                           f"value), found `{tail.__class__.__name__}` "
                           "(docs/records.md §4)")
        return ExprBlockArm(stmts[:-1], tail.expr, line)

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
        # item 384: a Python `lambda x: …` lexes as the identifier `lambda`
        # juxtaposed with a param name (or a bare `:`), a shape revl never has
        # (no two identifiers abut, and `:` cannot follow an expression here).
        # Redirect to the arrow `x => …` before the `:` reports the cryptic
        # `expected an expression, found ':'`. Guarded on the juxtaposition so
        # a value legitimately referenced as `lambda` (never followed by an
        # ident or `:` in expression position) is untouched.
        if (tok.kind == "ident" and tok.value == "lambda"
                and self.pos + 1 < len(self.toks)
                and self.toks[self.pos + 1].kind in ("ident", ":")):
            raise self.err(
                tok.line,
                "revl has no `lambda`",
                hint="an anonymous function is an arrow `x => …` "
                     "(e.g. `xs.map(x => x + 1)`) (syntax-2.0 §3.2)",
            )
        # item 249 Slice C: the scoped declassifier `endorse[<origin>](v, reason
        # = "...")`. `endorse` is an ident, intercepted before the plain
        # variable-reference path when it heads a `[` or `(` (its call/scope
        # forms); a bare `endorse` used as a value name is untouched.
        if (tok.kind == "ident" and tok.value == "endorse"
                and self.pos + 1 < len(self.toks)
                and self.toks[self.pos + 1].kind in ("[", "(")):
            return self._endorse_expr()
        if self._is_name_tok(tok):
            # item 158: a variable *reference* is a name position too — a param
            # (or record field) named with a contextual noun must be usable, and
            # none of the five nouns can lead an expression (their keyword roles
            # are all dispatched or `expect`ed before `_primary` is ever reached
            # for a leading token), so reading one here is unambiguous.
            self.next()
            if self.at("=>"):
                self.next()
                return ExprArrow([tok.value], self._arrow_body(), tok.line, [None])
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
                    params.append(self._name())
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
                return ExprArrow(params, self._arrow_body(), tok.line, param_types)
            self.next()
            node = self.pure_expr()
            # item 384: `(a, b)` is a Python/JS tuple — revl has no tuple type.
            # (An arrow's `(x, y) => …` param list was already dispatched by
            # `_arrow_params_ahead`, so a comma here is a value tuple.) Redirect
            # to a named record instead of the cryptic `expected ), found ','`.
            if self.at(","):
                raise self.err(
                    self.peek().line,
                    "revl has no tuples — `(a, b)` is not a value",
                    hint="group values in a record with named fields, e.g. "
                         "`{ first: a, second: b }` (syntax-2.0 §2)",
                )
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
                # The top-level `|` here separates base from updates; suppress
                # its reading as bitwise OR (item 366). A parenthesised `|`
                # inside the base is unaffected — `_bor` clears the flag on read.
                self._suppress_bor = True
                base = self.pure_expr()
                self._suppress_bor = False
                self.expect("|")
                updates = []
                while not self.at("}"):
                    fname = self._record_key_name()
                    self.expect("=")
                    updates.append((fname, self.pure_expr()))
                    if self.at(","):
                        self.next()
                self.expect("}")
                return ExprRecordUpdate(base, updates, tok.line)
            self.next()
            fields = []
            while not self.at("}"):
                # item 384: a string-keyed literal `{"k": v}` is a Python/JS
                # dict; revl records take bare identifier keys. Redirect to
                # ident keys or `Map` instead of `expected ident, found 'k'`.
                if self.at("string"):
                    raise self.err(
                        self.peek().line,
                        "revl records use identifier keys, not string keys "
                        "like `{\"k\": v}`",
                        hint="write `{ k: v }` with a bare-identifier key, or use "
                             "`Map.new()` for dynamic string keys (docs/records.md, "
                             "docs/stdlib-2.0.md)",
                    )
                fname = self._record_key_name()
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
                # item 383: `[x for x in xs]` is a comprehension — a shape revl
                # does not have. Without this the loop re-enters `pure_expr` on
                # the `for` keyword and reports the cryptic `expected an
                # expression, found 'for'`; redirect to the spelling that works.
                if self.at("kw", "for"):
                    raise self.err(
                        self.peek().line,
                        "revl has no list comprehensions",
                        hint="use `xs.map(x => …)` / `xs.filter(x => …)` "
                             "(with `use \"stdlib/list.rvl\"`), or a "
                             "`for (x of xs) { … }` loop that pushes onto a "
                             "`var` list")
                if self.at(","):
                    self.next()
            self.expect("]")
            return ExprList(items, tok.line)
        if tok.kind == "kw" and tok.value == "match":
            return self._match_expr()
        if tok.kind == "kw" and tok.value == "if":
            return self._if_expr()
        self._reject_incr_decr(tok)  # item 384 / syntax-2.0 §3.3
        raise self.err(tok.line, f"expected an expression, found {tok.value!r}")

    def _reject_foreign_keyword(self, tok) -> None:
        """item 384: a known-foreign statement/declaration keyword (`def`,
        `throw`, `elif`, `lambda`, `const`, `print`) lexes as an identifier and
        reaches a statement/declaration dispatch as an error. Redirect it to
        the revl spelling instead of the cryptic `expected a … found '<kw>'`."""
        if tok.kind == "ident":
            hit = _FOREIGN_STMT_KEYWORDS.get(tok.value)
            if hit is not None:
                raise self.err(tok.line, hit[0], hint=hit[1])

    def _reject_incr_decr(self, tok) -> None:
        """item 384 / syntax-2.0 §3.3: `i++` / `i--` lex as two adjacent
        `+`/`-` tokens — revl has no in/decrement, expressions are pure. This
        fires ONLY on the `_primary` error path, which no valid program
        reaches, so it is false-positive-free. `++` strands a `+` in operand
        position (`_unary` has no unary `+`), so the erroring token IS a `+`
        with a `+` on one side; postfix `--` is consumed as `x - (- <next>)`,
        so the erroring token sits just past a `- -` pair. Either way the
        author wanted mutation: redirect to the §3.3-promised `+= 1` / `-= 1`
        instead of the cryptic `expected an expression, found '+'`."""
        toks, i = self.toks, self.pos
        sign = None
        if tok.kind == "+" and (
                (i > 0 and toks[i - 1].kind == "+")
                or (i + 1 < len(toks) and toks[i + 1].kind == "+")):
            sign = "+"
        elif i >= 2 and toks[i - 1].kind == "-" and toks[i - 2].kind == "-":
            sign = "-"
        if sign is None:
            return
        word = "increment" if sign == "+" else "decrement"
        # postfix `--` errors on the token PAST the operator pair; point the
        # diagnostic back at the operator itself.
        line = toks[i - 1].line if sign == "-" else tok.line
        raise self.err(
            line,
            f"revl has no `{sign}{sign}` {word} operator — expressions are pure "
            "(syntax-2.0 §3.3)",
            hint=f"mutate a `var` with `{sign}= 1` (write `i {sign}= 1`), inside "
                 "a `while`/`for` loop body",
        )

    def _arrow_body(self):
        """The body of an arrow `=> …`. A closure body is a single pure
        *expression*; revl has no statement-block arrow, so a value returned
        by a closure is computed, never assigned into an enclosing cell.

        The one shape that reads as an *attempt* at reference capture —
        `(…) => { name = … }`, a closure writing a name bound in an enclosing
        scope — is refused here with an explicit diagnostic (roadmap item 129,
        docs/closures.md) instead of the incidental record-literal parse error
        (`{ name =` is not a record: records key with `:`, updates with `|`).

        revl closures capture strictly BY VALUE (syntax-2.0 §3.5): they
        snapshot the values they read. That is not an ergonomic choice — it is
        what keeps the value-semantic equality the derived LIFO teardown (G7)
        and no-residue containment (A8) rest on. An inverse the teardown
        accumulator holds closes over the *values* the forward effect used; a
        closure that could write a live mutable cell would let those values
        shift out from under the inverse, and the recovery-exactness proof
        would lose its subject. So there is no shared mutable environment to
        write through, and the write form is rejected rather than snapshotted."""
        if self.at("{"):
            nxt = self.toks[self.pos + 1]
            after = self.toks[self.pos + 2]
            compound = (after.kind in ("+", "-", "*", "/", "%")
                        and self.toks[self.pos + 3].kind == "=")
            if self._is_name_tok(nxt) and (after.kind == "=" or compound):
                raise self.err(
                    nxt.line,
                    f"a closure cannot assign to `{nxt.value}`: captures are "
                    "by value, not by reference (G6)",
                    hint="a revl closure snapshots the values it reads "
                         "(syntax-2.0 §3.5, docs/closures.md) — there is no "
                         "shared mutable cell to write through. Return the "
                         f"computed value, or mutate the `var` `{nxt.value}` in "
                         "the enclosing `fn` body, not inside the closure.",
                )
        return self.pure_expr()

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
            self._is_name_tok(head) and self.toks[i + 2].kind in (",", ")", ":")
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
                params.append(self._name())
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
                while True:
                    self._skip_semis()
                    if self.at("}"):
                        break
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
