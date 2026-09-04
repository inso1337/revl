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
import warnings
from dataclasses import dataclass, field

from .errors import RevlError
from .typecheck import parse_type, format_type, FN_HEAD


class LiteralSecretDefaultWarning(UserWarning):
    """A component config field declared `Secret[T]` carries a LITERAL default
    (roadmap item 256 Slice 3; issue #192).

    What `Secret[T]` buys is redaction at the boundaries the marking reaches:
    the `<name>.config` run trace, WAL records, seam failure text, MCP
    approval tickets. It does NOT make the value absent from the compiled
    artifact. A default is written by the author into the source, so it
    lowers into the IR like any other default (`lower._ir_config_field`) and
    into the emitted `ConfigSchema` — the `secret` flag rides ALONGSIDE the
    plaintext there, it does not replace it.

    That is by construction and stays legal: a canary fixture needs a real
    value in place to detect a leak, which is why this warns rather than
    refusing. The risk it addresses is the READER's, not the mechanism's —
    someone writing `config { api_key: Secret[Str] = "sk-live-..." }` may
    believe the type is protecting that literal.

    The form with no value anywhere in the source or the IR is item 256's
    `secret NAME for CAP` (`parser.SecretDecl`, which has `name`, `capability`
    and `line` and NO value field): the runtime resolves the value against the
    name and injects it into the bound capability's extern bodies only."""

# a qualifier head standing on its own (not the tail of a longer identifier such
# as a user type `MyTrusted[T]`), used only as the byte-identity fast-path guard
_QUALIFIER_RE = re.compile(r"(?<![A-Za-z0-9_])(?:Untrusted|Trusted|Secret)\[")

# The three qualifier heads. Orthogonal to the base type (open question 2).
# `Secret` (item 256 Slice 3) is the confidentiality qualifier of section 7: a
# `Secret[T]` value is a real, projectable value the language reads and computes
# with, fenced not by an absent eliminator but by the flow walk refusing it at
# disclosure sinks (the `confidential` origin, DISJOINT from the bound key's
# `secret`).
_QUALIFIERS = ("Untrusted", "Trusted", "Secret")

# What a confidential value looks like once it leaves the process. A `Secret[T]`
# declaration authorises disclosure to the DECLARED RECEIVER and to nobody else;
# every durable or agent-visible rendering of that value (the WAL, the recorded
# timeline, an MCP response, the run log) carries this placeholder in its place.
#
# The string is part of the WAL's on-disk contract, so it is defined ONCE here
# and mirrored by `backends/python/confidential.py` (which must stay importable
# with no `revl` on the path — an emitted program runs against the backend tree
# alone). `tests/test_secret_externalization.py` pins the two to the same bytes.
REDACTED_SECRET = "<redacted:secret>"

# Coarse origin classes the static half tracks (Decision 2). The exact host in
# `web:example.com` is a runtime refinement filled by Slice B. `secret` (item 256
# Slice 1, the bound provider key) and `confidential` (item 256 Slice 3, the
# `Secret[T]` value qualifier) are DISJOINT: no sink and no declassifier admits
# both, which is what keeps the permissive `Secret[T]` receiver rule unreachable
# by a bound key and the total-refusal bound-key rule unreachable by a `Secret[T]`
# value (CRITICAL 1 fix, §4a / §7).
_ORIGIN_CLASSES = {"web", "net", "fs", "model", "input", "secret", "confidential"}

# Slice D: the two DERIVED classes. Sink-ness and source-ness are read off the
# granting side's declared capability scope, never from an author qualifier —
# `_sink_of`/`_origin_of` are the derivation, `_SINK_CLASS_SCOPES` the sink-class
# set of residual-risk 5. `policy` binds the moment a policy-writing crossing
# exists in-language (none does today; the scope is reserved so the row is ready).
_SINK_CLASS_SCOPES = {"shell", "exec", "terminal", "policy"}
# scopes whose emission return mints a source under taint-strict mode. `secret`
# is deliberately excluded — it arrives with item 256's own bound-emission rule,
# not the generic strict derivation.
_SOURCE_CLASS_SCOPES = {"web", "net", "fs", "model", "input"}


def _sink_of(capabilities) -> str | None:
    """The derived sink-class a crossing's capability scope grants (Slice D), or
    `None` when the scope is not a sink. Sibling of `_origin_of`: a shell / exec /
    terminal-scoped crossing is a sink even with no `Trusted[T]` qualifier, because
    sink-ness comes from the side that grants authority, not from the author."""
    for cap in capabilities or ():
        head = str(cap).split(".", 1)[0]
        if head in _SINK_CLASS_SCOPES:
            return head
    return None


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


def mentions_secret(type_name: str | None) -> bool:
    """True if a `Secret[...]` head appears anywhere the VALUE ITSELF reaches.

    The sibling of :func:`_mentions_trusted`, and the rule that decides which
    declarations get a confidentiality stamp in the IR. `top_qualifier` answers
    only for the OUTERMOST head, which is where the qualifier is written in the
    common case; a declaration that wraps the confidential payload in a type
    constructor writes it one level in:

        extern witnessed fn lease(...) -> Result[Secret[Str], Str]

    The bytes crossing there are exactly as confidential as `Secret[Str]`'s, so
    the marking has to follow the value rather than the spelling. `Result`,
    `Opt`/`Option`, `List`, `Map`, a tuple and a declared record are all
    containers whose value graph physically carries the secret bytes — and the
    runtime funnels that consume this marking are themselves value-graph walks
    (`confidential.register_secret_tree` / `redact_value` recurse into exactly
    those containers), so recursing here is what makes the two halves agree.

    The ONE argument position that is not a container is a FUNCTION type's:
    `Fn[Secret[Str], Int]` (and its `(Secret[Str]) -> Int` spelling) declares
    what a closure will one day be CALLED WITH, not what the closure value holds.
    A closure carries no confidential bytes for a redactor to find, so marking it
    would redact a `<function ...>` repr while the real disclosure — the call —
    happens elsewhere and is caught at its own crossing. Descending there would
    also silently promote whole containers of ordinary callbacks, which is
    exactly the over-redaction `SecretIndex` is careful to bound.

    Deliberately WIDER than what the static refusal walk mints (see
    `CONFIDENTIAL_ORIGIN` below): a stamp only tells a runtime "these bytes are
    confidential once they leave the process", so widening it can over-redact a
    durable record at worst. Minting the origin more widely would newly REFUSE
    programs that compile today, which is a language change and not this
    module's business."""
    if not type_name:
        return False
    head, args = parse_type(type_name)
    if head == "Secret":
        return True
    if head == FN_HEAD or head == "Fn":
        return False
    return any(mentions_secret(a) for a in args)


def secret_witness_position(type_name: str | None) -> bool:
    """True if a witnessed extern's declared return puts a `Secret[...]` in the
    position its WITNESS is read out of — the `Ok` arm of `Result[W, E]`.

    The narrower sibling of :func:`mentions_secret`, and the one a WAL writer
    wants. A witnessed extern's durable discharge-descriptor records the Ok
    witness as the inverse's referent argument, so `Result[Secret[Str], Str]`
    puts a confidential value on disk while `Result[Str, Secret[Str]]` — the
    same declaration with the qualifier on the error arm — does not. Reading
    `secret_return` alone would redact both and lose the second one's referent
    for nothing."""
    if not type_name:
        return False
    head, args = parse_type(type_name)
    return head == "Result" and len(args) == 2 and mentions_secret(args[0])


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
    # item 256 Slice 3: callable name -> {param_index: `confidential`} for params
    # declared `Secret[T]`. The sibling of `untrusted_params`, and the INSIDE half
    # of `secret_receivers`: the `Secret[T]` declaration authorises the crossing TO
    # this callable, and `secret_receivers` (read at the CALL SITE) admits it, but
    # inside the implementing body the parameter is still a confidential value. Seed
    # it here or the receiver body becomes a self-mintable universal declassifier —
    # `logit(x)`/`prompt(x)` on its own parameter would disclose the secret with no
    # `endorse`, no `declassify:confidential` token, and nothing on the audit surface.
    confidential_params: dict[str, dict[int, str]] = field(default_factory=dict)
    # verified fns whose return declares `Trusted[...]` — parser declassifiers
    declassifiers: set[str] = field(default_factory=set)
    # a human sink label for the diagnostic, keyed by callable name
    sink_kind: dict[str, str] = field(default_factory=dict)
    # Slice C: callable name -> the origins it is DECLARED to be allowed to
    # `endorse[<origin>]` in its body (the `endorse[web] fn ...` slot, or the
    # slot on the service operation a provide method implements). An `endorse`
    # whose origin is not in this set is refused at admission.
    declared_endorse: dict[str, frozenset] = field(default_factory=dict)
    # item 256: the capability tokens that carry a bound secret, and the names of
    # every declared extern. `secret_caps` is non-empty exactly when the program
    # binds a secret (so it engages the flow walk, and only then); `extern_names`
    # lets the crossing raises tell a real host-extern crossing (which must refuse
    # a `secret` argument) from a builtin constructor (`Ok`/`Some`/record/list),
    # which merely nests the secret and is caught at the container's own crossing.
    secret_caps: frozenset = field(default_factory=frozenset)
    extern_names: frozenset = field(default_factory=frozenset)
    # item 256 Slice 3: callable name -> the param indices declared `Secret[T]`,
    # the DECLARED disclosure receivers (§7b). A `confidential` value crosses a
    # boundary only where the receiving side declares it here, the dual of 249's
    # `Trusted[T]` sink; everywhere else a `confidential` value is refused. This is
    # kept DISJOINT from `sinks`: a `Secret[T]` param is not an authority sink (it
    # never refuses an `Untrusted[T]` value), it is a confidentiality receiver. A
    # `secret`-origin bound key is NEVER admitted here (only `confidential` is),
    # which is the A8 / CRITICAL 1 guarantee.
    secret_receivers: dict[str, set[int]] = field(default_factory=dict)
    # component name -> the config field names declared `Secret[T]`. The THIRD
    # place the `confidential` origin is minted, beside a `Secret[T]` extern
    # return and a `Secret[T]` parameter. A config field is an ordinary readable
    # binding inside every method of its component (`config.api_key`), so
    # without this the declared marking reached the emitters (which read it to
    # keep the value out of the run log) but never reached the ORIGIN LATTICE:
    # `config.api_key` was seeded CLEAN and was therefore invisible to every §7
    # rule, the provide-method return crossing included.
    secret_config: dict[str, frozenset] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        """Whether any taint surface exists at all. A program with no qualifier
        and no endorse slot engages nothing — the flow walk is skipped and stays
        byte-identical. A bound secret mints a `secret` source, so it engages the
        walk through `sources` (item 256); a `Secret[T]` receiver engages it
        through `secret_receivers` (item 256 Slice 3)."""
        return bool(self.sources or self.sinks or self.untrusted_params
                    or self.declassifiers or self.declared_endorse
                    or self.secret_receivers or self.secret_config)


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


def extract_and_normalize(program, taint_strict: bool = False) -> TaintModel:
    """Read the qualifier surface off every declaration, build the `TaintModel`,
    and STRIP the qualifiers from the declared types in place so the base checker
    and every emitter see bare types (orthogonality / byte-identity).

    `taint_strict` (Slice D, D3) turns on DERIVED sinks and sources: with no
    qualifier at all, a shell/exec/terminal-scoped crossing's parameters become
    sinks and a web/net/fs/model/input-scoped emission's return mints its origin.
    Off by default and byte-identical when off; on only under the untrusted-author
    profile or an explicit `revl compile --taint-strict`, so plain programs never
    move (the permanent additivity line, docs/design/249-taint-provenance.md).

    Mutates `program` — safe because a program is freshly parsed per compile.
    """
    model = TaintModel()

    # item 256: the capabilities that carry a bound secret, and every extern name.
    # A bound emission extern (its declared capability is in `secret_caps`) mints
    # the `secret` origin on its return UNCONDITIONALLY below - a bound key is a
    # security-critical origin whose containment is not opt-in, so it is not gated
    # on `taint_strict` the way the generic derived sources are (4a.1).
    secret_caps = frozenset(
        s.capability for s in getattr(program, "secrets", ()) or () if s.capability)
    model.secret_caps = secret_caps
    model.extern_names = frozenset(
        ext.name for ext in getattr(program, "externs", ()) or ())

    def _note_params(name: str, typed_params) -> set:
        """`typed_params` is a list of (index, type_string, setter). Records
        sink/untrusted params and strips the qualifier via `setter`.

        Returns the indices declared `Secret[T]`, so the caller can stamp them
        on the DECLARATION (`decl.secret_params`) as well as in the model. The
        model is keyed by operation name and is consumed by the flow walk; the
        stamp survives into the IR, which is the only channel by which a runtime
        can learn that an argument position is confidential — the qualifier
        itself is stripped here, so nothing downstream can re-derive it."""
        secret_indices: set = set()
        for index, type_str, setter in typed_params:
            qual = top_qualifier(type_str)
            if qual == "Trusted":
                model.sinks.setdefault(name, {})[index] = strip_qualifiers(type_str)
            elif qual == "Untrusted":
                model.untrusted_params.setdefault(name, {})[index] = _origin_of(())
            elif qual == "Secret":
                # ...and, INSIDE the implementing body, the parameter still
                # carries `confidential`. The declaration admits the crossing TO
                # the receiver, never onward disclosure. Kept on the OUTERMOST
                # qualifier alone: this one is a refusal input, and widening it
                # would newly reject programs that compile today.
                model.confidential_params.setdefault(
                    name, {})[index] = CONFIDENTIAL_ORIGIN
            if mentions_secret(type_str):
                # item 256 Slice 3: a declared `Secret[T]` receiver (§7b). A
                # `confidential` value is ADMITTED here and refused everywhere
                # else — the dual of a `Trusted[T]` sink, on a disjoint origin.
                # `mentions_secret`, not `top_qualifier`: a parameter that takes
                # the confidential payload inside a container is receiving the
                # same bytes, and the recorder that reads the surviving stamp
                # redacts the ARGUMENT, so the container is what it must name.
                # Admission-only, so widening it can never refuse a program that
                # compiles today.
                model.secret_receivers.setdefault(name, set()).add(index)
                secret_indices.add(index)
            clean = strip_qualifiers(type_str)
            if clean != type_str:
                setter(clean)
        return secret_indices

    # externs: an `Untrusted[T]` return is a taint source; a `Trusted[T]` param
    # is a sink; both are stripped to their base type.
    for ext in getattr(program, "externs", ()):
        if top_qualifier(ext.returns) == "Untrusted":
            model.sources[ext.name] = _origin_of(ext.capabilities)
        elif top_qualifier(ext.returns) == "Secret":
            # item 256 Slice 3: a `Secret[T]` return mints the `confidential`
            # origin where the value enters the value world (§7a) — the payment
            # token an emission hands back. DISJOINT from `secret`: the bound-key
            # override at the bottom of this loop can still stamp `secret` for a
            # bound emission cap, but the two never collapse onto one token.
            model.sources[ext.name] = CONFIDENTIAL_ORIGIN
        # The stamp the IR carries into the runtime (item 421 F6). `ext.returns`
        # is stripped a few lines below, so this attribute is the only surviving
        # record that the RETURN position is confidential (the return-side
        # counterpart of `secret_params`). A backend reads it off the IR to
        # register the produced value at its origin, which is what lets a sink
        # with no positional marking of its own (the host trace an operator
        # console prints, the durable WAL an inverse's referent is written to)
        # scrub it.
        #
        # `mentions_secret`, not `top_qualifier`: a declaration that hands the
        # confidential payload back inside a type constructor — the shape a
        # fallible lease has to use, `Result[Secret[Str], Str]` — produces the
        # very same bytes, and stamping only the unwrapped spelling left every
        # fallible secret-returning crossing unmarked at its origin.
        if mentions_secret(ext.returns):
            ext.secret_return = True
        # ...and, narrower, whether the WITNESS position specifically is
        # confidential. A witnessed extern's durable discharge-descriptor
        # records the Ok witness as the inverse's referent argument, so this is
        # the flag a WAL writer reads to keep a leased credential off disk.
        if secret_witness_position(ext.returns):
            ext.secret_witness = True
        params = []
        for i, p in enumerate(ext.params):
            params.append((i, p.type, _fnparam_setter(p)))
        ext.secret_params = frozenset(_note_params(ext.name, params))
        # Slice D (D1/D3): derive sinks and sources from the crossing's declared
        # capability scope, under strict mode, for any parameter/return the author
        # left unqualified. Additive to the annotated surface above — an already
        # `Trusted[T]`/`Untrusted[T]` slot keeps its annotation.
        if taint_strict and getattr(ext, "classification", None) == "emission":
            if _sink_of(ext.capabilities) is not None:
                for i, p in enumerate(ext.params):
                    model.sinks.setdefault(ext.name, {}).setdefault(i, p.type)
            if ext.capabilities:
                origin = _origin_of(ext.capabilities)
                if origin in _SOURCE_CLASS_SCOPES and ext.name not in model.sources:
                    model.sources[ext.name] = origin
        # item 256 (4a.1): a bound emission's return is minted `secret`,
        # unconditionally and overriding any `Untrusted[T]`/derived source. The
        # bound key is the one origin whose containment is not opt-in; matching is
        # by the DECLARED capability token (an emission with no scope names itself),
        # resolved at emit, never a runtime label (A6).
        if secret_caps and ext.classification == "emission":
            ext_caps = ext.capabilities or (ext.name,)
            if any(cap in secret_caps for cap in ext_caps):
                model.sources[ext.name] = SECRET_ORIGIN
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
        fn.secret_params = frozenset(_note_params(fn.name, params))
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
            secret_indices: set = set()
            for i, (pname, ptype) in enumerate(method.params):
                qual = top_qualifier(ptype)
                clean = strip_qualifiers(ptype)
                if qual == "Trusted":
                    model.sinks.setdefault(method.name, {})[i] = clean
                    model.sink_kind[method.name] = _sink_kind_for(method.name, ())
                elif qual == "Untrusted":
                    model.untrusted_params.setdefault(method.name, {})[i] = "input"
                elif qual == "Secret":
                    # the INSIDE half: the provide method implementing this
                    # operation sees the parameter as a `confidential` value, so
                    # disclosing it from the body is refused exactly as a call-site
                    # flow is. Without this the receiver body launders the
                    # qualifier away and is a universal declassifier. Kept on the
                    # OUTERMOST qualifier alone — a refusal input, see
                    # `mentions_secret`.
                    model.confidential_params.setdefault(
                        method.name, {})[i] = CONFIDENTIAL_ORIGIN
                if mentions_secret(ptype):
                    # item 256 Slice 3: a `Secret[T]` service-operation parameter
                    # is a declared disclosure receiver — the ONE crossing that
                    # admits a `confidential` value (§7b). Keyed by the operation
                    # name, so an `emit s.take(x)` call site resolves to it.
                    # `mentions_secret` so a payload wrapped in a container is
                    # marked at the same crossing (admission-only; see there).
                    model.secret_receivers.setdefault(method.name, set()).add(i)
                    secret_indices.add(i)
                new_params.append((pname, clean))
            method.params = new_params
            # The stamp the IR carries into the runtime. `method.params` above no
            # longer mentions `Secret[...]`, so this is the ONLY surviving record
            # that position `i` is a declared disclosure receiver — the recorder
            # reads it to decide what a crossing's argument may look like once it
            # is written to the WAL or handed to an MCP client.
            method.secret_params = frozenset(secret_indices)
            # Slice D (D1/D3): a shell/exec/terminal-scoped service operation is a
            # derived sink under strict mode, exactly as an extern is — a granted
            # tool surface annotates nothing yet still refuses untrusted input.
            if taint_strict and getattr(method, "emission", False) \
                    and _sink_of(getattr(method, "capabilities", None)) is not None:
                for i, (pname, ptype) in enumerate(new_params):
                    model.sinks.setdefault(method.name, {}).setdefault(i, ptype)
                model.sink_kind.setdefault(
                    method.name,
                    _sink_kind_for(method.name, getattr(method, "capabilities", None)))
            method.returns = strip_qualifiers(method.returns)

    # component config fields: a `Secret[T]` field is a declared confidential
    # INPUT. The qualifier is stripped exactly as everywhere else — before this,
    # `Secret[Str]` reached the base checker as an opaque type name, so the field
    # matched no `_TYPES` entry and could not even take a `Str` default — and the
    # flag it leaves behind is what the emitted `ConfigSchema` reads to keep the
    # value out of the run log and the `revl_load` MCP response (item 256 Slice 3,
    # §7b). Byte-identical for a field with no qualifier.
    for comp in getattr(program, "components", ()):
        confidential_fields: set = set()
        for cfield in getattr(comp, "config", ()) or ():
            # `mentions_secret`, not `top_qualifier`: a config field that holds
            # its credential inside a container (`Opt[Secret[Str]]`, the shape an
            # optional API key takes) carries the same bytes into the run log and
            # the `revl_load` MCP response, so it earns the same stamp.
            if mentions_secret(cfield.type):
                cfield.secret = True
                _warn_on_literal_secret_default(comp, cfield)
                # ... and record it in the MODEL, so the flow walk mints
                # `confidential` on a `config.<field>` read. The emitters read
                # `cfield.secret`; the checker reads this. Both come off the one
                # declaration, so they cannot drift apart.
                confidential_fields.add(cfield.name)
            clean = strip_qualifiers(cfield.type)
            if clean != cfield.type:
                cfield.type = clean
        if confidential_fields:
            model.secret_config[comp.name] = frozenset(confidential_fields)

    return model


def _warn_on_literal_secret_default(comp, cfield) -> None:
    """Warn when a `Secret[T]` config field's default is a literal (issue #192).

    A WARNING, never a refusal. The repo's own leak canaries put a real value
    in place precisely so a test can detect it downstream, and refusing would
    break the fixtures that prove the redaction works. What is wrong here is a
    reader's belief, not the compiler's behaviour, so the repair is a sentence
    that says what `Secret[T]` does and does not cover, plus the name of the
    form that has no value to leak.

    `= null` is not reported. The parser lowers `null` to `None`, which is
    indistinguishable from "no default at all" by the time it reaches here,
    and it is not a credential either way.
    """
    default = getattr(cfield, "default", None)
    if default is None:
        return
    where = f"{getattr(comp, 'name', '?')}.{cfield.name}"
    warnings.warn(
        f"config field `{where}` (line {cfield.line}) is declared "
        f"`{cfield.type}` and carries a literal default. `Secret[T]` does NOT "
        f"keep that literal out of the compiled artifact: a default is source "
        f"the author wrote, so it lowers into the IR's config entry and into "
        f"the emitted ConfigSchema verbatim, with the `secret` marking beside "
        f"it rather than in place of it. What the marking buys is REDACTION "
        f"at the boundaries it reaches - the run trace, WAL records, seam "
        f"failure text, approval tickets - not absence from the build output. "
        f"For a value that is in neither the source nor the IR, declare "
        f"`secret {cfield.name} for <capability>` and let the runtime resolve "
        f"it into the bound capability's extern bodies (item 256); leave this "
        f"field's value to be supplied at load time. A literal default here is "
        f"legitimate for a test canary, which is why this is a warning.",
        LiteralSecretDefaultWarning, stacklevel=2)


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

# item 256: the origin a capability-bound secret carries. Disjoint from every
# other origin and from the section-7 `confidential` qualifier, so no sink and no
# declassifier that admits another origin can admit this one. It is never a
# parameter marker, so a plain membership test finds it in any joined origin set.
SECRET_ORIGIN = "secret"

# item 256 Slice 3: the origin a `Secret[T]` value carries. DISJOINT from
# `secret` (the bound key) and from every 249 provenance origin. Its policy is
# the opposite of `secret`'s total refusal: a `confidential` value IS admitted at
# a declared `Secret[T]` receiver (§7b) and IS declassifiable at a declared
# `endorse[confidential]` (§7c). Keeping the two on disjoint strings is exactly
# what makes the permissive receiver rule unreachable by a bound key and the
# no-declassifier bound-key rule unreachable by a `Secret[T]` value.
CONFIDENTIAL_ORIGIN = "confidential"


def _carries_secret(t: Taint) -> bool:
    """Whether a value's origin set carries the bound-secret origin (item 256),
    directly or through a record/variant/generic join (`_join`/`_union_children`
    thread it into the container's origin union, so a nested secret is caught at
    whichever crossing the container reaches - 4a.2 kind 5)."""
    return SECRET_ORIGIN in t.origins


def _carries_confidential(t: Taint) -> bool:
    """Whether a value's origin set carries the `Secret[T]` `confidential` origin
    (item 256 Slice 3), directly or nested in a record/variant/generic (it rides
    the same value-graph joins as `secret`, so a generic round-trip or a nested
    field does NOT launder it — the A2 no-launder-through-generic case)."""
    return CONFIDENTIAL_ORIGIN in t.origins


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
    * `clears`: parameter index -> the set of concrete origins a body-internal
      `endorse[<origin>]` on that parameter's flow-to-return path downgrades
      (item 249, Finding 1). Origin-precise: a call site subtracts exactly these
      origins from the concrete argument before propagating it, so `endorse[web]`
      on a `web` argument clears `web` while a `fs`/`secret`/... argument still
      launders through NOTHING — the boundary is no longer a blanket declassifier.
    """

    flows_to_return: set = field(default_factory=set)
    mints: set = field(default_factory=set)
    reaches_sink: dict = field(default_factory=dict)
    clears: dict = field(default_factory=dict)

    def merge(self, flows: set, mints: set, sink_hits: dict,
              clears: dict | None = None) -> bool:
        """Fold one body-walk's findings in. Returns whether the monotone part
        (the parameter sets, the per-parameter cleared-origin sets, and the sink
        key set) grew — the fixed point's `changed` signal. A shorter `via` for an
        already-known sink refines the message in place WITHOUT signalling change,
        so via refinement cannot make the iteration oscillate."""
        changed = False
        if not flows <= self.flows_to_return:
            self.flows_to_return |= flows
            changed = True
        if not mints <= self.mints:
            self.mints |= mints
            changed = True
        for index, cleared in (clears or {}).items():
            prev = self.clears.get(index, frozenset())
            grown = prev | frozenset(cleared)
            if grown != prev:
                self.clears[index] = grown
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
                 endorse_label: str = "", enforce: bool = True,
                 known_callables: frozenset = frozenset(), any_sink: bool = False,
                 state_env: dict | None = None,
                 state_names: frozenset = frozenset(),
                 untrusted: bool = False,
                 provide_return: bool = False,
                 secret_config: frozenset = frozenset(),
                 config_env: dict | None = None,
                 comp_config: dict | None = None) -> None:
        self.model = model
        self.filename = filename
        self.line = line
        # item 274: whether the AUTHOR is untrusted (the untrusted-author
        # profile). Under it the author cannot mint a declassifier
        # (`no_declassify`), so a G9 sink refusal has no author-side path — the
        # navigable map collapses to the single non-discriminating `blocked`
        # verdict rather than teaching a declassifier that would itself refuse.
        self.untrusted = untrusted
        # item 256 (4a.2 kind 4): this checker walks a `provide` method body, so a
        # return whose taint carries `secret` hands the bound key across the
        # service / MCP bridge and is refused at the method return. Off for module
        # `fn` bodies (a plain fn return is not itself a crossing - the secret
        # propagates through the fn's signature and is caught at whatever crossing
        # the caller reaches).
        self.provide_return = provide_return
        # The config fields of the component this body belongs to that were
        # declared `Secret[T]` (`model.secret_config`). Empty for a top-level
        # `fn`, which has no `config` to read.
        self.secret_config = secret_config
        # SPAWN-CONFIG taint (the cross-component arm). A child's `config.<f>`
        # read was seeded CLEAN unless the field was declared `Secret[T]`, so a
        # parent handing an `Untrusted[T]` value through `spawn Child with { f:
        # d }` laundered it: the child emitted `config.f` into a `Trusted[T]`
        # sink and nothing connected the two ends. The equivalent hand-off
        # through a service OPERATION was already refused, by the ordinary
        # signature machinery — a config field is the same hand-off through a
        # different door, so it is given the same treatment rather than a new
        # rule: `config_env` seeds the field with a parameter marker during
        # inference (making `<Component>#config` a callable whose "parameters"
        # are its config fields, in `comp_config` order), and the spawn site
        # applies that inferred signature exactly as a call site does.
        self.config_env: dict = config_env if config_env is not None else {}
        self.comp_config: dict = comp_config or {}
        self.signatures = signatures or {}
        self.infer = infer
        self.qualname = qualname
        # whether a sink refusal actually raises. Off during the state fixpoint's
        # collection sweeps (Slice B3), where a body is re-walked only to discover
        # what taint it writes into component state — the refusal pass runs after.
        self.enforce = enforce
        # Slice B4: the names that resolve to a callable the checker can NAME
        # (top-level fns, externs, service ops, constructors). A `{kind:call}`
        # through a `var` callee that is none of these and resolves to no known
        # fn value is an UNNAMEABLE indirect call — over-approximated as a sink on
        # every argument when the program has any sink at all (`any_sink`).
        self.known_callables = known_callables
        self.any_sink = any_sink
        # Slice B4: local bindings that hold a reference to a NAMED callable
        # (`let g = upper`), so an indirect call `g(x)` carries `upper`'s signature.
        self.fn_refs: dict[str, str] = {}
        # Slice B3: the per-component state world. `state_names` are the activation
        # bindings shared across every provide method; `state_env` is their
        # accumulated taint (join over all writers, computed to a fixpoint before
        # the refusal pass). `state_writes` records what THIS walk wrote, folded
        # back by the fixpoint driver.
        self.state_env: dict = state_env if state_env is not None else {}
        self.state_names = state_names
        self.state_writes: dict[str, Taint] = {}
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
        # Slice D (D2): the approval scopes threaded on emits that CARRIED taint —
        # the `with a` capability on a tainted outbound send. The policy-gated tier
        # (`web-taint may not reach net without approval`) reads these to decide
        # whether a forbidden flow is covered by an approval (the item-246 surface).
        self.reach_approvals: set[str] = set()
        # Slice C: the enriched declassify records — `{origin, method, reason,
        # line, approved}` — that ride beside the coarse `declassify:` token so
        # the audit surface shows why each downgrade was granted.
        self.declassify_records: list[dict] = []
        # inference-mode accumulators (Slice B): the taint that reaches a return
        # statement, and, per parameter index, the sink an argument reaches with
        # the naming chain behind it.
        self.return_taint: Taint = CLEAN
        self.sink_hits: dict[int, tuple] = {}
        # inference-mode accumulator (item 249, Finding 1): per parameter index,
        # the concrete origins a body-internal `endorse[<origin>]` on that
        # parameter downgrades. Folded into the callable's `_Signature.clears` so
        # a call site subtracts exactly these origins — the scoped, origin-precise
        # declassification is honoured ACROSS the call, not silently blanket-cleaned.
        self.endorse_clears: dict[int, set] = {}
        # the argument taints of the most-recently-walked call, so an `emit` step
        # can record the taint that flows OUTBOUND across the boundary (Decision 5)
        # — not only the emission's return, which is clean for a value-passing send.
        self._emit_args: list | None = None
        # item 256 Slice 3: the param indices of that same call the callee declares
        # `Secret[T]` — the disclosure-receiver positions. The `emit` arm consults
        # this so a `confidential` argument landing on a declared `Secret[T]`
        # receiver is ADMITTED, while one crossing to an undeclared receiver (an
        # LLM prompt, an un-approved realm, a plain service op) is refused (§7b).
        self._emit_secret_receivers: set = set()

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
            # a pure-fn or fn-value call, across both IR dialects: `{kind:var,
            # name}` (top-level fn bodies) and `{kind:name, id}` (component bodies).
            if isinstance(callee, dict) and callee.get("kind") in ("var", "name"):
                return callee.get("name") or callee.get("id")
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
        if not self.enforce:
            return
        concrete_origins = sorted(o for o in arg_taint.origins
                                  if _param_index(o) is None)
        origins = ", ".join(concrete_origins)
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
            navigate=self._sink_navigate(sink_name, kind, concrete_origins),
        )

    def _refuse_secret(self, sink_name: str, kind: str, index: int | None,
                       arg_taint: Taint, node) -> None:
        """A capability-bound secret (item 256) has reached a boundary crossing.
        Refuse it unconditionally - the bound key is refused at EVERY crossing kind
        (4a.2), with no declassifier (4a.3). The one crossing that does NOT refuse
        is a re-entry into an extern body of the same bound capability, which is
        handled upstream by the `model.sources` early return (a call to a bound
        emission returns before any raise - 4b), never here."""
        chain_parts = arg_taint.via + (sink_name,)
        chain = " -> ".join(chain_parts) if chain_parts else "the bound key"
        where = (f" at argument {index + 1} of `{sink_name}`"
                 if index is not None else f" of `{sink_name}`")
        raise RevlError(
            self.filename, self._line_of(node),
            f"a capability-bound secret flows into {kind}{where} - a bound "
            f"provider key can never leave its capability's own extern bodies, "
            f"and no revl construct or declared crossing may carry it out "
            f"(G-SECRET)",
            hint="a `secret NAME for CAP` value is confined to CAP's emission "
                 "bodies by construction: it has NO declassifier (`endorse[secret]` "
                 "is refused) and no allowed sink except a re-emission through the "
                 "same bound capability. If a host body reflected the injected key "
                 "into a return, stop reflecting it - the key is a host-scope local "
                 "handed straight to the provider call, never a revl value. The "
                 f"reflecting path is {chain} "
                 "(docs/design/256-capability-bound-secrets.md §4).",
            code="G-SECRET", category="taint-secret",
        )

    def _refuse_confidential(self, sink_name: str, kind: str, index: int | None,
                             arg_taint: Taint, node) -> None:
        """A `Secret[T]` value (origin `confidential`, item 256 Slice 3) has
        reached a DISCLOSURE sink (§7b): a log, an ordinary JSON serialization, an
        LLM prompt, an MCP tool return, an un-approved realm crossing, or a
        capability crossing whose receiver does not declare `Secret[T]`. Refused
        with G-SECRET-FLOW. Unlike `secret`, a `confidential` value is NOT refused
        everywhere: it is admitted at a declared `Secret[T]` receiver (the caller
        checks that before reaching here) and declassifiable at a declared
        `endorse[confidential]` (§7c)."""
        chain_parts = arg_taint.via + (sink_name,)
        chain = " -> ".join(chain_parts) if chain_parts else "a Secret[T] value"
        where = (f" at argument {index + 1} of `{sink_name}`"
                 if index is not None else f" of `{sink_name}`")
        raise RevlError(
            self.filename, self._line_of(node),
            f"a Secret[T] value flows into {kind}{where} - a confidential value "
            f"may not reach a disclosure sink (a log, an ordinary serialization, "
            f"an LLM prompt, an MCP tool return, or a capability crossing whose "
            f"receiver does not declare `Secret[T]`) (G-SECRET-FLOW)",
            hint="a `Secret[T]` value crosses a boundary ONLY where the receiving "
                 "side declares a `Secret[T]` parameter (the dual of a `Trusted[T]` "
                 "sink), and downgrades ONLY at a declared, audited "
                 "`endorse[confidential](<value>, reason = \"...\")` slot. Route it "
                 "through a declared `Secret[T]` receiver, or endorse it at a "
                 f"declared point. The disclosing path is {chain} "
                 "(docs/design/256-capability-bound-secrets.md §7).",
            code="G-SECRET-FLOW", category="taint-secret-flow",
        )

    def _sink_navigate(self, sink_name: str, kind: str, origins: list) -> dict:
        """The taint-sink family's nearest allowed (item 274, design §2.1),
        computed from the model in hand: the in-scope declassifiers, the endorse
        form with the concrete origin, and blocked-when-forbidden.

        Under the untrusted-author profile (`no_declassify`), the author cannot
        mint or reach its own declassifier, so there is no author-side path: the
        record collapses to the single non-discriminating `blocked` verdict, and
        the honest hint is to return the untrusted value to the harness. On the
        trusted view the alternatives are enumerated."""
        from . import navigate as nav  # noqa: PLC0415 — lazy, additive
        origin = origins[0] if origins else "<origin>"
        if self.untrusted:
            # `no_declassify`: no author-side path exists, and the record must be
            # byte-identical to every other policy-family refusal under this
            # profile so the author cannot tell which gate fired (design §4).
            return nav.collapsed()
        alts = []
        # the in-scope declassifiers, by name. A `verified fn` returning
        # `Trusted[T]` is exactly what the sink accepts, so routing the value
        # through one clears THIS gate by construction — the declassifier being
        # in scope is a static fact at the refusal site (immutable operand).
        for dname in sorted(self.model.declassifiers):
            alts.append(nav.alternative(
                enacts=nav.ENACTS_AUTHOR,
                action=(f"parse it through the in-scope declassifier "
                        f"`{dname}` (a `verified fn` returning `Trusted[T]`) "
                        f"before the sink"),
                ref=dname, clears=True))
        # the endorse form with the concrete origin. Whether the enclosing
        # declaration already grants `endorse[origin]` decides the marker: a
        # granted slot clears the gate; an ungranted one needs the author to add
        # the slot first, so it is a `candidate`.
        granted = origin in self.endorse_allowed
        alts.append(nav.alternative(
            enacts=nav.ENACTS_AUTHOR,
            action=(f"endorse it at a declared point: "
                    f"`endorse[{origin}](<value>, reason = \"...\")`"
                    + ("" if granted
                       else f" (declare the slot first: `endorse[{origin}] "
                            f"fn ...` on the enclosing declaration)")),
            ref=f"endorse[{origin}]", clears=granted))
        return nav.record(
            family="taint-sink",
            refused={"sink": sink_name, "kind": kind, "origins": origins},
            blocked=False, alternatives=alts, profile=None)

    # -- the scoped declassifier (Slice C) -------------------------------------
    def _endorse(self, node, arg_taints: list) -> Taint:
        """A scoped `endorse[<origin>](v, reason=...)`: authorise the downgrade
        against the enclosing declaration's declared slot, record it on the audit
        surface, and return CLEAN.

        In INFERENCE mode the fixed point neither enforces nor records the audit
        surface, but it MUST stay origin-precise across the call boundary (item
        249, Finding 1). A scoped `endorse[<origin>]` is NOT a blanket sanitizer:
        it clears only its own declared origin. So the parameter markers on the
        argument's flow-to-return path are KEPT (the parameter still `flows_to_return`),
        while the concrete `origin` is recorded, per marker, as CLEARED for that
        parameter. The call site then subtracts exactly that origin from the
        concrete argument — a `web` endorse over a `fs`/`secret`/... argument
        launders nothing, so a one-hop helper can no longer act as a total
        declassifier for every origin. A bare (originless) endorse is the only
        full clean-out, matching the enforcing pass's blanket-clean fallback."""
        meta = node.get("endorse") if isinstance(node, dict) else None
        origin = meta.get("origin") if isinstance(meta, dict) else None
        if self.infer:
            value_taint = arg_taints[0] if arg_taints else CLEAN
            if origin is None:
                return CLEAN
            markers = frozenset(o for o in value_taint.origins
                                if _param_index(o) is not None)
            if not markers:
                # a body-minted origin (a source called inside the body) endorsed
                # here. No marker means no argument-derived flow to record a
                # `clears` for, but the endorse still only clears its own declared
                # origin: mirror the enforcing residual so a non-matching
                # `endorse[X]` over a body-minted foreign origin does not launder
                # it clean. A matching endorse (X over an X value) still clears to
                # empty and returns CLEAN.
                residual = frozenset(o for o in value_taint.origins if o != origin)
                return Taint(residual, value_taint.via) if residual else CLEAN
            for m in markers:
                self.endorse_clears.setdefault(_param_index(m), set()).add(origin)
            # keep the markers flowing (the parameter still reaches the return);
            # drop the seeded concrete origins, whose precise per-origin
            # contribution the call site reconstructs from the real argument.
            return Taint(markers, value_taint.via)
        # the state-collection sweeps (Slice B3, enforce=False) re-walk method
        # bodies only to discover state writes and carry no declared-endorse slot;
        # the undeclared-endorse refusal belongs to the enforcing pass alone.
        if not self.enforce:
            value_taint = arg_taints[0] if arg_taints else CLEAN
            if origin is None:
                return CLEAN
            residual = frozenset(o for o in value_taint.origins if o != origin)
            return Taint(residual, value_taint.via) if residual else CLEAN
        # item 256 (4a.3): `endorse[secret]` is refused UNCONDITIONALLY, before
        # the declared-slot check, so no declaration can ever grant it. A bound
        # provider key has no "I know what I am doing" downgrade edge - this is the
        # sharp line from the section-7 `confidential` origin, which DOES have a
        # declared, audited downgrade. The two carry disjoint origins precisely so
        # no declassifier can confuse them (CRITICAL 1 fix).
        if origin == SECRET_ORIGIN:
            raise RevlError(
                self.filename,
                self._line_of(meta if isinstance(meta, dict) else node),
                "`endorse[secret]` is refused: a capability-bound secret has no "
                "declassifier - a bound provider key can never be downgraded "
                "(G-SECRET)",
                hint="there is no audited downgrade for a bound key, by design. "
                     "Remove the `endorse[secret]`; the key belongs only in its "
                     "capability's own extern bodies as a host-scope local, never "
                     "as a revl value (docs/design/256-capability-bound-secrets.md "
                     "§4a.3).",
                code="G-SECRET", category="taint-secret",
            )
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

        # a component config read. Ordinarily trusted by construction, exactly
        # like a literal — the author wrote the field and the operator supplies
        # its value. A field DECLARED `Secret[T]`, though, is a declared
        # confidential INPUT: the operator hands the component a credential, and
        # every §7 rule downstream (a disclosure sink, an emission crossing, the
        # provide-method return across the service / MCP bridge and the placement
        # seam) has to see it as one. It is the third mint of the `confidential`
        # origin, beside a `Secret[T]` extern return and a `Secret[T]` parameter,
        # and the only one that had no seed here.
        if kind == "config":
            fname = node.get("field")
            if fname in self.secret_config:
                return Taint(frozenset({CONFIDENTIAL_ORIGIN}),
                             (f"config.{fname}",))
            return self.config_env.get(fname, CLEAN)

        # a literal is trusted by construction (Decision 2, trusted origins)
        if kind in ("lit", "int", "float", "string", "str", "bool"):
            return CLEAN

        # a read of / write into a component STATE world (Slice B3): a call whose
        # receiver is a state binding (`store.get(k)`, `store.insert(k, v)`) reads
        # the world's accumulated taint and records any tainted argument as a write.
        if kind == "call" and self.state_names:
            target = node.get("target")
            if isinstance(target, dict):
                tname = target.get("id") or target.get("name")
                if tname in self.state_names:
                    return self._taint_of_state_access(tname, node, env)

        # a call / emission: check its sinks, then compute the result's taint
        callee = self._callee_name(node)
        if callee is not None:
            callee_node = node.get("callee")
            indirect = (node.get("kind") == "call"
                        and isinstance(callee_node, dict)
                        and callee_node.get("kind") in ("var", "name"))
            return self._taint_of_call(callee, node.get("args") or [], env, node,
                                       indirect=indirect)

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

        # `spawn C with { <field>: <value> }`: the config hand-off is a CALL
        # into the child, whose "parameters" are its config fields. Apply the
        # child's inferred config signature the way any call site applies a
        # callee's, so a tainted value landing on a field the child sinks is
        # refused HERE, at the hand-off the author wrote.
        if kind == "spawn":
            child = node.get("component")
            fields = self.comp_config.get(child)
            if fields:
                supplied = node.get("config") or {}
                arg_taints = [self.taint_of(supplied.get(f), env)
                              if f in supplied else CLEAN
                              for f in fields]
                self._check_sinks(f"{child}#config", arg_taints, node)
            return self._union_children(node, env)

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
            if key in ("kind", "op", "name", "id", "method", "line", "src_line"):
                continue
            if key == "callee" and isinstance(value, dict) \
                    and value.get("kind") in ("var", "name"):
                # A directly-named callee is the call TARGET, not a value that
                # flows: `_callee_name` resolves it and `_taint_of_call` owns
                # the sink check, so this fallback is not even reached for one.
                # A COMPUTED callee is a different thing — `[f, g][i](x)`,
                # `(c ? f : g)(x)` — an ordinary expression that carries taint
                # and can nest a sink call of its own. Skipping the whole
                # `callee` slot dropped both (the `_calls_in` blind spot in the
                # same shape), so only the named form is skipped here.
                continue
            result = _join(result, self.taint_of(value, env))
        return result

    def _taint_of_state_access(self, name: str, node, env: dict) -> Taint:
        """A call whose receiver is a component-state world (Slice B3). Every such
        call is treated at the whole-container grain: a tainted argument is a WRITE
        that joins into the world's taint (methods run in unknown order, so the
        join over all writers is the only sound seed), and the call READS the
        world's accumulated taint (a `.get()` returns whatever any writer stored).
        Sound and coarse — precise element tracking is Slice E's runtime tag."""
        arg_taints = [self.taint_of(a, env) for a in (node.get("args") or [])]
        combined = CLEAN
        for t in arg_taints:
            combined = _join(combined, t)
        if combined.dirty:
            real = frozenset(o for o in combined.origins if _param_index(o) is None)
            if real:
                write = Taint(real, combined.via)
                self.state_writes[name] = _join(
                    self.state_writes.get(name, CLEAN), write)
        return _join(self.state_env.get(name, CLEAN), combined)

    def _is_unnameable(self, name: str) -> bool:
        """Whether an indirect-call callee resolves to no callable the checker can
        name (Slice B4): not a top-level fn/extern/service op/constructor, not a
        known signature, not a source/sink/declassifier, not a named fn value."""
        return (name not in self.known_callables
                and name not in self.signatures
                and name not in self.model.sinks
                and name not in self.model.sources
                and name not in self.model.declassifiers
                and name != "endorse")

    def _taint_of_call(self, callee: str, args, env: dict, node,
                       indirect: bool = False) -> Taint:
        arg_taints = [self.taint_of(a, env) for a in args]
        # remember the outermost call's arguments so an enclosing `emit` records
        # the taint crossing the boundary (set last, so the outer call wins).
        self._emit_args = arg_taints
        # item 256 Slice 3: and the receiver positions the outermost call declares
        # `Secret[T]`, so the `emit` arm can admit a `confidential` argument that
        # lands on one. Resolved against the DIRECT callee (a required-service op
        # or extern named at this call site), never a runtime label.
        self._emit_secret_receivers = self.model.secret_receivers.get(callee, set())

        # Slice B4: an indirect call `g(x)` where `g` names a known callable
        # (`let g = upper`) carries that callable's signature/sink; resolve it.
        resolved = self.fn_refs.get(callee, callee) if indirect else callee

        # item 256 (4a.2 kind 3): the unnameable indirect / `*` callable. A
        # first-class emitting callable revl cannot name must refuse a `secret`
        # argument INDEPENDENTLY of `any_sink` - what cannot be named cannot be
        # proven to re-emit through the bound capability, so it cannot be the 4b
        # same-capability re-entry. This fires even in a sink-free program (a bound
        # secret is itself the reason the flow walk is active).
        if (indirect and self._is_unnameable(resolved)
                and not self.infer and self.enforce):
            for index, at in enumerate(arg_taints):
                if _carries_secret(at):
                    self._refuse_secret(
                        "an unnameable callable",
                        "a first-class function value revl cannot name",
                        index, at, node)
                # item 256 Slice 3: an unnameable callable cannot DECLARE a
                # `Secret[T]` receiver (what cannot be named cannot be shown to
                # admit a confidential value), so a `confidential` argument to it
                # is a disclosure crossing with no declaration - refused (§7b).
                if _carries_confidential(at):
                    self._refuse_confidential(
                        "an unnameable callable",
                        "a first-class function value revl cannot name",
                        index, at, node)
        # Slice B4: an indirect call the checker cannot name is over-approximate —
        # every argument position is a sink (what cannot be named cannot be proven
        # safe), but only when the program has a sink at all, so a sink-free program
        # is unaffected. This mirrors G4's `*` for a first-class dispatch.
        if indirect and self.any_sink and self._is_unnameable(resolved):
            kind = "an unnameable call (a first-class function value)"
            for index, at in enumerate(arg_taints):
                if at.origins:
                    self._on_sink("an unnamed callable", kind, index, at, node,
                                  ("a first-class function value",))
            result = CLEAN
            for t in arg_taints:
                result = _join(result, t)
            # ... AND the callee VALUE's own taint. A closure carries what it
            # CAPTURED, and the capture is not an argument: `let f = (x) => d`
            # over an `Untrusted[Str]` `d`, then `f("z")`, produced `d`'s value
            # from clean arguments alone, so `emit run(f("z"))` reached a
            # `Trusted[Str]` shell sink with no declassification (G9). The
            # binding already holds the join of the arrow's free names
            # (`_union_children` walks an arrow body), and an arrow MINTED by a
            # fn (`fn mk(d) -> (Str) -> Str { return (x) => d }`) carries the
            # same join out through `mk`'s return — so reading the binding here
            # covers both shapes with one join, and stays exact for a closure
            # that captured nothing.
            result = _join(result, env.get(callee, CLEAN))
            if result.dirty:
                result = Taint(result.origins, result.via + (f"{callee}()",))
            return result

        callee = resolved

        # item 256 Slice 3 (§7b): a `confidential` argument reaching a REAL extern
        # host call is a disclosure crossing (a log, a serialization, an LLM
        # prompt) UNLESS the extern declares a `Secret[T]` receiver at this
        # position. Checked HERE, before the `model.sources` early return below, so
        # a confidential value handed to a bound emission (itself a `model.*`
        # source that returns early) is still refused - the bound-key §4b
        # same-capability re-emission admits a `secret` value back into its own
        # emission, never a `confidential` one (the two rules never cross). The
        # `secret` refusal stays AFTER the sources return, so §4b keeps admitting
        # the bound key's own re-entry.
        if (not self.infer and self.enforce
                and callee in self.model.extern_names):
            receivers = self.model.secret_receivers.get(callee, set())
            for index, at in enumerate(arg_taints):
                if _carries_confidential(at) and index not in receivers:
                    self._refuse_confidential(
                        callee, "an extern host call (a disclosure sink)",
                        index, at, node)

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
            # item 256 (4a.3): a `verified fn` returning `Trusted[T]` does NOT
            # launder a `secret`-carrying argument - the parser-declassifier clean
            # is SKIPPED for the bound key and the crossing is refused. There is no
            # declassifier for a bound provider key, verified-fn or otherwise.
            if not self.infer and self.enforce:
                for index, at in enumerate(arg_taints):
                    if _carries_secret(at):
                        self._refuse_secret(callee, "a verified-fn declassifier",
                                            index, at, node)
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
                    # item 249, Finding 1: subtract the origins the callee's
                    # scoped `endorse[<origin>]` cleared FOR THIS PARAMETER. The
                    # matching-origin case (`endorse[web]` over a `web` argument)
                    # cancels to clean; a cross-origin argument (`fs`, `secret`,
                    # ...) is unaffected and still propagates to the sink.
                    contributed = arg_taints[index].origins - sig.clears.get(
                        index, frozenset())
                    if not contributed:
                        continue
                    origins = origins | contributed
                    if not via:
                        via = arg_taints[index].via + (f"{callee}()",)
            if origins:
                return Taint(origins, via)
            return CLEAN

        # item 256 (4a.2 kind 2): a plain (non-declared-sink, non-source) extern
        # call is a host crossing. A `secret`-carrying argument to it is refused -
        # an ordinary extern that is neither the same bound capability's re-entry
        # (which returned above via `model.sources`) nor an emit still must not
        # receive the bound key. Gated on a REAL extern name, so a builtin
        # constructor (`Ok`/`Some`) merely nests the secret and is caught at the
        # container's own crossing (kind 5), not here.
        if (not self.infer and self.enforce
                and callee in self.model.extern_names):
            for index, at in enumerate(arg_taints):
                if _carries_secret(at):
                    self._refuse_secret(callee, "an extern host call", index, at,
                                        node)
                    # the `confidential` disclosure refusal for this same crossing
                    # ran earlier (before the `model.sources` early return), so a
                    # bound emission that returns early is still fenced. The
                    # DISJOINTNESS (A8): a `secret` bound key is refused here even
                    # at a `Secret[T]` receiver position - the receiver admits
                    # `confidential` only, never `secret`.

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
            value = stmt.get("value")
            env[stmt["name"]] = self.taint_of(value, env)
            self._note_fn_ref(stmt.get("name"), value)
            return
        if step == "assign":
            # a `var` reassignment carries taint across the binding (and, inside a
            # loop, across the back edge — the loop fixpoint below re-walks until
            # the joined environment stabilises).
            value = stmt.get("value")
            env[stmt["name"]] = self.taint_of(value, env)
            self._note_fn_ref(stmt.get("name"), value)
            return
        if step in ("while", "for"):
            self._loop(stmt, env)
            return
        if step in ("return", "emit", "expr", "fail"):
            self._emit_args = None
            result = self.taint_of(stmt.get("expr") or stmt.get("value"), env)
            if step == "return" and self.infer:
                # inference mode: the return value's taint (parameter markers
                # and minted origins) feeds `flows_to_return` and `mints`.
                self.return_taint = _join(self.return_taint, result)
            if (step == "return" and self.provide_return
                    and self.enforce and not self.infer
                    and _carries_secret(result)):
                # item 256 (4a.2 kind 4): a `provide` method returning a
                # `secret`-carrying value hands the bound key across the service /
                # MCP bridge. Refused at the method return (the crossing is the
                # return, not an `emit` or an extern call).
                self._refuse_secret("this provide method",
                                    "a provide-method return across the "
                                    "service / MCP bridge", None, result, stmt)
            if (step == "return" and self.provide_return
                    and self.enforce and not self.infer
                    and _carries_confidential(result)):
                # item 256 Slice 3 (§7b): an MCP tool return is a disclosure sink.
                # A `provide` method returning a `confidential` value hands it
                # across the service / MCP bridge to a client that never declared
                # a `Secret[T]` receiver, so it is refused. (Route the value to a
                # declared `Secret[T]` receiver, or endorse it, before returning.)
                self._refuse_confidential(
                    "this provide method",
                    "a provide-method return across the service / MCP bridge "
                    "(an MCP tool return, or a placement-seam reply - the Err "
                    "half of a Result included, which is marshalled BY VALUE "
                    "like any other return)", None, result, stmt)
            if step == "emit":
                # the taint crossing the boundary here (Decision 5): the emission's
                # return AND the arguments it carries outward — a value-passing send
                # returns clean but still exfiltrates its tainted argument.
                outbound = result
                for at in (self._emit_args or ()):
                    outbound = _join(outbound, at)
                real = {o for o in outbound.origins if _param_index(o) is None}
                # item 256 (4a.2 kind 1): the emit arm. A `secret`-carrying value
                # crossing an emission is refused here rather than merely recorded -
                # this is the model-prompt / outbound-send crossing. The 4b
                # same-capability re-emission never reaches this arm: the bound key
                # is a host-scope local handed straight to the provider call inside
                # the extern body (never a revl value), and a re-entry into the
                # bound extern returns via `model.sources` before any emit records
                # its argument. So a `secret` reaching the emit arm is always an
                # exfiltration to a crossing that is not the bound body itself.
                if (SECRET_ORIGIN in real and self.enforce and not self.infer):
                    self._refuse_secret("this emission",
                                        "an emission crossing", None,
                                        outbound, stmt)
                # item 256 Slice 3 (§7b): the emission crossing for a `Secret[T]`
                # value - an LLM prompt (an argument to a `model.*` emission), an
                # un-approved realm crossing, or a capability-boundary crossing
                # whose receiver did not declare `Secret[T]`. A `confidential`
                # argument landing on a declared `Secret[T]` receiver position is
                # ADMITTED (that is the ONE crossing that does not refuse); every
                # other unadmitted `confidential` value crossing here is refused.
                if self.enforce and not self.infer:
                    receivers = self._emit_secret_receivers or set()
                    unadmitted = CLEAN
                    for i, at in enumerate(self._emit_args or ()):
                        if i not in receivers and _carries_confidential(at):
                            unadmitted = _join(unadmitted, at)
                    if CONFIDENTIAL_ORIGIN in unadmitted.origins:
                        self._refuse_confidential(
                            "this emission",
                            "an emission crossing (an LLM prompt / a capability "
                            "boundary without a declared `Secret[T]` receiver)",
                            None, unadmitted, stmt)
                if real:
                    # An *absolute-refusal* sink already raised above; what remains
                    # is the policy-gated tier (e.g. web-taint into `send.*`) —
                    # recorded, so `audit --diff` sees the exfiltration edge widen.
                    self.reaches |= real
                    # Slice D (D2): if this tainted send is approval-covered
                    # (`with a`), remember the scope so `<origin>-taint may not
                    # reach <cap> without approval` admits the approved flow.
                    approval = stmt.get("approval")
                    if isinstance(approval, dict) and approval.get("capability"):
                        self.reach_approvals.add(approval["capability"])
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

    def _note_fn_ref(self, name, value) -> None:
        """Record `let <name> = <var:known-callable>` (Slice B4), so a later
        indirect call `name(x)` carries the referenced callable's signature. Only
        a bare reference to a NAMEABLE callable is tracked; a closure or an
        unresolved value stays unnameable (and is over-approximated at the call)."""
        if not isinstance(name, str) or not isinstance(value, dict):
            return
        if value.get("kind") in ("var", "name"):
            ref = value.get("name") or value.get("id")
            if isinstance(ref, str) and not self._is_unnameable(ref):
                self.fn_refs[name] = ref
            else:
                self.fn_refs.pop(name, None)
        else:
            self.fn_refs.pop(name, None)

    def _loop(self, stmt, env: dict) -> None:
        """A `while`/`for` body walked to a fixed point (Slice B3, body-local back
        edges): a binding rebound to a tainted value on the back edge must be seen
        tainted on its reads, not clean on the first pass. Iterate the body,
        JOINING the environment across passes (a may-analysis over the loop), until
        no binding's taint grows — monotone over the finite lattice, so it stops."""
        body = stmt.get("body") or []
        if stmt.get("step") == "for":
            # the loop variable carries the iterable's element taint (coarse: the
            # iterable's join), so a tainted collection taints every iteration.
            it = self.taint_of(stmt.get("iterable"), env)
            if stmt.get("bind"):
                env[stmt["bind"]] = it
        else:
            self.taint_of(stmt.get("cond"), env)  # visit the condition's sinks
        merged = dict(env)
        bound = 2 * (len(body) + 1) + 4
        while bound > 0:
            bound -= 1
            trial = dict(merged)
            self.run(body, trial)
            grew = False
            for key, value in trial.items():
                old = merged.get(key)
                joined = value if old is None else _join(old, value)
                if old is None or joined.origins != old.origins:
                    merged[key] = joined
                    grew = True
            if not grew:
                break
        env.update(merged)


def _declared_param_origins(model: TaintModel, key: str) -> dict[int, frozenset]:
    """The origins a callable's DECLARED parameter qualifiers put on its
    parameters inside its own body, keyed by parameter index.

    Two qualifiers seed taint here, and both must, for the same reason: the
    qualifier states what the value IS, so the body has to see it. `Untrusted[T]`
    seeds its provenance origin (landed, item 249); `Secret[T]` seeds
    `confidential` (item 256 Slice 3). The `Secret[T]` case is the one that used
    to be missing: `secret_receivers` is consulted only at the CALL SITE, to admit
    the crossing, and the qualifier was then stripped — so the receiver's own body
    saw a bare `Str` with empty taint and could hand it to a log, an LLM prompt or
    `fs.write` with no `endorse` and no audit token. A declared receiver is
    authorised to RECEIVE the value, never to disclose it onward."""
    origins: dict[int, frozenset] = {}
    for index, origin in (model.untrusted_params.get(key) or {}).items():
        origins[index] = origins.get(index, frozenset()) | {origin}
    for index, origin in (model.confidential_params.get(key) or {}).items():
        origins[index] = origins.get(index, frozenset()) | {origin}
    return origins


def _seed_param_env(model: TaintModel, key: str, params, env: dict) -> None:
    """Seed a body's environment from its declared parameter qualifiers."""
    seeded = _declared_param_origins(model, key)
    for i, param in enumerate(params or []):
        pname = param["name"] if isinstance(param, dict) else param
        if i in seeded:
            env[pname] = Taint(seeded[i], (pname,))


def _param_names(params) -> list[str]:
    """Parameter names from a lowered param list (dicts for fns, bare strings
    for provide methods)."""
    return [p["name"] if isinstance(p, dict) else p for p in (params or [])]


def secret_config_fields(comp) -> frozenset:
    """The config field names a lowered IR component declares `Secret[T]`.

    Read off `config[i]["secret"]`, the stamp `extract_and_normalize` leaves and
    the emitters already read — one declaration, no second IR key."""
    if not isinstance(comp, dict):
        return frozenset()
    return frozenset(
        f["name"] for f in (comp.get("config") or ())
        if isinstance(f, dict) and f.get("secret") and f.get("name"))


def component_config_order(components) -> dict:
    """`{component name: (field names, in declaration order)}` — the positional
    reading a spawn-config hand-off is checked against. Empty for a composition
    whose components declare no config, so those programs are untouched."""
    order: dict = {}
    for comp in components or ():
        fields = tuple(f["name"] for f in (comp.get("config") or ())
                       if isinstance(f, dict) and f.get("name"))
        if fields and comp.get("name"):
            order[comp["name"]] = fields
    return order


def _callables(fns, components, filename: str):
    """Every callable whose body inference walks (Slice B): top-level fns and
    component provide methods. Yields `(qualname, key, params, body, source,
    line)`, where `key` is the name a call site resolves to (the fn name or, for
    a provide method, the service operation name) and `qualname` is the human
    name for the diagnostic chain (`Component.method`).

    Plus one SYNTHETIC callable per component that declares config: the
    component's whole body under the key `<Component>#config`, whose parameters
    are its config fields. Inference over it answers "which config field of this
    component reaches a sink", which is what a `spawn C with { … }` hand-off
    needs to judge its arguments — the same question, and the same machinery, a
    service-operation hand-off already uses. The `#` keeps the key out of every
    real callable namespace."""
    for fn in fns:
        yield (fn["name"], fn["name"], _param_names(fn.get("params")),
               fn.get("body") or [], fn.get("source") or filename,
               fn.get("line") or 0, frozenset(), None)
    for comp in components:
        source = comp.get("source") or filename
        cname = comp.get("name") or "?"
        # the component's `Secret[T]` config fields travel with its methods, so
        # inference mints `confidential` into a method's signature exactly as
        # the refusal pass does.
        secret_config = secret_config_fields(comp)
        for step in comp.get("body") or []:
            if not isinstance(step, dict) or step.get("step") != "provide":
                continue
            for method in step.get("methods") or []:
                mname = method.get("name")
                yield (f"{cname}.{mname}", mname,
                       _param_names(method.get("params")),
                       method.get("body") or [], source, method.get("line") or 0,
                       secret_config, None)
        config_fields = tuple(f["name"] for f in (comp.get("config") or ())
                              if isinstance(f, dict) and f.get("name"))
        if config_fields:
            yield (f"{cname}.config", f"{cname}#config", list(config_fields),
                   comp.get("body") or [], source, comp.get("line") or 0,
                   secret_config, config_fields)


# constructor / builtin callables that may appear as a bare callee but are not a
# user fn — nameable, so never over-approximated as an unnameable indirect call.
_BUILTIN_CALLABLE_NAMES = frozenset({"Ok", "Err", "Some", "None", "endorse"})


def _known_callables(program, fns, components, model: TaintModel) -> frozenset:
    """Every name that resolves to a callable the checker can NAME (Slice B4):
    top-level fns, externs, service operations, constructors, and everything the
    taint model already knows. A `var`-callee outside this set (and unresolved by
    a fn-value binding) is an unnameable indirect call, over-approximated as a
    sink on every argument."""
    known: set = set(_BUILTIN_CALLABLE_NAMES)
    known |= {fn.get("name") for fn in fns if fn.get("name")}
    for ext in getattr(program, "externs", ()) or ():
        known.add(ext.name)
    for svc in getattr(program, "services", ()) or ():
        known |= set(getattr(svc, "methods", {}) or {})
    known |= set(model.sources) | set(model.sinks) | set(model.declassifiers)
    return frozenset(n for n in known if n)


def _state_bindings(body) -> frozenset:
    """The component-state world names (Slice B3): every activation-body binding
    that holds live, cross-method state — an effect-acquired world (`let store =
    effect ...`, an IR `bind`) or a mutable var. A plain immutable `let` (an
    approval, a config read) is not shared mutable state and is excluded. A `let`
    inside a provide method is method-local and is never one of these."""
    names: set = set()
    for step in body or []:
        if not isinstance(step, dict) or step.get("step") == "provide":
            continue
        bind = step.get("bind")           # an effect-acquired world (`let-effect`)
        if isinstance(bind, str):
            names.add(bind)
        name = step.get("name")           # a mutable activation var
        if isinstance(name, str) and step.get("mutable"):
            names.add(name)
    return frozenset(names)


def _infer_state_env(body, model: TaintModel, source: str, line: int,
                     signatures: dict, state_names: frozenset,
                     known: frozenset, any_sink: bool,
                     secret_config: frozenset = frozenset()) -> dict:
    """The per-component state environment (Slice B3), to a fixed point: seed each
    world from its activation binding, then join in every tainted write from every
    method (methods run in unknown order, so the join over all writers is the only
    sound seed). Non-enforcing — the refusal pass runs afterwards with this env."""
    state_env: dict = {}
    act_steps = [s for s in body
                 if isinstance(s, dict) and s.get("step") != "provide"]
    act = _FlowChecker(model, source, line, signatures=signatures, enforce=False,
                       known_callables=known, any_sink=any_sink,
                       state_env=state_env, state_names=state_names,
                       secret_config=secret_config)
    act_env: dict = {}
    act.run(act_steps, act_env)
    for name in state_names:
        seeded = act_env.get(name)
        if seeded is not None and seeded.dirty:
            real = frozenset(o for o in seeded.origins if _param_index(o) is None)
            if real:
                state_env[name] = Taint(real, seeded.via)
    # A WRITE INTO a state world from the activation body counts too. There are
    # two ways a world becomes tainted, and the loop above sees only the first:
    #
    #   let store = effect Map.new()             # the BINDING carries the taint
    #   effect store.insert("k", config.token)   # a CALL writes INTO the world
    #
    # The second leaves nothing in `act_env["store"]` — `store` is bound to a
    # clean `Map.new()` and never rebound. `_taint_of_state_access` records it
    # where every other writer is recorded, `state_writes`, which the method
    # fixpoint below already folds in. Fold the activation body's the same way,
    # or a world written only there is seeded CLEAN and every later `.get()`
    # reads back clean — a declared `Secret[T]` losing its `confidential` origin
    # across a state write, and reaching a disclosure sink with no `endorse`
    # (§7, docs/design/256-capability-bound-secrets.md). The identical program
    # with the `insert` inside a provide method was always refused; where the
    # write is spelled is not a confidentiality boundary.
    for name, taint in act.state_writes.items():
        joined = _join(state_env.get(name, CLEAN), taint)
        if joined.dirty:
            state_env[name] = joined

    methods: list = []
    for step in body or []:
        if isinstance(step, dict) and step.get("step") == "provide":
            methods += step.get("methods") or []
    bound = 2 * len(methods) + 6
    changed = True
    while changed and bound > 0:
        changed = False
        bound -= 1
        for method in methods:
            mname = method.get("name")
            checker = _FlowChecker(
                model, source, method.get("line") or line, signatures=signatures,
                enforce=False, known_callables=known, any_sink=any_sink,
                state_env=state_env, state_names=state_names,
                secret_config=secret_config)
            env: dict = {}
            _seed_param_env(model, mname, method.get("params"), env)
            checker.run(method.get("body") or [], env)
            for name, taint in checker.state_writes.items():
                old = state_env.get(name, CLEAN)
                joined = _join(old, taint)
                if joined.origins != old.origins:
                    state_env[name] = joined
                    changed = True
    return state_env


def _infer_signatures(fns, components, model: TaintModel, filename: str,
                      known: frozenset = frozenset(),
                      any_sink: bool = False) -> dict:
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
    comp_config = component_config_order(components)
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
        for (qual, key, params, body, source, line, secret_config,
             config_fields) in callables:
            # A config pseudo-callable's "parameters" are read as `config.<f>`,
            # not as names, so the markers are seeded into `config_env`.
            seed_target: dict = {}
            checker = _FlowChecker(model, source, line, signatures=signatures,
                                   infer=True, qualname=qual,
                                   known_callables=known, any_sink=any_sink,
                                   secret_config=secret_config,
                                   comp_config=comp_config,
                                   config_env=(seed_target if config_fields
                                               else None))
            env: dict = {}
            seeded = _declared_param_origins(model, key)
            for i, pname in enumerate(params):
                origins = {_param_marker(i)} | set(seeded.get(i, ()))
                (seed_target if config_fields else env)[pname] = Taint(
                    frozenset(origins), (pname,))
            checker.run(body, env)
            flows = {i for i in range(len(params))
                     if _param_marker(i) in checker.return_taint.origins}
            mints = {o for o in checker.return_taint.origins
                     if _param_index(o) is None}
            if signatures[key].merge(flows, mints, checker.sink_hits,
                                     checker.endorse_clears):
                changed = True
    return signatures


def check_taint(program, fns, components, model: TaintModel,
                filename: str, untrusted: bool = False) -> None:
    """Refuse any untrusted value that reaches a sink without a declassifier
    (G9). No-op — and byte-identical — when the program uses no qualifier.

    `untrusted` (item 274) marks the untrusted-author profile: a G9 sink
    refusal then carries the collapsed navigable verdict instead of teaching a
    declassifier the profile's `no_declassify` would itself refuse.

    Slice B: taint propagates across call boundaries. First infer a per-callable
    taint signature to a least fixed point, then make one refusal pass in which
    every call site applies the callee's signature — a tainted argument reaching
    a `reaches_sink` position is refused at the *call site*, with a via chain
    that crosses component boundaries."""
    if not model.active:
        return

    known = _known_callables(program, fns, components, model)
    any_sink = bool(model.sinks)
    signatures = _infer_signatures(fns, components, model, filename, known, any_sink)
    comp_config = component_config_order(components)

    # top-level pure fns (lowered IR): seed params declared `Untrusted[T]`, run
    # the refusal pass, AND collect each fn's declassification/reach provenance
    # (item 249, Finding 2). A top-level `fn` declassifier — `endorse[web] fn
    # wash(...)` — is not owned by a component, so without this its downgrade is
    # invisible to the `declassify:` audit token, `may not declassify` policy, and
    # `audit --diff`. Every component that (transitively) reaches the washer folds
    # its provenance onto its own boundary entry, exactly as a provide method's is.
    fn_prov: dict[str, tuple[set, list, set]] = {}
    for fn in fns:
        checker = _FlowChecker(model, fn.get("source") or filename,
                               fn.get("line") or 0, signatures=signatures,
                               endorse_allowed=model.declared_endorse.get(
                                   fn["name"], frozenset()),
                               endorse_label=fn["name"],
                               known_callables=known, any_sink=any_sink,
                               untrusted=untrusted)
        env: dict = {}
        _seed_param_env(model, fn["name"], fn.get("params"), env)
        checker.run(fn.get("body") or [], env)
        if checker.declassified or checker.reaches:
            fn_prov[fn["name"]] = (set(checker.declassified),
                                   list(checker.declassify_records),
                                   set(checker.reaches))

    # the fn -> fn call graph, walked only when a top-level fn actually declassifies
    # (empty otherwise, so an endorse-free program stays byte-identical). Lets a
    # component fold the provenance of every washer it reaches, directly or through
    # another fn.
    fn_names = {fn.get("name") for fn in fns}
    fn_calls: dict[str, set] = {}
    if fn_prov:
        from .emission_analysis import _calls_in  # noqa: PLC0415 — lazy, avoids cycle
        for fn in fns:
            called: set = set()
            _calls_in(fn.get("body") or [], called)
            fn_calls[fn["name"]] = {c for c in called if c in fn_names}

    def _reached_fns(body) -> set:
        """Top-level fn names a component body reaches, transitively."""
        direct: set = set()
        _calls_in(body, direct)
        seen: set = set()
        stack = [c for c in direct if c in fn_names]
        while stack:
            name = stack.pop()
            if name in seen:
                continue
            seen.add(name)
            stack.extend(fn_calls.get(name, ()))
        return seen

    # the IR carries no per-body line, so fall back to the component's declared
    # line (from the AST) — better than 0 for a component-body refusal.
    comp_lines = {c.name: getattr(c, "line", 0)
                  for c in getattr(program, "components", ())}

    # component provide-method bodies (lowered IR)
    for comp in components:
        source = comp.get("source") or filename
        comp_body = comp.get("body") or []
        line = comp_lines.get(comp.get("name"), 0)
        # Slice B3: thread taint through component state — compute each world's
        # accumulated taint to a fixed point before the refusal pass, so a value
        # stored by one method is seen tainted when another reads it.
        state_names = _state_bindings(comp_body)
        secret_config = secret_config_fields(comp)
        state_env = (_infer_state_env(comp_body, model, source, line, signatures,
                                      state_names, known, any_sink,
                                      secret_config)
                     if state_names else {})
        reaches, declassified, records, approvals = _walk_component_methods(
            comp_body, model, source, line, signatures,
            comp.get("name") or "", known, any_sink, state_env, state_names,
            untrusted=untrusted, secret_config=secret_config,
            comp_config=comp_config)
        # item 249, Finding 2: fold the provenance of every top-level fn washer
        # this component reaches onto its own surface, so a declassification done
        # inside a helper fn is not invisible to the audit token / policy.
        if fn_prov:
            for name in _reached_fns(comp_body):
                fp = fn_prov.get(name)
                if fp is None:
                    continue
                declassified |= fp[0]
                records.extend(fp[1])
                reaches |= fp[2]
        # fold the per-component provenance onto the IR entry (Decision 5), so
        # `_boundary` can emit `taint:`/`declassify:` tokens. Additive: absent
        # when the component touches no taint, so its IR stays byte-identical.
        if reaches or declassified:
            comp["taint"] = {
                "reaches": sorted(reaches),
                "declassify": sorted(declassified),
                # Slice D (D2): approval scopes threaded on tainted sends, so the
                # `<origin>-taint may not reach <cap> without approval` tier can
                # tell an approved flow from a bare one. Present only when a
                # tainted send is approval-covered, so it never moves a plain surface.
                **({"reach_approvals": sorted(approvals)} if approvals else {}),
                # Slice C: the enriched declassify records ride beside the coarse
                # `declassify:<origin>` token (which stays the stable diff key).
                # Sorted for a deterministic audit surface.
                **({"declassify_records": sorted(
                    records, key=lambda r: (str(r.get("origin")),
                                            str(r.get("method")),
                                            r.get("line") or 0))}
                   if records else {}),
            }


# ---------------------------------------------------------------------------
# item 444: the compile-to-runtime taint-origin channel
# ---------------------------------------------------------------------------


def _secret_param(params) -> bool:
    """Whether any declared parameter carries the `Secret[T]` IR marking."""
    if not isinstance(params, (list, tuple)):
        return False
    return any(isinstance(p, dict) and p.get("secret") for p in params)


def declares_confidential_surface(ir) -> bool:
    """Whether a composition declares ANY surface that can mint the `secret` or
    `confidential` origin, read off the IR document alone (item 444).

    These declarations are the only places the two origins enter the value
    graph. `extract_and_normalize` mints `secret` from a bound emission
    capability (`secret_caps`, item 256 Slice 1 — the `secrets` IR key) and
    `confidential` from a `Secret[T]` extern return (`secret_return`), a
    `Secret[T]` parameter on an extern / service operation / top-level fn
    (`params[i]["secret"]`) and a `Secret[T]` config field (`config[i]["secret"]`)
    — and from nothing else. A composition that declares none of them therefore
    cannot produce a value carrying either origin, anywhere, at any crossing.

    Every marking read here is one the compiler already stamps for the runtime's
    own redaction (`backends/python/confidential.SecretIndex` reads the same
    keys), so this adds no IR surface of its own."""
    if not isinstance(ir, dict):
        return True                                  # no IR is no proof
    if ir.get("secrets"):
        return True
    for spec in (ir.get("services") or {}).values():
        for method in ((spec or {}).get("methods") or {}).values():
            if _secret_param((method or {}).get("params")):
                return True
    for ext in (ir.get("externs") or ()):
        if not isinstance(ext, dict):
            continue
        if ext.get("secret_return") or ext.get("secrets"):
            return True
        if _secret_param(ext.get("params")):
            return True
    for fn in (ir.get("functions") or ()):
        if isinstance(fn, dict) and _secret_param(fn.get("params")):
            return True
    for comp in (ir.get("components") or ()):
        if not isinstance(comp, dict):
            continue
        for cfield in (comp.get("config") or ()):
            if isinstance(cfield, dict) and cfield.get("secret"):
                return True
    return False


class OriginIndex:
    """What the taint checker proved about a composition, read back off the IR
    document the driver already holds (roadmap item 444).

    Item 121's `promptDigest` is a fail-closed gate: `revl_prompt_digest`
    (`backends/python/runtime.py`) emits a digest ONLY when taint analysis is
    engaged AND the crossing's arguments are proven to carry neither `secret`
    (the bound provider key, item 256 Slice 1) nor `confidential` (the
    `Secret[T]` qualifier, Slice 3). Until this landed the driver had no channel
    to answer either question and passed `taint_engaged=False` unconditionally,
    so the digest was suppressed on every shipped run — safe, and inert.

    The channel is the IR itself, so no new IR key exists and every golden
    document stays byte-identical. Two facts are read off it:

    * :attr:`engaged` — the whole-program CERTIFICATE
      (:func:`declares_confidential_surface`, inverted). True only when the
      composition declares no surface that can mint `secret` or `confidential`
      anywhere. That it is a DECLARATION-level proof is exactly what makes it
      sound: it is deliberately NOT a per-crossing judgment, because in the
      refusal pass an unqualified parameter is seeded CLEAN, so a per-crossing
      origin set is an UNDER-approximation across a call boundary — and a
      fail-closed gate must never rest on one.

    * :meth:`origins_for` — the origins the checker's flow walk recorded as
      REACHING an emission crossing in that component (`comp["taint"]["reaches"]`,
      item 249 Decision 5): the union over that component's crossings, so it
      OVER-approximates any single one. It can therefore only over-suppress,
      never under-suppress — the same direction as `SecretIndex.crossing`'s
      operation-name fallback.

    Both must hold for a digest to be emitted. The certificate is what turns
    `taint_engaged` on; the origins are then re-checked by `revl_prompt_digest`
    itself, so a future checker change that let `confidential` reach a crossing
    under a clean certificate is still caught by the gate it already has. A
    composition that declares ANY confidentiality surface certifies FALSE and
    every one of its crossings stays suppressed."""

    __slots__ = ("_certified", "_reaches")

    def __init__(self, ir=None) -> None:
        # A document with no `components` key is not a composition this can
        # reason about (a bare or partial driver, a generation not yet
        # compiled): unproven, so the gate stays closed.
        usable = isinstance(ir, dict) and "components" in ir
        self._certified = bool(usable) and not declares_confidential_surface(ir)
        self._reaches: dict = {}
        if not usable:
            return
        for comp in (ir.get("components") or ()):
            if not isinstance(comp, dict):
                continue
            comp_taint = comp.get("taint")
            if isinstance(comp_taint, dict):
                self._reaches[comp.get("name")] = frozenset(
                    comp_taint.get("reaches") or ())

    @property
    def engaged(self) -> bool:
        """Whether the taint analysis counts as ENGAGED for the digest gate: the
        composition is certified to declare no `secret`/`confidential` surface,
        so what the flow walk recorded at a crossing is a complete account of
        the origins that crossing's arguments can carry."""
        return self._certified

    def origins_for(self, component) -> frozenset | None:
        """The origins the checker recorded reaching an emission crossing in
        `component`, or ``None`` when the composition is not certified (which
        `revl_prompt_digest` treats as unproven and suppresses). A certified
        component the walk recorded nothing for carries no origin at all."""
        if not self._certified:
            return None
        return self._reaches.get(component, frozenset())


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
                            component: str = "",
                            known: frozenset = frozenset(),
                            any_sink: bool = False,
                            state_env: dict | None = None,
                            state_names: frozenset = frozenset(),
                            untrusted: bool = False,
                            secret_config: frozenset = frozenset(),
                            comp_config: dict | None = None
                            ) -> tuple[set, set, list, set]:
    reaches: set = set()
    declassified: set = set()
    records: list[dict] = []
    approvals: set = set()

    # The ACTIVATION body (every step that is not a `provide` block). It runs once
    # when the component activates and is exactly where `examples/migrator.rvl` and
    # `examples/fault_sweep_two_phase.rvl` teach authors to write emissions — so an
    # `emit` here is a real boundary crossing and must face the same refusals a
    # `provide` method's does. Nothing enforced over it before: the only other walk
    # of these steps is `_infer_state_env`'s deliberately NON-enforcing seeding
    # sweep, which runs BEFORE the state fixed point has converged (refusing there
    # would depend on iteration order, and it is skipped entirely when the component
    # has no state world). That sweep stays non-enforcing; this is the one enforcing
    # pass, run after `state_env` is final, so each activation-body statement is
    # walked by exactly one enforcing checker and no diagnostic is reported twice.
    act_steps = [s for s in body
                 if isinstance(s, dict) and s.get("step") != "provide"]
    if act_steps:
        act = _FlowChecker(
            model, source, line, signatures=signatures,
            # a component activation body carries no `endorse[...]` slot of its
            # own (only a `fn` or a service operation can declare one), so an
            # `endorse` written here is undeclared and refused — a declassification
            # is never ambient (Slice C).
            endorse_allowed=frozenset(),
            endorse_label=f"{component} activation" if component else "activation",
            known_callables=known, any_sink=any_sink,
            state_env=state_env if state_env is not None else {},
            state_names=state_names, untrusted=untrusted,
            # an activation-body `return` is not a crossing of the service / MCP
            # bridge, so the provide-method return rules do not apply here.
            provide_return=False, secret_config=secret_config,
            comp_config=comp_config)
        act.run(act_steps, {})
        reaches |= act.reaches
        declassified |= act.declassified
        records.extend(act.declassify_records)
        approvals |= act.reach_approvals

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
                    endorse_label=label, known_callables=known, any_sink=any_sink,
                    state_env=state_env if state_env is not None else {},
                    state_names=state_names, untrusted=untrusted,
                    provide_return=True, secret_config=secret_config,
                    comp_config=comp_config)
                env: dict = {}
                _seed_param_env(model, mname, method.get("params"), env)
                checker.run(method.get("body") or [], env)
                reaches |= checker.reaches
                declassified |= checker.declassified
                records.extend(checker.declassify_records)
                approvals |= checker.reach_approvals
    return reaches, declassified, records, approvals
