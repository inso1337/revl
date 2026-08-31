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
"""

from __future__ import annotations

from dataclasses import dataclass


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

_RESOURCE = "resource"
_CEILING = "ceiling"


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


# name -> (kind, order-key). The order-key selects the value order in
# `_param_leq`; canonicalization is applied at parse by `_canon_value`.
#
# TODO(294-slice2): symbolic values (`path=config.job_root`), comparable only to
# the identical symbol until a spawn-site `with { }` literal binding substitutes
# them (per-instance attenuation).
# TODO(294-slice3): ceiling-kind parameters ERASE at mint into a grant's
# `remainingUses` counter and are EXEMPT from crossing-coverage; slice 1 only
# compares them declaration-to-declaration (the static order). The gate side
# (`mcp/session.py`) owns the erasure and the cone-aware grant lookups.
_REGISTRY: dict[str, tuple[str, str]] = {
    "path": (_RESOURCE, "path"),
    "host": (_RESOURCE, "discrete"),
    "table": (_RESOURCE, "discrete"),
    "calls": (_CEILING, "ceiling"),
    "size": (_CEILING, "ceiling"),
}


def is_registered(name: str) -> bool:
    return name in _REGISTRY


def registered_names() -> list[str]:
    return sorted(_REGISTRY)


def _canon_value(name: str, value: object) -> object:
    """Canonicalize (and validate) a raw parameter value for its registered
    order. String literals are expected for resource kinds, integers for
    ceilings. The path order stores a component tuple; discrete stores the
    string; ceiling stores the int."""
    kind, order = _REGISTRY[name]
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
        rendered = ",".join(f"{n}={_render_value(n, v)}" for n, v in self.params)
        return f"{self.token}({rendered})"


def _render_value(name: str, value: object) -> str:
    _kind, order = _REGISTRY[name]
    if order == "ceiling":
        return str(value)
    if order == "path":
        return '"/' + "/".join(value) + '"'
    return f'"{value}"'


# --------------------------------------------------------------- construction
#
# Two constructors, one validator (`_make_cap`): `make_cap` from structured
# pieces (the parser hands it python str/int values), `parse_cap` from a
# canonical string (a fold re-reads a stored `capabilities` entry). The single
# canonical point the representation mandate requires is `_make_cap`.


def _make_cap(token: str, raw_params: list[tuple[str, object]]) -> Cap:
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
    for name, value in raw_params:
        if name in seen:
            raise CapError(
                f"duplicate capability parameter `{name}`",
                hint="bind each parameter once")
        seen.add(name)
        if not is_registered(name):
            names = ", ".join(f"`{n}`" for n in registered_names())
            raise CapError(
                f"unknown capability parameter `{name}`",
                hint=f"the capability parameter registry is closed; known "
                     f"parameters are {names} (a typo narrows nothing and is "
                     "refused rather than silently ignored)")
        canon.append((name, _canon_value(name, value)))
    canon.sort(key=lambda kv: kv[0])
    return Cap(token, tuple(canon))


def make_cap(token: str, raw_params: list[tuple[str, object]] | None = None) -> Cap:
    """Build a validated `Cap` from a token and raw `(name, value)` pairs (the
    parser's entry point: values are already python `str`/`int` from literals)."""
    return _make_cap(token, list(raw_params or []))


def parse_cap(text: str) -> Cap:
    """Re-read a canonical capability spelling into a `Cap` (a fold's entry
    point over a stored `capabilities` string). A bare dotted token with no `(`
    is `Cap(token, ())`, so every pre-294 token round-trips unchanged."""
    open_i = text.find("(")
    if open_i < 0:
        return _make_cap(text, [])
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
    return _make_cap(token, raw)


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
    try:
        return int(raw)
    except ValueError as exc:
        raise CapError(f"malformed capability parameter value `{raw}`") from exc


# --------------------------------------------------------------- the order


def _leq_path(narrow: tuple, wide: tuple) -> bool:
    """`narrow <=_path wide` iff `wide`'s component list is a PREFIX of
    `narrow`'s, component-wise (never string-prefix): `/tmp/job-42 <= /tmp`
    holds, `/tmp/jobber <= /tmp/job` does NOT."""
    if len(wide) > len(narrow):
        return False
    return narrow[:len(wide)] == wide


def _param_leq(name: str, narrow: object, wide: object) -> bool:
    """`narrow <=_k wide` in parameter `k`'s value order (narrow is at-or-below
    wide, i.e. narrower authority)."""
    _kind, order = _REGISTRY[name]
    if order == "path":
        return _leq_path(narrow, wide)
    if order == "ceiling":
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
        if not _param_leq(k, bmap[k], av):
            return False
    return True


def covered_by_any(held: "list[Cap] | set[Cap]", c: Cap) -> bool:
    """Whether some held capability covers `c` (the per-child existential scan
    the attenuation fold runs)."""
    return any(covers(h, c) for h in held)


def covers_set(held: "list[Cap] | set[Cap]",
               reach: "list[Cap] | set[Cap]") -> list[Cap]:
    """The reach elements NOT covered by any held element - the attenuation
    check's `extra`. Empty means admitted; non-empty names the widenings."""
    return [c for c in reach if not covered_by_any(held, c)]
