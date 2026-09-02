"""Composition rows: the row table (roadmap item 426, slice S1).

`docs/design/426-composition-layers.md` §1, §1.3, §6, §11 (S1), and item 424's
residual R2 (the `granted` clause, §1.2 of `docs/design/424-dsh-language-gaps.md`).

A composition document declares ROWS. A row is one component placed into one
composition, and it carries four separable things that the flat file list the
compiler takes today conflates into one:

- `label`   — IDENTITY. Declared, stable, scoped to the declaring document's
              origin (`<origin>::@<label>`). Survives an upstream rename AND an
              upstream surface addition, which is the property no derived id has.
- `claims`  — the CONTRACT, asserted in the document and checked against the
              component header. This is what G2 keys on, and it is the address a
              later patch resolves against (§2).
- `component` — PROVENANCE. Never identity (§1.5).
- `config`  — data, checked against the component's declared `config` types.

**This module is a PRE-LINKER artifact and is not on the trusted path.** It
resolves to the same `paths` list `compile_files` already takes; every guarantee
is still decided by `_link` (G2, G3) over the compiled result. A bug here can
only refuse something admissible, never admit something `_link` would refuse —
which is the soundness argument 426 §3.3 relies on.

**Header-only.** Resolution parses each row's source and reads the `component`
declaration's HEADER (`requires` / `provides` / `config`) and nothing else. No
body is lowered, so `revl composition --check` is cheap by construction (§1.3)
and stays cheap when the fold (S2) lands on top of it.

Not in this slice, and deliberately: layers and the four patch operations (S2),
incremental admission (S3), the confinement split and the per-root profile
(S4, which waits on 425 F1), the authority panel (S5), and distribution (S6).
`open`, `reach`, `place` and `variant` are S2/S5 surface and are not parsed yet,
so writing one is a parse error naming the clauses that do exist.
"""

from __future__ import annotations

import os

from .errors import RevlError
from .lower import _config_default_type
from .parser import CompositionDecl, IsolateStmt, Program, RowDecl, parse_file
from .typecheck import compatible

# The project's own origin. Reserved and unmintable by anyone else: a third
# party's rows are scoped to its `[trucs]` key, never to `.` (426 §1.2).
PROJECT_ORIGIN = "."

# Where truc vendors a fetched package (`src/revl/truc/reproduce.py:258`).
_VENDOR_DIR = "trucs"


def qualified(origin: str, label: str) -> str:
    """`<origin>::@<label>` — the fully qualified row label (426 §1.2)."""
    return f"{origin}::@{label}"


def origin_of(path: str, root: str | None = None) -> str:
    """The origin a document declared at `path` is scoped to.

    Either the project itself (`.`) or a truc key from `truc.toml`'s `[trucs]`
    table, which is also the vendor directory name and the lock row key. Two
    origins declaring the same bare label do not collide, because they are two
    different qualified labels; there is no registry of labels and no squatting
    policy to write (426 §1.2).
    """
    root = os.path.abspath(root or os.getcwd())
    rel = os.path.relpath(os.path.abspath(path), root)
    parts = rel.split(os.sep)
    if len(parts) >= 3 and parts[0] == _VENDOR_DIR:
        return parts[1]
    return PROJECT_ORIGIN


# ---------------------------------------------------------------- headers

class _Header:
    """The header half of one `component` declaration: what §1.3 needs and
    nothing else. Built from the parse tree, with no body lowered."""

    __slots__ = ("name", "source", "provides", "requires", "config", "realms",
                 "line")

    def __init__(self, decl, source: str):
        self.name = decl.name
        self.source = source
        self.line = decl.line
        self.provides = {key: (svc, line) for key, svc, line in decl.provides}
        self.requires = {key: (svc, line) for key, svc, line in decl.requires}
        self.config = {cfg.name: cfg for cfg in decl.config}
        # The claim set G2 keys on is `(key, realm)` (426 §1.1), and the realm
        # comes from `isolate <key> in realm(<name>)`, which is a BODY statement.
        # `_component_header_stub` (lower.py:5195) drops it because it is
        # recovering from a FAILED body lowering; resolution has the whole parse
        # tree and simply reads the statement, so realms cost nothing and the
        # sanctioned multi-provider shape (two rows claiming `kv` in two realms)
        # resolves instead of falsely colliding. Still no body is LOWERED.
        self.realms = {
            stmt.key: stmt.realm
            for stmt in decl.body
            if isinstance(stmt, IsolateStmt) and stmt.key in self.provides
        }

    def realm_of(self, key: str) -> str | None:
        """`None` is the shared realm."""
        return self.realms.get(key)


def _headers(path: str) -> list[_Header]:
    program = parse_file(path)
    return [_Header(decl, path) for decl in program.components]


# ---------------------------------------------------------------- resolution

def _claim_ir(claim: tuple[str, str | None]) -> dict:
    key, realm = claim
    return {"key": key} if realm is None else {"key": key, "realm": realm}


def claim_str(claim: tuple[str, str | None]) -> str:
    """A `(key, realm)` pair as the address a patch writes (426 §2.3):
    `key("db")` in the shared realm, `key("kv", realm: "tenant_a")` otherwise."""
    key, realm = claim
    return f'key("{key}")' if realm is None else f'key("{key}", realm: "{realm}")'


class Row:
    """One resolved row of the row table."""

    __slots__ = ("label", "origin", "source", "component", "claims",
                 "extra_claims", "requires", "config", "granted", "line")

    def __init__(self, label, origin, source, component, claims, extra_claims,
                 requires, config, granted, line):
        self.label = label
        self.origin = origin
        self.source = source
        self.component = component
        # `(key, realm)` pairs, realm `None` == the shared realm.
        self.claims = claims              # asserted, in declaration order
        self.extra_claims = extra_claims  # header claims the assertion omits
        self.requires = requires
        self.config = config              # field -> value
        self.granted = granted            # None == clause not written
        self.line = line

    @property
    def qualified(self) -> str:
        return qualified(self.origin, self.label)

    def to_ir(self) -> dict:
        """The row's IR shape. Ordered and free of absolute paths, so two
        machines resolving the same composition produce byte-identical rows
        (426 exit test 18)."""
        out = {
            "label": self.label,
            "origin": self.origin,
            "qualified": self.qualified,
            "source": self.source,
            "component": self.component,
            "claims": [_claim_ir(claim) for claim in self.claims],
            "requires": sorted(self.requires),
        }
        if self.extra_claims:
            out["extraClaims"] = [_claim_ir(claim) for claim in self.extra_claims]
        if self.config:
            out["config"] = dict(self.config)
        if self.granted is not None:
            out["granted"] = list(self.granted)
        return out


class RowTable:
    """The resolved base composition: rows, plus the file list `compile_files`
    takes. The composition is source of truth for semantics (426 decision 7)."""

    __slots__ = ("name", "origin", "source", "rows", "uses")

    def __init__(self, name, origin, source, rows, uses):
        self.name = name
        self.origin = origin
        self.source = source
        self.rows = rows
        self.uses = uses

    def to_ir(self) -> dict:
        return {
            "composition": self.name,
            "origin": self.origin,
            "source": self.source,
            "rows": [row.to_ir() for row in self.rows],
        }

    def wiring(self) -> dict:
        """The rename-invariant projection: label -> what the row claims and
        what it requires. `component` is provenance and is deliberately absent,
        which is why renaming a component upstream produces an EMPTY wiring diff
        (426 §1.4, exit test 2)."""
        return {
            row.qualified: {
                "claims": sorted(claim_str(c)
                                 for c in {*row.claims, *row.extra_claims}),
                "requires": sorted(row.requires),
            }
            for row in self.rows
        }

    def paths(self) -> list[str]:
        """The compile roots: every row's source plus the composition's own
        `use` paths, deduplicated in declaration order."""
        out: list[str] = []
        for path in [*self.uses, *(row.source for row in self.rows)]:
            if path not in out:
                out.append(path)
        return out


def _relative(path: str, root: str) -> str:
    """Provenance recorded relative to `root`, so an IR document stays
    machine-independent (the same rule `parse_file` follows)."""
    return os.path.relpath(os.path.abspath(path), os.path.abspath(root)) \
        .replace(os.sep, "/")


def _pick_component(row: RowDecl, headers: list[_Header], doc: str,
                    rel: str) -> _Header:
    if row.component is not None:
        for header in headers:
            if header.name == row.component:
                return header
        known = ", ".join(f"`{h.name}`" for h in headers) or "<none>"
        raise RevlError(
            doc, row.line,
            f"row `@{row.label}` names component `{row.component}`, which "
            f"`{rel}` does not declare",
            hint=f"components in `{rel}`: {known}")
    if not headers:
        raise RevlError(
            doc, row.line,
            f"row `@{row.label}` reads `{rel}`, which declares no component",
            hint="a row places ONE component into the composition; point "
                 "`from` at a file that declares one")
    if len(headers) > 1:
        known = ", ".join(f"`{h.name}`" for h in headers)
        raise RevlError(
            doc, row.line,
            f"row `@{row.label}` reads `{rel}`, which declares "
            f"{len(headers)} components",
            hint=f"disambiguate with a `component <Name>` clause ({known}); "
                 "the name is provenance, never the row's identity (426 §1.5)")
    return headers[0]


def _check_claims(row: RowDecl, header: _Header, doc: str,
                  rel: str) -> tuple[list, list]:
    """§1.3: the assertion is checked against the header. §1.4: a provision the
    header GAINED is reported and admitted (the label survives, which is the
    whole point of decision 1); one it LOST is a refusal naming the row, the
    lost key and the source that dropped it."""
    for key, line in row.claims:
        if key not in header.provides:
            served = ", ".join(f"`{k}`" for k in header.provides) or "nothing"
            raise RevlError(
                doc, line,
                f"row `@{row.label}` asserts `provides {key}`, but component "
                f"`{header.name}` in `{rel}` provides {served}",
                hint="a row's `provides` clause is an assertion the composition "
                     "cannot lie about; either the key was renamed or removed "
                     "upstream, or the assertion is wrong (426 §1.3, §1.4)")
    asserted = {key for key, _ in row.claims}
    claims = [(key, header.realm_of(key)) for key, _ in row.claims]
    extra = [(key, header.realm_of(key))
             for key in header.provides if key not in asserted]
    return claims, extra


def _check_config(row: RowDecl, header: _Header, doc: str,
                  rel: str) -> dict[str, object]:
    """A config value that does not fit the declared type does not admit, and
    the refusal names the field and the declared type (426 §3.2). Same shape as
    `_lower_lifecycle_config` (lower.py:4197), one tier up."""
    given: dict[str, object] = {}
    for name, value, line in row.config:
        cfg = header.config.get(name)
        if cfg is None:
            known = ", ".join(f"`{f}`" for f in header.config) or "<none>"
            raise RevlError(
                doc, line,
                f"`{name}` is not a config field of `{header.name}` "
                f"(row `@{row.label}`)",
                hint=f"config fields of `{header.name}` in `{rel}`: {known}")
        actual = _config_default_type(value)
        if actual is not None and not compatible(cfg.type, actual):
            raise RevlError(
                doc, line,
                f"config field `{name}` of row `@{row.label}` is declared "
                f"`{cfg.type}` but the composition supplies `{actual}`",
                hint="a config typo is a refusal here, not a runtime surprise "
                     "(426 §3.2)")
        given[name] = value
    missing = [name for name, cfg in header.config.items()
               if cfg.default is None and name not in given]
    if missing:
        listed = ", ".join(f"`{name}`" for name in missing)
        raise RevlError(
            doc, row.line,
            f"row `@{row.label}` is missing required config {listed} for "
            f"component `{header.name}`",
            hint=f"write `config {{ {missing[0]}: ... }}` on the row")
    return given


def _check_granted(row: RowDecl, header: _Header, doc: str) -> list[str] | None:
    """Item 424 R2, completing 426 §9.3 Part 2.

    `granted` names the services a CONFINED row may compose against — it is what
    `AdmissionProfile.untrusted_author(granted)` takes, and something has to
    produce it. Three rules, from 424 §1.2:

    - it defaults to EMPTY, never to "everything the row requires";
    - a row whose `requires` is not a subset of its `granted` is refused at
      resolution, naming the ungranted key;
    - it is writable only in the base composition and the site layer, never in a
      stack layer (no layer may raise its own authority). That third rule needs
      layers, so it lands with S2; the clause and the subset check are here,
      which is exactly the split 424 §1.3 slice A1 states.

    A row that writes NO clause is unconfined (first-party) and the subset check
    does not apply to it: wiring the profile is 426 S4 and waits on 425 F1. A
    row that writes `granted { }` is asking for the empty grant and gets it.
    """
    if row.granted is None:
        return None
    allowed = [key for key, _ in row.granted]
    for key, (_svc, line) in header.requires.items():
        if key not in allowed:
            listed = ", ".join(f"`{k}`" for k in allowed) or "<empty>"
            raise RevlError(
                doc, row.line,
                f"row `@{row.label}` requires `{key}`, which its `granted` "
                f"clause does not list",
                hint=f"granted on `@{row.label}`: {listed}. `granted` defaults "
                     "to EMPTY, so every key the row reaches is listed "
                     "explicitly (424 R2, 426 §9.3 Part 2)")
    return allowed


def resolve(decl: CompositionDecl, doc_path: str,
            root: str | None = None) -> RowTable:
    """Resolve one composition declaration into its row table.

    Header-only: every row's source is parsed and its component header read, and
    no body is lowered. Every failure is a REFUSAL naming the row (426 §2.4:
    an address that resolves to nothing is a refusal, never a no-op).
    """
    # Provenance and origin are recorded relative to the PROJECT root (the
    # invocation cwd by default), the same rule `parse_file` follows, so an IR
    # document stays machine-independent.
    root = os.path.abspath(root or os.getcwd())
    doc = decl.source or doc_path
    origin = origin_of(doc_path, root)
    base = os.path.dirname(os.path.abspath(doc_path))

    rows: list[Row] = []
    claimed: dict = {}
    for row in decl.rows:
        source = os.path.join(base, row.path)
        if not os.path.isfile(source):
            raise RevlError(
                doc, row.line,
                f"row `@{row.label}` reads `{row.path}`, which does not exist",
                hint=f"resolved against `{_relative(base, root)}`")
        rel = _relative(source, root)
        header = _pick_component(row, _headers(source), doc, rel)
        claims, extra = _check_claims(row, header, doc, rel)
        resolved = Row(
            label=row.label,
            origin=origin,
            source=rel,
            component=header.name,
            claims=claims,
            extra_claims=extra,
            requires=sorted(header.requires),
            config=_check_config(row, header, doc, rel),
            granted=_check_granted(row, header, doc),
            line=row.line,
        )
        # A pre-check of G2 (provision disjointness) at the row level, so the
        # refusal names ROWS rather than the two component names the operator
        # may not have written (426 §3.4). `_link` still runs G2 unchanged over
        # the compiled result — this only produces a better message, which is
        # what keeps the resolver off the trusted path (§3.3).
        for claim in [*resolved.claims, *resolved.extra_claims]:
            other = claimed.get(claim)
            if other is not None:
                raise RevlError(
                    doc, row.line,
                    f"{claim_str(claim)} is claimed by both row "
                    f"`{other.qualified}` (component `{other.component}`) and "
                    f"row `{resolved.qualified}` (component "
                    f"`{resolved.component}`) in composition {decl.name}",
                    hint="at most one row may claim a `(key, realm)` pair; this "
                         "is G2, provision disjointness, seen at the row level")
            claimed[claim] = resolved
        rows.append(resolved)

    uses = []
    for path, line in decl.uses:
        target = os.path.join(base, path)
        if not os.path.isfile(target):
            raise RevlError(
                doc, line,
                f"composition {decl.name} uses `{path}`, which does not exist",
                hint=f"resolved against `{_relative(base, root)}`")
        uses.append(_relative(target, root))
    return RowTable(decl.name, origin, _relative(doc_path, root), rows, uses)


def sole_composition(program: Program, path: str) -> CompositionDecl:
    """The one composition a document declares, or a refusal naming what it
    found instead."""
    if not program.compositions:
        raise RevlError(path, 1, f"`{os.path.basename(path)}` declares no composition",
                        hint="a composition document reads `composition Name { row ... }`")
    if len(program.compositions) > 1:
        names = ", ".join(c.name for c in program.compositions)
        raise RevlError(path, program.compositions[1].line,
                        f"`{os.path.basename(path)}` declares {len(program.compositions)} "
                        f"compositions ({names})",
                        hint="one composition per document: the document IS the "
                             "composition's origin scope (426 §1.2)")
    return program.compositions[0]


def resolve_file(path: str, root: str | None = None) -> RowTable:
    """Parse a composition document and resolve its row table."""
    return resolve(sole_composition(parse_file(path), path), path, root)


def compile_composition(path: str, root: str | None = None, **kwargs) -> dict:
    """Compile a composition document: resolve the row table, compile the rows
    it names, and carry the table into the IR and the manifest.

    This is the bootstrap shrink 426 §6 point 3 claims: a declared composition
    is compiled and READ (its rows are already in the IR) instead of compiled,
    emitted, exec'd, called and parsed back out of a JSON string.
    """
    from .compiler import compile_files  # noqa: PLC0415 (cycle: compiler -> parser -> here)

    root = os.path.abspath(root or os.getcwd())
    table = resolve_file(path, root)
    document = compile_files([os.path.join(root, p) for p in table.paths()], **kwargs)
    document["rows"] = table.to_ir()
    if isinstance(document.get("manifest"), dict):
        document["manifest"]["rows"] = document["rows"]
    return document
