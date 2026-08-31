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

import re
from dataclasses import dataclass, field

from .errors import RevlError
from .typecheck import parse_type, format_type, FN_HEAD

# a qualifier head standing on its own (not the tail of a longer identifier such
# as a user type `MyTrusted[T]`), used only as the byte-identity fast-path guard
_QUALIFIER_RE = re.compile(r"(?<![A-Za-z0-9_])(?:Untrusted|Trusted)\[")

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
    if not type_name or not _has_qualifier(type_name):
        # No qualifier anywhere: return the string VERBATIM. Never round-trip a
        # qualifier-free type through parse_type/format_type — that would
        # renormalise spacing (`Map[Str,Int]` -> `Map[Str, Int]`) and break
        # byte-identity for programs that use no taint annotation.
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


def _has_qualifier(type_name: str | None) -> bool:
    """Whether any `Untrusted[...]`/`Trusted[...]` qualifier head appears, as a
    standalone head. The byte-identity fast path: a type with none is returned
    verbatim, never round-tripped through parse_type/format_type."""
    return bool(type_name) and _QUALIFIER_RE.search(type_name) is not None


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
    # Slice C: callable name -> the origins it is DECLARED to be allowed to
    # `endorse[<origin>]` in its body (the `endorse[web] fn ...` slot, or the
    # slot on the service operation a provide method implements). An `endorse`
    # whose origin is not in this set is refused at admission.
    declared_endorse: dict[str, frozenset] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        """Whether any taint surface exists at all. A program with no qualifier
        and no endorse slot engages nothing — the flow walk is skipped and stays
        byte-identical."""
        return bool(self.sources or self.sinks or self.untrusted_params
                    or self.declassifiers or self.declared_endorse)


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
        endorse_origins = getattr(fn, "endorse_origins", frozenset())
        if endorse_origins:
            model.declared_endorse[fn.name] = frozenset(endorse_origins)
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
            endorse_origins = getattr(method, "endorse_origins", frozenset())
            if endorse_origins:
                # keyed by the operation name — a provide method implementing it
                # resolves to the same key (see `_callables`), so the method
                # inherits the operation's declared declassification rights.
                model.declared_endorse[method.name] = frozenset(endorse_origins)
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

# A parameter marker (Slice B). During interprocedural inference each parameter
# of a callable is seeded with a symbolic origin `\x00p<i>` so the walk can read
# off which parameters reach the return value or a sink — the transfer function
# the signature fixed point iterates. The prefix is a NUL byte so it can never
# collide with a real origin label (`web`, `net`, ...) or a user type.
_PARAM_PREFIX = "\x00p"


def _param_marker(index: int) -> str:
    return f"{_PARAM_PREFIX}{index}"


def _param_index(origin: str) -> int | None:
    """The parameter index behind a marker origin, or None for a real origin."""
    if isinstance(origin, str) and origin.startswith(_PARAM_PREFIX):
        return int(origin[len(_PARAM_PREFIX):])
    return None


@dataclass(frozen=True)
class Taint:
    """A value's taint: the set of origin labels it carries (bottom `{}` =
    trusted), plus the shortest naming chain behind it, for the diagnostic.

    `fields` gives per-field taint for a record value (Slice B, field-granular
    reads): a field read takes the field's own taint, not the record join, so a
    clean field of a partly-untrusted record stays clean. It is empty for every
    non-record value, and is dropped on `_join` (a joined value is no longer a
    known record shape), so the record's `origins` union remains the sound
    over-approximation whenever the field structure is lost."""

    origins: frozenset = frozenset()
    via: tuple = ()
    fields: dict = field(default_factory=dict)

    @property
    def dirty(self) -> bool:
        return bool(self.origins)


CLEAN = Taint()


def _join(a: Taint, b: Taint) -> Taint:
    """Set-union join (Decision 2). The `via` chain follows whichever side
    actually carries taint, so the message names a real tainting path. Field
    structure is not preserved across a join — only the origin union is."""
    if not a.origins:
        return b
    if not b.origins:
        return a
    via = a.via if len(a.via) <= len(b.via) else b.via
    return Taint(a.origins | b.origins, via)


@dataclass
class _Signature:
    """The inferred taint signature of a callable (Slice B), the transfer
    function the interprocedural fixed point converges to. All three fields are
    monotone (grow-only) so the fixed point terminates over the finite lattice.

    * `flows_to_return`: parameter indices whose taint reaches the return value;
    * `mints`: concrete origins the body itself joins into the return (from the
      sources it calls) — flows even when every argument is clean;
    * `reaches_sink`: parameter index -> `(sink_name, sink_kind, via)`, the sinks
      an argument reaches transitively through any chain of calls, with the
      cross-body naming chain for the G9 diagnostic.
    """

    flows_to_return: set = field(default_factory=set)
    mints: set = field(default_factory=set)
    reaches_sink: dict = field(default_factory=dict)

    def merge(self, flows: set, mints: set, sink_hits: dict) -> bool:
        """Fold one body-walk's findings in. Returns whether the monotone part
        (the parameter sets and the sink key set) grew — the fixed point's
        `changed` signal. A shorter `via` for an already-known sink refines the
        message in place WITHOUT signalling change, so via refinement cannot
        make the iteration oscillate."""
        changed = False
        if not flows <= self.flows_to_return:
            self.flows_to_return |= flows
            changed = True
        if not mints <= self.mints:
            self.mints |= mints
            changed = True
        for index, hit in sink_hits.items():
            prev = self.reaches_sink.get(index)
            if prev is None:
                self.reaches_sink[index] = hit
                changed = True
            elif len(hit[2]) < len(prev[2]):
                self.reaches_sink[index] = hit  # shorter chain, not a growth
        return changed


class _FlowChecker:
    """Walks one callable body, threading a per-binding taint environment.

    Two modes over the same transfer function (Slice B):

    * refusal mode (`infer=False`, the default): the landed behaviour — refuse
      at every sink an untrusted value reaches (G9), now *including* sinks a
      callee's inferred signature says an argument reaches transitively;
    * inference mode (`infer=True`): parameters are seeded with symbolic markers
      and the walk RECORDS which reach the return value or a sink instead of
      refusing, producing the callable's `_Signature`.
    """

    def __init__(self, model: TaintModel, filename: str, line: int,
                 signatures: dict | None = None, infer: bool = False,
                 qualname: str = "", endorse_allowed: frozenset = frozenset(),
                 endorse_label: str = "") -> None:
        self.model = model
        self.filename = filename
        self.line = line
        self.signatures = signatures or {}
        self.infer = infer
        self.qualname = qualname
        # Slice C: the origins the enclosing declaration is allowed to `endorse`,
        # and a human label for the declassify record (the fn / `Component.method`
        # name). An `endorse[o]` with `o` not in `endorse_allowed` is refused.
        self.endorse_allowed = endorse_allowed
        self.endorse_label = endorse_label
        # provenance for the G8 audit surface (Decision 5): the origins that
        # reach an emission here, and the origins declassified here. Populated as
        # the walk proceeds; folded onto the component's IR entry by the caller.
        self.reaches: set[str] = set()
        self.declassified: set[str] = set()
        # Slice C: the enriched declassify records — `{origin, method, reason,
        # line, approved}` — that ride beside the coarse `declassify:` token so
        # the audit surface shows why each downgrade was granted.
        self.declassify_records: list[dict] = []
        # inference-mode accumulators (Slice B): the taint that reaches a return
        # statement, and, per parameter index, the sink an argument reaches with
        # the naming chain behind it.
        self.return_taint: Taint = CLEAN
        self.sink_hits: dict[int, tuple] = {}

    # -- callee-name extraction across the two IR dialects ---------------------
    @staticmethod
    def _callee_name(node: dict) -> str | None:
        """The callable name a node invokes, across every dialect:
          * `{kind:fn, name}`                           a component host call
          * `{kind:call, callee:{kind:var, name}}`      a pure-fn call
          * `{kind:call, target:{kind:req}, method}`    a required-service method
            (`emit s.run(page)`) — the sink is keyed by the METHOD name, so a
            `Trusted[T]` service-operation parameter is enforced too.
        """
        if node.get("kind") == "fn" and isinstance(node.get("name"), str):
            return node["name"]
        if node.get("kind") == "call":
            callee = node.get("callee")
            if isinstance(callee, dict) and callee.get("kind") == "var":
                return callee.get("name")
            if isinstance(node.get("method"), str):
                return node["method"]
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
    def _check_sinks(self, callee: str, arg_taints: list, node) -> None:
        """Both sink tiers at a call site: a directly-declared `Trusted[T]`
        parameter (landed), and a parameter the callee's inferred signature
        proves reaches a sink transitively (Slice B)."""
        sink_params = self.model.sinks.get(callee)
        if sink_params:
            kind = self.model.sink_kind.get(callee, f"the trusted sink `{callee}`")
            for index in sink_params:
                if index < len(arg_taints) and arg_taints[index].dirty:
                    # a direct sink: the naming chain ends at the sink itself.
                    self._on_sink(callee, kind, index, arg_taints[index], node,
                                  (callee,))
        sig = self.signatures.get(callee)
        if sig:
            for index, (sink_name, kind, via) in sig.reaches_sink.items():
                if index < len(arg_taints) and arg_taints[index].dirty:
                    # a transitive sink: the callee's inferred cross-body chain.
                    self._on_sink(sink_name, kind, index, arg_taints[index],
                                  node, via)

    def _on_sink(self, sink_name: str, kind: str, index: int, arg_taint: Taint,
                 node, internal_via: tuple) -> None:
        """A tainted argument has reached a sink. In inference mode record which
        parameter markers reached it (extending the naming chain with this
        callable's own qualified name); in refusal mode raise G9."""
        if self.infer:
            for origin in arg_taint.origins:
                pidx = _param_index(origin)
                if pidx is None:
                    continue
                via = (self.qualname,) + internal_via if self.qualname else internal_via
                prev = self.sink_hits.get(pidx)
                if prev is None or len(via) < len(prev[2]):
                    self.sink_hits[pidx] = (sink_name, kind, via)
            return
        origins = ", ".join(sorted(o for o in arg_taint.origins
                                   if _param_index(o) is None))
        chain_parts = arg_taint.via + internal_via
        chain = " -> ".join(chain_parts) if chain_parts else "an untrusted value"
        raise RevlError(
            self.filename, self._line_of(node),
            f"untrusted value ({origins}) flows into {kind} at argument "
            f"{index + 1} of `{sink_name}` — untrusted input cannot directly "
            f"create authority (G9)",
            hint="declassify it on the way in: parse it with a `verified fn` that "
                 "returns `Trusted[T]` (the failure branch is a typed `Result`, "
                 "not a smuggled string), or endorse it at a declared point — "
                 "`endorse[<origin>](<value>, reason = \"...\")`, an audited, "
                 "policy-forbiddable downgrade the enclosing declaration must "
                 f"grant. The tainting path is {chain}.",
            code="G9", category="taint-flow",
        )

    # -- the scoped declassifier (Slice C) -------------------------------------
    def _endorse(self, node, arg_taints: list) -> Taint:
        """A scoped `endorse[<origin>](v, reason=...)`: authorise the downgrade
        against the enclosing declaration's declared slot, record it on the audit
        surface, and return CLEAN. In inference mode it is a pure clean-out (the
        signature fixed point does not enforce or record)."""
        meta = node.get("endorse") if isinstance(node, dict) else None
        origin = meta.get("origin") if isinstance(meta, dict) else None
        if self.infer:
            return CLEAN
        # the declared-slot authorisation: the enclosing fn / operation must
        # declare `endorse[<origin>]`, or the downgrade is refused at admission —
        # a declassification is never ambient (Slice C, the whole claim).
        if origin is not None and origin not in self.endorse_allowed:
            where = self.endorse_label or "this declaration"
            declared = ", ".join(f"`endorse[{o}]`"
                                 for o in sorted(self.endorse_allowed)) or "none"
            raise RevlError(
                self.filename, self._line_of(meta if isinstance(meta, dict) else node),
                f"undeclared declassification: `endorse[{origin}]` is used in "
                f"`{where}`, but its declaration does not grant it — a downgrade "
                f"must appear in the enclosing declaration (G9)",
                hint=f"declare the slot on the enclosing `fn`/operation — "
                     f"`endorse[{origin}] fn ...` (or `emission endorse[{origin}] "
                     f"fn ...` on the service operation). It declares {declared} "
                     f"today. A declared endorse is auditable and "
                     f"policy-forbiddable (item 249, Slice C).",
                code="G9", category="taint-declassify",
            )
        # record the downgraded origin on the audit surface (Decision 5).
        value_taint = arg_taints[0] if arg_taints else CLEAN
        if origin is not None:
            self.declassified.add(origin)
        else:
            self.declassified |= {o for o in value_taint.origins
                                  if _param_index(o) is None}
        if isinstance(meta, dict):
            approval = meta.get("approval") or {}
            self.declassify_records.append({
                "origin": origin,
                "method": self.endorse_label,
                "reason": meta.get("reason"),
                "line": meta.get("line"),
                **({"approved": approval.get("capability")}
                   if approval.get("capability") else {}),
            })
        # origin-precise downgrade: `endorse[<origin>]` clears ONLY the declared
        # origin, so a value carrying a second, un-endorsed origin is still
        # refused at a sink (a downgrade is scoped, never a blanket clean). A
        # bare (originless) endorse falls back to a full clean.
        if origin is None:
            return CLEAN
        residual = frozenset(o for o in value_taint.origins if o != origin)
        return Taint(residual, value_taint.via) if residual else CLEAN

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

        # record construction is field-granular (Slice B): each field carries its
        # own taint, so a later read of a clean field stays clean even when a
        # sibling field is untrusted. The record value's `origins` is still the
        # union, so passing the whole record to a sink refuses.
        if kind == "record":
            fields: dict = {}
            origins: frozenset = frozenset()
            via: tuple = ()
            for pair in node.get("fields") or []:
                if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
                    continue
                fname, fval = pair[0], pair[1]
                ft = self.taint_of(fval, env)
                fields[fname] = ft
                if ft.dirty:
                    origins = origins | ft.origins
                    if not via:
                        via = ft.via
            return Taint(origins, via, fields)

        # a field read takes the field's own taint when the record shape is
        # known; otherwise it falls back to the record join (no-false-clean).
        if kind == "field":
            target = self.taint_of(node.get("target"), env)
            fname = node.get("name")
            if fname in target.fields:
                return target.fields[fname]
            return Taint(target.origins, target.via)

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

        # sink checks (both tiers): a directly-declared `Trusted[T]` parameter,
        # and a parameter a callee's inferred signature reaches transitively.
        self._check_sinks(callee, arg_taints, node)

        # a taint source (declared `Untrusted[T]` return): mint its origin
        if callee in self.model.sources:
            origin = self.model.sources[callee]
            return Taint(frozenset({origin}), (f"{callee}()",))

        # the scoped `endorse[<origin>]` declassifier (Slice C): the downgrade is
        # granted only where the enclosing declaration declared the slot, its
        # reason lands on the audit surface, and the origin token feeds
        # `audit --diff` / `may not declassify` policy exactly as before.
        if callee == "endorse":
            return self._endorse(node, arg_taints)

        # a declassifier (verified-fn parser): clean. Record the origins it
        # downgrades onto the audit surface (Decision 5) — a `declassify:` token
        # an auditor and `revl audit --diff` can see. Only real origins are
        # recorded; inference markers are internal bookkeeping.
        if callee in self.model.declassifiers:
            for t in arg_taints:
                self.declassified |= {o for o in t.origins if _param_index(o) is None}
            return CLEAN

        # a callable with an inferred signature (a top-level fn or a provide
        # method): propagate precisely — the result carries the taint of exactly
        # the arguments that flow to the return, plus the origins the body mints.
        sig = self.signatures.get(callee)
        if sig is not None:
            origins: frozenset = frozenset(sig.mints)
            via: tuple = (f"{callee}()",) if sig.mints else ()
            for index in sig.flows_to_return:
                if index < len(arg_taints) and arg_taints[index].dirty:
                    origins = origins | arg_taints[index].origins
                    if not via:
                        via = arg_taints[index].via + (f"{callee}()",)
            if origins:
                return Taint(origins, via)
            return CLEAN

        # an ordinary opaque call (an extern with no declared source, a builtin):
        # the result is tainted iff any argument is — the static
        # over-approximation, biased to refusing, never clean (no-false-clean).
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
            result = self.taint_of(stmt.get("expr") or stmt.get("value"), env)
            if step == "return" and self.infer:
                # inference mode: the return value's taint (parameter markers
                # and minted origins) feeds `flows_to_return` and `mints`.
                self.return_taint = _join(self.return_taint, result)
            if step == "emit" and result.dirty:
                # a value of these origins reaches an emission here (Decision 5).
                # An *absolute-refusal* sink already raised above; what remains is
                # the policy-gated tier (e.g. web-taint into `send.*`) — recorded,
                # so `audit --diff` sees a newly-routed exfiltration edge widen.
                self.reaches |= result.origins
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


def _param_names(params) -> list[str]:
    """Parameter names from a lowered param list (dicts for fns, bare strings
    for provide methods)."""
    return [p["name"] if isinstance(p, dict) else p for p in (params or [])]


def _callables(fns, components, filename: str):
    """Every callable whose body inference walks (Slice B): top-level fns and
    component provide methods. Yields `(qualname, key, params, body, source,
    line)`, where `key` is the name a call site resolves to (the fn name or, for
    a provide method, the service operation name) and `qualname` is the human
    name for the diagnostic chain (`Component.method`)."""
    for fn in fns:
        yield (fn["name"], fn["name"], _param_names(fn.get("params")),
               fn.get("body") or [], fn.get("source") or filename,
               fn.get("line") or 0)
    for comp in components:
        source = comp.get("source") or filename
        cname = comp.get("name") or "?"
        for step in comp.get("body") or []:
            if not isinstance(step, dict) or step.get("step") != "provide":
                continue
            for method in step.get("methods") or []:
                mname = method.get("name")
                yield (f"{cname}.{mname}", mname,
                       _param_names(method.get("params")),
                       method.get("body") or [], source, method.get("line") or 0)


def _infer_signatures(fns, components, model: TaintModel, filename: str) -> dict:
    """Infer a `_Signature` for every callable, as a least fixed point over the
    same call graph the emission analysis walks (`_emitting_capabilities`).

    Each iteration re-walks every body in INFERENCE mode with the current
    signatures, seeding each parameter with a symbolic marker (plus its declared
    `Untrusted` origin, if any), and folds the findings back with `merge`. The
    transfer is monotone and the lattice is finite (parameter indices bounded by
    arity, origins by the finite origin-class set), so the iteration terminates
    at a least fixed point — mutual recursion converges exactly as G4's emission
    fixed point does. The bound guards a malformed graph from looping forever."""
    signatures: dict[str, _Signature] = {}
    callables = list(_callables(fns, components, filename))
    for _qual, key, *_rest in callables:
        signatures.setdefault(key, _Signature())

    # the fixed point converges in at most one sweep per call-graph hop; the
    # bound is a generous safety net (a malformed graph cannot loop forever),
    # never the normal stop — `changed` breaks the loop as soon as it stabilises.
    bound = 2 * len(callables) + 10
    changed = True
    while changed and bound > 0:
        changed = False
        bound -= 1
        for qual, key, params, body, source, line in callables:
            checker = _FlowChecker(model, source, line, signatures=signatures,
                                   infer=True, qualname=qual)
            env: dict = {}
            seeded = model.untrusted_params.get(key, {})
            for i, pname in enumerate(params):
                origins = {_param_marker(i)}
                if i in seeded:
                    origins.add(seeded[i])
                env[pname] = Taint(frozenset(origins), (pname,))
            checker.run(body, env)
            flows = {i for i in range(len(params))
                     if _param_marker(i) in checker.return_taint.origins}
            mints = {o for o in checker.return_taint.origins
                     if _param_index(o) is None}
            if signatures[key].merge(flows, mints, checker.sink_hits):
                changed = True
    return signatures


def check_taint(program, fns, components, model: TaintModel,
                filename: str) -> None:
    """Refuse any untrusted value that reaches a sink without a declassifier
    (G9). No-op — and byte-identical — when the program uses no qualifier.

    Slice B: taint propagates across call boundaries. First infer a per-callable
    taint signature to a least fixed point, then make one refusal pass in which
    every call site applies the callee's signature — a tainted argument reaching
    a `reaches_sink` position is refused at the *call site*, with a via chain
    that crosses component boundaries."""
    if not model.active:
        return

    signatures = _infer_signatures(fns, components, model, filename)

    # top-level pure fns (lowered IR): seed params declared `Untrusted[T]`
    for fn in fns:
        checker = _FlowChecker(model, fn.get("source") or filename,
                               fn.get("line") or 0, signatures=signatures,
                               endorse_allowed=model.declared_endorse.get(
                                   fn["name"], frozenset()),
                               endorse_label=fn["name"])
        env: dict = {}
        seeded = model.untrusted_params.get(fn["name"], {})
        for i, param in enumerate(fn.get("params") or []):
            pname = param["name"] if isinstance(param, dict) else param
            if i in seeded:
                env[pname] = Taint(frozenset({seeded[i]}), (pname,))
        checker.run(fn.get("body") or [], env)

    # the IR carries no per-body line, so fall back to the component's declared
    # line (from the AST) — better than 0 for a component-body refusal.
    comp_lines = {c.name: getattr(c, "line", 0)
                  for c in getattr(program, "components", ())}

    # component provide-method bodies (lowered IR)
    for comp in components:
        source = comp.get("source") or filename
        reaches, declassified, records = _walk_component_methods(
            comp.get("body") or [], model, source,
            comp_lines.get(comp.get("name"), 0), signatures,
            comp.get("name") or "")
        # fold the per-component provenance onto the IR entry (Decision 5), so
        # `_boundary` can emit `taint:`/`declassify:` tokens. Additive: absent
        # when the component touches no taint, so its IR stays byte-identical.
        if reaches or declassified:
            comp["taint"] = {
                "reaches": sorted(reaches),
                "declassify": sorted(declassified),
                # Slice C: the enriched declassify records ride beside the coarse
                # `declassify:<origin>` token (which stays the stable diff key).
                # Sorted for a deterministic audit surface.
                **({"declassify_records": sorted(
                    records, key=lambda r: (str(r.get("origin")),
                                            str(r.get("method")),
                                            r.get("line") or 0))}
                   if records else {}),
            }


def splice_declassifiers(node):
    """Replace every `endorse(v)` call node with its argument `v`, everywhere in
    the IR. `endorse` is identity on the base type (its whole job is the taint
    downgrade, which the verdict has already consumed), so after `check_taint`
    runs it is spliced out and no emitter or golden ever sees a call to it.

    Runs over the whole document, so a program with no `endorse` is rebuilt
    identically (byte-identity). Returns the transformed node."""
    if isinstance(node, list):
        return [splice_declassifiers(item) for item in node]
    if not isinstance(node, dict):
        return node
    name = None
    if node.get("kind") == "fn":
        name = node.get("name")
    elif node.get("kind") == "call":
        callee = node.get("callee")
        if isinstance(callee, dict) and callee.get("kind") == "var":
            name = callee.get("name")
    if name == "endorse":
        args = node.get("args") or []
        if args:
            return splice_declassifiers(args[0])
    return {key: splice_declassifiers(value) for key, value in node.items()}


def _walk_component_methods(body, model: TaintModel, source: str,
                            line: int = 0,
                            signatures: dict | None = None,
                            component: str = "") -> tuple[set, set, list]:
    reaches: set = set()
    declassified: set = set()
    records: list[dict] = []
    for step in body:
        if not isinstance(step, dict):
            continue
        if step.get("step") == "provide":
            for method in step.get("methods") or []:
                mname = method.get("name")
                label = f"{component}.{mname}" if component else (mname or "")
                checker = _FlowChecker(
                    model, source, method.get("line") or line,
                    signatures=signatures,
                    endorse_allowed=model.declared_endorse.get(mname, frozenset()),
                    endorse_label=label)
                env: dict = {}
                seeded = model.untrusted_params.get(mname, {})
                for i, pname in enumerate(method.get("params") or []):
                    if i in seeded:
                        env[pname] = Taint(frozenset({seeded[i]}), (pname,))
                checker.run(method.get("body") or [], env)
                reaches |= checker.reaches
                declassified |= checker.declassified
                records.extend(checker.declassify_records)
    return reaches, declassified, records
