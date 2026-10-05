"""Realm placeholders: a realm the author names and the operator binds
(issue #1728, docs/design/1728-realm-placeholder.md).

`isolate kv in realm(?tenant)` says the provision is isolated, and which
placeholder it belongs to, without naming a realm. `isolate db in realms(?a,
?b)` says the same of a route's legs: the author writes the shape of the
fan-out, the operator binds each leg's realm. The untrusted-author
profile forbids naming a realm (G9, `admit_profile.check_no_realm_placement`):
a realm is an authority address, and an author that picks its own picks which
of the operator's standing approvals cover its crossings. A placeholder picks
nothing. The operator binds it, in the admission profile
(`AdmissionProfile.realm_bindings`, set by `revl compile --bind-realm` and
`revl mcp serve --bind-realm`), and the binding is applied here, after the
profile's structural checks and BEFORE lowering. So everything downstream (the
G2 link check over `(key, realm)`, the manifest's `isolate` map, the approval
policy's realm scoping) sees an ordinary realm, the one the operator chose.

An unbound placeholder is refused by name. It has no realm, so there is
nothing to check provision disjointness against, and admitting it into the
shared realm would decide for the operator.
"""

from __future__ import annotations

import re

from .errors import RevlError

# the realm-label grammar `Parser.realm_label` enforces on a literal, applied
# to the operator's bound value too
_REALM_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")

# the opening the self-host gate spells byte for byte, and the census
# classifier's marker for it (tests/test_selfhost_lower.py `_classify`)
UNBOUND = "realm placeholder `?"


def _placeholders(node, out: list) -> None:
    """Every `isolate` reachable from a parsed AST fragment that names a
    placeholder: a singular `realm(?<name>)`, or a `realms(...)` route with a
    `?<name>` among its realms."""
    from .parser import IsolateStmt, RouteStmt  # noqa: PLC0415 - import cycle
    if isinstance(node, IsolateStmt):
        if node.placeholder is not None:
            out.append(node)
        return
    if isinstance(node, RouteStmt):
        if node.placeholders:
            out.append(node)
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            _placeholders(item, out)
        return
    fields = getattr(node, "__dict__", None)
    if fields:
        for value in fields.values():
            _placeholders(value, out)


def unbound_route_message(name: str, key: str) -> str:
    return (f"{UNBOUND}{name}` (on `isolate {key} in realms(...)`) is not bound: "
            f"the operator binds it to a realm at admission, and until then that "
            f"leg of the route has no realm to resolve `{key}` in")


def unbound_message(name: str, key: str) -> str:
    return (f"{UNBOUND}{name}` (on `isolate {key}`) is not bound: the operator "
            f"binds it to a realm at admission, and until then the provision "
            f"has no realm to check G2 against")


def bind(program, bindings: dict | None) -> None:
    """Bind every placeholder in `program` from `bindings` (name -> realm), in
    place, or refuse the first unbound one by name. A route is checked again
    after binding: two of its entries that land in one realm route the key to
    the same `(key, realm)` twice, refused G2 as a repeated literal would be."""
    from .parser import RouteStmt  # noqa: PLC0415 - import cycle
    found: list = []
    _placeholders(program.components, found)
    bindings = bindings or {}
    for stmt in found:
        if isinstance(stmt, RouteStmt):
            _bind_route(program, stmt, bindings)
            continue
        stmt.realm = _bound(program, stmt, stmt.placeholder, bindings)


def _bound(program, stmt, name: str, bindings: dict, *, route: bool = False) -> str:
    realm = bindings.get(name)
    if realm is None:
        raise RevlError(
            program.filename, stmt.line,
            (unbound_route_message if route else unbound_message)(name, stmt.key),
            hint=(f"bind it with `--bind-realm {name}=<realm>` "
                  f"on `revl compile` or `revl mcp serve` (the operator's "
                  f"side), or write `realm(\"<label>\")` if you are the "
                  f"operator (issue #1728)"),
            code="G2", category="admission")
    return realm


def _bind_route(program, stmt, bindings: dict) -> None:
    """Bind a `realms(...)` route's placeholders in list order, then refuse a
    realm the bound list names twice."""
    realms = list(stmt.realms)
    for index, name in stmt.placeholders:
        realms[index] = _bound(program, stmt, name, bindings, route=True)
    spelled = [f"?{name}" for _i, name in sorted(stmt.placeholders)]
    for index, realm in enumerate(realms):
        if realm in realms[:index]:
            first = realms.index(realm)
            both = [_spelling(stmt, first), _spelling(stmt, index)]
            raise RevlError(
                program.filename, stmt.line,
                f"`isolate {stmt.key} in realms(...)` routes to realm `{realm}` "
                f"twice: {both[0]} and {both[1]} are bound to it, and a route "
                f"names each realm once (G2 keeps one provider per (key, realm))",
                hint=("the operator binds each placeholder; bind "
                      + ", ".join(spelled) + " to distinct realms, or drop the "
                      "repeated entry (issue #1728)"),
                code="G2", category="admission")
    stmt.realms = realms


def _spelling(stmt, index: int) -> str:
    held = dict(stmt.placeholders)
    return f"`?{held[index]}`" if index in held else f'`"{stmt.realms[index]}"`'


def parse_bindings(pairs) -> tuple:
    """`["tenant=tenant_a", ...]` (the `--bind-realm` flag) as the profile's
    sorted `((name, realm), ...)`, or a ValueError naming the bad pair."""
    out: dict = {}
    for pair in pairs or ():
        name, sep, realm = str(pair).partition("=")
        name, realm = name.strip(), realm.strip()
        if not sep or not name or not realm:
            raise ValueError(f"--bind-realm takes NAME=REALM, got {pair!r}")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"invalid realm placeholder name {name!r}")
        if not _REALM_LABEL_RE.fullmatch(realm):
            raise ValueError(f"invalid realm label {realm!r} for `?{name}`")
        if name in out and out[name] != realm:
            raise ValueError(f"realm placeholder `?{name}` is bound twice")
        out[name] = realm
    return tuple(sorted(out.items()))
