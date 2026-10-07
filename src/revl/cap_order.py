"""The capability partial order for parameterized tokens (item 294, Slice 1).

A capability stops being a bare token compared by identity and becomes a point
in a partial order UNDER its token: a pair `(T, P)` of a token `T` (exactly
today's dotted token) and a valuation `P`, a finite map from parameter names to
values. This module is the ONE place a parameterized capability is parsed, the
ONE registry of parameter kinds and their value orders, and the ONE definition
of the `covers` relation. Every fold that compares capabilities (the spawn
attenuation bridge in `lower.py`, and later the G4 bound, the gate ledger, and
the 411 plan gate) imports `covers`/`covers_set` from here so the algebra has a
single implementation (the representation mandate, docs/design/294...md).

Slice 1 is static only: the source grammar, the closed registry, the partial
order, and the key-to-token attenuation bridge. The lease runtime, cone-aware
grant lookups, and 411 mount enforcement are later slices.

Additivity is the load-bearing property: a bare token `fs.write` parses to
`Cap("fs.write", ())` with an empty valuation, and every `covers` comparison
between empty-valuation pairs reduces to token identity - bit-for-bit the old
string comparison. No parameter, no behaviour change.

issue #1938: the registry is closed against UNDECLARED names, not against
extension. A capability may declare its own resource dimensions
(`capability mail.send(account: discrete)`), which then bind from call
arguments and appear in the capability's valuation - and therefore in the
approval ticket's spelling and in the audit token - where a fact carried
outside the spelling would not be audited at all. The declaration is threaded
in explicitly (`declared`/`declarations` parameters, never module state), and
what it may NOT do is choose a new ORDER: `CORE_KINDS` is the three orders
above. Because a canonical value's TYPE encodes its order, the order algebra
(`covers`, `to_str`, `split_ceilings`) stays declaration-free, and only
admission plus value canonicalization consult the declaration.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass


# issue #1985: an ARGUMENT-bound capability parameter value is the enclosing
# declaration's own parameter name - a bare ident. Unambiguous against every
# other value form by construction: a quoted literal, an integer, and a
# `config.<field>` path all read differently.
_ARGUMENT_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")


class CapError(ValueError):
    """A malformed or unregistered parameterized capability. Carries an optional
    `hint` so the parser can surface it with the same shape as its other
    refusals (closed registry: unknown parameter names refuse AT PARSE)."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


# --------------------------------------------------------------- the registry
#
# CLOSED by construction: a parameter name absent from this table refuses at
# parse rather than defaulting to a discrete order. A discrete default would
# make the no-widening invariant false as written and opens a typo hazard
# (`pth="/data"` would parse, narrow nothing, and partition the token's cone).
# Adding a parameter kind is adding a row here, with its kind, its value order,
# and its canonicalization. `kind` is "resource" (compared at crossings) or
# "ceiling" (declaration-to-declaration only in slice 1; erased into a grant's
# remainingUses at mint in slice 2).
#
# issue #1938: the registry is closed against UNDECLARED names, not against
# extension. A capability may DECLARE its own resource parameters
# (`capability mail.send(account: discrete)`), and a declared name is then legal
# for that capability exactly where a core name is. What a declaration may NOT
# do is choose a new ORDER: `CORE_KINDS` is the same three orders this table
# already spells, so the no-widening argument's "three value orders" count is
# unchanged. The point of the declaration is that the fact it names - which
# mailbox sends - becomes part of the capability's valuation, and therefore of
# the approval ticket's spelling and of the audit token, instead of living
# outside them in a gate spelling the audit never sees.

_RESOURCE = "resource"
_CEILING = "ceiling"

#: The kind spellings a `capability <token>(<name>: <kind>)` declaration may
#: use, and the `(kind, order)` row each denotes (issue #1938). Deliberately the
#: SAME three orders `_REGISTRY` already uses, so declaring a dimension adds a
#: name and never an order. `ceiling` is the only ceiling surface a declaration
#: offers: a declared ceiling is a plain non-negative integer, so no unit
#: vocabulary (`bytes`/`duration` suffixes) has to be re-spelled outside the
#: registry that owns it.
CORE_KINDS: frozenset[str] = frozenset({"path", "discrete", "ceiling"})

_KIND_ORDERS: dict[str, tuple[str, str]] = {
    "path": (_RESOURCE, "path"),
    "discrete": (_RESOURCE, "discrete"),
    "ceiling": (_CEILING, "ceiling"),
}


def _canon_path(raw: str) -> tuple[str, ...]:
    """Canonicalize a path VALUE to its component list, lexical only.

    Split on `/`, drop a single trailing slash (`/tmp/` == `/tmp`, one spelling
    per cone), refuse `.`/`..`/empty components, refuse a non-absolute path, and
    refuse `"/"` itself (a valid path value has at least one component; a
    root-wide narrowing narrows nothing and is already spelled by the bare
    token). Symlinks, case folding, and Unicode normalization are runtime facts
    about a real filesystem; the static order does not claim them."""
    if not raw.startswith("/"):
        raise CapError(
            f"path value `{raw}` is not absolute",
            hint="a `path=` capability parameter names an absolute path "
                 "(`path=\"/data/incoming\"`); a relative path has no cone")
    body = raw[1:]
    if body.endswith("/"):
        body = body[:-1]
    parts = body.split("/") if body else []
    components: list[str] = []
    for part in parts:
        if part == "":
            raise CapError(
                f"path value `{raw}` has an empty component (a `//`)",
                hint="write each component once, separated by a single `/`")
        if part in (".", ".."):
            raise CapError(
                f"path value `{raw}` has a `{part}` component",
                hint="the static path order is lexical and does not resolve "
                     "`.`/`..`; spell the canonical path")
        components.append(part)
    if not components:
        raise CapError(
            'path value "/" narrows nothing',
            hint="a root-wide path is already spelled by the bare token "
                 "(`fs.write` means all of `fs.write`); a `path=` value must "
                 "have at least one component")
    return tuple(components)


def _forbidden_chars(raw: str, name: str) -> None:
    """The canonical stored spelling is `token(name="value",...)`; a value
    carrying one of the format's own metacharacters would not round-trip, so it
    is refused at parse. Paths, hostnames, and table names never need them."""
    for ch in '"(),=':
        if ch in raw:
            raise CapError(
                f"capability parameter `{name}` value contains a `{ch}`",
                hint="a capability parameter value may not contain any of "
                     "`\" ( ) , =` (the token spelling reserves them)")


def _canon_discrete(raw: str) -> str:
    return raw


# 1024-based byte units and millisecond-based duration units for the emission
# budget ceilings (item 260, Slice 3): `bytes="10MB"` canonicalizes to
# `10485760`, `time="2s"` to `2000` (ms). Wall-clock and payload size are
# runtime quantities; the static order only compares the declared ceilings.
_BYTE_UNITS: dict[str, int] = {
    "b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3, "tb": 1024 ** 4}
_DURATION_UNITS: dict[str, int] = {
    "ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000}


def _parse_suffixed(raw: str, units: dict[str, int], name: str,
                    what: str, example: str) -> int:
    """Canonicalize a `<int><unit>` budget literal (`"10MB"`, `"2s"`) to its base
    integer (bytes / milliseconds). The suffix is matched longest-first so `ms`
    wins over `s` and `kb` over `b`; the numeric prefix is a non-negative
    integer. A bare integer arrives as a python `int` and never reaches here."""
    low = raw.strip().lower()
    for unit in sorted(units, key=len, reverse=True):
        if low.endswith(unit):
            num = low[:-len(unit)].strip()
            if num:
                try:
                    return int(num) * units[unit]
                except ValueError:
                    break
            break
    raise CapError(
        f"capability parameter `{name}` value `{raw}` is not {what}",
        hint=f"write e.g. `{example}` (an integer with a unit suffix; "
             f"units: {', '.join(sorted(units))})")


# name -> (kind, order-key). The order-key selects the value order in
# `_param_leq`; canonicalization is applied at parse by `_canon_value`.
#
# Symbolic values (`path=config.job_root`, item 294 Slice 2) are `Symbol`s:
# comparable ONLY to the identical symbol until a spawn-site `with { }` literal
# binding substitutes them (`substitute`), giving per-instance attenuation. An
# unresolved symbol is incomparable to a literal, hence refused (fail closed).
# Ceiling-kind parameters ERASE at mint into a grant's `remainingUses` counter
# and are EXEMPT from crossing-coverage (a crossing binds no ceiling): the
# spawn-attenuation surface strips them (`split_ceilings`) before `covers_set`,
# and the DEDICATED ceiling-attenuation check (item 260, `lower._check_spawn_
# attenuation`) compares them separately over the unstripped pairs. The gate side
# (`mcp/session.py`) owns the mint-time erasure and the cone-aware grant lookups.
_REGISTRY: dict[str, tuple[str, str]] = {
    "path": (_RESOURCE, "path"),
    "host": (_RESOURCE, "discrete"),
    "table": (_RESOURCE, "discrete"),
    "calls": (_CEILING, "ceiling"),
    "size": (_CEILING, "bytes"),
    "time": (_CEILING, "duration"),
}

# Budget sugar (item 260 §3.1): `budget.requests`/`budget.bytes` reconcile onto
# the ceiling params `covers` already orders. `requests` is an ALIAS of `calls`
# (the same quantity cardinality proves, so the static check is exactly
# "proved-max <= declared calls"); `bytes` an alias of `size`. The alias is
# resolved at parse so both spellings canonicalize to one stored param.
_ALIASES: dict[str, str] = {
    "requests": "calls",
    "bytes": "size",
}


def _normalize(name: str) -> str:
    """Resolve a budget alias (`requests` -> `calls`, `bytes` -> `size`) to its
    canonical stored parameter name; every other name is returned unchanged."""
    return _ALIASES.get(name, name)


# ------------------------------------------------- declared dimensions (#1938)
#
# A capability's own resource dimensions. `Declared` is the declaration for ONE
# capability token (parameter name -> kind spelling); `DeclaredMap` is every
# declaration a compilation unit holds (capability token -> Declared). Both are
# plain JSON-shaped maps because they travel on the IR.
Declared = Mapping[str, str]
DeclaredMap = Mapping[str, Declared]

# The READ sentinel: `parse_stored_cap` passes this as `declared` to say "this is
# a stored, already-canonical spelling whose declaring capability's declaration
# is not in scope". It is deliberately NOT a mapping - `declared_order` returns
# None for it, so nothing can accidentally treat it as a declaration.
_ADMIT_UNKNOWN = object()


def declarations_of(decls: "Iterable[object] | None") -> dict[str, Declared]:
    """Fold a program's `capability` declarations into the `token -> Declared`
    map the readers take (issue #1938).

    Each element is a declaration NODE (`revl.parser.CapabilityDecl`), read by
    the two attributes it must have: `.token` (the dotted capability token) and
    `.parameters` (an iterable of `(name, kind, line)`). Kept here rather than
    on `Program` so every reader - the lowerer, the distiller, a policy load -
    folds it the same way, and so `cap_order` stays free of a `parser` import.

    An empty result is the identity: every existing program declares nothing,
    so every read path keeps the core registry as its only name source."""
    out: dict[str, dict[str, str]] = {}
    for decl in decls or ():
        out[decl.token] = {name: kind for name, kind, _line in decl.parameters}
    return out


def declared_order(name: str, declared: "Declared | None") -> tuple[str, str] | None:
    """The `(kind, order)` a capability's own declaration gives `name`, or None
    when `declared` does not name it. A declaration may only choose from
    `CORE_KINDS`, so the vocabulary of ORDERS does not grow.

    `declared` may be the `_ADMIT_UNKNOWN` read sentinel (issue #1938), which is
    not a mapping and names nothing: it answers None, so a stored read resolves
    only core rows and every declared name takes the type-based path."""
    if not isinstance(declared, Mapping):
        return None
    kind = declared.get(name)
    if kind is None:
        return None
    return _KIND_ORDERS[kind]


def _order_of(name: str, declared: "Declared | None") -> tuple[str, str]:
    """The `(kind, order)` of a parameter name: the core registry row, else the
    declaring capability's own declaration.

    Raises when neither holds the name. That raise is the FAIL-CLOSED backstop
    and the reason the registry stays closed against undeclared names: a
    declared spelling read where its declaration is not in scope refuses, rather
    than being admitted at an order guessed from its bytes."""
    row = _REGISTRY.get(name)
    if row is not None:
        return row
    row = declared_order(name, declared)
    if row is None:
        raise CapError(f"unknown capability parameter `{name}`")
    return row


def parse_declarations(pairs: "Iterable[tuple[str, str]]", *, what: str) -> dict[str, str]:
    """Validate one `capability <token>(<name>: <kind>, ...)` declaration's
    parameter list into `{name: kind}` (issue #1938).

    The kind vocabulary is closed to `CORE_KINDS` - the same three value orders
    the core registry already spells - so declaring a dimension adds a NAME the
    capability may bind, never a new ORDER the no-widening argument has to
    re-prove. A name the core registry already holds is refused: a declared
    dimension may not re-spell a core one, or one name would denote two orders
    depending on which declaration happened to be in scope."""
    out: dict[str, str] = {}
    for name, kind in pairs:
        if name in out:
            raise CapError(
                f"duplicate capability parameter `{name}` in the declaration "
                f"of `{what}`",
                hint="declare each parameter once")
        if kind not in CORE_KINDS:
            known = ", ".join(f"`{k}`" for k in sorted(CORE_KINDS))
            raise CapError(
                f"unknown capability parameter kind `{kind}` for `{name}` in "
                f"the declaration of `{what}`",
                hint="a declared dimension chooses one of the three value "
                     f"orders the core registry already has: {known}")
        if _normalize(name) in _REGISTRY:
            raise CapError(
                f"the declared parameter `{name}` of `{what}` is already a "
                "core capability parameter",
                hint="a declared dimension adds a NAME the core registry does "
                     "not hold; a core name keeps its core order everywhere")
        out[name] = kind
    return out


def is_registered(name: str, declared: "Declared | None" = None) -> bool:
    """Whether `name` is a legal parameter name: a core registry row, or - issue
    #1938 - a name the declaring capability DECLARES. Every other name still
    refuses, so the typo hazard is unchanged in kind."""
    return _normalize(name) in _REGISTRY \
        or declared_order(name, declared) is not None


def registered_names(declared: "Declared | None" = None) -> list[str]:
    if not isinstance(declared, Mapping):
        declared = None
    return sorted(set(_REGISTRY) | set(_ALIASES) | set(declared or ()))


def is_ceiling(name: str, declared: "Declared | None" = None) -> bool:
    """Whether a parameter is ceiling-kind (`calls`/`requests`, `size`/`bytes`,
    `time`, or a declared `ceiling`): compared declaration-to-declaration and
    mint-vs-declaration, ERASED from a grant's valuation at mint (translated into
    `remainingUses`), and EXEMPT from crossing-coverage. Resource kinds (`path`,
    `host`, `table`, and every declared `path`/`discrete`) are compared at
    crossings (Slice 2)."""
    name = _normalize(name)
    row = _REGISTRY.get(name) or declared_order(name, declared)
    return row is not None and row[0] == _CEILING


def split_ceilings(cap: "Cap") -> "tuple[Cap, dict[str, int]]":
    """Split a `Cap` into its resource-only projection and its ceiling map.

    A CROSSING binds no ceiling parameter (one call is one call), so a grant
    that kept `calls=N` in its valuation would cover no crossing ever. At mint
    the ceiling params are stripped from the stored capability (so the grant's
    cone is compared on its resource parameters alone) and returned separately;
    the gate translates `calls` into the shipped `remainingUses` counter. The
    resource projection re-canonicalizes through `Cap`, so a grant spelled
    `model.complete(calls=3)` stores as bare `model.complete` with `{calls: 3}`
    peeled off.

    The split reads the KIND off the canonical VALUE's own type rather than off
    the name (issue #1938): canonicalization stores every ceiling as an `int`
    and every resource kind as a `str`, a component `tuple`, or a `Symbol`
    (`_canon_value` refuses a `Symbol` on a ceiling), so the two kinds are
    disjoint by construction and a DECLARED ceiling strips exactly like a core
    one with no declaration in scope."""
    ceilings: dict[str, int] = {}
    kept: list[tuple[str, object]] = []
    for name, value in cap.params:
        if isinstance(value, int) and not isinstance(value, bool):
            ceilings[name] = value  # already an int by canonicalization
        else:
            kept.append((name, value))
    return Cap(cap.token, tuple(kept)), ceilings


def _canon_value(name: str, value: object,
                 declared: "Declared | None" = None) -> object:
    """Canonicalize (and validate) a raw parameter value for its order. String
    literals are expected for resource kinds, integers for ceilings. The path
    order stores a component tuple; discrete stores the string; ceiling stores
    the int. A `Symbol` - a per-instance `config.` value or the enclosing
    declaration's own argument (issue #1985) - is stored as-is on a resource
    kind and refused on a ceiling (a ceiling is a STATIC bound, item 294
    Slice 2).

    The order comes from `_order_of`: the core registry row, or - issue #1938 -
    the declaring capability's own declared kind. A declared name with no
    declaration in scope refuses here, which is what keeps a DECLARED spelling
    from being re-read at a guessed order."""
    kind, order = _order_of(name, declared)
    if isinstance(value, Symbol):
        if kind == _CEILING:
            what = ("an argument-bound" if value.is_arg
                    else f"a per-instance `{value.ref}`")
            raise CapError(
                f"capability parameter `{name}` is a ceiling and takes a static "
                f"integer, not {what} value",
                hint=f"write `{name}=10` (a static bound); a per-instance "
                     "`config.` value or a caller argument bounds a destination "
                     "(`path`/`host`/`table`), not a count")
        return value
    if order == "ceiling":
        if not isinstance(value, int) or isinstance(value, bool):
            raise CapError(
                f"capability parameter `{name}` expects an integer ceiling",
                hint=f"write `{name}=10` (a numeric bound); `{name}` is a "
                     "ceiling parameter")
        if value < 0:
            raise CapError(
                f"capability parameter `{name}={value}` is negative",
                hint="a ceiling is a count of at most N; write a non-negative "
                     "integer")
        return value
    if order in ("bytes", "duration"):
        # a byte or duration budget ceiling: a bare integer (base unit, or a
        # re-read canonical value) passes through; a suffixed string literal
        # (`"10MB"`, `"2s"`) canonicalizes to its base integer.
        if isinstance(value, bool):
            raise CapError(
                f"capability parameter `{name}` expects a size/duration ceiling")
        if isinstance(value, int):
            if value < 0:
                raise CapError(
                    f"capability parameter `{name}={value}` is negative",
                    hint="a ceiling is at most N; write a non-negative value")
            return value
        if isinstance(value, str):
            if order == "bytes":
                return _parse_suffixed(value, _BYTE_UNITS, name,
                                       "a byte size", 'bytes="10MB"')
            return _parse_suffixed(value, _DURATION_UNITS, name,
                                   "a duration", 'time="2s"')
        raise CapError(
            f"capability parameter `{name}` expects an integer or a "
            "unit-suffixed string")
    if order == "path" and isinstance(value, tuple):
        # already canonical (issue #1938): the caller holds the component tuple
        # rather than the quoted spelling, e.g. `distill._resource_join` joining
        # path cones. Every component was validated when the value was first
        # canonicalized, so re-validating would be a no-op.
        return value
    # resource kinds take a string literal
    if not isinstance(value, str):
        raise CapError(
            f"capability parameter `{name}` expects a string value",
            hint=f'write `{name}="..."` (a string literal)')
    _forbidden_chars(value, name)
    if order == "path":
        return _canon_path(value)
    return _canon_discrete(value)


# --------------------------------------------------------------- the pair (T, P)


@dataclass(frozen=True)
class Symbol:
    """A per-instance symbolic capability parameter value (item 294, Slice 2):
    the `config.job_root` in `fs.write(path=config.job_root)`.

    A symbol is OPAQUE to the static order - comparable only to the identical
    symbol (`_param_leq`) - until a spawn-site `with { }` block binds its config
    field to a string literal, where the checker substitutes it (`substitute`)
    and the resolved literal enters the cone. An unresolved symbol is
    incomparable to any literal, and incomparable means REFUSED, never admitted
    (fail closed): a child reaching `path=config.job_root` under a parent holding
    `path="/tmp"` is refused UNLESS the spawn resolves the symbol into the
    parent's cone. This is the design's first symbol rule - only a literal
    binding substitutes; anything else leaves the parameter symbolic.

    The SAME opacity carries the argument-bound destination (issue #1985):
    `emission[network.call(host=host)] fn get(url: Str, host: Str)` binds the
    declaration's own parameter into the token, so the declared destination is
    the CALLER's value rather than a compile-time constant. Its `ref` is the
    parameter name - a bare ident, never a `config.` path - and it is stored
    identically opaque: equal only to the identical binding, incomparable to any
    literal, hence never silently covered by a policy rule or a parent spelling
    a CONSTANT destination (`network.call(host="api.example.test")` refuses it,
    a bare `network.call` still covers it). Only a `config.` symbol is a spawn
    `with { }` binding target (`substitute`), so an argument is never resolved
    into a literal by a spawn site - there is nothing to resolve it FROM."""

    ref: str   # the source spelling: "config.job_root", or the argument "host"

    @property
    def is_arg(self) -> bool:
        """Whether the symbol names the enclosing declaration's own parameter
        (an argument-bound destination, issue #1985) rather than a per-instance
        `config.` field. Only a `config.` symbol is substitutable, so the two are
        never interchangeable: the distinction is carried by the spelling the
        parser produced, and re-read by `_parse_value`."""
        return not self.ref.startswith("config.")

    @property
    def field(self) -> str:
        """The config field the symbol names (`job_root` for `config.job_root`),
        the key a spawn-site `with { }` binds; for an argument symbol, the
        parameter's name."""
        return self.ref.split(".", 1)[1] if "." in self.ref else self.ref

    def to_str(self) -> str:
        """The canonical source-facing spelling: unquoted, so it re-reads as the
        symbolic binding it is and never as a discrete string."""
        return self.ref


@dataclass(frozen=True)
class Cap:
    """A capability as a point in the partial order: a token and a valuation.

    `params` is a tuple of `(name, value)` pairs sorted by name so two Caps with
    the same bindings are equal and hashable (they travel through `set`s in the
    attenuation fold). Values are canonical: a path is a component tuple, a
    discrete value a string, a ceiling an int. A bare token is `Cap(T, ())`."""

    token: str
    params: tuple[tuple[str, object], ...] = ()

    def param_map(self) -> dict:
        return dict(self.params)

    def is_bare(self) -> bool:
        return not self.params

    def to_str(self) -> str:
        """The canonical source-facing spelling. A bare token renders as the
        token itself (byte-identical to the old string), so parameter-free
        audit and IR output are unchanged."""
        if not self.params:
            return self.token
        rendered = ",".join(f"{n}={_render_value(v)}" for n, v in self.params)
        return f"{self.token}({rendered})"


def _render_value(value: object) -> str:
    """Render one canonical value back to its source spelling.

    The order is read off the VALUE's own type, never off the name (issue
    #1938): canonicalization stores a `path` as a component tuple, a `discrete`
    as a string, and every ceiling as an `int`, so the three orders are disjoint
    by construction. That is exactly what lets a DECLARED parameter round-trip
    through `parse_cap` - and through `Cap.to_str` at every fold that records a
    spelling - without the declaration in scope. Both string-shaped orders
    render as a quoted literal, so a path and a discrete of the same characters
    are one spelling, which is what makes the round-trip total."""
    if isinstance(value, Symbol):
        # unquoted, so it re-reads as a symbol and never as a discrete string
        return value.ref
    if isinstance(value, int) and not isinstance(value, bool):
        # every ceiling stores its canonical integer (a count, bytes, or ms);
        # rendering it as the bare integer round-trips through `_canon_value`.
        return str(value)
    if isinstance(value, tuple):
        return '"/' + "/".join(value) + '"'
    return f'"{value}"'


# --------------------------------------------------------------- construction
#
# Two constructors, one validator (`_make_cap`): `make_cap` from structured
# pieces (the parser hands it python str/int values), `parse_cap` from a
# canonical string (a fold re-reads a stored `capabilities` entry). The single
# canonical point the representation mandate requires is `_make_cap`.


def _make_cap(token: str, raw_params: list[tuple[str, object]],
              declared: "Declared | None" = None) -> Cap:
    if not token:
        raise CapError("empty capability token")
    seen: set[str] = set()
    canon: list[tuple[str, object]] = []
    has_params = bool(raw_params)
    if token == "*" and has_params:
        raise CapError(
            "`*` takes no capability parameters",
            hint="an unnameable reach cannot be bounded by name (the same rule "
                 "that refuses approving `*`); drop the parameter list")
    for raw_name, value in raw_params:
        # resolve a budget alias to its canonical stored name FIRST, so
        # `requests`/`calls` (and `bytes`/`size`) are one parameter: binding both
        # is a duplicate, and both canonicalize to a single stored spelling.
        name = _normalize(raw_name)
        if name in seen:
            raise CapError(
                f"duplicate capability parameter `{raw_name}`",
                hint="bind each parameter once")
        seen.add(name)
        if not is_registered(raw_name, declared):
            if declared is _ADMIT_UNKNOWN:
                # issue #1938: a STORED spelling read where the declaring
                # capability's declaration is not in scope. The value is already
                # canonical, so it passes through unchanged and `covers` reads
                # its order off its own type - which is exact for every order
                # except a declared `path`, whose component tuple renders
                # byte-identically to the `discrete` string of the same
                # characters. The degradation is therefore EQUALITY instead of
                # path containment: strictly narrower, so a read can never widen
                # a cone, only fail to find one (fail closed).
                canon.append((name, value))
                continue
            names = ", ".join(f"`{n}`" for n in registered_names(declared))
            raise CapError(
                f"unknown capability parameter `{name}`",
                hint=f"the capability parameter registry is closed; known "
                     f"parameters are {names} (a typo narrows nothing and is "
                     "refused rather than silently ignored)")
        canon.append((name, _canon_value(name, value, declared)))
    canon.sort(key=lambda kv: kv[0])
    return Cap(token, tuple(canon))


def make_cap(token: str, raw_params: list[tuple[str, object]] | None = None,
             declared: "Declared | None" = None) -> Cap:
    """Build a validated `Cap` from a token and raw `(name, value)` pairs (the
    parser's entry point: values are already python `str`/`int`/`Symbol`).

    `declared` is the declaration for THIS token (issue #1938), when one is in
    scope: a name it holds is admitted where a core name is, at the order the
    declaration chose. Omitting it is not a permissive default - every name
    outside the core registry still refuses."""
    return _make_cap(token, list(raw_params or []), declared)


def substitute(cap: Cap, bindings: dict[str, str],
               declared: "Declared | None" = None) -> Cap:
    """Resolve a Cap's per-instance `config.` symbols against a spawn site's
    literal `with { }` bindings (item 294, Slice 2).

    `bindings` maps a config FIELD name to a python string literal. Each symbol
    parameter whose field is bound is replaced by that literal and
    re-canonicalized through the ONE canonical point (`make_cap`), so a resolved
    `path` becomes a real cone element (a component tuple) comparable in the
    order. A symbol whose field is unbound is left symbolic (the caller omits a
    field bound to a non-literal, so its symbol also stays), hence still
    incomparable to a literal parent - refused, never admitted (fail closed).

    A cap carrying no symbol is returned unchanged (identity), so a
    parameter-free or literal-only reach is byte-identical: substitution is inert
    unless a per-instance symbol is actually present and actually bound.

    An ARGUMENT-bound symbol (issue #1985 - `host=host`) is never a binding
    target: a spawn site's `with { }` block binds a child's `config.` FIELDS, and
    an argument is supplied by the CALLER, not by the spawn. Substituting one
    would let `with { host: "/tmp" }` silently narrow a declaration whose
    destination nothing but the caller names - so only a `config.` symbol is
    resolved here."""
    if not any(isinstance(v, Symbol) for _n, v in cap.params):
        return cap
    raw: list[tuple[str, object]] = []
    for name, value in cap.params:
        if (isinstance(value, Symbol) and not value.is_arg
                and value.field in bindings):
            raw.append((name, bindings[value.field]))
        else:
            raw.append((name, value))
    # issue #1938: `substitute` only ever re-canonicalizes values that came off
    # an ALREADY-PARSED `Cap`, so a name the caller cannot classify is admitted
    # unchanged rather than refused. A resolved `config.` symbol for a DECLARED
    # path degrades from a component tuple to the equal-charactered `discrete`
    # string, which narrows the comparison to equality - fail closed, and
    # byte-identical for every core parameter, whose order is still read from
    # `_REGISTRY`.
    return _make_cap(cap.token, raw,
                     _ADMIT_UNKNOWN if declared is None else declared)


def parse_cap(text: str, declared: "Declared | None" = None) -> Cap:
    """Re-read a canonical capability spelling into a `Cap` (a fold's entry
    point over a stored `capabilities` string). A bare dotted token with no `(`
    is `Cap(token, ())`, so every pre-294 token round-trips unchanged.

    `declared` is the declaration for the capability this spelling names (issue
    #1938), when one is in scope. Passing it is what lets a DECLARED dimension
    be read back; omitting it is fail-closed, not permissive - the closure is
    enforced on the NAME here too, so a spelling carrying a declared parameter
    refuses where its declaration is not available. `covers`, `Cap.to_str` and
    `split_ceilings` deliberately need no declaration at all (they read the
    order off the canonical value's type), so a comparison over an
    already-canonical pair is declaration-free."""
    open_i = text.find("(")
    if open_i < 0:
        return _make_cap(text, [], declared)
    if not text.endswith(")"):
        raise CapError(f"malformed capability parameter list in `{text}`")
    token = text[:open_i]
    inner = text[open_i + 1:-1]
    raw: list[tuple[str, object]] = []
    if inner.strip():
        for piece in _split_params(inner):
            eq = piece.find("=")
            if eq < 0:
                raise CapError(f"malformed capability parameter `{piece}`")
            name = piece[:eq].strip()
            value = _parse_value(piece[eq + 1:].strip())
            raw.append((name, value))
    return _make_cap(token, raw, declared)


def parse_stored_cap(text: str) -> Cap:
    """Re-read a STORED canonical capability spelling whose declaring
    capability's declaration is not in scope (issue #1938).

    This is the fold-side twin of `parse_cap`. The approval WAL, a distilled
    `auto-approve` rule, an operator policy file and a `may reach` clause all
    hold a spelling that the frontend already canonicalized, and none of them
    can see the `capability <token>(...)` declaration that named its parameters -
    a policy document is loaded on its own, and a WAL record outlives the
    compilation that wrote it. Refusing such a spelling (as `parse_cap` does)
    would make a declared dimension unusable on every recorded surface, so the
    stored read ADMITS a name it cannot classify and passes the value through
    unchanged.

    What that costs, precisely: the value is already canonical, and its own
    Python type carries its order - a ceiling is an `int`, a `path` a component
    tuple, a `discrete` a string - so `covers`, `_param_leq` and `Cap.to_str`
    need no declaration. The one order a type cannot recover is a declared
    `path`, whose tuple renders to the same quoted bytes as the `discrete`
    string of the same characters; comparing such a pair degrades to EQUALITY
    instead of containment. Equality is strictly narrower than path containment,
    so the degradation can never widen a cone - it can only fail to find one,
    which is the fail-closed direction every other read here takes.

    Admission is untouched: `make_cap`/`parse_cap` still refuse an undeclared
    name at the frontend, which is where the typo hazard lives."""
    return parse_cap(text, _ADMIT_UNKNOWN)  # type: ignore[arg-type]


def _split_params(inner: str) -> list[str]:
    """Split a canonical `name=v,name=v` body on the top-level commas. Values
    are quoted strings or bare integers with no nested commas of their own (the
    forbidden-char rule guarantees it), so a plain split is safe."""
    parts: list[str] = []
    depth = 0
    in_str = False
    current: list[str] = []
    for ch in inner:
        if ch == '"':
            in_str = not in_str
            current.append(ch)
        elif ch == "," and not in_str and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _parse_value(raw: str) -> object:
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return raw[1:-1]
    if raw.startswith("config."):
        # a per-instance symbol re-read from its canonical (unquoted) spelling
        return Symbol(raw)
    if _ARGUMENT_RE.match(raw):
        # an argument-bound destination (issue #1985), re-read from its canonical
        # (unquoted) spelling: the enclosing declaration's own parameter name.
        # Unquoted, so it can never be mistaken for a discrete literal.
        return Symbol(raw)
    try:
        return int(raw)
    except ValueError as exc:
        raise CapError(f"malformed capability parameter value `{raw}`") from exc


def argument_bindings(caps: "list[str] | tuple[str, ...]",
                      declarations: "DeclaredMap | None" = None) \
        -> list[tuple[str, str]]:
    """The argument-bound destinations of a list of canonical capability
    spellings, as `(capability, parameter-name)` pairs (issue #1985).

    A declaration's own checker reads this to refuse a binding that names
    something other than one of ITS parameters - `emission[network.call(host=dest)]
    fn get(url: Str, host: Str)` names no destination anything enforces, so it is
    refused rather than stored as an opaque token nothing can ever satisfy.

    `declarations` maps a capability token to its own declared dimensions (issue
    #1938), so a spelling that carries a DECLARED parameter reads back; omitting
    it leaves the core registry as the only name source (fail closed)."""
    found: list[tuple[str, str]] = []
    for text in caps or ():
        token = text.split("(", 1)[0]
        declared = (declarations or {}).get(token)
        for _name, value in parse_cap(text, declared).params:
            if isinstance(value, Symbol) and value.is_arg:
                found.append((text, value.ref))
    return found


# --------------------------------------------------------------- the order


def _leq_path(narrow: tuple, wide: tuple) -> bool:
    """`narrow <=_path wide` iff `wide`'s component list is a PREFIX of
    `narrow`'s, component-wise (never string-prefix): `/tmp/job-42 <= /tmp`
    holds, `/tmp/jobber <= /tmp/job` does NOT."""
    if len(wide) > len(narrow):
        return False
    return narrow[:len(wide)] == wide


def _param_leq(narrow: object, wide: object) -> bool:
    """`narrow <=_k wide` in parameter `k`'s value order (narrow is at-or-below
    wide, i.e. narrower authority).

    The order is read off the canonical VALUES' own types, never off the
    parameter name (issue #1938): a `path` stores a component tuple, a
    `discrete` a string, and every ceiling an `int`, so the three orders are
    disjoint by construction and the comparison needs no declaration in scope.
    A declared `path`/`discrete`/`ceiling` therefore compares exactly like the
    core parameter of the same order."""
    if isinstance(narrow, Symbol) or isinstance(wide, Symbol):
        # a per-instance symbol is comparable ONLY to the identical symbol
        # (item 294, Slice 2): a symbol vs a literal - on either side - is
        # incomparable, hence not `<=` (fail closed). Two equal `Symbol`s
        # compare equal by frozen-dataclass identity of `ref`.
        return narrow == wide
    if isinstance(narrow, tuple) and isinstance(wide, tuple):
        return _leq_path(narrow, wide)
    if isinstance(narrow, int) and isinstance(wide, int) \
            and not isinstance(narrow, bool) and not isinstance(wide, bool):
        return narrow <= wide          # a smaller ceiling is narrower
    return narrow == wide               # discrete: equality only


def covers(a: Cap, b: Cap) -> bool:
    """`a covers b`  iff  `b <= a` (b is at-or-below a in the partial order):
    same token, and b narrows every parameter a binds.

    Clause 1 (token identity): distinct tokens are incomparable, exactly as
    today; parameterization never bridges tokens. `*` is strictly top of the
    whole order and covered only by `*`.

    Clause 2 (per parameter): for every parameter `k` bound on the WIDER side
    `a`, `b` must also bind `k` and `b[k] <=_k a[k]`. A parameter bound on `b`
    but absent from `a` is free on the wider side and only narrows. Hence a bare
    token tops its cone (a with no params covers every b with the same token),
    and dropping a parameter widens and is refused."""
    if a.token == "*":
        return b.token == "*"
    if b.token == "*":
        return False
    if a.token != b.token:
        return False
    bmap = b.param_map()
    for k, av in a.params:
        if k not in bmap:
            return False              # b drops a parameter a binds: b is wider
        if not _param_leq(bmap[k], av):
            return False
    return True


def disjoint(a: Cap, b: Cap) -> bool:
    """Whether two capabilities can never touch the same declared resource
    (item 259, S2.2). Defined on the SAME partial order `covers` uses, so the
    algebra stays in one place; the parallel-emission partition (`parallel.py`)
    consumes it to prove two emissions independent.

    Slice 1 is (D1) only:

      * (D1) DISTINCT TOKENS. Capabilities under different tokens are
        incomparable in the order (`covers` clause 1: parameterization never
        bridges tokens), so `send.email` and `db.write` can never name the same
        boundary. Distinct tokens are therefore disjoint. This is the whole of
        slice 1's independence proof.

    `*` (top of the order, an unnameable reach) is disjoint from NOTHING,
    including another `*`: it may reach any boundary, so it is never provably
    independent of anything. Any pair touching `*` returns False.

    NOT-YET (D2, deferred to a later slice). Two capabilities under the SAME
    token are disjoint when their resource valuations have no common lower bound
    (two sibling paths `path="/a"` vs `path="/b"`, neither a prefix of the other
    under `_leq_path`; two distinct discrete values `host="x"` vs `host="y"`),
    after projecting out ceiling parameters with `split_ceilings`. Until (D2)
    lands, this predicate treats EVERY same-token pair as NOT disjoint (returns
    False), which is fail-safe: (D2) can only ever ADD parallelism, never remove
    a sequential guarantee."""
    if a.token == "*" or b.token == "*":
        return False
    # (D1) distinct tokens are incomparable, hence disjoint. (D2) same-token
    # sibling-cone disjointness is deferred (see NOT-YET above), so a same-token
    # pair is conservatively NOT disjoint for now.
    return a.token != b.token


def covered_by_any(held: "list[Cap] | set[Cap]", c: Cap) -> bool:
    """Whether some held capability covers `c` (the per-child existential scan
    the attenuation fold runs)."""
    return any(covers(h, c) for h in held)


def covers_set(held: "list[Cap] | set[Cap]",
               reach: "list[Cap] | set[Cap]") -> list[Cap]:
    """The reach elements NOT covered by any held element - the attenuation
    check's `extra`. Empty means admitted; non-empty names the widenings."""
    return [c for c in reach if not covered_by_any(held, c)]
