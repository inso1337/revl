"""Checking and lowering: typed AST -> backend IR (docs/backend-ir.md).

Guarantee enforcement map (DESIGN.md §4):
  G1 undeclared access        -> _lower_postfix name resolution
  G2 provision disjointness   -> link()
  G3 dependency cycles        -> link()
  G4 inverse-or-emit          -> parser (missing undo) + emission checks here
  G5 no teardown registration -> by construction (undo is an expression;
                                 there is no syntactic position for effects)
  G6 purity outside effects   -> by construction (statement grammar)
  A2 no acquisition after provide -> check_component body scan
"""

from __future__ import annotations

import dataclasses
import keyword
import os
import re

from . import holes
from .errors import RevlError, RevlErrors
from .why import CHAIN, SET, TraceStep, WhyTrace
from .typecheck import (
    CASES_KEY,
    FN_HEAD,
    FNS_KEY,
    _SIZED_HEADS,
    check_ast,
    collect_tparams,
    validate_explicit_tparams,
    render_type,
    mark_tparams,
    check_type_wellformed,
    check_config_field_is_data,
    compatible,
    format_type,
    host_check,
    _HOST_FAMILIES,
    _HOST_RESULT_SIG,
    infer_ast,
    infer_ir,
    mismatch,
    pin_hole,
    null_error,
    parse_type,
    POISON,
    structural_fields,
    substitute,
    unify,
    _is_wildcard,
    _check_method_namespace_disjoint,
)
from .taint import extract_and_normalize, check_taint, splice_declassifiers
from .resources import (
    NO_HANDLE_RETURNS,
    acquire_return_is_nominal_handle,
    closing_ops,
    resource_in,
    resource_taint,
)
from .parser import (
    _describe_expr,
    AbortStmt,
    AdvanceStmt,
    AssertStmt,
    AssignStmt,
    AwaitStmt,
    BreakStmt,
    CallStmt,
    ComponentDecl,
    ContinueStmt,
    EffectStmt,
    EmitExpr,
    EmitStmt,
    FailStmt,
    ExprArrow,
    ExprBin,
    ExprBlockArm,
    ExprCall,
    ExprEndorse,
    ExprField,
    ExprHole,
    ExprIf,
    ExprIndex,
    ExprList,
    ExprLit,
    ExprMatch,
    ExprOptCall,
    ExprOptField,
    ExprRecord,
    ExprRecordUpdate,
    ExprStmt,
    ExprUn,
    ExprVar,
    FnDecl,
    ForStmt,
    HandoffStmt,
    IfStmt,
    Interp,
    InterceptStmt,
    IsolateStmt,
    LeaseAcquire,
    LetApprovalStmt,
    LetEffect,
    LetPatternStmt,
    LetStmt,
    LoadStmt,
    ListPattern,
    Lit,
    Postfix,
    Program,
    ProvideStmt,
    RecordPattern,
    ResidueStmt,
    ReturnStmt,
    RouteStmt,
    ServiceDecl,
    SpawnExpr,
    TestDecl,
    TimerStmt,
    TypeDecl,
    UnloadStmt,
    WhileStmt,
    LIST_TRANSFORMS,
    desugar_list_transform,
)

IR_VERSION = 1

# item 384: foreign-construct redirect table (name-resolver half).
#
# A large share of first-try authorship failures used a known-foreign idiom
# that LEXES as an ordinary identifier — `and`/`or`/`not`, `True`/`False`,
# `const`, `len(...)`, `print`, `throw` — and so fell through to the generic
# G1 "`X` is not declared … declare it with `let`/`var`". That message
# ACTIVELY MISLEADS: it tells the author to declare a variable when the real
# fix is a different construct. This table implements syntax-2.0 §0/§10's
# exclusion-diagnostic philosophy (already done for `null`/`class`/`switch`/
# `let`-reassign) for the identifier-shaped holes: when an undeclared name is
# a known foreign idiom, the resolver names the idiom and the revl spelling
# instead of emitting G1.
#
# PRECISION: the redirect fires ONLY on an *undeclared* name. None of these
# is a revl keyword, so an author who genuinely binds one (`let and = …`,
# `fn len(...) { … }`) shadows the entry and never sees the redirect — the
# resolver reaches this table only after `scope`/`callables` lookup has
# already missed. So no valid revl program changes behaviour; only the
# message on an already-failing program does. Each value is (message, hint).
_FOREIGN_NAME_REDIRECTS = {
    "and": ("`and` is not a revl operator",
            "use `&&` for boolean conjunction (syntax-2.0 §3.2)"),
    "or": ("`or` is not a revl operator",
           "use `||` for boolean disjunction (syntax-2.0 §3.2)"),
    "not": ("`not` is not a revl operator",
            "use the prefix `!` for boolean negation (syntax-2.0 §3.2)"),
    "True": ("revl booleans are lowercase",
             "use `true`, not `True` (syntax-2.0 §3.2)"),
    "False": ("revl booleans are lowercase",
              "use `false`, not `False` (syntax-2.0 §3.2)"),
    "const": ("revl has no `const`",
              "use `let` (single-assignment) or `var` (mutable) (syntax-2.0 §3.5)"),
    "len": ("revl has no `len(...)`",
            "a list's length is `xs.length()` (docs/stdlib-2.0.md)"),
    "print": ("revl has no `print`",
              "pure code has no I/O — emit output through a service effect "
              "(syntax-2.0 §4)"),
    "throw": ("revl has no `throw`",
              "a pure function returns a `Result`; a component activation body "
              "signals failure with `fail` (syntax-2.0 §3.3, §4b.5)"),
    "def": ("revl has no `def`",
            "a function is declared with `fn` (syntax-2.0 §3.1)"),
    "lambda": ("revl has no `lambda`",
               "an anonymous function is an arrow `x => …` (syntax-2.0 §3.2)"),
    "elif": ("revl has no `elif`",
             "chain with `else if` (syntax-2.0 §3.2)"),
}


def _reject_foreign_name(name, filename, line):
    """If `name` is a known foreign idiom that lexes as an identifier, raise
    the specific redirect instead of the generic, misleading G1 (item 384).
    Returns None (and the caller falls through to G1) for any other name."""
    hit = _FOREIGN_NAME_REDIRECTS.get(name)
    if hit is not None:
        message, hint = hit
        raise RevlError(filename, line, message, hint=hint)


# finding 6: the specified stdlib surface (docs/stdlib-2.0.md). Method calls
# on values must name one of these (arity-checked); everything else is a
# compile error — never a verbatim pass-through to whatever the host object
# happens to have. Names are chosen to be collision-free with the v1 host
# stub objects (open/close/query/execute/new/get/insert/remove/drop).
_BUILTIN_METHODS = {
    "length": 0, "push": 1, "slice": 2, "charAt": 1,
    "charCodeAt": 1, "codepoint_at": 1, "indexOf": 1, "concat": 1,
    "split": 1, "join": 1, "repeat": 1,
    # The prefix/suffix probes (FR-6, docs/stdlib-2.0.md §Str.startsWith):
    # Str-only, one Str argument.
    "startsWith": 1, "endsWith": 1,
    # Single-character ASCII classification (item 233, docs/stdlib-2.0.md
    # §Str.is_alnum): Str-only, no argument. Cuts the self-host lexer's
    # per-byte revl-fn-call + code-point-range tax to a native inline test.
    "is_alnum": 0, "is_digit": 0, "is_alpha": 0, "is_space": 0,
    # Integer division and modulo. `/` and `%` keep the meaning TypeScript
    # gives them (§0); these name what they do, so no tier has to guess and
    # none can quietly pick its host's convention (docs/arithmetic.md).
    "div_trunc": 1, "div_floor": 1, "div_euclid": 1, "mod": 1,
    # Int/Int32 width conversions (docs/arithmetic.md). `to_int` widens an
    # Int32 to Int (lossless); `to_int32` narrows an Int to Int32 (checked,
    # traps out of the 32-bit range).
    "to_int": 0, "to_int32": 0,
    # The total forms (docs/arithmetic.md): a zero divisor is the *point*
    # here, so these are deliberately absent from _DIVIDES_BY below — a
    # literal zero argument is refused for the faulting operations only.
    "checked_div_trunc": 1, "checked_div_floor": 1,
    "checked_div_euclid": 1, "checked_mod": 1,
    # The Map value type (docs/stdlib-2.0.md §Map). Names disjoint from the
    # host verb set (open/close/query/execute/new/get/insert/remove/drop):
    # the two method namespaces stay collision-free by construction.
    "set": 2, "lookup": 1, "has": 1,
    # The iteration/remove step (docs/stdlib-2.0.md §Map): same namespace
    # discipline — disjoint from the host verb set by construction.
    "size": 0, "keys": 0, "remove": 1,
    # The rendering builtin (docs/stdlib-2.0.md §Int.to_str).
    "to_str": 0,
    # The Value dot-method accessors (roadmap item 189): receiver-first sugar
    # for stdlib/value.rvl's `value_*` free functions. `.field(k)` takes one
    # Str; `.str()`/`.list()`/`.keys()` take none (`keys` arity already set by
    # the Map row above — the two share the name, disambiguated by the receiver
    # type at lower time). Each is rewritten to a plain CALL of its `value_*`
    # equivalent below (`_VALUE_ACCESSORS`), so it emits byte-identically to the
    # nested free-function form and needs NO new IR expr-kind.
    "field": 1, "str": 0, "list": 0,
}

# The Value dot-accessor method -> the `value_*` free function it desugars to
# (roadmap item 189, stdlib/value.rvl). `node.field("k").str()` lowers to the
# SAME call IR as `value_str(value_field(node, "k"))`, so it is pure sugar: the
# emitted code is byte-identical on every tier value.rvl runs on, with zero
# per-backend work (the existing call-rendering path handles all six tiers).
_VALUE_ACCESSORS = {
    "field": "value_field", "str": "value_str",
    "list": "value_list", "keys": "value_keys",
}

# The disjointness this comment block promises is a *checked* claim, enforced
# at module load (table-edit time), not at golden-diff time: editing this
# table with a name from the host stub surface reclassifies every host call
# site of that name and surfaces only as broken tests across the suite
# (roadmap 75(b) tooling half; dogfood/findings-mapiter.md §2). `remove` is
# the ONE sanctioned overlap (docs/stdlib-2.0.md §Map) — dispatch by receiver
# kind is what makes it safe.
_check_method_namespace_disjoint(_BUILTIN_METHODS, "_BUILTIN_METHODS")


# item 246: a reserved key on the `types` table (like FNS_KEY/CASES_KEY) carrying
# the declaration-owned approval facts so `_lower_emit_step` can consult them
# without a signature change to `_lower_component`. `{"required": {cap, ...},
# "externs": {name: entry}}`.
APPROVAL_KEY = "__approval__"


def _approval_index(externs: list) -> dict:
    """The declaration-owned approval facts (item 246). `required` is the set of
    capability tokens whose crossing needs a covering `with e` edge — a host
    emission extern contributes its NAME (the token the G8 audit and the boundary
    policy already use for it). `externs` indexes the lowered entries so a single
    emit's crossed capabilities can be resolved at the crossing site."""
    by_name = {e["name"]: e for e in externs}
    required = {e["name"] for e in externs if e.get("requires_approval")}
    return {"required": required, "externs": by_name}


def _approval_covers(scope: str, token: str) -> bool:
    """Whether an `Approval[scope]` covers a crossing of capability `token`:
    `token` is within `scope`'s reach. Exact match, or a glob scope
    (`prod.*`) matching the token (Decision 3, `C within C'`'s scope)."""
    from fnmatch import fnmatchcase  # noqa: PLC0415 — stdlib, only on a crossing
    return scope == token or fnmatchcase(token, scope)


def _emit_crossed_caps(node: dict, env: "Env") -> list:
    """The capability token(s) a single `emit <call>` crosses. A req-target
    service emission contributes the method's `emission[...]` scope (or `*` when
    bare); a direct host emission extern contributes its name (or its declared
    scope). The same tokens the G8 boundary surface names, resolved for ONE
    crossing so the approval obligation is per-crossing, not per-method."""
    kind = node.get("kind")
    target = node.get("target")
    if kind == "call" and isinstance(target, dict) and target.get("kind") == "req":
        service = env.requires.get(target.get("name"))
        svc = env.services.get(service) if service else None
        spec = svc.methods.get(node.get("method")) if svc is not None else None
        caps = getattr(spec, "capabilities", None) if spec is not None else None
        return list(caps) if caps else ["*"]
    # a direct host-extern emission call: `{"kind": "fn", "name": <extern>}`.
    if kind == "fn":
        index = (env.types.get(APPROVAL_KEY) or {}).get("externs") or {}
        entry = index.get(node.get("name"))
        if entry is not None and entry.get("class") == "emission":
            return list(entry.get("capabilities") or [node.get("name")])
    return []


# Integer division and modulo are undefined at zero, and every tier says so
# differently — python raises, rust and wasm trap, java throws, and TypeScript
# used to hand back Infinity. A *literal* zero divisor is never a program
# anyone meant to write, so it does not need to reach any of them.
_DIVIDES_BY = ("div_trunc", "div_floor", "div_euclid", "mod")


def _refuse_zero_divisor(method: str, args, filename: str, line: int) -> None:
    from .parser import ExprLit as _Lit

    if method not in _DIVIDES_BY or not args:
        return
    divisor = args[0]
    if isinstance(divisor, _Lit) and divisor.value == 0:
        raise RevlError(
            filename, line,
            f"`{method}` by a literal zero is undefined",
            hint="integer division and modulo have no value at zero; guard the "
                 "divisor (`if (b == 0) { ... }`) or use a non-zero constant")


def _refuse_unpinned_stdlib_method(method: str, recv_t: str | None,
                                   filename: str, line: int) -> None:
    """The stdlib-named-method sliver (roadmap 75(b), docs/contract-errata.md
    "Typing gaps"): a method call on a receiver whose type no constructor pins
    must not lower as the builtin. At runtime the value is whatever the host
    returned (or whatever the type parameter was instantiated with), not a
    Str/List/Int/Int32/Bytes/Map value, so the builtin dispatch misbehaves on
    every tier. Only a receiver the checker can *prove* is a stdlib value may
    take the builtin table; everything else stays on the G8 audit surface,
    refused here with the same HOST-METHOD diagnostic the family surface uses."""
    shown = render_type(recv_t) or "unknown"
    raise RevlError(
        filename, line,
        f"stdlib method `{method}` on a value of {shown} type — no constructor "
        "pins this receiver as a Str/List/Int/Int32/Bytes/Map value, so the "
        "checker refuses to lower the call as the builtin",
        hint="host-object results carry no type (G8): annotate the binding "
             "(`let v: Str = ...`) or route the value through a declared type "
             "before calling methods on it",
        code="HOST-METHOD", category="host-boundary",
    )


def _is_host_valued(expr, scope) -> bool:
    """A receiver holding a HOST object (Map.new(), Pool.open(...), or a let
    bound to one): its methods belong to the host stub, not the stdlib
    table, and stay verbatim."""
    from .parser import ExprCall as _C, ExprField as _F, ExprVar as _V

    if isinstance(expr, _V):
        return scope.get(expr.name) == "host"
    if isinstance(expr, _C) and isinstance(expr.callee, _F)             and isinstance(expr.callee.target, _V):
        # `Map.empty()` is the Map VALUE constructor (docs/stdlib-2.0.md
        # §Map) — a pure value, not a host acquisition. Its constructor ROOT
        # is a host callable, so without this exclusion a `let m =
        # Map.empty()` bound the name as "host" and every later
        # m.set/m.lookup/m.has lowered as a verbatim *field* call: unchecked
        # at compile time, AttributeError-shaped at runtime.
        if expr.callee.target.name == "Map" and expr.callee.name == "empty":
            return False
        return expr.callee.target.name in _HOST_CALLABLES
    return False
IR_VERSION_V2 = 2  # emitted only when a compiled component uses realms/interception
IR_VERSION_V3 = 3  # emitted when a program uses full-language features (fn/type)

# ── IR expression-kind schema (roadmap item 76a) ─────────────────────────────
# The complete set of expression kinds this frontend can lower, split by the
# positions in which each can appear. This is the *registration point* for a
# new expression kind: adding one to the lowering MUST add it to the position
# set(s) that match where the frontend can produce it — which automatically
# adds it to EXPR_KINDS — and then
# tests/test_expr_dispatcher_conformance.py fails until every backend declares
# where it handles or deliberately refuses the kind in each dispatcher. A kind
# that ships without a registration is exactly the "patched one of two paths"
# failure the test exists to turn red.
#
#   FN        — a pure-function body (``_lower_pure_expr``).
#   COMPONENT — a component body / provide-method body / block-effect setup
#               (``_lower_component_pure_expr`` + component step lowering).
#
# The split is factual, not aspirational: `len` and `interp` are produced only
# in fn bodies (component positions spell `.length` as a `field` and templates
# as `format`), while `name`/`config`/`req`/`host`/`format`/`fn`/`spawn`/
# `instance-get` are produced only in component positions. Everything else can
# appear in both.
EXPR_KINDS_FN: frozenset[str] = frozenset({
    "adt", "arrow", "bin", "builtin", "call", "field", "hole", "if", "index",
    "interp", "len", "list", "lit", "maplit", "match", "optcall", "optfield",
    "record", "record_update", "un", "var",
})
EXPR_KINDS_COMPONENT: frozenset[str] = frozenset({
    "adt", "arrow", "bin", "builtin", "call", "config", "field", "fn",
    "format", "hole", "host", "if", "index", "instance-get", "list", "lit",
    "maplit", "match", "name", "optcall", "optfield", "record",
    "record_update", "req", "spawn", "un", "var",
})
EXPR_KINDS: frozenset[str] = EXPR_KINDS_FN | EXPR_KINDS_COMPONENT

# the default shared realm (paper Def. 28: an unisolated key resolves to
# its own realm); rendered as "shared" in diagnostics
SHARED_REALM = ""

# multi-realm require routing strategies (roadmap item 162). The name is an
# annotation the runtime router (item 161) consumes to pick a distribution
# policy across the bound realms; the frontend validates it against this
# closed set so a typo (`strategy(round_robbin)`) is a compile-time refusal,
# not a silent runtime fallback. `None` (strategy omitted) records "router's
# default" and is always accepted.
KNOWN_STRATEGIES: frozenset[str] = frozenset({
    "round_robin",   # rotate across the realms in declaration order
    "least_loaded",  # route to the realm whose provider reports least load
    "random",        # uniform random pick
    "sticky",        # pin a caller-derived key to one realm (session affinity)
})

# key namespacing (docs/namespacing.md): a provision key may be written
# `ns::local`. The *full* string is the key's wiring identity — G2
# disjointness, injection resolution and the admission gate all compare the
# qualified string, so two authors' `acme::db` and `bcorp::db` never collide.
# The trailing segment is the code-facing binding name a `requires` introduces
# (the consumer still writes `db.query(...)` in its body). An unqualified key
# has an empty namespace and a binding equal to itself, so v1 programs are
# unaffected in every respect.
KEY_NAMESPACE_SEP = "::"


def _key_binding(key: str) -> str:
    """The code-facing local name of a (possibly namespaced) provision key.

    `acme::db` binds `db`; an unqualified `db` binds itself. This is the name
    a `requires` clause introduces into the component body and type env."""
    return key.rsplit(KEY_NAMESPACE_SEP, 1)[-1]

# A3: identifiers that must never appear verbatim in emitted code on either
# host. Python keywords come from the keyword module; the rest is a curated
# union of TS reserved words and backend-adapter names.
#
# item 406 (cross-tier consistency): every name the TS emitter reserves for its
# own scaffolding (backends/typescript/emit.py `EMITTER_RESERVED` = ctx, config,
# rawConfig, host, Context) is renamed HERE, at the tier-agnostic frontend, so a
# user binding of one of them is made host-safe once and compiles uniformly on
# every tier. `ctx`/`config` were always in this set; `rawConfig`/`host`/
# `Context` were not, so a component/fn binding one of them (e.g. `let host = …`,
# as selfhost/emit_java.rvl itself does) type-checked and ran on py/rust/java/go/
# wasm but died LATE at TS emit with "collides with emitter scaffolding". They
# are the cross-tier analogue of the py emitter's own reserved bare-names, made
# to compile by item 160's aliasing rather than refused: a name that only a
# backend's scaffolding claims is renamed, never rejected, because it is a
# perfectly ordinary identifier the author is entitled to use.
_HOST_RESERVED = {
    "ctx", "config", "frame", "fiber", "self",
    "rawConfig", "host", "Context",
    "function", "var", "let", "const", "new", "class", "this", "typeof",
    "delete", "in", "of", "instanceof", "void", "export", "default",
    "require", "module", "exports", "import", "yield", "async", "await",
    "true", "false", "null", "undefined", "NaN",
}


def _safe_name(name: str, taken: set[str]) -> str:
    candidate = name
    while (
        candidate in _HOST_RESERVED
        or keyword.iskeyword(candidate)
        or keyword.issoftkeyword(candidate)
        or candidate in taken
    ):
        candidate += "_"
    return candidate


class Env:
    def __init__(self, component: ComponentDecl, services: dict[str, ServiceDecl], filename: str,
                 types: dict | None = None):
        self.component = component
        self.services = services
        self.filename = filename
        self.types = types or {}
        # names whose call reaches an irreversible host effect (set by
        # check_and_lower once externs/fns are lowered)
        self.emitting_fns: set = set()
        # the same relation refined to *which* boundaries each one reaches:
        # name -> capability set (docs/capabilities.md)
        self.emitting_caps: dict[str, set] = {}
        # why-trace support for the above (why.py); None when unavailable,
        # in which case rejections carry no derivation but are unchanged
        self.emission_evidence: "_EmissionEvidence | None" = None
        # names of `witnessed`-classified externs in scope (item 243, Slice 2,
        # docs/design/243-witnessed-externs.md): set by `_lower_component` so
        # an effect-position acquisition calling one of these lowers to the
        # transactional accumulator entry instead of an ordinary bracket.
        self.witnessed_externs: set = set()
        # async externs in scope (roadmap item 80): name -> its ExternDecl, so
        # the v1 coloring check can name/locate an async extern a method reaches
        # (docs/design/async-extern.md §3). Set alongside the emitting sets.
        self.async_externs: dict = {}
        # the phase-2 async-colored set (async externs + transitively colored
        # fns, docs/design/async-extern.md §3): what a provide-method admission
        # tests membership against. Set alongside `async_externs`.
        self.async_callables: set = set()
        # component-body type environment: safe-name -> type, plus the
        # "config.<field>" and "req.<local>" markers infer_ir resolves
        self.type_env: dict[str, str] = {}
        for cfg_field in component.config:
            self.type_env[f"config.{cfg_field.name}"] = cfg_field.type
        self.config_fields = {f.name for f in component.config}
        self.requires = dict()  # binding (local name) -> service name
        # binding -> the qualified wiring key it resolves against; for an
        # unqualified requirement this is the binding itself (docs/namespacing.md)
        self.require_keys: dict[str, str] = dict()
        # item 296: alias token carry-over. binding -> the consumer-facing
        # capability tokens an emission crossing through this alias contributes
        # (from the `carrying(...)` clause). Empty for every ordinary require,
        # so emission attribution is byte-identical for programs that do not
        # use the feature.
        self.require_carry: dict[str, tuple[str, ...]] = dict()
        _carry_src = getattr(component, "require_carry", None) or {}
        for key, svc, line in component.requires:
            if svc not in services:
                raise RevlError(filename, line, f"unknown service `{svc}` in `requires` of {component.name}")
            binding = _key_binding(key)
            if binding in self.requires:
                raise RevlError(filename, line, f"duplicate requirement name `{binding}` in {component.name}")
            self.requires[binding] = svc
            self.require_keys[binding] = key
            self.type_env[f"req.{binding}"] = svc
            carried = _carry_src.get(key) or _carry_src.get(binding)
            if carried:
                self.require_carry[binding] = tuple(carried)
        self.locals: dict[str, str] = {}  # surface name -> host-safe IR name (A3)
        self.params: dict[str, str] = {}
        self._taken: set[str] = set()
        # host provenance (docs/stdlib-2.0.md §Map): component locals bound to
        # a host acquisition (`let store = effect Map.new()`), mapping the
        # host-safe IR name to the host family it belongs to (`Map`/`Pool`/
        # `Job`). Their method calls belong to the host stub surface and stay
        # verbatim — checked BEFORE the stdlib builtin table, so a value-type
        # method that shares a spelling with a host verb (`remove`) cannot
        # capture them, and checked AGAINST that family surface so an unknown
        # verb (`store.frobnicate(k)`) is refused here rather than compiled as
        # a pass-through that only fails at the host runtime (item 401, the
        # item-84 crash shape).
        self.host_locals: dict[str, str] = {}

    def bind_local(self, name: str, line: int) -> str:
        if name in self.locals or name in self.requires or name in self.params:
            raise RevlError(self.filename, line, f"name `{name}` is already bound in {self.component.name}")
        safe = _safe_name(name, self._taken)
        self._taken.add(safe)
        self.locals[name] = safe
        return safe

    def bind_params(self, names: list[str], line: int) -> dict[str, str]:
        params: dict[str, str] = {}
        taken = set(self._taken)
        for name in names:
            if name in params:
                raise RevlError(self.filename, line, f"duplicate parameter `{name}`")
            safe = _safe_name(name, taken)
            taken.add(safe)
            params[name] = safe
        return params


# ---------------------------------------------------------------------------
# v2.0: type declarations & pure functions (docs/syntax-2.0.md §2–§3)
# ---------------------------------------------------------------------------

# builtin (non-record) type heads: destructuring a value of one of these with
# a record/list pattern is a type error, not a host pass-through
_BUILTIN_NONRECORD = {"Str", "Int", "Int32", "Bool", "Float", "Bytes", "Unit",
                      "List", "Map", "Opt", "Result"}

# host roots a pure fn may call without an explicit binding (DESIGN §7 builtins)
_HOST_CALLABLES = {"Map", "Pool", "Job"}

# The host-Map iteration surface (items 84/86/88, docs/stdlib-2.0.md §Map).
# `size`/`keys` are backed by EVERY tier's host Map runtime (py runtime.py
# `Map.size`/`Map.keys`, ts `MapHandle.size`/`keys`, and their go/rust/java
# mirrors), so they are legal verbs on a host-provenance local — but they
# share a spelling with the VALUE-Map builtins and so live in
# `_BUILTIN_METHODS`, not `_HOST_ARG_SIG` (whose names must stay disjoint from
# the builtin table — the disjointness check at import time enforces it). On a
# host local they dispatch as host verbs (a plain `call` node, selected by
# receiver kind), the same dual dispatch the sanctioned `remove` overlap rides.
# Kept beside `_HOST_CALLABLES` so the host-Map surface — `_HOST_FAMILIES`'s
# `Map` row plus this — is enumerable in one place; maps verb -> arity.
_HOST_MAP_ITER_VERBS: dict[str, int] = {"size": 0, "keys": 0}


def _host_result_type(acquire: dict, env: "Env") -> str | None:
    """The declared RESULT type of a result-declared host verb call on a host
    local, or `None` when `acquire` is not such a call (item 397).

    The host frontier types arguments and deliberately not results, except for
    the one-verb-wide result column `_HOST_RESULT_SIG` (today: `Map.
    insert_if_absent -> Bool`). A lowered host-verb call is
    `{"kind": "call", "target": {"kind": "name", "id": <safe>}, "method": ...}`
    where the target names a host local (its family lives in `env.host_locals`).
    This is the compare-and-set (CAS) shape whose Bool the program consumes with
    a pure `if` — the classification is a host verb in the effect stratum,
    spelled as an acquisition, whose let-effect binding is TYPED
    (docs/design/397-insert-if-absent.md §Classification)."""
    if not isinstance(acquire, dict) or acquire.get("kind") != "call":
        return None
    target = acquire.get("target")
    if not isinstance(target, dict) or target.get("kind") != "name":
        return None
    family = env.host_locals.get(target.get("id"))
    if family is None:
        return None
    return _HOST_RESULT_SIG.get(f"{family}.{acquire.get('method')}")


def _check_host_verb(family: str, verb: str, argc: int,
                     filename: str | None, line: int) -> None:
    """Admit a verb call on a host-provenance local (item 401).

    The known surface is the family's stub verbs (`_HOST_FAMILIES`, derived
    from `_HOST_ARG_SIG`) plus, for a host Map, the iteration verbs
    (`_HOST_MAP_ITER_VERBS`). A verb in neither is refused with the same named
    HOST-METHOD diagnostic the constructor-tracked receiver path uses, naming
    the FULL surface — so a typo or a value-Map method wrongly aimed at a host
    Map (`store.lookup(k)`, which no host runtime backs) is a compile error
    here rather than an unchecked pass-through that only crashes at the host
    runtime (the item-84 shape).

    Only the VERB NAME and ARITY are checked, never the argument TYPES: a host
    Map's key and value are generically typed on the tiers that genericized it
    (`Map[V]`; items 113/176), the checker's `["Str","Str"]` row is the known
    frontier/emitter disagreement the host boundary already carries opaquely
    (`_HOST_ARG_SIG` header; docs/design/397-insert-if-absent.md), and the
    pre-item-401 pass-through type-checked nothing here. Enforcing the row's
    `Str` would reject valid programs (`m.insert(k, double(v))` with a non-Str
    value/key), which is a separate frontier decision, not item 401's.
    """
    family_surface = _HOST_FAMILIES.get(family, {})
    iter_verbs = _HOST_MAP_ITER_VERBS if family == "Map" else {}
    if verb in family_surface:
        arity = len(family_surface[verb])
    elif verb in iter_verbs:
        arity = iter_verbs[verb]
    else:
        if filename:
            surface = ", ".join(sorted(set(family_surface) | set(iter_verbs)))
            raise RevlError(
                filename, line,
                f"`{family}` has no method `{verb}` (its surface: {surface})",
                hint="host objects are checked against the stub surface spelled "
                     "in docs/stdlib-2.0.md — a misspelled method compiles on "
                     "every tier and only fails at the host runtime",
                code="HOST-METHOD", category="host-boundary")
        return
    if filename and argc != arity:
        raise RevlError(
            filename, line,
            f"host `{family}.{verb}` takes {arity} argument"
            f"{'' if arity == 1 else 's'}, got {argc}",
            hint=f"the signature is `.{verb}("
                 f"{', '.join(family_surface.get(verb, [])) if verb in family_surface else ''})`",
            code="HOST-ARITY", category="host-boundary")


# item 395 / Stage 5 gate of docs/design/378-sync-extern-service-reach.md: the
# backend tiers whose emitter HAS the extern config-injection seam (binds
# `_revl_config` in the extern body from the plug-time composition config map).
# Item 378 landed option (b) py-ONLY; Stage 5 grew the seam to ts/go/java/rust,
# each emitting a module-global config map + a fail-loud lookup that mirrors the
# py `_REVL_EXTERN_CONFIG` / `_revl_extern_config` shape. wasm stays OUT: its
# extern body is raw WAT and its only config channel is a scalar-only, spawn-
# time runtime import, with no plug-time config dict to bind (see the design's
# Stage 5 note and backends/wasm/emit.py config refusal). A config extern that
# carries a host body for a tier NOT in this set is refused at compile
# (`_lower_externs`), because that tier would emit the body with `_revl_config`
# unbound: a late, mis-attributed failure. The key is the @-body spelling
# (`py`/`ts`/`go`/`java`/`rs`).
_CONFIG_INJECTION_TIERS = {"py", "ts", "go", "java", "rs"}
# item 396 option B: the tiers on which a host-module `ref` is native (py, ts).
# Imported from `hostref` so the compile-time gate and the resolver agree; a ref
# on go/rust/java/wasm is refused in `_lower_externs`.
from .hostref import EXTERN_REF_TIERS as _EXTERN_REF_TIERS  # noqa: E402
# Opt/Result constructors, recognized so `Some(x)`/`Ok(r)` resolve (syntax-2.0 §2)
_BUILTIN_CONSTRUCTORS = {"Some", "None", "Ok", "Err"}
# taint declassifier operators (roadmap item 249, Decision 3.2): `endorse(v)` is
# the audited downgrade of an `Untrusted[T]` to a `Trusted[T]`. It is recognized
# as a callable so it lowers through the ordinary call machinery; it is identity
# on its argument's base type (typed and unwrapped as such), so after the taint
# verdict runs it is spliced out of the IR and no emitter ever sees it.
_DECLASSIFY_BUILTINS = {"endorse"}


def _endorse_node(inner: dict, expr: "ExprEndorse", approval: dict | None) -> dict:
    """Build the IR node for a scoped `endorse[<origin>](v, reason=...)` (item
    249, Slice C). It keeps the SAME `call`-to-`endorse` shape Slice A produced —
    so base typing (identity on the argument), the callable resolution, and the
    post-verdict splice are all unchanged — and rides an additive `endorse`
    metadata dict the taint checker reads: the declared origin it downgrades, its
    mandatory reason, its source line, and any `with` approval edge. The whole
    node splices out before any emitter sees it, metadata and all."""
    meta: dict = {"origin": expr.origin, "reason": expr.reason,
                  "line": expr.line}
    if approval is not None:
        meta["approval"] = approval
    return {"kind": "call", "callee": {"kind": "var", "name": "endorse"},
            "args": [inner], "endorse": meta}


# Builtin heads a `type X = Y` right-hand side may name. `Any`/`Never` are the
# type algebra's wildcards; the rest are the declared builtin surface.
_ALIASABLE_BUILTINS = _BUILTIN_NONRECORD | {"Any", "Never"}


def _alias_target(decl: TypeDecl, declared: set[str]) -> str | None:
    """The type `type X = Y` aliases, or None when the decl is a real variant.

    `type X = Y` is TypeScript's alias spelling, and syntax-2.0's governing
    principle is that no construct may exist in both languages with silently
    different meaning. So the split follows TypeScript's own:

    - Where TypeScript *compiles* it — `Y` names an existing type — revl means
      what TypeScript means: a transparent alias. `type Sku = Str` used to
      declare a one-case variant whose case was named `Str`, which made the
      author's own alias unusable (`f("abc")` was refused for a `Sku`
      parameter) while `return Str` was accepted as a case constructor.
    - Where TypeScript *rejects* it — `Y` is undeclared (TS2304) — revl is free
      to mean something else, because there is no shared meaning to diverge
      from. `type Status = Pending` keeps its one-case-variant reading, which
      is how an opaque nominal is spelled today.

    A payload makes it a newtype (`type W = Wrap(Int)`), never an alias.
    """
    if decl.fields or len(decl.cases) != 1:
        return None
    case = decl.cases[0]
    if case.payload is not None:
        return None
    head, args = parse_type(case.name)
    if args:
        return case.name  # a type application; the parser only builds these here
    if head in _ALIASABLE_BUILTINS or head in declared:
        return case.name
    return None


def _resolve_type_aliases(program: Program, filename: str) -> None:
    """Erase transparent type aliases from the program, in place.

    Aliases are substituted at every declaration site and their declarations
    dropped, so nothing downstream — the type table, the checker, the IR, the
    backends — ever sees the alias name. That is what `transparent` means, and
    it is the reading TypeScript has: `Sku` and `Str` are interchangeable in
    both directions. A *nominal* alias would be a distinct type needing
    construction syntax revl does not have, and would re-commit the very sin
    this fixes (both languages compiling `type X = Y` with different meanings).
    """
    declared = {d.name for d in program.type_decls}
    aliases: dict[str, TypeDecl] = {}
    for decl in program.type_decls:
        target = _alias_target(decl, declared)
        if target is None:
            continue
        if decl.params:
            raise RevlError(
                filename, decl.line,
                f"type alias `{decl.name}` cannot declare type parameters",
                hint="an alias is substituted verbatim, so it has nothing to "
                     "instantiate — drop the parameters, or declare a variant "
                     "with named cases (syntax-2.0 §2)",
            )
        # an alias is a declaration, so its right-hand side is checked here
        # rather than only where the alias happens to be used
        check_type_wellformed(filename, decl.line, target)
        aliases[decl.name] = decl
    if not aliases:
        return

    def expand(type_name: str, stack: tuple) -> str:
        head, args = parse_type(type_name)
        if args:
            return format_type(head, [expand(a, stack) for a in args])
        if head not in aliases:
            return head
        if head in stack:
            chain = " -> ".join(stack[stack.index(head):] + (head,))
            raise RevlError(
                filename, aliases[head].line,
                f"type alias cycle: {chain}",
                hint="an alias is substituted verbatim, so a cycle has no "
                     "expansion — break it, or declare one of them as a variant",
            )
        return expand(_alias_target(aliases[head], declared), stack + (head,))

    resolved = {name: expand(_alias_target(decl, declared), (name,))
                for name, decl in aliases.items()}

    def subst(type_name):
        if not type_name:
            return type_name
        head, args = parse_type(type_name)
        if args:
            return format_type(head, [subst(a) for a in args])
        return resolved.get(head, head)

    # every declaration site that carries a type annotation; kept in step with
    # `_validate_declared_types` below, which enumerates the same surface
    for fn in program.fn_decls:
        for p in fn.params:
            p.type = subst(p.type)
        fn.returns = subst(fn.returns)
    for ext in program.externs:
        for p in ext.params:
            p.type = subst(p.type)
        ext.returns = subst(ext.returns)
    for svc in program.services:
        for m in svc.methods.values():
            m.params = [(pname, subst(ptype)) for pname, ptype in m.params]
            m.returns = subst(m.returns)
    for decl in program.type_decls:
        for fld in decl.fields:
            fld.type = subst(fld.type)
        for case in decl.cases:
            case.payload = subst(case.payload)
    for comp in program.components:
        for cfg in comp.config:
            cfg.type = subst(cfg.type)
        for stmt in comp.body:
            if not isinstance(stmt, ProvideStmt):
                continue
            for method in stmt.methods:
                method.param_types = [subst(t) for t in method.param_types]
                method.returns = subst(method.returns)
                _subst_body_annotations(method.body, subst)
    for fn in program.fn_decls:
        _subst_body_annotations(fn.body, subst)
    for test in program.tests:
        _subst_body_annotations(test.body, subst)

    program.type_decls = [d for d in program.type_decls if d.name not in aliases]


def _subst_body_annotations(stmts: list, subst) -> None:
    """Expand type aliases in the annotations a *body* can carry.

    `let g: Handler = …` and `(v: Handler) => …` are type annotations like any
    other, but they live inside statements rather than at a declaration site,
    so the declaration sweep above cannot reach them — and an alias that
    survived here would reach the checker as an undeclared type name."""
    def walk_expr(expr) -> None:
        if isinstance(expr, ExprArrow):
            expr.param_types = [subst(t) for t in expr.param_types]
            walk_expr(expr.body)
        elif isinstance(expr, ExprBin):
            walk_expr(expr.left)
            walk_expr(expr.right)
        elif isinstance(expr, ExprUn):
            walk_expr(expr.operand)
        elif isinstance(expr, ExprCall):
            walk_expr(expr.callee)
            for arg in expr.args:
                walk_expr(arg)
        elif isinstance(expr, (ExprField, ExprOptField)):
            walk_expr(expr.target)
        elif isinstance(expr, ExprOptCall):
            walk_expr(expr.target)
            for arg in expr.args:
                walk_expr(arg)
        elif isinstance(expr, ExprIndex):
            walk_expr(expr.target)
            walk_expr(expr.index)
        elif isinstance(expr, ExprIf):
            walk_expr(expr.cond)
            walk_expr(expr.then)
            walk_expr(expr.otherwise)
        elif isinstance(expr, ExprRecord):
            for _, value in expr.fields:
                walk_expr(value)
        elif isinstance(expr, ExprRecordUpdate):
            walk_expr(expr.base)
            for _, value in expr.updates:
                walk_expr(value)
        elif isinstance(expr, ExprList):
            for item in expr.items:
                walk_expr(item)
        elif isinstance(expr, ExprMatch):
            walk_expr(expr.scrutinee)
            for _, _, body in expr.arms:
                walk_expr(body)
                if isinstance(body, ExprBlockArm):
                    for stmt in body.stmts:
                        walk_expr(stmt.value)
                    walk_expr(body.tail)
        elif isinstance(expr, Interp):
            for kind, part in expr.parts:
                if kind == "expr":
                    walk_expr(part)

    def walk_stmt(stmt) -> None:
        if isinstance(stmt, LetStmt):
            stmt.type = subst(stmt.type)
            walk_expr(stmt.value)
        elif isinstance(stmt, (LetPatternStmt, AssignStmt)):
            walk_expr(stmt.value)
        elif isinstance(stmt, (ExprStmt, AssertStmt, FailStmt, AwaitStmt)):
            walk_expr(stmt.expr)
        elif isinstance(stmt, ReturnStmt):
            if stmt.expr is not None:
                walk_expr(stmt.expr)
        elif isinstance(stmt, IfStmt):
            walk_expr(stmt.cond)
            for child in list(stmt.then) + list(stmt.otherwise or []):
                walk_stmt(child)
        elif isinstance(stmt, WhileStmt):
            walk_expr(stmt.cond)
            for child in stmt.body:
                walk_stmt(child)
        elif isinstance(stmt, ForStmt):
            walk_expr(stmt.iterable)
            for child in stmt.body:
                walk_stmt(child)

    for stmt in stmts:
        walk_stmt(stmt)


def _validate_declared_types(program: Program, filename: str) -> None:
    """Reject malformed type annotations (bare builtin generics like `Opt`,
    `List[]`) at every declaration site before checking begins — otherwise a
    zero-arg generic reaches the type algebra and crashes it."""
    for fn in program.fn_decls:
        for p in fn.params:
            # A module `fn` parameter is the one v1 position that admits an
            # async function type `(…) -> Async[T]` (item 92).
            check_type_wellformed(filename, p.line, p.type,
                                  allow_async_param=not fn.verified)
        check_type_wellformed(filename, fn.line, fn.returns)
    for ext in program.externs:
        for p in ext.params:
            check_type_wellformed(filename, p.line, p.type)
        check_type_wellformed(filename, ext.line, ext.returns)
    for svc in program.services:
        for m in svc.methods.values():
            for _, ptype in m.params:
                check_type_wellformed(filename, m.line, ptype)
            check_type_wellformed(filename, m.line, m.returns)
    for decl in program.type_decls:
        for field in decl.fields:
            check_type_wellformed(filename, field.line, field.type)
        for case in decl.cases:
            check_type_wellformed(filename, case.line, case.payload)
    # item 378: a config value is injected as static data at plug/spawn/load
    # time, so a config field's declared type must be built, transitively, out of
    # data, never an arrow type (a live callable) or a `service` (a capability).
    # Resolve nominal record/ADT/alias heads through a lightweight type-table and
    # the set of declared service names. A duplicate type/field is reported by the
    # dedicated lowering pass; `setdefault` here just avoids raising twice.
    service_names = {svc.name for svc in program.services}
    config_type_defs: dict[str, dict] = {}
    for decl in program.type_decls:
        if decl.fields:
            config_type_defs.setdefault(
                decl.name,
                {"kind": "record",
                 "fields": {f.name: f.type for f in decl.fields}})
        else:
            config_type_defs.setdefault(
                decl.name,
                {"kind": "variant",
                 "cases": [{"name": c.name, "payload": c.payload}
                           for c in decl.cases]})

    def _check_config(owner: str, fields) -> None:
        for cfg in fields:
            check_type_wellformed(filename, cfg.line, cfg.type)
            check_config_field_is_data(
                filename, cfg.line, cfg.name, owner, cfg.type,
                service_names=service_names, type_defs=config_type_defs)

    for comp in program.components:
        _check_config(f"component `{comp.name}`", comp.config)
    # item 379: an extern's typed config schema was NEVER wellformed-checked, so
    # `extern pure fn thing(x) config { handler: (Str) -> Str }` compiled and its
    # body could invoke the injected callable past every authority fold. Check it
    # at the same data-only bar as a component's config.
    for ext in program.externs:
        if ext.config:
            _check_config(f"extern `{ext.name}`", ext.config)


def _lower_type_decls(program: Program, filename: str) -> dict:
    types: dict[str, dict] = {}
    for decl in program.type_decls:
        if decl.name in types:
            raise RevlError(filename, decl.line, f"duplicate type `{decl.name}`")
        if decl.fields:
            fields: dict[str, str] = {}
            for field in decl.fields:
                if field.name in fields:
                    raise RevlError(filename, field.line,
                                    f"duplicate field `{field.name}` in record `{decl.name}`")
                fields[field.name] = field.type
            types[decl.name] = {"params": decl.params, "kind": "record", "fields": fields}
        else:
            cases: list[dict] = []
            seen: set[str] = set()
            for case in decl.cases:
                if case.name in seen:
                    raise RevlError(filename, case.line,
                                    f"duplicate case `{case.name}` in type `{decl.name}`")
                seen.add(case.name)
                cases.append({"name": case.name, "payload": case.payload})
            types[decl.name] = {"params": decl.params, "kind": "variant", "cases": cases}
    return types


def _fn_call_graph(program: Program) -> dict[str, set[str]]:
    """Direct-call graph over the file's functions.

    Host roots and constructors are not function declarations, so they are
    ignored: only `ExprVar` callees whose name is another `fn` are edges.
    """
    fn_names = {fn.name for fn in program.fn_decls}
    graph = {fn.name: set() for fn in program.fn_decls}

    def collect_expr(expr) -> None:
        if isinstance(expr, ExprCall):
            if isinstance(expr.callee, ExprVar) and expr.callee.name in fn_names:
                graph[decl.name].add(expr.callee.name)
            collect_expr(expr.callee)
            for arg in expr.args:
                collect_expr(arg)
        elif isinstance(expr, ExprBin):
            collect_expr(expr.left)
            collect_expr(expr.right)
        elif isinstance(expr, ExprUn):
            collect_expr(expr.operand)
        elif isinstance(expr, ExprField):
            collect_expr(expr.target)
        elif isinstance(expr, ExprIndex):
            collect_expr(expr.target)
            collect_expr(expr.index)
        elif isinstance(expr, ExprIf):
            collect_expr(expr.cond)
            collect_expr(expr.then)
            collect_expr(expr.otherwise)
        elif isinstance(expr, ExprRecord):
            for _, value in expr.fields:
                collect_expr(value)
        elif isinstance(expr, ExprRecordUpdate):
            collect_expr(expr.base)
            for _, value in expr.updates:
                collect_expr(value)
        elif isinstance(expr, ExprList):
            for item in expr.items:
                collect_expr(item)
        elif isinstance(expr, ExprArrow):
            collect_expr(expr.body)

    def collect_stmt(stmt) -> None:
        if isinstance(stmt, LetStmt):
            collect_expr(stmt.value)
        elif isinstance(stmt, LetPatternStmt):
            collect_expr(stmt.value)
        elif isinstance(stmt, AssignStmt):
            collect_expr(stmt.value)
        elif isinstance(stmt, ReturnStmt):
            if stmt.expr is not None:
                collect_expr(stmt.expr)
        elif isinstance(stmt, IfStmt):
            collect_expr(stmt.cond)
            for branch in (stmt.then, stmt.otherwise or []):
                for child in branch:
                    collect_stmt(child)
        elif isinstance(stmt, WhileStmt):
            collect_expr(stmt.cond)
            for child in stmt.body:
                collect_stmt(child)
        elif isinstance(stmt, ForStmt):
            collect_expr(stmt.iterable)
            for child in stmt.body:
                collect_stmt(child)
        elif isinstance(stmt, (ExprStmt, AssertStmt)):
            collect_expr(stmt.expr)

    for decl in program.fn_decls:
        for stmt in decl.body:
            collect_stmt(stmt)
    return graph


def _reaches_self(start: str, graph: dict[str, set[str]]) -> bool:
    seen: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        for succ in graph.get(node, ()):
            if succ == start:
                return True
            if succ not in seen:
                seen.add(succ)
                stack.append(succ)
    return False


def _check_verified_totality(program: Program, filename: str) -> None:
    """Totality gate for `verified fn` (syntax-2.0 §7).

    This first cut is deliberately conservative: a verified function may not
    participate in any cycle in the direct-call graph, because the checker
    cannot currently prove structural descent. Loop bodies are traversed so
    recursion through `while`/`for` is still caught.
    """
    verified = [fn for fn in program.fn_decls if fn.verified]
    if not verified:
        return
    graph = _fn_call_graph(program)
    for decl in verified:
        if _reaches_self(decl.name, graph):
            raise RevlError(
                filename,
                decl.line,
                f"verified fn `{decl.name}` is not total: it participates in "
                "direct or mutual recursion (verified totality, syntax-2.0 §7)",
                hint="use structural recursion on a structurally smaller value, "
                     "or a syntactically bounded loop",
            )


def _has_return(stmts) -> bool:
    """True when a `return` appears anywhere in this statement tree."""
    for stmt in stmts:
        if isinstance(stmt, ReturnStmt):
            return True
        if isinstance(stmt, IfStmt):
            if _has_return(stmt.then) or _has_return(stmt.otherwise or []):
                return True
        elif isinstance(stmt, (WhileStmt, ForStmt)):
            if _has_return(stmt.body):
                return True
    return False


def _loop_has_targeting_break(stmts) -> bool:
    """True when a bare `break` in `stmts` targets the loop these statements are
    the body of — i.e. a `break` at any statement depth that is not itself
    inside a nested `while`/`for` (a nested loop captures its own `break`). With
    only unlabeled `break`, "targets this loop" is exactly "not shadowed by a
    nearer loop" (JLS 14.21's reachability rule for the same conservative
    analysis, docs/design/379-break-continue.md)."""
    for stmt in stmts:
        if isinstance(stmt, BreakStmt):
            return True
        if isinstance(stmt, IfStmt):
            if (_loop_has_targeting_break(stmt.then)
                    or _loop_has_targeting_break(stmt.otherwise or [])):
                return True
        # a nested while/for is NOT descended: a `break` inside it targets that
        # inner loop, not this one.
    return False


def _definitely_returns(stmts) -> bool:
    """True when control cannot reach the end of `stmts` without returning.

    Deliberately the same conservative rule Java and Rust apply, so a body this
    accepts is a body those tiers accept:

    - a `return` terminates;
    - an `if` terminates only when it has an `else` *and* both arms terminate
      (a bare `if` may be skipped);
    - `for` and a conditional `while` may run zero times, so neither terminates;
    - `while (true)` diverges — and so terminates the path — *iff* its body has
      no reachable `break` that targets it (item 379,
      docs/design/379-break-continue.md). A `while (true)` with a reachable
      `break` may leave the loop and fall through, so it does NOT terminate;
      one with no such `break` never exits, exactly as Java (JLS 14.21) and Rust
      (`loop {}` : `!`) judge it, and no tier needs a fallthrough value.
    """
    for stmt in stmts:
        if isinstance(stmt, ReturnStmt):
            return True
        if isinstance(stmt, IfStmt):
            if (stmt.otherwise is not None
                    and _definitely_returns(stmt.then)
                    and _definitely_returns(stmt.otherwise)):
                return True
        elif isinstance(stmt, WhileStmt):
            if (isinstance(stmt.cond, ExprLit) and stmt.cond.value is True
                    and not _loop_has_targeting_break(stmt.body)):
                return True
    return False


def _check_returns_on_every_path(decl: FnDecl, filename: str) -> None:
    """A fn with a declared return type must return on every path.

    Falling off the end is a portability trap, not a nicety: Python (the
    reference backend) silently yields `None`, TypeScript yields `undefined`,
    while Rust refuses with E0308 and Java with "missing return statement". A
    program the checker accepts must compile on every tier, so the strict
    reading is the frontend's.
    """
    if not decl.returns or _definitely_returns(decl.body):
        return
    last_line = getattr(decl.body[-1], "line", decl.line) if decl.body else decl.line
    if not _has_return(decl.body):
        raise RevlError(
            filename, decl.line,
            f"`{decl.name}` is declared to return `{decl.returns}` but its body "
            f"never returns a value",
            hint=f"end the body with `return <{decl.returns}>`, or drop the "
                 f"`-> {decl.returns}` annotation if the fn produces nothing — "
                 "revl has no implicit result (rust E0308, java \"missing return "
                 "statement\")",
            code="T1", category="type-mismatch",
            expected=decl.returns, actual=None,
        )
    raise RevlError(
        filename, last_line,
        f"`{decl.name}` is declared to return `{decl.returns}` but control can "
        f"reach the end of its body without a `return`",
        hint="every path must return: give the trailing `if` an `else` that "
             "returns, or add a final `return` after it — a `for`/`while` may "
             "run zero times and never counts (rust E0308, java \"missing "
             "return statement\")",
        code="T1", category="type-mismatch",
        expected=decl.returns, actual=None,
    )


def _same_file_two_copies(a: str, b: str) -> bool:
    """True when `a` and `b` are distinct paths to a byte-identical file with
    the same basename — the "two copies of one module" case (roadmap 394)."""
    if os.path.basename(a) != os.path.basename(b):
        return False
    if os.path.abspath(a) == os.path.abspath(b):
        return False
    try:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


def _duplicate_symbol_message(kind: str, name: str,
                              first_file: str, second_file: str) -> str:
    """A duplicate-symbol message that names BOTH declaring files by absolute
    path (roadmap 394, revl-harness F-H39.3).

    The two files that declare `name` are frequently the same module reached
    under two `use` spellings — a search-path spelling (`use "stdlib/str.rvl"`)
    and a vendored byte-identical copy (`use "../../stdlib/str.rvl"`). Naming
    only one path reads as a bug in revl's own stdlib; naming both — and, when
    they are byte-identical copies of one file, saying so — makes "you have two
    copies of the same module" self-evident.
    """
    a = os.path.abspath(first_file)
    b = os.path.abspath(second_file)
    msg = (f"duplicate {kind} `{name}` — declared in both\n"
           f"    {a}\n"
           f"  and\n"
           f"    {b}")
    if _same_file_two_copies(a, b):
        msg += ("\n  these are byte-identical copies of the same file under two "
                "paths: you have two copies of one module (often a vendored "
                "copy reached under two `use` spellings) — delete one copy, or "
                "`use` a single shared path so it loads as one module")
    return msg


def _lower_fns(program: Program, filename: str, types: dict | None = None) -> list:
    _check_verified_totality(program, filename)
    types = types or {}
    default_callables = (
        _HOST_CALLABLES
        | _BUILTIN_CONSTRUCTORS
        | _DECLASSIFY_BUILTINS
        | {fn.name for fn in program.fn_decls}
        | {ext.name for ext in program.externs}
    )
    fns: list[dict] = []
    # name -> the file that first declared it, so a duplicate names BOTH files
    # (roadmap 394): a same-named fn reached under two `use` spellings loads as
    # two modules, and the diagnostic must show both resolved paths.
    seen: dict[str, str] = {}
    # Install the block-arm lift sink for the duration of fn-body lowering: a
    # statement-block match arm is lambda-lifted into a synthetic helper fn
    # (`_lift_block_arm`) collected here, then appended to `fns` below. `taken`
    # seeds the fresh-name search so a helper never shadows a user fn/extern.
    # setdefault installs the sink into `types` for the duration of fn-body
    # lowering (drained below); `_lift_block_arm` reads it back via
    # `types.get(LIFT_SINK)`, so the return value is not needed here.
    types.setdefault(LIFT_SINK, {
        "fns": [],
        "n": 0,
        "taken": ({fn.name for fn in program.fn_decls}
                  | {ext.name for ext in program.externs}),
    })
    for decl in program.fn_decls:
        # In a multi-file composition `filename` is only the first source
        # (paths[0]); a fn parsed from a LATER file carries its own `source`, so
        # its diagnostics must name that file, not paths[0] (roadmap 312).
        decl_file = decl.source or filename
        if decl.name in seen:
            first_file = seen[decl.name]
            if os.path.abspath(first_file) == os.path.abspath(decl_file):
                # two fns of one name in ONE file: the terse message already
                # points at the sole file, so keep it byte-identical (roadmap
                # 394 only widens the CROSS-FILE case).
                raise RevlError(decl_file, decl.line,
                                f"duplicate function `{decl.name}`")
            raise RevlError(
                decl_file, decl.line,
                _duplicate_symbol_message("function", decl.name,
                                          first_file, decl_file))
        seen[decl.name] = decl_file
        # the body sees the *marked* signature: this fn's own type parameters
        # are wildcards inside it (they are universally quantified there), while
        # a one-letter nominal type stays checked
        sig = (types.get(FNS_KEY) or {}).get(decl.name) or {}
        marked_params = sig.get("params") or [p.type for p in decl.params]
        marked_returns = sig.get("returns", decl.returns)
        scope: dict[str, bool] = {}
        type_env: dict[str, str] = {}
        for param, marked in zip(decl.params, marked_params):
            if param.name in scope:
                raise RevlError(decl_file, param.line,
                                f"duplicate parameter `{param.name}` in fn {decl.name}")
            scope[param.name] = False
            type_env[param.name] = marked
        module_callables = program.fn_scopes.get(id(decl), default_callables)
        callables = _HOST_CALLABLES | _BUILTIN_CONSTRUCTORS | _DECLASSIFY_BUILTINS | set(module_callables) | {ext.name for ext in program.externs}
        alias_fns = program.fn_alias_scopes.get(id(decl), {})
        body: list[dict] = []
        for stmt in decl.body:
            _lower_pure_stmt(stmt, scope, callables, alias_fns, body, decl_file, type_env, types,
                             expected_return=marked_returns)
        _check_returns_on_every_path(decl, decl_file)
        # phase-2 async coloring (docs/design/async-extern.md §3): a module
        # `fn` that reaches an async extern — directly or transitively — is no
        # longer refused here; it becomes async-colored by the `_async_callables`
        # fixed point in `check_and_lower`, which then stamps `"async": True` on
        # this entry. First-class *value* use of an async callable stays refused
        # (an arrow type carries no color), also in that post-pass once the
        # colored set is known. This function is now pure lowering.
        entry = {
            "name": decl.name,
            "params": [{"name": p.name, "type": p.type} for p in decl.params],
            "returns": decl.returns,
            "public": decl.public,
            "body": body,
        }
        if decl.verified:
            entry["verified"] = True
        if decl.source:
            _retarget_holes(entry["body"], decl.source)
        fns.append(entry)
    # Drain the lift sink: synthetic arm helpers join the functions list, so the
    # async-coloring and emission fixed points that run over `fns` see them.
    lifted = types.pop(LIFT_SINK, None)
    if lifted and lifted["fns"]:
        fns.extend(lifted["fns"])
    return fns


def _signature_table(program: Program, types: dict | None = None) -> dict:
    """{name: {"params": [type...], "returns": type|None, "tparams": set}} for
    fns + externs.

    Each signature's type parameters are marked here, once, so the rest of the
    checker can tell a universally quantified `T` from a nominal type that
    merely has a one-letter name. The set is the explicit `fn id[T](...)`
    names the decl carries (validated for shadowing here — see
    `validate_explicit_tparams`), plus — only when the decl carries NO list —
    the implicit single-uppercase names (roadmap 75(c): an explicit list
    turns the heuristic off; declared means declared).
    Marked types never reach the IR — this table is the checker's view, and
    `_lower_fns`/`_lower_externs` emit the author's spelling, byte-identical
    whether a parameter was implicit or explicit."""
    declared = {name: spec for name, spec in (types or {}).items()
                if not name.startswith("__")}
    sigs: dict = {}
    for decl in list(program.fn_decls) + list(program.externs):
        raw_params = [p.type for p in decl.params]
        explicit = validate_explicit_tparams(
            getattr(decl, "type_params", ()) or (), declared,
            program.filename, decl.line)
        tparams = collect_tparams(raw_params + [decl.returns], declared,
                                  explicit=explicit)
        # default-value expressions (roadmap item 187), aligned with `params`.
        # `None` for a parameter without a default; the ordering invariant
        # (defaults are trailing) is enforced in the parser, so `required` is
        # simply the count of leading non-defaulted parameters. Externs never
        # carry defaults (`FnParam.default` stays `None` there), so this is a
        # no-op for the extern half of the table.
        defaults = [getattr(p, "default", None) for p in decl.params]
        required = next((i for i, d in enumerate(defaults) if d is not None),
                        len(defaults))
        sigs[decl.name] = {
            "params": [mark_tparams(t, tparams) for t in raw_params],
            "returns": mark_tparams(decl.returns, tparams),
            "tparams": tparams,
            "defaults": defaults,
            "required": required,
        }
    return sigs


def _with_default_args(callee_name: str, given: list, types: dict, lower_one):
    """Append default-value IR for omitted trailing parameters (item 187).

    Call-site resolution: the emitters only ever see a fully-supplied argument
    list, so no tier needs default-parameter machinery. `given` is the
    already-lowered actual arguments; `lower_one` lowers one default
    *expression* AST to IR in the caller's context (a default is a pure
    expression that closes over nothing, so the caller's scope is immaterial).
    A signature with no defaults, or a call that supplies every argument,
    returns `given` unchanged — byte-identical to before item 187."""
    sig = (types.get(FNS_KEY) or {}).get(callee_name)
    if not sig:
        return given
    defaults = sig.get("defaults") or []
    if len(given) >= len(defaults):
        return given
    out = list(given)
    for d in defaults[len(given):]:
        # `d` is non-None here: the arity check admitted this call only because
        # every omitted trailing parameter carries a default.
        out.append(lower_one(d))
    return out


def _case_table(types: dict) -> dict:
    """ADT constructor table: case name -> {"adt", "payload"}. Builtins first;
    ambiguous user cases (same name in two ADTs) are dropped to stay silent."""
    cases: dict = {
        "Some": {"adt": "Opt[Any]", "payload": None},
        "None": {"adt": "Opt[Any]", "payload": None},
        "Ok": {"adt": "Result[Any, Any]", "payload": None},
        "Err": {"adt": "Result[Any, Any]", "payload": None},
    }
    ambiguous: set = set()
    for type_name, spec in types.items():
        if type_name.startswith("__") or spec.get("kind") != "variant":
            continue
        for case in spec.get("cases", []):
            name = case["name"]
            # `Some`/`None` are reserved for `Opt` (host-null representation);
            # a user ADT reusing them is ambiguous and dropped.
            if name in ("Some", "None"):
                ambiguous.add(name)
                cases.pop(name, None)
                continue
            # a user ADT may reuse `Ok`/`Err`: the user's declaration shadows
            # the built-in `Result` (the docs' own `type Outcome = Ok(Row) | …`
            # does exactly this).
            if name in ("Ok", "Err") and cases.get(name, {}).get("adt", "").startswith("Result"):
                cases[name] = {"adt": type_name, "payload": case["payload"]}
                continue
            if name in cases or name in ambiguous:
                ambiguous.add(name)
                cases.pop(name, None)
                continue
            cases[name] = {"adt": type_name, "payload": case["payload"]}
    return cases


def _arrow_param_types(expr) -> list:
    """An arrow's parameter types, one per parameter (None where unknown).

    The checker writes what it resolved back onto the AST node
    (`typecheck.py::_resolve_arrow`), so this is either the author's `(v: Int)`
    annotations, the types the expected function type supplied, or Nones when
    the arrow is still on the unchecked frontier."""
    written = list(getattr(expr, "param_types", None) or [])
    written += [None] * (len(expr.params) - len(written))
    return written[:len(expr.params)]


def _expr_static_type(expr, type_env: dict, types: dict) -> str | None:
    """Best-effort static type of a pure expression.

    match exhaustiveness is a best-effort check: when the scrutinee's type is
    not recoverable from the local type environment, lowering still proceeds
    (the Python emitter adds a runtime fallback for those cases).
    """
    # delegated to the bidirectional checker's inference (non-raising form)
    return infer_ast(expr, type_env, types)


def _field_declared_type(target_type: str | None, field_name: str,
                         types: dict) -> str | None:
    """The DECLARED type of ``target_type.field_name`` when the target resolves
    to a record (structural or nominal), else ``None``.

    Used to decide whether a field read is TOTAL: a field whose declared type is
    ``Opt[T]`` reads back the empty Opt on absence rather than raising, so the
    designed spelling `e.kind ?? default` means the same on every tier (item
    380). A structural literal record binding is checked first (item 71 keeps
    those field-checkable), then the nominal record table.
    """
    if not target_type:
        return None
    struct = structural_fields(target_type)
    if struct is not None:
        return struct.get(field_name)
    head, _ = parse_type(target_type)
    spec = types.get(head or "")
    if spec is not None and spec.get("kind") == "record":
        return (spec.get("fields") or {}).get(field_name)
    return None


def _field_is_opt(target_type: str | None, field_name: str, types: dict) -> bool:
    """Whether ``target_type.field_name`` is declared ``Opt[...]`` — the trigger
    for a TOTAL field read (item 380)."""
    declared = _field_declared_type(target_type, field_name, types)
    return bool(declared) and parse_type(declared)[0] == "Opt"


def _type_arg(type_name: str | None, base: str) -> str | None:
    """Best-effort inner type of ``base[...]``."""
    if type_name and type_name.startswith(base + "["):
        return type_name[len(base) + 1 : -1]
    return None


def _is_sized_type(type_name: str | None) -> bool:
    return type_name in ("Str", "Bytes") or bool(type_name and type_name.startswith("List["))


def _variant_case_payload(types: dict, type_name: str | None, case_name: str) -> str | None:
    spec = types.get(type_name or "")
    if spec is None or spec.get("kind") != "variant":
        return None
    for case in spec.get("cases", []):
        if case["name"] == case_name:
            return case["payload"]
    return None


def _arm_payload_type(scrutinee_type: str | None, pattern: str, types: dict) -> str | None:
    """The payload type bound by a match arm. User variants come from the case
    table; built-in Opt/Result payloads come from the scrutinee's type args
    (`Opt[T]` -> Some binds T; `Result[T, E]` -> Ok binds T, Err binds E)."""
    user = _variant_case_payload(types, scrutinee_type, pattern)
    if user is not None:
        return user
    head, args = parse_type(scrutinee_type)
    if head == "Opt" and pattern == "Some" and args:
        return args[0]
    if head == "Result" and args and len(args) == 2:
        if pattern == "Ok":
            return args[0]
        if pattern == "Err":
            return args[1]
    return None


def _check_match_exhaustiveness(expr: ExprMatch, type_env: dict, types: dict, filename: str) -> None:
    type_name = _expr_static_type(expr.scrutinee, type_env, types)
    spec = types.get(type_name or "")
    if spec is None or spec.get("kind") != "variant":
        return
    # An arm naming something that is not a case of the scrutinee's ADT has no
    # meaning on any tier: Java emits a `case` label for a constant that does
    # not exist ("cannot find symbol") and the Rust emitter raises EmitError.
    # Python/TS silently never take the arm, so the divergence is a portability
    # bug rather than a compile error there — which is exactly what revl exists
    # to refuse.
    declared = [case["name"] for case in spec.get("cases", [])]
    for pattern, _, _ in expr.arms:
        if pattern != "_" and pattern not in declared:
            raise RevlError(
                filename, expr.line,
                f"`{pattern}` is not a case of `{type_name}` "
                f"(cases: {', '.join(f'`{c}`' for c in declared)})",
                hint="a match arm names one of the ADT's declared cases, or `_` for "
                     "a catch-all — a bare name is not a binding pattern "
                     "(syntax-2.0 §3.3); check the spelling or add the case to "
                     f"`type {type_name}`",
            )
    covered = {pattern for pattern, _, _ in expr.arms if pattern != "_"}
    if "_" in {pattern for pattern, _, _ in expr.arms}:
        return
    missing = [case["name"] for case in spec.get("cases", []) if case["name"] not in covered]
    if not missing:
        return
    if len(missing) == 1:
        rendered = f"`{missing[0]}`"
        plural = "case"
    else:
        rendered = ", ".join(f"`{name}`" for name in missing)
        plural = "cases"
    raise RevlError(filename, expr.line, f"non-exhaustive match: missing {plural} {rendered}")


def _mutable_free_vars(expr, scope: dict, bound: set[str] | None = None) -> set[str]:
    """Mutable `var` names referenced by ``expr`` and not shadowed by a
    lambda parameter.  Arrow literals snapshot these by value."""
    bound = set(bound or ())
    if isinstance(expr, ExprVar):
        if expr.name not in bound and scope.get(expr.name) is True:
            return {expr.name}
        return set()
    if isinstance(expr, ExprArrow):
        return _mutable_free_vars(expr.body, scope, bound | set(expr.params))
    if isinstance(expr, ExprBin):
        return _mutable_free_vars(expr.left, scope, bound) | _mutable_free_vars(expr.right, scope, bound)
    if isinstance(expr, ExprUn):
        return _mutable_free_vars(expr.operand, scope, bound)
    if isinstance(expr, ExprCall):
        found = _mutable_free_vars(expr.callee, scope, bound)
        for arg in expr.args:
            found |= _mutable_free_vars(arg, scope, bound)
        return found
    if isinstance(expr, ExprField):
        return _mutable_free_vars(expr.target, scope, bound)
    if isinstance(expr, ExprIndex):
        return _mutable_free_vars(expr.target, scope, bound) | _mutable_free_vars(expr.index, scope, bound)
    if isinstance(expr, ExprIf):
        return (
            _mutable_free_vars(expr.cond, scope, bound)
            | _mutable_free_vars(expr.then, scope, bound)
            | _mutable_free_vars(expr.otherwise, scope, bound)
        )
    if isinstance(expr, ExprRecord):
        found: set[str] = set()
        for _, value in expr.fields:
            found |= _mutable_free_vars(value, scope, bound)
        return found
    if isinstance(expr, ExprRecordUpdate):
        found = _mutable_free_vars(expr.base, scope, bound)
        for _, value in expr.updates:
            found |= _mutable_free_vars(value, scope, bound)
        return found
    if isinstance(expr, ExprList):
        found = set()
        for item in expr.items:
            found |= _mutable_free_vars(item, scope, bound)
        return found
    if isinstance(expr, ExprMatch):
        found = _mutable_free_vars(expr.scrutinee, scope, bound)
        for _, bind, body in expr.arms:
            arm_bound = set(bound)
            if bind is not None:
                arm_bound.add(bind)
            found |= _mutable_free_vars(body, scope, arm_bound)
        return found
    return set()


def _block_arm_unimplemented(filename: str, line: int) -> RevlError:
    """Block-bodied match arms lower by lambda-lifting into a synthetic helper
    fn (`_lift_block_arm`), which needs a lift sink in scope. A position without
    one (e.g. an extern undo expression) cannot lift, so it refuses loudly
    rather than half-emit."""
    return RevlError(
        filename, line,
        "a block-bodied match arm (`=> { … ; expr }`) is not lowerable here",
        hint="block arms lower inside a module `fn` body; lift the block into a "
             "named helper `fn` for this position (docs/records.md §6)",
    )


# The lift sink threads a synthetic-fn accumulator through fn-body lowering
# under a private (`__`-prefixed, so never serialised) key on the shared
# `types` dict. `_lower_fns` installs it and drains it into the functions list.
LIFT_SINK = "__arm_lift__"


def _pattern_binds(pattern) -> list[str]:
    """The names a `let`-pattern binds (record fields, list binds, list rest)."""
    names = list(getattr(pattern, "fields", None) or [])
    names += list(getattr(pattern, "binds", None) or [])
    rest = getattr(pattern, "rest", None)
    if rest:
        names.append(rest)
    return names


def _collect_arm_names(node, referenced: set[str], bound: set[str]) -> None:
    """Collect the names a block-arm subtree *reads* into ``referenced`` and the
    names it *declares* into ``bound``, so `_lift_block_arm` can capture exactly
    the enclosing bindings the arm uses.

    A generic AST walk: only the name-reading node (`ExprVar`, and the target of
    an assignment) and the name-binding nodes (`let`/`var`, `let`-pattern, `for`)
    are special-cased; every other node recurses over its dataclass fields."""
    if isinstance(node, list):
        for item in node:
            _collect_arm_names(item, referenced, bound)
        return
    if isinstance(node, ExprVar):
        referenced.add(node.name)
        return
    if isinstance(node, LetStmt):
        _collect_arm_names(node.value, referenced, bound)
        bound.add(node.name)
        return
    if isinstance(node, LetPatternStmt):
        _collect_arm_names(node.value, referenced, bound)
        for name in _pattern_binds(node.pattern):
            bound.add(name)
        return
    if isinstance(node, ForStmt):
        _collect_arm_names(node.iterable, referenced, bound)
        bound.add(node.bind)
        _collect_arm_names(node.body, referenced, bound)
        return
    if isinstance(node, AssignStmt):
        # the target names an existing binding the block writes into
        referenced.add(node.name)
        _collect_arm_names(node.value, referenced, bound)
        return
    if dataclasses.is_dataclass(node):
        for field in dataclasses.fields(node):
            _collect_arm_names(getattr(node, field.name), referenced, bound)
        return
    # scalars (str/int/None) carry no names


def _lift_block_arm(expr, scope: dict, callables: set, alias_fns: dict,
                    filename: str, type_env: dict, types: dict) -> dict:
    """Lower a statement-block match arm by lambda-lifting it into a synthetic
    helper fn (docs/records.md §6).

    The arm's statements become the helper's body, its final expression becomes
    the helper's `return`, and every enclosing name the block reads becomes a
    parameter. The arm's value is then a *call* to that helper — an expression,
    so the IR arm-body-as-expression invariant holds and every backend that
    emits a fn call emits the arm without new emit support.

    The body is lowered with the ordinary fn-body machinery (`_lower_pure_stmt`),
    so the arm obeys exactly the purity/effect rules of any pure value block: an
    effect statement, or an assignment to a captured (now immutable) name, is
    refused just as it would be in a normal block that produces a value."""
    sink = types.get(LIFT_SINK)
    if sink is None:
        raise _block_arm_unimplemented(filename, expr.line)

    referenced: set[str] = set()
    bound: set[str] = set()
    for stmt in expr.stmts:
        _collect_arm_names(stmt, referenced, bound)
    _collect_arm_names(expr.tail, referenced, bound)
    captures = sorted(n for n in referenced if n in scope and n not in bound)

    params: list[dict] = []
    arm_scope: dict = {}
    arm_type_env: dict = {}
    for cap in captures:
        ctype = type_env.get(cap)
        if ctype is None:
            raise RevlError(
                filename, expr.line,
                f"a match block arm reads `{cap}`, whose type is not known here",
                hint="annotate the binding this arm reads so the lifted arm "
                     "helper can type its parameter (docs/records.md §4)")
        params.append({"name": cap, "type": ctype})
        # a captured `var` enters the helper as an immutable parameter: reading
        # it is fine, assigning to it is refused, which is exactly the purity a
        # value-producing block owes its enclosing scope.
        arm_scope[cap] = "host" if scope.get(cap) == "host" else False
        arm_type_env[cap] = ctype

    arm_body: list = []
    for stmt in expr.stmts:
        _lower_pure_stmt(stmt, arm_scope, callables, alias_fns, arm_body,
                         filename, arm_type_env, types)
    ret_type = infer_ast(expr.tail, arm_type_env, types, filename)
    arm_body.append({
        "step": "return",
        "expr": _lower_pure_expr(expr.tail, arm_scope, callables, alias_fns,
                                 filename, arm_type_env, types),
    })

    # A fresh, backend-safe helper name: no leading underscore (emitters reserve
    # that for scaffolding) and never a user fn/extern name.
    taken = sink["taken"]
    name = f"match_arm_{sink['n']}"
    while name in taken:
        sink["n"] += 1
        name = f"match_arm_{sink['n']}"
    sink["n"] += 1
    taken.add(name)
    sink["fns"].append({
        "name": name,
        "params": params,
        "returns": ret_type,
        "public": False,
        "body": arm_body,
    })
    return {"kind": "call",
            "callee": {"kind": "var", "name": name},
            "args": [{"kind": "var", "name": cap} for cap in captures]}


class _LaxScope(dict):
    """Permissive scope for extern undo/compensate refs.

    Host blocks are verbatim text; their undo/compensate expressions are
    host-level names (e.g. `close(socket)`) and are not checked against revl
    function scope.
    """

    def __contains__(self, key) -> bool:
        return True


def _lower_extern_expr(expr, filename: str) -> dict:
    return _lower_pure_expr(expr, _LaxScope(), set(), {}, filename)


def _check_extern_undo(expr, decl_name: str, slot: str, types: dict,
                       filename: str, result_type: str | None = None) -> None:
    """The extern-level `undo`/`compensate` slot, checked.

    Component-site `effect ... undo ...` runs through the component
    expression machinery, so it resolves every name against the bindings
    in scope at the effect site. An extern declares no bindings, so the
    slot's variable namespace is almost empty — with ONE exception. The
    `undo` of an acquire extern with a declared return type binds `result`,
    the acquired value: the teardown exists to release exactly what the
    acquisition produced (the WIT resource model is the canonical user —
    `extern acquire fn r_new(...) -> R undo r_drop(result)`). Nothing else
    is visible: the extern's own parameters are *not* implicitly captured
    (no tier defines teardown parameter capture; inventing that here would
    be unsound speculation), and `compensate` — a best-effort follow-up to
    a one-way emission, not an inverse — binds nothing at all. What the
    slot must satisfy mirrors the component-site rules plus the arity/type
    rigor of every other call:

    1. the callee is a plain call to a DECLARED callable (fn/extern via the
       shared signature table, host builtin, ADT constructor);
    2. self-reference is refused — the teardown exists to INVERT the
       acquisition; calling the extern again would re-acquire mid-cleanup;
    3. arity and argument types are checked against the declared signature,
       with `result: T` in scope exactly when described above.
    """
    from .parser import ExprCall, ExprField, ExprVar

    def _walk(e):
        if isinstance(e, ExprVar):
            if e.name == "result":
                if result_type is not None:
                    return  # the one implicit binding: the acquired value
                if slot == "compensate":
                    raise RevlError(
                        filename, e.line,
                        f"`result` is not bound in the `compensate` slot of "
                        f"extern `{decl_name}`",
                        hint="`result` names the value an acquire returns; a "
                             "compensation follows a one-way emission, which "
                             "acquires nothing — compensate from constants or "
                             "other declared fns")
                raise RevlError(
                    filename, e.line,
                    f"`result` does not exist here — extern `{decl_name}` "
                    "declares no return type, so there is no acquired value "
                    "to bind",
                    hint="declare `-> T` on the extern to give the teardown "
                         "its `result`, or tear down from constants")
            # any other bare name: the only implicit binding is `result`
            # (undo of an acquire) — locals don't exist, and the extern's
            # own parameters are not visible (no tier defines teardown
            # parameter capture)
            if result_type is not None:
                raise RevlError(
                    filename, e.line,
                    f"`{e.name}` is not declared — the `{slot}` slot of "
                    f"extern `{decl_name}` sees only the implicit `result` "
                    "binding",
                    hint="`result` is the acquired value; the extern's own "
                         "parameters are not visible to its teardown, so "
                         "anything else must travel as constants or through "
                         "other declared fns")
            raise RevlError(
                filename, e.line,
                f"`{e.name}` is not declared — the `{slot}` slot of extern "
                f"`{decl_name}` runs with no variables in scope",
                hint="an extern's own parameters are not visible to its "
                     "teardown; the undo must work from constants or from "
                     "other declared fns")
        if isinstance(e, ExprCall):
            if isinstance(e.callee, ExprVar):
                name = e.callee.name
                if name == decl_name:
                    raise RevlError(
                        filename, e.line,
                        f"extern `{decl_name}`'s `{slot}` cannot call the "
                        "extern itself",
                        hint="the teardown runs to invert this acquisition — "
                             "calling it again would re-acquire during "
                             "cleanup; call the declared inverse instead")
                if (name not in (types.get(FNS_KEY) or {})
                        and name not in (types.get(CASES_KEY) or {})
                        and name not in _HOST_CALLABLES
                        and name not in _BUILTIN_CONSTRUCTORS):
                    raise RevlError(
                        filename, e.line,
                        f"`{name}` is not declared — the `{slot}` slot of "
                        f"extern `{decl_name}` may only call a declared fn, "
                        "extern, or host builtin",
                        hint="an extern binds no callables (only the acquire's "
                             "`result` value is ever in scope), so every "
                             "callee must be a module-level declaration")
            elif isinstance(e.callee, ExprField):
                raise RevlError(
                    filename, e.line,
                    f"the `{slot}` slot of extern `{decl_name}` must be a "
                    "plain call to a declared fn or extern",
                    hint="host objects cannot be acquired inside a teardown "
                         "expression; call the declared inverse directly")
            for a in e.args:
                _walk(a)
            return
        # generic recursion over the dataclass children (bin operands, record
        # fields, match arms, template parts, ...)
        for f in type(e).__dataclass_fields__:
            v = getattr(e, f)
            if hasattr(v, "__dataclass_fields__"):
                _walk(v)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    if hasattr(x, "__dataclass_fields__"):
                        _walk(x)
                    elif isinstance(x, (list, tuple)):
                        for y in x:
                            if hasattr(y, "__dataclass_fields__"):
                                _walk(y)

    _walk(expr)
    # the tenv carries exactly what the slot binds: `result: T` for the undo
    # of an acquire with a declared return, nothing otherwise — so argument
    # type-checking sees the acquired value at its declared type
    tenv = {"result": result_type} if result_type is not None else {}
    check_ast(expr, None, tenv, types, filename,
              f"{slot} of extern `{decl_name}`")


_UNIT_RETURNS = (None, "Unit")


def _check_deferred_extern(decl, filename: str) -> None:
    """The `deferred` checker obligations (docs/design/245-session-commit.md,
    Decision 2). Every rule keeps class (b) — the deferrable emission — an exact
    type judgment: the checker can enforce the abort/commit semantics only
    because these hold at declaration.

    1. `deferred` only on an `emission`. `pure` has nothing to defer; `acquire`
       and `witnessed` must run mid-session (their return is the resource or the
       witness).
    2. A deferred emission returns `Unit`. The call completes before the action
       fires, so no value can flow back; a non-Unit return would be a lie the
       program could branch on. This is also the mechanical (b)/(c) boundary: an
       emission whose response the task needs mid-session cannot type `deferred`.
    3. `deferred` and `compensate` are mutually exclusive. A compensation offsets
       a fired emission on abort; a deferred emission's abort drops the queue
       (nothing fired, nothing to offset), so the pair is dead code.
    4. `deferred` and `async` are mutually exclusive (v1): there is no response
       to await.
    """
    if decl.classification != "emission":
        raise RevlError(
            filename, decl.line,
            f"`deferred` is only valid on an `emission` extern; `{decl.name}` is "
            f"`{decl.classification}`",
            hint="a `pure` extern has nothing to defer, and an `acquire` or "
                 "`witnessed` extern must run mid-session — its return is the "
                 "resource or the witness (docs/design/245-session-commit.md)",
            code="G4", category="deferred")
    if decl.returns not in _UNIT_RETURNS:
        raise RevlError(
            filename, decl.line,
            f"a `deferred` emission must return `Unit`; `{decl.name}` returns "
            f"`{decl.returns}`",
            hint="a deferred emission returns before the world changes, so no "
                 "value can flow back from it — an emission whose response the "
                 "task needs mid-session stays an immediate emission (drop "
                 "`deferred`)",
            code="G4", category="deferred")
    if decl.compensate is not None:
        raise RevlError(
            filename, decl.line,
            f"a `deferred` emission cannot declare `compensate`; `{decl.name}` "
            f"declares both",
            hint="a compensation offsets a fired emission on abort, but a "
                 "deferred emission's abort drops the queue — nothing fired, so "
                 "there is nothing to offset. The pair is dead code by "
                 "construction (docs/design/245-session-commit.md)",
            code="G5", category="deferred")
    if decl.async_:
        raise RevlError(
            filename, decl.line,
            f"a `deferred` emission cannot be `async` (v1); `{decl.name}` is both",
            hint="a deferred emission returns Unit at the call and fires later at "
                 "the session commit, so there is no response to await",
            code="G4", category="deferred")


def _check_poly_extern(decl, filename: str) -> None:
    """The `fn|async` (caller-decided colour) checker obligations
    (docs/design/388-caller-decided-extern-colour.md, option a, stages 2 and 7).

    A poly extern is one authored host body whose colour is decided at the CALL
    SITE — a sync call site clones it to a `def`/`function`, an async call site
    to an `async def`/`async function`. That is sound only for a colour-agnostic
    (await-free) body, which the compiler cannot verify inside opaque host text
    (G8, item 24; the body is not sandboxed, docs/design/329-untrusted-author-
    profile.md). So these are the honest-by-review envelope plus one cheap lint:

    1. `fn|async` only on an `emission`. The colour it wears is the async/sync
       emission colour; `pure`/`acquire`/`witnessed` have no async story (they
       run on pure or synchronous teardown paths), exactly as a fixed `async`
       extern is emission-only (lower.py async-validity block).
    2. `fn|async` and a fixed `async` are mutually exclusive. A fixed `async`
       already picks the colour, so pairing it with the "either colour" marker is
       contradictory.
    3. `fn|async` and `deferred` are mutually exclusive. A deferred emission
       returns Unit at the call and fires later, so there is no call-site colour
       to decide (the same reason `deferred` and `async` are exclusive).
    4. A poly extern cannot declare `compensate`, exactly as a fixed `async`
       extern cannot (the compensation seam is synchronous on every tier).
    5. The `await`-keyword lint: refuse a `fn|async` `@py`/`@ts` body whose text
       contains the tier's suspend keyword (`await`). A colour-agnostic body
       cannot suspend, so its sync clone would be invalid host code (an `await`
       outside an `async def` is a Python SyntaxError). Stated honestly as a LINT,
       not a proof: a body can suspend without the keyword (an event-loop
       `run_until_complete`), and the keyword can appear inside a string literal.
    """
    if decl.classification != "emission":
        raise RevlError(
            filename, decl.line,
            f"`fn|async` (caller-decided colour) is only valid on an `emission` "
            f"extern; `{decl.name}` is `{decl.classification}`",
            hint="the marker chooses the sync-vs-async EMISSION colour at the call "
                 "site; a `pure` extern is callable from pure positions with no "
                 "async story, and an `acquire`/`witnessed` extern runs on the "
                 "synchronous teardown path (docs/design/388-caller-decided-"
                 "extern-colour.md)")
    if decl.async_:
        raise RevlError(
            filename, decl.line,
            f"extern `{decl.name}` is both `async` and `fn|async` — a fixed "
            f"`async` already picks the colour",
            hint="drop the `async` modifier to let the call site decide the "
                 "colour, or drop `|async` to fix it async (item 388)")
    if decl.deferred:
        raise RevlError(
            filename, decl.line,
            f"a `fn|async` emission cannot be `deferred`; `{decl.name}` is both",
            hint="a deferred emission returns Unit at the call and fires at the "
                 "session commit, so there is no call-site colour to decide "
                 "(item 388)")
    if decl.compensate is not None:
        raise RevlError(
            filename, decl.line,
            f"a `fn|async` emission cannot declare `compensate`; `{decl.name}` "
            f"declares both",
            hint="the compensation seam is synchronous on every tier, exactly as "
                 "for a fixed `async` extern (item 388)")
    for body in decl.bodies:
        # word-boundary match so `await` the keyword is caught while an
        # identifier like `awaited_result` or `no_await` is not.
        if re.search(r"\bawait\b", body.text):
            raise RevlError(
                filename, body.line,
                f"the `fn|async` extern `{decl.name}` has an `@{body.backend}` "
                f"body containing `await`, but a caller-decided-colour body must "
                f"be colour-agnostic (await-free)",
                hint="a `fn|async` body is cloned into a sync `def`/`function` at "
                     "sync call sites, where an `await` is invalid — author the "
                     "suspending work as a fixed `async` extern instead, or (once "
                     "item 373 lands) share only the await-free span through a "
                     "host-body fragment (item 388)")


def _check_deferred_not_in_teardown(program, filename: str) -> None:
    """Rule: deferred emissions are refused in teardown positions — the `undo`
    and `compensate` slots (docs/design/245-session-commit.md, Decision 2).
    Teardown runs at or after the verdict; enqueueing into a queue that is
    already flushing or dropped is unanswerable (the same spirit as 247's "a
    compensation emits and returns; it does not accumulate")."""
    from .parser import ExprCall, ExprVar

    deferred = {d.name for d in program.externs if d.deferred}
    if not deferred:
        return

    def _scan(expr, decl_name: str, slot: str) -> None:
        if isinstance(expr, ExprCall):
            callee = expr.callee
            if isinstance(callee, ExprVar) and callee.name in deferred:
                raise RevlError(
                    filename, callee.line,
                    f"the `{slot}` slot of extern `{decl_name}` calls deferred "
                    f"emission `{callee.name}` — deferred emissions are refused "
                    f"in teardown positions",
                    hint="teardown runs at or after the session verdict; a "
                         "deferred emission would enqueue into a queue that is "
                         "already flushing or dropped, which is unanswerable "
                         "(docs/design/245-session-commit.md)",
                    code="G5", category="deferred")
            for arg in expr.args or []:
                _scan(arg, decl_name, slot)

    for decl in program.externs:
        if decl.undo is not None:
            _scan(decl.undo, decl.name, "undo")
        if decl.compensate is not None:
            _scan(decl.compensate, decl.name, "compensate")


def _check_witnessed_inverse(decl, extern_class: dict, emitting_fns: set,
                             emitting_witness: dict, filename: str) -> None:
    """Rule 3 (docs/design/243-witnessed-externs.md): a witnessed extern's
    declared inverse must be classified **non-emission AND non-witnessed**.

    An emission inverse would cross a one-way boundary during teardown — the
    exact thing G5's no-emission-in-undo forbids, and it degrades a proof-grade
    rollback into best-effort residue. A witnessed inverse is infinite regress:
    its own inverse would need registering, and so on. The declared inverse is
    a host-LOCAL restore, so only `pure`/`acquire` callees are admissible.

    `undo some_emission(result)` passes `_check_extern_undo` today (the shared
    `mode="undo"` machinery only checks the callee is *declared*), so this walk
    is the explicit close: every extern a witnessed `undo` calls is held to the
    rule, which also fences the latent acquire hole the same expression opens.

    The extern-name check alone was an escape (the 330->329-transitive shape on
    the teardown path): a callee that is a plain top-level `fn` is not in
    `extern_class`, so it passed, yet a `fn` body may itself reach an emission
    (a legal emitting fn), so the emission still fires on abort, invisible to
    every fold and the approval gate. `emitting_fns` is the emission-reach fixed
    point over the fn call graph (emission_analysis.py): a fn is in it iff its
    call transitively reaches an `emission`/`witnessed` extern. Refusing a
    callee found there follows fn calls transitively, so a fn-wrapped emission in
    an inverse is refused exactly like a direct one. `compensate` is walked
    alongside `undo` so the same escape cannot open on that slot when a backend
    wires it. A pure/local fn (no emission reach) is in neither table and still
    passes."""
    from .parser import ExprCall, ExprVar

    def _walk(e):
        if isinstance(e, ExprCall):
            if isinstance(e.callee, ExprVar):
                name = e.callee.name
                bad = extern_class.get(name)
                if bad in ("emission", "witnessed"):
                    kind = ("an emission" if bad == "emission"
                            else "itself witnessed")
                    why = ("emissions are one-way boundary crossings and may not "
                           "run in teardown (G5)" if bad == "emission"
                           else "a witnessed inverse would need its own inverse "
                                "registered — infinite regress")
                    raise RevlError(
                        filename, e.line,
                        f"the inverse of witnessed extern `{decl.name}` calls "
                        f"`{name}`, which is {kind} — a witnessed inverse must be "
                        f"a host-local restore (G5)",
                        hint=f"{why}; declare the inverse `pure` or `acquire` "
                             f"(docs/design/243-witnessed-externs.md)",
                        code="G5", category="witnessed",
                    )
                if bad is None and name in emitting_fns:
                    # a plain top-level `fn` whose body transitively reaches an
                    # emission: the same boundary crossing as a direct emission
                    # inverse, one `fn` indirection later. Name the reached
                    # boundary and the fn path so the author reads the derivation.
                    chain = _emission_chain(name, emitting_witness)
                    reached = chain[-1]
                    term = extern_class.get(reached)
                    kind = ("an emission" if term == "emission"
                            else "itself witnessed")
                    path = " -> ".join(chain)
                    raise RevlError(
                        filename, e.line,
                        f"the inverse of witnessed extern `{decl.name}` calls "
                        f"`{name}`, a fn that reaches {kind} `{reached}` "
                        f"(through {path}), so a witnessed inverse must be a "
                        f"host-local restore (G5)",
                        hint="emissions are one-way boundary crossings and may "
                             "not run in teardown, even through a fn wrapper "
                             "(G5); declare the inverse `pure` or `acquire`, or "
                             "route no emission through it "
                             "(docs/design/243-witnessed-externs.md)",
                        code="G5", category="witnessed",
                    )
            for a in e.args:
                _walk(a)
            return
        for f in type(e).__dataclass_fields__:
            v = getattr(e, f)
            if hasattr(v, "__dataclass_fields__"):
                _walk(v)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    if hasattr(x, "__dataclass_fields__"):
                        _walk(x)

    _walk(decl.undo)
    if decl.compensate is not None:
        _walk(decl.compensate)


# item 309: the idempotency register partial order (design §2, §"question 4").
# `declared` (a trust-me claim) is the floor; `keyed` (dedup-by-construction on a
# named key) and `shape-proven` (a statically-checked restore-to-recorded-value
# native body) are the two STRONG peers, either satisfying a strong floor. The
# 290 `requires register <level>` and 309 `requires idempotent-teardown(strength)`
# policies read this order.
# `strong` is a policy FLOOR level only (never an actual IR register): it means
# "any strong register", so it ranks with the two strong peers at 1.
_REGISTER_RANK = {"declared": 0, "keyed": 1, "shape-proven": 1, "strong": 1}


def _idempotent_register(decl) -> str:
    """The honesty register for `decl`'s idempotency claim (design §2).

    A keyed emission is dedup-safe BY CONSTRUCTION (`keyed`). A native inverse in
    restore-to-recorded-value form would be `shape-proven`, but that check needs
    244's revl-expressed bodies — TODO(309-slice4): detect the shape and upgrade
    an `undo idempotent` native body to `shape-proven`. Every other claim (a bare
    `idempotent` emission, an `undo idempotent` over a host body) is the author's
    `declared` claim, machine-checked only for shape."""
    if decl.idempotency_key is not None:
        return "keyed"
    return "declared"


def _register_satisfies(actual: str | None, floor: str) -> bool:
    """True when the register `actual` meets or exceeds the policy `floor` under
    309's PARTIAL order. `None` (no idempotency claim) never satisfies a floor.
    `keyed` and `shape-proven` are peers: either satisfies a strong floor, and
    neither satisfies the other only if the floor names the specific peer — which
    the strength grammar never does (it names `declared`/`keyed`/`shape-proven`
    as a MINIMUM rank, so the two strong forms are interchangeable at rank 1)."""
    if actual is None:
        return False
    return _REGISTER_RANK.get(actual, -1) >= _REGISTER_RANK.get(floor, 0)


def _witnessed_extern_names(program: Program) -> set[str]:
    """Names of every `witnessed`-classified extern declared in *program*
    (docs/design/243-witnessed-externs.md). Shared by the effect-position
    refusal (rule 1) below and the call-site transactional lowering
    (Slice 2, `_lower_component`)."""
    return {ext.name for ext in program.externs if ext.classification == "witnessed"}


def _refuse_witnessed_outside_effect_position(program: Program, filename: str) -> None:
    """Rule 1 (docs/design/243-witnessed-externs.md): a witnessed extern is
    refused outside effect position — no bare call from a plain `fn`/`test`
    body.

    A witnessed mutation is reversible only because the teardown accumulator
    auto-registers its declared inverse. A `fn`/`test` body has no accumulator,
    so a call there would mutate the host with the inverse dropped on the floor
    — silently irreversible, the one outcome the classification exists to
    prevent. Every extern is otherwise callable from a fn/test body (they seed
    the `callables` set), so this is an explicit refusal, checked over the
    author's AST where the call site still has a line."""
    from .parser import ExprCall, ExprVar

    witnessed = _witnessed_extern_names(program)
    if not witnessed:
        return

    def _walk(node, where: str):
        if isinstance(node, ExprCall) and isinstance(node.callee, ExprVar) \
                and node.callee.name in witnessed:
            raise RevlError(
                filename, node.line,
                f"witnessed extern `{node.callee.name}` cannot be called in {where} "
                f"— a witnessed mutation is only valid in effect position (G4)",
                hint="its declared inverse is auto-registered by the teardown "
                     "accumulator, which exists only in a component activation; a "
                     "fn/test body has none, so the mutation would be irreversible "
                     "(docs/design/243-witnessed-externs.md)",
                code="G4", category="witnessed",
            )
        if hasattr(node, "__dataclass_fields__"):
            for f in type(node).__dataclass_fields__:
                _walk(getattr(node, f), where)
        elif isinstance(node, (list, tuple)):
            for x in node:
                _walk(x, where)

    for fn in program.fn_decls:
        for stmt in fn.body:
            _walk(stmt, f"the body of fn `{fn.name}`")
    for test in program.tests:
        for stmt in test.body:
            _walk(stmt, f"the body of test `{test.name}`")


def _refuse_teardown_bound_externs_in_fn_body(program: Program, filename: str) -> None:
    """Items 399 and 400: the acquire-with-`undo` and `deferred`-emission twins
    of the witnessed rule-1 refusal above. Each carries effect machinery that
    exists only in effect position, so a bare call from a plain `fn`/`test` body
    would silently drop it:

    * item 399: an `acquire` extern that declares an `undo` has no teardown
      accumulator to auto-register that `undo` in a fn/test body, so the teardown
      is dropped on every path and the acquisition is silently irreversible.
    * item 400: a `deferred` emission has no session commit in a fn/test body to
      defer to, so it fires immediately (once per loop iteration), bypassing the
      commit queue that `deferred` exists to feed (item 245: deferred emissions
      fire at commit, an abort drops the queue).

    An `acquire` with NO `undo`, and a non-deferred emission, are unaffected;
    both stay callable from a fn/test body. Checked over the author's AST, beside
    the witnessed refusal, where the call site still has a line."""
    from .parser import ExprCall, ExprVar

    acquire_undo = {
        ext.name for ext in program.externs
        if ext.classification == "acquire" and ext.undo is not None}
    deferred = {ext.name for ext in program.externs if ext.deferred}
    if not acquire_undo and not deferred:
        return

    def _walk(node, where: str):
        if isinstance(node, ExprCall) and isinstance(node.callee, ExprVar):
            name = node.callee.name
            if name in acquire_undo:
                raise RevlError(
                    filename, node.line,
                    f"`acquire` extern `{name}` cannot be called in {where}; its "
                    f"declared `undo` teardown would be dropped, leaving the "
                    f"acquisition irreversible (G4)",
                    hint="an `acquire` extern's `undo` is auto-registered by the "
                         "teardown accumulator, which exists only in a component "
                         "activation or provide-method body; call it from there so "
                         "the teardown can run (docs/design/243-witnessed-"
                         "externs.md)",
                    code="G4", category="acquire",
                )
            if name in deferred:
                raise RevlError(
                    filename, node.line,
                    f"`deferred` emission extern `{name}` cannot be called in "
                    f"{where}; a fn/test body has no session commit for the "
                    f"deferral to fire at (G4)",
                    hint="a deferred emission enqueues onto the session deferral "
                         "queue and fires at commit; that queue exists only inside "
                         "a component activation or provide-method body, so call it "
                         "from there (docs/design/245-session-commit.md)",
                    code="G4", category="deferred",
                )
        if hasattr(node, "__dataclass_fields__"):
            for f in type(node).__dataclass_fields__:
                _walk(getattr(node, f), where)
        elif isinstance(node, (list, tuple)):
            for x in node:
                _walk(x, where)

    for fn in program.fn_decls:
        for stmt in fn.body:
            _walk(stmt, f"the body of fn `{fn.name}`")
    for test in program.tests:
        for stmt in test.body:
            _walk(stmt, f"the body of test `{test.name}`")


def _lower_externs(program: Program, filename: str, types: dict,
                   fns: list | None = None) -> list:
    externs: list[dict] = []
    # name -> first declaring file, so a cross-module duplicate names BOTH files
    # (roadmap 394), same as `_lower_fns`. Externs carry provenance in
    # `program.decl_files` (they have no `.source` field of their own).
    seen: dict[str, str] = {}
    # classification of every extern by name, so a declared inverse can be held
    # to the "non-emission AND non-witnessed" rule (docs/design/243 rule 3): an
    # inverse that itself emits crosses a one-way boundary in teardown (G5), and
    # a witnessed inverse is infinite regress. Built before the loop so an
    # inverse may name an extern declared later in the file.
    extern_class = {d.name: d.classification for d in program.externs}
    # G5 transitive teardown guard: a witnessed inverse that calls a plain `fn`
    # which itself reaches an emission is as much a teardown emission as a direct
    # one (the 330->329-transitive shape on the teardown path). Reuse the
    # emission-reach fixed point over the lowered fn bodies (emission_analysis.py),
    # seeded from the extern classifications, so `_check_witnessed_inverse` can
    # follow fn calls transitively instead of inspecting extern names only.
    # Computed once, lazily, and only when a witnessed extern actually declares an
    # inverse, so every other program stays byte-identical and pays nothing.
    _inverse_reach: dict = {}

    def _witnessed_inverse_reach():
        if not _inverse_reach:
            witness: dict[str, str] = {}
            extern_seed = [
                {"name": d.name, "class": d.classification,
                 "capabilities": list(d.capabilities or [])}
                for d in program.externs]
            reach = set(_emitting_capabilities(fns or [], extern_seed, witness))
            _inverse_reach["fns"] = reach
            _inverse_reach["witness"] = witness
        return _inverse_reach["fns"], _inverse_reach["witness"]
    # item 245: a deferred emission may not appear in any extern's teardown slot
    # (a whole-program check, so an `undo`/`compensate` naming a deferred extern
    # declared later in the file is still caught).
    _check_deferred_not_in_teardown(program, filename)
    for decl in program.externs:
        decl_file = program.decl_files.get(id(decl), filename)
        if decl.name in seen:
            first_file = seen[decl.name]
            if os.path.abspath(first_file) == os.path.abspath(decl_file):
                # a same-file re-declaration: keep the terse single-file message
                raise RevlError(decl_file, decl.line,
                                f"duplicate extern `{decl.name}`")
            raise RevlError(
                decl_file, decl.line,
                _duplicate_symbol_message("extern", decl.name,
                                          first_file, decl_file))
        seen[decl.name] = decl_file
        if decl.classification == "acquire" and decl.undo is None:
            raise RevlError(
                filename, decl.line,
                f"acquire extern `{decl.name}` must declare `undo` (G4)",
                hint="an `acquire` crosses into an observable effect and needs a teardown inverse",
            )
        # R0 (item 308): an `acquire` return must be a NOMINAL OPAQUE HANDLE
        # type. A primitive (`Int`) or a structural carrier (`Result[..]`,
        # `Opt[..]`, `List[..]`, a function type, a record literal) is refused
        # at the declaration. The acquire returns are the resource-taint base
        # (`resources.resource_base`); promoting that base into the frontend
        # ownership checks means the base must carry identity, and a primitive
        # cannot — `-> Int` would make every integer a borrowed handle and the
        # language unwritable (design doc 308, R0).
        if (decl.classification == "acquire" and decl.returns is not None
                and decl.returns not in NO_HANDLE_RETURNS
                and not acquire_return_is_nominal_handle(decl.returns)):
            raise RevlError(
                filename, decl.line,
                f"acquire extern `{decl.name}` returns `{decl.returns}`, which is "
                f"not a nominal opaque handle type (item 308, R0)",
                hint="an `acquire` return is an owned resource handle whose "
                     "identity the ownership checks track; declare an opaque "
                     "handle type (a bare nominal like `LogHandle`) and return "
                     "that, not a primitive or a structural carrier "
                     "(`Result[..]`, `Opt[..]`, `List[..]`, a function type)",
            )
        if decl.classification == "pure" and (decl.undo is not None or decl.compensate is not None):
            raise RevlError(
                filename, decl.line,
                f"pure extern `{decl.name}` cannot declare `undo` or `compensate`",
                hint="`pure` means no observable effect, so there is nothing to invert or compensate",
            )
        if decl.classification == "emission" and decl.undo is not None:
            raise RevlError(
                filename, decl.line,
                f"emission extern `{decl.name}` cannot declare `undo`",
                hint="emissions are one-way boundary crossings; use `compensate` for a best-effort cleanup",
            )
        # item 246: `requires approval` gates a boundary CROSSING, and only an
        # `emission` extern crosses irreversibly. A witnessed extern is already
        # reversible (class a, auto-approved), a pure/acquire one crosses nothing,
        # so the clause is meaningless there — reject the claim, don't drop it.
        if decl.requires_approval and decl.classification != "emission":
            raise RevlError(
                filename, decl.line,
                f"`{decl.classification}` extern `{decl.name}` cannot declare "
                f"`requires approval`",
                hint="only an `emission` extern crosses irreversibly; a witnessed "
                     "extern is already revertible and a pure/acquire one crosses "
                     "no boundary, so there is nothing to gate (item 246)",
            )
        # item 373: the reach clause `emission(confined: <param>)` names what an
        # irreversible crossing is BOUNDED to, so `revl audit` can show the one
        # property a reviewer needs (the confinement) and `audit --diff` can flag
        # a weakening. Two rules, enforced here next to the sibling classification
        # checks:
        #   (1) reach is emission-only — a witnessed extern is already reversible,
        #       a pure/acquire one crosses no boundary, so "confined to" is
        #       meaningless there; reject the claim rather than drop it.
        #   (2) the confinement TARGET must name a real PARAMETER, not a literal
        #       the host body picks. This is the partial check that makes the
        #       otherwise trust-me reach honest-by-review: a host body that
        #       ignores the parameter and confines to a baked-in fallback is now a
        #       reviewable lie, and a reach naming a non-parameter fails to compile.
        if decl.reach is not None:
            reach_kind, reach_target = decl.reach
            if decl.classification != "emission":
                raise RevlError(
                    filename, decl.line,
                    f"`{decl.classification}` extern `{decl.name}` cannot declare a "
                    f"`({reach_kind}: ...)` reach clause",
                    hint="only an `emission` crosses irreversibly, so only an emission "
                         "is worth bounding; a witnessed extern is already revertible and "
                         "a pure/acquire one crosses no boundary (item 373)",
                )
            param_names = {p.name for p in decl.params}
            if reach_target not in param_names:
                params_list = ", ".join(sorted(param_names)) or "(none)"
                raise RevlError(
                    filename, decl.line,
                    f"emission `{decl.name}` is `confined: {reach_target}`, but "
                    f"`{reach_target}` is not one of its parameters ({params_list})",
                    hint="the confinement target must name a PARAMETER — the region an "
                         "emission is bounded to has to be caller-supplied data the host "
                         "body cannot swap for a literal, or the reach claim is "
                         "unreviewable (item 373)",
                )
        # item 309: the `idempotent` emission modifier and its `idempotent(key: p)`
        # keyed form. Two rules, enforced here next to the sibling reach checks:
        #   (1) `idempotent`/`idempotent(key:)` is EMISSION-ONLY. It is item-44's
        #       delivery claim (the remote treats re-delivery as delivery), and
        #       only an emission crosses to a remote. Placed on a witnessed/acquire
        #       extern it would read as a keyed INVERSE, which 243 rule 3 forbids
        #       (inverses are non-emission); reject the claim rather than drop it.
        #   (2) the key TARGET must name a real PARAMETER and be scalar-
        #       serializable (`Str`/`Int`), so a fresh process can read the same
        #       key VALUE off the WAL descriptor on every re-issue (§1b, §1c).
        if decl.idempotent and decl.classification != "emission":
            raise RevlError(
                filename, decl.line,
                f"`{decl.classification}` extern `{decl.name}` cannot be declared "
                f"`idempotent`",
                hint="`idempotent` is a delivery claim about a boundary crossing, "
                     "so it is emission-only; a witnessed/acquire reversal is a "
                     "non-emission INVERSE (243 rule 3), and its at-most-once claim "
                     "is spelled `undo idempotent <inverse>(result)` instead "
                     "(docs/design/309-idempotent-inverse.md, §1b)",
                code="G4", category="idempotent")
        if decl.idempotency_key is not None:
            param_by_name = {p.name: p for p in decl.params}
            if decl.idempotency_key not in param_by_name:
                params_list = ", ".join(sorted(param_by_name)) or "(none)"
                raise RevlError(
                    filename, decl.line,
                    f"emission `{decl.name}` declares `idempotent(key: "
                    f"{decl.idempotency_key})`, but `{decl.idempotency_key}` is not "
                    f"one of its parameters ({params_list})",
                    hint="the idempotency key names the PARAMETER whose per-call "
                         "value the remote dedups on; it must resolve against the "
                         "signature (item 309, §1b)",
                    code="G4", category="idempotent")
            key_type = param_by_name[decl.idempotency_key].type
            if key_type not in ("Str", "Int"):
                raise RevlError(
                    filename, decl.line,
                    f"emission `{decl.name}` idempotency key "
                    f"`{decl.idempotency_key}` has type `{key_type}`, which is not "
                    f"scalar-serializable",
                    hint="an idempotency key rides the WAL descriptor as durable "
                         "data every re-issue reads back, so it must be a "
                         "serializable scalar (`Str` or `Int`), not a host handle "
                         "or a compound type (item 309, §1b)",
                    code="G4", category="idempotent")
        # witnessed-inverse externs (docs/design/243-witnessed-externs.md). A
        # witnessed mutation is a transaction, not a bracket: its declared `undo`
        # is auto-registered by the accumulator and replays on abort only. The
        # correctness envelope is checked here, before the descriptor reaches the
        # IR — the Slice-2 runtime seam trusts these invariants.
        witness_type: str | None = None
        if decl.classification == "witnessed":
            if decl.compensate is not None:
                raise RevlError(
                    filename, decl.line,
                    f"witnessed extern `{decl.name}` cannot declare `compensate`",
                    hint="a witnessed mutation carries a proof-grade `undo`; "
                         "`compensate` is the best-effort form for one-way emissions",
                    code="G5", category="witnessed",
                )
            if decl.undo is None:
                raise RevlError(
                    filename, decl.line,
                    f"witnessed extern `{decl.name}` must declare `undo` (G4)",
                    hint="a witnessed mutation is reversible only because it names the "
                         "inverse the accumulator auto-registers; declare "
                         "`undo <inverse>(result)`",
                    code="G4", category="witnessed",
                )
            # The witness is the return value, and it must be a `Result[W, E]` so
            # the inverse registers on `Ok` only (a failed mutation touched
            # nothing): rule "registered only on Ok" (docs/design/243).
            head, args = parse_type(decl.returns)
            if head != "Result" or len(args) != 2:
                raise RevlError(
                    filename, decl.line,
                    f"witnessed extern `{decl.name}` must return "
                    f"`Result[Witness, Error]` — the fallible witness the inverse "
                    f"binds (G4)",
                    hint="every host mutation can fail (ENOENT/EPERM/disk-full); the "
                         "inverse is auto-registered on the `Ok` branch only, so the "
                         "return must be a `Result`",
                    code="G4", category="witnessed",
                )
            witness_type = args[0]
            # The witness must be WAL-serializable DATA, not a host handle: a
            # crash leaves only the write-ahead log, and an inverse closing over
            # an in-process object is residue, not recovery (recovery.py). A host
            # object type (Map/Pool/Job) cannot be reconstructed from the WAL.
            wt_head, _ = parse_type(witness_type)
            if wt_head in _HOST_CALLABLES:
                raise RevlError(
                    filename, decl.line,
                    f"witnessed extern `{decl.name}` witness `{witness_type}` is a "
                    f"host object — the witness must be WAL-serializable data (G8)",
                    hint="a host handle dies with the process; after a crash only the "
                         "write-ahead log survives, so the witness must be durable data "
                         "(paths, refs, a record) the inverse can be rebuilt from",
                    code="G8", category="witnessed",
                )
        # `async` validity (docs/design/async-extern.md §1): the modifier is
        # legal only on `emission` externs, and an async extern may not declare
        # `compensate` in v1 (the compensation seam is synchronous on every
        # tier). These sit next to the classification rules above and refuse
        # with honest messages before the flag reaches the IR.
        if decl.async_:
            if decl.classification == "pure":
                raise RevlError(
                    filename, decl.line,
                    f"`pure` extern `{decl.name}` cannot be `async` — a suspension is "
                    f"observable; classify it `emission`",
                    hint="pure externs are callable from every pure position (tests, "
                         "match guards, undo slots), which have no async story",
                )
            if decl.classification == "acquire":
                raise RevlError(
                    filename, decl.line,
                    f"`acquire` extern `{decl.name}` cannot be `async` yet — an "
                    f"acquire's `undo` runs on the synchronous teardown/unwind path",
                    hint="classify it `emission`, or file the need if an awaited "
                         "teardown ever becomes real",
                )
            if decl.classification == "witnessed":
                raise RevlError(
                    filename, decl.line,
                    f"`witnessed` extern `{decl.name}` cannot be `async` yet — its "
                    f"declared inverse replays on the synchronous abort/teardown path",
                    hint="classify it `emission`, or file the need if an awaited "
                         "rollback ever becomes real",
                )
            if decl.compensate is not None:
                raise RevlError(
                    filename, decl.line,
                    "an `async` extern cannot declare `compensate` yet — the "
                    "compensation seam is synchronous on every tier",
                )
        # `deferred` validity (docs/design/245-session-commit.md, Decision 2).
        # The class of an action is a total function of its checked
        # classification: nothing at runtime or in the harness can move an
        # action between classes, so the rules that keep class (b) honest are
        # enforced here, before the flag reaches the IR.
        if decl.deferred:
            _check_deferred_extern(decl, filename)
        # item 388: the caller-decided colour marker `fn|async`. A poly extern
        # fixes no colour; lowering splits it into concrete sync/async clones per
        # call-site colour (below, in the component-lowering post-pass). Its
        # validity envelope mirrors the `async` one directly above, because a poly
        # extern IS an emission that may be emitted async: emission-only, not
        # `deferred`, not ALSO a fixed `async` (the two spellings are mutually
        # exclusive — a fixed `async` already picks the colour), and no
        # `compensate` (the compensation seam is synchronous, exactly as for a
        # fixed `async` extern). Plus the cheap per-backend `await`-keyword lint:
        # a colour-agnostic body cannot suspend, so an `await` in a `fn|async`
        # host body is refused. This is a LINT, not a proof — colour-agnosticism
        # is unprovable inside opaque host text (G8, item 24) — but it catches the
        # common authoring mistake at compile time.
        # item 396 option B: a `HostRef` body has no author-written TEXT, so the
        # colour-poly await-lint (which scans body text) and 388's clone
        # synthesis (which deep-copies `bodies`, not `refs`) do not cover it.
        # Rather than risk a silently-dropped ref clone, refuse the poly+ref
        # combination outright with a clear redirect (the decision recorded for
        # item 388 composition). Fixed-colour refs compose freely.
        from .parser import HostRef as _HostRef  # noqa: PLC0415
        if decl.colour_poly and any(isinstance(b, _HostRef) for b in decl.bodies):
            offender = next(b for b in decl.bodies if isinstance(b, _HostRef))
            raise RevlError(
                filename, offender.line,
                f"a `fn|async` (caller-decided-colour) extern cannot use a "
                f"host-module ref; `{decl.name}` declares both",
                hint="a poly extern is cloned per call-site colour, and one host "
                     "symbol cannot be both a coroutine and a plain callable, so "
                     "a ref cannot serve both clones. Fix the colour "
                     "(`async fn`/`fn`) and ref a matching sync-or-async symbol, "
                     "or use an inline body (item 396 option B / 388)")
        if decl.colour_poly:
            _check_poly_extern(decl, filename)
        # item 379 / option (b) of docs/design/378-sync-extern-service-reach.md:
        # validate the typed `config` schema the same way a component's config is
        # validated (lower.py:4681 default-type compatibility). Config is STATIC
        # data resolved once at plug, so there is no service reach and no async
        # op: A1 and the capability gate are untouched (design "(b)"). Duplicate
        # field names are refused, and a non-`null` default must fit its declared
        # field type. The schema reaches the IR below only when non-empty, so an
        # extern with no `config` block is byte-identical.
        seen_cfg: set[str] = set()
        for cfg in decl.config:
            if cfg.name in seen_cfg:
                raise RevlError(filename, cfg.line,
                                f"duplicate config field `{cfg.name}` in extern `{decl.name}`")
            seen_cfg.add(cfg.name)
            if cfg.default is None:
                continue
            lit_type = _config_default_type(cfg.default)
            if lit_type is not None and not compatible(cfg.type, lit_type):
                raise mismatch(filename, cfg.line,
                               f"config field `{cfg.name}` default of extern `{decl.name}`",
                               cfg.type, lit_type)
        # item 395 / Stage-5 TIER GATE (Fable review, before ts/go/rust/java
        # config injection): option (b)'s config coeffect binds `_revl_config` in
        # the extern body ONLY on a tier whose emitter has the injection seam
        # (`_CONFIG_INJECTION_TIERS`, py-only today). A config extern that carries
        # a host body for a seam-less tier would emit that body with `_revl_config`
        # UNBOUND — a late, mis-attributed failure (runtime ReferenceError on ts;
        # a compile error of the emitted artifact on go/rust/java). Refuse the
        # whole hazard class HERE, at compile, naming the offending tier and
        # redirecting to option (c). Gated on `decl.config`, so a non-config
        # extern with any host body is untouched (byte-identical). Ordered by the
        # author's @-body spelling for a deterministic first offender.
        from .parser import HostBodyFile, HostRef  # noqa: PLC0415
        if decl.config:
            for body in decl.bodies:
                # item 396 option B: a `config` extern binds `_revl_config` as
                # the first line of the emitted BODY for that text to read; a ref
                # has no author-written body, so binding it inside the thunk would
                # bind a name the referenced host symbol cannot see. Refuse
                # config+ref with a redirect to an inline body that forwards it.
                if isinstance(body, HostRef):
                    raise RevlError(
                        decl_file, body.line,
                        f"a config extern cannot use a host-module ref; "
                        f"`{decl.name}` declares both",
                        hint="`config` binds `_revl_config` inside the emitted "
                             "body for the body text to read, but a ref has no "
                             "body text — the referenced host symbol never sees "
                             "the name. Use an inline `= @backend { ... }` body "
                             "that reads `_revl_config` and forwards it to the "
                             "host call (item 396 option B).")
                if body.backend not in _CONFIG_INJECTION_TIERS:
                    raise RevlError(
                        decl_file, body.line,
                        f"extern config is not yet supported on the @{body.backend} "
                        f"tier (config extern `{decl.name}`)",
                        hint=(
                            f"only the @py emitter binds `_revl_config` from the "
                            f"plug-time config map today; a @{body.backend} body "
                            f"would emit with `_revl_config` unbound. Use option "
                            f"(c): give the mechanism a home component that "
                            f"`requires` the service "
                            f"(docs/design/378-sync-extern-service-reach.md)."),
                    )
        bodies: dict[str, str] = {}
        # item 396: provenance for a body SPLICED from an external file. Absent
        # unless the file form is used, so every existing extern's IR is
        # byte-identical. Records the written path and the sha256 of the raw
        # file bytes so two compiles are byte-comparable and `revl verify` can
        # re-hash the file.
        body_files: dict[str, dict] = {}
        # item 396 option B: a per-tier host-MODULE ref (symbol + root-relative
        # path + pinned content hash). Additive next to `bodies`; a backend key
        # may appear in `bodies` OR `refs` but never both (the duplicate-body
        # refusal, extended). Absent unless a ref is used, so every existing IR
        # is byte-identical.
        refs: dict[str, dict] = {}
        for body in decl.bodies:
            if isinstance(body, HostBodyFile):
                # The compiler's body-file resolver replaces every HostBodyFile
                # with a resolved HostBody before lowering; reaching here means
                # the resolution seam was skipped (an internal error, not an
                # author error).
                raise RevlError(
                    filename, body.line,
                    f"internal: unresolved @{body.backend} host-body file for "
                    f"extern `{decl.name}` reached lowering (item 396)")
            if isinstance(body, HostRef):
                # tier gate (mirrors the config-injection gate): "import a symbol
                # from a checked file" is native on py and ts only; go/rust/java
                # lack a file-addressable module primitive and wasm cannot import
                # a file. Refuse the others at compile, naming the tier — never a
                # broken artifact. A tier joins EXTERN_REF_TIERS only behind its
                # own design note, not by analogy.
                if body.backend not in _EXTERN_REF_TIERS:
                    raise RevlError(
                        filename, body.line,
                        f"a host-module ref (`= @{body.backend} ref ...`) is not "
                        f"supported on the @{body.backend} tier (extern "
                        f"`{decl.name}`)",
                        hint="`ref` imports a symbol from a file-addressable host "
                             "module, which is native on @py and @ts only. go/"
                             "rust/java have no file-addressable import primitive "
                             "and wasm cannot import a file — each needs its own "
                             "design. Use an inline `= @backend { ... }` body or "
                             "a `= @backend file` splice on this tier "
                             "(item 396 option B).")
                if body.rel_path is None:
                    raise RevlError(
                        filename, body.line,
                        f"internal: unresolved @{body.backend} host-module ref "
                        f"for extern `{decl.name}` reached lowering (item 396)")
                if body.backend in bodies or body.backend in refs:
                    raise RevlError(filename, body.line,
                                    f"duplicate @{body.backend} body for extern `{decl.name}`")
                refs[body.backend] = {
                    "symbol": body.symbol,
                    "path": body.rel_path,
                    "sha256": body.sha256,
                    # item 410: the root KIND, ADDITIVE. Present only for an
                    # install-origin (stdlib / REVL_IMPORT_PATH) ref, so a
                    # user-origin ref IR is byte-identical to 396(B) (the
                    # 342/388 additivity discipline: absent, never `"user"`).
                    # Selects the runner's install root vs the user root at
                    # deploy.
                    **({"root": body.root_kind}
                       if getattr(body, "root_kind", None) else {}),
                }
                continue
            if body.backend in bodies or body.backend in refs:
                raise RevlError(filename, body.line,
                                f"duplicate @{body.backend} body for extern `{decl.name}`")
            bodies[body.backend] = body.text
            if body.source_path is not None:
                body_files[body.backend] = {"path": body.source_path,
                                            "sha256": body.sha256}
        entry: dict = {
            "name": decl.name,
            "class": decl.classification,
            "params": [{"name": p.name, "type": p.type} for p in decl.params],
            "returns": decl.returns,
            "bodies": bodies,
            # item 396 option A: file-splice provenance, present only when a
            # `= @backend file` body was used (additive: every existing IR is
            # byte-identical without it).
            **({"body_files": body_files} if body_files else {}),
            # item 396 option B: host-module refs (symbol + root-relative path +
            # pinned content hash), present only when a `= @backend ref` body was
            # used. The py/ts emitters generate a lazy import thunk from it and
            # the py driver hash-checks the file at plug (additive).
            **({"refs": refs} if refs else {}),
            # additive async flag (docs/design/async-extern.md §4), mirroring
            # the service-method spelling at lower.py:2583. Absent means sync;
            # `ir_version` stays 3 (confirmed human decision, §4).
            #
            # item 388: a poly extern (`fn|async`) is PRE-SEEDED here as the async
            # form (`async: True`) so awaited call sites resolve during the
            # coloring fixpoint and component lowering (the ordering wrinkle,
            # design §"honest hard part" #3). `colour_poly: True` marks the entry
            # for `_finalize_poly_externs`, the post-pass that — after every
            # component has recorded which colours its call sites requested —
            # keeps this entry only if an async call site exists, splits off a
            # `_revl_sync` clone only if a sync call site exists, and PRUNES the
            # unused colour. The marker is stripped there, so the final IR carries
            # only concrete clones and a poly extern nobody calls emits nothing
            # (additive, the item-342 property extended to externs).
            **({"async": True, "colour_poly": True} if decl.colour_poly
               else {"async": True} if decl.async_ else {}),
            # additive deferred flag (docs/design/245-session-commit.md,
            # Decision 2): class (b), the deferrable emission. Absent means the
            # emission fires at the call (class c). Only an `emission` extern may
            # carry it (checked above), so no non-emission extern's IR changes.
            **({"deferred": True} if decl.deferred else {}),
            # item 246: the declaration-owned `requires approval` floor. Absent
            # unless the author wrote it, so every existing extern's IR is
            # byte-identical. A crossing reaching this extern needs a covering
            # `with e` edge or lowering refuses (Decision 3).
            **({"requires_approval": True} if decl.requires_approval else {}),
            # item 373: the reach clause, recorded as `{"kind", "target"}`. Absent
            # unless the author wrote it, so every existing extern's IR is
            # byte-identical (a bare emission is "unconfined" = no key). `revl
            # audit` prints it and `audit --diff` reads it to flag a weakening.
            **({"reach": {"kind": decl.reach[0], "target": decl.reach[1]}}
               if decl.reach is not None else {}),
            # item 309: the idempotency register, ADDITIVE. Absent unless the
            # author declared one of the three surfaces, so every existing
            # extern's IR is byte-identical. `undo_idempotent` marks an
            # at-most-once INVERSE (undo slot); `idempotent`/`idempotency_key`
            # mark a re-deliverable emission (bare = trust-me, keyed = by
            # construction). `register` is the per-declaration honesty tier the
            # audit prints and the 290/309 policy floors read (partial order:
            # declared < keyed, declared < shape-proven; keyed/shape-proven are
            # peers) (docs/design/309-idempotent-inverse.md, §2, §"question 4").
            **({"undo_idempotent": True} if decl.undo_idempotent else {}),
            **({"idempotent": True} if decl.idempotent else {}),
            **({"idempotency_key": decl.idempotency_key}
               if decl.idempotency_key is not None else {}),
            **({"register": _idempotent_register(decl)}
               if (decl.idempotent or decl.undo_idempotent) else {}),
            # item 379: the typed config schema, in the SAME shape a component
            # carries it (lower.py:4956 `[{"name","type","default"}]`), so the
            # emitter and driver reuse the component config path verbatim. Absent
            # unless the author wrote a `config` block, so every existing extern's
            # IR is byte-identical.
            **({"config": [{"name": f.name, "type": f.type, "default": f.default}
                           for f in decl.config]} if decl.config else {}),
        }
        if decl.classification == "witnessed":
            # The witnessed descriptor the Slice-2 runtime teardown loop reads
            # (docs/design/243-witnessed-externs.md). `entry_kind: "transactional"`
            # is the SECOND accumulator entry kind — distinct from an acquire's
            # bracket: it replays on abort ONLY and is discharged (+ its witness
            # GC'd) on commit, where a bracket also replays on clean unload.
            # `ok_conditional` records that the inverse auto-registers on the
            # Result's `Ok` branch only; `witness` is the WAL-serializable data
            # type the inverse is reconstructed from.
            entry["entry_kind"] = "transactional"
            entry["revertible"] = True
            entry["ok_conditional"] = True
            entry["witness"] = witness_type
            if decl.capabilities:
                entry["capabilities"] = list(decl.capabilities)
        if decl.classification == "emission" and decl.capabilities:
            # item 343: a capability-scoped `emission[gateway.send]` records its
            # declared TOKEN in the IR, exactly as a witnessed extern does. The
            # 246 class->approval classifier and 344 standing grants read this to
            # key the crossing on the token instead of the extern name. Absent
            # (a bare `emission`) means no key is written, so every existing
            # emission extern's IR stays byte-identical (name-as-capability).
            entry["capabilities"] = list(decl.capabilities)
        if decl.undo is not None:
            # An acquire binds its declared return as `result`; a witnessed
            # extern binds the `Ok` witness (the Result's success payload), which
            # is exactly what the auto-registered inverse receives on abort.
            undo_result_type = (witness_type if decl.classification == "witnessed"
                                else decl.returns)
            # Classification of the inverse before its arg types: a witnessed or
            # emission inverse is refused on principle, whatever it is applied to.
            if decl.classification == "witnessed":
                reach, reach_witness = _witnessed_inverse_reach()
                _check_witnessed_inverse(decl, extern_class, reach,
                                         reach_witness, filename)
            _check_extern_undo(decl.undo, decl.name, "undo", types, filename,
                               result_type=undo_result_type)
            entry["undo"] = _lower_extern_expr(decl.undo, filename)
        if decl.compensate is not None:
            _check_extern_undo(decl.compensate, decl.name, "compensate",
                               types, filename)
            entry["compensate"] = _lower_extern_expr(decl.compensate, filename)
        externs.append(entry)
    return externs


def _lower_tests(program: Program, filename: str, types: dict,
                 services: dict | None = None) -> list:
    """Lower `test` and `lifecycle test` blocks to IR v3 test units (§7/§7.1).

    `types` (the case/signature table) is threaded through so a `test` body is
    the same expression scope a `fn` body is: nullary user-ADT constructors
    resolve as values (`let s = FirstTime`) and statements are type-checked.
    """
    if not program.tests:
        return []
    # A `test` body is the same callable scope a `fn` body is (mirrors
    # `_lower_fns`' `default_callables` and `_lower_prop_tests`): module `fn`s
    # AND `extern`s, plus the host callables/constructors already in fn scope.
    # Without the externs an in-module `extern pure fn` visible to fn bodies was
    # invisible inside a `test`, forcing every extern-backed helper to be
    # wrapped in a `fn` just to be unit-tested in-file (roadmap item 182).
    callables = (_HOST_CALLABLES | _BUILTIN_CONSTRUCTORS | _DECLASSIFY_BUILTINS
                 | {fn.name for fn in program.fn_decls}
                 | {ext.name for ext in program.externs})
    tests: list[dict] = []
    seen: set[str] = set()
    for decl in program.tests:
        if decl.name in seen:
            raise RevlError(filename, decl.line, f"duplicate test `{decl.name}`")
        seen.add(decl.name)
        if decl.lifecycle:
            tests.append({
                "name": decl.name,
                "lifecycle": True,
                "body": _lower_lifecycle_body(decl, program, services or {}, filename,
                                              callables, types),
            })
            continue
        scope: dict[str, bool] = {}
        type_env: dict[str, str] = {}
        body: list[dict] = []
        for stmt in decl.body:
            _lower_pure_stmt(stmt, scope, callables, {}, body, filename, type_env, types)
        tests.append({"name": decl.name, "body": body})
    return tests


# ---------------------------------------------------------------- prop tests
#
# `prop test "name" (params) { assert … }` (roadmap item 37): the parameters
# are *generated inputs*, and the body is a pure property that must hold for
# every generated value.  Lowering does two things a plain `test` does not:
#
#   * it validates that every parameter type is one the generator can DERIVE a
#     value from (a primitive, an `Opt`/`List`/`Result` of one, or a declared
#     record/ADT), so an ungeneratable parameter is a compile error rather than
#     a runtime surprise; and
#   * it records the parameters (with their resolved types) alongside the
#     lowered body in a `prop_tests` IR section, from which the py runner
#     (src/revl/fault.py) derives the generators and the shrinker.
#
# The body itself lowers exactly as a `fn`/`test` body does, with the
# parameters in scope — so `assert` reads identically to everywhere else.
_GENERATABLE_PRIMITIVES = {"Int", "Int32", "Bool", "Str", "Float", "F64", "Num"}


def _check_generatable(filename: str, line: int, type_name: str, types: dict,
                       propname: str, record_stack: tuple = (),
                       variant_seen: frozenset = frozenset()) -> None:
    """Reject a prop-test parameter (or nested) type the generator cannot make.

    Generatable: a primitive; `Opt[T]`/`List[T]` of a generatable argument; a
    declared record whose fields are generatable; a declared ADT whose case
    payloads are generatable.  A record that (transitively) contains
    itself is genuinely non-constructible and is named as such; recursive ADTs
    are fine (they have a base case, and the runner bounds generation depth).
    """
    head, args = parse_type(type_name)
    if head in _GENERATABLE_PRIMITIVES and not args:
        return
    if head == "Opt" and len(args) == 1:
        _check_generatable(filename, line, args[0], types, propname,
                           record_stack, variant_seen)
        return
    if head == "List" and len(args) == 1:
        _check_generatable(filename, line, args[0], types, propname,
                           record_stack, variant_seen)
        return
    spec = types.get(head) if head and not args else None
    if spec and spec.get("kind") == "record":
        if head in record_stack:
            cycle = " -> ".join(record_stack + (head,))
            raise RevlError(
                filename, line,
                f"prop test `{propname}`: record type `{head}` contains itself "
                f"({cycle}) and cannot be generated",
                hint="a record always holds all its fields, so a self-containing record "
                     "has no finite value; break the cycle with an `Opt[...]` or `List[...]`")
        for ftype in spec.get("fields", {}).values():
            _check_generatable(filename, line, ftype, types, propname,
                               record_stack + (head,), variant_seen)
        return
    if spec and spec.get("kind") == "variant":
        if head in variant_seen:
            # already validated on this path — a recursive ADT is fine (it has a
            # base case; the depth-bounded generator handles it). Stop rather
            # than descend forever.
            return
        for case in spec.get("cases") or []:
            if case.get("payload") is not None:
                # a case payload may reference the ADT recursively — the record
                # cycle stack is not carried across the ADT boundary (that split
                # is a valid base/recursive one), but the variant set is, to
                # terminate on a self-referential ADT
                _check_generatable(filename, line, case["payload"], types, propname,
                                   (), variant_seen | {head})
        return
    raise RevlError(
        filename, line,
        f"prop test `{propname}`: parameter type `{type_name}` cannot be generated",
        hint="a prop-test parameter must be a type the checker can derive inputs from: "
             "`Int`/`Int32`/`Bool`/`Str`/`Float`, an `Opt[...]`/`List[...]` of one, or a "
             "declared record/ADT (docs/prop-test.md)")


def _lower_prop_tests(program: Program, filename: str, types: dict,
                      services: dict | None = None) -> list:
    """Lower `prop test` blocks to IR prop units (docs/prop-test.md)."""
    if not program.prop_tests:
        return []
    callables = (_HOST_CALLABLES | _BUILTIN_CONSTRUCTORS | _DECLASSIFY_BUILTINS
                 | {fn.name for fn in program.fn_decls}
                 | {ext.name for ext in program.externs})
    units: list[dict] = []
    seen: set[str] = set()
    for decl in program.prop_tests:
        if decl.name in seen:
            raise RevlError(filename, decl.line, f"duplicate prop test `{decl.name}`")
        seen.add(decl.name)
        scope: dict[str, bool] = {}
        type_env: dict[str, str] = {}
        for param in decl.params:
            check_type_wellformed(filename, param.line, param.type)
            _check_generatable(filename, param.line, param.type, types, decl.name)
            scope[param.name] = False
            type_env[param.name] = param.type
        body: list[dict] = []
        for stmt in decl.body:
            _lower_pure_stmt(stmt, scope, callables, {}, body, filename, type_env, types)
        units.append({
            "name": decl.name,
            "params": [{"name": p.name, "type": p.type} for p in decl.params],
            "body": body,
        })
    return units


# `fail at effect X` is sugar: it resolves to the index of the body step that
# binds X, so every backend and the runner see exactly one addressing scheme
# (a step index).  Index is the primitive because it is total — *every* body
# step has one, including `emit`, `provide`, `await` and unbound `effect`
# blocks — while a name only exists for `let effect NAME = …`.  The name form
# is the refactor-stable one to write, so both spellings are kept and the
# diagnostics always report the pair.
_FAULT_ASSERTS = (
    "failed", "no-residue", "inverses-lifo", "no-emissions", "siblings-unaffected",
)


def _fault_effect_index(body: list, name: str) -> int | None:
    """1-based index of the top-level `let-effect` step binding *name*."""
    for index, step in enumerate(body, 1):
        if step.get("step") == "let-effect" and step.get("bind") == name:
            return index
    return None


def _lower_fault_tests(program: Program, components: list, filename: str) -> list:
    """Lower `fault test` blocks to IR fault units (docs/fault-tests.md).

    Runs after component lowering: the injection point is validated against
    the *lowered* body, so `fail at step 4` is a compile error when the
    component only has three steps rather than a confusing runtime miss.
    """
    if not program.fault_tests:
        return []
    by_name = {component["name"]: component for component in components}
    units: list[dict] = []
    seen: set[str] = set()
    for decl in program.fault_tests:
        if decl.name in seen:
            raise RevlError(filename, decl.line, f"duplicate fault test `{decl.name}`")
        seen.add(decl.name)
        component = by_name.get(decl.component)
        if component is None:
            known = ", ".join(sorted(by_name)) or "(none in this composition)"
            raise RevlError(filename, decl.line,
                            f"fault test `{decl.name}` names unknown component `{decl.component}`",
                            hint=f"components in this composition: {known}")
        body = component.get("body") or []
        if not body:
            raise RevlError(filename, decl.line,
                            f"component `{decl.component}` has an empty activation body — "
                            f"there is no point at which it can fail")
        if decl.at_effect is not None:
            step = _fault_effect_index(body, decl.at_effect)
            if step is None:
                bindings = [s.get("bind") for s in body if s.get("step") == "let-effect"]
                known = ", ".join(f"`{b}`" for b in bindings) or "(none)"
                raise RevlError(
                    filename, decl.line,
                    f"fault test `{decl.name}`: component `{decl.component}` has no "
                    f"`let … effect` step bound to `{decl.at_effect}`",
                    hint=f"effect bindings in `{decl.component}`: {known}")
        else:
            step = decl.at_step
            if step > len(body):
                raise RevlError(
                    filename, decl.line,
                    f"fault test `{decl.name}`: `fail at step {step}` is past the end of "
                    f"`{decl.component}` (its activation body has {len(body)} step(s))")
        known_config = {field.get("name") for field in component.get("config") or []}
        for key in decl.config:
            if key not in known_config:
                fields = ", ".join(sorted(known_config)) or "(none)"
                raise RevlError(
                    filename, decl.line,
                    f"fault test `{decl.name}`: `{decl.component}` has no config field `{key}`",
                    hint=f"config fields of `{decl.component}`: {fields}")
        asserts: list[str] = []
        for kind, line in decl.asserts:
            if kind not in _FAULT_ASSERTS:  # pragma: no cover — parser gates the spelling
                raise RevlError(filename, line, f"unknown fault-test assertion `{kind}`")
            if kind not in asserts:
                asserts.append(kind)
        unit = {
            "name": decl.name,
            "component": decl.component,
            "at": {"step": step},
            "assert": asserts,
        }
        if decl.at_effect is not None:
            unit["at"]["effect"] = decl.at_effect
        if decl.config:
            unit["config"] = dict(decl.config)
        units.append(unit)
    return units

def _lower_lifecycle_body(decl: TestDecl, program: Program, services: dict, filename: str,
                          callables: set, types: dict) -> list:
    """Lower a `lifecycle test` body (syntax-2.0 §7.1).

    The body is a *linear script* over a live composition, so the checker can
    track exactly what is loaded and what keys are provided at every point —
    every diagnostic below is a compile error, not a runtime surprise.

    G2 (provision disjointness) is what makes this tractable and is also the
    reason there is no `swap C -> C2` statement: two components may not
    provide the same key in one document, so a replacement *provider* is not
    expressible; a replacement *instance* is `unload C` then `load C with
    { ... }`, which this statement set already spells.
    """
    components = {comp.name: comp for comp in program.components}
    loaded: dict[str, ComponentDecl] = {}    # component name -> decl
    provided: dict[str, str] = {}            # provision key -> component name
    scope: dict[str, bool] = {}
    type_env: dict[str, str] = {}
    body: list[dict] = []

    def _known() -> str:
        return ", ".join(f"`{name}`" for name in sorted(components)) or "<none>"

    for stmt in decl.body:
        if isinstance(stmt, LoadStmt):
            comp = components.get(stmt.component)
            if comp is None:
                raise RevlError(filename, stmt.line,
                                f"unknown component `{stmt.component}`",
                                hint=f"a lifecycle test loads components declared in this "
                                     f"document: {_known()}")
            if stmt.component in loaded:
                raise RevlError(
                    filename, stmt.line,
                    f"`{stmt.component}` is already loaded",
                    hint="one instance per component at a time — `unload` it first; two live "
                         "providers of one key is exactly what G2 forbids (§7.1)",
                )
            config = _lower_lifecycle_config(stmt, comp, filename, scope, callables,
                                             type_env, types)
            for key, _svc, _line in comp.provides:
                provided[key] = comp.name
            loaded[comp.name] = comp
            body.append({"step": "load", "component": comp.name, "config": config})
        elif isinstance(stmt, UnloadStmt):
            if stmt.component not in loaded:
                if stmt.component not in components:
                    raise RevlError(filename, stmt.line,
                                    f"unknown component `{stmt.component}`",
                                    hint=f"declared components: {_known()}")
                raise RevlError(filename, stmt.line,
                                f"`{stmt.component}` is not loaded at this point")
            comp = loaded.pop(stmt.component)
            for key, _svc, _line in comp.provides:
                provided.pop(key, None)
            body.append({"step": "unload", "component": comp.name})
        elif isinstance(stmt, CallStmt):
            body.append(_lower_lifecycle_call(stmt, provided, components, services, filename,
                                              scope, callables, type_env, types))
        elif isinstance(stmt, AdvanceStmt):
            # item 102: drive the clock coeffect (item 57) forward. Nothing else
            # in a lifecycle test moves the clock, so this is what makes a
            # timer's firing an assertable timeline step.
            body.append({"step": "advance", "ms": stmt.ms})
        elif isinstance(stmt, AbortStmt):
            # item 377 (F-H1.7): drive the enclosing session frame's 245 abort —
            # mark every live frame aborting, replay the witnessed inverses LIFO,
            # drop the deferral queue. Like `Session.abort`, it tears the live
            # composition down, so nothing is loaded afterwards. Refuse an abort
            # with nothing loaded: there is no session frame to abort, exactly as
            # `Session.abort` needs an owner (it would be a vacuous no-op).
            if not loaded:
                raise RevlError(
                    filename, stmt.line,
                    "`abort` has nothing to abort — no component is loaded at "
                    "this point",
                    hint="`abort` reverts the witnessed effects of the live "
                         "composition; `load` a component and drive it first "
                         "(item 377, docs/design/245-session-commit.md)")
            body.append({"step": "abort"})
            # the abort tears the composition down (session-abort semantics), so
            # the checker's model of what is live returns to empty — a later
            # `unload X` correctly reads as "not loaded", and a later `call`
            # against a since-torn-down key is refused at compile time.
            loaded.clear()
            provided.clear()
        elif isinstance(stmt, ResidueStmt):
            body.append({"step": "assert_no_residue"})
        elif isinstance(stmt, AssertStmt):
            expr = stmt.expr
            if isinstance(expr, ExprVar) and expr.name not in scope:
                raise RevlError(
                    filename, stmt.line,
                    f"unknown lifecycle assertion `{expr.name}`",
                    hint="the lifecycle assertion is `assert no_residue` (§7.1); anything else "
                         "after `assert` is a Bool expression over this test's `let` bindings",
                )
            _bool_cond(expr, type_env, types, filename, "assert")
            body.append({"step": "assert",
                         "expr": _lower_pure_expr(expr, scope, callables, {}, filename,
                                                  type_env, types)})
        else:  # pragma: no cover — the lifecycle grammar produces nothing else
            raise RevlError(filename, getattr(stmt, "line", decl.line),
                            "unexpected statement in a lifecycle test body")

    return body


def _lower_lifecycle_config(stmt: LoadStmt, comp: ComponentDecl, filename: str, scope: dict,
                            callables: set, type_env: dict, types: dict) -> dict:
    """Check and lower `load C with { field: expr, ... }` against C's `config`."""
    fields = {cfg.name: cfg for cfg in comp.config}
    given: dict[str, dict] = {}
    for name, value, line in stmt.config:
        cfg = fields.get(name)
        if cfg is None:
            known = ", ".join(f"`{f}`" for f in fields) or "<none>"
            raise RevlError(filename, line,
                            f"`{name}` is not a config field of {comp.name}",
                            hint=f"config fields of {comp.name}: {known}")
        if name in given:
            raise RevlError(filename, line, f"duplicate config field `{name}`")
        check_ast(value, cfg.type, type_env, types, filename,
                  f"config field `{name}` of {comp.name}")
        given[name] = _lower_pure_expr(value, scope, callables, {}, filename, type_env, types)
    missing = [cfg.name for cfg in comp.config if cfg.default is None and cfg.name not in given]
    if missing:
        listed = ", ".join(f"`{name}`" for name in missing)
        raise RevlError(filename, stmt.line,
                        f"`load {comp.name}` is missing required config {listed}",
                        hint=f"write `load {comp.name} with {{ {missing[0]}: ... }}`")
    return given


def _lower_lifecycle_call(stmt: CallStmt, provided: dict, components: dict, services: dict,
                          filename: str, scope: dict, callables: set,
                          type_env: dict, types: dict) -> dict:
    """Check and lower `call key.op(args)` / `let x = call key.op(args)`.

    A lifecycle test drives the composition from *outside* it — it is not a
    provider — so G4's `emit` marker does not apply here: the bound G4
    enforces is a service declaration bounding *its providers*, and a test has
    no declaration to exceed (§7.1).
    """
    owner = provided.get(stmt.key)
    if owner is None:
        keys = sorted({key for comp in components.values() for key, _s, _l in comp.provides})
        if stmt.key in keys:
            raise RevlError(
                filename, stmt.line,
                f"no provider for key `{stmt.key}` at this point",
                hint="load the component that provides it before calling through the key",
            )
        listed = ", ".join(f"`{key}`" for key in keys) or "<none>"
        raise RevlError(filename, stmt.line,
                        f"unknown provision key `{stmt.key}`",
                        hint=f"keys provided in this document: {listed}")
    comp = components[owner]
    service_name = next(svc for key, svc, _line in comp.provides if key == stmt.key)
    svc = services.get(service_name)
    if svc is None:  # pragma: no cover — checked when the component was lowered
        raise RevlError(filename, stmt.line, f"unknown service `{service_name}`")
    method = svc.methods.get(stmt.method)
    if method is None:
        listed = ", ".join(f"`{name}`" for name in svc.methods) or "<none>"
        raise RevlError(filename, stmt.line,
                        f"`{stmt.key}.{stmt.method}` is not an operation of service "
                        f"{service_name}",
                        hint=f"operations of {service_name}: {listed}")
    if len(stmt.args) != len(method.params):
        raise RevlError(filename, stmt.line,
                        f"`{stmt.key}.{stmt.method}` takes {len(method.params)} "
                        f"argument(s), {len(stmt.args)} given")
    args = []
    for arg, (pname, ptype) in zip(stmt.args, method.params):
        check_ast(arg, ptype, type_env, types, filename,
                  f"`{stmt.key}.{stmt.method}` argument `{pname}`")
        args.append(_lower_pure_expr(arg, scope, callables, {}, filename, type_env, types))
    node = {"step": "call", "key": stmt.key, "method": stmt.method, "args": args}
    if stmt.bind is not None:
        if stmt.bind in scope:
            raise RevlError(filename, stmt.line,
                            f"`{stmt.bind}` is already declared in this lifecycle test",
                            hint="bindings are test-scoped, not block-scoped — "
                                 "sibling branches share one namespace; rename "
                                 "or reuse the existing binding")
        scope[stmt.bind] = False
        if method.returns:
            type_env[stmt.bind] = method.returns
        node["bind"] = stmt.bind
    return node


def _bool_cond(expr, type_env: dict, types: dict, filename: str, where: str) -> None:
    # a condition is a check position like any other: `if (hole "…")` is a
    # `Bool` obligation, not an untyped hole (docs/holes.md)
    if pin_hole(expr, "Bool", filename, f"`{where}` condition"):
        return
    t = infer_ast(expr, type_env, types, filename)
    if t is not None and t != "Bool":
        raise mismatch(filename, getattr(expr, "line", 0), f"`{where}` condition", "Bool", t)


def _lower_pure_stmt(stmt, scope: dict, callables: set, alias_fns: dict, body: list, filename: str,
                     type_env: dict | None = None, types: dict | None = None,
                     expected_return: str | None = None) -> None:
    type_env = type_env if type_env is not None else {}
    types = types if types is not None else {}
    if isinstance(stmt, LetStmt):
        if stmt.name in scope:
            raise RevlError(filename, stmt.line,
                            f"`{stmt.name}` is already declared in this function",
                            hint="a `let` binds a name once per scope — rename this "
                                 "binding, or use `=` to reassign an existing `var`. "
                                 "(Disjoint sibling blocks — the two arms of an "
                                 "if/else — may reuse a name, since only one is live.)")
        # host provenance: a let bound to a host constructor call carries
        # host-object methods, exempt from the stdlib method table
        if not stmt.mutable and _is_host_valued(stmt.value, scope):
            scope[stmt.name] = "host"
        else:
            scope[stmt.name] = stmt.mutable
        declared = getattr(stmt, "type", None)
        if declared is not None:
            # `let g: (Int) -> Int = v => v + 1` — the annotation is the
            # checking position for the right-hand side, which is what gives an
            # un-annotated arrow its parameter and return types.
            check_type_wellformed(filename, stmt.line, declared)
            check_ast(stmt.value, declared, type_env, types, filename,
                      f"`let {stmt.name}: {render_type(declared)}`")
            type_env[stmt.name] = declared
            actual_declared = infer_ast(stmt.value, type_env, types, None)
        else:
            inferred = infer_ast(stmt.value, type_env, types, filename)
            if inferred is not None:
                type_env[stmt.name] = inferred
            actual_declared = None
        lowered_value = _lower_pure_expr(stmt.value, scope, callables, alias_fns, filename, type_env, types)
        # an annotated `let x: Float = 3` is a coercion site too (docs/arithmetic.md)
        _mark_widen(declared, actual_declared, lowered_value)
        _pin_empty_literal(declared, lowered_value)
        body.append({"step": "let", "name": stmt.name,
                     "value": lowered_value,
                     "mutable": stmt.mutable})
    elif isinstance(stmt, LetPatternStmt):
        _lower_let_pattern_stmt(stmt, scope, callables, alias_fns, body, filename, type_env, types)
    elif isinstance(stmt, AssignStmt):
        if stmt.name not in scope:
            _reject_foreign_name(stmt.name, filename, stmt.line)  # item 384
            raise RevlError(filename, stmt.line, f"`{stmt.name}` is not declared in this function",
                            hint="declare it with `let` (single-assignment) or `var` (mutable)")
        if not scope[stmt.name]:
            raise RevlError(filename, stmt.line,
                            f"cannot reassign `{stmt.name}` — it is `let` (single-assignment)",
                            hint="declare it with `var` to make it mutable (syntax-2.0 §3.5)")
        if stmt.op == "=":
            value = stmt.value
        else:
            # `x += e` desugars to `x = x + e`; the parser only composes the
            # operator, so lowering never has to remember it beyond this point.
            value = ExprBin(stmt.op[:-1], ExprVar(stmt.name, stmt.line), stmt.value, stmt.line)
        declared = type_env.get(stmt.name)
        if isinstance(value, ExprArrow) and declared:
            # reassigning a `var` of function type: the arrow needs the
            # declaration as its *checking* position, exactly as at the `let`.
            # Inference alone returns nothing for an un-annotated arrow, so the
            # comparison below would pass anything (docs/function-types.md).
            check_ast(value, declared, type_env, types, filename,
                      f"assignment to `{stmt.name}` (a `{render_type(declared)}` variable)")
        inferred = infer_ast(value, type_env, types, filename)
        if declared and inferred and not compatible(declared, inferred):
            raise mismatch(filename, stmt.line,
                           f"assignment to `{stmt.name}` (a `{render_type(declared)}` variable)",
                           declared, inferred)
        if inferred is not None and declared is None:
            type_env[stmt.name] = inferred
        lowered_value = _lower_pure_expr(value, scope, callables, alias_fns, filename, type_env, types)
        # assigning into a `Float`-declared variable is a coercion site too
        _mark_widen(declared, inferred, lowered_value)
        body.append({"step": "assign", "name": stmt.name,
                     "value": lowered_value})
    elif isinstance(stmt, ReturnStmt):
        if stmt.expr is not None:
            check_ast(stmt.expr, expected_return, type_env, types, filename, "this function's return")
        elif expected_return:
            raise RevlError(filename, stmt.line,
                            f"bare `return` in a function declared to return `{expected_return}`")
        lowered = (None if stmt.expr is None else
                   _lower_pure_expr(stmt.expr, scope, callables, alias_fns,
                                    filename, type_env, types))
        if lowered is not None and expected_return:
            actual_ret = infer_ast(stmt.expr, type_env, types, filename)
            # `return 3` in a `-> Float` fn is a coercion site (docs/arithmetic.md)
            _mark_widen(expected_return, actual_ret, lowered)
            lowered = _inject_opt(expected_return,
                                  actual_ret,
                                  lowered)
        body.append({"step": "return", "expr": lowered})
    elif isinstance(stmt, IfStmt):
        then: list[dict] = []
        _bool_cond(stmt.cond, type_env, types, filename, "if")
        # Each arm is its own block: it snapshots the enclosing scope/type_env
        # (as `while`/`for`/`match` arms already do) rather than sharing one flat
        # dict. This block-scopes the arm's `let`s — they do not leak past the
        # `if`, and the two arms cannot collide with each other, so disjoint
        # sibling branches may reuse a name. A redeclaration *within* one arm, or
        # in one straight-line scope, still hits the guard: the snapshot is
        # mutated in sequence, so the second `let` sees the first.
        then_scope = dict(scope)
        then_type_env = dict(type_env)
        for s in stmt.then:
            _lower_pure_stmt(s, then_scope, callables, alias_fns, then, filename, then_type_env, types, expected_return)
        otherwise = None
        if stmt.otherwise is not None:
            otherwise = []
            else_scope = dict(scope)
            else_type_env = dict(type_env)
            for s in stmt.otherwise:
                _lower_pure_stmt(s, else_scope, callables, alias_fns, otherwise, filename, else_type_env, types, expected_return)
        body.append({"step": "if", "cond": _lower_pure_expr(stmt.cond, scope, callables, alias_fns, filename, type_env, types),
                     "then": then, "else": otherwise})
    elif isinstance(stmt, WhileStmt):
        _bool_cond(stmt.cond, type_env, types, filename, "while")
        cond = _lower_pure_expr(stmt.cond, scope, callables, alias_fns, filename, type_env, types)
        inner_scope = dict(scope)
        inner_type_env = dict(type_env)
        inner_body: list[dict] = []
        for s in stmt.body:
            _lower_pure_stmt(s, inner_scope, callables, alias_fns, inner_body, filename, inner_type_env, types, expected_return)
        body.append({"step": "while", "cond": cond, "body": inner_body})
    elif isinstance(stmt, ForStmt):
        if stmt.bind in scope:
            raise RevlError(filename, stmt.line,
                            f"`{stmt.bind}` is already declared in this function",
                            hint="a loop binding shadows nothing already in scope — "
                                 "rename it. (Disjoint sibling blocks may reuse a "
                                 "name, since only one is live.)")
        iter_diag = infer_ast(stmt.iterable, type_env, types, filename)
        if iter_diag is not None and parse_type(iter_diag)[0] != "List":
            raise RevlError(filename, stmt.line,
                            f"`for ... of` iterates a `List[...]`, got `{render_type(iter_diag)}`")
        iterable = _lower_pure_expr(stmt.iterable, scope, callables, alias_fns, filename, type_env, types)
        inner_scope = dict(scope)
        inner_type_env = dict(type_env)
        inner_scope[stmt.bind] = False
        iter_type = _expr_static_type(stmt.iterable, type_env, types)
        element_type = _type_arg(iter_type, "List")
        if element_type is not None:
            inner_type_env[stmt.bind] = element_type
        inner_body = []
        for s in stmt.body:
            _lower_pure_stmt(s, inner_scope, callables, alias_fns, inner_body, filename, inner_type_env, types, expected_return)
        body.append({"step": "for", "bind": stmt.bind, "iterable": iterable, "body": inner_body})
    elif isinstance(stmt, ExprStmt):
        infer_ast(stmt.expr, type_env, types, filename)
        body.append({"step": "expr", "expr": _lower_pure_expr(stmt.expr, scope, callables, alias_fns, filename, type_env, types)})
    elif isinstance(stmt, AssertStmt):
        _bool_cond(stmt.expr, type_env, types, filename, "assert")
        body.append({"step": "assert", "expr": _lower_pure_expr(stmt.expr, scope, callables, alias_fns, filename, type_env, types)})
    elif isinstance(stmt, BreakStmt):
        # item 379: additive step kind, no payload. The parser already refused a
        # `break` outside a loop, so lowering only records it; teardown is
        # untouched (break/continue are frame-neutral — no revl teardown
        # boundary coincides with a loop boundary, docs/design/379-break-
        # continue.md Decision 1).
        body.append({"step": "break", "line": stmt.line})
    elif isinstance(stmt, ContinueStmt):
        body.append({"step": "continue", "line": stmt.line})
    else:  # pragma: no cover — grammar prevents it
        raise RevlError(filename, getattr(stmt, "line", 1), "unexpected statement in fn body")


def _lower_let_pattern_stmt(stmt: LetPatternStmt, scope: dict, callables: set, alias_fns: dict,
                            body: list, filename: str, type_env: dict, types: dict) -> None:
    """Lower a `let`/`var` destructuring to one ``let_pattern`` step."""
    pattern = stmt.pattern
    if isinstance(pattern, RecordPattern):
        names = list(pattern.fields)
        if not names:
            raise RevlError(filename, pattern.line, "record destructuring needs at least one field")
        if len(set(names)) != len(names):
            raise RevlError(filename, pattern.line, "duplicate name in record destructuring")
        for name in names:
            if name in scope:
                raise RevlError(filename, stmt.line,
                                f"`{name}` is already declared in this function",
                                hint="a `let` binds a name once per scope — rename this "
                                     "binding, or use `=` to reassign an existing `var`. "
                                     "(Disjoint sibling blocks may reuse a name, since "
                                     "only one is live.)")
        value_type = _expr_static_type(stmt.value, type_env, types)
        spec = types.get(value_type or "")
        if spec is not None:
            if spec.get("kind") != "record":
                raise RevlError(
                    filename, stmt.line,
                    f"record destructuring requires a record, but `{render_type(value_type)}` is not a record",
                )
            fields = spec.get("fields", {})
            for name in names:
                if name not in fields:
                    raise RevlError(filename, pattern.line,
                                    f"`{name}` is not a field of record `{render_type(value_type)}`")
        elif value_type is not None and parse_type(value_type)[0] in _BUILTIN_NONRECORD:
            raise RevlError(
                filename, stmt.line,
                f"record destructuring requires a record, but `{render_type(value_type)}` is not a record",
            )
        value_ir = _lower_pure_expr(stmt.value, scope, callables, alias_fns, filename, type_env, types)
        for name in names:
            scope[name] = stmt.mutable
            if spec is not None:
                type_env[name] = spec.get("fields", {})[name]
        body.append({
            "step": "let_pattern",
            "pattern": "record",
            "names": names,
            "value": value_ir,
            "mutable": stmt.mutable,
        })
        return

    if isinstance(pattern, ListPattern):
        if not pattern.binds:
            raise RevlError(filename, pattern.line,
                            "list destructuring needs at least one binding before `...rest`")
        names = list(pattern.binds)
        if pattern.rest is not None:
            names.append(pattern.rest)
        if len(set(names)) != len(names):
            raise RevlError(filename, pattern.line, "duplicate name in list destructuring")
        for name in names:
            if name in scope:
                raise RevlError(filename, stmt.line,
                                f"`{name}` is already declared in this function",
                                hint="a `let` binds a name once per scope — rename this "
                                     "binding, or use `=` to reassign an existing `var`. "
                                     "(Disjoint sibling blocks may reuse a name, since "
                                     "only one is live.)")
        value_type = _expr_static_type(stmt.value, type_env, types)
        if value_type is not None and parse_type(value_type)[0] != "List" and (
            types.get(value_type) is not None
            or parse_type(value_type)[0] in _BUILTIN_NONRECORD
        ):
            raise RevlError(
                filename, stmt.line,
                f"list destructuring requires a `List[...]`, but `{render_type(value_type)}` is not a list",
            )
        value_ir = _lower_pure_expr(stmt.value, scope, callables, alias_fns, filename, type_env, types)
        element_type = _type_arg(value_type, "List")
        for name in pattern.binds:
            scope[name] = stmt.mutable
            if element_type is not None:
                type_env[name] = element_type
        if pattern.rest is not None:
            scope[pattern.rest] = stmt.mutable
            if value_type is not None:
                type_env[pattern.rest] = value_type
        body.append({
            "step": "let_pattern",
            "pattern": "list",
            "names": pattern.binds,
            "rest": pattern.rest,
            "value": value_ir,
            "mutable": stmt.mutable,
        })
        return

    raise RevlError(filename, stmt.line, "unexpected destructuring pattern")


def _tagged_case(name: str, types: dict) -> dict | None:
    """If `name` is a *tagged* ADT constructor — the built-in `Result`
    (`Ok`/`Err`) or any user variant case — return its case-table entry.
    `Opt` (`Some`/`None`) is not tagged: it stays host-null/value, so it is
    excluded here and handled as identity/null."""
    if name in ("Some", "None"):
        return None
    return (types.get(CASES_KEY) or {}).get(name)


def _lower_hole(expr: ExprHole, filename: str) -> dict:
    """`hole` -> the IR's one non-executable node (docs/holes.md).

    A hole must know its type by now: the checker pins it from context in
    check position, and `hole[T]` states it outright. Anywhere else the
    honest answer is a rejection — guessing would hand the author a type the
    compiler invented, which is exactly the drowning-in-noise failure holes
    exist to fix.
    """
    type_name = expr.known_type
    if type_name is None:
        raise RevlError(
            filename, expr.line,
            "this `hole` has no expected type — nothing in its context says "
            "what it must eventually be",
            hint="annotate it (`hole[Str] \"why\"`), or put it somewhere with a "
                 "declared type: a `fn`'s `-> T`, a service method's return, or "
                 "an argument of a declared function (docs/holes.md)",
            code="T3", category="hole",
        )
    check_type_wellformed(filename, expr.line, type_name)
    node = {"kind": "hole", "type": type_name, "file": filename, "line": expr.line}
    if expr.message is not None:
        node["message"] = expr.message
    return node


def _retarget_holes(node, source: str) -> None:
    """Point holes at the file they were written in.

    Lowering runs over one merged program, so its `filename` is the first
    root path; a declaration carries its own provenance and an obligation an
    agent must go and fill is worthless with the wrong path on it.
    """
    if isinstance(node, dict):
        if node.get("kind") == "hole":
            node["file"] = source
        for value in node.values():
            _retarget_holes(value, source)
    elif isinstance(node, list):
        for value in node:
            _retarget_holes(value, source)


# Operators whose meaning depends on whether the operands are Int or Float.
# `/` and `%` because integer and float division differ; `+`, `-` and `*`
# because Int is a *bounded* 64-bit type whose overflow traps, and a tier
# cannot check a bound it does not know applies. Comparisons and booleans
# gain nothing from the annotation, so they do not carry it.
_TYPED_ARITH_OPS = ("/", "%", "+", "-", "*")


def _str_literal_value(value):
    """Canonical IR form for a `Str` literal: a sequence of Unicode scalar
    values (code points), never UTF-16 surrogate pairs (docs/strings.md,
    roadmap item 51 — the string wave).

    The lexer stores raw source characters and a Python `str` is already a
    code-point sequence, so for a well-formed program this is the identity.
    It is stated explicitly here because it is the invariant every backend's
    literal escaper depends on: each emitter reads this value and must escape
    *from code points* (`\\u{1F600}` on rust, `\\U0001F600` on go, the raw
    scalar in wasm's UTF-8 pool), never re-encode it as UTF-16. A lone
    surrogate reaching this point would mean an upstream path re-introduced
    UTF-16 — the exact defect that made astral literals uncompilable on go and
    rust — so it is rejected here rather than emitted as invalid source.
    """
    if isinstance(value, str):
        for ch in value:
            if 0xD800 <= ord(ch) <= 0xDFFF:
                raise ValueError(
                    "string literal carries a lone surrogate; a Str literal is "
                    "a sequence of Unicode code points (docs/strings.md)")
    return value


def _mark_widen(expected: str | None, actual: str | None, node: dict | None) -> dict | None:
    """Mark an implicit `Int` -> `Float` coercion site in the IR.

    `compatible` lets an `Int` stand where a `Float` is declared, but until
    now the widening was invisible: a `call` argument, a `let` value or a
    `return` expression reached every backend as a bare `lit`/`var`, and the
    tiers that keep `Int` and `Float` apart split three ways on `ident(3)`
    (docs/arithmetic.md) — rust refused with E0308, TypeScript computed the
    wrong answer (`3n === 3` is false), and python/go/java absorbed it behind
    a host rule. This is the same shape of gap `operands` closed for `/`, and
    it closes the same way: the frontend — the single IR producer, and the
    only stage that knows both types — annotates the coercion site, and every
    backend can emit the conversion.

    The marker is additive (`"widen": "Float"` on the coerced node), so no
    `ir_version` changes and v1/v2/v3 reference documents stay
    byte-identical: a coercion site requires a declared `Float` position,
    which only full-language (v3) sources can express.
    """
    if node is None:
        return node
    ehead = parse_type(expected)[0]
    ahead = parse_type(actual)[0]
    if ehead == "Float" and ahead in ("Int", "Int32"):
        node["widen"] = "Float"
    elif ehead == "Int" and ahead == "Int32":
        # Int32 -> Int is a lossless *widening* the tiers that keep the two
        # widths apart (rust i32/i64, wasm, go, java, ts number/bigint) must
        # emit explicitly, exactly as Int -> Float is marked. `"widen": "Int"`
        # names the target width; python absorbs it (one int type).
        node["widen"] = "Int"
    return node


def _pin_empty_literal(declared: str | None, node: dict | None) -> None:
    """An annotated `let`/`var` pins an empty-collection literal (roadmap 76b, 107).

    `var m: Map[Str, Int] = Map.empty()` lowers to a `maplit` node that knows
    nothing about the author's annotation — the checker accepts the empty map
    (it types `Map[Str, Never]`, bottom, and flows into any `Map[K, V]`) but
    the go tier refuses an unpinned empty Map because Go infers composite
    literals positionally, not from later use. The annotation the author
    already wrote is the pin, so the frontend — the single IR producer, and
    the only stage that knows the declared type — attaches it to the literal:
    ``"expected": "Map[Str, Int]"`` on the `maplit` node. The marker is
    additive and appears only where an annotation exists, so v1/v2/v3
    reference documents without one stay byte-identical.
    """
    if declared is None or not isinstance(node, dict):
        return
    kind = node.get("kind")
    if kind == "maplit":
        node["expected"] = declared
    elif (
        kind == "list"
        and not (node.get("items") or [])
        and declared.startswith("List[")
    ):
        # `var out: List[Int] = []` (roadmap 107): the checker accepts the empty
        # list (it types `List[Never]`, bottom, and flows into any `List[T]`) but
        # a positional emitter — the wasm tier — has no element to infer from and
        # refuses. The author's annotation is the pin, threaded onto the literal
        # exactly as the empty-Map case above, so the emitter can type it.
        node["expected"] = declared


def _lower_pure_expr(expr, scope: dict, callables: set, alias_fns: dict, filename: str,
                     type_env: dict | None = None, types: dict | None = None) -> dict:
    type_env = type_env if type_env is not None else {}
    types = types if types is not None else {}
    if isinstance(expr, ExprHole):
        return _lower_hole(expr, filename)
    if isinstance(expr, ExprEndorse):
        inner = _lower_pure_expr(expr.expr, scope, callables, alias_fns, filename,
                                 type_env, types)
        if expr.approval is not None:
            # approvals are acquired in a component activation body (item 246),
            # not in a top-level `fn`; a `with` here has nothing to bind.
            raise RevlError(
                filename, expr.line,
                "`endorse ... with <approval>` is only available in a component "
                "or provide-method body — a top-level `fn` cannot acquire an "
                "`Approval[C]`",
                hint="move the approval-gated endorse into the provide method, or "
                     "drop the `with` clause (item 246/249)")
        return _endorse_node(inner, expr, None)
    # ADT construction (Result / user variants) lowers to a tagged `adt` node
    # (Opt's Some/None are not tagged — handled as identity/null downstream).
    _cases = types.get(CASES_KEY) or {}
    if isinstance(expr, ExprCall) and isinstance(expr.callee, ExprVar) \
            and _tagged_case(expr.callee.name, types) is not None:
        info = _cases[expr.callee.name]
        return {"kind": "adt", "type": info["adt"], "case": expr.callee.name,
                "args": [_lower_pure_expr(a, scope, callables, alias_fns, filename, type_env, types)
                         for a in expr.args]}
    if isinstance(expr, ExprVar):
        info = _tagged_case(expr.name, types)
        if info is not None and info.get("payload") is None \
                and not str(info.get("adt", "")).startswith(("Result", "Opt")):
            return {"kind": "adt", "type": info["adt"], "case": expr.name, "args": []}
    # Module-namespace call: `alias.fn(args)` desugars to the imported public
    # function by its original name (IR functions are top-level, not nested
    # in namespace objects).
    if (
        isinstance(expr, ExprCall)
        and isinstance(expr.callee, ExprField)
        and isinstance(expr.callee.target, ExprVar)
    ):
        alias = expr.callee.target.name
        if alias in alias_fns:
            name = expr.callee.name
            if name not in alias_fns[alias]:
                raise RevlError(
                    filename,
                    expr.callee.line,
                    f"`{alias}.{name}` is not a public function in module `{alias}`",
                    hint=f"`use ... as {alias}` imports only `pub` declarations (G1)",
                )
            return {
                "kind": "call",
                "callee": {"kind": "var", "name": name},
                "args": [_lower_pure_expr(a, scope, callables, alias_fns, filename, type_env, types) for a in expr.args],
            }
    if isinstance(expr, ExprLit):
        if expr.value is None:
            raise null_error(filename, expr.line)
        return {"kind": "lit", "value": _str_literal_value(expr.value)}
    if isinstance(expr, ExprVar):
        if expr.name not in scope and expr.name not in callables:
            _reject_foreign_name(expr.name, filename, expr.line)  # item 384
            raise RevlError(filename, expr.line, f"`{expr.name}` is not declared in this function",
                            hint="declare it with `let`/`var` or add it as a parameter (G1)")
        return {"kind": "var", "name": expr.name}
    if isinstance(expr, ExprBin):
        node = {"kind": "bin", "op": expr.op,
                "left": _lower_pure_expr(expr.left, scope, callables, alias_fns, filename, type_env, types),
                "right": _lower_pure_expr(expr.right, scope, callables, alias_fns, filename, type_env, types)}
        # `/` and `%` mean different things on Int and Float, and a backend
        # cannot tell them apart from the node alone: python and TypeScript
        # both render `7 / 2` as 3.5 while rust renders 3, and neither is
        # wrong given what it was told. Carrying the *operand* type (not the
        # result) is what makes the arithmetic specifiable at all — see
        # docs/arithmetic.md. Inference runs without a filename so an
        # undetermined operand is an absent annotation, never an error.
        if expr.op in _TYPED_ARITH_OPS:
            left_type = infer_ast(expr.left, type_env, types, None)
            right_type = infer_ast(expr.right, type_env, types, None)
            if left_type == "Int32" and right_type == "Int32":
                # Int32 arithmetic traps at the i32 edge, the same discipline
                # `Int` has at i64 (docs/arithmetic.md). `/` still yields Float
                # and `%` is width-agnostic; only `+ - *` need the i32 helper.
                node["operands"] = "Int32"
            elif left_type == "Int" and right_type == "Int":
                node["operands"] = "Int"
            elif "Float" in (left_type, right_type):
                node["operands"] = "Float"
        return node
    if isinstance(expr, ExprUn):
        node = {"kind": "un", "op": expr.op,
                "operand": _lower_pure_expr(expr.operand, scope, callables, alias_fns, filename, type_env, types)}
        # Unary minus is arithmetic too: negating Int.MIN overflows, and a
        # backend cannot tell an Int negation from a Float one without the
        # operand type — the same information `bin` carries for the same
        # reason. Only `Int` is annotated: it is the type whose bound a
        # backend must re-impose (docs/arithmetic.md), and no tier needs to
        # treat Float negation specially.
        if expr.op == "-":
            operand_type = infer_ast(expr.operand, type_env, types, None)
            if operand_type in ("Int", "Int32"):
                # `-x` is `0 - x`; on Int32, `0 - Int32.MIN` overflows the i32
                # range just as `0 - Int.MIN` overflows i64, so the bound is
                # re-imposed at the tier (docs/arithmetic.md).
                node["operands"] = operand_type
        return node
    if isinstance(expr, ExprCall):
        _callee = expr.callee
        # the Map VALUE constructor (docs/stdlib-2.0.md §Map): intercepted
        # before the host-receiver test, because `Map` is a host root — but
        # `empty` is a value-namespace name, disjoint from the host verbs.
        if (isinstance(_callee, ExprField)
                and isinstance(_callee.target, ExprVar)
                and _callee.target.name == "Map"
                and _callee.target.name not in scope
                and _callee.name == "empty"):
            if expr.args:
                raise RevlError(
                    filename, expr.line,
                    f"`Map.empty()` takes no arguments, {len(expr.args)} given",
                    hint="build up an empty map with `set`: `Map.empty().set(\"k\", v)`",
                )
            return {"kind": "maplit", "entries": []}
        _host_receiver = isinstance(_callee, ExprField) and (
            _is_host_valued(_callee.target, scope)
            # the constructor root itself (Map.new(), Pool.open(...)):
            or (isinstance(_callee.target, ExprVar)
                and _callee.target.name in _HOST_CALLABLES
                and _callee.target.name not in scope)
        )
        if isinstance(expr.callee, ExprField) and not _host_receiver:
            method = expr.callee.name
            # item 383: receiver-first list transforms (`xs.map(f)` etc.) are
            # SUGAR for their generic free function; desugar to the plain call
            # here (the checker already typed the desugared form) so the
            # existing generic-call path lowers it. Pure syntactic redirect —
            # no builtin-method row, so no new per-backend branch.
            if method in LIST_TRANSFORMS:
                return _lower_pure_expr(desugar_list_transform(expr), scope,
                                        callables, alias_fns, filename,
                                        type_env, types)
            arity = _BUILTIN_METHODS.get(method)
            if arity is None:
                raise RevlError(
                    filename, expr.line,
                    f"no builtin method `{method}` on values — the stdlib surface is "
                    f"{', '.join(sorted(_BUILTIN_METHODS))} (docs/stdlib-2.0.md)",
                    hint="records carry data, not methods; call functions as `f(x)`, "
                         "and call arrows through a `let` binding",
                )
            # The stdlib-named-method sliver (roadmap 75(b)): only a receiver
            # the checker can *prove* is a stdlib value (Str/List/Int/Int32/
            # Bytes/Map) may take the builtin table. A receiver whose
            # provenance no constructor pins — an extern's return, a
            # host-object result, a type parameter — used to lower a
            # stdlib-named method as that builtin and misdispatch at runtime.
            # Refuse it like any other host-boundary method (HOST-METHOD); the
            # fence then closes to exactly "host-object results are on the
            # audit surface" (G8).
            recv_t = _expr_static_type(expr.callee.target, type_env, types)
            if _is_wildcard(recv_t):
                _refuse_unpinned_stdlib_method(method, recv_t, filename,
                                               expr.line)
            if len(expr.args) != arity:
                raise RevlError(filename, expr.line,
                                f"builtin `{method}` takes {arity} argument(s), "
                                f"{len(expr.args)} given")
            _refuse_zero_divisor(method, expr.args, filename, expr.line)
            # Value dot-accessors (roadmap item 189): a `Value` receiver's
            # `.field`/`.str`/`.list`/`.keys` is receiver-first SUGAR for the
            # `value_*` free function — it desugars here to the SAME call IR
            # `value_str(value_field(...))` lowers to, so the emitted code is
            # byte-identical on every tier and no backend needs a new branch.
            # Gated on a proven `Value` receiver: `.keys()` also names a Map
            # builtin, which keeps the generic path below (recv head != Value).
            # `.field`/`.str`/`.list` are Value-only, so the checker already
            # proved the receiver is `Value` by the time lowering runs.
            if method in _VALUE_ACCESSORS and parse_type(recv_t)[0] == "Value":
                return {
                    "kind": "call",
                    "callee": {"kind": "var", "name": _VALUE_ACCESSORS[method]},
                    "args": [_lower_pure_expr(expr.callee.target, scope, callables, alias_fns, filename, type_env, types)]
                    + [_lower_pure_expr(a, scope, callables, alias_fns, filename, type_env, types) for a in expr.args],
                }
            node: dict = {"kind": "builtin", "method": method,
                          "target": _lower_pure_expr(expr.callee.target, scope, callables, alias_fns, filename, type_env, types),
                          "args": [_lower_pure_expr(a, scope, callables, alias_fns, filename, type_env, types) for a in expr.args]}
            if method == "to_int":
                # `to_int` is spelled for two receiver families (Int32 widen,
                # Str parse) — the backends must dispatch on the receiver's
                # static type, which the IR node would otherwise not carry
                # (the same reason `un` annotates Int negation). Annotate it,
                # exactly as the checker selected the row.
                node["recv"] = infer_ast(expr.callee.target, type_env, types, None)
            return node
        # A call argument that widens Int -> Float is marked on the argument
        # node (`_mark_widen`) so every backend emits the conversion — the
        # `ident(3)` gap in docs/arithmetic.md. Generic signatures are
        # instantiated first, exactly as the checker does, so a `T` bound to
        # `Float` by this call marks too. Inference runs without a filename:
        # the checking pass has already diagnosed real mismatches, so this is
        # annotation only.
        if isinstance(expr.callee, ExprVar):
            sig = (types.get(FNS_KEY) or {}).get(expr.callee.name)
            if sig is not None and sig.get("params"):
                params = list(sig["params"])
                if sig.get("tparams"):
                    subst: dict = {}
                    arg_types = [infer_ast(a, type_env, types, None) for a in expr.args]
                    for p, a in zip(params, arg_types):
                        unify(p, a, subst)
                    params = [substitute(p, subst) for p in params]
                lowered_args = [_lower_pure_expr(a, scope, callables, alias_fns, filename, type_env, types)
                                for a in expr.args]
                for p, a, node in zip(params,
                                      [infer_ast(a, type_env, types, None) for a in expr.args],
                                      lowered_args):
                    _mark_widen(p, a, node)
                # item 187: fill omitted trailing arguments with each defaulted
                # parameter's default expression. Lower each default here and
                # mark the Int->Float widening on it exactly as a written
                # argument would get, so a `Float = 0` default emits correctly.
                sig = types.get(FNS_KEY, {}).get(expr.callee.name) or {}
                defaults = sig.get("defaults") or []
                for i in range(len(lowered_args), len(params)):
                    dexpr = defaults[i]
                    dnode = _lower_pure_expr(dexpr, scope, callables, alias_fns,
                                             filename, type_env, types)
                    _mark_widen(params[i], infer_ast(dexpr, type_env, types, None),
                                dnode)
                    lowered_args.append(dnode)
                return {"kind": "call", "callee": _lower_pure_expr(expr.callee, scope, callables, alias_fns, filename, type_env, types),
                        "args": lowered_args}
        return {"kind": "call", "callee": _lower_pure_expr(expr.callee, scope, callables, alias_fns, filename, type_env, types),
                "args": [_lower_pure_expr(a, scope, callables, alias_fns, filename, type_env, types) for a in expr.args]}
    if isinstance(expr, ExprField):
        target_type = _expr_static_type(expr.target, type_env, types)
        if expr.name == "length" and _is_sized_type(target_type):
            return {"kind": "len",
                    "target": _lower_pure_expr(expr.target, scope, callables, alias_fns, filename, type_env, types)}
        node = {"kind": "field",
                "target": _lower_pure_expr(expr.target, scope, callables, alias_fns, filename, type_env, types),
                "name": expr.name}
        # item 380: a field whose declared type is `Opt[T]` reads TOTAL — absent
        # yields the empty Opt, never a raise (py) or a `??`-outliving `undefined`
        # (ts) — so `e.kind ?? default` means the same on every tier.
        if _field_is_opt(target_type, expr.name, types):
            node["opt"] = True
        return node
    if isinstance(expr, ExprIndex):
        return {"kind": "index", "target": _lower_pure_expr(expr.target, scope, callables, alias_fns, filename, type_env, types),
                "index": _lower_pure_expr(expr.index, scope, callables, alias_fns, filename, type_env, types)}
    if isinstance(expr, ExprIf):
        return {"kind": "if", "cond": _lower_pure_expr(expr.cond, scope, callables, alias_fns, filename, type_env, types),
                "then": _lower_pure_expr(expr.then, scope, callables, alias_fns, filename, type_env, types),
                "else": _lower_pure_expr(expr.otherwise, scope, callables, alias_fns, filename, type_env, types)}
    if isinstance(expr, ExprRecord):
        # A record is a value type (syntax-2.0 §3.5): a field is initialised by
        # *copying* the initialiser's value into the record. Reading a `var`'s
        # field into a record (`{ x: v.field }`) has always been allowed for
        # exactly this reason, and a bare `var` read (`{ x: v }`) is the same
        # copy — the value is taken, the mutable cell is not captured, so the
        # `var` still never escapes its function. Both forms lower identically.
        return {"kind": "record",
                "fields": [[name, _lower_pure_expr(e, scope, callables, alias_fns, filename, type_env, types)]
                           for name, e in expr.fields]}
    if isinstance(expr, ExprRecordUpdate):
        return {"kind": "record_update",
                "base": _lower_pure_expr(expr.base, scope, callables, alias_fns, filename, type_env, types),
                "updates": [[name, _lower_pure_expr(e, scope, callables, alias_fns, filename, type_env, types)]
                            for name, e in expr.updates]}
    if isinstance(expr, ExprBlockArm):
        return _lift_block_arm(expr, scope, callables, alias_fns, filename,
                               type_env, types)
    if isinstance(expr, ExprList):
        return {"kind": "list",
                "items": [_lower_pure_expr(e, scope, callables, alias_fns, filename, type_env, types) for e in expr.items]}
    if isinstance(expr, ExprArrow):
        inner = dict(scope)
        inner_type_env = dict(type_env)
        param_types = _arrow_param_types(expr)
        for param, ptype in zip(expr.params, param_types):
            inner[param] = False
            if ptype:
                inner_type_env[param] = ptype
            else:
                inner_type_env.pop(param, None)
        captures = sorted(_mutable_free_vars(expr.body, scope, set(expr.params)))
        _b1_capture_check(expr, type_env, types, filename, expr.line)
        node = {"kind": "arrow", "params": expr.params, "captures": captures,
                "body": _lower_pure_expr(expr.body, inner, callables, alias_fns, filename, inner_type_env, types)}
        # IR v3: an arrow that the checker typed carries its signature, so a
        # backend can declare it instead of guessing (docs/function-types.md).
        # Both keys are absent together when the arrow is still untyped.
        if any(p is not None for p in param_types) or expr.returns:
            node["param_types"] = param_types
            node["returns"] = expr.returns
        # item 92: an arrow the checker typed against `(…) -> Async[T]` carries
        # the async color into the IR, so every emitter reads one shape
        # (`.get("async")`) instead of parsing the `returns` string.
        if expr.returns and parse_type(expr.returns)[0] == "Async":
            node["async"] = True
        return node
    if isinstance(expr, ExprMatch):
        scrutinee_type = _expr_static_type(expr.scrutinee, type_env, types)
        _check_match_exhaustiveness(expr, type_env, types, filename)
        scrutinee = _lower_pure_expr(expr.scrutinee, scope, callables, alias_fns, filename, type_env, types)
        arms = []
        for pattern, bind, body in expr.arms:
            inner_scope = dict(scope)
            inner_type_env = dict(type_env)
            payload_type = _arm_payload_type(scrutinee_type, pattern, types)
            if bind is not None:
                inner_scope[bind] = False
                inner_type_env.pop(bind, None)
                if payload_type is not None:
                    inner_type_env[bind] = payload_type
            arm = {
                "pattern": pattern,
                "bind": bind,
                "body": _lower_pure_expr(body, inner_scope, callables, alias_fns, filename, inner_type_env, types),
            }
            # payload type helps backends that must cast (e.g. Java's tagged
            # Result); other emitters ignore it
            if payload_type is not None:
                arm["payload_type"] = payload_type
            arms.append(arm)
        return {"kind": "match", "scrutinee": scrutinee, "arms": arms}
    if isinstance(expr, ExprOptField):
        return {
            "kind": "optfield",
            "target": _lower_pure_expr(expr.target, scope, callables, alias_fns, filename, type_env, types),
            "name": expr.name,
        }
    if isinstance(expr, ExprOptCall):
        return {
            "kind": "optcall",
            "target": _lower_pure_expr(expr.target, scope, callables, alias_fns, filename, type_env, types),
            "method": expr.method,
            "args": [_lower_pure_expr(a, scope, callables, alias_fns, filename, type_env, types) for a in expr.args],
        }
    if isinstance(expr, Interp):
        # each `${...}` segment is a full expression, lowered (and G1/type-
        # checked) like any other; text segments pass through
        parts: list = []
        for kind, value in expr.parts:
            if kind == "text":
                parts.append(["text", value])
            else:
                parts.append(["expr", _lower_pure_expr(
                    value, scope, callables, alias_fns, filename, type_env, types)])
        return {"kind": "interp", "parts": parts}
    raise RevlError(filename, getattr(expr, "line", 1), "unexpected expression in fn body")


def _inject_opt(expected: str | None, actual: str | None, node: dict) -> dict:
    """Materialize the `T` -> `Opt[T]` injection the checker accepts.

    `compatible` lets a `T` stand where an `Opt[T]` is declared (typecheck.py,
    "Opt discipline"), but accepting the *type* is only half of it: the value
    still has to become an optional. Nothing did that, so a method declared
    `-> Opt[Int]` returning `1` emitted `Option<i64> { 1 }` on rust and
    `Optional<Long> { return 1L; }` on java — both rejected by their
    compilers. python and TypeScript never noticed, which is why it survived:
    the injection is invisible on an untyped tier.

    Done here rather than in each backend because the frontend is the single
    IR producer, and because deciding it in a backend needs the very type
    information the emitters do not carry.
    """
    if actual is None:
        return node
    ehead, eargs = parse_type(expected)
    if ehead != "Opt" or not eargs:
        return node
    if parse_type(actual)[0] == "Opt":
        return node          # already an optional; injecting would double-wrap
    return {"kind": "call", "callee": {"kind": "var", "name": "Some"}, "args": [node]}


def _default_expr_callees(expr, out: set) -> None:
    """Collect the names of every function/constructor *called* inside a
    default-value expression (item 187 purity check). Only var-headed call
    callees matter — an effectful operation is always a named extern/fn."""
    if isinstance(expr, ExprCall):
        if isinstance(expr.callee, ExprVar):
            out.add(expr.callee.name)
        _default_expr_callees(expr.callee, out)
        for a in expr.args:
            _default_expr_callees(a, out)
    elif isinstance(expr, ExprBin):
        _default_expr_callees(expr.left, out)
        _default_expr_callees(expr.right, out)
    elif isinstance(expr, ExprUn):
        _default_expr_callees(expr.operand, out)
    elif isinstance(expr, (ExprField, ExprOptField)):
        _default_expr_callees(expr.target, out)
    elif isinstance(expr, ExprOptCall):
        _default_expr_callees(expr.target, out)
        for a in expr.args:
            _default_expr_callees(a, out)
    elif isinstance(expr, ExprIndex):
        _default_expr_callees(expr.target, out)
        _default_expr_callees(expr.index, out)
    elif isinstance(expr, ExprIf):
        _default_expr_callees(expr.cond, out)
        _default_expr_callees(expr.then, out)
        _default_expr_callees(expr.otherwise, out)
    elif isinstance(expr, ExprRecord):
        for _, value in expr.fields:
            _default_expr_callees(value, out)
    elif isinstance(expr, ExprRecordUpdate):
        _default_expr_callees(expr.base, out)
        for _, value in expr.updates:
            _default_expr_callees(value, out)
    elif isinstance(expr, ExprList):
        for item in expr.items:
            _default_expr_callees(item, out)
    elif isinstance(expr, Interp):
        for kind, part in expr.parts:
            if kind == "expr":
                _default_expr_callees(part, out)


def _validate_default_params(program: Program, types: dict,
                             emitting_fns: set) -> None:
    """Decl-site checks for default parameters (item 187): a default is a pure
    expression that type-checks against its parameter. The ordering invariant
    (defaults are trailing) is enforced in the parser; here we (a) refuse an
    effectful default — one that reaches an emission/acquire/witnessed extern
    or an emitting fn, which would smuggle an effect into an otherwise pure
    call-site expansion — and (b) type-check the default against the declared
    parameter type, once, at the declaration rather than at every call."""
    effectful = {ext.name for ext in program.externs
                 if ext.classification != "pure"} | set(emitting_fns or ())
    for decl in program.fn_decls:
        for p in decl.params:
            default = getattr(p, "default", None)
            if default is None:
                continue
            reached: set = set()
            _default_expr_callees(default, reached)
            bad = sorted(reached & effectful)
            if bad:
                raise RevlError(
                    program.filename, p.line,
                    f"default for parameter `{p.name}` calls `{bad[0]}`, which "
                    "is effectful",
                    hint="a default value must be a pure expression — it is "
                         "evaluated at the call site whenever the argument is "
                         "omitted, and an effect there would be invisible in the "
                         "source",
                    code="G6", category="purity",
                )
            dt = infer_ast(default, {}, types, program.filename)
            if dt is not None and not compatible(p.type, dt):
                raise mismatch(program.filename, p.line,
                               f"default for parameter `{p.name}`", p.type, dt)


def _component_header_stub(comp: ComponentDecl, filename: str) -> dict:
    """A header-only placeholder for a component whose BODY lowering aborted
    (item 386, Stage 1, Change 1 — the soundness fix).

    Body lowering raised, so no lowered body exists — but DROPPING the component
    corrupts `_link`: the multi-realm route check would fabricate "no component
    provides key in realm" for a consumer routing to this component's key, and
    G2 (provision conflict) / G3 (dependency cycle) would silently MISS a real
    conflict or cycle on a key this component's HEADER declares. So we keep the
    topology complete from the parts available BEFORE body lowering — the
    `requires`/`provides` clauses on the `component` declaration — and mark it
    `poisoned` so the body-walking post-passes (taint, spawn bounds/attenuation,
    holes) skip it. `isolate`/`routes` are body statements, so a stub carries
    none: its provisions sit in the shared realm, which is exactly enough to
    stop the route-check fabrication and keep G2/G3 sound over its keys."""
    return {
        "name": comp.name,
        "source": comp.source or filename,
        "requires": {local for local, _svc, _line in comp.requires},
        "provides": {key for key, _svc, _line in comp.provides},
        "body": [],
        "poisoned": True,
    }


def _raise_collected(errors: list[RevlError], program: Program) -> None:
    """Dedup, order, and raise the collected refusals as a `RevlErrors` carrier
    (item 386, Stage 1, Change 4).

    Dedup key is `(code, filename, line, message)` — two recovery paths reaching
    the same refusal collapse to one. Ordering is by COMPILE ORDER, not
    alphabetical: `program.filename` (paths[0]) first, then each component's
    `source` in declaration order (== the multi-file argument order, roadmap
    312), then line. Python's sort is stable, so ties keep the pipeline
    (append) order. This makes `diagnostics[0]` equal to what today's
    single-error compile reports for the same input, keeping every existing
    single-error test and consumer stable."""
    file_rank: dict[str, int] = {}

    def _rank(name: str) -> int:
        if name not in file_rank:
            file_rank[name] = len(file_rank)
        return file_rank[name]

    _rank(program.filename)
    for comp in program.components:
        _rank(comp.source or program.filename)

    seen: set = set()
    unique: list[RevlError] = []
    for err in errors:
        key = (err.code, err.filename, err.line, err.message)
        if key in seen:
            continue
        seen.add(key)
        unique.append(err)

    unique.sort(key=lambda e: (file_rank.get(e.filename, len(file_rank)), e.line))
    raise RevlErrors(unique)


# --- item 379 / Decision 2 (docs/design/379-break-continue.md) --------------
# The frame-neutrality of `break`/`continue` rests on a grammar accident: loops
# live only in the fn statement grammar, and every teardown-registering form
# lives only in the activation/method grammar, so the two never meet and no
# emitter wraps a loop in teardown scaffolding. This pass makes that accident an
# enforced whole-IR invariant, run once over the lowered IR: no registering step
# may sit inside a `while`/`for` body, and — the same invariant read the other
# way — no `while`/`for` step may sit in a component activation, provide-method,
# or setup body. Either leak would let a future item register teardown at a loop
# boundary; the java setup emitter (`_emit_setup_stmt`) would even compile the
# loop-in-activation form silently, so a parse-time counter or a single
# lowering-local assert would not catch it on every tier.

_REGISTERING_STEP_KINDS = frozenset({
    "effect", "let-effect", "emit", "timer", "approval", "spawn",
})
_LOOP_STEP_KINDS = frozenset({"while", "for"})


def _iter_all_fn_steps(steps):
    """Every step in a fn-grammar body, descending `if` arms and loop bodies."""
    for step in steps or []:
        yield step
        kind = step.get("step")
        if kind in _LOOP_STEP_KINDS:
            yield from _iter_all_fn_steps(step.get("body") or [])
        elif kind == "if":
            yield from _iter_all_fn_steps(step.get("then") or [])
            yield from _iter_all_fn_steps(step.get("else") or [])


def _iter_loop_scoped_steps(steps):
    """Every step lexically inside a `while`/`for` body within a fn-grammar step
    list (descending `if` arms, and nested loops via `_iter_all_fn_steps`)."""
    for step in steps or []:
        kind = step.get("step")
        if kind in _LOOP_STEP_KINDS:
            yield from _iter_all_fn_steps(step.get("body") or [])
        elif kind == "if":
            yield from _iter_loop_scoped_steps(step.get("then") or [])
            yield from _iter_loop_scoped_steps(step.get("else") or [])


def _find_loop_step(node):
    """Deep-search an activation/method/setup IR subtree for a `while`/`for`
    step. Expression nodes key on `kind`, never `step`, and component IR never
    embeds a module `fn` body, so this never false-positives on a real loop."""
    if isinstance(node, dict):
        if node.get("step") in _LOOP_STEP_KINDS:
            return node
        for value in node.values():
            found = _find_loop_step(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_loop_step(item)
            if found is not None:
                return found
    return None


def _validate_no_loop_scoped_registration(ir: dict, filename: str) -> None:
    doc = "docs/design/379-break-continue.md"
    fn_bodies: list[list] = [fn.get("body") or [] for fn in ir.get("functions") or []]
    for key in ("tests", "fault_tests", "prop_tests"):
        for unit in ir.get(key) or []:
            body = unit.get("body")
            if isinstance(body, list):
                fn_bodies.append(body)
    for body in fn_bodies:
        for step in _iter_loop_scoped_steps(body):
            kind = step.get("step")
            if kind in _REGISTERING_STEP_KINDS:
                raise RevlError(
                    filename, step.get("line", 0),
                    f"a `{kind}` step registers teardown and may not appear "
                    f"inside a `while`/`for` body",
                    hint="`break`/`continue` are frame-neutral only because loops "
                         "and registration never meet; an item that wants them to "
                         f"must first amend the teardown contract ({doc})",
                )
    for component in ir.get("components") or []:
        loop = _find_loop_step(component)
        if loop is not None:
            raise RevlError(
                filename, loop.get("line", 0),
                "a `while`/`for` loop may not appear in a component activation "
                "or provide-method body",
                hint="iteration lives in the fn statement grammar; lift the loop "
                     f"into a module `fn` and call it ({doc})",
            )


def check_and_lower(program: Program, ambient: dict | None = None,
                    taint_strict: bool = False) -> dict:
    """Check and lower a program, optionally against an *ambient* composition
    (a running manifest, DESIGN §4's runtime-admission gate): ambient services
    are in scope without redeclaration, and G2/G3 are checked over the union
    of ambient and newly compiled components.

    `ambient`: {"services": <v1 services table>, "components": [<manifest
    component entries>]} — see compile_files for how it is derived.

    `taint_strict` (item 249, Slice D) turns on derived taint sinks and sources —
    off by default and byte-identical when off, on under the untrusted-author
    profile or `revl compile --taint-strict`.
    """
    ambient = ambient or {}

    # Taint/provenance (roadmap item 249, Slice A). Read the `Untrusted[T]` /
    # `Trusted[T]` qualifier surface off every declaration and STRIP it from the
    # declared types in place, so base typing, method lookup and the emitted IR
    # are byte-identical for any program that uses no qualifier. The flow verdict
    # (`check_taint`, below) runs once every component body is lowered.
    taint_model = extract_and_normalize(program, taint_strict=taint_strict)

    ambient_services = {
        name: _service_from_ir(name, spec)
        for name, spec in (ambient.get("services") or {}).items()
    }

    services: dict[str, ServiceDecl] = {}
    for svc in program.services:
        if svc.name in services:
            raise RevlError(program.filename, svc.line, f"duplicate service `{svc.name}`")
        prior = ambient_services.get(svc.name)
        if prior is not None:
            _admit_service_replacement(program, svc, prior, ambient)
        services[svc.name] = svc
    for name, svc in ambient_services.items():
        services.setdefault(name, svc)

    component_callables = (
        _HOST_CALLABLES
        | _BUILTIN_CONSTRUCTORS
        | _DECLASSIFY_BUILTINS
        | {fn.name for fn in program.fn_decls}
        | {ext.name for ext in program.externs}
    )

    # types and signatures are built first so component lowering can
    # type-check service/fn call sites (the sound-typing milestone)
    _resolve_type_aliases(program, program.filename)
    _validate_declared_types(program, program.filename)
    types = _lower_type_decls(program, program.filename)
    types[FNS_KEY] = _signature_table(program, types)
    types[CASES_KEY] = _case_table(types)
    fns = _lower_fns(program, program.filename, types)
    externs = _lower_externs(program, program.filename, types, fns)
    # item 388: the poly externs (`fn|async`), pre-seeded above as async IR
    # entries. `extern_colour_instances` is the shared registry each provide
    # method fills with the colour its call sites of a poly extern requested (the
    # analog of `sync_monomorphs`); `_finalize_poly_externs` reads it after the
    # component loop to split/prune each poly entry into its concrete clones.
    # Empty unless the program declares a poly extern, so every downstream section
    # is byte-identical without one.
    poly_extern_names: set = {
        d.name for d in program.externs if getattr(d, "colour_poly", False)}
    extern_colour_instances: dict = {}
    # item 246: the declaration-owned approval facts, stashed on the type table so
    # `_lower_emit_step`'s per-crossing obligation can read them with no signature
    # change. Empty `required` set unless an extern declared `requires approval`,
    # so a program with none is byte-identical.
    types[APPROVAL_KEY] = _approval_index(externs)
    # witnessed externs are refused outside effect position (item 243 rule 1):
    # a fn/test body has no teardown accumulator, so the auto-registered inverse
    # would be dropped and the mutation would be silently irreversible.
    _refuse_witnessed_outside_effect_position(program, program.filename)
    # items 399/400: the acquire-with-`undo` and `deferred`-emission twins of the
    # witnessed rule-1 refusal. A fn/test body has no teardown accumulator and no
    # session commit, so a bare call would drop the declared `undo` (399) or fire
    # the deferred emission immediately, bypassing the commit queue (400).
    _refuse_teardown_bound_externs_in_fn_body(program, program.filename)
    # Slice 2: the set every component's effect-position lowering consults to
    # tell a witnessed acquisition from an ordinary one (docs/design/243-
    # witnessed-externs.md). Computed once — every component shares the same
    # extern table.
    witnessed_externs = _witnessed_extern_names(program)
    # One fixed point, two consumers: `emitting_caps` is what it computes
    # (docs/capabilities.md), `witness` is why (why.py). Evidence never
    # decides a rejection, it only explains one.
    emission_evidence = _EmissionEvidence(program)
    emitting_caps = _emitting_capabilities(fns, externs, emission_evidence.witness)
    emitting_fns = set(emitting_caps)

    # item 187: default-parameter values must be pure and well-typed. Checked
    # here, once the emission fixed point is known, so an effectful default is
    # refused before any call site expands it.
    _validate_default_params(program, types, emitting_fns)

    # phase-2 async coloring (docs/design/async-extern.md §3): the async twin
    # of the emission fixed point above. Seed = async externs; a module `fn`
    # that reaches a colored callee — directly or transitively — is itself
    # colored. `async_witness` records the shortest derivation for diagnostics.
    async_witness: dict[str, str] = {}
    async_colored = _async_callables(fns, externs, async_witness)

    # sync/async arrow polymorphism (roadmap item 342, the dual of item 92).
    # A fn colored async *solely* because it calls its own async-typed callback
    # parameter — and reaching no other suspension — is "colour-polymorphic":
    # its async-ness is contingent on the arrow actually passed. When a sync
    # caller hands it a genuinely-sync arrow (a value that trivially lifts into
    # a completed async, item 92 §2), that call site does not suspend, so the
    # fn is monomorphized to a SYNC clone there — one source loop serves an
    # async evolve path and a sync tool-call path with no duplicated twin.
    colour_polymorphic: set = _colour_polymorphic_fns(fns, async_colored)
    # Filled by the sync call sites (provide methods, module fns, and — below —
    # `test` bodies): monomorph-name -> origin fn name. Synthesized into `fns`
    # once every call site has registered.
    sync_monomorphs: dict[str, str] = {}

    # item 387 (finishing item 342 phase 2): 342 hooked its monomorphization
    # into `_lower_provide` alone, so a module `fn` reaching a colour-polymorphic
    # loop only through genuinely-sync arrows was auto-colored async here instead
    # of kept sync — and a sync context calling it then diverged (py ran to a
    # bare coroutine, ts refused at emit, H29). Redirect those free-fn call
    # sites to the sync monomorph and recolor, so a free fn whose only async
    # reach was such a call is sync on BOTH tiers. No-op (and `async_colored`
    # byte-identical) when no such call exists.
    if colour_polymorphic:
        async_colored = _monomorphize_free_fn_calls(
            fns, externs, colour_polymorphic, async_colored, types,
            sync_monomorphs, component_callables, async_witness)
        colour_polymorphic = _colour_polymorphic_fns(fns, async_colored)

    # Stamp `"async": True` on every colored fn entry (the emitters read it
    # with `.get("async")`, needing no reachability analysis of their own),
    # mirroring the extern spelling in `_lower_externs`. And refuse first-class
    # *value* use of an async callable: an arrow type carries no color, so
    # passing an async extern or a colored fn as a value would smuggle a
    # suspension past the checker — the async fixed point never widened for it,
    # so it is a compile error, not a coloring (async-extern.md §3, "First-class
    # values are refused, not widened").
    _fn_decls_by_name = {d.name: d for d in program.fn_decls}
    for entry in fns:
        name = entry["name"]
        called_vals: set = set()
        _calls_in(entry.get("body") or [], set(), values=called_vals)
        passed = sorted(called_vals & async_colored)
        if passed:
            decl = _fn_decls_by_name.get(name)
            raise RevlError(
                (decl.source if decl is not None else None) or program.filename,
                decl.line if decl is not None else 0,
                f"function `{name}` uses async callable `{passed[0]}` as a "
                f"function value, but an async callable has no arrow type",
                hint="call it directly from an async context — an arrow type "
                     "carries no async color, so a suspension cannot be awaited "
                     "through it (A1)",
                code="A1", category="async-propagation",
            )
        # item 92: a sync-typed arrow in a module fn that reaches an async
        # callable (an async extern or a colored fn) is the finding-#21 leak —
        # a compile error now. An arrow the checker typed against `(…) ->
        # Async[T]` carries the async flag and is admitted; a plain one that
        # reaches a suspension is refused. Pure fns have no req keys, so only
        # the named-callable reach applies here (rule 3 is component-only).
        _refuse_leaky_pure_arrow(entry.get("body") or [], async_colored,
                                 _fn_decls_by_name.get(name), program.filename)
        if name in async_colored:
            entry["async"] = True

    # instance-parametric components: one registry shared across the lowering
    # of every component, so `spawn C` can resolve C's config/provisions and
    # the linker can learn which components are runtime templates and what the
    # spawn (instance) graph is (docs/design-v2-instances.md).
    spawn_reg: dict = {
        "by_name": {c.name: c for c in program.components},
        "edges": [],        # (spawner, target) — the instance graph
        "templates": set(),  # components that are spawn targets (excluded from static composition)
        "sites": [],        # G4 spawn-boundary obligations, checked after lowering
    }

    # item 386, Stage 1: collect ALL refusals in one pass instead of aborting
    # on the first. `errors` accumulates every recoverable refusal; the
    # carrier is raised once at the end (below). The component loop is the
    # cleanest recovery unit — by this point the type and signature tables
    # already exist, so one component's failure does not poison another's
    # lowering.
    errors: list[RevlError] = []

    def _collect(fn, *args, **kwargs):
        """Run a whole-composition post-pass, collecting a `RevlError` instead
        of aborting so its sibling passes still run (item 386, Change 2). An
        UNEXPECTED (non-`RevlError`) crash is dropped only when the compile is
        ALREADY failing: a post-pass tripping over poisoned/partial state must
        not replace N good diagnostics with a traceback. On an otherwise-clean
        compile it propagates as the bug it is."""
        try:
            return fn(*args, **kwargs)
        except RevlError as post_error:
            errors.append(post_error)
            return None
        except Exception:  # noqa: BLE001 — see docstring: guarded only when failing
            if errors:
                return None
            raise

    components = []
    seen = set()
    for comp in program.components:
        if comp.name in seen:
            # A duplicate component is already represented in the topology by
            # its first declaration, so record the refusal but append NO stub
            # (a second same-named entry would fabricate a `_link` G2 conflict).
            errors.append(RevlError(comp.source or program.filename, comp.line,
                                    f"duplicate component `{comp.name}`"))
            continue
        seen.add(comp.name)
        try:
            # A multi-file composition merges declarations from several sources
            # into one Program whose `filename` is only the first argument
            # (paths[0]). Body diagnostics render through `env.filename`, so a
            # component from a LATER file must lower under its own `source`.
            # Otherwise its rejection names the first source with this file's
            # line number (roadmap 312).
            lowered_comp = _lower_component(comp, services,
                                            comp.source or program.filename,
                                            component_callables, types, emitting_fns,
                                            emitting_caps, emission_evidence, spawn_reg,
                                            async_colored, witnessed_externs,
                                            colour_polymorphic, sync_monomorphs,
                                            poly_extern_names, extern_colour_instances,
                                            errors=errors)
            if comp.source:
                _retarget_holes(lowered_comp, comp.source)
            # async coloring (docs/design/async-extern.md §3, "Component
            # bodies"): a setup/activation body (an `emit` step, an `effect`, an
            # `await` step) may not reach an async callable — an async extern
            # *or* a phase-2 colored fn — because divert/inertia semantics are
            # out of scope for v1. Provide-method bodies are checked at their
            # own site above, so they are pruned here. A component that recovered
            # past a refused statement (item 386, Stage 2) has a partial body and
            # is already failing, so skip the reach sweep — walking its poisoned
            # body could fabricate or crash, and its diagnostics are collected.
            if async_colored and not lowered_comp.get("poisoned"):
                _reached: set = set()
                _async_reached_outside_provide(lowered_comp, async_colored, _reached)
                if _reached:
                    _culprit = sorted(_reached)[0]
                    _kind = "extern" if _culprit in {
                        e.name for e in program.externs if e.async_} else "function"
                    raise RevlError(
                        comp.source or program.filename, comp.line,
                        f"component `{comp.name}` reaches async {_kind} "
                        f"`{_culprit}` in a setup/activation body, which "
                        f"cannot suspend a fiber (A1)",
                        hint="wrap the suspending call in an `async fn` service "
                             "operation and drive it from a provide method — v1 does "
                             "not lower an awaited `emit` step",
                        code="A1", category="async-propagation",
                    )
            components.append(lowered_comp)
        except RevlError as comp_error:
            # This component's BODY lowering aborted. DROPPING it would corrupt
            # `_link` (the route check fabricates "no provider", G2/G3 silently
            # miss real conflicts/cycles on its keys), so append a HEADER-ONLY
            # stub — provides/requires from the `comp` declaration, marked
            # `poisoned` — to keep the topology complete, and continue.
            errors.append(comp_error)
            components.append(_component_header_stub(comp, program.filename))
            continue

    # item 386: the body-walking post-passes run over the SUCCESSFULLY lowered
    # components only. A poisoned header stub has no lowered body, so feeding it
    # to taint / spawn / hole walks would crash or fabricate; `_link` alone sees
    # the full list (stubs included) because it needs the complete topology.
    live_components = [c for c in components if not c.get("poisoned")]

    # sync/async arrow polymorphism (item 342): materialize the sync clones the
    # sync call sites above requested. Additive — a program with no lifted call
    # site registers none, so `fns` (and every downstream section) is
    # byte-identical to before.
    _collect(_synthesize_sync_monomorphs, fns, sync_monomorphs)

    # Taint/provenance verdict (item 249, Slice A): refuse any untrusted-origin
    # value that reaches a `Trusted[T]` sink without a declassifier on its path
    # (G9). No-op and byte-identical when the program declared no qualifier.
    _collect(check_taint, program, fns, live_components, taint_model, program.filename)

    # state hand-off admission (roadmap item 53): a candidate provider that
    # *accepts* a `handoff` on a key some running provider *exports* must accept
    # a §5-compatible shape — else the swap would drop the predecessor's state.
    # No-op unless the ambient carries running hand-offs (a swap against a
    # stateful running provider), so a fresh compile is unaffected.
    _collect(_admit_handoff_replacement, program, live_components, ambient)

    # G4/G6 across the spawn boundary: a spawner's declared emission upper
    # bound must cover what its spawned instances emit (decision 8). Checked
    # here, after every component's emission surface is known.
    _collect(_check_spawn_emission_bounds, live_components, services, spawn_reg,
             program.filename)

    # Capability attenuation across the spawn boundary (item 66): a child's
    # capability set must be a checked subset of its spawner's held authority,
    # so lineage narrows monotonically and a supervisor cannot amplify. Returns
    # the per-instance attenuation chain for the G8 audit surface.
    attenuation_chain = _collect(_check_spawn_attenuation,
                                 live_components, services, spawn_reg,
                                 program.filename)

    fault_tests = _collect(_lower_fault_tests, program, live_components,
                           program.filename)

    # `_link` collects its own G2/G3 refusals into the shared `errors` sink
    # (item 386, Change 2): it must see the FULL component list, stubs included,
    # so its topology is complete — a real conflict/cycle on a refused
    # component's declared key is still reported, and a consumer routing to it
    # gets no fabricated "no provider" error.
    manifest = _collect(_link, program, components, ambient.get("components") or [],
                        templates=spawn_reg["templates"], errors=errors)
    if manifest and attenuation_chain:
        # additive, spawn-only: a non-spawning composition has no `instances`
        # key, so its manifest is byte-identical to before (docs/capability-
        # attenuation.md).
        manifest["instances"] = attenuation_chain

    # lifecycle tests are lowered last: they check against the component
    # declarations, so a broken component must report itself first
    tests = _collect(_lower_tests, program, program.filename, types, services)
    prop_tests = _collect(_lower_prop_tests, program, program.filename, types, services)

    # item 387: a plain `test`/`prop test` body is a pure SYNC context. Finish
    # item 342 there — monomorphize each colour-polymorphic call handed only
    # genuinely-sync arrows to its sync clone, and refuse (A1) any residual reach
    # of an async callable — so a test never emits a bare, un-awaited call to an
    # async callable (py) that the ts emitter would refuse (the H29 divergence).
    # Then re-run the sync-clone synthesis to materialize any clone a test body
    # was the sole caller of. Both are no-ops (IR byte-identical) when no async
    # callable and no colour-polymorphic fn exist.
    if async_colored or colour_polymorphic:
        _collect(_admit_sync_test_bodies, tests, prop_tests, async_colored,
                 colour_polymorphic, types, sync_monomorphs, component_callables,
                 program.filename)
        _synthesize_sync_monomorphs(fns, sync_monomorphs)

    # item 388: caller-decided extern colour — split and prune, run LAST so every
    # section that can name an extern (fns, components, tests, prop tests) has
    # been lowered. Each provide method has recorded which colours its poly-extern
    # call sites requested (`extern_colour_instances`); the sync PROVIDE-method
    # calls have already been rewritten to the `_revl_sync` clone. Materialize the
    # concrete clones and PRUNE the colour no call site used (the eager-expand-
    # and-prune resolution of the ordering wrinkle). The async clone additionally
    # survives whenever the ORIGINAL name is still referenced anywhere — an async
    # method, a module `fn`, or a `test` — so no such call dangles. Additive: no
    # poly extern means `poly_extern_names` is empty and `externs` is unchanged.
    if poly_extern_names:
        poly_referenced: set = set()
        for _section in (fns, live_components, tests, prop_tests):
            _calls_in(_section or [], poly_referenced)
        _collect(_finalize_poly_externs, externs, poly_extern_names,
                 extern_colour_instances, poly_referenced)

    # item 386: every recoverable refusal is now collected. Raise them together
    # as a `RevlErrors` carrier BEFORE building the IR — the result dict reads
    # `manifest`/`components`/etc. which may be partial or poisoned on the error
    # path, so short-circuiting here keeps a failing compile from crashing while
    # assembling an IR nobody will read. A clean compile has `errors == []` and
    # falls straight through, byte-identical to before.
    if errors:
        _raise_collected(errors, program)

    uses_components_2 = any(
        isinstance(stmt, (FailStmt, IfStmt))
        or (isinstance(stmt, (LetEffect, EffectStmt)) and stmt.setup)
        for comp in program.components
        for stmt in comp.body
    )
    # a timer (`every`/`after`, item 57) is additive v3: a consumer predating
    # the construct refuses the whole document rather than silently dropping a
    # `timer` body step it cannot schedule (docs/time-coeffect.md). ir_version
    # stays 3 — no bump beyond it.
    uses_timers = any(
        isinstance(stmt, TimerStmt)
        for comp in program.components
        for stmt in comp.body
    )
    uses_v2 = any(
        comp.get("isolate") or comp.get("intercept") or comp.get("routes")
        for comp in components)
    uses_v3 = any(not name.startswith("__") for name in types) or bool(fns) or bool(externs) or bool(tests)
    uses_v3 = uses_v3 or any(
        svc.commutative or any(m.async_ or m.commutative or m.idempotent
                               for m in svc.methods.values())
        for svc in services.values()
    )
    uses_v3 = uses_v3 or uses_components_2
    uses_v3 = uses_v3 or uses_timers
    # a `fault_tests` section is an additive v3 feature; the version bump is
    # itself a guard, so a consumer that predates the section refuses the
    # whole document instead of silently dropping the fault tests
    uses_v3 = uses_v3 or bool(fault_tests)
    # a `prop_tests` section is likewise additive v3 (roadmap item 37)
    uses_v3 = uses_v3 or bool(prop_tests)

    def _has_builtin(node) -> bool:
        if isinstance(node, dict):
            # a tagged ADT construction is a v3 feature too (`adt` node)
            if node.get("kind") in ("builtin", "adt"):
                return True
            return any(_has_builtin(v) for v in node.values())
        if isinstance(node, list):
            return any(_has_builtin(v) for v in node)
        return False

    uses_v3 = uses_v3 or any(_has_builtin(comp.get("body")) for comp in components)
    # instance-parametric components are a v3 feature: a `spawn` node in any
    # body bumps the version so a consumer predating the feature refuses the
    # document rather than mis-composing a runtime template as a static entry
    # (docs/design-v2-instances.md)
    uses_v3 = uses_v3 or bool(spawn_reg["templates"])

    result = {
        "ir_version": IR_VERSION_V3 if uses_v3 else (IR_VERSION_V2 if uses_v2 else IR_VERSION),
        "services": {
            name: {
                **({"commutative": True} if svc.commutative else {}),
                "methods": {
                    m.name: {
                        "params": [{"name": p, "type": t} for p, t in m.params],
                        "returns": m.returns,
                        "emission": m.emission,
                        # only a *scoped* emission carries the key: bare
                        # `emission` means "any capability", and its absence
                        # is exactly that (so no pre-capability IR changes)
                        **({"capabilities": list(m.capabilities)}
                           if m.capabilities is not None else {}),
                        **({"async": True} if m.async_ else {}),
                        **({"commutative": True} if m.commutative else {}),
                        # delivery semantics (roadmap item 44): the checked
                        # right for the runtime to auto-retry this emission
                        **({"idempotent": True} if m.idempotent else {}),
                    }
                    for m in svc.methods.values()
                },
            }
            for name, svc in services.items()
        },
        "components": components,
        "manifest": manifest,
    }
    user_types = {name: spec for name, spec in types.items() if not name.startswith("__")}
    if user_types:
        result["types"] = user_types
    if fns:
        result["functions"] = fns
    if externs:
        result["externs"] = externs
    if tests:
        result["tests"] = tests
    # the obligation ledger (docs/holes.md). Present only when the draft has
    # holes, so an IR document for finished code is byte-identical to before.
    obligations = holes.collect(result)
    if obligations:
        result["holes"] = obligations

    if fault_tests:
        result["fault_tests"] = fault_tests
    if prop_tests:
        result["prop_tests"] = prop_tests
    # item 249: the taint verdict has run, so `endorse(v)` — identity on the base
    # type — is spliced out of the IR here; no emitter or golden sees it. A
    # program with no `endorse` is rebuilt identically (byte-identity).
    if taint_model.active:
        result = splice_declassifiers(result)
    # item 379 Decision 2: the frame-neutrality invariant, enforced over the
    # fully-assembled IR (after every body is lowered and any declassifier
    # splice has run).
    _validate_no_loop_scoped_registration(result, program.filename)
    return result


# Admission gate and service-compatibility relation (DESIGN.md §5/§6.6) live
# in `admission.py` (roadmap item 17). Re-exported here so `revl.lower` keeps
# its public import surface: the check-and-lower spine, `plan.py`, and the
# service-compat tests all import these names from `revl.lower`.
from .admission import (  # noqa: E402,F401
    _Drift,
    _Touchers,
    _admit_handoff_replacement,
    _admit_service_replacement,
    _caps_str,
    _caps_widen,
    _drift_error,
    _handoff_compatible,
    _handoff_error,
    _service_compatible,
    _service_equal,
    _service_from_ir,
    _service_touchers,
)


# ---------------------------------------------------------------- components

def _component_scope(env: Env) -> dict[str, str]:
    scope = {name: safe for name, safe in env.locals.items()}
    scope.update(env.params)
    return scope


def _component_req_call(env: Env, root: str, method: str, args: list, line: int) -> dict:
    if root not in env.requires:
        raise RevlError(env.filename, line,
                        f"`{root}` is not a declared requirement of {env.component.name}")
    svc = env.services[env.requires[root]]
    decl = svc.methods.get(method)
    if decl is None:
        raise RevlError(env.filename, line,
                        f"`{root}.{method}` is not a method of service {svc.name}")
    if len(args) != len(decl.params):
        raise RevlError(env.filename, line,
                        f"`{root}.{method}` takes {len(decl.params)} "
                        f"argument(s), {len(args)} given")
    if decl.emission and getattr(env, "_expr_mode", "setup") == "setup":
        raise RevlError(
            env.filename, line,
            f"call to emission `{root}.{method}` must be marked `emit` (G4)",
            hint="an emission crosses the system boundary and cannot be reverted; "
                 "`emit` makes that visible at the call site",
        )
    for arg, (pname, ptype) in zip(args, decl.params):
        actual = infer_ir(arg, env.type_env, env.types, env.services)
        if ptype and actual and not compatible(ptype, actual):
            raise mismatch(env.filename, line,
                           f"`{root}.{method}` argument `{pname}`", ptype, actual)
    return {"kind": "call", "target": {"kind": "req", "name": root},
            "method": method, "args": args}


def _refuse_block_arm_stmt(stmt, filename: str):
    """A provide-method match block arm lowers only `let` bindings + a final
    expression (roadmap item 361): that is the shape BOTH tiers emit as an
    expression (an awaited walrus sequence on the expression-only python tier,
    an IIFE on ts). A loop / reassignment / destructuring is refused with a
    clear message rather than mis-compiled — the sound residual, narrower than
    the former blanket "not lowerable here" refusal."""
    desc = {
        AssignStmt: "a reassignment",
        WhileStmt: "a `while` loop",
        ForStmt: "a `for` loop",
        IfStmt: "a statement `if`",
        LetPatternStmt: "a destructuring `let`",
    }.get(type(stmt), "this statement")
    raise RevlError(
        filename, getattr(stmt, "line", 0),
        f"{desc} is not lowered inside a provide-method match block arm",
        hint="a block arm here supports `let` bindings and a final expression "
             "(the shape both tiers emit as an expression); lift a loop or "
             "reassignment into a module `fn`, or rewrite it with `let` / an "
             "`if`-expression (docs/records.md §6)",
        code="G6", category="block-arm",
    )


def _lower_component_block_arm(expr, env: Env, scope: dict[str, str],
                               callables: set, pure_only: bool = False) -> dict:
    """Lower a statement-block match arm (`=> { let x = …; expr }`) inside a
    component / provide-method body (roadmap item 361).

    A module-fn block arm is lambda-lifted into a synthetic helper `fn`
    (`_lift_block_arm`), but a provide-method block arm may read component
    `config` or a required service, which a module `fn` cannot hold — so it is
    lowered *inline* as a `do` expression (a `let`-sequence + a final value).
    The emitters render it as an IIFE (ts) / an awaited walrus sequence (py),
    so an async extern reached in the block is awaited within the method's
    in-flight window. The async-coloring fixed point and the A1 refusals are
    left untouched: they walk the lowered body (`_calls_in`), see the async
    callable inside the `do` node, and still refuse a *sync* method reaching
    it — only an `async` method admits and awaits it."""
    filename = env.filename
    inner = dict(scope)
    taken = set(inner.values())
    saved_tenv = dict(env.type_env)
    stmts: list[dict] = []
    try:
        for st in expr.stmts:
            if not isinstance(st, LetStmt):
                _refuse_block_arm_stmt(st, filename)
            value = _lower_component_pure_expr(st.value, env, inner, callables, pure_only)
            safe = _safe_name(st.name, taken)
            taken.add(safe)
            inner[st.name] = safe
            if st.type is not None:
                check_type_wellformed(filename, st.line, st.type)
                env.type_env[safe] = st.type
            else:
                inferred = infer_ir(value, env.type_env, env.types, env.services)
                if inferred is not None:
                    env.type_env[safe] = inferred
            stmts.append({"step": "let", "name": safe, "value": value,
                          "mutable": bool(st.mutable)})
        tail = _lower_component_pure_expr(expr.tail, env, inner, callables, pure_only)
    finally:
        env.type_env = saved_tenv
    return {"kind": "do", "stmts": stmts, "tail": tail}


def _lower_component_pure_expr(expr, env: Env, scope: dict[str, str], callables: set,
                               pure_only: bool = False) -> dict:
    filename = env.filename
    line = getattr(expr, "line", 0)

    if isinstance(expr, ExprHole):
        return _lower_hole(expr, filename)

    if isinstance(expr, ExprEndorse):
        inner = _lower_component_pure_expr(expr.expr, env, scope, callables, pure_only)
        approval = _lower_endorse_approval(expr, env, scope, callables, pure_only)
        return _endorse_node(inner, expr, approval)

    # ADT construction (Result / user variants) — same tagged `adt` node as
    # the pure-fn path; Opt's Some/None stay untagged.
    cases = env.types.get(CASES_KEY) or {}
    if isinstance(expr, ExprCall) and isinstance(expr.callee, ExprVar) \
            and _tagged_case(expr.callee.name, env.types) is not None:
        info = cases[expr.callee.name]
        return {"kind": "adt", "type": info["adt"], "case": expr.callee.name,
                "args": [_lower_component_pure_expr(a, env, scope, callables, pure_only)
                         for a in expr.args]}
    if isinstance(expr, ExprVar):
        info = _tagged_case(expr.name, env.types)
        if info is not None and info.get("payload") is None \
                and not str(info.get("adt", "")).startswith(("Result", "Opt")):
            return {"kind": "adt", "type": info["adt"], "case": expr.name, "args": []}

    if isinstance(expr, EmitExpr):
        # the value of an irreversible call; the marker stays at the call site
        saved_mode = getattr(env, "_expr_mode", "setup")
        env._expr_mode = "emit"
        try:
            node = _lower_component_pure_expr(expr.expr, env, scope, callables, pure_only)
        finally:
            env._expr_mode = saved_mode
        if not _is_emission_call(node, env):
            raise RevlError(
                filename, expr.line,
                f"`emit` on {_node_desc(node)}, which is not declared `emission`",
                hint="only calls to `emission` service operations cross the boundary; "
                     "drop the `emit` marker (G4)",
                code="G4", category="emission",
            )
        return node
    if isinstance(expr, ExprMatch):
        # the ADT eliminator, available in component and method bodies too:
        # a component consuming a Result should not have to call out to a fn
        scrutinee = _lower_component_pure_expr(expr.scrutinee, env, scope,
                                               callables, pure_only)
        scrutinee_type = _expr_static_type(expr.scrutinee, env.type_env, env.types)
        _check_match_exhaustiveness(expr, env.type_env, env.types, filename)
        arms = []
        for pattern, bind, body in expr.arms:
            inner = dict(scope)
            payload_type = _arm_payload_type(scrutinee_type, pattern, env.types)
            if bind is not None:
                safe = _safe_name(bind, set(scope.values()))
                inner[bind] = safe
            arm = {
                "pattern": pattern,
                "bind": inner.get(bind) if bind is not None else None,
                "body": _lower_component_pure_expr(body, env, inner, callables, pure_only),
            }
            # the payload type a match arm binds, when the scrutinee's static
            # type is recoverable — the same key the pure-fn lowering
            # (`_lower_pure_expr`) writes, so a backend that must cast (java's
            # tagged Result) can do so in component/method bodies too
            if payload_type is not None:
                arm["payload_type"] = payload_type
            arms.append(arm)
        return {"kind": "match", "scrutinee": scrutinee, "arms": arms}
    if isinstance(expr, ExprLit):
        if expr.value is None:
            raise null_error(filename, line)
        return {"kind": "lit", "value": _str_literal_value(expr.value)}
    if isinstance(expr, Interp):
        return _lower_expr(expr, env, mode=getattr(env, "_expr_mode", "setup"))
    if isinstance(expr, ExprVar):
        name = expr.name
        if name in scope:
            return {"kind": "name", "id": scope[name]}
        info = _tagged_case(name, env.types)
        if info is not None and info.get("payload") is None \
                and not str(info.get("adt", "")).startswith(("Result", "Opt")):
            return {"kind": "adt", "type": info["adt"], "case": name, "args": []}
        if name in callables:
            return {"kind": "var", "name": name}
        if getattr(env, "_plain_body", False):
            declared = ", ".join(f"`{r}`" for r in env.requires) or "<nothing>"
            raise RevlError(
                filename, line,
                f"`{name}` is not a declared requirement of {env.component.name}",
                hint=f"component {env.component.name} requires {declared} — "
                     f"add `requires {name}: <Service>`?",
            )
        _reject_foreign_name(name, filename, line)  # item 384
        raise RevlError(filename, line,
                        f"`{name}` is not declared in this component effect block",
                        hint="declare it with `let` in the effect block, or use a "
                             "requirement/config field (G1)")
    if isinstance(expr, ExprField):
        if isinstance(expr.target, ExprVar) and expr.target.name == "config":
            if expr.name not in env.config_fields:
                raise RevlError(filename, line,
                                f"`{expr.name}` is not a config field of {env.component.name}")
            return {"kind": "config", "field": expr.name}
        lowered_target = _lower_component_pure_expr(expr.target, env, scope, callables,
                                                    pure_only)
        # `s.<key>` on a spawn handle is a provision read, not a record field
        # (docs/design-v2-instances.md). Only a name this component bound to a
        # handle carries `Instance[C]`, so it stays on the supervision tree.
        inst = _instance_handle_component(lowered_target, env)
        if inst is not None:
            return _lower_instance_get(lowered_target, expr.name, line, inst, env)
        target_type = infer_ir(lowered_target, env.type_env, env.types, env.services)
        # item 392: the provide-method / component-body twin of the item 380(2)
        # refusal in `infer_ast`. A field read off a value whose static type is
        # `Any`/`Value` (the erased-dynamic types — a `json_parse` result) is the
        # 279/299 silent-divergence class: py raises `KeyError` on an absent key,
        # ts yields `undefined`, and neither is a defensible total answer for a
        # field the author declared present. `infer_ast` (stratum 1 — fn/test/
        # module-fn bodies) already refuses it, and the component-setup sweep
        # reaches the same refusal in `infer_ir`; but a `provide` method body and
        # a component pure-expression position lower through here WITHOUT a
        # filename-carrying sweep, so the same expression compiled clean — the
        # same context-scoping gap as the `.length`-in-provide-method case marked
        # just below. Refuse it here with the identical diagnostic so the
        # divergence is a compile error on every tier, wherever the read sits.
        _thead, _ = parse_type(target_type)
        if filename and _thead in ("Any", "Value"):
            raise RevlError(
                filename, line,
                f"field read `.{expr.name}` on a value of type "
                f"`{render_type(target_type)}` — an erased value has no known fields",
                hint=("bind it to a record type first "
                      f"(`let e: SomeRecord = …; e.{expr.name}` — an `Opt[T]` "
                      "field then reads back the empty Opt on absence), or walk "
                      "it with stdlib/value.rvl (`value_is_object(v)`, "
                      f"`value_opt(v, \"{expr.name}\")`, `value_field_or`)"),
                code="T1", category="type-mismatch")
        node = {"kind": "field", "target": lowered_target, "name": expr.name}
        # item 104 (cross-tier): the property form `.length` in a COMPONENT
        # position stays a `field` node (the fn-body form is a `len` node — that
        # split is deliberate). But `.length` on a sized value (Str/Bytes/List)
        # is the code-point/element count, not a record slot: each tier's field
        # emitter reads a record field by `getattr`/member, which raises on a
        # `Str`. The component emitters carry no static type at the field site
        # (unlike the typed wasm/rust emitters), so the frontend marks the node
        # here — with the SAME `_is_sized_type` check — and the field handlers
        # honour the mark, rendering the code-point path. Gated on a sized type,
        # so a record whose field is literally named `length` still reads its
        # slot.
        if expr.name == "length" and _is_sized_type(target_type):
            node["sized_length"] = True
        # item 380: an `Opt[T]`-declared field reads TOTAL on every tier.
        elif _field_is_opt(target_type, expr.name, env.types):
            node["opt"] = True
        return node
    if isinstance(expr, ExprCall):
        args = [_lower_component_pure_expr(a, env, scope, callables, pure_only)
                for a in expr.args]
        if isinstance(expr.callee, ExprField) and isinstance(expr.callee.target, ExprVar):
            root = expr.callee.target.name
            method = expr.callee.name
            if root in _HOST_CALLABLES:
                # the Map VALUE constructor (docs/stdlib-2.0.md §Map) — a
                # pure value, not an effect; intercepted before the host
                # builtin check (`empty` is not a host verb).
                if root == "Map" and method == "empty":
                    if args:
                        raise RevlError(
                            filename, line,
                            f"`Map.empty()` takes no arguments, {len(args)} given",
                            hint="build up an empty map with `set`: "
                                 "`Map.empty().set(\"k\", v)`",
                        )
                    return {"kind": "maplit", "entries": []}
                if pure_only:
                    raise RevlError(
                        filename, line,
                        f"host builtin `{root}.{method}` is an effect and cannot appear "
                        "in pure setup",
                        hint="move the acquisition to the final expression of the effect block (G6)",
                    )
                host_check(f"{root}.{method}",
                           [infer_ir(a, env.type_env, env.types, env.services)
                            for a in args],
                           filename, line)
                return {"kind": "host", "fn": f"{root}.{method}", "args": args}
            # host provenance (docs/stdlib-2.0.md §Map): a local bound to a
            # host acquisition keeps its stub verb surface verbatim — checked
            # BEFORE the builtin table so the sanctioned `remove` overlap
            # dispatches by receiver kind, never by name alone. The verb is
            # checked against the acquisition's family surface (item 401): an
            # unknown verb (`store.frobnicate(k)`) is refused here (HOST-METHOD)
            # instead of compiling as a pass-through that only crashes at the
            # host runtime, the item-84 shape.
            if scope.get(root) in env.host_locals:
                _check_host_verb(
                    env.host_locals[scope[root]], method, len(args),
                    filename, line)
                return {"kind": "call",
                        "target": {"kind": "name", "id": scope[root]},
                        "method": method, "args": args}
            if root in env.requires:
                if pure_only:
                    raise RevlError(
                        filename, line,
                        f"call to required service `{root}.{method}` is an effect and cannot "
                        "appear in pure setup",
                        hint="service calls must be acquired with `effect ... undo ...` or "
                             "marked `emit` (G4/G6)",
                    )
                return _component_req_call(env, root, method, args, line)
            if method in _BUILTIN_METHODS:
                # The stdlib-named-method sliver (roadmap 75(b)), same rule as
                # a `fn` body: only a receiver the checker can *prove* is a
                # stdlib value may take the builtin table. A local of unknown
                # type (a host-object result such as `let v = store.get(k)`)
                # used to lower a stdlib-named method as that builtin and
                # misdispatch at runtime; refuse it (HOST-METHOD). Undeclared
                # roots fall through and are caught by G1 name resolution.
                if root in scope:
                    recv_t = infer_ir({"kind": "name", "id": scope[root]},
                                      env.type_env, env.types, env.services)
                    if _is_wildcard(recv_t):
                        _refuse_unpinned_stdlib_method(method, recv_t,
                                                       filename, line)
                if len(args) != _BUILTIN_METHODS[method]:
                    raise RevlError(filename, line,
                                    f"builtin `{method}` takes {_BUILTIN_METHODS[method]} "
                                    f"argument(s), {len(args)} given")
                node: dict = {"kind": "builtin", "method": method,
                              "target": _lower_component_pure_expr(expr.callee.target, env, scope,
                                                                   callables, pure_only),
                              "args": args}
                if method == "to_int":
                    # receiver-family dispatch (`recv`), as in a fn body
                    node["recv"] = infer_ir({"kind": "name", "id": scope[root]},
                                            env.type_env, env.types, env.services)
                return node
            if root in scope:
                # A method on a local that is a *known* stdlib-bearing value
                # (Str/List/Bytes) must be a builtin — builtins were already
                # handled above, so a non-builtin here is a typo/misuse, not a
                # host method. Receivers of unknown/host type infer to None and
                # stay lenient (host provenance is exempt — docs/stdlib-2.0.md).
                recv_t = infer_ir({"kind": "name", "id": scope[root]},
                                  env.type_env, env.types, env.services)
                if parse_type(recv_t)[0] in _SIZED_HEADS:
                    raise RevlError(
                        filename, line,
                        f"no builtin method `{method}` on `{recv_t}` — the stdlib surface is "
                        f"{', '.join(sorted(_BUILTIN_METHODS))} (docs/stdlib-2.0.md)",
                        hint="records carry data, not methods; call functions as `f(x)` (G6)",
                    )
                return {"kind": "call",
                        "target": {"kind": "name", "id": scope[root]},
                        "method": method, "args": args}
        if isinstance(expr.callee, ExprVar):
            name = expr.callee.name
            # ADT/Opt construction lowers exactly as it does in a `fn` body:
            # tagged variants to an `adt` node, Some/None to identity/null.
            # Emitting a plain call here would name a constructor the host
            # runtimes do not define (they are not functions).
            if _tagged_case(name, env.types) is not None:
                info = (env.types.get(CASES_KEY) or {})[name]
                return {"kind": "adt", "type": info["adt"], "case": name, "args": args}
            if name in _BUILTIN_CONSTRUCTORS:
                return {"kind": "call", "callee": {"kind": "var", "name": name},
                        "args": args}
            if name in callables:
                # item 187: fill omitted trailing arguments with their default
                # expressions before async coercion, so the module-fn call the
                # emitters see is fully supplied (no per-tier default handling).
                filled = _with_default_args(
                    name, args, env.types,
                    lambda d: _lower_component_pure_expr(d, env, scope,
                                                         callables, pure_only))
                return {"kind": "fn", "name": name,
                        "args": _coerce_async_args(name, filled, env, line)}
        if isinstance(expr.callee, ExprField) and expr.callee.name in _BUILTIN_METHODS:
            method = expr.callee.name
            # the same sliver guard as the var-root path above: a non-var
            # receiver of unknown type (e.g. `open_pool("pg://").remove(k)`)
            # must not lower as the builtin either
            target = _lower_component_pure_expr(expr.callee.target, env, scope,
                                                callables, pure_only)
            recv_t = infer_ir(target, env.type_env, env.types, env.services)
            if _is_wildcard(recv_t):
                _refuse_unpinned_stdlib_method(method, recv_t, filename, line)
            if len(args) != _BUILTIN_METHODS[method]:
                raise RevlError(filename, line,
                                f"builtin `{method}` takes {_BUILTIN_METHODS[method]} "
                                f"argument(s), {len(args)} given")
            node: dict = {"kind": "builtin", "method": method, "target": target,
                          "args": args}
            if method == "to_int":
                # receiver-family dispatch (`recv`), as in a fn body
                node["recv"] = infer_ast(expr.callee.target, env.type_env,
                                         env.types, None)
            return node
        callee_node = _lower_component_pure_expr(expr.callee, env, scope, callables,
                                                 pure_only)
        # a provision method call off a spawn handle (`s.<key>.<method>(...)`)
        # crosses the boundary exactly as a `req` emission does, so the same
        # `emit`-marker discipline applies — an unmarked emission is refused
        # (G4), not silently lowered. The `req` path enforces this inline
        # (`_component_req_call`); the handle path reaches the generic
        # fall-through, so the check lives here.
        inst = _instance_get_call({"kind": "call", "callee": callee_node,
                                   "args": args}, env)
        if inst is not None:
            recv, decl = inst
            if decl.emission and getattr(env, "_expr_mode", "setup") == "setup":
                handle = recv.get("target") if isinstance(recv.get("target"), dict) else {}
                spelled = ".".join(p for p in (handle.get("id"), recv.get("key"),
                                               callee_node.get("name")) if p)
                raise RevlError(
                    filename, line,
                    f"call to emission `{spelled}` must be marked `emit` (G4)",
                    hint="an emission crosses the system boundary and cannot be reverted; "
                         "`emit` makes that visible at the call site",
                    code="G4", category="emission",
                )
        return {"kind": "call", "callee": callee_node, "args": args}
    if isinstance(expr, ExprBin):
        return {"kind": "bin", "op": expr.op,
                "left": _lower_component_pure_expr(expr.left, env, scope, callables,
                                                   pure_only),
                "right": _lower_component_pure_expr(expr.right, env, scope, callables,
                                                    pure_only)}
    if isinstance(expr, ExprUn):
        return {"kind": "un", "op": expr.op,
                "operand": _lower_component_pure_expr(expr.operand, env, scope, callables,
                                                      pure_only)}
    if isinstance(expr, ExprIndex):
        return {"kind": "index",
                "target": _lower_component_pure_expr(expr.target, env, scope, callables,
                                                     pure_only),
                "index": _lower_component_pure_expr(expr.index, env, scope, callables,
                                                    pure_only)}
    if isinstance(expr, ExprIf):
        return {"kind": "if",
                "cond": _lower_component_pure_expr(expr.cond, env, scope, callables,
                                                   pure_only),
                "then": _lower_component_pure_expr(expr.then, env, scope, callables,
                                                   pure_only),
                "else": _lower_component_pure_expr(expr.otherwise, env, scope, callables,
                                                   pure_only)}
    if isinstance(expr, ExprRecord):
        return {"kind": "record",
                "fields": [[name, _lower_component_pure_expr(e, env, scope, callables,
                                                             pure_only)]
                           for name, e in expr.fields]}
    if isinstance(expr, ExprRecordUpdate):
        return {"kind": "record_update",
                "base": _lower_component_pure_expr(expr.base, env, scope, callables,
                                                   pure_only),
                "updates": [[name, _lower_component_pure_expr(e, env, scope, callables,
                                                              pure_only)]
                            for name, e in expr.updates]}
    if isinstance(expr, ExprBlockArm):
        return _lower_component_block_arm(expr, env, scope, callables, pure_only)
    if isinstance(expr, ExprList):
        return {"kind": "list",
                "items": [_lower_component_pure_expr(e, env, scope, callables, pure_only)
                          for e in expr.items]}
    if isinstance(expr, ExprArrow):
        # Arrow parameters bind in the arrow's body scope — including inside
        # provide-method bodies (roadmap 77a / FR-1): the pure-helper +
        # callback-arrow escape depends on `msgs2 => emit model.complete(msgs2)`
        # resolving `msgs2` to the arrow parameter, not misreading it as a
        # missing component requirement. Same shape as the pure-fn path below:
        # params shadow the enclosing scope; free vars captured from it are
        # snapshotted by value (unchanged).
        inner = dict(scope)
        param_types = _arrow_param_types(expr)
        for param, ptype in zip(expr.params, param_types):
            # the component path resolves names through `id` (unlike the
            # pure-fn path's raw `var`), so the arrow parameter maps to its
            # own name — the emitted lambda binds params positionally, and a
            # `name` node for the parameter must render to exactly that.
            inner[param] = param
            if ptype:
                env.type_env[param] = ptype
            else:
                env.type_env.pop(param, None)
        captures = sorted(_mutable_free_vars(expr.body, scope, set(expr.params)))
        _b1_capture_check(expr, env.type_env, env.types, filename, expr.line)
        node = {"kind": "arrow", "params": expr.params, "captures": captures,
                "body": _lower_component_pure_expr(expr.body, env, inner, callables,
                                                   pure_only)}
        if any(p is not None for p in param_types) or expr.returns:
            node["param_types"] = param_types
            node["returns"] = expr.returns
        return node
    if isinstance(expr, ExprOptField):
        return {
            "kind": "optfield",
            "target": _lower_component_pure_expr(expr.target, env, scope, callables, pure_only),
            "name": expr.name,
        }
    if isinstance(expr, ExprOptCall):
        return {
            "kind": "optcall",
            "target": _lower_component_pure_expr(expr.target, env, scope, callables, pure_only),
            "method": expr.method,
            "args": [_lower_component_pure_expr(a, env, scope, callables, pure_only) for a in expr.args],
        }
    raise RevlError(filename, line, "unsupported expression in component effect block",
                    hint="block-effect setup is stratum-1 pure code (G6)")


def _lower_component_setup_stmt(stmt, env: Env, scope: dict[str, str], callables: set,
                                mutables: set[str], out: list) -> None:
    filename = env.filename

    def _sweep(node, line):
        # Run the raising type oracle over a lowered setup node (HOLE 3(c) /
        # HOLE 2): definite operator/builtin misuse in stratum-1 setup code now
        # raises, exactly as it does in a `fn` body. Unknown/host operands infer
        # to None and are left alone. Returns the inferred type (or None).
        return infer_ir(node, env.type_env, env.types, env.services, filename, line)

    if isinstance(stmt, LetStmt):
        safe = env.bind_local(stmt.name, stmt.line)
        scope[stmt.name] = safe
        if stmt.mutable:
            mutables.add(stmt.name)
        value = _lower_component_pure_expr(stmt.value, env, scope, callables,
                                           pure_only=True)
        inferred = _sweep(value, stmt.line)
        if inferred is not None:
            env.type_env[safe] = inferred
        out.append({"step": "let", "name": safe, "value": value})
    elif isinstance(stmt, AssignStmt):
        if stmt.name not in scope:
            _reject_foreign_name(stmt.name, filename, stmt.line)  # item 384
            raise RevlError(filename, stmt.line,
                            f"`{stmt.name}` is not declared in this effect block",
                            hint="declare it with `let`/`var` first (G1)")
        if stmt.name not in mutables:
            raise RevlError(filename, stmt.line,
                            f"cannot reassign `{stmt.name}` — it is `let` (single-assignment)",
                            hint="declare it with `var` to make it mutable (syntax-2.0 §3.5)")
        value = _lower_component_pure_expr(stmt.value, env, scope, callables,
                                           pure_only=True)
        _sweep(value, stmt.line)
        out.append({"step": "assign", "name": scope[stmt.name], "value": value})
    elif isinstance(stmt, ExprStmt):
        expr = _lower_component_pure_expr(stmt.expr, env, scope, callables,
                                          pure_only=True)
        _sweep(expr, stmt.line)
        out.append({"step": "expr", "expr": expr})
    elif isinstance(stmt, IfStmt):
        cond = _lower_component_pure_expr(stmt.cond, env, scope, callables,
                                          pure_only=True)
        cond_t = _sweep(cond, stmt.line)
        if cond_t is not None and cond_t != "Bool":
            raise mismatch(filename, stmt.line, "`if` condition", "Bool", cond_t)
        then: list[dict] = []
        for s in stmt.then:
            _lower_component_setup_stmt(s, env, scope, callables, mutables, then)
        otherwise = None
        if stmt.otherwise is not None:
            otherwise = []
            for s in stmt.otherwise:
                _lower_component_setup_stmt(s, env, scope, callables, mutables, otherwise)
        out.append({"step": "if", "cond": cond, "then": then})
        if otherwise is not None:
            out[-1]["else"] = otherwise
    elif isinstance(stmt, AssertStmt):
        expr = _lower_component_pure_expr(stmt.expr, env, scope, callables,
                                          pure_only=True)
        assert_t = _sweep(expr, stmt.line)
        if assert_t is not None and assert_t != "Bool":
            raise mismatch(filename, stmt.line, "`assert`", "Bool", assert_t)
        out.append({"step": "assert", "expr": expr})
    else:
        raise RevlError(filename, getattr(stmt, "line", 0),
                        "unsupported statement in effect block setup",
                        hint="effect block setup is stratum-1 pure code: "
                             "`let`/`var`/`if`/pure calls are allowed (G6)")


def _lower_component_guard_stmts(stmts: list, env: Env, callables: set) -> list[dict]:
    out: list[dict] = []
    for stmt in stmts:
        if isinstance(stmt, FailStmt):
            out.append({
                "step": "fail",
                "message": _lower_component_pure_expr(
                    stmt.message, env, _component_scope(env), callables,
                    pure_only=True,
                ),
            })
        elif isinstance(stmt, IfStmt):
            out.append(_lower_component_if(stmt, env, callables))
        else:
            raise RevlError(env.filename, getattr(stmt, "line", 0),
                            "only `fail` (and nested `if` guards) may appear in a component guard",
                            hint="component `if` is for deliberate L-Raise decisions, not general "
                                 "control flow (G6)")
    return out


def _lower_component_if(stmt: IfStmt, env: Env, callables: set) -> dict:
    scope = _component_scope(env)
    # a guard condition is a `Bool` position, so a hole there is a `Bool`
    # obligation rather than an untyped one (docs/holes.md)
    pin_hole(stmt.cond, "Bool", env.filename, "`if` guard condition")
    step = {
        "step": "if",
        "cond": _lower_component_pure_expr(stmt.cond, env, scope, callables,
                                           pure_only=True),
        "then": _lower_component_guard_stmts(stmt.then, env, callables),
    }
    if stmt.otherwise is not None:
        step["else"] = _lower_component_guard_stmts(stmt.otherwise, env, callables)
    return step


def _config_default_type(value) -> str | None:
    """Surface type of a config-default literal (bool before int: bool is an
    int subclass)."""
    if isinstance(value, bool):
        return "Bool"
    if isinstance(value, int):
        return "Int"
    if isinstance(value, float):
        return "Float"
    if isinstance(value, str):
        return "Str"
    return None


# Emission reachability, capability sets, and the G4 evidence trail live in
# `emission_analysis.py` (roadmap item 17). Re-exported here so `revl.lower`
# keeps its public import surface: the spine, `query.py`, `__main__.py`, and
# the capability tests all import these names from `revl.lower`.
from .emission_analysis import (  # noqa: E402,F401
    _EmissionEvidence,
    _async_callables,
    _is_async_fn_type,
    _calls_in,
    _capability_hint,
    _emission_chain,
    _emitting_capabilities,
    _emitting_fns,
    _method_emissions,
    _witness_depth,
)


def _async_reached_outside_provide(node, async_names: set, acc: set) -> None:
    """Async extern names a lowered component body reaches, *excluding* the
    positions that carry their own in-flight window or admission gate. Mirrors
    `_calls_in`'s call/value detection but prunes provide steps, timer steps,
    and — since item 131 — the `effect`/`let-effect`/`emit`/`await` steps.

    A `timer` body (item 57) reaching an async op is no longer refused here: a
    timer is a spawned in-flight handle (item 106's `Async[T]` window), so it is
    coloured async in `_lower_timer_step` and its firing is awaited/cancelled by
    the runtime, exactly like a provide method's own async reach is admitted at
    its site above (docs/time-coeffect.md §async).

    Item 131: the `await` step is pruned (its suspension is admitted, widened to
    async externs/colored fns), and an AWAIT-MARKED effect/emit step is pruned
    (an awaited async acquisition/emission is admitted). A NON-await effect/emit
    step is still walked, so an async-callable reached without `await` keeps this
    legacy "reaches async … in a setup/activation body" refusal — the message
    the self-hosted admission gate mirrors. The req-op family that gate is blind
    to is caught instead by `_admit_effect_async`/`_admit_emit_async` (rule 1),
    which run during lowering and set the `async` flag this prune reads."""
    if isinstance(node, dict):
        step = node.get("step")
        if step in ("provide", "timer", "await"):
            return
        if step in ("effect", "let-effect", "emit") and node.get("async"):
            return
        kind = node.get("kind")
        if kind == "fn" and node.get("name") in async_names:
            acc.add(node["name"])
        callee = node.get("callee")
        if kind == "call" and isinstance(callee, dict) \
                and callee.get("kind") == "var" and callee.get("name") in async_names:
            acc.add(callee["name"])
        if kind == "var" and node.get("name") in async_names:
            acc.add(node["name"])
        for value in node.values():
            _async_reached_outside_provide(value, async_names, acc)
    elif isinstance(node, list):
        for value in node:
            _async_reached_outside_provide(value, async_names, acc)


def _req_op_is_async(node, env) -> bool:
    """A lowered call/emit node that reaches an async service operation through
    a required key — rule 3 of the item-92 async-reach (a suspension source the
    name-based `_calls_in` is blind to)."""
    if not isinstance(node, dict):
        return False
    target = node.get("target")
    method = node.get("method")
    if isinstance(target, dict) and target.get("kind") == "req" and method:
        svc = env.services.get(env.requires.get(target.get("name")) or "")
        decl = svc.methods.get(method) if svc is not None else None
        return bool(decl is not None and getattr(decl, "async_", False))
    return False


def _reached_async_req_ops(node, env, acc: list) -> None:
    """Collect req-target async service ops (`req.method` whose op is `async
    fn`) a lowered method body reaches DIRECTLY — in statement OR expression
    position (a ternary arm, a nested expression), the blind spot item 117
    closes. The name-based `_calls_in` the A1 provide-method check runs sees
    async *callables* (externs, colored fns) but never an async *service
    operation* reached through a required key (rule 3 of the item-92 async-
    reach), so a sync method whose ternary returns `emit m.op(x)` slipped
    through as admitted (finding #40).

    Any nested arrow is pruned: an arrow value's async-reach is the concern of
    `_refuse_leaky_arrow`/`_refuse_leaky_pure_arrow` (finding #21), which raise
    the arrow-color diagnostic instead — this walk owns only the ops the method
    body reaches without an intervening arrow. Each hit is
    `(req_name, method, op_decl)`."""
    if isinstance(node, dict):
        if node.get("kind") == "arrow":
            return
        if _req_op_is_async(node, env):
            target = node.get("target")
            method = node.get("method")
            svc = env.services.get(env.requires.get(target.get("name")) or "")
            op_decl = svc.methods.get(method) if svc is not None else None
            acc.append((target.get("name"), method, op_decl))
        for value in node.values():
            _reached_async_req_ops(value, env, acc)
    elif isinstance(node, list):
        for value in node:
            _reached_async_req_ops(value, env, acc)


def _first_suspension(node, env) -> str | None:
    """The first suspension source a lowered ACTIVATION-body expression reaches,
    named for a diagnostic, or None (item 131). A suspension source is either a
    req-target `async fn` service operation (rule 3 of the item-92 async-reach,
    the blind spot the name-based walk misses) or an async-colored callable — an
    `async` extern or a phase-2 colored fn (rule 1). Nested arrows are pruned:
    an arrow value's color is its own concern, refused by `_refuse_leaky_arrow`.

    This is the shared predicate behind the four admission rules of item 131 §3.
    It looks past the name-based `_async_reached_outside_provide` walk precisely
    where that walk is blind — a req-target async op — closing the two silent
    `effect`/`emit` leaks and the two silent teardown leaks the probe table
    named."""
    reached: list = []
    _reached_async_req_ops(node, env, reached)
    if reached:
        req_name, method, _op = reached[0]
        return f"{req_name}.{method}"
    called: set = set()
    _calls_in(node, called, stop_async_arrows=True)
    hit = sorted(called & (getattr(env, "async_callables", None) or set()))
    if hit:
        return hit[0]
    return None


def _first_reqop_suspension(node, env) -> str | None:
    """The first REQ-TARGET async service op a lowered expression reaches (rule
    3 of the item-92 async-reach), or None. This is the family the name-based
    `_async_reached_outside_provide` fence is blind to — the two silent leaks of
    item 131's probe table. An async-colored callable (an async extern or a
    phase-2 colored fn) is deliberately NOT reported here: that family is still
    refused by the name-based fence with its legacy "reaches async … in a
    setup/activation body" diagnostic (which the self-hosted gate mirrors), so
    the effect-composition rule-1 refusal owns only the req-op family it is the
    first to name."""
    reached: list = []
    _reached_async_req_ops(node, env, reached)
    if reached:
        req_name, method, _op = reached[0]
        return f"{req_name}.{method}"
    return None


def _admit_effect_async(stmt, step: dict, env: "Env", filename: str) -> None:
    """Enforce the exact `await`/async pairing on an `effect`/`let-effect`
    acquisition and refuse a suspending teardown (item 131 §3, rules 1-3).

    The acquisition (and any pure `setup`) is a FORWARD-path position: it may
    reach a suspension iff the surface spelled `effect await`. The `undo` is a
    TEARDOWN position: it may never reach one, because the two-phase abort loop
    is synchronous on every tier (docs/design/teardown-contract.md). Sets the
    additive IR `async` flag on the step whose surface carried `await`; a sync
    acquisition carries no key and lowers byte-identically to before."""
    is_async = getattr(stmt, "is_async", False)
    # Rule 1 (async without `await`) fires here only for the REQ-OP family — the
    # silent leak. The async-callable family (extern / colored fn) in a non-await
    # acquisition is still refused by the name-based `_async_reached_outside_
    # provide` fence with its legacy message, which the self-hosted gate mirrors.
    reqop = _first_reqop_suspension(step.get("acquire"), env)
    if reqop is None and step.get("setup"):
        reqop = _first_reqop_suspension(step["setup"], env)
    # Rule 2 (await without async) reads the FULL suspension surface (either
    # family): an `await` marker must name a real divert window of any kind.
    reach = _first_suspension(step.get("acquire"), env)
    if reach is None and step.get("setup"):
        reach = _first_suspension(step["setup"], env)
    if reqop is not None and not is_async:
        # Rule 1: async without `await`.
        raise RevlError(
            filename, stmt.line,
            f"component `{env.component.name}` acquires through async operation "
            f"`{reqop}` but the effect is not awaited; the binding would hold "
            f"the in-flight value, not the result (A1)",
            hint=f"write `effect await {reqop.split('.')[-1]}(...) undo ...`; the "
                 "await is a divert boundary (paper §4.3.2), so it is spelled, "
                 "never inserted",
            code="A1", category="async-propagation")
    if is_async and reach is None:
        # Rule 2: `await` without async — the marker must name a real divert
        # window, never decoration (exact pairing, both directions).
        raise RevlError(
            filename, stmt.line,
            "`effect await` on an acquisition that reaches nothing async — an "
            "`await` in an activation body is a real divert window (A1)",
            hint="nothing here suspends; drop `await` and write `effect <expr> "
                 "undo ...`",
            code="A1", category="async-propagation")
    if is_async:
        step["async"] = True
    undo_reach = _first_suspension(step.get("undo"), env)
    if undo_reach is not None:
        # Rule 3: teardown never suspends.
        raise RevlError(
            filename, stmt.line,
            f"`undo` reaches async operation `{undo_reach}`, but teardown is "
            "synchronous on every tier — a suspension there would be a teardown "
            "that can hang or silently no-op (A1)",
            hint="the two-phase abort loop does not await "
                 "(docs/design/teardown-contract.md, the bound rule); keep the "
                 "inverse a synchronous call",
            code="A1", category="async-propagation")


def _admit_emit_async(stmt, step: dict, env: "Env", filename: str) -> None:
    """Enforce the `await`/async pairing on an `emit` step and refuse a
    suspending `compensate` (item 131 §3, rules 1-3). The emission expression is
    forward-path (awaited iff the surface spelled `await emit`); the
    `compensate` slot is teardown-position and may never suspend."""
    is_async = getattr(stmt, "is_async", False)
    # Rule 1 fires here only for the REQ-OP family (the silent leak); an
    # async-callable emission in a non-await step stays with the legacy fence.
    reqop = _first_reqop_suspension(step.get("expr"), env)
    reach = _first_suspension(step.get("expr"), env)
    if reqop is not None and not is_async:
        # Rule 1: async without `await`.
        raise RevlError(
            filename, stmt.line,
            f"`emit` step reaches async operation `{reqop}` but the emission is "
            "not awaited; py would build a coroutine it never awaits (the "
            "emission never fires) and ts a floating unordered Promise (A1)",
            hint=f"write `await emit {reqop.split('.')[-1]}(...)`; the await is a "
                 "divert boundary (paper §4.3.2), so it is spelled, never inserted",
            code="A1", category="async-propagation")
    if is_async and reach is None:
        # Rule 2: `await` without async.
        raise RevlError(
            filename, stmt.line,
            "`await emit` on an emission that reaches nothing async — an `await` "
            "in an activation body is a real divert window (A1)",
            hint="nothing here suspends; drop `await` and write `emit <expr>`",
            code="A1", category="async-propagation")
    if is_async:
        step["async"] = True
    comp_reach = _first_suspension(step.get("compensate"), env)
    if comp_reach is not None:
        # Rule 3: teardown never suspends.
        raise RevlError(
            filename, stmt.line,
            f"`compensate` reaches async operation `{comp_reach}`, but teardown "
            "is synchronous on every tier — a suspension there would be a "
            "compensation that can hang or silently no-op (A1)",
            hint="the two-phase abort loop does not await "
                 "(docs/design/teardown-contract.md, the bound rule); keep the "
                 "compensation a synchronous call",
            code="A1", category="async-propagation")


def _arrow_reaches_async(body, env) -> bool:
    """True if a lowered arrow body reaches a suspension (item 92 §3): a
    req-target async service op (rule 3), or a call of an async-colored callable
    — an async extern or a phase-2 colored fn (rules 1). A nested async-flagged
    arrow is a *value* whose suspension is its own, so it is pruned."""
    hit = False

    def walk(n):
        nonlocal hit
        if hit:
            return
        if isinstance(n, dict):
            if n.get("kind") == "arrow" and n.get("async"):
                return
            if _req_op_is_async(n, env):
                hit = True
                return
            for value in n.values():
                walk(value)
        elif isinstance(n, list):
            for value in n:
                walk(value)

    walk(body)
    if hit:
        return True
    called: set = set()
    _calls_in(body, called, stop_async_arrows=True)
    return bool(called & (env.async_callables or set()))


def _refuse_leaky_arrow(node, env, source: str, line: int = 0) -> None:
    """Walk a lowered body for a *sync-typed* arrow that reaches an async
    operation — the item-92 leak (finding #21), now a compile error instead of
    an unawaited coroutine at runtime. An async-flagged arrow is admitted; a
    plain one that reaches a suspension is refused with the A1 diagnostic."""
    if isinstance(node, dict):
        if node.get("kind") == "arrow":
            if not node.get("async") and _arrow_reaches_async(node.get("body"), env):
                raise RevlError(
                    source or env.filename, node.get("line") or line or 0,
                    "this arrow reaches an async operation, but its type carries "
                    "no async color — the caller would receive an unawaited "
                    "suspension (A1)",
                    hint="declare the receiving parameter `(…) -> Async[T]` so "
                         "every call through it is awaited, or move the "
                         "suspending call out of the arrow "
                         "(docs/design/async-function-values.md)",
                    code="A1", category="async-propagation",
                )
            # its own body still walked below (a sync inner arrow may leak)
        for value in node.values():
            _refuse_leaky_arrow(value, env, source)
    elif isinstance(node, list):
        for value in node:
            _refuse_leaky_arrow(value, env, source)


def _coerce_async_args(callee_name, args, env, line):
    """At a component-body call to a module `fn`, admit an arrow into each
    parameter declared `(…) -> Async[T]` (item 92) by stamping the async color
    into the IR. The component path never runs `_check_arrow`, so this is where
    the color is placed; the pure-fn path gets it from `_resolve_arrow` instead.

    v1 admits only an *arrow* in an async slot (the harness's shape — a sync
    arrow is the accepted sync->async coercion, an async-bodied one is colored).
    A non-arrow value (a bare callable name, a fn-typed local) is refused: it
    would need an `as_async` wrapper the blocking backends do not yet erase —
    a filed follow-up, refused here rather than leaked."""
    sig = (env.types.get(FNS_KEY) or {}).get(callee_name)
    if not sig:
        return args
    params = sig.get("params") or []
    out = list(args)
    for i, ptype in enumerate(params):
        if i >= len(out) or not _is_async_fn_type(ptype):
            continue
        arg = out[i]
        if isinstance(arg, dict) and arg.get("kind") == "arrow":
            arg["async"] = True
            if not arg.get("returns"):
                arg["returns"] = render_type(parse_type(ptype)[1][-1])
        else:
            raise RevlError(
                env.filename, line,
                f"argument {i + 1} of `{callee_name}(...)` is declared "
                f"`{render_type(ptype)}` (async), but only an arrow may be "
                "passed into an async parameter in v1",
                hint="wrap it in an arrow, e.g. `x => f(x)`, so the emitter can "
                     "place the async boundary "
                     "(docs/design/async-function-values.md)",
                code="A1", category="async-propagation",
            )
    return out


def _sync_monomorph_name(origin: str, env) -> str:
    """A collision-free identifier for `origin`'s sync clone (item 342). Every
    call site for the same origin computes the same name: it depends only on the
    stable `env.callables` and the shared, converging `env.sync_monomorphs`."""
    name = f"{origin}_revl_sync"
    while name in (env.callables or set()) or (
            name in env.sync_monomorphs and env.sync_monomorphs[name] != origin):
        name += "_"
    return name


def _monomorph_callee(node):
    """The colour-polymorphic callee name of a lowered call, and a setter that
    rewrites it, for BOTH lowered call shapes: a component body's `{kind: fn,
    name}` and a module-`fn`/`test` body's `{kind: call, callee: {kind: var,
    name}}` (item 387 — 342 originally saw only the former). Returns
    `(origin, set_name)` or `(None, None)`."""
    if node.get("kind") == "fn" and isinstance(node.get("name"), str):
        def _set(m, _n=node):
            _n["name"] = m
        return node["name"], _set
    if node.get("kind") == "call":
        callee = node.get("callee")
        if isinstance(callee, dict) and callee.get("kind") == "var" \
                and isinstance(callee.get("name"), str):
            def _set(m, _c=callee):
                _c["name"] = m
            return callee["name"], _set
    return None, None


def _monomorphize_sync_callback_calls(node, env) -> None:
    """Sync/async arrow polymorphism at a sync call site (item 342 + item 387).

    A sync context (a provide method — item 342; or a module `fn`/`test` body —
    item 387) that calls a colour-polymorphic fn (one async solely by its own
    callback parameter) with a genuinely-sync arrow does not suspend: the arrow
    trivially lifts into a completed async. Instead of forcing the context async
    (A1) or authoring a duplicate sync loop, the call is redirected to a SYNC
    monomorph of the fn — `async` dropped, the callback de-async'd — and the
    arrow is un-stamped back to a plain sync value. `env.sync_monomorphs` records
    the request; the clone is synthesized once, after every call site is seen.

    Only fires when EVERY async-typed-param argument is a genuinely-sync arrow.
    If any such arrow reaches a real suspension, the call is left untouched: the
    A1 admission then refuses it, because a sync context truly cannot await it."""
    if isinstance(node, dict):
        origin, set_name = _monomorph_callee(node)
        if origin is not None and origin in env.colour_polymorphic:
            sig = (env.types.get(FNS_KEY) or {}).get(origin) or {}
            params = sig.get("params") or []
            args = node.get("args") or []
            async_arrows: list = []
            liftable = True
            for i, ptype in enumerate(params):
                if not _is_async_fn_type(ptype):
                    continue
                arg = args[i] if i < len(args) else None
                if (isinstance(arg, dict) and arg.get("kind") == "arrow"
                        and not _arrow_reaches_async(arg.get("body"), env)):
                    async_arrows.append((arg, ptype))
                else:
                    # a genuinely-async arrow (or a non-arrow) — not liftable;
                    # leave the async loop in place for the A1 admission to judge
                    liftable = False
                    break
            if liftable and async_arrows:
                mono = _sync_monomorph_name(origin, env)
                env.sync_monomorphs[mono] = origin
                set_name(mono)
                for arg, ptype in async_arrows:
                    arg.pop("async", None)
                    inner = parse_type(ptype)[1][-1]          # Async[T]
                    unwrapped = parse_type(inner)[1]
                    arg["returns"] = render_type(unwrapped[0]) if unwrapped else None
        for value in node.values():
            _monomorphize_sync_callback_calls(value, env)
    elif isinstance(node, list):
        for value in node:
            _monomorphize_sync_callback_calls(value, env)


def _resolve_poly_extern_calls(node, env, is_async: bool) -> None:
    """Caller-decided extern colour at a call site (item 388, stage 3 — the
    EXTERN analog of `_monomorphize_sync_callback_calls`).

    A poly extern (`fn|async`) is pre-seeded as the async form (its IR entry
    carries `async: True`, named `engine_run`). Walk a provide-method body and,
    at each call of a poly extern, record the enclosing method's colour into the
    shared `env.extern_colour_instances` map so the post-pass knows which clones
    to keep:

    - An ASYNC method's call resolves to the async clone (the pre-seeded entry,
      original name), left in place: it is in `async_externs`/`_PY_ASYNC_EXTERNS`,
      so the existing name-keyed await machinery awaits it, which A1 permits
      inside an async method.
    - A SYNC method's call is rewritten to the `_revl_sync` clone name and the
      sync colour recorded. The rewrite runs BEFORE the A1 admission (exactly
      where item 342's monomorph hook runs), so the sync call site has already
      cleared async membership — the sync clone is a concrete `def` not in the
      async set, so A1 never fires and no `await` lands in the sync function.

    Reuses `_monomorph_callee` for both lowered call shapes (`{kind: fn, name}`
    and `{kind: call, callee: {kind: var, name}}`)."""
    if isinstance(node, dict):
        origin, set_name = _monomorph_callee(node)
        if origin is not None and origin in env.poly_externs:
            inst = env.extern_colour_instances.setdefault(
                origin, {"sync": False, "async": False, "sync_name": None})
            if is_async:
                inst["async"] = True
            else:
                sync_name = inst["sync_name"] or _sync_monomorph_name(origin, env)
                inst["sync"] = True
                inst["sync_name"] = sync_name
                set_name(sync_name)
        for value in node.values():
            _resolve_poly_extern_calls(value, env, is_async)
    elif isinstance(node, list):
        for value in node:
            _resolve_poly_extern_calls(value, env, is_async)


def _finalize_poly_externs(externs: list, poly_names: set,
                           instances: dict, referenced: set) -> None:
    """Materialize and prune the concrete clones of every poly extern (item 388,
    stage 4 — the EXTERN analog of `_synthesize_sync_monomorphs`, plus the
    eager-expand-and-PRUNE resolution of the ordering wrinkle).

    Each poly extern was pre-seeded as one async IR entry (`colour_poly: True`).
    After every component has recorded which colours its call sites requested
    (`instances[name]`), rewrite that single entry into the concrete clones that
    are actually used, IN PLACE so emission order is deterministic:

    - async requested -> keep the pre-seeded entry as the async clone (original
      name, `async: True`);
    - sync requested  -> a deep-copied clone with `async` dropped and the
      `_revl_sync` name the sync call sites were rewritten to;
    - a colour NObody requested is PRUNED. A poly extern called in only one
      colour emits exactly one clone (a sync-only program emits no async clone
      and vice-versa); a poly extern nobody calls emits nothing at all (additive:
      parser, IR, and every golden stay byte-identical without a poly extern).

    The `colour_poly` marker is stripped from every surviving clone, so the final
    IR carries only ordinary concrete extern entries the emitters already
    understand."""
    import copy

    for name in poly_names:
        idx = next((i for i, e in enumerate(externs)
                    if e.get("name") == name and e.get("colour_poly")), None)
        if idx is None:
            continue
        preseed = externs[idx]
        preseed.pop("colour_poly", None)
        inst = instances.get(name) or {}
        replacement: list = []
        # keep the async clone if an async provide method requested it OR the
        # original (async) name still appears anywhere in the final IR — a call
        # from an async method, a module `fn`, or a `test` that was never
        # monomorphized to the sync clone (only sync PROVIDE methods rewrite their
        # calls). Without this second condition such a residual reference would
        # dangle after the async clone was pruned.
        if inst.get("async") or name in referenced:
            replacement.append(preseed)          # keep as the async clone
        if inst.get("sync"):
            clone = copy.deepcopy(preseed)
            clone["name"] = inst.get("sync_name") or f"{name}_revl_sync"
            clone.pop("async", None)             # the sync clone is a blocking def
            replacement.append(clone)
        externs[idx:idx + 1] = replacement       # prune the unused colour


def _synthesize_sync_monomorphs(fns: list, sync_monomorphs: dict) -> None:
    """Materialize the sync clones requested by item-342 call sites, appending
    each to `fns`. A clone is the origin fn with `async` dropped and every
    async-typed callback parameter de-async'd (`(A) -> Async[T]` -> `(A) -> T`),
    so both emitters render it as a plain sync fn awaiting nothing — the body is
    byte-identical, only the header and the callback's colour differ."""
    import copy

    by_name = {f["name"]: f for f in fns}
    for mono, origin in sorted(sync_monomorphs.items()):
        if mono in by_name:            # already synthesized (shared clone)
            continue
        src = by_name.get(origin)
        if src is None:                # origin vanished — nothing to clone
            continue
        clone = copy.deepcopy(src)
        clone["name"] = mono
        clone.pop("async", None)
        for p in clone.get("params") or []:
            if _is_async_fn_type(p.get("type")):
                p["type"] = _strip_async_fn_return(p["type"])
        fns.append(clone)
        by_name[mono] = clone


def _strip_async_fn_return(fn_type: str) -> str:
    """`(A, B) -> Async[T]` -> `(A, B) -> T`: the sync reading of an async
    callback type (item 342). A non-async fn type is returned unchanged."""
    head, args = parse_type(fn_type)
    if head != FN_HEAD or not args:
        return fn_type
    ret_head, ret_args = parse_type(args[-1])
    if ret_head != "Async" or not ret_args:
        return fn_type
    parts = list(args[:-1]) + [ret_args[0]]
    return f"({', '.join(render_type(p) for p in parts[:-1])}) -> {render_type(parts[-1])}"


class _FreeFnMonoEnv:
    """A lightweight `env` shim exposing exactly the attributes the item-342
    monomorphization walk reads, so `_monomorphize_sync_callback_calls` and
    `_arrow_reaches_async` can run over a module `fn` body or a `test`/`prop
    test` body — none of which has a component `Env`. Such a body binds no
    required key, so `services`/`requires` are empty and `_req_op_is_async` is
    always False (rule 3 is component-only)."""

    def __init__(self, colour_polymorphic, types, sync_monomorphs, callables,
                 async_callables):
        self.colour_polymorphic = colour_polymorphic
        self.types = types
        self.sync_monomorphs = sync_monomorphs
        self.callables = callables
        self.async_callables = async_callables
        self.services: dict = {}
        self.requires: dict = {}


def _colour_polymorphic_fns(fns: list, async_colored: set) -> set:
    """The item-342 colour-polymorphic set: a fn colored async SOLELY because it
    calls its own async-typed callback parameter — it has such a param, it is
    colored, and its body reaches no OTHER async name (`stop_async_arrows` prunes
    a nested async arrow, whose suspension is its own value). Its async-ness is
    contingent on the arrow actually passed: a genuinely-sync arrow lifts the fn
    to a sync clone (`_monomorphize_sync_callback_calls`)."""
    poly: set = set()
    for entry in fns:
        if entry["name"] not in async_colored:
            continue
        if not any(_is_async_fn_type(p.get("type"))
                   for p in entry.get("params") or []):
            continue
        reached: set = set()
        _calls_in(entry.get("body") or [], reached, stop_async_arrows=True)
        if not (reached & async_colored):
            poly.add(entry["name"])
    return poly


def _monomorphize_free_fn_calls(fns, externs, colour_polymorphic, preliminary,
                                types, sync_monomorphs, callables,
                                async_witness) -> set:
    """Complete item-342 at MODULE-`fn` call sites (item 387).

    342 hooked its sync monomorphization into `_lower_provide` alone, so a free
    `fn` that reaches a colour-polymorphic loop ONLY by handing it genuinely-sync
    arrows was auto-colored async by the phase-2 fixed point instead of being
    kept sync. A sync context then calling that fn (a `test` block, a sync
    provide method) diverged: py emitted a bare, un-awaited call yielding a
    coroutine while ts refused at emit, naming this very frontend hole (H29).
    Here such a fn is kept sync and its call redirected to the sync monomorph,
    exactly as a sync provide method's call is.

    An async caller is left untouched (item-92 coercion, the loop stays async):
    `genuinely_async` is the colour of the call graph once EVERY liftable
    polymorphic call is made sync, so a fn still colored there reaches a real
    suspension on its own account (a real async extern, or a genuinely-async
    arrow into the loop) and keeps its async loop. Returns the final
    `async_colored` after the rewrite (== `genuinely_async` by construction)."""
    import copy

    # 1) genuinely-async fns: recolor a throwaway copy in which every liftable
    #    polymorphic call is already synced. A fn still colored there is async on
    #    its own account, independent of the arrows a caller happens to pass.
    probe = copy.deepcopy(fns)
    probe_env = _FreeFnMonoEnv(colour_polymorphic, types, {}, callables, preliminary)
    for f in probe:
        _monomorphize_sync_callback_calls(f.get("body") or [], probe_env)
    genuinely_async = _async_callables(probe, externs)

    # 2) rewrite the REAL bodies of the fns that end up sync. A genuinely-async
    #    fn is skipped so its loop stays async — item 92's coercion, unchanged.
    real_env = _FreeFnMonoEnv(colour_polymorphic, types, sync_monomorphs,
                              callables, preliminary)
    for f in fns:
        if f["name"] in genuinely_async:
            continue
        _monomorphize_sync_callback_calls(f.get("body") or [], real_env)

    # 3) recolor for the final verdict over the rewritten bodies.
    async_witness.clear()
    return _async_callables(fns, externs, async_witness)


def _admit_sync_test_bodies(tests, prop_tests, async_colored, colour_polymorphic,
                            types, sync_monomorphs, callables, filename) -> None:
    """A plain `test`/`prop test` body is a pure SYNC context — it cannot await.
    Complete item-342 there too (item 387): monomorphize each colour-polymorphic
    call handed only genuinely-sync arrows to its sync clone, then refuse (A1)
    any residual reach of an async callable. Without this a test calling an async
    callable diverged — py emitted a bare, un-awaited call yielding a coroutine
    while ts refused at emit, naming this frontend hole (H29). A `lifecycle test`
    is untouched: it drives a live composition and awaits through the runtime."""
    env = _FreeFnMonoEnv(colour_polymorphic, types, sync_monomorphs, callables,
                         async_colored)
    for unit in list(tests or []) + list(prop_tests or []):
        if unit.get("lifecycle"):
            continue
        body = unit.get("body")
        if not body:
            continue
        if colour_polymorphic:
            _monomorphize_sync_callback_calls(body, env)
        # residual reach of an async callable — a genuinely-async callee a sync
        # test cannot await, or one passed as a value. Both tiers must refuse.
        called: set = set()
        values: set = set()
        _calls_in(body, called, values=values)
        hit = sorted((called | values) & (async_colored or set()))
        if hit:
            raise RevlError(
                filename, 0,
                f"test `{unit.get('name')}` reaches async callable `{hit[0]}`, "
                f"but a `test` body is a synchronous context with no in-flight "
                f"window to await it (A1)",
                hint="drive the async operation from a `lifecycle test` (which "
                     "runs a live composition and can await it through a provide "
                     "method), or reach it only from an `async fn` service "
                     "operation (docs/design/async-extern.md §3)",
                code="A1", category="async-propagation",
            )


def _refuse_leaky_pure_arrow(node, async_colored, decl, filename) -> None:
    """The module-fn twin of `_refuse_leaky_arrow`: a sync-typed arrow whose
    body reaches an async callable (a colored name — pure fns have no req keys)
    is the item-92 leak. Admitted (async-flagged) arrows are skipped."""
    if isinstance(node, dict):
        if node.get("kind") == "arrow" and not node.get("async"):
            called: set = set()
            _calls_in(node.get("body"), called, stop_async_arrows=True)
            hit = sorted(called & (async_colored or set()))
            if hit:
                raise RevlError(
                    (decl.source if decl is not None else None) or filename,
                    getattr(decl, "line", 0) or 0,
                    f"this arrow reaches async callable `{hit[0]}`, but its type "
                    "carries no async color — the caller would receive an "
                    "unawaited suspension (A1)",
                    hint="declare the receiving parameter `(…) -> Async[T]` so "
                         "every call through it is awaited, or move the "
                         "suspending call out of the arrow "
                         "(docs/design/async-function-values.md)",
                    code="A1", category="async-propagation",
                )
        for value in node.values():
            _refuse_leaky_pure_arrow(value, async_colored, decl, filename)
    elif isinstance(node, list):
        for value in node:
            _refuse_leaky_pure_arrow(value, async_colored, decl, filename)


def _lower_effect_step(acquire: dict, undo_expr, env: "Env", filename: str, line: int,
                       *, bind: str | None, raw_acquire=None) -> dict:
    """Build the `effect`/`let-effect` IR step for one activation-body
    acquisition (item 243 Slice 2, docs/design/243-witnessed-externs.md).

    A witnessed acquisition auto-registers its extern's DECLARED inverse as a
    transactional accumulator entry — there is no site-spelled undo, so the
    step carries no `"undo"` key at all (the parser already refuses a site
    `undo` token there; this is the defensive twin, and the checked source of
    the shape). `backends/python/emit.py._witnessed_extern` recognises this
    shape by matching the acquisition's callee name against the externs
    table, not by an IR step field, reads the DECLARED inverse from there,
    and emits the Ok-conditional transactional registration (Slice 2a). Every
    other acquisition keeps its ordinary site-spelled undo, lowered exactly
    as before — this helper changes no IR for a non-witnessed program.

    Deferred parser gate (item 315): a bare-name call missing its `undo` is
    admitted by the parser even when the callee's classification is not yet
    known — `use` imports resolve one file at a time, after parsing, so the
    parser cannot tell a same-file witnessed call from an IMPORTED one (or
    from a plain extern that is simply missing its `undo`) at parse time.
    `env.witnessed_externs` is built from `_witnessed_extern_names` over the
    MERGED, post-import program (`check_and_lower`), so by the time this runs
    an imported witnessed extern is indistinguishable from a same-file one —
    this is where the parser's deferred call is finally decided. If the
    callee turns out not to be witnessed after all, this raises the exact
    "effect has no `undo`" refusal (G4) the parser used to raise directly;
    `raw_acquire` (the original AST expression) is needed only to render that
    message the same way `_describe_expr` always has."""
    step_kind = "let-effect" if bind is not None else "effect"
    # item 397: the unbound statement form of a result-declared host verb (a
    # CAS like `insert_if_absent`) is refused. A CAS whose Bool nobody reads is
    # a plain `insert` with extra steps, and its site-spelled `undo` would be
    # registered unconditionally — removing the WINNER's entry on a `false`
    # CAS at teardown, exactly the corruption single-use exists to prevent
    # (docs/design/397-insert-if-absent.md §Classification, two sharp edges).
    if bind is None and _host_result_type(acquire, env) is not None:
        verb = str(acquire.get("method"))
        raise RevlError(
            filename, line,
            f"`{verb}` returns a value and must be bound: "
            f"`let ok = effect <map>.{verb}(k, v) undo <map>.remove(k)`",
            hint="a compare-and-set reports whether it inserted; discarding "
                 "that Bool makes its `undo` unsound (it would remove the "
                 "winning claimant's entry on a `false`). Bind the result, or "
                 "use `insert` for an unconditional overwrite",
            code="G4", category="host-boundary")
    wit_name = acquire.get("name") if acquire.get("kind") == "fn" else None
    if wit_name is not None and wit_name in env.witnessed_externs:
        if undo_expr is not None:
            raise RevlError(
                filename, line,
                f"witnessed extern `{wit_name}` cannot declare a site `undo`",
                hint="its declared inverse is auto-registered by the teardown "
                     "accumulator on the `Ok` branch; the accumulator owns "
                     "the inverse, not the call site "
                     "(docs/design/243-witnessed-externs.md)",
                code="G4", category="witnessed",
            )
        step = {"step": step_kind, "acquire": acquire}
    elif undo_expr is None:
        head = _describe_expr(raw_acquire) if raw_acquire is not None else "the expression"
        raise RevlError(
            filename, line,
            f"effect has no `undo` and {head} is not pure",
            hint=f"write `effect {head}(...) undo <expr>`, or mark the call "
                 "`emit` if it deliberately crosses the system boundary (G4)",
            code="G4", category="witnessed",
        )
    else:
        undo = _lower_expr(undo_expr, env, mode="undo")
        step = {"step": step_kind, "acquire": acquire, "undo": undo}
    if bind is not None:
        step["bind"] = bind
    return step


# ---------------------------------------------------------------------------
# item 308: effect ownership modes (owned + borrowed v1). The resource-taint
# base (R0: nominal opaque handles) lives in `resources.py`; these helpers are
# the frontend consumers of it — O1 (no hand-call of a declared inverse) and B1
# (a borrowed resource does not escape its scope). `owned` is the implicit mode
# of a handle bound by `let x = effect <acquire> …` at activation scope; every
# other resource-typed position is `borrowed` (positional, never dataflow — the
# safe direction, since every wrong answer is a false positive, not an unsound
# admission).
#
# Deferred, each with its landing place named in the design doc:
#   TODO(308-followup): `shared` mode (a 294 lease binding); `shared`/`transfer`
#     are reserved contextual keywords, not implemented in v1.
#   TODO(308-followup): explicit `transfer` (a source marker that moves the
#     bracket; the only honest future for a handoff of resource-carrying state).
#   TODO(308-followup): the retaining-extern audit (F10) — a report-only `revl
#     audit` listing of resource-typed arguments reaching a non-inverse extern
#     or bridge service (the declaration-is-the-proof-surface limitation).
#   TODO(308-followup): the method-scope acquire early-release surface (F9). v1
#     does NOT refuse method-scope acquires wholesale — the corpus admits them
#     (item 399, provide-method acquisitions), so refusing them here would break
#     additivity. Their escape hazards (return/store/capture of the method-scoped
#     handle) ARE caught by B1; the residual leak-until-unload lifetime concern
#     is the deferred F9 decision.
# ---------------------------------------------------------------------------


def _resource_ctx(types: dict) -> tuple[set, set]:
    """`(taint, closers)` from a lowering `types` table (its `APPROVAL_KEY`
    carries the externs index). Cached on the table under a private key so a
    per-site call is O(1) after the first."""
    cache = types.get("__resource_ctx__")
    if cache is not None:
        return cache
    idx = (types.get(APPROVAL_KEY) or {}).get("externs") or {}
    externs = list(idx.values())
    ctx = (resource_taint(externs, types), closing_ops(externs))
    try:
        types["__resource_ctx__"] = ctx
    except Exception:  # pragma: no cover - types is always a plain dict
        pass
    return ctx


def _owned_handles(env: "Env") -> set:
    """The activation-scope owned-handle safe-names of the component being
    lowered (bound by `let x = effect <acquire-class extern>`)."""
    owned = getattr(env, "_owned_handles", None)
    if owned is None:
        owned = set()
        env._owned_handles = owned
    return owned


def _node_local_name(node) -> str | None:
    """The bare local name a lowered leaf node references, or None. Covers the
    `{"kind": "name", "id": ..}` and `{"kind": "var", "name": ..}` shapes."""
    if not isinstance(node, dict):
        return None
    if node.get("kind") in ("name", "var"):
        return node.get("id") or node.get("name")
    return None


def _node_resource(node, env: "Env", taint: set) -> str | None:
    """The resource type a lowered expression node carries, or None."""
    if not isinstance(node, dict):
        return None
    t = infer_ir(node, env.type_env, env.types, env.services)
    return resource_in(t, taint)


def _walk_call_nodes(node):
    """Yield every lowered call node (`kind` fn/call) reachable in `node`."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if cur.get("kind") in ("fn", "call"):
                yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _walk_value_nodes(node):
    """Yield every dict node reachable in a lowered expression tree."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _o1_check(node, env: "Env", filename: str, line: int, *,
              position: str, exempt_handle: str | None = None) -> None:
    """O1: refuse a hand-call of a declared inverse (a closing op) on a
    resource-typed argument anywhere in `node`. `position` names the syntactic
    slot for the diagnostic (`body` / `undo` / `compensate`). `exempt_handle`
    is the acquiring binding's own handle safe-name: in that binding's own
    `undo`, a closer call on that handle IS the bracket being created, not a
    double-close, and is admitted (the mandatory own-undo exemption)."""
    taint, closers = _resource_ctx(env.types)
    if not closers:
        return
    for call in _walk_call_nodes(node):
        name = call.get("name") if call.get("kind") == "fn" else \
            (call.get("callee") or {}).get("name")
        if name not in closers:
            continue
        for arg in call.get("args") or []:
            rt = _node_resource(arg, env, taint)
            if not rt:
                continue
            if (exempt_handle is not None
                    and _node_local_name(arg) == exempt_handle):
                continue
            owned = _node_local_name(arg) in _owned_handles(env)
            mode = "owned" if owned else "borrowed"
            raise RevlError(
                filename, line,
                f"`{name}(...)` is a declared inverse (a close) of a resource; "
                f"the {mode} handle `{rt}` is closed exactly once by the "
                f"acquiring activation's teardown (G7), so hand-calling it here "
                f"(in {position} position) would double-close (item 308, O1)",
                hint="let teardown run the inverse; if a resource must end "
                     "early, that is an explicit-release surface revl does not "
                     "have yet (only the acquiring binding's own `undo` may name "
                     "its inverse)",
                code="G7", category="ownership",
            )


def _b1_no_resource(node, env: "Env", filename: str, line: int, *,
                    clause: str, borrows_only: bool = True,
                    extra_owned: set | None = None) -> None:
    """B1: refuse a resource-typed value appearing anywhere in `node`. When
    `borrows_only` is True the owner's own handle is admitted (the owner-carve-
    out); when False (a `compensate` position, clause 5) an OWNED handle is
    refused too, because compensations run in teardown Phase 2 after Phase 1
    closed every bracket — a resource there is use-after-close by phase order.
    `extra_owned` adds method-scope owned-handle names (an acquire bound inside
    the current provide method) to the owner set for the own-undo exemption."""
    taint, _ = _resource_ctx(env.types)
    if not taint:
        return
    owned = _owned_handles(env) | (extra_owned or set())

    def _is_owned(n):
        return _node_local_name(n) in owned

    for sub in _walk_value_nodes(node):
        rt = _node_resource(sub, env, taint)
        if not rt:
            continue
        if borrows_only and _is_owned(sub):
            continue
        mode = "owned" if _is_owned(sub) else "borrowed"
        raise RevlError(filename, line, _b1_message(clause, mode, rt),
                        hint=_b1_hint(clause), code="G7", category="ownership")
    # a borrowed value with no local name (a bare call result / field read) is
    # still a resource by type; catch it when the whole node is the resource.
    rt = _node_resource(node, env, taint)
    if rt and not (borrows_only and _is_owned(node)):
        mode = "owned" if _is_owned(node) else "borrowed"
        raise RevlError(filename, line, _b1_message(clause, mode, rt),
                        hint=_b1_hint(clause), code="G7", category="ownership")


_B1_CLAUSE_TEXT = {
    "state": "stored into activation-level state",
    "capture": "captured by a closure",
    "return": "returned across a signature",
    "carrier": "placed in an escaping record/collection value",
    "undo": "placed in an `undo` expression",
    "witnessed": "passed to a witnessed effect's argument list",
    "compensate": "placed in a `compensate` expression",
    "spawn": "seated in a `spawn` config value",
    "handoff": "carried by a `handoff` type",
}


# container-mutating verbs whose target is an activation-level host local: a
# resource seated through one of these is stored into activation state (B1
# clause 1), unlike a plain fn/service call which is legitimate down-passing.
_MUTATING_VERBS = frozenset({
    "insert", "put", "set", "push", "add", "append", "store", "enqueue",
})


def _b1_flag_if_borrow(v, env: "Env", taint: set, owned: set, filename: str,
                       line: int, clause: str) -> None:
    rt = _node_resource(v, env, taint)
    if rt and _node_local_name(v) not in owned:
        raise RevlError(filename, line, _b1_message(clause, "borrowed", rt),
                        hint=_b1_hint(clause), code="G7", category="ownership")


def _b1_body_scan(node, env: "Env", filename: str, line: int) -> None:
    """B1 clauses 1 (store into activation state) and 4 (escaping carrier):
    refuse a BORROWED resource value placed in a record/list/map literal, or
    inserted into an activation-level container (a mutating verb on an
    activation host local). Both orders are caught — the value is checked
    wherever it is seated, whether the carrier is then parked or was already
    parked. The owner's own handle is exempt (these clauses bind borrows only);
    passing a borrow DOWN a plain call chain stays admitted."""
    taint, _ = _resource_ctx(env.types)
    if not taint:
        return
    owned = _owned_handles(env)
    host_locals = getattr(env, "host_locals", {}) or {}
    for sub in _walk_value_nodes(node):
        kind = sub.get("kind")
        if kind == "record":
            for _, v in sub.get("fields") or []:
                _b1_flag_if_borrow(v, env, taint, owned, filename, line, "carrier")
        elif kind == "list":
            for v in sub.get("items") or []:
                _b1_flag_if_borrow(v, env, taint, owned, filename, line, "carrier")
        elif kind == "map":
            for entry in sub.get("entries") or []:
                v = entry[1] if isinstance(entry, (list, tuple)) and len(entry) == 2 \
                    else entry
                _b1_flag_if_borrow(v, env, taint, owned, filename, line, "carrier")
        elif kind == "call" and sub.get("method") in _MUTATING_VERBS:
            if _node_local_name(sub.get("target") or {}) in host_locals:
                for a in sub.get("args") or []:
                    _b1_flag_if_borrow(a, env, taint, owned, filename, line, "state")


def _ownership_check_expr(node, env: "Env", filename: str, line: int) -> None:
    """Run the body-position ownership checks over one lowered expression: O1
    (no hand-call of a declared inverse, no own-undo exemption in a body
    position) and B1 clauses 1/4 (no borrow stored into activation state or an
    escaping carrier)."""
    if node is None:
        return
    _o1_check(node, env, filename, line, position="body")
    _b1_body_scan(node, env, filename, line)


def _ownership_walk_method(steps, env: "Env", filename: str, line: int) -> None:
    """Apply the body-position O1/B1 checks over a lowered provide-METHOD body.
    A handle a service method parameter carries is a BORROW; an acquire bound
    inside the method is owned by the method (its own `undo` is exempt). Runs
    O1 (no hand-close) and B1 clauses 1/4/5 across the method's statements —
    clause 3 (return) and the emit `compensate` half are enforced inline where
    they are lowered."""
    method_owned: set = set()
    taint, _ = _resource_ctx(env.types)
    for st in steps or []:
        if not isinstance(st, dict):
            continue
        stp = st.get("step")
        if stp in ("let-effect", "effect"):
            acq = st.get("acquire")
            bind = st.get("bind")
            acq_res = _node_resource(acq, env, taint) if acq is not None else None
            undo = st.get("undo")
            if undo is not None:
                exempt = bind if (acq_res and bind) else None
                _o1_check(undo, env, filename, line, position="undo",
                          exempt_handle=exempt)
                _b1_no_resource(undo, env, filename, line, clause="undo",
                                borrows_only=True, extra_owned=method_owned)
            _ownership_check_expr(acq, env, filename, line)
            _b1_witnessed_check(acq, env, filename, line)
            if acq_res and bind:
                method_owned.add(bind)
        elif stp == "emit":
            _ownership_check_expr(st.get("expr"), env, filename, line)
        elif stp in ("let", "assign"):
            _ownership_check_expr(st.get("value"), env, filename, line)
        elif stp in ("return", "await"):
            _ownership_check_expr(st.get("expr"), env, filename, line)


def _b1_witnessed_check(acquire, env: "Env", filename: str, line: int) -> None:
    """B1 clause 5 (witnessed half): a witnessed effect's declared inverse is
    auto-registered on the same per-activation accumulator as an `undo`, so a
    resource-typed value in its argument list rides teardown and replays after
    the owner is gone. Refuse a resource argument to a witnessed acquisition."""
    if not isinstance(acquire, dict) or acquire.get("kind") != "fn":
        return
    if acquire.get("name") not in getattr(env, "witnessed_externs", set()):
        return
    taint, _ = _resource_ctx(env.types)
    if not taint:
        return
    owned = _owned_handles(env)
    for a in acquire.get("args") or []:
        rt = _node_resource(a, env, taint)
        if rt:
            mode = "owned" if _node_local_name(a) in owned else "borrowed"
            raise RevlError(filename, line, _b1_message("witnessed", mode, rt),
                            hint=_b1_hint("witnessed"), code="G7",
                            category="ownership")


def _all_free_names(expr, bound: set[str]) -> set[str]:
    """Every `ExprVar` name referenced in `expr` and not shadowed by a lambda
    parameter or match binding (all free vars, unlike `_mutable_free_vars`
    which keeps only mutable ones)."""
    bound = set(bound or ())
    if isinstance(expr, ExprVar):
        return set() if expr.name in bound else {expr.name}
    if isinstance(expr, ExprArrow):
        return _all_free_names(expr.body, bound | set(expr.params))
    if isinstance(expr, ExprMatch):
        found = _all_free_names(expr.scrutinee, bound)
        for _, bind, body in expr.arms:
            arm_bound = set(bound) | ({bind} if bind is not None else set())
            found |= _all_free_names(body, arm_bound)
        return found
    found: set[str] = set()
    for attr in ("left", "right", "operand", "callee", "target", "index",
                 "cond", "then", "otherwise", "base"):
        child = getattr(expr, attr, None)
        if child is not None and not isinstance(child, (str, int, bool)):
            found |= _all_free_names(child, bound)
    for arg in getattr(expr, "args", None) or []:
        found |= _all_free_names(arg, bound)
    for _, value in getattr(expr, "fields", None) or []:
        found |= _all_free_names(value, bound)
    for _, value in getattr(expr, "updates", None) or []:
        found |= _all_free_names(value, bound)
    for item in getattr(expr, "items", None) or []:
        found |= _all_free_names(item, bound)
    return found


def _b1_capture_check(arrow, type_env: dict, types: dict, filename: str,
                      line: int) -> None:
    """item 308, B1 clause 2: refuse capturing ANY resource-typed value in a
    closure (owner included). A closure value's type (`() -> Int`) erases the
    capture, so a closure carrying a handle is invisible to the taint fixpoint
    and launders the handle across a signature (the recommended v1 rule is the
    outright refusal, strictly smaller than closure-value taint tracking)."""
    taint, _ = _resource_ctx(types)
    if not taint:
        return
    for name in _all_free_names(arrow.body, set(arrow.params)):
        t = type_env.get(name)
        rt = resource_in(t, taint) if t else None
        if rt:
            raise RevlError(
                filename, line, _b1_message("capture", "borrowed", rt),
                hint=_b1_hint("capture"), code="G7", category="ownership")


def _b1_return_admitted(node, env: "Env", taint: set) -> bool:
    """B1 clause 3: is this resource-typed return a borrow-CREATING move (so it
    is admitted) rather than a borrow-escape?

      * the owner returning its OWN activation handle (a bare name in the owned
        set), or
      * a FRESH MINT: a direct call whose result is the resource and NONE of
        whose arguments carry a resource — a constructor/factory handing the
        caller a newly-made handle (the transfer case). A call that threads a
        resource ARGUMENT into its result is a carrier/laundering and stays
        refused.
    """
    if _node_local_name(node) in _owned_handles(env):
        return True
    if isinstance(node, dict) and node.get("kind") in ("fn", "call"):
        for a in node.get("args") or []:
            if _node_resource(a, env, taint):
                return False
        return True
    return False


def _b1_message(clause: str, mode: str, rt: str) -> str:
    what = _B1_CLAUSE_TEXT.get(clause, clause)
    return (f"{mode} resource `{rt}` cannot be {what}; a borrow is confined to "
            f"the scope that received it and may not outlive its owner's "
            f"bracket (G7) (item 308, B1)")


def _b1_hint(clause: str) -> str:
    if clause in ("state", "carrier"):
        return ("restructure so the owner holds the handle and lends it per "
                "call; a borrow may be passed further down a call chain, but "
                "not parked in activation state or an escaping carrier")
    if clause == "capture":
        return ("v1 refuses capturing any resource handle in a closure (owner "
                "included): a closure value's type erases the capture, so it "
                "would launder the handle across a signature")
    if clause == "return":
        return ("only the owner may return its OWN handle from a provide "
                "method; a borrow (or a tainted carrier) may not be returned "
                "onward")
    if clause in ("undo", "witnessed", "compensate"):
        return ("teardown-position captures outlive or outrun the bracket; a "
                "compensate runs after every bracket closed, so no resource "
                "value (owned or borrowed) may appear there")
    if clause == "spawn":
        return ("spawn config seats the value in the child's activation for its "
                "whole lifetime; a child acquires its own handle or calls the "
                "owner's service per use")
    if clause == "handoff":
        return ("a handoff re-seats the predecessor's resource vector on the "
                "successor while the predecessor's teardown closes it; a real "
                "handle transfer is the deferred `transfer` marker, not handoff "
                "shape-compat")
    return "a borrow may not escape its scope"


def _poison_failed_binding(stmt, env: "Env") -> None:
    """Bind a recovered binding-statement's name to the poison sentinel so its
    later uses stay silent (item 386, Stage 2).

    When a `let`-binding component statement refuses (its initializer was a type
    error), the name may be UNBOUND — the refusal fired before `bind_local` ran —
    or bound with a now-stale type. Either way, rebinding it to `POISON` (an
    absorbing, silent wildcard) is what stops the downstream statements that read
    it from each fabricating a second diagnostic: without a binding at all, every
    later reference would raise an undefined-name cascade; with a concrete stale
    type, a type cascade. A non-binding action (an `effect`/`emit`/`fail` with no
    handle) introduces no reusable name, so there is nothing to poison and this
    is a no-op."""
    bind = getattr(stmt, "bind", None)
    if not bind:
        return
    safe = env.locals.get(bind)
    if safe is None:
        try:
            safe = env.bind_local(bind, getattr(stmt, "line", 0))
        except RevlError:
            return  # the name was already bound (e.g. a duplicate-name refusal)
    env.type_env[safe] = POISON


def _lower_component(comp: ComponentDecl, services: dict[str, ServiceDecl], filename: str,
                     callables: set | None = None, types: dict | None = None,
                     emitting_fns: set | None = None,
                     emitting_caps: dict | None = None,
                     emission_evidence: "_EmissionEvidence | None" = None,
                     spawn_reg: dict | None = None,
                     async_colored: set | None = None,
                     witnessed_externs: set | None = None,
                     colour_polymorphic: set | None = None,
                     sync_monomorphs: dict | None = None,
                     poly_externs: set | None = None,
                     extern_colour_instances: dict | None = None,
                     errors: list | None = None) -> dict:
    env = Env(comp, services, filename, types)
    env.emitting_fns = emitting_fns or set()
    env.emitting_caps = emitting_caps or {}
    env.emission_evidence = emission_evidence
    env.witnessed_externs = witnessed_externs or set()
    env.async_externs = dict(emission_evidence.async_externs) if emission_evidence else {}
    # the phase-2 async-colored set (async externs + fns that transitively
    # reach one, docs/design/async-extern.md §3): the provide-method admission
    # tests membership here, not just direct extern calls, so a sync method
    # reaching a colored fn is refused too. Falls back to the extern names
    # alone when the fixed point was not supplied (older callers/tests).
    env.async_callables = set(async_colored) if async_colored is not None \
        else set(env.async_externs)
    # sync/async arrow polymorphism (item 342): the colour-polymorphic fns
    # (async solely by their own callback param) and the shared registry the
    # sync call sites fill with monomorph requests (monomorph-name -> origin).
    env.colour_polymorphic = set(colour_polymorphic) if colour_polymorphic else set()
    env.sync_monomorphs = sync_monomorphs if sync_monomorphs is not None else {}
    # item 388: the poly externs (`fn|async`) and the shared registry each
    # provide method fills with the call-site colour it requested. `_resolve_poly
    # _extern_calls` reads `poly_externs` to spot a poly-extern call and records
    # into `extern_colour_instances`; `_finalize_poly_externs` reads it after the
    # component loop. Empty unless the program declares a poly extern.
    env.poly_externs = set(poly_externs) if poly_externs else set()
    env.extern_colour_instances = (extern_colour_instances
                                   if extern_colour_instances is not None else {})
    env.callables = callables or set()  # module fns/externs/hosts for unified expressions
    # instance-parametric components: the registry of spawn targets, edges,
    # templates and G4 spawn-boundary sites (docs/design-v2-instances.md)
    env.spawn_reg = spawn_reg

    # a config default must fit its declared field type (config typing);
    # a `null` default is the documented optional exception and is allowed
    for cfg in comp.config:
        if cfg.default is None:
            continue
        lit_type = _config_default_type(cfg.default)
        if lit_type is not None and not compatible(cfg.type, lit_type):
            raise mismatch(filename, cfg.line,
                           f"config field `{cfg.name}` default", cfg.type, lit_type)

    provides = {}
    for key, svc, line in comp.provides:
        if svc not in services:
            raise RevlError(filename, line, f"unknown service `{svc}` in `provides` of {comp.name}")
        if key in provides:
            raise RevlError(filename, line, f"duplicate provision key `{key}` in {comp.name}")
        provides[key] = svc

    body = []
    provided_keys: set[str] = set()
    provide_seen_line: int | None = None
    isolate: dict[str, str] = {}
    intercept: dict[str, dict] = {}
    routes: dict[str, dict] = {}
    handoff: dict | None = None
    action_seen = False
    # item 386, Stage 2: set when a statement's type-check refused and was
    # recovered past (statement-boundary synchronization). A component with any
    # recovered statement is marked `poisoned` below, exactly like a Stage-1
    # header-abort stub, so the body-walking post-passes skip its partial body.
    recovered = False

    def _dispatch_action(stmt, provide_seen_line):
        """Lower one *action* statement (effect/emit/fail/if/provide/…),
        appending its step(s) to `body`, and return the (possibly updated)
        `provide_seen_line`. Factored out of the body loop so the loop can wrap
        it in the Stage-2 statement-boundary recovery (item 386): a `RevlError`
        raised here is caught one frame out, recorded, and the walk resumes at
        the next statement. It closes over the component's lowering state
        (`env`, `body`, `provides`, `provided_keys`, `filename`, `callables`)."""
        if isinstance(stmt, (LetEffect, EffectStmt, TimerStmt)) and provide_seen_line is not None:
            raise RevlError(
                filename, stmt.line,
                "acquisition after `provide` — an effect acquired after a provision "
                "would be reverted while dependents can still call the service",
                hint="move acquisitions above the `provide` block (linker rule A2). "
                     "A timer is an acquisition too: its schedule is armed at "
                     "activation and cancelled on teardown (docs/time-coeffect.md)"
                     if isinstance(stmt, TimerStmt) else
                     "move acquisitions above the `provide` block (linker rule A2)",
            )
        if isinstance(stmt, EffectStmt) and isinstance(stmt.acquire, SpawnExpr):
            # a spawn's inverse is the instance's own teardown, which needs a
            # handle to name — so a spawn must be bound (decision 2)
            raise RevlError(
                filename, stmt.line,
                "`spawn` must be bound to a handle: "
                f"`let s = effect spawn {stmt.acquire.component} … undo s.dispose()`",
            )
        if isinstance(stmt, LetEffect) and isinstance(stmt.acquire, LeaseAcquire):
            # item 294 Slice 2: a capability lease. The acquire is not a host
            # call but a class-(c)-gated, ticket-mediated mint of a standing grant
            # over the capability's cone (session._enforce_lease_gate raises the
            # ticket before boot; an ungated run refuses). It lowers to a reserved
            # runtime acquisition returning a lease handle, and the site `undo
            # l.revoke()` retires that grant on the LIFO teardown. Duration/uses
            # reuse the grant's expiresAt/remainingUses; nothing here mints — the
            # grant is minted from the approved ticket, never self-minted.
            body.append(_lower_lease_step(stmt, env, filename))
        elif isinstance(stmt, LetEffect):
            if stmt.setup:
                saved_locals = dict(env.locals)
                saved_taken = set(env._taken)
                saved_type_env = dict(env.type_env)
                setup_steps: list[dict] = []
                scope = _component_scope(env)
                mutables: set[str] = set()
                for setup_stmt in stmt.setup:
                    _lower_component_setup_stmt(setup_stmt, env, scope, callables or set(),
                                                mutables, setup_steps)
                acquire = _lower_component_pure_expr(stmt.acquire, env, scope, callables or set())
                env.locals = saved_locals
                env._taken = saved_taken
                # setup-let types are block-scoped; drop them so a recycled safe
                # name (env._taken is restored above) can't read a stale type
                env.type_env = saved_type_env
            else:
                setup_steps = []
                acquire = _lower_expr(stmt.acquire, env, mode="setup")
            safe = env.bind_local(stmt.bind, stmt.line)
            # host provenance: an effect-acquired HOST object (`Map.new()`)
            # keeps its verb surface verbatim, exempt from the stdlib table —
            # see Env.host_locals. Record the acquisition's family (`Map` from
            # `Map.new`) so later method calls are checked against that family's
            # verb surface (item 401).
            if acquire.get("kind") == "host":
                env.host_locals[safe] = str(acquire["fn"]).partition(".")[0]
            acquired_type = infer_ir(acquire, env.type_env, env.types, env.services)
            # item 397: a result-declared host verb (a Bool CAS like
            # `insert_if_absent`) types its bind from the frontier's result
            # column. `infer_ir` cannot see host-local provenance (it takes no
            # `host_locals`), so the type is resolved here where it is known.
            if acquired_type is None:
                acquired_type = _host_result_type(acquire, env)
            if acquired_type is not None:
                env.type_env[safe] = acquired_type
            step = _lower_effect_step(acquire, stmt.undo, env, filename, stmt.line,
                                      bind=safe, raw_acquire=stmt.acquire)
            if setup_steps:
                step["setup"] = setup_steps
            if getattr(stmt, "verified", False):
                step["verified"] = True
            _admit_effect_async(stmt, step, env, filename)
            # item 308: a `let x = effect <acquire> …` whose acquisition yields a
            # resource makes `x` the OWNED handle of this activation. O1 admits
            # this binding's own `undo` naming its own inverse on `x` (the
            # own-undo exemption); B1 clause 5 still refuses a BORROW smuggled
            # into that undo.
            _taint308, _ = _resource_ctx(env.types)
            if resource_in(acquired_type, _taint308):
                _owned_handles(env).add(safe)
            if step.get("undo") is not None:
                _o1_check(step["undo"], env, filename, stmt.line,
                          position="undo", exempt_handle=safe)
                _b1_no_resource(step["undo"], env, filename, stmt.line,
                                clause="undo", borrows_only=True)
            _ownership_check_expr(step.get("acquire"), env, filename, stmt.line)
            _b1_witnessed_check(step.get("acquire"), env, filename, stmt.line)
            body.append(step)
        elif isinstance(stmt, EffectStmt):
            if stmt.setup:
                saved_locals = dict(env.locals)
                saved_taken = set(env._taken)
                saved_type_env = dict(env.type_env)
                setup_steps = []
                scope = _component_scope(env)
                mutables = set()
                for setup_stmt in stmt.setup:
                    _lower_component_setup_stmt(setup_stmt, env, scope, callables or set(),
                                                mutables, setup_steps)
                acquire = _lower_component_pure_expr(stmt.acquire, env, scope, callables or set())
                env.locals = saved_locals
                env._taken = saved_taken
                env.type_env = saved_type_env
            else:
                setup_steps = []
                acquire = _lower_expr(stmt.acquire, env, mode="setup")
            step = _lower_effect_step(acquire, stmt.undo, env, filename, stmt.line,
                                      bind=None, raw_acquire=stmt.acquire)
            if setup_steps:
                step["setup"] = setup_steps
            if getattr(stmt, "verified", False):
                step["verified"] = True
            _admit_effect_async(stmt, step, env, filename)
            # item 308: an unbound effect creates no owned handle, so its `undo`
            # gets no own-undo exemption (O1) and may carry no resource (B1).
            if step.get("undo") is not None:
                _o1_check(step["undo"], env, filename, stmt.line, position="undo")
                _b1_no_resource(step["undo"], env, filename, stmt.line,
                                clause="undo", borrows_only=True)
            _ownership_check_expr(step.get("acquire"), env, filename, stmt.line)
            _b1_witnessed_check(step.get("acquire"), env, filename, stmt.line)
            body.append(step)
        elif isinstance(stmt, FailStmt):
            body.append({
                "step": "fail",
                "message": _lower_component_pure_expr(
                    stmt.message, env, _component_scope(env), callables or set(),
                    pure_only=True,
                ),
            })
        elif isinstance(stmt, IfStmt):
            body.append(_lower_component_if(stmt, env, callables or set()))
        elif isinstance(stmt, LetApprovalStmt):
            body.append(_lower_let_approval(stmt, env))
        elif isinstance(stmt, EmitStmt):
            emit_step = _lower_emit_step(stmt, env)
            _admit_emit_async(stmt, emit_step, env, filename)
            body.append(emit_step)
        elif isinstance(stmt, TimerStmt):
            body.append(_lower_timer_step(stmt, env))
        elif isinstance(stmt, AwaitStmt):
            body.append({"step": "await", "expr": _lower_expr(stmt.expr, env, mode="setup")})
        elif isinstance(stmt, ProvideStmt):
            provide_seen_line = stmt.line
            body.append(_lower_provide(stmt, provides, provided_keys, env))
        else:  # pragma: no cover — grammar prevents it
            raise RevlError(filename, stmt.line, "unexpected statement in component body")
        return provide_seen_line

    for stmt in comp.body:
        if isinstance(stmt, HandoffStmt):
            # `handoff <key>: <Type>` (roadmap item 53): the verified state
            # hand-off of a stateful provider. A prelude declaration — it names
            # what this provider's live state *is*, before any effect creates
            # it — and targets a key this component provides (its own state
            # crosses to whoever re-provides that key). At most one per
            # component: a component's activation frame holds one resource
            # vector, so one declared shape describes it without ambiguity.
            if action_seen:
                raise RevlError(
                    filename, stmt.line,
                    "`handoff` must precede every effect, emit, await, and provide statement",
                    hint="a hand-off declares the provider's state shape before any "
                         "effect creates it (prelude rule, docs/state-handoff.md)",
                )
            if stmt.key not in provides:
                raise RevlError(
                    filename, stmt.line,
                    f"`{stmt.key}` is not a declared provision of {comp.name}",
                    hint="`handoff` targets a key this component provides — it is the "
                         "state a successor re-providing that key inherits (item 53)",
                )
            if handoff is not None:
                raise RevlError(
                    filename, stmt.line,
                    f"{comp.name} declares more than one `handoff` — a component has one "
                    f"activation frame, so it hands off one state shape",
                    hint="thread every piece of live state through the one hand-off type "
                         "(a record or Map), docs/state-handoff.md",
                )
            # item 246, invariant 5 (non-persistence): a hand-off crosses the
            # session boundary, so an `Approval[C]` may not be its shape. The
            # general type well-formedness rule refuses it (with `Async` etc.).
            check_type_wellformed(filename, stmt.line, stmt.state_type)
            # item 308, B1 clause 7: a resource-tainted handoff type re-seats the
            # predecessor's live handle vector on the successor while the
            # predecessor's teardown closes it — the successor starts warm with a
            # dead descriptor. Refuse it at admission from the shared taint set.
            _taint308, _ = _resource_ctx(env.types)
            _ho_res = resource_in(stmt.state_type, _taint308)
            if _ho_res:
                raise RevlError(
                    filename, stmt.line,
                    _b1_message("handoff", "owned", _ho_res),
                    hint=_b1_hint("handoff"), code="G7", category="ownership")
            handoff = {"key": stmt.key, "type": stmt.state_type}
            continue
        if isinstance(stmt, RouteStmt):
            # multi-realm bind (item 162): `isolate <key> in realms(...) [strategy(...)]`.
            # A prelude declaration, exactly like `isolate`/`intercept` — it derives
            # the resolution context (the router's realm set) before any dependency
            # access, so it must precede every action.
            if action_seen:
                raise RevlError(
                    filename, stmt.line,
                    "`isolate ... in realms(...)` must precede every effect, emit, await, "
                    "and provide statement",
                    hint="realm bindings derive the resolution context before any dependency "
                         "access (prelude rule, docs/design-v2-realms.md)",
                )
            required_keys = set(env.require_keys.values())
            # routing distributes a *consumer's* dependency across N backend
            # realms — it targets a REQUIRED key. A provision has one installed
            # instance in one realm (G2), so routing a provision is meaningless.
            if stmt.key not in required_keys:
                if stmt.key in provides:
                    raise RevlError(
                        filename, stmt.line,
                        f"`isolate ... in realms(...)` routes a *required* key — `{stmt.key}` "
                        f"is a provision of {comp.name}",
                        hint="a multi-realm bind distributes a consumer's dependency across "
                             "backend realms (item 162); a provision has one installed "
                             "instance in one realm (G2). Route the key you require, not the "
                             "one you provide",
                    )
                raise RevlError(
                    filename, stmt.line,
                    f"`{stmt.key}` is not a declared requirement of {comp.name}",
                    hint="`isolate ... in realms(...)` targets a key from the `requires` "
                         "clause (G1)",
                )
            # one binding per key: a key is pinned to one realm *or* routed across
            # a realm set, never both, and never two conflicting route sets.
            if stmt.key in isolate:
                raise RevlError(
                    filename, stmt.line,
                    f"key `{stmt.key}` is already isolated to a single realm in {comp.name} — "
                    f"it cannot also be routed across `realms(...)`",
                    hint="use either `isolate <key> in realm(<name>)` (pin) or "
                         "`isolate <key> in realms(...)` (route), not both",
                )
            if stmt.key in routes:
                raise RevlError(
                    filename, stmt.line,
                    f"key `{stmt.key}` is routed twice in {comp.name}",
                )
            if stmt.strategy is not None and stmt.strategy not in KNOWN_STRATEGIES:
                known = ", ".join(sorted(KNOWN_STRATEGIES))
                raise RevlError(
                    filename, stmt.line,
                    f"unknown routing strategy `{stmt.strategy}` for `{stmt.key}` in "
                    f"{comp.name}",
                    hint=f"a strategy is validated at compile time so a typo is not a silent "
                         f"runtime fallback; known strategies are: {known} (or omit "
                         f"`strategy(...)` for the router's default)",
                )
            routes[stmt.key] = {"realms": list(stmt.realms), "strategy": stmt.strategy}
            continue
        if isinstance(stmt, (IsolateStmt, InterceptStmt)):
            # prelude rule: realm/metadata declarations derive the resolution
            # context (Def. 27/29) and must precede every dependency access
            if action_seen:
                kw = "isolate" if isinstance(stmt, IsolateStmt) else "intercept"
                raise RevlError(
                    filename, stmt.line,
                    f"`{kw}` must precede every effect, emit, await, and provide statement",
                    hint="realm and metadata declarations derive the resolution context "
                         "before any dependency access (prelude rule, docs/design-v2-realms.md)",
                )
            # isolate/intercept name a key by its *qualified* wiring identity,
            # the same string G2 and the linker compare (docs/namespacing.md)
            required_keys = set(env.require_keys.values())
            if isinstance(stmt, IsolateStmt):
                if stmt.key not in required_keys and stmt.key not in provides:
                    raise RevlError(
                        filename, stmt.line,
                        f"`{stmt.key}` is not a declared requirement or provision of {comp.name}",
                        hint="`isolate` targets a key from the component header (G1)",
                    )
                if stmt.key in isolate:
                    raise RevlError(filename, stmt.line,
                                    f"key `{stmt.key}` is isolated twice in {comp.name}")
                isolate[stmt.key] = stmt.realm
            else:
                if stmt.key in provides and stmt.key not in required_keys:
                    raise RevlError(
                        filename, stmt.line,
                        f"`intercept` applies to required keys only — `{stmt.key}` is a provision",
                        hint="interception is the component-declared metadata d(k) of Def. 30, "
                             "whose domain is the dependency set; providers receive metadata "
                             "from their consumers' declarations",
                    )
                if stmt.key not in required_keys:
                    raise RevlError(
                        filename, stmt.line,
                        f"`{stmt.key}` is not a declared requirement of {comp.name}",
                        hint="`intercept` targets a key from the `requires` clause (G1)",
                    )
                if stmt.key in intercept:
                    raise RevlError(filename, stmt.line,
                                    f"key `{stmt.key}` is intercepted twice in {comp.name}")
                intercept[stmt.key] = stmt.metadata
            continue
        action_seen = True
        try:
            provide_seen_line = _dispatch_action(stmt, provide_seen_line)
        except RevlError as stmt_error:
            # item 386, Stage 2: statement-boundary synchronization. This
            # statement's type-check (or a per-statement structural rule) refused.
            # With a collect-all sink, record that ONE diagnostic and resume at
            # the NEXT statement, so several independent bad statements in one
            # component are all reported in a single pass. A binding form still
            # binds its name — to POISON — so the later statements that USE it read
            # a silent wildcard instead of fabricating a second mismatch at every
            # use (the sentinel is emitted where poison is BORN, here, never where
            # it propagates). With no sink (a direct/legacy caller, or a nested
            # lowering re-entering the spine) the refusal propagates to the
            # component boundary, byte-identical to Stage 1.
            if errors is None:
                raise
            errors.append(stmt_error)
            _poison_failed_binding(stmt, env)
            recovered = True
            continue

    lowered = {
        "name": comp.name,
        "source": comp.source or filename,
        "config": [{"name": f.name, "type": f.type, "default": f.default} for f in comp.config],
        # the IR carries the *qualified* wiring key (G2 / injection identity),
        # not the code-facing binding; for unqualified keys the two coincide,
        # so v1 documents stay byte-identical (docs/namespacing.md)
        "requires": {env.require_keys[binding]: svc for binding, svc in env.requires.items()},
        "provides": provides,
        "body": body,
    }
    # v2 fields appear only when used, so v1 documents stay byte-identical
    if isolate:
        lowered["isolate"] = isolate
    if intercept:
        lowered["intercept"] = intercept
    # multi-realm bind (item 162), additive — a component with no `realms(...)`
    # route carries no `routes` key, so its IR is byte-identical to before.
    if routes:
        lowered["routes"] = routes
    # item 53: the state hand-off contract, additive — a stateless component
    # (the overwhelming majority) carries no `handoff` key, so its IR is
    # byte-identical to before. `ir_version` stays 3.
    if handoff is not None:
        lowered["handoff"] = handoff
    # item 386, Stage 2: a component that recovered past a refused statement has
    # a partial, untrustworthy body. Mark it `poisoned` — exactly like a Stage-1
    # header-abort stub — so the body-walking post-passes (taint, spawn
    # bounds/attenuation, holes) skip it while `_link` still sees its complete
    # header topology (provides/requires) and reports real G2/G3 on its keys.
    # The compile is already failing (its refusals are in `errors`), so this
    # partial IR is never emitted; the raise in `check_and_lower` precedes IR
    # assembly. A clean component never sets `recovered`, so its IR is
    # byte-identical to before.
    if recovered:
        lowered["poisoned"] = True
    return lowered


def _lower_provide(stmt: ProvideStmt, provides: dict[str, str], provided_keys: set[str], env: Env) -> dict:
    filename = env.filename
    comp = env.component
    if stmt.key not in provides:
        declared = ", ".join(f"`{k}`" for k in provides) or "none"
        raise RevlError(
            filename, stmt.line,
            f"`{stmt.key}` is not declared in the `provides` clause of {comp.name} (A9)",
            hint=f"rename the provide block to a declared key (declared: {declared}), "
                 f"or add `{stmt.key}: <Service>` to the `provides` clause of {comp.name}",
        )
    if stmt.key in provided_keys:
        raise RevlError(filename, stmt.line, f"provision `{stmt.key}` is installed twice in {comp.name}")
    provided_keys.add(stmt.key)

    svc = env.services[provides[stmt.key]]
    methods = []
    implemented = set()
    for method in stmt.methods:
        decl = svc.methods.get(method.name)
        if decl is None:
            raise RevlError(filename, method.line,
                            f"`{method.name}` is not a method of service {svc.name}")
        if len(method.params) != len(decl.params):
            raise RevlError(
                filename, method.line,
                f"method `{method.name}` of provision `{stmt.key}` takes {len(method.params)} "
                f"params but service {svc.name} declares {len(decl.params)}",
            )
        if method.async_ != decl.async_:
            raise RevlError(
                filename, method.line,
                f"method `{method.name}` of provision `{stmt.key}` is "
                f"{'async' if method.async_ else 'not async'} but service {svc.name} "
                f"declares it {'async' if decl.async_ else 'not async'}",
            )
        if method.name in implemented:
            raise RevlError(filename, method.line, f"duplicate method `{method.name}` in provision `{stmt.key}`")
        implemented.add(method.name)

        # optional param annotations (syntax-2.0: models write `fn query(sql:
        # Str)` on autopilot): well-formed and checked against the service's
        # declared type (A6 — the service is the source of truth).
        for surface, annotation, (_, svc_ptype) in zip(
            method.params, method.param_types or [None] * len(method.params), decl.params
        ):
            if annotation is None:
                continue
            check_type_wellformed(filename, method.line, annotation)
            if svc_ptype and not (compatible(svc_ptype, annotation)
                                  and compatible(annotation, svc_ptype)):
                raise mismatch(
                    filename, method.line,
                    f"parameter `{surface}` of `{method.name}` (from service `{svc.name}`)",
                    svc_ptype, annotation)
        # optional `-> T` return annotation, checked against the service
        if method.returns is not None:
            check_type_wellformed(filename, method.line, method.returns)
            if decl.returns and not (compatible(decl.returns, method.returns)
                                     and compatible(method.returns, decl.returns)):
                raise mismatch(
                    filename, method.line,
                    f"return type of `{method.name}` (from service `{svc.name}`)",
                    decl.returns, method.returns)

        saved = env.params
        env.params = env.bind_params(method.params, method.line)
        # method params carry the service's declared types (A6): surface
        # names bind the body, the service contributes the signature
        saved_tenv = dict(env.type_env)
        method_locals: dict[str, str] = {}
        for surface, (_, ptype) in zip(method.params, decl.params):
            env.type_env[env.params[surface]] = ptype
        mbody = []
        returned = False

        def _sweep(node, line):
            # item 404: the provide-method twin of the activation-setup
            # `_sweep` (`_lower_component_setup_stmt`). A provide-method body is
            # stratum-3 lowered code that ran `infer_ir` only in its NON-raising
            # oracle mode, so a definite operator / index / builtin misuse that a
            # `fn`/`test` body (stratum 1, `infer_ast`) refuses compiled clean
            # inside a provide method — the same context-scoping gap item 392
            # closed for the Any-field-read. Run the SAME raising oracle over the
            # lowered pure value here so the whole refusal class fires uniformly.
            # Unknown / host operands infer to None and are left alone, exactly
            # as in the setup sweep. Returns the inferred type (or None).
            return infer_ir(node, env.type_env, env.types, env.services,
                            filename, line)

        for mstmt in method.body:
            if returned:
                raise RevlError(filename, mstmt.line, "unreachable statement after `return`")
            if getattr(mstmt, "verified", False):
                # `verified effect` is inverse round-trip tested by activating
                # the component and tearing it down (roadmap item 26); that
                # round-trip is only well-defined for an *activation-body*
                # effect, whose inverse the fiber's teardown runs. A
                # method-body effect runs per request, so it has no such
                # closed activate/teardown window — reject rather than accept
                # a marker the runner cannot honour.
                raise RevlError(
                    filename, mstmt.line,
                    "`verified effect` is only allowed in a component activation body",
                    hint="inverse round-trip testing activates the component and tears it "
                         "down; a provide-method effect runs per request and has no such "
                         "window (docs/verified-effect.md). Drop `verified`, or move the "
                         "effect to the activation body.")
            if isinstance(mstmt, LetEffect) and not isinstance(mstmt.acquire, SpawnExpr):
                # item 397: the NARROW lift of the phase-1 spawn-only rule. A
                # let-effect in a provide-method body is additionally admitted
                # when its acquire is a result-declared host verb (today:
                # exactly `insert_if_absent`) on an existing host local. The
                # bind names a checked VALUE (`Bool`), not a request-scoped
                # instance: there is no handle and no nested teardown scope, so
                # the effect-and-undo pair joins the activation frame's teardown
                # accumulator exactly as the unbound method-time `insert` does
                # today (demo/components/user_cache.rvl) — only the result gains
                # a (typed) name. The general method-body acquisition stays
                # deferred (docs/design/397-insert-if-absent.md §The one grammar
                # extension).
                acquire = _lower_expr(mstmt.acquire, env, mode="setup")
                result_type = _host_result_type(acquire, env)
                if result_type is None:
                    raise RevlError(
                        filename, mstmt.line,
                        "only `spawn` may be acquired inside a provide-method body",
                        hint="a request-scoped instance gets a nested teardown "
                             "scope; other acquisitions belong in the activation "
                             "body (docs/design-v2-instances.md). A result-declared "
                             "host verb (a Bool compare-and-set like "
                             "`insert_if_absent`) may be bound here")
                if mstmt.bind in env.params or mstmt.bind in method_locals:
                    raise RevlError(filename, mstmt.line,
                                    f"`{mstmt.bind}` is already bound in `{method.name}`")
                safe = _safe_name(mstmt.bind,
                                  set(env.params.values()) | set(method_locals.values()))
                method_locals[mstmt.bind] = safe
                env.params[mstmt.bind] = safe  # visible to later statements
                # a checked Bool value, NOT a host receiver: entered in the
                # type env so a pure `if`/ternary on it typechecks, and kept
                # OUT of host_locals (it has no verb surface).
                env.type_env[safe] = result_type
                mbody.append(_lower_effect_step(
                    acquire, mstmt.undo, env, filename, mstmt.line,
                    bind=safe, raw_acquire=mstmt.acquire))
            elif isinstance(mstmt, LetEffect):
                # item zero (docs/design-v2-instances.md): a spawn inside a
                # provide-method is a request-scoped instance. It gets its own
                # nested teardown scope (its child fiber), so `s.dispose()`
                # reclaims it when the instance dies, not when the component
                # tears down. Only `spawn` may be acquired here in phase 1 —
                # a general method-body acquisition is a separate feature.
                if mstmt.bind in env.params or mstmt.bind in method_locals:
                    raise RevlError(filename, mstmt.line,
                                    f"`{mstmt.bind}` is already bound in `{method.name}`")
                safe = _safe_name(mstmt.bind,
                                  set(env.params.values()) | set(method_locals.values()))
                acquire = _lower_expr(mstmt.acquire, env, mode="setup")
                method_locals[mstmt.bind] = safe
                env.params[mstmt.bind] = safe  # visible to later statements
                # record the handle's `Instance[C]` type so a later `s.<key>`
                # provision read (docs/design-v2-instances.md) resolves here too
                handle_type = infer_ir(acquire, env.type_env, env.types, env.services)
                if handle_type is not None:
                    env.type_env[safe] = handle_type
                undo = _lower_expr(mstmt.undo, env, mode="undo")
                mbody.append({"step": "let-effect", "bind": safe,
                              "acquire": acquire, "undo": undo})
            elif isinstance(mstmt, EffectStmt):
                if isinstance(mstmt.acquire, SpawnExpr):
                    raise RevlError(
                        filename, mstmt.line,
                        "`spawn` must be bound to a handle: "
                        f"`let s = effect spawn {mstmt.acquire.component} … undo s.dispose()`",
                    )
                # item 318 (docs/design/243-witnessed-externs.md): a witnessed
                # effect is now valid in a provide-method body — THE dominant
                # H1 gate. An agent's fs mutation fires per tool call from a
                # provide-method, not the activation body; its declared inverse
                # auto-registers into the ENCLOSING COMPONENT'S activation frame
                # as a transactional entry (`Frame.transactional_method`), which
                # is component-long and whose commit/abort already discharges on
                # a clean unload / reverts on abort (Slice 2a). A witnessed call
                # carries no site `undo`; a plain effect still requires one.
                # `_lower_effect_step` makes exactly that distinction (the same
                # call the activation body uses): a witnessed acquisition lowers
                # to a `{"step": "effect", "acquire": ...}` with no `undo` key
                # (emit's `_method_witnessed_step` keys the transactional
                # registration off the acquisition's callee), and a plain
                # missing-undo effect raises the unchanged G4 refusal.
                acquire = _lower_expr(mstmt.acquire, env, mode="setup")
                mbody.append(_lower_effect_step(
                    acquire, mstmt.undo, env, filename, mstmt.line,
                    bind=None, raw_acquire=mstmt.acquire))
            elif isinstance(mstmt, EmitStmt):
                mbody.append(_lower_emit_step(mstmt, env))
            elif isinstance(mstmt, AwaitStmt):
                if not decl.async_:
                    raise RevlError(
                        filename, mstmt.line,
                        "`await` is only allowed in a component body",
                        hint="a provide method runs while the component is ACTIVE; iteration "
                             "boundaries (paper §4.3.2) exist only during activation (A1)",
                    )
                mbody.append({"step": "await", "expr": _lower_expr(mstmt.expr, env, mode="setup")})
            elif isinstance(mstmt, LetStmt):
                # a plain value binding: name an intermediate result instead
                # of nesting every call into a single expression
                if mstmt.name in env.params or mstmt.name in method_locals:
                    raise RevlError(filename, mstmt.line,
                                    f"`{mstmt.name}` is already bound in `{method.name}`")
                safe = _safe_name(mstmt.name, set(env.params.values()) | set(method_locals.values()))
                value = _lower_expr(mstmt.value, env, mode="setup")
                # item 404: raise on a definite operator/index/builtin misuse in
                # the bound value, uniformly with a `fn`/`test` body.
                swept = _sweep(value, mstmt.line)
                method_locals[mstmt.name] = safe
                env.params[mstmt.name] = safe  # visible to later statements
                if mstmt.type is not None:
                    # a `let x: T` annotation in a provide-method body. It is
                    # recorded rather than ignored so `infer_ir` sees it, but
                    # this is stratum 3: the value is *not* checked against it
                    # (docs/function-types.md §limits).
                    check_type_wellformed(filename, mstmt.line, mstmt.type)
                    env.type_env[safe] = mstmt.type
                    # ... and it pins an empty-collection literal on the right:
                    # `var m: Map[Str, Int] = Map.empty()` is the author's own
                    # expected type (roadmap 76b), carried on the `maplit` node
                    _pin_empty_literal(mstmt.type, value)
                else:
                    # record the inferred type exactly as the activation-body
                    # setup sweep does, so a later method on the binding is
                    # checked against the *real* type (`let xs = [1, 2]` then
                    # `xs.length()` is a List length, not an unpinned call) —
                    # the stdlib-named-method guard (roadmap 75(b)) depends on
                    # provable receiver types.
                    if swept is not None:
                        env.type_env[safe] = swept
                mbody.append({"step": "let", "name": safe, "value": value,
                              "mutable": bool(mstmt.mutable)})
            elif isinstance(mstmt, AssignStmt):
                if mstmt.name not in method_locals:
                    _reject_foreign_name(mstmt.name, filename, mstmt.line)  # item 384
                    raise RevlError(filename, mstmt.line,
                                    f"`{mstmt.name}` is not declared in `{method.name}`",
                                    hint="declare it with `let` (single-assignment) or "
                                         "`var` (mutable)")
                assigned = _lower_expr(mstmt.value, env, mode="setup")
                _sweep(assigned, mstmt.line)  # item 404
                mbody.append({"step": "assign", "name": method_locals[mstmt.name],
                              "value": assigned})
            elif isinstance(mstmt, ReturnStmt):
                if mstmt.expr is None:
                    # a void operation: `fn f(x) { return }`
                    if decl.returns:
                        raise RevlError(
                            filename, mstmt.line,
                            f"`{method.name}` returns `{decl.returns}` but this "
                            "`return` carries no value")
                    mbody.append({"step": "return", "expr": None})
                    returned = True
                    continue
                # a hole in return position takes the *service's* declared
                # return type: the service is the source of truth for the
                # signature (A6), so it is also the source of the obligation
                pin_hole(mstmt.expr, decl.returns, filename,
                         f"`{method.name}` returns")
                lowered_return = _lower_expr(mstmt.expr, env, mode="setup")
                # item 308, B1 clause 3: a resource-tainted return escapes the
                # activation across a signature. Three shapes are admitted, all
                # borrow-CREATING moves rather than borrow-escapes:
                #   * the OWNER returning its OWN activation handle (owner-holds);
                #   * a FRESH MINT — a direct call to a resource CONSTRUCTOR none
                #     of whose arguments carry a resource (a factory returning a
                #     newly-acquired handle; the caller becomes its owner, the
                #     transfer case whose full teardown-migration surface is
                #     deferred). This keeps resource-constructor factories, e.g.
                #     the WIT-import codegen's `fn open(p) = wit_open(p)`,
                #     compiling — refusing them would be a false-positive wall.
                # A bare BORROW (a parameter/local handle) or a tainted CARRIER
                # (`Session` wrapping a `Sock`, `wrap(c)` threading a borrow) is
                # refused. Keys on the tainted return TYPE, not the bare handle.
                _taint308, _ = _resource_ctx(env.types)
                _ret_res = (resource_in(decl.returns, _taint308)
                            or _node_resource(lowered_return, env, _taint308))
                if _ret_res and not _b1_return_admitted(lowered_return, env, _taint308):
                    raise RevlError(
                        filename, mstmt.line,
                        _b1_message("return", "borrowed", _ret_res),
                        hint=_b1_hint("return"), code="G7",
                        category="ownership")
                # item 404: sweep the returned value with the raising oracle so
                # an internal operator/index/builtin misuse is refused uniformly
                # with a `fn`/`test` body, not only the return-type mismatch.
                actual = _sweep(lowered_return, mstmt.line)
                if decl.returns:
                    if actual and not compatible(decl.returns, actual):
                        raise mismatch(filename, mstmt.line,
                                       f"`{method.name}` returns", decl.returns, actual)
                    lowered_return = _inject_opt(decl.returns, actual, lowered_return)
                mbody.append({"step": "return", "expr": lowered_return})
                returned = True
            else:  # pragma: no cover
                raise RevlError(filename, mstmt.line, "unexpected statement in method body")
        if decl.returns and not returned:
            # same guarantee as a `fn` with a declared return, on the other
            # surface that has one: the emitted java method and rust trait impl
            # both fall off the end ("missing return statement" / E0308) while
            # python hands the caller a silent None
            raise RevlError(
                comp.source or filename, method.line,
                f"`{method.name}` implements `{svc.name}.{method.name}`, which "
                f"returns `{render_type(decl.returns)}`, but this body never "
                f"returns a value",
                hint=f"end the body with `return <{render_type(decl.returns)}>` — a "
                     f"provider must produce what its service promises, or "
                     f"consumers bound to `{svc.name}` receive nothing (rust E0308, "
                     'java "missing return statement")',
                code="T1", category="type-mismatch",
                expected=decl.returns, actual=None,
            )
        # item 308: O1/B1 over the provide-method body — run BEFORE the param
        # types are restored out of `env.type_env`, so a parameter handle (a
        # borrow) is typed and detectable. An acquire bound in the method is
        # method-owned (its own `undo` is exempt).
        _ownership_walk_method(mbody, env, comp.source or filename, method.line)
        safe_params = [env.params[p] for p in method.params]
        env.params = saved
        env.type_env = saved_tenv

        # A service declaration is an *upper bound* on its providers' effects:
        # consumers bind to the service, not to this component, and a provider
        # may be purer than declared but never less. Without this, a plain
        # declaration hides an irreversible call from every consumer — and from
        # the G8 audit, which enumerates a caller's emissions by reading the
        # declarations of the methods it calls.
        if not decl.emission:
            caused_steps: dict[str, list] = {}
            caused, caps_used = _method_emissions(mbody, env, caused_steps)
            if caused:
                evidence = ", ".join(f"`{item}`" for item in caused)
                # the derivation for the *first* culprit: the message already
                # names them all, and one worked example is what the author
                # needs to see. The chain runs method -> ... -> emission.
                chain = caused_steps.get(caused[0]) or []
                why = None
                if chain:
                    head = TraceStep(method.name, "provide-method",
                                     comp.source or filename, method.line,
                                     f"provision `{stmt.key}`")
                    why = WhyTrace(kind="emission-propagation",
                                   subject=f"{svc.name}.{method.name}",
                                   steps=[head, *chain], shape=CHAIN)
                hint = (
                    f"a service declaration bounds what its providers may do — "
                    f"mark it `emission fn {method.name}(...)` in service "
                    f"`{svc.name}`, or move the irreversible call out of this "
                    f"method (G4)"
                )
                if "*" in caps_used:
                    # the capability set carries `*`: somewhere in the chain a
                    # call dispatches through a first-class function value
                    # (an arrow-typed parameter or binding), so the analysis
                    # cannot name what that call runs. Say so — the author is
                    # looking at a helper that looks pure and is not.
                    hint += (
                        ". This trace crosses a first-class dispatch: a call "
                        "invokes a function *value* (an arrow-typed parameter "
                        "or binding) rather than a named `fn`, so revl cannot "
                        "statically bound what it runs and must treat the "
                        "whole chain as possibly emitting"
                    )
                raise RevlError(
                    # the offending body lives in the component's own file,
                    # which is not the merged program filename
                    comp.source or filename, method.line,
                    f"`{svc.name}.{method.name}` is declared plain, but this "
                    f"implementation reaches {evidence}",
                    hint=hint,
                    code="G4", category="emission-propagation",
                    why=why,
                )
        elif decl.capabilities is not None:
            # the same upper bound, one refinement finer: `emission[db]` says
            # *where* a provider may cross, so the body's capability set must
            # be a subset. A provider using fewer capabilities is purer than
            # declared, which is the sound direction (docs/capabilities.md).
            caused, used = _method_emissions(mbody, env)
            extra = sorted(used - set(decl.capabilities))
            if extra:
                declared = ", ".join(decl.capabilities)
                offending = ", ".join(
                    "an unnameable host boundary" if cap == "*" else f"`{cap}`"
                    for cap in extra)
                evidence = ", ".join(f"`{item}`" for item in caused)
                raise RevlError(
                    comp.source or filename, method.line,
                    f"`{svc.name}.{method.name}` is declared "
                    f"`emission[{declared}]`, but this implementation emits "
                    f"through {offending}"
                    + (f" (reaching {evidence})" if evidence else ""),
                    hint=_capability_hint(svc.name, method.name,
                                          decl.capabilities, extra),
                    code="G4", category="emission-capability",
                )

        # sync/async arrow polymorphism (item 342): in a SYNC method, redirect
        # each call of a colour-polymorphic fn that receives only genuinely-sync
        # arrows to a sync monomorph, so the call no longer reaches an async
        # callable. Runs before the A1 admission below, whose `async_callables`
        # membership it thereby clears for the lifted case. An async method is
        # left untouched — item 92's coercion (loop stays async) is unchanged.
        if not decl.async_ and env.colour_polymorphic:
            _monomorphize_sync_callback_calls(mbody, env)

        # item 388: caller-decided extern colour. Record the colour each
        # poly-extern (`fn|async`) call site requests, and in a SYNC method
        # rewrite the call to the extern's `_revl_sync` clone. Runs in BOTH
        # colours, right beside the item-342 hook and BEFORE the A1 admission
        # below: the sync rewrite clears the poly extern's async membership so a
        # sync method calling it is not refused, while an async method's call
        # stays at the pre-seeded async name and is awaited (admitted here).
        if env.poly_externs:
            _resolve_poly_extern_calls(mbody, env, bool(decl.async_))

        # async coloring (docs/design/async-extern.md §3): an async call is
        # admitted only inside a provide method whose service operation is
        # declared `async fn`. The admission tests membership in the phase-2
        # *colored* set (async externs and any fn that transitively reaches
        # one), not just direct extern calls, so a sync method reaching a
        # colored helper fn is refused too — the twin of the emission-
        # propagation diagnostic above. The service declaration is the upper
        # bound on its providers, so asynchrony, like emission-ness, is a
        # declared property (A1).
        if env.async_callables:
            called: set = set()
            values: set = set()
            _calls_in(mbody, called, values=values)

            def _async_kind(name: str) -> str:
                return "extern" if name in env.async_externs else "function"

            def _async_locate(name: str):
                cdecl = (env.async_externs.get(name)
                         or (env.emission_evidence._decls.get(name)
                             if env.emission_evidence is not None else None))
                ev = env.emission_evidence
                return (ev.locate(cdecl) if ev is not None and cdecl is not None
                        else (None, getattr(cdecl, "line", None)))

            # a first-class reference to an async callable is refused in every
            # context, even an async method: an arrow type carries no color, so
            # the emitter cannot know to await it (async-extern.md §3, "refused,
            # not widened").
            passed_async = sorted(values & env.async_callables)
            if passed_async:
                culprit = passed_async[0]
                raise RevlError(
                    comp.source or filename, method.line,
                    f"`{svc.name}.{method.name}` uses async {_async_kind(culprit)} "
                    f"`{culprit}` as a function value, but an async callable has no "
                    f"arrow type",
                    hint="call it directly from an async context — an arrow type "
                         "carries no async color, so a suspension cannot be awaited "
                         "through it (A1)",
                    code="A1", category="async-propagation",
                )
            called_async = sorted(called & env.async_callables)
            if called_async and not decl.async_:
                culprit = called_async[0]
                cfile, cline = _async_locate(culprit)
                head = TraceStep(method.name, "provide-method",
                                 comp.source or filename, method.line,
                                 f"provision `{stmt.key}`")
                tail = TraceStep(culprit, f"async-{_async_kind(culprit)}",
                                 cfile, cline, f"async {_async_kind(culprit)}")
                why = WhyTrace(kind="async-propagation",
                               subject=f"{svc.name}.{method.name}",
                               steps=[head, tail], shape=CHAIN)
                evidence = ", ".join(f"`{name}`" for name in called_async)
                reached = (f"async extern {evidence}"
                           if culprit in env.async_externs
                           else f"async function {evidence}")
                raise RevlError(
                    comp.source or filename, method.line,
                    f"`{svc.name}.{method.name}` is declared sync, but this "
                    f"implementation reaches {reached} — a sync "
                    f"method has no in-flight window (A1)",
                    hint=f"declare the operation `async fn {method.name}(...)` in "
                         f"service `{svc.name}`, or move the suspending call out of "
                         f"this method",
                    code="A1", category="async-propagation",
                    why=why,
                )

        # item 117 (finding #40): the name-based reach above is blind to an
        # async *service operation* reached through a required key — the
        # complement of the async-callable case. A sync provide method that
        # reaches such an op in ANY position (a `let x = emit m.op(...)`
        # statement, or nested in a ternary arm / expression) has no in-flight
        # window to await it in, so it is refused — the expression-position
        # complement of item 141, which awaits the same emission inside an
        # *async* method (that path, `decl.async_`, is deliberately left
        # admitted here). Not guarded by `env.async_callables`: an async svc op
        # can be reached with no async externs/colored fns present at all.
        if not decl.async_:
            reached_ops: list = []
            _reached_async_req_ops(mbody, env, reached_ops)
            if reached_ops:
                req_name, op_method, op_decl = reached_ops[0]
                culprit = f"{req_name}.{op_method}"
                head = TraceStep(method.name, "provide-method",
                                 comp.source or filename, method.line,
                                 f"provision `{stmt.key}`")
                tail = TraceStep(culprit, "async-operation",
                                 getattr(op_decl, "source", None),
                                 getattr(op_decl, "line", None),
                                 "async service operation")
                why = WhyTrace(kind="async-propagation",
                               subject=f"{svc.name}.{method.name}",
                               steps=[head, tail], shape=CHAIN)
                raise RevlError(
                    comp.source or filename, method.line,
                    f"`{svc.name}.{method.name}` is declared sync, but this "
                    f"implementation reaches async operation `{culprit}` — a "
                    f"sync method has no in-flight window (A1)",
                    hint=f"declare the operation `async fn {method.name}(...)` "
                         f"in service `{svc.name}`, or move the suspending call "
                         f"out of this method",
                    code="A1", category="async-propagation",
                    why=why,
                )

        # item 92: a sync-typed arrow in this method that reaches an async
        # operation is the finding-#21 leak — a compile error now, not an
        # unawaited coroutine at runtime. Admitted (async-flagged) arrows are
        # skipped inside the walk.
        _refuse_leaky_arrow(mbody, env, comp.source or filename, method.line)

        methods.append({"name": method.name, "params": safe_params, "body": mbody})

    missing = set(svc.methods) - implemented
    if missing:
        name = sorted(missing)[0]
        raise RevlError(filename, stmt.line,
                        f"provision `{stmt.key}` is missing method `{name}` declared by service {svc.name}")

    return {"step": "provide", "name": stmt.key, "service": svc.name, "methods": methods}


# ---------------------------------------------------------------- expressions

def _lower_let_approval(stmt: LetApprovalStmt, env: Env) -> dict:
    """`let a = await approval[C] { fields }` -> the `approval` body step (item
    246). Binds `a` at type `Approval[C]` and lowers the field expressions (the
    human's evidence). The suspension itself is the runtime's job; the checker's
    is to make `a` an unforgeable `Approval[C]` and to record C and the fields."""
    request = stmt.request
    fields = [[name, _lower_expr(fexpr, env, mode="pure")]
              for name, fexpr in request.fields]
    safe = env.bind_local(stmt.bind, stmt.line)
    env.type_env[safe] = f"Approval[{request.capability}]"
    return {"step": "approval", "bind": safe, "capability": request.capability,
            "fields": fields}


def _lower_emit_step(stmt: EmitStmt, env: Env) -> dict:
    node = _lower_expr(stmt.expr, env, mode="emit")
    if not _is_emission_call(node, env):
        desc = _node_desc(node)
        raise RevlError(env.filename, stmt.line,
                        f"`emit` on {desc}, which is not declared `emission`",
                        hint="only calls to `emission` service operations cross the boundary; "
                             "drop the `emit` marker (G4)")
    step = {"step": "emit", "expr": node}
    if stmt.compensate is not None:
        # compensation is teardown-position: emissions are permitted bare (A5)
        comp = _lower_expr(stmt.compensate, env, mode="undo")
        step["compensate"] = comp
        # item 308, B1 clause 5 (compensate half): a compensation runs in
        # teardown Phase 2, AFTER Phase 1 closed every bracket, so ANY resource
        # value there (owned OR borrowed) is use-after-close by phase order.
        # O1 also refuses a declared inverse smuggled into a compensate.
        _o1_check(comp, env, env.filename, stmt.line, position="compensate")
        _b1_no_resource(comp, env, env.filename, stmt.line,
                        clause="compensate", borrows_only=False)
    _lower_emit_approval(stmt, node, step, env)
    return step


def _approval_scope_of(expr_type: str | None) -> str | None:
    """The capability scope `C` of a value typed `Approval[C]`, or None when the
    type is not an approval. `Approval[payment]` -> `"payment"`."""
    if not isinstance(expr_type, str):
        return None
    head, args = parse_type(expr_type)
    if head == "Approval" and len(args) == 1:
        return args[0]
    return None


def _lower_emit_approval(stmt: EmitStmt, node: dict, step: dict, env: Env) -> None:
    """Thread the `with e` edge onto the emit step and enforce the checker
    obligation (item 246, Decision 3). Two halves:

      * if the emit carries `with e`, `e` must type `Approval[C']`; the edge
        (`{"capability": C'}`) is recorded so the runtime frame check and the
        admission walk can read what the crossing was approved for;
      * every capability this crossing crosses that is DECLARATION-approval-
        required (an extern that declared `requires approval`) must be covered by
        that edge, else lowering refuses — the declaration-owned floor that holds
        with no policy file. Policy-owned requirements are the same shape, checked
        at admission over the recorded edge (the policy is not known here)."""
    crossed = _emit_crossed_caps(node, env)
    edge_scope = None
    if stmt.approval is not None:
        appr_node = _lower_expr(stmt.approval, env, mode="pure")
        appr_type = infer_ir(appr_node, env.type_env, env.types, env.services)
        edge_scope = _approval_scope_of(appr_type)
        if edge_scope is None:
            raise RevlError(
                env.filename, stmt.line,
                f"`with` on `emit` expects an `Approval[C]` value, but the "
                f"expression has type {appr_type or 'unknown'}",
                hint="thread the value produced by `await approval[C] { ... }` "
                     "(or an `Approval[C]` parameter) — nothing else produces an "
                     "approval (item 246)")
        step["approval"] = {"capability": edge_scope, "expr": appr_node}
    required = (env.types.get(APPROVAL_KEY) or {}).get("required") or set()
    for token in crossed:
        if token not in required:
            continue
        if edge_scope is None or not _approval_covers(edge_scope, token):
            raise RevlError(
                env.filename, stmt.line,
                f"crossing capability `{token}` requires approval, but this "
                f"`emit` carries no covering `with` edge",
                hint=f"acquire an approval — `let a = await approval[{token}] "
                     f"{{ ... }}` — and thread it: `emit … with a` (item 246, "
                     f"Decision 3, unreachable-without)",
                code="G4", category="approval")


def _lower_endorse_approval(expr: "ExprEndorse", env: Env, scope: dict,
                            callables: set, pure_only: bool) -> dict | None:
    """Resolve an `endorse[...](...) with <appr>` approval edge (item 249 Slice C,
    on the item 246 surface). `<appr>` must type `Approval[C]`; the recorded edge
    `{capability: C}` is what a `capability declassify.<origin> requires approval`
    policy rule checks coverage against, exactly as an `emit … with a` edge is.

    Returns None when the endorse carries no `with`."""
    if expr.approval is None:
        return None
    from .parser import ExprVar as _EV  # noqa: PLC0415 — lazy, avoids cycle
    appr_node = _lower_component_pure_expr(_EV(expr.approval, expr.line), env,
                                           scope, callables, pure_only)
    appr_type = infer_ir(appr_node, env.type_env, env.types, env.services)
    edge_scope = _approval_scope_of(appr_type)
    if edge_scope is None:
        raise RevlError(
            env.filename, expr.line,
            f"`endorse ... with` expects an `Approval[C]` value, but "
            f"`{expr.approval}` has type {appr_type or 'unknown'}",
            hint="thread the value produced by `await approval[declassify."
                 f"{expr.origin}] {{ ... }}` — nothing else produces an approval "
                 "(item 246/249)")
    return {"capability": edge_scope, "expr": appr_node}


def _lower_timer_step(stmt: TimerStmt, env: Env) -> dict:
    """`every`/`after` -> a `timer` body step (item 57, docs/time-coeffect.md).

    A timer is a revertible schedule; the step carries the delay and the
    emissions the firing runs. The body's `emit` statements lower through the
    *same* `_lower_emit_step` and the *same* `env` as a top-level emit, so the
    firing is bounded by exactly the component's declared capabilities — a
    timer body reaching an undeclared service is refused at G1/G4 like any other
    emission, and `_collect_emit_caps` (which recurses into the nested `body`)
    surfaces the firing's reach to the G8 audit as component reach. The schedule
    itself is not an emission (crossing time is not crossing the system
    boundary); its inverse is the runtime's cancellation, derived like any other
    effect teardown, so it carries no `undo` slot in the IR."""
    body = [_lower_emit_step(inner, env) for inner in stmt.body]
    step = {
        "step": "timer",
        "mode": stmt.mode,
        "interval_ms": stmt.interval_ms,
        "body": body,
    }
    # async colouring (item 170): a timer body reaching an async op — a
    # req-target async service operation (`emit agent.run_in(...)`, the
    # scheduled-agent-run shape) or an async callable (an async extern / a
    # phase-2 colored fn) — is ADMITTED and coloured async, not refused. The
    # firing then opens an `Async[T]` in-flight window (item 106): the runtime
    # awaits the body on the tick and CANCELS any pending firing + in-flight
    # work on unload (the timer's revertible-effect/undo contract, now covering
    # the async case — R4/A8). A sync timer body carries no `async` key and is
    # byte-identical to before.
    if _timer_body_reaches_async(body, env):
        step["async"] = True
    return step


def _timer_body_reaches_async(body, env) -> bool:
    """True if a lowered timer body reaches a suspension (item 170): a
    req-target async service op (rule 3 of the item-92 async-reach), or a call
    of an async-colored callable — an async extern or a phase-2 colored fn
    (rule 1). Mirrors `_arrow_reaches_async`, but a timer body is a list of
    `emit` steps with no intervening arrow value, so nothing is pruned."""
    hit = False

    def walk(n):
        nonlocal hit
        if hit:
            return
        if isinstance(n, dict):
            if _req_op_is_async(n, env):
                hit = True
                return
            for value in n.values():
                walk(value)
        elif isinstance(n, list):
            for value in n:
                walk(value)

    walk(body)
    if hit:
        return True
    called: set = set()
    _calls_in(body, called, stop_async_arrows=True)
    return bool(called & (getattr(env, "async_callables", None) or set()))


def _instance_get_call(node: dict, env: Env):
    """If `node` is a method call whose receiver is a provision read off a
    spawn handle (`s.<key>.<method>(...)`), return `(instance_get, decl)`:
    the `instance-get` node and the `MethodDecl` for the method on the service
    that key yields — else `None`.

    A provision method call does not lower to a `req`-target call; the pure
    expression stratum lowers `s.<key>` to an `instance-get`, and the trailing
    `.<method>(...)` wraps it in a `field` callee (`_lower_component_pure_expr`
    ExprCall fall-through). So the receiver is reached through `callee`, not the
    `target` slot a required-service call uses."""
    if node.get("kind") != "call":
        return None
    callee = node.get("callee")
    if not (isinstance(callee, dict) and callee.get("kind") == "field"):
        return None
    recv = callee.get("target")
    if not (isinstance(recv, dict) and recv.get("kind") == "instance-get"):
        return None
    svc = env.services.get(recv.get("service"))
    if svc is None:
        return None
    decl = svc.methods.get(callee.get("name"))
    return (recv, decl) if decl is not None else None


def _is_emission_call(node: dict, env: Env) -> bool:
    # an `emission` extern (or a function reaching one) is a boundary
    # crossing exactly as a service emission is, so `emit` marks it too
    if node.get("kind") == "fn" and node.get("name") in getattr(env, "emitting_fns", ()):
        return True
    if node.get("kind") != "call":
        return False
    target = node.get("target")
    if target is None:
        # a provision method call off a spawn handle carries its receiver in
        # `callee` (an `instance-get`), not the `req` `target` slot — walk the
        # handle's provision to the service and read the method's emission-ness
        # there rather than assuming a `req` target (which KeyError'd here)
        inst = _instance_get_call(node, env)
        return inst is not None and inst[1].emission
    if target.get("kind") != "req":
        return False
    svc = env.services[env.requires[target["name"]]]
    decl = svc.methods.get(node["method"])
    return decl is not None and decl.emission


def _lower_lease_step(stmt: "LetEffect", env: Env, filename: str) -> dict:
    """Lower `let l = effect lease <cap> [ttl <d>] [uses <n>] undo l.revoke()`
    (item 294 Slice 2) to a `let-effect` step whose acquire is the reserved
    runtime lease acquisition and whose undo is the own-handle revoke.

    Two dedicated IR nodes keep the lease off the host-verb surface: `lease-
    acquire` emits `_revl_frame.acquire_lease(...)` (the gate mints the grant
    from an approved ticket; the acquire only binds a handle to it), and `lease-
    revoke` emits `<handle>.revoke()` (retiring the grant by its OWN requestId on
    the LIFO teardown — the always-safe direction, revoking your own authority).
    The disposer MUST be exactly `<bind>.revoke()`: the design's scoped exemption
    is the own-requestId revoke only; any other disposer would name a grant the
    scope did not mint and belongs on the ordinary 379 gate, which no source form
    reaches."""
    acq: LeaseAcquire = stmt.acquire            # type: ignore[assignment]
    from .parser import ExprCall, ExprField, ExprVar  # noqa: PLC0415
    undo = stmt.undo
    ok = (isinstance(undo, ExprCall) and not undo.args
          and isinstance(undo.callee, ExprField)
          and undo.callee.name == "revoke"
          and isinstance(undo.callee.target, ExprVar)
          and undo.callee.target.name == stmt.bind)
    if not ok:
        raise RevlError(
            filename, acq.line,
            f"a lease's `undo` must be `{stmt.bind}.revoke()` — its own revoke",
            hint="a lease is torn down by revoking the grant it acquired; the "
                 "own-requestId revoke is the only exempt disposer (item 294). "
                 "A disposer naming another grant goes through the operator "
                 "revoke gate (item 379), which no source form reaches",
            code="G4", category="capability")
    safe = env.bind_local(stmt.bind, stmt.line)
    step = {
        "step": "let-effect",
        "bind": safe,
        "lease": {"capability": acq.capability,
                  "ttlMs": acq.ttl_ms, "uses": acq.uses},
        "acquire": {"kind": "lease-acquire", "capability": acq.capability,
                    "ttlMs": acq.ttl_ms, "uses": acq.uses, "line": acq.line},
        "undo": {"kind": "lease-revoke", "handle": safe, "line": acq.line},
    }
    return step


def _lower_spawn(expr: SpawnExpr, env: Env, mode: str) -> dict:
    """Lower `spawn <Component> with { ... }` to the frozen spawn IR node
    (docs/design-v2-instances.md).

    An instance is an acquisition: the node rides in the `acquire` slot of a
    `let-effect` step, the handle is that step's `bind`, and the teardown is
    its `undo`.  Instance identity + local realm are carried by `realms` — the
    keys the target provides, each isolated into a *fresh* local realm at spawn
    time so any number of instances coexist without a G2 collision (decision 3,
    5).  The target is a *template*: it never joins the static composition, so
    its provisions never enter the link-time G2/G3 table (decision 5/6)."""
    reg = getattr(env, "spawn_reg", None)
    if reg is None or expr.component not in reg["by_name"]:
        raise RevlError(
            env.filename, expr.line,
            f"`spawn {expr.component}` names an unknown component",
            hint="a spawn target is a component declared in this composition "
                 "(docs/design-v2-instances.md); a running/ambient component "
                 "cannot be spawned in phase 1",
        )
    if mode != "setup":
        # spawn is only meaningful as an acquisition; `undo`/`emit` positions
        # have no handle to bind and no teardown to invert
        raise RevlError(
            env.filename, expr.line,
            "`spawn` is only valid as an acquisition — `let s = effect spawn "
            f"{expr.component} … undo s.dispose()`",
        )
    target = reg["by_name"][expr.component]
    tconfig = {f.name: f for f in target.config}
    lowered_cfg: dict[str, dict] = {}
    for field, vexpr in expr.config.items():
        if field not in tconfig:
            raise RevlError(
                env.filename, expr.line,
                f"`{field}` is not a config field of {expr.component}",
                hint="spawn config carries the target's declared `config { }` fields",
            )
        # item 378: type-check the config VALUE against the (data-only) declared
        # config type, exactly as `load C with { … }` does. Without this the
        # producer seam was open: a spawn config value was lowered with no
        # check_ast, so a wrong-typed (or callable-carrying) value smuggled in.
        check_ast(vexpr, tconfig[field].type, env.type_env, env.types,
                  env.filename, f"config field `{field}` of {expr.component}")
        lowered_cfg[field] = _lower_expr(vexpr, env, "setup")
        # item 308, B1 clause 6: a resource-tainted spawn config value seats the
        # handle in the child's activation for its whole lifetime (a
        # per-invocation borrow escalated to an activation-lifetime hold).
        _taint308, _ = _resource_ctx(env.types)
        _cfg_res = (resource_in(tconfig[field].type, _taint308)
                    or _node_resource(lowered_cfg[field], env, _taint308))
        if _cfg_res:
            raise RevlError(
                env.filename, expr.line,
                _b1_message("spawn", "borrowed", _cfg_res),
                hint=_b1_hint("spawn"), code="G7", category="ownership")
    for f in target.config:
        if f.default is None and f.name not in expr.config:
            raise RevlError(
                env.filename, expr.line,
                f"spawn {expr.component} is missing required config field `{f.name}`",
                hint=f"provide it: `spawn {expr.component} with {{ {f.name}: … }}`",
            )
    realms = sorted(key for key, _svc, _line in target.provides)
    reg["edges"].append((env.component.name, expr.component))
    reg["templates"].add(expr.component)
    return {
        "kind": "spawn",
        "component": expr.component,
        "config": lowered_cfg,
        # each provided key gets its own fresh LOCAL realm at runtime; this is
        # how per-instance non-collision is carried into every tier's IR
        "realms": realms,
        "line": expr.line,
    }


def _instance_handle_component(node: dict, env: Env) -> str | None:
    """`C` if `node` is a name bound to a spawn handle typed `Instance[C]`,
    else None.

    The type is recorded only when the current component itself binds a
    `spawn` acquisition (`env.type_env[handle] = "Instance[C]"`), so a name
    can carry it only for a handle *this* component holds. There is no surface
    that names another component's handle, which is exactly why `s.<key>`
    cannot reach a sibling's or the root's provision — it keeps instance
    addressing on the supervision tree (docs/design-v2-instances.md 1/2)."""
    if not isinstance(node, dict) or node.get("kind") != "name":
        return None
    head, args = parse_type(env.type_env.get(node.get("id")))
    if head == "Instance" and args:
        return args[0]
    return None


def _lower_instance_get(target: dict, key: str, line: int,
                        component_name: str, env: Env) -> dict:
    """Lower `s.<key>` (a provision read off a spawn handle) to the frozen
    `instance-get` IR node (docs/design-v2-instances.md).

    `<key>` must be a key the target component *provides*; reading a key it
    does not provide is a compile error. At runtime every tier resolves `key`
    through the handle's stored instance context — the private local realm the
    matching `spawn` node isolated the key into — so the read yields *that
    instance's* provision and no other's."""
    reg = getattr(env, "spawn_reg", None)
    target_decl = reg["by_name"].get(component_name) if reg else None
    provides = {k: svc for k, svc, _line in (target_decl.provides if target_decl else [])}
    if key not in provides:
        known = ", ".join(f"`{k}`" for k in sorted(provides)) or "<nothing>"
        raise RevlError(
            env.filename, line,
            f"`{key}` is not a provision of {component_name}",
            hint=f"a spawn handle reads only a key its component provides — "
                 f"{component_name} provides {known}",
        )
    return {
        "kind": "instance-get",
        "target": target,
        "component": component_name,
        "key": key,
        # the service type the key yields — the typing rule's result, frozen
        # inline so no tier re-derives it (mirrors `spawn.realms` carrying the
        # provided keys); advisory, like any host-frontier value's type
        "service": provides[key],
        "line": line,
    }


def _lower_expr(expr, env: Env, mode: str):
    """mode: 'setup' | 'undo' | 'emit'.

    Emission calls are illegal in 'setup' (must be marked `emit`; the outer
    call of an `emit` statement is verified by `_lower_emit`) and — documented
    v0 exception — permitted bare in 'undo', where the expression position
    leaves no room for a marker (DESIGN §3.5 note; the compensate slot
    arrives with IR v1/A5).
    """
    if isinstance(expr, SpawnExpr):
        return _lower_spawn(expr, env, mode)
    if isinstance(expr, Lit):
        if expr.value is None:
            raise null_error(env.filename, expr.line)
        return {"kind": "lit", "value": _str_literal_value(expr.value)}
    if isinstance(expr, Interp):
        template = []
        args = []
        for kind, value in expr.parts:
            if kind == "text":
                template.append(value.replace("$", "$$"))  # A4: literal dollars
            else:
                template.append(f"${len(args)}")
                # `value` is a full expression AST; lower it in this context
                args.append(_lower_expr(value, env, mode))
        return {"kind": "format", "template": "".join(template), "args": args}
    if isinstance(expr, Postfix):
        return _lower_postfix(expr, env, mode)
    # v2.0 unified grammar: component positions carry the full pure-expression
    # stratum; the mode/context ride on env so the shared lowering needs no
    # signature churn (finding 1 — strata compose)
    saved = getattr(env, "_expr_mode", None), getattr(env, "_plain_body", None)
    env._expr_mode, env._plain_body = mode, True
    try:
        return _lower_component_pure_expr(
            expr, env, _component_scope(env), getattr(env, "callables", set()))
    finally:
        env._expr_mode, env._plain_body = saved


def _lower_postfix(expr: Postfix, env: Env, mode: str):
    comp = env.component
    head = expr.head
    ops = list(expr.ops)

    if head == "config":
        if not ops or ops[0].args is not None:
            raise RevlError(env.filename, expr.line, "`config` is accessed as `config.<field>`")
        field = ops.pop(0)
        if field.name not in env.config_fields:
            raise RevlError(env.filename, field.line,
                            f"`{field.name}` is not a config field of {comp.name}")
        node = {"kind": "config", "field": field.name}
    elif head in env.params or head in env.locals:
        node = {"kind": "name", "id": env.params.get(head) or env.locals[head]}
    elif head in env.requires:
        if not ops or ops[0].args is None:
            raise RevlError(env.filename, expr.line,
                            f"a required service is used through its methods: `{head}.<method>(...)`")
        node = {"kind": "req", "name": head}
    elif head[:1].isupper() and ops and ops[0].args is not None:
        call = ops.pop(0)
        # the Map VALUE constructor (docs/stdlib-2.0.md §Map): a value, not
        # a host acquisition — intercepted before host_check.
        if head == "Map" and call.name == "empty":
            if call.args:
                raise RevlError(
                    env.filename, expr.line,
                    f"`Map.empty()` takes no arguments, {len(call.args)} given",
                    hint="build up an empty map with `set`: "
                         "`Map.empty().set(\"k\", v)`",
                )
            node = {"kind": "maplit", "entries": []}
            for op in ops:
                if op.args is None:
                    raise RevlError(env.filename, expr.line,
                                    "field access `.{}` is not supported in v0 — only method calls".format(op.name))
                node = {"kind": "builtin", "method": op.name,
                        "target": node,
                        "args": [_lower_expr(a, env, mode) for a in op.args]}
            return node
        host_args = [_lower_expr(a, env, mode) for a in call.args]
        host_check(f"{head}.{call.name}",
                   [infer_ir(a, env.type_env, env.types, env.services)
                    for a in host_args],
                   env.filename, expr.line)
        node = {"kind": "host", "fn": f"{head}.{call.name}", "args": host_args}
    else:
        declared = ", ".join(f"`{r}`" for r in env.requires) or "<nothing>"
        raise RevlError(
            env.filename, expr.line,
            f"`{head}` is not a declared requirement of {comp.name}",
            hint=f"component {comp.name} requires {declared} — add `requires {head}: <Service>`?",
        )

    for op in ops:
        if op.args is None:
            # The one legal bare field access in a component/method body is
            # reading a provision off a spawn handle: `s.<key>` where
            # `s : Instance[C]` and `<key>` is a key C provides. Only a name
            # the current component itself bound to a handle can carry the
            # `Instance[C]` type, so this never becomes a lookup of a sibling's
            # or the root's provision — it is the parent reaching *its own*
            # instance (supervision-tree addressing, docs/design-v2-instances.md
            # decision 1/2).
            inst = _instance_handle_component(node, env)
            if inst is not None:
                node = _lower_instance_get(node, op.name, op.line, inst, env)
                continue
            raise RevlError(env.filename, op.line,
                            f"field access `.{op.name}` is not supported in v0 — only method calls")
        if node["kind"] == "req":
            svc = env.services[env.requires[node["name"]]]
            decl = svc.methods.get(op.name)
            if decl is None:
                raise RevlError(env.filename, op.line,
                                f"`{node['name']}.{op.name}` is not a method of service {svc.name}")
            if len(op.args) != len(decl.params):
                raise RevlError(env.filename, op.line,
                                f"`{node['name']}.{op.name}` takes {len(decl.params)} "
                                f"argument(s), {len(op.args)} given")
            if decl.emission and mode == "setup":
                raise RevlError(
                    env.filename, op.line,
                    f"call to emission `{node['name']}.{op.name}` must be marked `emit` (G4)",
                    hint="an emission crosses the system boundary and cannot be reverted; "
                         "`emit` makes that visible at the call site",
                )
            lowered = [_lower_expr(a, env, mode) for a in op.args]
            for arg, (pname, ptype) in zip(lowered, decl.params):
                actual = infer_ir(arg, env.type_env, env.types, env.services)
                if ptype and actual and not compatible(ptype, actual):
                    raise mismatch(env.filename, op.line,
                                   f"`{node['name']}.{op.name}` argument `{pname}`",
                                   ptype, actual)
            node = {"kind": "call", "target": node, "method": op.name, "args": lowered}
            continue
        # host provenance (docs/stdlib-2.0.md §Map): a verb call on a local
        # bound to a host acquisition (`effect store.insert(k, v)` in an
        # activation or provide-method body) is checked against that family's
        # surface (item 401). An unknown verb is refused here (HOST-METHOD)
        # instead of lowering to a call that only crashes at the host runtime,
        # the item-84 shape. Only the FIRST verb off the host local is checked;
        # a host verb's RESULT is opaque, so a chained call reads no family.
        host_args = [_lower_expr(a, env, mode) for a in op.args]
        if node.get("kind") == "name" and node.get("id") in env.host_locals:
            _check_host_verb(
                env.host_locals[node["id"]], op.name, len(host_args),
                env.filename, op.line)
        node = {
            "kind": "call",
            "target": node,
            "method": op.name,
            "args": host_args,
        }
    return node


def _node_desc(node: dict) -> str:
    if node.get("kind") == "call":
        target = node.get("target")
        if isinstance(target, dict) and target.get("kind") == "req":
            return f"`{target['name']}.{node['method']}`"
        callee = node.get("callee")
        if isinstance(callee, dict) and callee.get("kind") == "field":
            recv = callee.get("target")
            if isinstance(recv, dict) and recv.get("kind") == "instance-get":
                # a provision method call off a spawn handle: name it the way
                # it was written, `<key>.<method>`, so the diagnostic points at
                # the provision access rather than "a call to <method>"
                return f"`{recv.get('key')}.{callee.get('name')}`"
            return f"a call to `{callee.get('name')}`"
        return f"a call to `{node.get('method')}`"
    return f"a {node.get('kind', 'value')} expression"


# ---------------------------------------------------------------- linker

def _link(program: Program, components: list[dict], ambient_components: list[dict],
          templates: set | None = None, errors: list | None = None) -> dict:
    """G2/G3 over the union of ambient (running) and new components, and the
    composition manifest (cordisc-compatible schema: components with
    name/file/inject/provides, plus loadOrder).

    `templates` names components that are *spawn targets* — runtime instances,
    not static composition members (docs/design-v2-instances.md). They are
    excluded from `entries`, so their provisions never enter the link-time
    G2/G3 table and they take no place in `loadOrder`: at runtime each is
    instantiated into its own fresh local realm by `spawn`, disjoint by
    construction (decision 5/6). Non-spawning programs have no templates, so
    the manifest is byte-identical to before.

    `errors` (item 386, Change 2): when a collect-all sink is supplied, every G2
    provision conflict, multi-realm route gap, and G3 cycle is APPENDED to it
    instead of raised, so the linker reports all of them in one pass — capped at
    one reported cycle per strongly-connected set to avoid overlapping-path
    noise. When `errors is None` (a direct caller), the first refusal is raised,
    the legacy fail-fast behavior."""
    templates = templates or set()

    def _fail(err: RevlError) -> None:
        """Collect the refusal if a sink was supplied, else raise it (legacy)."""
        if errors is None:
            raise err
        errors.append(err)

    # name-keyed, not positionally zipped: a collected duplicate-component
    # refusal (item 386) can leave `components` shorter than
    # `program.components`, which would misalign a `zip`. First declaration of
    # a name wins its line.
    lines: dict[str, int] = {}
    for decl in program.components:
        lines.setdefault(decl.name, decl.line)

    entries: list[dict] = []
    for amb in ambient_components:
        entry = {
            "name": amb.get("name"),
            "file": amb.get("file", ""),
            "inject": list(amb.get("inject") or []),
            "provides": list(amb.get("provides") or []),
        }
        if amb.get("isolate"):
            entry["isolate"] = dict(amb["isolate"])
        if amb.get("intercept"):
            entry["intercept"] = dict(amb["intercept"])
        if amb.get("routes"):
            entry["routes"] = {k: dict(v) for k, v in amb["routes"].items()}
        entries.append(entry)
    for comp in components:
        if comp["name"] in templates:
            # a spawn target is instantiated at runtime, not composed: its
            # provisions live in fresh per-instance local realms, so it never
            # participates in the static G2/G3 table or loadOrder (decision 5/6)
            continue
        entry = {
            "name": comp["name"],
            "file": comp.get("source", ""),
            "inject": sorted(comp["requires"]),
            "provides": sorted(comp["provides"]),
        }
        if comp.get("isolate"):
            entry["isolate"] = dict(comp["isolate"])
        if comp.get("intercept"):
            entry["intercept"] = dict(comp["intercept"])
        if comp.get("routes"):
            entry["routes"] = {k: dict(v) for k, v in comp["routes"].items()}
        entries.append(entry)

    def _line(name: str) -> int:
        return lines.get(name, 1)

    # why-trace locations: a locally compiled component knows its own file and
    # declaration line; an *ambient* entry (read back from a running manifest)
    # has a file but no line, so `line` stays None there rather than lying.
    by_name = {entry["name"]: entry for entry in entries}

    def _where(name: str) -> tuple[str | None, int | None]:
        entry = by_name.get(name) or {}
        file = entry.get("file") or program.filename or None
        return (file, lines.get(name))

    def _realm(entry: dict, key: str) -> str:
        return (entry.get("isolate") or {}).get(key, SHARED_REALM)

    # v2: provision disjointness is per-(key, realm) — same key in different
    # realms is the multi-tenancy feature, same realm is the conflict. The
    # realm is named only when it isn't the shared one, so v1 diagnostics
    # are unchanged.
    provider_of: dict[tuple[str, str], str] = {}
    for entry in entries:
        for key in entry["provides"]:
            realm = _realm(entry, key)
            where = "" if realm == SHARED_REALM else f" in realm `{realm}`"
            if (key, realm) in provider_of:
                first = provider_of[(key, realm)]
                # both exhibits, both locations: the message names them, the
                # trace says where to go and look (why.py)
                detail = f"provides `{key}`{where}"
                why = WhyTrace(
                    kind="provision-conflict", subject=key, shape=SET,
                    steps=[
                        TraceStep(first, "provider", *_where(first), detail),
                        TraceStep(entry["name"], "provider",
                                  *_where(entry["name"]), detail),
                    ])
                _fail(RevlError(
                    program.filename, _line(entry["name"]),
                    f"provision conflict: key `{key}`{where} is provided "
                    f"by both {provider_of[(key, realm)]} and {entry['name']} (G2)",
                    why=why,
                ))
                # keep the FIRST provider registered (item 386): the conflict is
                # reported once, and downstream route/edge resolution still finds
                # a provider for the key rather than fabricating a "no provider".
                continue
            provider_of[(key, realm)] = entry["name"]

    def _routes(entry: dict) -> dict:
        return entry.get("routes") or {}

    # multi-realm bind verification (item 162): a `realms(...)` route binds the
    # key across N named realms, and the consumer routes across them at runtime.
    # The compiler verifies EACH named realm has a provider of the key — a route
    # that names a realm with no provider would dangle at that leg. This does NOT
    # relax G2 (one provider per (key, realm) is still enforced above); it is the
    # dual existence check on the consumption side. Providers of a routed key may
    # sit in the shared realm too (`realm` unnamed) — a route target is matched
    # against the same per-(key, realm) table, so the named realm must have its
    # own provider.
    for entry in entries:
        for key, route in _routes(entry).items():
            for realm in route["realms"]:
                if (key, realm) not in provider_of:
                    strat = route.get("strategy")
                    strat_where = f" strategy(`{strat}`)" if strat else ""
                    why = WhyTrace(
                        kind="unmet-requirement", subject=key, shape=CHAIN,
                        steps=[TraceStep(entry["name"], "consumer",
                                         *_where(entry["name"]),
                                         f"routes `{key}` across "
                                         f"{len(route['realms'])} realms{strat_where}")])
                    _fail(RevlError(
                        program.filename, _line(entry["name"]),
                        f"multi-realm bind of `{key}` in {entry['name']} names realm "
                        f"`{realm}`, but no component provides `{key}` in realm `{realm}` "
                        f"(item 162: every routed realm needs a provider)",
                        hint="a `realms(...)` route distributes across backend realms; each "
                             "named realm must have its own provider of the key (G2 keeps it "
                             "to exactly one). Add a provider isolated into realm "
                             f"`{realm}`, or drop `{realm}` from the route",
                        why=why,
                    ))

    # edges: provider -> consumer where the consumer's realm for a key
    # matches the provider's — realm separation legitimately breaks cycles
    graph: dict[str, list[str]] = {entry["name"]: [] for entry in entries}
    indegree: dict[str, int] = {entry["name"]: 0 for entry in entries}
    # which key carries each provider -> consumer edge, so a cycle can say
    # *what* is being waited on at every hop and not just who waits
    edge_key: dict[tuple[str, str], str] = {}
    for entry in entries:
        # a routed key is resolved per-realm below, not through the single-realm
        # table — skip it here so a stray shared-realm provider does not shadow
        # the route's own targets.
        routed = _routes(entry)
        for key in entry["inject"]:
            if key in routed:
                # one edge per routed realm: every backend provider must be
                # ACTIVE before the consumer that routes to it (loadOrder), and
                # each leg can still surface in a cycle trace by its key.
                for realm in routed[key]["realms"]:
                    provider = provider_of.get((key, realm))
                    if provider == entry["name"]:  # pragma: no cover — a route
                        # targets a *required* key; a self-provision in a routed
                        # realm cannot arise (the key is not this comp's provision)
                        continue
                    if provider is not None:
                        graph[provider].append(entry["name"])
                        edge_key.setdefault((provider, entry["name"]), key)
                        indegree[entry["name"]] += 1
                continue
            provider = provider_of.get((key, _realm(entry, key)))
            if provider == entry["name"]:
                name = entry["name"]
                _fail(RevlError(
                    program.filename, _line(name),
                    f"component {name} requires a key it provides itself (`{key}`) (G3)",
                    why=WhyTrace(
                        kind="dependency-cycle", subject=name, shape=CHAIN,
                        steps=[TraceStep(name, "component", *_where(name),
                                         f"provides and requires `{key}`")]),
                ))
                continue
            if provider is not None:
                graph[provider].append(entry["name"])
                edge_key.setdefault((provider, entry["name"]), key)
                indegree[entry["name"]] += 1

    state: dict[str, int] = {}
    stack: list[str] = []
    # item 386, Change 2: nodes already named in a reported cycle. A single
    # strongly-connected set has many overlapping cycles; reporting each is
    # noise, so once any node of a cycle is reported we suppress further cycles
    # that touch it — one G3 per SCC.
    cycled: set = set()

    def visit(name: str):
        state[name] = 1
        stack.append(name)
        for succ in graph[name]:
            if state.get(succ) == 1:
                cycle = stack[stack.index(succ):] + [succ]
                # one step per hop: each names the key it provides onward, so
                # the closing repeat shows the edge that shuts the loop
                steps = [
                    TraceStep(node, "component", *_where(node),
                              (f"provides `{edge_key[(node, cycle[i + 1])]}`"
                               if i + 1 < len(cycle) else None))
                    for i, node in enumerate(cycle)
                ]
                cyc_err = RevlError(
                    program.filename, _line(succ),
                    "dependency cycle: " + " -> ".join(cycle) + " (G3)",
                    why=WhyTrace(kind="dependency-cycle", subject=succ,
                                 steps=steps, shape=CHAIN))
                if errors is None:
                    raise cyc_err
                if not any(node in cycled for node in cycle):
                    cycled.update(cycle)
                    errors.append(cyc_err)
            if state.get(succ, 0) == 0:
                visit(succ)
        stack.pop()
        state[name] = 2

    for entry in entries:
        if state.get(entry["name"], 0) == 0:
            visit(entry["name"])

    # providers-first load order (Kahn, stable in entry order)
    order: list[str] = []
    ready = [e["name"] for e in entries if indegree[e["name"]] == 0]
    while ready:
        name = ready.pop(0)
        order.append(name)
        for succ in graph[name]:
            indegree[succ] -= 1
            if indegree[succ] == 0:
                ready.append(succ)

    manifest = {"components": entries, "loadOrder": order}
    if templates:
        # G8: the instance dimension is dynamic. These components are not
        # statically composed — each is instantiated `× dynamic` times at
        # runtime — so they carry no loadOrder position; the audit reports
        # them separately (docs/design-v2-instances.md, decision 7).
        manifest["templates"] = sorted(templates)
    return manifest


# ------------------------------------------------------------------ spawn G4

def _find_spawn_nodes(node) -> "list[dict]":
    """Every `spawn` acquire node reachable in a lowered body subtree."""
    out: list[dict] = []
    if isinstance(node, dict):
        if node.get("kind") == "spawn":
            out.append(node)
        for value in node.values():
            out.extend(_find_spawn_nodes(value))
    elif isinstance(node, list):
        for value in node:
            out.extend(_find_spawn_nodes(value))
    return out


def _collect_emit_caps(node, caps: set) -> None:
    """The capabilities an `emit` step in a lowered body crosses. A req-keyed
    emission is capability = the key (G2 names the boundary); any host emission
    is the unnameable `*` (no `emission[...]` list can name it)."""
    if isinstance(node, dict):
        if node.get("step") == "emit":
            expr = node.get("expr") or {}
            target = expr.get("target") or {}
            if target.get("kind") == "req":
                caps.add(target.get("name"))
            else:
                caps.add("*")
        for value in node.values():
            _collect_emit_caps(value, caps)
    elif isinstance(node, list):
        for value in node:
            _collect_emit_caps(value, caps)


def _cap_keyed(key: str, cap_str: str) -> "object":
    """The key-to-token bridge (item 294): resolve a declared emission token
    string to a `Cap` keyed by the WIRING KEY, carrying the declared token's
    VALUATION. The token identity stays the wiring key (exactly today's fold
    element, so a parameter-free token (`kv_a`) yields `Cap("kv_a", ())` which
    compares bit-for-bit like the old string) while the parameter map now rides
    into the fold so a narrowing that lives only in `P` actually changes the
    verdict. Both `reach` and `held` are built through this one function, so the
    two sides of `covers` are always structured `(T, P)` valuations."""
    from . import cap_order  # noqa: PLC0415 - lazy, avoids an import cycle
    return cap_order.Cap(key, cap_order.parse_cap(cap_str).params)


def _emit_step_caps_pairs(node: dict, requires_map: dict, services: dict) -> list:
    """The `Cap`(s) a single lowered `emit` step crosses, resolved through the
    key-to-token bridge. A req-keyed emission resolves key -> requires-target
    service -> the method being called -> that method's `emission[...]`
    valuation(s); a bare or unresolvable method degrades to the bare key
    (`Cap(key, ())`, today's element); a host emission is the unnameable `*`."""
    from . import cap_order  # noqa: PLC0415 - lazy, avoids an import cycle
    expr = node.get("expr") or {}
    target = expr.get("target") or {}
    if target.get("kind") != "req":
        return [cap_order.Cap("*", ())]
    key = target.get("name")
    svc = services.get(requires_map.get(key)) if requires_map else None
    decl = svc.methods.get(expr.get("method")) if svc is not None else None
    cap_strs = getattr(decl, "capabilities", None) if decl is not None else None
    if not cap_strs:
        return [cap_order.Cap(key, ())]
    return [_cap_keyed(key, s) for s in cap_strs]


def _collect_emit_caps_pairs(node, caps: set, requires_map: dict,
                             services: dict) -> None:
    """`_collect_emit_caps`, resolved to structured `Cap`s through the bridge.
    Same traversal (emit STEPS only), so a parameter-free body yields the same
    set of elements as today, spelled as bare `Cap`s."""
    if isinstance(node, dict):
        if node.get("step") == "emit":
            caps.update(_emit_step_caps_pairs(node, requires_map, services))
        for value in node.values():
            _collect_emit_caps_pairs(value, caps, requires_map, services)
    elif isinstance(node, list):
        for value in node:
            _collect_emit_caps_pairs(value, caps, requires_map, services)


def _spawn_reached_surface_pairs(components: list[dict],
                                 services: dict) -> dict[str, set]:
    """Per-component actual capability reach as structured `(T, P)` pairs,
    resolved through the bridge: the boundaries a component's own code crosses
    (the key-and-valuation of every `emit` step, `*` for a host emission), so a
    parameterized crossing survives into the attenuation fold as its valuation
    rather than degrading to a bare wiring key."""
    surface: dict[str, set] = {}
    for comp in components:
        requires_map = comp.get("requires") or {}
        caps: set = set()
        _collect_emit_caps_pairs(comp.get("body") or [], caps, requires_map,
                                 services)
        surface[comp["name"]] = caps
    return surface


def _held_capabilities_pairs(comp: dict, base_surface: set,
                             services: dict) -> set:
    """What a component holds (the capabilities it may pass down to a child it
    spawns, item 66, lineage) as structured `(T, P)` pairs. A requires key
    resolves, through the same bridge, to the declared emission valuation(s) of
    the service it wires: a bare-token service method contributes the bare key
    `Cap(key, ())` (today's element, byte-identical), and a parameterized one
    contributes its narrower cone INSTEAD of the bare key, which is what lets a
    parent that holds `fs.write(path="/tmp")` refuse a child reaching wider. A
    plain or unresolvable service keeps the bare key (a child cannot reach a
    non-emission key, so this only preserves byte-identity)."""
    from . import cap_order  # noqa: PLC0415 - lazy, avoids an import cycle
    held: set = set(base_surface)
    for key, svcname in (comp.get("requires") or {}).items():
        svc = services.get(svcname)
        emission_methods = ([m for m in svc.methods.values() if m.emission]
                            if svc is not None else [])
        if not emission_methods:
            held.add(cap_order.Cap(key, ()))
            continue
        for m in emission_methods:
            if not m.capabilities:
                held.add(cap_order.Cap(key, ()))
            else:
                for s in m.capabilities:
                    held.add(_cap_keyed(key, s))
    return held


def _spawn_emission_surface(components: list[dict], services: dict) -> dict[str, set]:
    """Per-component upper bound on what activating an instance of it can emit.

    A conservative, sound over-approximation (decision 8): the union of every
    emission method declared by the services it provides (uncapped -> `*`), its
    own `emit` steps, and — by fixpoint over the spawn graph — everything its
    own spawned children can emit. Never under-approximates, so the bound it
    enforces on a spawner can only be tighter than the truth, never looser."""
    surface: dict[str, set] = {}
    for comp in components:
        caps: set = set()
        for _key, svcname in (comp.get("provides") or {}).items():
            svc = services.get(svcname)
            if svc is None:
                continue
            for m in svc.methods.values():
                if m.emission:
                    caps.update({"*"} if m.capabilities is None else set(m.capabilities))
        _collect_emit_caps(comp.get("body") or [], caps)
        surface[comp["name"]] = caps
    # the transitive closure over the spawn graph is applied by the caller,
    # which holds the edge list
    return surface


def _spawn_surface_closure(base: dict[str, set], edges: list) -> dict[str, set]:
    """Fold the spawn graph into a per-component base surface: after this, a
    component's set is everything it can emit *plus* everything its transitive
    spawned children can (`docs/capabilities.md`). The least fixed point the
    edge list induces — the same closure `_check_spawn_emission_bounds` takes,
    factored out so the attenuation check reads the *reachable* set of a child
    (its own emissions and its descendants') against what a spawner holds."""
    closed: dict[str, set] = {name: set(caps) for name, caps in base.items()}
    changed = True
    while changed:
        changed = False
        for parent, child in edges:
            before = len(closed.setdefault(parent, set()))
            closed[parent].update(closed.get(child, set()))
            if len(closed[parent]) != before:
                changed = True
    return closed


def _activation_spawn_sites(comp: dict) -> "list[dict]":
    """The `spawn` nodes in a component's *activation* body — the top-level
    supervision `let s = effect spawn C ...`, excluding spawns nested inside a
    `provide` method. Provide-method spawns carry an `emission[...]` clause that
    already bounds them (`_check_spawn_emission_bounds`); the activation body
    has no such clause, which is exactly the hole item 66 closes: without an
    attenuation rule a supervisor is a capability amplifier."""
    sites: list[dict] = []
    for step in comp.get("body") or []:
        if step.get("step") == "provide":
            continue
        sites.extend(_find_spawn_nodes(step))
    return sites


def _cap_offending(cap: "object") -> str:
    """Render an uncovered capability for a refusal message: the unnameable host
    boundary reads in words, everything else as its canonical `(T, P)` spelling
    (`fs.write`, `fs.write(path="/etc")`)."""
    return ("an unnameable host boundary" if cap.token == "*"
            else f"`{cap.to_str()}`")


def _widening_reason(cap: "object", held: set) -> str | None:
    """Why a child capability is not covered by any held one, when the token IS
    held but the VALUATION widens it (the item 294 case). Names the parameter
    and the direction; returns None when the token itself is absent (today's
    missing-boundary case, whose message is enough)."""
    same_token = [h for h in held if h.token == cap.token and h.token != "*"]
    if not same_token:
        return None
    child_params = cap.param_map()
    for h in same_token:
        for name, _wide in h.params:
            if name not in child_params:
                return (f"a capability parameter only narrows; `{cap.to_str()}` "
                        f"drops `{name}`, which `{h.to_str()}` binds; a dropped "
                        f"parameter is wider, so the child reaches more than the "
                        f"parent holds")
    # the token is held with the parameter bound on both sides but the value
    # widens (e.g. a path outside the held cone)
    h = same_token[0]
    return (f"a capability parameter only narrows; `{cap.to_str()}` is wider "
            f"than the held `{h.to_str()}` (its value is not within the parent's "
            f"cone)")


def _cap_sorted_strs(caps: set) -> list[str]:
    """Canonical spellings of a Cap set, sorted (the audit-chain rendering). A
    bare `Cap(key, ())` renders as `key`, byte-identical to the old string, so a
    parameter-free chain is unchanged."""
    return sorted(c.to_str() for c in caps)


def _check_spawn_attenuation(components: list[dict], services: dict,
                             spawn_reg: dict, filename: str) -> list[dict]:
    """Capability attenuation on spawn (item 66, extended by item 294): a
    spawned child's capability set must be a **checked subset** of its
    spawner's (monotone shrinkage, the direction §5 admits for purity). A spawn
    may narrow (pass down less), never widen (grant a boundary the parent does
    not hold), so spawning cannot amplify authority and each per-tenant instance
    gets least-authority for free: an instance whose template reaches only
    `kv_a` provably cannot reach `kv_b`, even when the spawner holds both.

    The fold compares structured `(T, P)` capabilities via `cap_order.covers`,
    resolved through the key-to-token bridge (`_cap_keyed`) on BOTH sides, so a
    parameterized token is actually compared: a child declaring
    `fs.write(path="/etc")` under a parent holding `fs.write(path="/tmp")` is
    refused, and a bare `fs.write` child under that parent is refused too (a
    dropped parameter widens). A parameter-free program yields bare `Cap`s that
    compare bit-for-bit like the old wiring-key strings (additive, item 294
    Slice 1). Where G4 bounds a component's declaration, item 33 the
    composition, and item 55 the operators, this bounds **lineage**.

    Applies to activation-body spawns (see `_activation_spawn_sites`); returns
    the per-instance attenuation chain (spawner → child narrowing) for the G8
    audit surface. Raises on a widening spawn, naming the chain.

    TODO(294-slice2): per-instance `with { }` literal substitution into
    `config.`-valued parameters; the chain would then show resolved paths."""
    edges = spawn_reg.get("edges") or []
    if not edges:
        return []
    from . import cap_order  # noqa: PLC0415 - lazy, avoids an import cycle
    base = _spawn_reached_surface_pairs(components, services)
    reachable = _spawn_surface_closure(base, edges)

    chain: list[dict] = []
    seen: set = set()
    for comp in components:
        held = _held_capabilities_pairs(comp, base.get(comp["name"], set()),
                                        services)
        for spawn in _activation_spawn_sites(comp):
            child = spawn.get("component")
            child_reach = reachable.get(child, set())
            extra = cap_order.covers_set(held, child_reach)
            line = spawn.get("line", comp.get("line", 1))
            if extra:
                extra = sorted(extra, key=lambda c: c.to_str())
                held_str = ", ".join(
                    f"`{s}`" for s in _cap_sorted_strs(held)) or "no capabilities"
                offending = ", ".join(_cap_offending(c) for c in extra)
                reasons = [r for r in (_widening_reason(c, held) for c in extra)
                           if r is not None]
                extra_hint = ("; " + "; ".join(reasons)) if reasons else ""
                raise RevlError(
                    comp.get("source") or filename, line,
                    f"`{comp['name']}` spawns `{child}`, granting it {offending}, "
                    f"but `{comp['name']}` holds only {held_str} — a spawn may "
                    "narrow a child's capabilities, never widen them",
                    hint="a spawned child's capability set must be covered by "
                         f"its spawner's (attenuation, item 66/294); "
                         f"`{comp['name']}` cannot pass down {offending} it does "
                         f"not hold; add the matching `requires` to "
                         f"`{comp['name']}` so it holds what it grants, or narrow "
                         f"the capability on `{child}` "
                         "(monotone shrinkage: narrowing is sound, widening is "
                         "not)" + extra_hint,
                    code="G4", category="capability-attenuation",
                )
            edge = (comp["name"], child)
            if edge in seen:
                continue
            seen.add(edge)
            # the attenuation chain per instance: what the spawner holds, what
            # the instance is granted (its reachable set), and what was dropped
            # on the way down — the least-authority proof, per lineage edge.
            chain.append({
                "parent": comp["name"],
                "child": child,
                "holds": _cap_sorted_strs(held),
                "granted": _cap_sorted_strs(child_reach),
                "attenuated": _cap_sorted_strs(held - child_reach),
                "line": line,
            })
    return chain


def _check_spawn_emission_bounds(components: list[dict], services: dict,
                                 spawn_reg: dict, filename: str) -> None:
    """G4/G6 across the spawn boundary (decision 8): a spawner's declared
    emission bound must cover what its spawned instances emit, so emissions
    cannot escape their bound by being moved into a spawned child.

    Only *bounded* spawn sites are constrained — a spawn inside a provide-method
    whose service declares `plain` or `emission[caps]`. A spawn in an activation
    body has no emission clause to widen (as body-level `emit` has none), so it
    is unconstrained here, exactly as today."""
    if not spawn_reg.get("edges"):
        return
    surface = _spawn_emission_surface(components, services)
    # fold the spawn graph into each surface (transitive closure)
    edges = spawn_reg["edges"]
    changed = True
    while changed:
        changed = False
        for parent, child in edges:
            before = len(surface.get(parent, set()))
            surface.setdefault(parent, set()).update(surface.get(child, set()))
            if len(surface[parent]) != before:
                changed = True

    for comp in components:
        for step in comp.get("body") or []:
            if step.get("step") != "provide":
                continue
            key = step.get("name")
            svc = services.get((comp.get("provides") or {}).get(key) or "")
            if svc is None:
                continue
            for method in step.get("methods") or []:
                decl = svc.methods.get(method.get("name"))
                if decl is None:
                    continue
                for spawn in _find_spawn_nodes(method.get("body") or []):
                    target = spawn.get("component")
                    tsurf = surface.get(target, set())
                    if not tsurf:
                        continue
                    line = spawn.get("line", step.get("line", 1))
                    where = f"{svc.name}.{method.get('name')}"
                    if not decl.emission:
                        evidence = ", ".join(f"`{c}`" for c in sorted(tsurf))
                        raise RevlError(
                            comp.get("source") or filename, line,
                            f"`{where}` is declared plain, but it spawns "
                            f"`{target}`, which emits through {evidence}",
                            hint="a spawner's emission bound must cover its "
                                 "instances' — declare `emission fn "
                                 f"{method.get('name')}(...)` on service `{svc.name}`, "
                                 "or move the spawn out of this method (G4)",
                            code="G4", category="emission-propagation",
                        )
                    if decl.capabilities is not None:
                        extra = sorted(tsurf - set(decl.capabilities))
                        if extra:
                            declared = ", ".join(decl.capabilities) or "no capabilities"
                            offending = ", ".join(
                                "an unnameable host boundary" if c == "*" else f"`{c}`"
                                for c in extra)
                            raise RevlError(
                                comp.get("source") or filename, line,
                                f"`{where}` is declared `emission[{declared}]`, but it "
                                f"spawns `{target}`, which emits through {offending}",
                                hint="a spawner's emission bound must cover its "
                                     f"instances' — widen `emission[...]` on service "
                                     f"`{svc.name}` to include {offending}, or move the "
                                     "spawn out of this method (G4)",
                                code="G4", category="emission-capability",
                            )
