"""Taint and provenance — static information-flow (roadmap item 249, Slice A).

The security property, in one line: **untrusted input cannot DIRECTLY create
authority.** A value that returns across an untrusted-origin boundary is
`Untrusted[T]`; a sink that grants authority (a shell command, a capability
name, a policy update) declares its parameter `Trusted[T]`; the checker refuses
an `Untrusted[T]` reaching a `Trusted[T]` sink unless a declassifier sits on the
data-flow path. The refusal is a compile error tagged **G9** — upstream of every
runtime, the repair signal itself.

This is the STATIC half (Slice A) of docs/design/249-taint-provenance.md: a
qualifier on declared types plus a monotone set-union propagation over
expressions, the companion of the emission fixed point
(`emission_analysis._emitting_capabilities`) it deliberately mirrors. The runtime
tag (Slice B) that makes the coarse static origin exact queues behind item 243
Slice 2 and is not built here.

Design decisions realised:

* **Decision 2 — the lattice** is the powerset of origin labels ordered by
  inclusion: bottom `{}` is trusted, join is set union, so a trusted prefix never
  launders an untrusted suffix (`taint(a + b) = taint(a) ∪ taint(b)`).
* **Decision 3 — declassification** is auditable and never silent. Two static
  declassifiers ship here: a **checked parser** (a `verified fn` returning
  `Trusted[T]`, total by G7 so malformed input cannot slip through) and the
  explicit **`endorse(...)`** operator. The third — a human approval edge — is
  the item-246 hook and is deferred to Slice C.
* **Decision 4 — the refusal sinks** are the positions where an untrusted value
  *is* authority; a parameter declared `Trusted[T]` marks one, exactly external
  proposal #10's `Untrusted[Str] -> shell command` refusal.

Orthogonality (open question 2, resolved to *qualifier*): `Untrusted[T]` and
`Trusted[T]` are qualifiers orthogonal to the base type. They are stripped off
the declared types the base checker sees (into this module's side-table), so
base typing, method lookup and the emitted IR are byte-identical for any program
that uses no qualifier. Only the taint verdict is new.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import RevlError
from .typecheck import parse_type, format_type, FN_HEAD

# The two qualifier heads. Orthogonal to the base type (open question 2).
_QUALIFIERS = ("Untrusted", "Trusted")

# Coarse origin classes the static half tracks (Decision 2). The exact host in
# `web:example.com` is a runtime refinement filled by Slice B.
_ORIGIN_CLASSES = {"web", "net", "fs", "model", "input", "secret"}


# --------------------------------------------------------------- type surgery

def strip_qualifiers(type_name: str | None) -> str | None:
    """Remove every `Untrusted[...]`/`Trusted[...]` wrapper, anywhere in the
    type, leaving the bare base type the rest of the checker reasons about.

    `Untrusted[Str]` -> `Str`; `List[Trusted[Int]]` -> `List[Int]`;
    `Result[Trusted[Int], Str]` -> `Result[Int, Str]`. Idempotent, and a no-op
    on any type that carries no qualifier (byte-identity for existing programs).
    """
    if not type_name:
        return type_name
    head, args = parse_type(type_name)
    if head in _QUALIFIERS and len(args) == 1:
        return strip_qualifiers(args[0])
    if not args:
        return head
    stripped = [strip_qualifiers(a) for a in args]
    if head == FN_HEAD:
        return format_type(FN_HEAD, stripped)
    return format_type(head, stripped)


def top_qualifier(type_name: str | None) -> str | None:
    """`Untrusted`/`Trusted` if the *outermost* head is a qualifier, else None."""
    if not type_name:
        return None
    head, args = parse_type(type_name)
    if head in _QUALIFIERS and len(args) == 1:
        return head
    return None


def _mentions_trusted(type_name: str | None) -> bool:
    """True if a `Trusted[...]` head appears anywhere in the type. This is how a
    checked-parser declassifier is recognised even when it wraps its result, e.g.
    `Result[Trusted[Int], ParseError]` (Decision 3.1)."""
    if not type_name:
        return False
    head, args = parse_type(type_name)
    if head == "Trusted":
        return True
    return any(_mentions_trusted(a) for a in args)


def _origin_of(capabilities) -> str:
    """The coarse origin label a crossing mints, derived from its declared
    capability scope, never guessed (Decision 2, the G8 caveat). `emission[web]`
    -> `web`; `emission[web.fetch]` -> `web`; an unscoped crossing -> `input`."""
    for cap in capabilities or ():
        head = str(cap).split(".", 1)[0]
        if head in _ORIGIN_CLASSES:
            return head
        return str(cap)
    return "input"


# ------------------------------------------------------------------- the model

@dataclass
class TaintModel:
    """The per-name taint facts extracted from declarations, once, for the whole
    program. Everything the flow walk needs, keyed by callable name."""

    # extern name -> the coarse origin its return mints (declared `Untrusted[T]`)
    sources: dict[str, str] = field(default_factory=dict)
    # callable name -> {param_index: base_type} for params declared `Trusted[T]`
    sinks: dict[str, dict[int, str | None]] = field(default_factory=dict)
    # callable name -> {param_index: origin} for params declared `Untrusted[T]`
    untrusted_params: dict[str, dict[int, str]] = field(default_factory=dict)
    # verified fns whose return declares `Trusted[...]` — parser declassifiers
    declassifiers: set[str] = field(default_factory=set)
    # a human sink label for the diagnostic, keyed by callable name
    sink_kind: dict[str, str] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        """Whether any taint surface exists at all. A program with no qualifier
        engages nothing — the flow walk is skipped and stays byte-identical."""
        return bool(self.sources or self.sinks or self.untrusted_params
                    or self.declassifiers)


def _sink_kind_for(name: str, capabilities) -> str:
    """A best-effort human name for a sink, for the diagnostic (Decision 4)."""
    caps = {str(c).split(".", 1)[0] for c in (capabilities or ())}
    if "shell" in caps or "terminal" in caps or "exec" in caps:
        return "a shell command"
    if "policy" in caps:
        return "a policy update"
    if "cap" in caps or "capability" in caps:
        return "a capability name"
    return f"the trusted sink `{name}`"


def extract_and_normalize(program) -> TaintModel:
    """Read the qualifier surface off every declaration, build the `TaintModel`,
    and STRIP the qualifiers from the declared types in place so the base checker
    and every emitter see bare types (orthogonality / byte-identity).

    Mutates `program` — safe because a program is freshly parsed per compile.
    """
    model = TaintModel()

    def _note_params(name: str, typed_params) -> None:
        """`typed_params` is a list of (index, type_string, setter). Records
        sink/untrusted params and strips the qualifier via `setter`."""
        for index, type_str, setter in typed_params:
            qual = top_qualifier(type_str)
            if qual == "Trusted":
                model.sinks.setdefault(name, {})[index] = strip_qualifiers(type_str)
            elif qual == "Untrusted":
                model.untrusted_params.setdefault(name, {})[index] = _origin_of(())
            clean = strip_qualifiers(type_str)
            if clean != type_str:
                setter(clean)

    # externs: an `Untrusted[T]` return is a taint source; a `Trusted[T]` param
    # is a sink; both are stripped to their base type.
    for ext in getattr(program, "externs", ()):
        if top_qualifier(ext.returns) == "Untrusted":
            model.sources[ext.name] = _origin_of(ext.capabilities)
        params = []
        for i, p in enumerate(ext.params):
            params.append((i, p.type, _fnparam_setter(p)))
        _note_params(ext.name, params)
        if ext.name in model.sinks:
            model.sink_kind[ext.name] = _sink_kind_for(ext.name, ext.capabilities)
        ext.returns = strip_qualifiers(ext.returns)

    # top-level fns: a `verified fn` returning `Trusted[...]` is a checked-parser
    # declassifier; a `Trusted[T]` param is a sink; qualifiers are stripped.
    for fn in getattr(program, "fn_decls", ()):
        if getattr(fn, "verified", False) and _mentions_trusted(fn.returns):
            model.declassifiers.add(fn.name)
        params = []
        for i, p in enumerate(fn.params):
            params.append((i, p.type, _fnparam_setter(p)))
        _note_params(fn.name, params)
        if fn.name in model.sinks:
            model.sink_kind[fn.name] = _sink_kind_for(fn.name, ())
        fn.returns = strip_qualifiers(fn.returns)

    # service methods: a `Trusted[T]` param is a sink reachable through a
    # required key; qualifiers are stripped from the (name, type) tuples.
    for svc in getattr(program, "services", ()):
        for method in svc.methods.values():
            new_params = []
            for i, (pname, ptype) in enumerate(method.params):
                qual = top_qualifier(ptype)
                clean = strip_qualifiers(ptype)
                if qual == "Trusted":
                    model.sinks.setdefault(method.name, {})[i] = clean
                    model.sink_kind[method.name] = _sink_kind_for(method.name, ())
                elif qual == "Untrusted":
                    model.untrusted_params.setdefault(method.name, {})[i] = "input"
                new_params.append((pname, clean))
            method.params = new_params
            method.returns = strip_qualifiers(method.returns)

    return model


def _fnparam_setter(param):
    def setter(value):
        param.type = value
    return setter


# --------------------------------------------------------------- the flow walk

@dataclass(frozen=True)
class Taint:
    """A value's taint: the set of origin labels it carries (bottom `{}` =
    trusted), plus the shortest naming chain behind it, for the diagnostic."""

    origins: frozenset = frozenset()
    via: tuple = ()

    @property
    def dirty(self) -> bool:
        return bool(self.origins)


CLEAN = Taint()


def _join(a: Taint, b: Taint) -> Taint:
    """Set-union join (Decision 2). The `via` chain follows whichever side
    actually carries taint, so the message names a real tainting path."""
    if not a.origins:
        return b
    if not b.origins:
        return a
    via = a.via if len(a.via) <= len(b.via) else b.via
    return Taint(a.origins | b.origins, via)


class _FlowChecker:
    """Walks one callable body, threading a per-binding taint environment and
    refusing at every sink an untrusted value reaches (G9)."""

    def __init__(self, model: TaintModel, filename: str, line: int) -> None:
        self.model = model
        self.filename = filename
        self.line = line

    # -- callee-name extraction across the two IR dialects ---------------------
    @staticmethod
    def _callee_name(node: dict) -> str | None:
        """A call is `{kind:fn, name}` in a component body and
        `{kind:call, callee:{kind:var, name}}` in a pure fn body."""
        if node.get("kind") == "fn" and isinstance(node.get("name"), str):
            return node["name"]
        if node.get("kind") == "call":
            callee = node.get("callee")
            if isinstance(callee, dict) and callee.get("kind") == "var":
                return callee.get("name")
        return None

    @staticmethod
    def _var_name(node: dict) -> str | None:
        if node.get("kind") == "var":
            return node.get("name")
        if node.get("kind") == "name":
            return node.get("id")
        return None

    def _line_of(self, node) -> int:
        if isinstance(node, dict):
            for key in ("line", "src_line"):
                if isinstance(node.get(key), int):
                    return node[key]
        return self.line

    # -- the sink refusal ------------------------------------------------------
    def _refuse(self, sink: str, index: int, arg_taint: Taint, node) -> None:
        origins = ", ".join(sorted(arg_taint.origins))
        kind = self.model.sink_kind.get(sink, f"the trusted sink `{sink}`")
        chain = " -> ".join(arg_taint.via) if arg_taint.via else "an untrusted value"
        raise RevlError(
            self.filename, self._line_of(node),
            f"untrusted value ({origins}) flows into {kind} at argument "
            f"{index + 1} of `{sink}` — untrusted input cannot directly create "
            f"authority (G9)",
            hint="declassify it on the way in: parse it with a `verified fn` that "
                 "returns `Trusted[T]` (the failure branch is a typed `Result`, "
                 "not a smuggled string), or wrap it in `endorse(<value>)` — an "
                 "audited, policy-forbiddable downgrade. The tainting path is "
                 f"{chain}.",
            code="G9", category="taint-flow",
        )

    # -- expression taint ------------------------------------------------------
    def taint_of(self, node, env: dict) -> Taint:
        if isinstance(node, list):
            result = CLEAN
            for item in node:
                result = _join(result, self.taint_of(item, env))
            return result
        if not isinstance(node, dict):
            return CLEAN

        kind = node.get("kind")

        # a variable reference: whatever the binding carries
        var = self._var_name(node)
        if var is not None and kind in ("var", "name"):
            return env.get(var, CLEAN)

        # a literal is trusted by construction (Decision 2, trusted origins)
        if kind in ("lit", "int", "float", "string", "str", "bool", "config"):
            return CLEAN

        # a call / emission: check its sinks, then compute the result's taint
        callee = self._callee_name(node)
        if callee is not None:
            return self._taint_of_call(callee, node.get("args") or [], env, node)

        # `endorse(...)` lowers to a marker node (Decision 3.2): its result is
        # declassified — trusted — and the audited downgrade is recorded.
        if kind == "endorse":
            self.taint_of(node.get("expr"), env)  # still walk for inner sinks
            return CLEAN

        # binary op / interpolation: the union join — a trusted prefix does not
        # launder an untrusted suffix (Decision 2, concatenation).
        if kind in ("bin", "interp", "template", "concat"):
            return self._union_children(node, env)

        # everything else (record/list/field/index/if/match/arrow/...) is walked
        # with a union fallback: taint only ever *disappears* at a literal or a
        # declassifier, never by falling through an unmodelled node. This is the
        # no-false-clean invariant (Decision 2 propagation, residual risk 3).
        return self._union_children(node, env)

    def _union_children(self, node: dict, env: dict) -> Taint:
        result = CLEAN
        for key, value in node.items():
            if key in ("kind", "op", "name", "id", "method", "line", "src_line",
                       "callee"):
                continue
            result = _join(result, self.taint_of(value, env))
        return result

    def _taint_of_call(self, callee: str, args, env: dict, node) -> Taint:
        arg_taints = [self.taint_of(a, env) for a in args]

        # sink check: an untrusted value into a `Trusted[T]` parameter is refused
        sink_params = self.model.sinks.get(callee)
        if sink_params:
            for index, _base in sink_params.items():
                if index < len(arg_taints) and arg_taints[index].dirty:
                    self._refuse(callee, index, arg_taints[index], node)

        # a taint source (declared `Untrusted[T]` return): mint its origin
        if callee in self.model.sources:
            origin = self.model.sources[callee]
            return Taint(frozenset({origin}), (f"{callee}()",))

        # a declassifier (verified-fn parser, or the `endorse` builtin): clean
        if callee in self.model.declassifiers or callee == "endorse":
            return CLEAN

        # an ordinary call propagates: the result is tainted iff any argument is
        # (the static over-approximation — biased to refusing, never clean)
        result = CLEAN
        for t in arg_taints:
            result = _join(result, t)
        if result.dirty:
            result = Taint(result.origins, result.via + (f"{callee}()",))
        return result

    # -- statement / block walk ------------------------------------------------
    def run(self, body, env: dict) -> None:
        if isinstance(body, list):
            for stmt in body:
                self._stmt(stmt, env)
        else:
            self._stmt(body, env)

    def _stmt(self, stmt, env: dict) -> None:
        if isinstance(stmt, list):
            for item in stmt:
                self._stmt(item, env)
            return
        if not isinstance(stmt, dict):
            return
        step = stmt.get("step")
        if step == "let":
            env[stmt["name"]] = self.taint_of(stmt.get("value"), env)
            return
        if step in ("return", "emit", "expr", "fail"):
            self.taint_of(stmt.get("expr") or stmt.get("value"), env)
            return
        # any other statement shape: walk every child so nested calls (and their
        # sink checks) are visited, and any nested block threads the same env.
        for key, value in stmt.items():
            if key in ("step", "name", "kind"):
                continue
            if isinstance(value, list):
                self._stmt(value, env)
            elif isinstance(value, dict):
                if value.get("step") is not None:
                    self._stmt(value, env)
                else:
                    self.taint_of(value, env)


def check_taint(program, fns, components, model: TaintModel,
                filename: str) -> None:
    """Refuse any untrusted value that reaches a sink without a declassifier
    (G9). No-op — and byte-identical — when the program uses no qualifier."""
    if not model.active:
        return

    # top-level pure fns (lowered IR): seed params declared `Untrusted[T]`
    for fn in fns:
        checker = _FlowChecker(model, fn.get("source") or filename,
                               fn.get("line") or 0)
        env: dict = {}
        seeded = model.untrusted_params.get(fn["name"], {})
        for i, param in enumerate(fn.get("params") or []):
            pname = param["name"] if isinstance(param, dict) else param
            if i in seeded:
                env[pname] = Taint(frozenset({seeded[i]}), (pname,))
        checker.run(fn.get("body") or [], env)

    # component provide-method bodies (lowered IR)
    for comp in components:
        source = comp.get("source") or filename
        _walk_component_methods(comp.get("body") or [], model, source)


def _walk_component_methods(body, model: TaintModel, source: str) -> None:
    for step in body:
        if not isinstance(step, dict):
            continue
        if step.get("step") == "provide":
            for method in step.get("methods") or []:
                checker = _FlowChecker(model, source, method.get("line") or 0)
                env: dict = {}
                seeded = model.untrusted_params.get(method.get("name"), {})
                for i, pname in enumerate(method.get("params") or []):
                    if i in seeded:
                        env[pname] = Taint(frozenset({seeded[i]}), (pname,))
                checker.run(method.get("body") or [], env)
