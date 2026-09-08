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

from .admit_profile import AdmissionProfile
from .errors import RevlError
from .lower import _config_default_type
from .parser import (Address, CompositionDecl, IsolateStmt, LayerDecl,
                     PlaceDecl, Program, RowDecl, parse_file)
from .synthesize import (
    HOST_SHIMS, OBSERVER_METHOD, cap_token, check_address, check_remotable,
    synthesize_provider)
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
                 "extra_claims", "requires", "config", "granted", "line",
                 "provenance", "remote", "seam", "open", "reach", "place",
                 "host")

    def __init__(self, label, origin, source, component, claims, extra_claims,
                 requires, config, granted, line, provenance=None, remote=None,
                 seam=None, open=None, reach=None, place=None, host=None):
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
        # 426 §3.3 step 5: the ordered record of every (level, layer, op) that
        # touched this row. A row nobody patched carries one entry, the base.
        self.provenance = list(provenance or [])
        # item 424 C2: the admission facts of a `remote` row — the peer, the
        # reach, the failure mode. `None` for an ordinary row. This is where
        # "remoteness is an ADMISSION fact, never a wiring fact" is literally
        # true: it sits beside the wiring, and `wiring()` does not read it.
        self.remote = remote
        # item 424 B2: the interposition facts of a `seam` row — the edge it
        # wraps, the kind, the observer, the declared `through` reach. `None`
        # for an ordinary row. Like `remote`, an ADMISSION fact beside the
        # wiring, not read by `wiring()`.
        self.seam = seam
        # 426 S5: the config fields a stack layer may `configure` (§8.6, `open`)
        # and the composition-level host bound on a host-bearing field
        # (§8.3/§8.4, `reach`). `open` is a set of field names; `reach` maps a
        # field name to its declared host bound. Both `None` when the clause
        # was not written, so a row that uses neither is byte-identical.
        self.open = open              # set[str] | None
        self.reach = reach            # dict[str, str] | None
        # item 424 R3: the placement fact — `{"process": str, "backend": str?}`
        # — for a row a `place` statement targets. `None` for a row nobody
        # placed. Like `remote`/`seam`, an ADMISSION fact beside the wiring, not
        # read by `wiring()`, so an unplaced row is byte-identical.
        self.place = place            # dict | None
        # item 457 S3: the admission facts of a `host` row — the hosted service
        # and the shim binding module it is reached through. `None` for an
        # ordinary row. Like `remote`/`seam`, an ADMISSION fact beside the
        # wiring, not read by `wiring()`: bringing a host provider back in
        # process is a one-line composition edit and not a source edit across
        # every consumer.
        self.host = host              # dict | None

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
        if self.open is not None:
            out["open"] = sorted(self.open)
        if self.reach:
            out["reach"] = {field: self.reach[field] for field in sorted(self.reach)}
        if any(level for level, _, _ in self.provenance):
            # Only recorded once a layer actually touched the row, so a
            # composition with no layers produces the S1 document byte for byte.
            out["provenance"] = [{"level": level, "layer": layer, "op": op}
                                 for level, layer, op in self.provenance]
        if self.remote is not None:
            out["remote"] = dict(self.remote)
        if self.seam is not None:
            out["seam"] = dict(self.seam)
        if self.place is not None:
            out["place"] = dict(self.place)
        if self.host is not None:
            out["host"] = dict(self.host)
        return out


class RowTable:
    """The resolved base composition: rows, plus the file list `compile_files`
    takes. The composition is source of truth for semantics (426 decision 7)."""

    __slots__ = ("name", "origin", "source", "rows", "uses", "sources")

    def __init__(self, name, origin, source, rows, uses, sources=None):
        self.name = name
        self.origin = origin
        self.source = source
        self.rows = rows
        self.uses = uses
        # item 424 C2: `<relative path> -> revl source` for every row whose
        # provider was SYNTHESIZED. Nothing is written to disk; `compile_files`
        # already takes an in-memory `sources` map, so a synthesized provider is
        # compiled exactly like a file one and `_link` checks it identically.
        self.sources = sources or {}

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


def _check_open_reach(row: RowDecl, header: _Header, doc: str,
                      rel: str) -> tuple[set[str] | None, dict[str, str] | None]:
    """`open { ... }` and `reach { ... }` name config FIELDS, so both are
    checked against the component's declared config exactly as a config value
    is (426 §8.6, §8.3). A clause naming a field the component does not declare
    is a refusal, never a silent no-op — same discipline as `_check_config`.
    """
    open_fields: set[str] | None = None
    if row.open is not None:
        open_fields = set()
        for name, line in row.open:
            if name not in header.config:
                known = ", ".join(f"`{f}`" for f in header.config) or "<none>"
                raise RevlError(
                    doc, line,
                    f"`open` on row `@{row.label}` names `{name}`, which is not "
                    f"a config field of `{header.name}`",
                    hint=f"config fields of `{header.name}` in `{rel}`: {known} "
                         "(426 §8.6)")
            open_fields.add(name)
    reach: dict[str, str] | None = None
    if row.reach is not None:
        reach = {}
        for name, bound, line in row.reach:
            if name not in header.config:
                known = ", ".join(f"`{f}`" for f in header.config) or "<none>"
                raise RevlError(
                    doc, line,
                    f"`reach` on row `@{row.label}` bounds `{name}`, which is "
                    f"not a config field of `{header.name}`",
                    hint=f"config fields of `{header.name}` in `{rel}`: {known}. "
                         "`reach` bounds a host-bearing config field (426 §8.3)")
            reach[name] = bound
    return open_fields, reach


def _host_of(value: object) -> str | None:
    """The host authority a config VALUE reaches, or `None` if it names none.

    A `host("h:port")` bound and a config value like
    `"postgres://primary:5432/app"` are compared on this normalized authority
    (host, and port when present). The extraction is deliberately syntactic and
    conservative: anything it cannot parse to a host returns `None` and is
    treated by the caller as UNCLASSIFIABLE, which is the fail-closed direction
    §8.5 conjunct 5 requires — never silence.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    # drop any path/query and any userinfo
    text = text.split("/", 1)[0].split("?", 1)[0]
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    text = text.strip()
    return text or None


def _check_reach_bounds(label: str, config: dict, reach: dict[str, str] | None,
                        doc: str, line: int, where: str) -> None:
    """A config value on a field with a declared `reach` bound is checked
    against that bound (426 §8.3). A value whose host is outside the bound is a
    REFUSAL, never a silent redirect — "Widen the bound in your site layer, or
    refuse the layer." `where` names the layer/site the value came from, for
    the diagnostic.
    """
    if not reach:
        return
    for field, bound in reach.items():
        if field not in config:
            continue
        got = _host_of(config[field])
        want = _host_of(bound) or bound
        if got is not None and got != want:
            raise RevlError(
                doc, line,
                f"row `@{label}` configures `{field}` ({where}) to a value "
                f"outside the reach declared for it in the composition",
                hint=f"declared:  host(\"{want}\")\n"
                     f"requested: host(\"{got}\")\n"
                     "Widen the bound in your site layer, or refuse the layer "
                     "(426 §8.3)")


def _merge_replacement_reach(base_reach: dict[str, str] | None,
                             repl_reach: dict[str, str] | None,
                             layer: LayerDecl, line: int,
                             target: str) -> dict[str, str] | None:
    """The `reach` bound a stack-layer `replace` presents, clamped to the base.

    426 §8.3/§8.6: the composition-level `reach` bound belongs to the BASE
    composition. A `replace` inherits it and may NARROW it — bound a field the
    base left unbounded — but never WIDEN it. A base bound on a field is
    authoritative: a replacement that re-declares that field to a DIFFERENT
    authority, or drops it while declaring its own `reach` on other fields,
    would raise its own reach past what the base restricted. That replacement
    is REFUSED and the base keeps the bound — a replacement narrows or is
    refused, it never widens (the same admission discipline the rest of the
    fold keeps). This also subsumes the plain carry-forward: a replacement that
    declares no `reach` of its own inherits the base's unchanged.
    """
    base_reach = base_reach or {}
    repl_reach = repl_reach or {}
    merged = dict(repl_reach)
    for field, base_bound in base_reach.items():
        repl_bound = repl_reach.get(field)
        if repl_bound is not None:
            base_host = _host_of(base_bound) or base_bound
            repl_host = _host_of(repl_bound) or repl_bound
            if repl_host != base_host:
                raise _layer_error(
                    layer, line,
                    f"layer `{layer.name}` replaces row `{target}` with a "
                    f"component whose `reach` on `{field}` widens the bound the "
                    f"base composition declared",
                    hint=f"inherited: host(\"{base_host}\")\n"
                         f"requested: host(\"{repl_host}\")\n"
                         "a `replace` inherits the base's reach and may narrow "
                         "it, never widen or redirect it; drop the wider bound "
                         "or refuse the layer (426 §8.3/§8.6)")
        # The base bound is authoritative and carried forward as-is.
        merged[field] = base_bound
    return merged or None


def _merge_replacement_open(base_open: set[str] | None,
                            repl_open: set[str] | None,
                            layer: LayerDecl, line: int,
                            target: str) -> set[str] | None:
    """The `open` set a stack-layer `replace` presents, clamped to the base.

    426 §8.6/§8.3: the composition-level `open` set belongs to the BASE
    composition. `open` is the allowlist of config fields a non-owning stack
    layer may `configure`, so it GRANTS authority — the mirror image of a
    `reach` bound, which restricts it. A `replace` inherits the base's open set
    and may NARROW it — open fewer fields, keep more closed — but never WIDEN
    it: a replacement that re-declares `open` to include a field the base kept
    closed would grant configure authority the base withheld, and a later stack
    layer could then patch that field. That replacement is REFUSED. This also
    subsumes the plain carry-forward: a replacement that declares no `open` of
    its own inherits the base's unchanged.
    """
    if repl_open is None:
        # No `open` of its own: inherit the base's set unchanged.
        return base_open
    base_open = base_open or set()
    widened = repl_open - base_open
    if widened:
        opened = ", ".join(f"`{f}`" for f in sorted(widened))
        inherited = ", ".join(f"`{f}`" for f in sorted(base_open)) or "<none>"
        requested = ", ".join(f"`{f}`" for f in sorted(repl_open)) or "<none>"
        raise _layer_error(
            layer, line,
            f"layer `{layer.name}` replaces row `{target}` with a component "
            f"whose `open` set opens {opened}, which the base composition kept "
            f"closed",
            hint=f"inherited: open {{ {inherited} }}\n"
                 f"requested: open {{ {requested} }}\n"
                 "a `replace` inherits the base's open set and may narrow it "
                 "(open fewer, keep closed), never widen it; a later stack layer "
                 "could configure a field the base withheld. Drop the wider "
                 "`open` or refuse the layer (426 §8.6/§8.3)")
    # A subset (or the same set) narrows or preserves the base grant: kept.
    return repl_open


def _vendor_truc_of(abspath: str, root: str) -> str | None:
    """The truc a source path is VENDORED under (`trucs/<truc>/...`), or `None`
    if the path is the project's own (426 §7). Distribution facts key off where
    the bytes physically live, not off which document named them."""
    rel = os.path.relpath(abspath, root)
    parts = rel.split(os.sep)
    if len(parts) >= 2 and parts[0] == _VENDOR_DIR and parts[1] not in ("", ".."):
        return parts[1]
    return None


def _load_lock(root: str) -> dict | None:
    """The project's `truc.lock` as `name -> sourceHash`, or `None` if there is
    no lock file. A composition with no vendored truc needs no lock (§7); a
    composition that references one and has no lock is refused by the caller."""
    import json  # noqa: PLC0415
    path = os.path.join(root, "truc.lock")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.loads(fh.read())
    except (ValueError, OSError):
        return {}
    return {r.get("name"): r.get("sourceHash") for r in data.get("trucs", [])
            if isinstance(r, dict)}


def _require_pin(truc: str, root: str, doc: str, line: int) -> None:
    """426 exit test 17 / 428 F3, at composition resolution: a vendored truc a
    composition references must carry a NON-BLANK lock pin, or resolution
    refuses. This is the composition-side of the F3 gate `truc assemble`
    enforces for the vendor step — "the lock does not define a row and cannot
    add one; what the lock does is refuse" (§7). A missing lock, a missing row
    or a blank `sourceHash` are all "no pin"; a blank pin is no pin.
    """
    lock = _load_lock(root)
    pin = None if lock is None else lock.get(truc)
    if not pin:
        detail = ("there is no `truc.lock`" if lock is None
                  else "its lock row has a blank sourceHash"
                  if truc in (lock or {}) else
                  "no lock row names it")
        raise RevlError(
            doc, line,
            f"unpinned truc `{truc}`: a vendored truc the composition "
            f"references must carry a lock pin, but {detail}",
            hint="run `truc assemble` to pin the vendored trucs, and commit "
                 "`truc.lock` (426 §7 / exit test 17, the 428 F3 gate). A blank "
                 "`sourceHash` is no pin.")


def _resolve_row(row: RowDecl, origin: str, doc: str, base: str,
                 root: str) -> tuple[Row, _Header]:
    """One `row` declaration into one resolved `Row`, header-only.

    Pure with respect to the rest of the table: nothing here looks at another
    row, which is what lets the fold (S2) introduce and withdraw rows in any
    order and still reach one answer (426 §3.3). The cross-row check is
    `_check_disjoint`, and it runs ONCE, over the folded result.
    """
    source = os.path.abspath(os.path.join(base, row.path))
    if not os.path.isfile(source):
        raise RevlError(
            doc, row.line,
            f"row `@{row.label}` reads `{row.path}`, which does not exist",
            hint=f"resolved against `{_relative(base, root)}`")

    # 426 §4.1, exit test 15's second half — the VENDORED-DIR JAIL. A row a
    # vendored stack layer introduces must resolve inside that layer's OWN truc
    # directory (`trucs/<origin>/`). A layer whose `from` climbs out of its
    # vendored directory is refused, so it cannot read another truc's bytes or
    # the project's own sources and launder them as its own row. Project-local
    # documents (origin `.`) are the operator's and are not jailed.
    if origin != PROJECT_ORIGIN:
        jail = os.path.abspath(os.path.join(root, _VENDOR_DIR, origin))
        if os.path.commonpath([source, jail]) != jail:
            raise RevlError(
                doc, row.line,
                f"row `@{row.label}` of vendored layer `{origin}` reads "
                f"`{row.path}`, which resolves outside `{_VENDOR_DIR}/{origin}/`",
                hint="a stack layer's `from` path must resolve inside its own "
                     "truc's vendored directory (426 §4.1); it cannot reach "
                     "another truc's or the project's own sources")

    # 426 §7, exit test 17 — the MANDATORY PIN. When a document OUTSIDE a truc
    # (the consuming project, or another truc) references bytes vendored inside
    # `trucs/<T>/`, that truc must carry a non-blank lock pin. A truc's own
    # internal reference (its composition reading its sibling component) needs
    # no project lock — the pin is the CONSUMING project's integrity proof (§7).
    vendored = _vendor_truc_of(source, root)
    if vendored is not None and _vendor_truc_of(base, root) != vendored:
        _require_pin(vendored, root, doc, row.line)

    rel = _relative(source, root)
    header = _pick_component(row, _headers(source), doc, rel)
    claims, extra = _check_claims(row, header, doc, rel)
    config = _check_config(row, header, doc, rel)
    open_fields, reach = _check_open_reach(row, header, doc, rel)
    _check_reach_bounds(row.label, config, reach, doc, row.line, "declared")
    return Row(
        label=row.label,
        origin=origin,
        source=rel,
        component=header.name,
        claims=claims,
        extra_claims=extra,
        requires=sorted(header.requires),
        config=config,
        granted=_check_granted(row, header, doc),
        line=row.line,
        open=open_fields,
        reach=reach,
    ), header


def _check_disjoint(rows: list[Row], name: str, doc: str) -> None:
    """The G2 pre-check (provision disjointness) at the row level, so the
    refusal names ROWS rather than the two component names the operator may not
    have written (426 §3.4).

    `_link` still runs G2 unchanged over the compiled result — this only
    produces a better message, which is what keeps the resolver off the trusted
    path (§3.3). Under the fold it runs exactly once, over the FINAL table:
    running it per operation is the determinism trap §3.3 exists to kill, where
    `remove`-then-`add` succeeds and `add`-then-`remove` refuses.
    """
    claimed: dict = {}
    for resolved in rows:
        for claim in [*resolved.claims, *resolved.extra_claims]:
            other = claimed.get(claim)
            if other is not None:
                raise RevlError(
                    doc, resolved.line,
                    f"{claim_str(claim)} is claimed by both row "
                    f"`{other.qualified}` (component `{other.component}`) and "
                    f"row `{resolved.qualified}` (component "
                    f"`{resolved.component}`) in composition {name}",
                    hint="at most one row may claim a `(key, realm)` pair; this "
                         "is G2, provision disjointness, seen at the row level. "
                         "Two peers of one service are two rows in two REALMS "
                         "(424 D-424c.4)")
            claimed[claim] = resolved


def _resolve_uses(decl: CompositionDecl, doc: str, base: str,
                  root: str) -> list[str]:
    uses = []
    for path, line in decl.uses:
        target = os.path.join(base, path)
        if not os.path.isfile(target):
            raise RevlError(
                doc, line,
                f"composition {decl.name} uses `{path}`, which does not exist",
                hint=f"resolved against `{_relative(base, root)}`")
        uses.append(_relative(target, root))
    return uses


def _resolve_remotes(decl: CompositionDecl, doc: str, origin: str, root: str,
                     uses: list[str], rows: list["Row"]) -> dict:
    """item 424 C2: append the rows whose provider is SYNTHESIZED, and return
    the in-memory `<relative path> -> revl source` map they compiled from.

    Resolved after the file rows because the service declaration a remote row
    remotes is looked up in what those rows and the `use` list already name — a
    remote row introduces no new source of its own to search.
    """
    sources: dict[str, str] = {}
    if not decl.remotes:
        return sources
    catalog = _service_catalog([*uses, *(r.source for r in rows)], root)
    for remote in decl.remotes:
        rows.append(_resolve_remote(remote, catalog, decl, doc, origin, root,
                                    sources))
    return sources


def _service_catalog(paths: list[str], root: str) -> dict:
    """`service name -> (decl, relative path)` over the sources a composition
    already names. Header-only, like everything else here: the service
    declaration is read out of the parse tree and no body is lowered."""
    catalog: dict = {}
    for rel in dict.fromkeys(paths):
        program = parse_file(os.path.join(root, rel))
        for svc in program.services:
            catalog.setdefault(svc.name, (svc, rel))
    return catalog


def _synth_path(origin: str, label: str) -> str:
    """The provenance path a synthesized provider is recorded under.

    No file is written; this is the key the in-memory `sources` map is read by
    and the `source` the IR records, and it is derived from the origin and the
    label alone so two machines resolving the same composition produce
    byte-identical rows (426 exit test 18 holds for a remote row too).
    """
    scope = "_project" if origin == PROJECT_ORIGIN else origin
    return f".revl/synthesized/{scope}/{label}.remote.rvl"


def _resolve_remote(remote, catalog: dict, decl: CompositionDecl, doc: str,
                    origin: str, root: str, sources: dict) -> "Row":
    """Resolve one `remote` row: check the address, check that the service is
    remotable, synthesize its provider, and return an ordinary `Row`.

    It returns an ORDINARY row on purpose. Everything downstream — the wiring
    projection, the G2 pre-check, `paths()`, the load order — treats it exactly
    like a file row, which is D-424c.1 holding at the level of this module's own
    data structures and not just in the prose.
    """
    if remote.service not in catalog:
        known = ", ".join(f"`{n}`" for n in sorted(catalog)) or "<none>"
        raise RevlError(
            doc, remote.line,
            f"remote row `@{remote.label}` remotes service "
            f"`{remote.service}`, which composition {decl.name} does not "
            "declare",
            hint=f"services in scope: {known}. A remote row has no component "
                 "header, so the service declaration IS its contract; add a "
                 "`use` for the file declaring it")
    service, service_source = catalog[remote.service]

    check_address(remote.host, doc=doc, line=remote.host_line or remote.line,
                  label=remote.label)
    check_remotable(service, doc=doc, line=remote.line, label=remote.label,
                    on_failure=remote.on_failure,
                    on_failure_line=remote.on_failure_line)

    capability = cap_token(remote.host)
    component, text = synthesize_provider(service, "remote", {
        "label": remote.label, "key": remote.key, "host": remote.host,
        "realm": remote.realm, "capability": capability,
        "on_failure": remote.on_failure, "transport": remote.transport,
        "redirect": remote.redirect,
        "doc": doc, "line": remote.line,
    })
    rel = _synth_path(origin, remote.label)
    sources[rel] = text
    return Row(
        label=remote.label,
        origin=origin,
        source=rel,
        component=component,
        claims=[(remote.key, remote.realm)],
        extra_claims=[],
        # A synthesized remote provider requires nothing: it holds one extern
        # per method and no coeffect. That is not an omission — a row that
        # required something would have to name a provider for it, and there is
        # no source in which to write one.
        requires=[],
        config={},
        granted=None,
        line=remote.line,
        remote={
            "peer": remote.host,
            "service": remote.service,
            "serviceSource": service_source,
            "capability": capability,
            "onFailure": remote.on_failure,
            # The row's redirect policy, on the row table so the audit surface
            # and the manifest carry it: "where this crossing may reach" is not
            # readable from the peer address alone if the transport may be
            # redirected off it.
            "redirect": remote.redirect,
            "inverse": None,
            **({"realm": remote.realm} if remote.realm else {}),
            **({"transport": remote.transport} if remote.transport else {}),
        },
    )


def _resolve_hosts(decl: CompositionDecl, doc: str, origin: str, root: str,
                   uses: list[str], rows: list["Row"]) -> dict:
    """item 457 S3: append the rows whose provider is the HOST runtime's own
    ambient service, and return the in-memory `<relative path> -> revl source`
    map they compiled from — the exact shape `_resolve_remotes` returns, because
    a `host` row is the sibling of a `remote` row (design note 530 Decision A).

    Resolved after the file rows for the same reason `_resolve_remotes` is: the
    service declaration a host row hosts is looked up in what the file rows and
    the `use` list already name; a host row introduces no source of its own.
    """
    sources: dict[str, str] = {}
    if not decl.hosts:
        return sources
    catalog = _service_catalog([*uses, *(r.source for r in rows)], root)
    for host in decl.hosts:
        rows.append(_resolve_host(host, catalog, decl, doc, origin, root,
                                  sources))
    return sources


def _synth_host_path(origin: str, label: str) -> str:
    """The provenance path a synthesized HOST provider is recorded under, the
    host counterpart of `_synth_path`. No file is written; the key is derived
    from the origin and the label alone so two machines resolving the same
    composition produce byte-identical rows (426 exit test 18)."""
    scope = "_project" if origin == PROJECT_ORIGIN else origin
    return f".revl/synthesized/{scope}/{label}.host.rvl"


def _resolve_host(host, catalog: dict, decl: CompositionDecl, doc: str,
                  origin: str, root: str, sources: dict) -> "Row":
    """Resolve one `host` row: look the service up, synthesize its host-shim
    provider, and return an ORDINARY row — everything downstream treats it like
    a file row, which is design note 530 Decision A holding at the level of this
    module's data structures and not just the prose (as `_resolve_remote` does
    for a remote row)."""
    if host.service not in catalog:
        known = ", ".join(f"`{n}`" for n in sorted(catalog)) or "<none>"
        raise RevlError(
            doc, host.line,
            f"host row `@{host.label}` hosts service `{host.service}`, which "
            f"composition {decl.name} does not declare",
            hint=f"services in scope: {known}. A host row has no component "
                 "header, so the service declaration IS its contract; add a "
                 "`use` for the file declaring it")
    service, service_source = catalog[host.service]

    shim = HOST_SHIMS.get(host.service)
    component, text = synthesize_provider(service, "host", {
        "label": host.label, "key": host.key, "realm": host.realm,
        "doc": doc, "line": host.line,
    })
    rel = _synth_host_path(origin, host.label)
    sources[rel] = text
    return Row(
        label=host.label,
        origin=origin,
        source=rel,
        component=component,
        claims=[(host.key, host.realm)],
        extra_claims=[],
        # Like a synthesized remote provider, a host provider requires nothing:
        # it holds one extern per method and no coeffect. A row that required
        # something would have to name a provider for it, and there is no source
        # in which to write one.
        requires=[],
        config={},
        granted=None,
        line=host.line,
        host={
            "service": host.service,
            "serviceSource": service_source,
            # The shipped shim this host provider is reached through, so the
            # audit surface and the manifest carry "where this ambient reach
            # goes": the reviewed `@ts ref` module and the Cordis package it
            # wraps, not an opaque `globalThis`.
            "shim": shim["module"],
            "package": shim["package"],
            **({"realm": host.realm} if host.realm else {}),
        },
    )


def _seam_inner_key(key: str) -> str:
    """The DISTINCT inner key a seam's forwarder requires the wrapped provider
    under (item 424 B2).

    Same-key interposition is not expressible today — `isolate` binds ONE realm
    per key (`lower.py`), so a component cannot require `db` from an inner realm
    and provide `db` in the parent realm, and the only same-key shape the
    language admits routes through `realms(...)`, which is `run.py:747`'s hole
    (compiles, admits, never runs). So the forwarder requires the wrapped
    provider under THIS key and provides the outer key. The wrapped provider is
    placed under this key in its own source — §2.2's measured cost, unchanged —
    which is why a seam is not yet source-transparent for the wrapped provider.
    """
    return f"{key}__seamed"


def _synth_seam_path(origin: str, label: str) -> str:
    """The provenance path a synthesized SEAM forwarder is recorded under,
    the seam counterpart of `_synth_path`. No file is written."""
    scope = "_project" if origin == PROJECT_ORIGIN else origin
    return f".revl/synthesized/{scope}/{label}.seam.rvl"


def _edge_provider(rows: list["Row"], root: str, key: str,
                   realm: str | None) -> tuple[str | None, "Row | None"]:
    """The file row providing `(key, realm)` and the service it provides, or
    `(None, None)`. Header-only, and synthesized rows (no file on disk) are
    skipped: a seam wraps a placed provider, not another synthesized one."""
    for row in rows:
        source = os.path.join(root, row.source)
        if not os.path.isfile(source):
            continue
        for header in _headers(source):
            if header.name != row.component:
                continue
            if key in header.provides and header.realm_of(key) == realm:
                return header.provides[key][0], row
    return None, None


def _observer_provision(rows: list["Row"], root: str,
                        label: str) -> tuple[str | None, str | None]:
    """The `(key, service)` the observer row named by `@label` provides. The
    observer row must provide EXACTLY ONE key — a seam names one observer and
    the forwarder calls one method on it."""
    for row in rows:
        if row.label != label:
            continue
        source = os.path.join(root, row.source)
        if not os.path.isfile(source):
            return None, None
        for header in _headers(source):
            if header.name != row.component:
                continue
            if len(header.provides) != 1:
                return None, None
            (key, (svc, _line)), = header.provides.items()
            return key, svc
    return None, None


def _seam_reach_ceiling(service) -> frozenset | None:
    """The capabilities the wrapped SERVICE already declares across its methods —
    the emission bound a seam's `through` reach must stay within under §2.4's
    fallback.

    `None` means UNBOUNDED: some method is a bare `emission` ("any capability",
    `capabilities is None`), so nothing in `through` can exceed it and the subset
    check is vacuous. A plain `fn` method contributes nothing (its forwarder
    emits nothing there to bound, and G4 refuses a seam over a pure method
    anyway), and a method declaring `emission[caps]` contributes those caps.
    """
    ceiling: set[str] = set()
    for method in service.methods.values():
        if not method.emission:
            continue
        if method.capabilities is None:
            return None  # bare `emission` == any capability, no ceiling
        ceiling.update(method.capabilities)
    return frozenset(ceiling)


def _check_seam_through(service, seam, doc: str) -> None:
    """§2.4's FALLBACK for D-424b.5 — the no-rule-change half, and the half that
    IS buildable today. A seam's declared `through` reach must be a SUBSET of the
    wrapped service's own declared emission bound.

    G4 still checks the synthesized forwarder against the service declaration
    exactly as it checks any provider (no rule change, so a seam still compiles
    only where the service's bound already covers the crossing, §2.4). This adds
    only that the operator's declared `through` may not claim a reach the service
    never granted, so `through` names an ENFORCED reach rather than a decorative
    one — 424(b)'s exit is an interception surface whose reach is DECLARED and
    admitted, and this is the admission.

    The WIDENING variant — checking the forwarder against `through` INSTEAD of
    the service, letting the composition MINT a bound the service did not declare
    — is the rule change §2.4 reserves for the architect, weakens transitive
    purity of a plain `fn` across the seamed edge, and needs 426 S5's `seam:`
    token to keep the panel from printing `clean` across the insertion. It is
    not made here.
    """
    if not seam.through:
        return
    ceiling = _seam_reach_ceiling(service)
    if ceiling is None:
        return  # a bare `emission` method grants any capability
    for cap in seam.through:
        if cap not in ceiling:
            declared = ", ".join(f"`{c}`" for c in sorted(ceiling)) or "<none>"
            raise RevlError(
                doc, seam.line,
                f"seam row `@{seam.label}` declares `through {cap}`, a reach the "
                f"wrapped service `{service.name}` does not grant (it declares "
                f"{declared})",
                hint="§2.4's fallback bounds a seam's `through` reach by the "
                     "wrapped service's own `emission[...]` declaration: "
                     "`through` may name only a capability the service already "
                     "declares. Reaching a capability the service did NOT declare "
                     "is the D-424b.5 WIDENING, reserved for the architect and "
                     "dependent on 426 S5's `seam:` token (424 D-424b.5, §2.4)")


def _resolve_seams(decl: CompositionDecl, doc: str, origin: str, root: str,
                   uses: list[str], rows: list["Row"]) -> dict:
    """item 424 B2: append the rows whose provider is a SYNTHESIZED FORWARDER,
    and return the in-memory `<relative path> -> revl source` map they compiled
    from.

    Resolved after the file rows AND the remote rows, because a seam wraps an
    edge another row provides: the wrapped provider and the observer are looked
    up in what is already placed. The catalog is built from FILE sources only —
    a synthesized provider is not on disk to re-parse.
    """
    sources: dict[str, str] = {}
    if not decl.seams:
        return sources
    file_paths = [*uses, *(r.source for r in rows
                           if os.path.isfile(os.path.join(root, r.source)))]
    catalog = _service_catalog(file_paths, root)
    for seam in decl.seams:
        rows.append(_resolve_seam(seam, catalog, rows, decl, doc, origin, root,
                                  sources))
    return sources


def _resolve_seam(seam, catalog: dict, rows: list["Row"], decl: CompositionDecl,
                  doc: str, origin: str, root: str, sources: dict) -> "Row":
    """Resolve one `seam` row: find the wrapped provider and the observer,
    synthesize the forwarder, and return an ordinary `Row`.

    Like `_resolve_remote`, it returns an ORDINARY row: everything downstream
    treats the seam exactly like a file row, which is D-424b.2 holding at the
    level of this module's data structures — the composition places the seam and
    the seam never registers itself.
    """
    inner_key = _seam_inner_key(seam.key)
    wrapped_service, _row = _edge_provider(rows, root, inner_key, seam.realm)
    if wrapped_service is None:
        raise RevlError(
            doc, seam.line,
            f"seam row `@{seam.label}` wraps `{claim_str((seam.key, seam.realm))}`,"
            f" but no row provides the inner key `{inner_key}`",
            hint=f"a seam is a DISTINCT-KEY forwarder: it provides `{seam.key}` "
                 f"and requires the wrapped provider under `{inner_key}`, so the "
                 f"wrapped provider is placed under `{inner_key}` in its own "
                 "source (§2.2's cost; same-key interposition is run.py:747's "
                 "hole and is not expressible). Add the row that provides "
                 f"`{inner_key}`")

    obs_key, obs_service = _observer_provision(rows, root, seam.observer)
    if obs_key is None:
        raise RevlError(
            doc, seam.line,
            f"seam row `@{seam.label}` observes `with @{seam.observer}`, which is "
            "not a row that provides exactly one service",
            hint="`with @observer` names a row in this composition that provides "
                 "the observer service; that row must provide exactly one key, "
                 "the observer surface the forwarder calls (424 D-424b.3)")

    method = OBSERVER_METHOD[seam.kind]
    if obs_service not in catalog:
        raise RevlError(
            doc, seam.line,
            f"seam row `@{seam.label}`: observer service `{obs_service}` is not "
            "declared in this composition",
            hint="add a `use` for the file declaring the observer service")
    obs_decl = catalog[obs_service][0]
    if method not in obs_decl.methods:
        raise RevlError(
            doc, seam.line,
            f"seam row `@{seam.label}` is `{seam.kind}`, so its observer service "
            f"`{obs_service}` must declare `fn {method}(...)`, and it does not",
            hint=f"an `{seam.kind}` seam calls `{method}` on the observer: "
                 "`observe` calls `saw`, `decide` calls `allow` (424 D-424b.4)")

    if wrapped_service not in catalog:
        raise RevlError(
            doc, seam.line,
            f"seam row `@{seam.label}`: wrapped service `{wrapped_service}` is "
            "not declared in this composition",
            hint="add a `use` for the file declaring the wrapped service")
    wrapped_decl = catalog[wrapped_service][0]
    # §2.4's fallback for D-424b.5: the declared `through` reach must stay within
    # the wrapped service's own emission bound. Enforced here at resolution,
    # before synthesis, so the refusal names the seam row and the capability.
    _check_seam_through(wrapped_decl, seam, doc)

    component, text = synthesize_provider(wrapped_decl, "seam", {
        "label": seam.label, "key": seam.key, "realm": seam.realm,
        "inner_key": inner_key, "observer_key": obs_key,
        "observer_service": obs_service, "kind": seam.kind,
        "through": tuple(seam.through),
        "doc": doc, "line": seam.line,
    })
    rel = _synth_seam_path(origin, seam.label)
    sources[rel] = text
    return Row(
        label=seam.label,
        origin=origin,
        source=rel,
        component=component,
        claims=[(seam.key, seam.realm)],
        extra_claims=[],
        requires=sorted([inner_key, obs_key]),
        config={},
        granted=None,
        line=seam.line,
        seam={
            "edge": claim_str((seam.key, seam.realm)),
            "kind": seam.kind,
            "observer": seam.observer,
            "innerKey": inner_key,
            "service": wrapped_service,
            "observerService": obs_service,
            **({"realm": seam.realm} if seam.realm else {}),
            **({"through": list(seam.through)} if seam.through else {}),
        },
    )


def resolve(decl: CompositionDecl, doc_path: str,
            root: str | None = None) -> RowTable:
    """Resolve one composition declaration into its row table.

    Header-only: every row's source is parsed and its component header read, and
    no body is lowered. Every failure is a REFUSAL naming the row (426 §2.4:
    an address that resolves to nothing is a refusal, never a no-op).

    This is the BASE table (level 0). `fold` (S2) starts from it and applies the
    layers the document declares; a document declaring none folds to itself.
    """
    # Provenance and origin are recorded relative to the PROJECT root (the
    # invocation cwd by default), the same rule `parse_file` follows, so an IR
    # document stays machine-independent.
    root = os.path.abspath(root or os.getcwd())
    doc = decl.source or doc_path
    origin = origin_of(doc_path, root)
    base = os.path.dirname(os.path.abspath(doc_path))

    rows = [_resolve_row(row, origin, doc, base, root)[0] for row in decl.rows]
    # item 424 R3: base-composition placement. A `place` names an existing row,
    # so it runs after the rows resolve and refuses if it names nothing.
    if decl.places:
        rows_by_qual = {r.qualified: r for r in rows}
        for spec in decl.places:
            _record_place(spec, rows_by_qual, origin, doc, decl.name, 0, BASE_LAYER)
    uses = _resolve_uses(decl, doc, base, root)
    sources = _resolve_remotes(decl, doc, origin, root, uses, rows)
    sources.update(_resolve_hosts(decl, doc, origin, root, uses, rows))
    sources.update(_resolve_seams(decl, doc, origin, root, uses, rows))
    _check_disjoint(rows, decl.name, doc)
    return RowTable(decl.name, origin, _relative(doc_path, root), rows, uses,
                    sources)


# ------------------------------------------------------------------- the fold
#
# 426 S2. Resolution is a PURE FOLD and the gate is never inside it (§3.3), for
# two independent reasons and both are load-bearing:
#
# - CORRECTNESS. Per-operation admission opens a determinism trap: `remove
#   key("logger")` then `add @logger` succeeds while the reverse order refuses
#   on G2 at an intermediate state, so the verdict depends on the order the ops
#   happened to be listed in. Folding first means no intermediate state exists —
#   `_check_disjoint` runs ONCE, over the final table.
# - SOUNDNESS. `_link` still runs G2 and G3 unchanged over the assembled
#   composition. The fold is a pre-check whose only privilege is a better
#   message, so a bug here cannot admit something `_link` would refuse; it can
#   only over-refuse, which is a usability bug and not a soundness one.
#
# Every input is an ordered list in a file. Nothing here depends on filesystem
# iteration order, directory listing order, or wall-clock time.

BASE_LAYER = "<base>"

_LEVEL_NAME = {0: "base", 1: "stack", 2: "site", 3: "invocation"}

# item 424 R3: the backends a `place` may name. Mirrors
# `placement.KNOWN_BACKENDS` (the `[processes]`/`[tiers]` machinery `place`
# subsumes, `placement.py:85`), kept as a local constant so header-only
# resolution does not import the placement runner. An unknown backend is a
# REFUSAL, never a silent default — placement is structure and fails closed.
_KNOWN_BACKENDS = ("py", "node", "ts", "rust", "java", "go")


class _Slot:
    """One row in flight, with the header it resolved against (so a later
    `configure` re-checks its fields) and the layer that introduced it."""

    __slots__ = ("row", "header", "layer", "level")

    def __init__(self, row: Row, header: _Header, layer: str, level: int):
        self.row = row
        self.header = header
        self.layer = layer
        self.level = level


def _layer_error(layer: LayerDecl, line: int, message: str,
                 hint: str | None = None) -> RevlError:
    return RevlError(layer.source or "<layer>", line, message, hint=hint)


def _load_layer(path: str, target: CompositionDecl, doc: str, line: int,
                base: str, root: str) -> tuple[LayerDecl, str, str]:
    """Parse a layer document and check it is one.

    426 §6.1: **a layer document may contain ONLY layer operations.** No
    `component`, no `service`, no `extern`, no top-level `fn`. Without this rule
    a layer is a component-authoring surface, which is exactly the surface §4
    exists to profile, and the profile would then have to run over the layer
    document itself.
    """
    resolved = os.path.join(base, path)
    if not os.path.isfile(resolved):
        raise RevlError(
            doc, line,
            f"composition {target.name} declares the layer `{path}`, which does "
            "not exist",
            hint=f"resolved against `{_relative(base, root)}`. A missing layer is "
                 "a refusal, never a silently skipped one (426 §2.4)")
    program = parse_file(resolved)
    rel = _relative(resolved, root)
    foreign = [
        (kind, len(items))
        for kind, items in (("component", program.components),
                            ("service", program.services),
                            ("extern", program.externs),
                            ("fn", program.fn_decls),
                            ("composition", program.compositions))
        if items
    ]
    if foreign:
        listed = ", ".join(f"{count} `{kind}`" for kind, count in foreign)
        raise RevlError(
            rel, 1,
            f"`{rel}` is declared as a layer of {target.name} but also declares "
            f"{listed}",
            hint="a layer document contains ONLY layer operations (426 §6.1). A "
                 "layer that may author components is a component-authoring "
                 "surface, and the confinement profile would then have to run "
                 "over the layer document itself")
    if len(program.layers) != 1:
        raise RevlError(
            rel, program.layers[1].line if len(program.layers) > 1 else 1,
            f"`{rel}` declares {len(program.layers)} layers",
            hint="one layer per document: the document is the layer's origin "
                 "scope, the same rule a composition follows (426 §1.2)")
    layer = program.layers[0]
    if layer.target != target.name:
        raise RevlError(
            rel, layer.line,
            f"layer `{layer.name}` patches composition `{layer.target}`, but it "
            f"is declared in the stack of `{target.name}`",
            hint="a layer names the composition it patches, and the name is "
                 "checked rather than assumed")
    return layer, rel, origin_of(resolved, root)


def _address_token(address: Address, own_origin: str) -> tuple:
    """The address as a hashable identity, so two spellings of one target group
    together and two layers writing the same address are seen as one."""
    if address.kind == "key":
        return ("key", address.key, address.realm)
    return ("label", address.origin or own_origin, address.label)


def _resolve_address(address: Address, own_origin: str, slots: dict,
                     layer: LayerDecl, name: str) -> str:
    """An address to the qualified label of the row it names.

    **426 §2.4: an address that resolves to nothing is a REFUSAL, never a
    no-op.** This is the sharpest single difference from a patch system where a
    vanished target does nothing and the operator learns at runtime. The refusal
    names the address, the layer that wrote it, and what is there instead.
    """
    if address.kind == "label":
        want = qualified(address.origin or own_origin, address.label)
        if want in slots:
            return want
        near = [q for q in slots if q.endswith(f"::@{address.label}")]
        hint = (f"row `{near[0]}` has that label in another origin — address it "
                "fully qualified" if near else
                "rows in the folded composition: "
                + (", ".join(f"`{q}`" for q in slots) or "<none>"))
        raise _layer_error(
            layer, address.line,
            f"layer `{layer.name}` addresses row `{address.spelling()}`, which "
            f"no row is in composition {name}",
            hint=hint)
    claim = (address.key, address.realm)
    for qual, slot in slots.items():
        if claim in slot.row.claims or claim in slot.row.extra_claims:
            return qual
    served = []
    for qual, slot in slots.items():
        for other in [*slot.row.claims, *slot.row.extra_claims]:
            if other[0] == address.key:
                served.append(f"row `{qual}` claims {claim_str(other)}")
    hint = "; ".join(sorted(served)) if served else (
        "keys claimed in the folded composition: "
        + (", ".join(sorted({claim_str(c) for slot in slots.values()
                             for c in [*slot.row.claims, *slot.row.extra_claims]}))
           or "<none>"))
    raise _layer_error(
        layer, address.line,
        f"layer `{layer.name}` addresses {address.spelling()}, which no row "
        f"claims in composition {name}",
        hint=hint + ". Address the row directly by its label if you mean that "
             "exact row, or repin the source that dropped the key (426 §2.4)")


def _place_ir(spec: PlaceDecl) -> dict:
    """The placement fact recorded on a row (item 424 R3). `backend` is omitted
    when the placement leaves it to the composition's default, so a
    process-only placement stays a one-key dict."""
    out = {"process": spec.process}
    if spec.backend is not None:
        out["backend"] = spec.backend
    return out


def _place_target(address: Address, own_origin: str, rows_by_qual: dict,
                  source: str, name: str) -> str:
    """The qualified label a `place` address names, refusing (426 §2.4) if it
    names nothing. Like `_resolve_address` but reports against the placing
    document/layer `source` rather than a `LayerDecl`, so a base-composition
    `place` and a site-layer `place` share one resolver."""
    if address.kind == "label":
        want = qualified(address.origin or own_origin, address.label)
        if want in rows_by_qual:
            return want
        near = [q for q in rows_by_qual if q.endswith(f"::@{address.label}")]
        hint = (f"row `{near[0]}` has that label in another origin — address it "
                "fully qualified" if near else
                "rows in the composition: "
                + (", ".join(f"`{q}`" for q in rows_by_qual) or "<none>"))
        raise RevlError(
            source, address.line,
            f"`place {address.spelling()}` names row `{address.spelling()}`, "
            f"which is no row in composition {name}",
            hint=hint + " (426 §2.4: an address that resolves to nothing is a "
                        "refusal, never a no-op)")
    claim = (address.key, address.realm)
    for qual, row in rows_by_qual.items():
        if claim in row.claims or claim in row.extra_claims:
            return qual
    raise RevlError(
        source, address.line,
        f"`place {address.spelling()}` names a key no row claims in composition "
        f"{name}",
        hint="keys claimed here: "
             + (", ".join(sorted({claim_str(c) for row in rows_by_qual.values()
                                  for c in [*row.claims, *row.extra_claims]}))
                or "<none>")
             + " (426 §2.4: address a row that exists)")


def _record_place(spec: PlaceDecl, rows_by_qual: dict, own_origin: str,
                  source: str, name: str, level: int, layer_name: str) -> None:
    """Resolve one `place`'s address and record its fact on the target row.

    A later placement of the same row OVERWRITES an earlier one, which is how the
    site layer (level 2) has the operator's final say over the base (level 0).
    An unknown backend is refused here, naming the value (item 424 R3).
    """
    if spec.backend is not None and spec.backend not in _KNOWN_BACKENDS:
        raise RevlError(
            source, spec.line,
            f"`place {spec.address.spelling()}` names backend "
            f"`{spec.backend}`, which is not a known backend",
            hint="known backends: " + ", ".join(_KNOWN_BACKENDS)
                 + ". Placement is structure and fails closed, so an unknown "
                   "backend is a refusal rather than a silent default")
    qual = _place_target(spec.address, own_origin, rows_by_qual, source, name)
    row = rows_by_qual[qual]
    row.place = _place_ir(spec)
    if level:
        row.provenance.append((level, layer_name, "place"))


def _refuse_peers(sides: list[tuple[LayerDecl, int]], what: str, subject: str,
                  remedy: str, extra: str = "") -> RevlError:
    """A peer conflict. **Neither layer is preferred** (426 decision 4): the
    conflict refuses and only the operator's site layer resolves it.

    Both the message and its position are derived from the layer names SORTED,
    never from the order the stack listed them, so permuting the stack changes
    nothing at all — not the verdict, not the text, and not the file and line it
    is reported at (426 exit test 6).
    """
    ordered = sorted(sides, key=lambda side: side[0].name)
    (at, line), other = ordered[0], ordered[1]
    return RevlError(
        at.source or "<layer>", line,
        f"layer conflict on {subject}: stack layers `{at.name}` and "
        f"`{other[0].name}` both {what}",
        hint=f"neither layer is preferred (426 §3.4){extra}. Only the operator's "
             f"site layer decides, and it decides by naming what it means:\n"
             f"  {remedy}\n"
             "(this is G2, provision disjointness, seen at the layer level)")


def fold(decl: CompositionDecl, doc_path: str, root: str | None = None,
         overlay: dict | None = None) -> RowTable:
    """The base row table with its declared layers folded in (426 §3.3).

    1. Start from the base row table.
    2. Apply level 1 stack layers. Peer conflicts refuse, so the result is
       independent of the order in which stack layers are listed.
    3. Apply the site layer, which may resolve a level-1 refusal by naming both
       sides.
    4. Apply the invocation overlay, values only, never structure.
    5. Record, per row, the ordered provenance.

    Header-only throughout: `--admit` is still what compiles anything.
    """
    root = os.path.abspath(root or os.getcwd())
    doc = decl.source or doc_path
    origin = origin_of(doc_path, root)
    base = os.path.dirname(os.path.abspath(doc_path))

    slots: dict[str, _Slot] = {}
    for rowdecl in decl.rows:
        row, header = _resolve_row(rowdecl, origin, doc, base, root)
        row.provenance = [(0, BASE_LAYER, "row")]
        slots[row.qualified] = _Slot(row, header, BASE_LAYER, 0)

    stack: list[tuple[LayerDecl, str, str]] = [
        _load_layer(path, decl, doc, line, base, root) for path, line in decl.stack]
    site = _load_layer(decl.site[0], decl, doc, decl.site[1], base, root) \
        if decl.site is not None else None

    # The site layer's `resolve` directives are read BEFORE the peer rules run,
    # because resolving a peer conflict is precisely what they are for: a
    # conflict the operator has already decided must not refuse (426 §3.4).
    resolutions: dict[tuple, tuple[str, str, LayerDecl, int]] = {}
    if site is not None:
        site_layer, _, site_origin = site
        for op in site_layer.ops:
            if op.op != "resolve":
                continue
            token = _address_token(op.address, site_origin)
            winner = qualified(op.winner.origin or site_origin, op.winner.label)
            loser = qualified(op.loser.origin or site_origin, op.loser.label)
            if winner == loser:
                raise _layer_error(
                    site_layer, op.line,
                    f"`resolve {op.address.spelling()}` names `{winner}` on both "
                    "sides",
                    hint="a resolution names two DIFFERENT rows; naming one twice "
                         "decides nothing")
            resolutions[token] = (winner, loser, site_layer, op.line)

    _apply_stack(stack, slots, decl, doc, resolutions, root)
    if site is not None:
        _apply_site(site, slots, decl, doc, root)
    _apply_overlay(overlay or {}, slots, decl, doc)
    # item 424 R3: placement, applied over the folded rows. Base placements
    # (level 0) first, then the site layer's (level 2) — so the operator's site
    # layer has the final say, exactly as a stack layer may not place at all
    # (refused at parse). A `place` addressing a vanished row refuses (426 §2.4).
    rows_by_qual = {q: slot.row for q, slot in slots.items()}
    for spec in decl.places:
        _record_place(spec, rows_by_qual, origin, doc, decl.name, 0, BASE_LAYER)
    if site is not None:
        site_layer, site_rel, site_origin = site
        for op in site_layer.ops:
            if op.op == "place":
                _record_place(op.place, rows_by_qual, site_origin,
                              site_layer.source or site_rel, decl.name,
                              2, site_layer.name)
    rows = [slot.row for slot in slots.values()]

    uses = _resolve_uses(decl, doc, base, root)
    # item 424 C2: a `remote` row is an ordinary row, so it is resolved here
    # too — a composition that declares a layer must not silently lose it.
    # Layers reach rows by address and no op addresses a synthesized row, so
    # the remotes join AFTER the fold and before the one disjointness check.
    sources = _resolve_remotes(decl, doc, origin, root, uses, rows)
    # item 457 S3: a `host` row is an ordinary row like a `remote` one, so it
    # joins here too — no layer op addresses a synthesized row.
    sources.update(_resolve_hosts(decl, doc, origin, root, uses, rows))

    _check_disjoint(rows, decl.name, doc)
    return RowTable(decl.name, origin, _relative(doc_path, root), rows, uses,
                    sources)


def _reject_granted(layer: LayerDecl, rowdecl: RowDecl) -> None:
    """424 R2's third rule, the one 426 S1 could not build because it needs
    layers: `granted` is writable in the base composition and the site layer,
    **never in a stack layer**. No layer may raise its own authority."""
    if layer.site or rowdecl.granted is None:
        return
    raise _layer_error(
        layer, rowdecl.line,
        f"stack layer `{layer.name}` writes a `granted` clause on row "
        f"`@{rowdecl.label}`",
        hint="`granted` is the reach allowlist a CONFINED row may compose "
             "against, so a stack layer granting itself keys is a layer raising "
             "its own authority. It is writable in the base composition and in "
             "the site layer only (424 R2, 426 §9.3 Part 2)")


def _apply_stack(stack, slots, decl, doc, resolutions, root) -> None:
    """Level 1, in the one order that makes the fold order-free.

    Every operation in every stack layer is collected and grouped by its TARGET
    before anything is applied, so permuting the stack cannot change which group
    an operation lands in and therefore cannot change the verdict (§3.3, exit
    test 6).

    WITHDRAWALS ARE APPLIED BEFORE ADMISSIONS, which is what makes
    `remove key("logger")` + `add @logger` and the reverse produce the same
    answer (exit test 8). Admitting per operation is the determinism trap §3.3
    exists to kill; admitting the whole delta at once means the intermediate
    state where both rows claim `logger` never exists.
    """
    if not stack:
        return

    # (1) every addition, resolved. A row's header is read here; nothing
    #     cross-row is decided yet.
    added: dict[str, tuple[LayerDecl, RowDecl, Row, _Header]] = {}
    for layer, rel, layer_origin in stack:
        base_dir = os.path.dirname(os.path.join(root, rel))
        for op in layer.ops:
            if op.op in ("add", "replace"):
                _reject_granted(layer, op.row)
            if op.op != "add":
                continue
            row, header = _resolve_row(op.row, layer_origin, rel, base_dir, root)
            if row.qualified in slots:
                raise _layer_error(
                    layer, op.row.line,
                    f"layer `{layer.name}` adds row `{row.qualified}`, which "
                    f"composition {decl.name} already declares",
                    hint="`add` introduces a row; to change the one that is "
                         "there, address it with `replace` or `configure`")
            other = added.get(row.qualified)
            if other is not None:
                raise _refuse_peers(
                    [(other[0], other[1].line), (layer, op.row.line)],
                    "add it", f"row `{row.qualified}`",
                    f"replace {row.qualified} with the row you want")
            added[row.qualified] = (layer, op.row, row, header)

    # (2) the address universe: the base table PLUS every stack addition. A
    #     layer may address a row a peer added, and the answer does not depend
    #     on which layer was listed first.
    universe = dict(slots)
    for qual, (layer, _decl, row, header) in added.items():
        universe[qual] = _Slot(row, header, layer.name, 1)

    # (3) every other operation, grouped by target, then the §3.4 table.
    groups: dict[str, list] = {}
    for layer, rel, layer_origin in stack:
        _check_touches(layer, layer_origin, universe, decl, doc)
        for op in layer.ops:
            if op.op == "add":
                continue
            target = _resolve_address(op.address, layer_origin, universe, layer,
                                      decl.name)
            groups.setdefault(target, []).append((layer, layer_origin, op, rel))
    for target, ops in groups.items():
        _peer_rules(target, ops)

    # (4) withdrawals first, so an addition never has to see the row it
    #     supersedes.
    for target, ops in groups.items():
        if any(op.op == "remove" for _l, _o, op, _r in ops):
            slots.pop(target, None)
            added.pop(target, None)

    # (5) admissions, checked against the POST-withdrawal table.
    _apply_adds(added, slots, decl, resolutions)

    # (6) the patches.
    for target, ops in groups.items():
        for layer, layer_origin, op, rel in ops:
            if op.op == "remove":
                continue
            _apply_op(target, layer, layer_origin, op, rel, slots, decl,
                      doc, 1, root)


def _apply_adds(added, slots, decl, resolutions) -> None:
    """A stack addition enters the table only if its claims do not intersect a
    row already there or a peer's addition. That intersection is the one
    conflict the operator can pre-decide, with `resolve <address> to <winner>
    over <loser>` in the site layer (426 §3.4)."""
    dropped: set[str] = set()
    for winner, loser, _site, _line in resolutions.values():
        if loser in added and (winner in added or winner in slots):
            dropped.add(loser)
            if winner in added:
                added[winner][2].provenance = [
                    (1, added[winner][0].name, "add"), (2, _site.name, "resolve")]
        elif loser in slots and winner in added:
            slots.pop(loser)
            added[winner][2].provenance = [
                (1, added[winner][0].name, "add"), (2, _site.name, "resolve")]

    for qual, (layer, rowdecl, row, header) in added.items():
        if qual in dropped:
            continue                       # the operator decided against it
        for claim in [*row.claims, *row.extra_claims]:
            for other_qual, (other_layer, other_decl, other_row, _h) in added.items():
                if other_qual in (qual, *dropped):
                    continue
                if claim in other_row.claims or claim in other_row.extra_claims:
                    raise _refuse_peers(
                        [(other_layer, other_decl.line), (layer, rowdecl.line)],
                        f"add a row claiming {claim_str(claim)}",
                        claim_str(claim),
                        f"resolve {claim_str(claim)} to {other_qual} over {qual}",
                        extra=" — precedence never chooses a provider "
                              "(decision 4)")
            for slot in slots.values():
                if claim in slot.row.claims or claim in slot.row.extra_claims:
                    raise _layer_error(
                        layer, rowdecl.line,
                        f"layer `{layer.name}` adds row `{qual}`, whose "
                        f"{claim_str(claim)} row `{slot.row.qualified}` already "
                        f"claims in composition {decl.name}",
                        hint="a layer that means to take a claim over writes "
                             "`replace`, which is claim-preserving and loud in "
                             "the diff, or `remove` plus `add`, which the fold "
                             "applies as one delta (426 §3.2)")
        if not row.provenance:
            row.provenance = [(1, layer.name, "add")]
        slots[qual] = _Slot(row, header, layer.name, 1)


def _peer_rules(target: str, ops) -> None:
    """426 §3.4, the whole table.

    | two stack layers `replace` the same row            | REFUSED   |
    | two `configure` the same row and field, differently| REFUSED   |
    | two `configure` the same row, disjoint fields      | merged    |
    | two `remove` the same row                          | idempotent|
    | one `remove`s, another `replace`s or `configure`s  | REFUSED   |
    """
    replaces = [(layer, op) for layer, _o, op, _r in ops if op.op == "replace"]
    removes = [(layer, op) for layer, _o, op, _r in ops if op.op == "remove"]
    configures = [(layer, op) for layer, _o, op, _r in ops if op.op == "configure"]
    label = f"row `{target}`"

    if len(replaces) > 1:
        raise _refuse_peers(
            [(layer, op.line) for layer, op in replaces[:2]], "replace it",
            label, f"replace {target} with the row you want")
    if removes and (replaces or configures):
        other = (replaces or configures)[0]
        raise _refuse_peers(
            [(removes[0][0], removes[0][1].line), (other[0], other[1].line)],
            f"withdraw and {other[1].op} it", label,
            f"remove {target}, or {other[1].op} it — the site layer wins over "
            "any stack layer",
            extra=" — a withdrawal and a patch of the same row are not "
                  "reconcilable at any order")
    fields: dict[str, tuple[LayerDecl, int, object]] = {}
    for layer, op in configures:
        for name, value, _line in op.config:
            prior = fields.get(name)
            if prior is not None and prior[2] != value:
                raise _refuse_peers(
                    [(prior[0], prior[1]), (layer, op.line)],
                    f"set config field `{name}` of {label}, to "
                    f"{prior[2]!r} and {value!r}", label,
                    f"configure {target} with {{ {name}: ... }}")
            fields[name] = (layer, op.line, value)


def _apply_op(target, layer, layer_origin, op, rel, slots, decl, doc,
              level, root) -> None:
    slot = slots.get(target)
    if slot is None:
        return                                   # already withdrawn, idempotent
    if op.op == "remove":
        slots.pop(target)
        return
    if op.op == "resolve":
        return                                   # handled before the peer rules
    if op.op == "replace":
        base_dir = os.path.dirname(os.path.join(root, rel))
        row, header = _resolve_row(op.row, slot.row.origin, rel, base_dir, root)
        # 426 §3.2: **`replace` is claim-preserving.** A replacement claiming
        # exactly what it replaced can never create or destroy a G2 conflict,
        # which is the second determinism lever. Changing what a row claims is
        # expressible, but only as `remove` plus `add`, which is loud.
        was = {*slot.row.claims, *slot.row.extra_claims}
        now = {*row.claims, *row.extra_claims}
        if was != now:
            lost = ", ".join(sorted(claim_str(c) for c in was - now)) or "nothing"
            gained = ", ".join(sorted(claim_str(c) for c in now - was)) or "nothing"
            raise _layer_error(
                layer, op.row.line,
                f"layer `{layer.name}` replaces row `{target}` with a component "
                f"claiming a different set (drops {lost}, adds {gained})",
                hint="`replace` preserves the claim set, so it can never create "
                     "or destroy a G2 conflict. Changing what a row claims is "
                     "`remove` plus `add`, which is loud in the diff (426 §3.2)")
        # The LABEL is preserved: it is the row's identity and the replacement
        # is a new implementation of the same row, not a new row.
        row.label, row.origin = slot.row.label, slot.row.origin
        row.provenance = [*slot.row.provenance, (level, layer.name, "replace")]
        # 426 §8.3/§8.6: the composition-level `reach` bound and the `open` set
        # belong to the base composition; a stack-layer `replace` cannot drop
        # or widen them (that would be raising its own authority). Both are
        # inherited and clamped — a replacement may narrow (a tighter reach
        # bound, a smaller open set) but a widening one is REFUSED: a wider
        # reach lets a config value escape, a wider open set lets a later stack
        # layer configure a field the base kept closed. The reach bound is then
        # re-checked against the replacement's config below.
        row.reach = _merge_replacement_reach(
            slot.row.reach, row.reach, layer, op.row.line, target)
        row.open = _merge_replacement_open(
            slot.row.open, row.open, layer, op.row.line, target)
        _check_reach_bounds(row.label, row.config, row.reach,
                            layer.source or doc, op.row.line,
                            f"by layer `{layer.name}`")
        slots[target] = _Slot(row, header, layer.name, level)
        return
    if op.op == "configure":
        _configure(target, layer, op, slot, decl, doc, level)


def _configure(target, layer, op, slot, decl, doc, level) -> None:
    """`configure @id with { field: value }` merges fields into a row's config.

    426 §3.2's two rules, both kept:

    - `configure` against a NON-CONFIG row is a refusal, not a best-effort
      patch. A patch system that happily writes a key nothing reads is how a
      layered composition breaks silently.
    - a value that does not fit the declared return type **does not admit**, and
      the refusal names the field and the declared type. This is the most common
      way a layered composition breaks in practice, turned into a refusal before
      anything runs (exit test 9).

    §3.2 frames this as desugaring to a `replace` with a SYNTHESIZED config
    component, on the premise that revl has no config fields. That premise is
    stale: `config { field: Type = default }` is a declared part of a component
    header and S1 already checks a row's `config` block against it. So
    `configure` merges into that block and re-runs the same check, which is the
    same guarantee with no synthesis machinery. Synthesis remains the answer for
    configuration that is a SERVICE (a component whose provide-methods are
    constants), which is a different shape and not what an address names.
    """
    if not slot.header.config:
        raise _layer_error(
            layer, op.line,
            f"layer `{layer.name}` configures row `{target}`, whose component "
            f"`{slot.row.component}` declares no config",
            hint="`configure` against a non-config row is a refusal, never a "
                 "best-effort patch: the fields would be written and nothing "
                 "would read them (426 §3.2)")
    # 426 §8.6: a STACK layer (level 1) may configure only a row it also
    # add/replaces, or a field the base composition declared `open`. The SITE
    # layer (level 2) and the invocation overlay (level 3) configure anything —
    # the operator is not attacking themselves. An unrestricted `configure` on
    # a row a stack layer does not own is an authority with no declared reach,
    # the shape 424(b) refuses for interception.
    if level == 1:
        owns = any(lname == layer.name and o in ("add", "replace")
                   for _lvl, lname, o in slot.row.provenance)
        if not owns:
            allowed = slot.row.open or set()
            for name, _value, _line in op.config:
                # A field the component does not declare is a typo, not an
                # authority question — let `_check_config` give the clearer
                # "unknown config field" refusal below.
                if name not in slot.header.config:
                    continue
                if name not in allowed:
                    listed = ", ".join(f"`{f}`" for f in sorted(allowed)) or "<none>"
                    raise _layer_error(
                        layer, op.line,
                        f"stack layer `{layer.name}` configures `{name}` on row "
                        f"`{target}`, which it does not own and the base "
                        f"composition did not declare `open`",
                        hint=f"fields `open` on `{target}`: {listed}. A stack "
                             "layer configures only a row it add/replaces or a "
                             "field the base opened; the operator's SITE layer "
                             "configures anything (426 §8.6)")
    merged = RowDecl(
        label=slot.row.label, path="", claims=[], line=op.line,
        component=slot.row.component,
        config=[(name, value, op.line)
                for name, value in {**slot.row.config,
                                    **{n: v for n, v, _ in op.config}}.items()],
    )
    slot.row.config = _check_config(merged, slot.header, layer.source or doc,
                                    slot.row.source)
    # 426 §8.3: a `configure` that moves a bounded field outside its reach is a
    # refusal, re-checked against the folded value here rather than only at base
    # resolution — this is CRITICAL B's redirect (`url -> attacker.example`).
    _check_reach_bounds(slot.row.label, slot.row.config, slot.row.reach,
                        layer.source or doc, op.line, f"by layer `{layer.name}`")
    slot.row.provenance = [*slot.row.provenance, (level, layer.name, "configure")]


def _check_touches(layer: LayerDecl, layer_origin: str, slots, decl,
                   doc) -> None:
    """A layer whose ops address something outside its own `touches` is refused
    (426 §3.4).

    This is a CONVENIENCE made checkable, not a security property: an author who
    wants to touch a row simply lists it. What it buys is that a layer's reach
    can be read off its head without resolving it.
    """
    if layer.touches is None:
        return
    allowed = {_resolve_address(a, layer_origin, slots, layer, decl.name)
               for a in layer.touches}
    for op in layer.ops:
        if op.op == "add":
            reached = qualified(layer_origin, op.row.label)
        elif op.op == "resolve":
            continue
        else:
            reached = _resolve_address(op.address, layer_origin, slots, layer,
                                       decl.name)
        if reached not in allowed:
            listed = ", ".join(f"`{a}`" for a in sorted(allowed)) or "<none>"
            raise _layer_error(
                layer, op.line,
                f"layer `{layer.name}` {op.op}s row `{reached}`, which its own "
                "`touches` clause does not list",
                hint=f"declared touches: {listed}. Add it, or drop the operation "
                     "(426 §3.4)")


def _apply_site(site, slots, decl, doc, root) -> None:
    """Level 2. Exactly one, written by the operator, and it never refuses
    against a stack layer: the operator is not a peer of the layers, the
    operator is the person the refusal is shown to (426 §3.4)."""
    layer, rel, layer_origin = site
    base_dir = os.path.dirname(os.path.join(root, rel))
    for op in layer.ops:
        if op.op != "add":
            continue
        row, header = _resolve_row(op.row, layer_origin, rel, base_dir, root)
        if row.qualified in slots:
            raise _layer_error(
                layer, op.row.line,
                f"site layer `{layer.name}` adds row `{row.qualified}`, which "
                "is already in the composition",
                hint="`add` introduces a row; `replace` changes the one there")
        row.provenance = [(2, layer.name, "add")]
        slots[row.qualified] = _Slot(row, header, layer.name, 2)
    _check_touches(layer, layer_origin, slots, decl, doc)
    for op in layer.ops:
        if op.op == "add":
            continue
        if op.op == "place":
            # item 424 R3: placement is applied separately in `fold`, after the
            # whole fold, so the site layer's placement lands over any base
            # placement. It is not a config-style patch, so it does not flow
            # through `_apply_op`. Its authority (site-only) is enforced at parse.
            continue
        if op.op == "resolve":
            # Already consumed: a `resolve` that decided nothing is still a
            # refusal, because an operator who wrote it believed there was a
            # conflict and there was not.
            target = _resolve_address(op.address, layer_origin, slots, layer,
                                      decl.name)
            winner = qualified(op.winner.origin or layer_origin, op.winner.label)
            if target != winner:
                raise _layer_error(
                    layer, op.line,
                    f"`resolve {op.address.spelling()} to {op.winner.spelling()}` "
                    f"did not decide anything: that address resolves to row "
                    f"`{target}`",
                    hint="a resolution that changes nothing hides the conflict it "
                         "was written for — check the losing row is still in the "
                         "stack (426 §2.4)")
            continue
        target = _resolve_address(op.address, layer_origin, slots, layer,
                                  decl.name)
        if op.op in ("add", "replace"):
            _reject_granted(layer, op.row)
        _apply_op(target, layer, layer_origin, op, rel, slots, decl, doc,
                  2, root)


def _apply_overlay(overlay: dict, slots, decl, doc) -> None:
    """Level 3, the invocation overlay: **values only, never structure**
    (426 §3.1). `--set @db.pool=16` reaches a field of a row that already
    exists; it cannot add, remove or replace one, and it is typed exactly like
    every other config value."""
    for (label, field_name), value in overlay.items():
        if field_name in ("process", "backend"):
            # item 424 R3: placement is STRUCTURE, and the invocation overlay
            # carries VALUES only (426 §3.1). A row's process or backend is a
            # `place` statement in the base composition or the site layer, never
            # a `--set` at invocation — a run choosing where its own row is
            # judged is item 337's admission-theater at the last tier.
            raise RevlError(
                doc, 1,
                f"`--set @{label}.{field_name}` sets a row's {field_name}, which "
                "is STRUCTURE, not a value",
                hint="the invocation overlay carries VALUES only (426 §3.1). A "
                     "row's process/backend is placement, written as a `place` "
                     "statement in the base composition or the site layer, never "
                     "at invocation (424 R3)")
        matches = [q for q in slots if q.endswith(f"::@{label}")] \
            if "::" not in label else [label]
        matches = [q for q in matches if q in slots]
        if len(matches) != 1:
            raise RevlError(
                doc, 1,
                f"`--set @{label}.{field_name}` names row `@{label}`, which "
                + ("is not in the composition" if not matches
                   else f"is ambiguous ({', '.join(matches)})"),
                hint="the invocation overlay carries VALUES only — it reaches a "
                     "field of a row that already exists (426 §3.1)")
        slot = slots[matches[0]]
        merged = RowDecl(label=slot.row.label, path="", claims=[], line=1,
                         component=slot.row.component,
                         config=[(n, v, 1) for n, v in
                                 {**slot.row.config, field_name: value}.items()])
        slot.row.config = _check_config(merged, slot.header, doc, slot.row.source)
        slot.row.provenance = [*slot.row.provenance,
                               (3, "<invocation>", "configure")]


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
    if program.layers:
        raise RevlError(
            path, program.layers[0].line,
            f"`{os.path.basename(path)}` declares composition "
            f"`{program.compositions[0].name}` and also layer "
            f"`{program.layers[0].name}`",
            hint="a layer lives in its own document and is named in the "
                 "composition's `stack` (or `site`) list. A layer beside the "
                 "composition would be silently unapplied, and 426 §2.4 has no "
                 "silent no-ops")
    return program.compositions[0]


def resolve_file(path: str, root: str | None = None,
                 overlay: dict | None = None) -> RowTable:
    """Parse a composition document and resolve its row table, folding in the
    layers it declares.

    A document that declares no `stack` and no `site` folds to itself and
    produces the S1 table byte for byte, which is why this is the one entry
    point rather than two.
    """
    decl = sole_composition(parse_file(path), path)
    if not decl.stack and decl.site is None and not overlay:
        return resolve(decl, path, root)
    return fold(decl, path, root, overlay)


def _carry_rows(document: dict, table: RowTable) -> dict:
    """Carry the resolved row table into the IR and the manifest, so the
    document names the composition object whichever admission path built it."""
    document["rows"] = table.to_ir()
    if isinstance(document.get("manifest"), dict):
        document["manifest"]["rows"] = document["rows"]
    return document


def row_trust(row: "Row") -> str:
    """The row's TRUST CLASS by the DECLARING DOCUMENT (426 §4.1).

    Trust class is a distribution fact, not something a document chooses: a row
    whose current source was contributed by a STACK layer (level 1) is
    non-first-party whatever its `from` path names; the base composition (level
    0) and the operator's site layer (level 2) are first-party (§4.1). Read off
    the last add/replace/row entry in the fold provenance, which is where the
    source was set.
    """
    level = 0
    for lvl, _layer, op in row.provenance:
        if op in ("row", "add", "replace"):
            level = lvl
    return "non-first-party" if level == 1 else "first-party"


def _trusts(trust_host_code, qualified: str) -> bool:
    """Whether the operator asked to admit `qualified` under `--trust-host-code`
    (426 §8.8). `True` trusts every non-first-party row; a set trusts the named
    rows only. Anything else trusts none."""
    if trust_host_code is True:
        return True
    if isinstance(trust_host_code, (set, frozenset)):
        return qualified in trust_host_code
    return False


def _row_profiles(table: RowTable, root: str,
                  trust_host_code=False) -> dict | None:
    """The per-root admission-profile map `compile_files` takes (426 S4, §9.3
    Part 2), computed from each row's trust class (§4.1).

    A non-first-party row admits under `untrusted_author` — no host code, reach
    bounded to its `granted` set — so 425 F1's declared-`pure` exfiltrating body
    has no reachable spelling on the layer path. A first-party row is unmapped
    and falls back to the caller's single profile (the default trusted author).
    `--trust-host-code` lifts the profile off the named rows: they then admit
    like first-party source, which is the SHAPE change §8.8 describes, and the
    panel forfeits `clean` for having done so.
    """
    profiles: dict[str, AdmissionProfile] = {}
    for row in table.rows:
        if row_trust(row) != "non-first-party":
            continue
        if _trusts(trust_host_code, row.qualified):
            continue
        profiles[os.path.join(root, row.source)] = \
            AdmissionProfile.untrusted_author(row.granted or ())
    return profiles or None


def _admit_full(table: RowTable, root: str, **kwargs) -> dict:
    """Compile every row a table names — whole-composition admission.

    This is the admission path S1 shipped: the file list `compile_files` takes,
    composition-declared rather than hand-listed (the 426 §6 point 3 bootstrap
    shrink). `admit_composition(full=True)` and a layer-free composition both
    reach this.
    """
    from .compiler import compile_files  # noqa: PLC0415 (cycle: compiler -> parser -> here)

    # 426 S4/S5 (§9.3 Part 2, §4.1): the per-root admission-profile map. A
    # caller confining non-first-party rows passes `confine=True`, which builds
    # the map from the row table's trust classes (each stack-layer row under its
    # own `untrusted_author`, honouring `trust_host_code`); an explicit
    # `profiles=` still wins, and the default (neither given) is byte-identical
    # to the single-profile path S1 shipped.
    trust_host_code = kwargs.pop("trust_host_code", False)
    confine = kwargs.pop("confine", False)
    if confine and "profiles" not in kwargs:
        profiles = _row_profiles(table, root, trust_host_code)
        if profiles is not None:
            kwargs["profiles"] = profiles

    # item 424 C2: a synthesized provider is compiled from memory. It is
    # ORDINARY revl source and the ordinary compiler compiles it, so `_link`
    # runs G2, G3 and G4 over it exactly as it does over a file row — which is
    # why nothing in `synthesize.py` is on the trusted path either.
    sources = dict(kwargs.pop("sources", None) or {})
    for rel, text in table.sources.items():
        sources[os.path.join(root, rel)] = text
    document = compile_files([os.path.join(root, p) for p in table.paths()],
                             sources=sources or None, **kwargs)
    return _carry_rows(document, table)


def _resolved_delta(base: RowTable,
                    final: RowTable) -> tuple[list[Row], tuple[str, ...]]:
    """The resolved delta between an already-admitted base and the folded final
    composition (426 §5.1): the rows to (re)compile, and the running-manifest
    components the delta withdraws.

    A final row with no base row of its label is an ADD; one whose source
    component differs is a REPLACE; a base label absent from the final table is
    a REMOVE. Only ADD and REPLACE rows are compiled — the change §5.1 names.
    `replacing` withdraws every component a REMOVE drops or a REPLACE supersedes
    UNDER A NEW NAME; an in-place swap (same component name) is withdrawn
    implicitly by `compile_files`, so only a rename is named here. A
    configure-only patch changes neither source nor component, so it enters
    neither set — config never reaches the gate (§5.3), it lives in the rows IR.
    """
    base_by = {row.qualified: row for row in base.rows}
    final_by = {row.qualified: row for row in final.rows}
    delta: list[Row] = []
    replacing: list[str] = []
    for row in final.rows:
        prior = base_by.get(row.qualified)
        if prior is None:
            delta.append(row)                          # ADD
        elif prior.source != row.source or prior.component != row.component:
            delta.append(row)                          # REPLACE
            if prior.component != row.component:
                replacing.append(prior.component)      # a rename: name it
    for qualified, prior in base_by.items():
        if qualified not in final_by:
            replacing.append(prior.component)          # REMOVE
    return delta, tuple(replacing)


def _delta_sources(rows: list[Row], final: RowTable,
                   root: str) -> tuple[list[str], dict[str, str]]:
    """The compile roots for the delta rows — each row's source, deduplicated in
    order — plus the in-memory source for any SYNTHESIZED (remote) provider
    among them, keyed the way `compile_files` takes it (absolute path)."""
    paths: list[str] = []
    synth: dict[str, str] = {}
    for row in rows:
        if row.source not in paths:
            paths.append(row.source)
        text = final.sources.get(row.source)
        if text is not None:
            synth[os.path.join(root, row.source)] = text
    return paths, synth


def compile_composition(path: str, root: str | None = None,
                        overlay: dict | None = None, **kwargs) -> dict:
    """Compile a composition document: resolve the row table, compile the rows
    it names, and carry the table into the IR and the manifest.

    This is the bootstrap shrink 426 §6 point 3 claims: a declared composition
    is compiled and READ (its rows are already in the IR) instead of compiled,
    emitted, exec'd, called and parsed back out of a JSON string.

    Whole-composition admission: every row is compiled. `admit_composition` is
    the S3 incremental door onto the same verdict.
    """
    root = os.path.abspath(root or os.getcwd())
    return _admit_full(resolve_file(path, root, overlay), root, **kwargs)


def admit_composition(path: str, root: str | None = None,
                      overlay: dict | None = None, full: bool = False,
                      trust_host_code=False, **kwargs) -> dict:
    """Admit a composition, INCREMENTALLY by default (426 S3, §5.1).

    A composition that declares layers is a DELTA over an already-admitted
    base: the layers add, replace and remove rows. Incremental admission
    compiles the base once, then admits only the changed and `replacing` rows
    INTO that running manifest — `admit_into` over the resolved delta (§5.1) —
    so the cost of the operator's decision is one compile of the patched rows,
    not of the whole composition. `_link` still spans the union of the ambient
    manifest and the delta (`compiler.py`, `lower.py:_link`), so the verdict
    and the resulting manifest are the ones a whole-composition compile reaches
    for the SAME final composition. That equivalence is the property S3 owes,
    and it is why the fold stays off the trusted path (§3.3): a delta that would
    collide with the running composition is refused with the collision's
    why-trace, exactly as a full compile refuses it.

    `full=True` forces whole-composition admission — every row compiled, the
    behaviour `compile_composition` has always had. It is the escape hatch §5.1
    (I1) names for a future cross-row invariant that a per-row admission cannot
    honour, and it is what a caller reaches for to re-establish the base line
    the incremental verdict is checked against.

    A composition with no declared layer has no delta: the base IS the whole
    composition, so both modes are the full compile and this returns that
    document.
    """
    from .compiler import compile_files  # noqa: PLC0415 (cycle: compiler -> parser -> here)

    root = os.path.abspath(root or os.getcwd())
    decl = sole_composition(parse_file(path), path)
    final = resolve_file(path, root, overlay)

    confine = kwargs.pop("confine", False)
    if full or (not decl.stack and decl.site is None):
        return _admit_full(final, root, trust_host_code=trust_host_code,
                           confine=confine, **kwargs)

    base = resolve(decl, path, root)
    delta_rows, replacing = _resolved_delta(base, final)

    # The already-admitted base: one whole compile, the running manifest every
    # delta row is admitted INTO. The base is all first-party, so it carries no
    # per-root profile; the layer delta is where the trust classes appear.
    base_doc = _admit_full(base, root, **kwargs)
    if not delta_rows and not replacing:
        # A configure-only delta changes no wiring and no component, so the base
        # manifest already IS the resulting one (§5.3). Only the rows IR carries
        # the new config, so re-key the base document onto the folded table.
        return _carry_rows(base_doc, final)

    extra = dict(kwargs.pop("sources", None) or {})
    paths, synth = _delta_sources(delta_rows, final, root)
    extra.update(synth)
    # 426 S4/S5: with `confine`, the delta admits each row against its own trust
    # profile, in one call, into the base manifest (§9.3 Part 2). The profile
    # map is built from the FOLDED table's trust classes; keys not among the
    # delta roots are inert. An explicit `profiles=` still wins.
    profiles = kwargs.pop("profiles", None)
    if profiles is None and confine:
        profiles = _row_profiles(final, root, trust_host_code)
    document = compile_files(
        [os.path.join(root, p) for p in paths],
        manifest=base_doc, replacing=replacing,
        sources=extra or None, profiles=profiles, **kwargs)
    return _carry_rows(document, final)
