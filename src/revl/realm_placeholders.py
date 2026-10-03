"""Realm placeholders: a realm the author names and the operator binds
(issue #1728, docs/design/1728-realm-placeholder.md).

`isolate kv in realm(?tenant)` says the provision is isolated, and which
placeholder it belongs to, without naming a realm. The untrusted-author
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
    """Every placeholder `isolate` reachable from a parsed AST fragment."""
    from .parser import IsolateStmt  # noqa: PLC0415 - import cycle
    if isinstance(node, IsolateStmt):
        if node.placeholder is not None:
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


def unbound_message(name: str, key: str) -> str:
    return (f"{UNBOUND}{name}` (on `isolate {key}`) is not bound: the operator "
            f"binds it to a realm at admission, and until then the provision "
            f"has no realm to check G2 against")


def bind(program, bindings: dict | None) -> None:
    """Bind every placeholder in `program` from `bindings` (name -> realm), in
    place, or refuse the first unbound one by name."""
    found: list = []
    _placeholders(program.components, found)
    bindings = bindings or {}
    for stmt in found:
        realm = bindings.get(stmt.placeholder)
        if realm is None:
            raise RevlError(
                program.filename, stmt.line,
                unbound_message(stmt.placeholder, stmt.key),
                hint=(f"bind it with `--bind-realm {stmt.placeholder}=<realm>` "
                      f"on `revl compile` or `revl mcp serve` (the operator's "
                      f"side), or write `realm(\"<label>\")` if you are the "
                      f"operator (issue #1728)"),
                code="G2", category="admission")
        stmt.realm = realm


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
