"""The agent-first component registry — phase 0, the read path (roadmap item 49).

The product line (docs/registry.md): *agents import existing components instead
of regenerating them, and the whole loop costs two calls.* Phase 0 builds the
half that lets an agent find something to import — `revl_resolve` — over a
git-backed index. Publish/gauntlet (phase 1) and hosted-index/versioning
(phase 2) are explicitly out of scope here.

Two ideas do all the work, and neither is new:

* **The registry is a repository, not a service.** Components live on disk as
  ``registry/components/<name>/{component.rvl, manifest.json, dossier.json}``
  under a single namespace, first-come names (docs/registry.md §1). A generated
  ``index.json`` records what a resolver needs without opening every source;
  a *stale* index is a CI failure (`verify`), the same regenerate-or-red
  discipline the conformance baselines use.

* **Search is admission, run backwards.** A registry query — "who provides a
  service admissible for this need?" — is the §5 structural-compatibility
  relation (`admission._service_compatible`, re-exported from `lower`) used as
  a *filter*. This is the exact predicate the runtime admission gate calls
  through `refuse_admission -> _admit_service_replacement`; the search-as-
  admission probe (docs/registry-probe.md) proved it, and `plan._interface_drift`
  already drives that same relation over IR projections without raising. This
  module adds an index and a driver on top; it adds **zero** compatibility
  logic. If the §5 check wouldn't admit a candidate, `resolve` doesn't return it.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .compiler import compile_files
from .errors import RevlError
# The real §5 predicate — the same functions `refuse_admission` bottoms out in.
# We import them read-only and add no compatibility logic of our own.
from .lower import _service_compatible, _service_equal, _service_from_ir
from .parser import MethodDecl, ServiceDecl

INDEX_VERSION = "0"          # phase-0 index format; bumped only on a shape change
INDEX_FILENAME = "index.json"


# --------------------------------------------------------------- the index

@dataclass(frozen=True)
class RegistryEntry:
    """One published component, as the resolver sees it.

    `source` and `manifest` ride inline into a resolve result so the loop is
    two round-trips (docs/registry.md §2): the agent never makes a third call
    to fetch what it matched.
    """
    name: str
    source: str                       # component.rvl text, verbatim
    manifest: dict                    # manifest.json (item 28 interchange)
    dossier: dict | None              # dossier.json when the entry has one
    provides: dict                    # {key: ServiceName} across the entry
    requires: dict                    # {key: ServiceName}
    service_shapes: dict              # {ServiceName: shape} for each PROVIDED service
    capabilities: tuple               # sorted emission-capability labels ("*" = unscoped)
    emissions: int                    # how many boundary crossings the entry declares
    source_hash: str
    manifest_hash: str

    @property
    def evidence(self) -> str:
        """Strength of the entry's evidence: a gauntlet dossier outranks an
        audit-only manifest (docs/registry.md §2.3)."""
        return "gauntlet" if self.dossier is not None else "audit"

    @property
    def unbounded(self) -> bool:
        """True when any declared emission is unscoped (`*`) — the widest
        authority a component can reach."""
        return "*" in self.capabilities


# --------------------------------------------------------------- hashing / io

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _capabilities_of(boundary: dict) -> tuple[tuple[str, ...], int]:
    """The union of emission-capability labels a composition reaches, and the
    number of boundary crossings — the raw material for the least-authority
    ranking (docs/registry.md §2.1). `*` (unscoped) is carried through so it can
    rank a candidate last."""
    labels: set[str] = set()
    emissions = 0
    for stats in (boundary or {}).values():
        emissions += len(stats.get("emissions") or [])
        for caps in (stats.get("capabilities") or {}).values():
            labels.update(caps or [])
    return tuple(sorted(labels)), emissions


def _audit_document(ir: dict) -> dict:
    """The item-28 interchange document for a component — byte-identical to
    what `revl audit --json` emits, so an entry's manifest.json is reproducible
    from its source by the current compiler (docs/registry.md §1)."""
    from .__main__ import _boundary            # noqa: PLC0415 — lazy, like plan/mcp
    from .distribute import distributability   # noqa: PLC0415
    from .interchange import stamp             # noqa: PLC0415

    boundary = _boundary(ir)
    manifest = ir.get("manifest") or {}
    # A component's `file` in the manifest is a path relative to the current
    # working directory, so it is cwd-dependent — an entry compiled from two
    # directories would produce two different manifests. Normalize it to the
    # entry's stable filename so manifest.json is byte-reproducible wherever the
    # regenerator runs (the reproducibility invariant, docs/registry.md §1).
    for comp in manifest.get("components") or []:
        if comp.get("file"):
            comp["file"] = os.path.basename(comp["file"])
    return stamp({
        "manifest": manifest,
        "boundary": boundary,
        "externs": [
            {"name": ext["name"], "class": ext.get("class"),
             "backends": sorted((ext.get("bodies") or {}).keys())}
            for ext in ir.get("externs") or []
        ],
        "distributability": distributability(ir),
    })


def _entry_index_row(name: str, ir: dict, source: str, manifest: dict,
                     manifest_text: str) -> dict:
    """What the generated index records for one component — enough to rank and
    shortlist without opening its source."""
    provides: dict = {}
    requires: dict = {}
    for comp in ir.get("components") or []:
        provides.update(comp.get("provides") or {})
        requires.update(comp.get("requires") or {})
    services = ir.get("services") or {}
    shapes = {svc: services.get(svc) for svc in sorted(set(provides.values()))}
    capabilities, emissions = _capabilities_of(manifest.get("boundary") or {})
    return {
        "provides": provides,
        "requires": requires,
        "services": shapes,
        "capabilities": list(capabilities),
        "emissions": emissions,
        "sourceHash": _sha256(source),
        "manifestHash": _sha256(manifest_text),
    }


# --------------------------------------------------------------- build / verify

def _components_dir(registry_dir: str | os.PathLike) -> Path:
    return Path(registry_dir) / "components"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def build_index(registry_dir: str | os.PathLike, *, write: bool = True) -> dict:
    """Regenerate ``index.json`` (and each component's ``manifest.json``) from
    the component sources. This is the CI regenerator — never hand-edit the
    index; run this and commit its output (docs/registry.md §1).

    The manifest.json written here is the item-28 interchange document the
    current compiler produces for the source, so `verify` can catch an entry
    whose manifest no longer matches its own audit surface.
    """
    comps = _components_dir(registry_dir)
    rows: dict = {}
    for entry_dir in sorted(p for p in comps.iterdir() if p.is_dir()):
        name = entry_dir.name
        source_path = entry_dir / "component.rvl"
        source = _read(source_path)
        ir = compile_files([str(source_path)])
        manifest = _audit_document(ir)
        manifest_text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        if write:
            (entry_dir / "manifest.json").write_text(manifest_text, encoding="utf-8")
        rows[name] = _entry_index_row(name, ir, source, manifest, manifest_text)
    index = {"indexVersion": INDEX_VERSION, "components": rows}
    if write:
        index_text = json.dumps(index, indent=2, sort_keys=True) + "\n"
        (Path(registry_dir) / INDEX_FILENAME).write_text(index_text, encoding="utf-8")
    return index


def verify(registry_dir: str | os.PathLike) -> list[str]:
    """Return the reasons the registry is out of date — empty means current.

    Two invariants, both from docs/registry.md §1: the committed ``index.json``
    equals a fresh regeneration ("nothing checked it" must never read as
    "current"), and every entry's ``manifest.json`` is byte-reproducible from
    its ``component.rvl`` by the current compiler (an entry cannot lie about
    its own audit surface). A non-empty result is what turns the CI job red.
    """
    problems: list[str] = []
    registry_dir = Path(registry_dir)
    index_path = registry_dir / INDEX_FILENAME
    if not index_path.exists():
        return [f"{INDEX_FILENAME} is missing — run registry.build_index"]
    committed_index = json.loads(_read(index_path))
    fresh = build_index(registry_dir, write=False)
    if committed_index != fresh:
        problems.append(
            f"{INDEX_FILENAME} is stale — it does not match a fresh regeneration "
            f"from the component sources (run registry.build_index and commit)")
    comps = _components_dir(registry_dir)
    for entry_dir in sorted(p for p in comps.iterdir() if p.is_dir()):
        manifest_path = entry_dir / "manifest.json"
        source_path = entry_dir / "component.rvl"
        if not manifest_path.exists():
            problems.append(f"{entry_dir.name}: manifest.json is missing")
            continue
        ir = compile_files([str(source_path)])
        expected = json.dumps(_audit_document(ir), indent=2, sort_keys=True) + "\n"
        if _read(manifest_path) != expected:
            problems.append(
                f"{entry_dir.name}: manifest.json is not reproducible from "
                f"component.rvl by the current compiler")
    return problems


# --------------------------------------------------------------- the registry

class Registry:
    """A loaded, git-backed component index (docs/registry.md §1).

    Construct with `Registry.from_dir`. Nothing here runs candidate code:
    index rows and sources are data, and a fetched candidate only ever *runs*
    when it passes the admission gate at `revl_admit`/`revl_swap` time
    (docs/registry.md §4).
    """

    def __init__(self, entries: list[RegistryEntry]) -> None:
        self.entries = entries

    @classmethod
    def from_dir(cls, registry_dir: str | os.PathLike) -> "Registry":
        registry_dir = Path(registry_dir)
        index_path = registry_dir / INDEX_FILENAME
        rows = (json.loads(_read(index_path)).get("components") or {}
                if index_path.exists() else {})
        comps = _components_dir(registry_dir)
        entries: list[RegistryEntry] = []
        for name, row in sorted(rows.items()):
            entry_dir = comps / name
            source = _read(entry_dir / "component.rvl")
            manifest = json.loads(_read(entry_dir / "manifest.json"))
            dossier_path = entry_dir / "dossier.json"
            dossier = (json.loads(_read(dossier_path))
                       if dossier_path.exists() else None)
            entries.append(RegistryEntry(
                name=name,
                source=source,
                manifest=manifest,
                dossier=dossier,
                provides=row.get("provides") or {},
                requires=row.get("requires") or {},
                service_shapes=row.get("services") or {},
                capabilities=tuple(row.get("capabilities") or ()),
                emissions=int(row.get("emissions") or 0),
                source_hash=row.get("sourceHash") or _sha256(source),
                manifest_hash=row.get("manifestHash") or "",
            ))
        return cls(entries)

    def resolve(self, need, manifest: dict | None = None,
                limit: int = 5) -> dict:
        return resolve(self, need, manifest=manifest, limit=limit)


# --------------------------------------------------------------- the need

@dataclass(frozen=True)
class _Need:
    """A canonical service shape a candidate is filtered against."""
    label: str            # a human tag for the `why`/diagnostics, never matched on
    decl: ServiceDecl     # the shape; `_service_compatible` ignores the name


def _split_top(text: str, sep: str) -> list[str]:
    """Split on `sep` at bracket depth zero, so `Map[Str, Int]` stays intact."""
    out, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        elif ch == sep and depth == 0:
            out.append(text[start:i])
            start = i + 1
    out.append(text[start:])
    return out


def _method_from_signature(signature: str, emission: bool) -> tuple[str, MethodDecl]:
    """Reconstruct a `MethodDecl` from an item-32 fillSpec signature line,
    e.g. ``query(sql: Str) -> List[Row]`` — read-only on fillspec, its inverse.
    """
    name, rest = signature.split("(", 1)
    params_str, after = rest.split(")", 1)
    params: list[tuple[str, str | None]] = []
    for part in _split_top(params_str, ","):
        part = part.strip()
        if not part:
            continue
        pname, _, ptype = part.partition(":")
        params.append((pname.strip(), ptype.strip() or None))
    returns = None
    if "->" in after:
        returns = after.split("->", 1)[1].strip() or None
    return name.strip(), MethodDecl(name.strip(), params, returns,
                                    bool(emission), 0)


def _needs_from_fillspec(spec: dict) -> list[_Need]:
    """A hole's fill spec (item 32) as a set of service needs.

    A hole is a well-formed query by construction (docs/registry.md §2): its
    ``reachableServices`` name the injected dependencies the fill must work
    through, each with a full signature and emission flag. Every distinct
    service becomes a need — "who could provide the dependency this hole calls?"
    — reconstructed verbatim from the fillSpec, adding no shape the checker did
    not already publish.
    """
    reachable = spec.get("reachableServices") or []
    by_service: dict[str, dict] = {}
    for item in reachable:
        svc = item.get("service")
        methods = by_service.setdefault(svc, {})
        mname, decl = _method_from_signature(item.get("signature", ""),
                                             item.get("emission"))
        methods[mname] = decl
    return [_Need(f"fillSpec:{svc}", ServiceDecl(svc, methods, 0))
            for svc, methods in by_service.items()]


def _needs(need) -> list[_Need]:
    """Reduce a `need` to one or more canonical service shapes.

    Three forms, all collapsing to a `ServiceDecl` shape (docs/registry.md §2):
    a revl ``service`` declaration as source; a hole's fill spec object
    (item 32) verbatim; or the shape object the index itself stores.
    """
    if isinstance(need, str):
        from .compiler import compile_source  # noqa: PLC0415
        doc = compile_source(need, "<need>.rvl")
        services = doc.get("services") or {}
        if len(services) != 1:
            raise RevlError("<need>.rvl", 1,
                            f"a service need must declare exactly one service, "
                            f"found {len(services)}",
                            hint="pass a single `service` declaration, a fill "
                                 "spec, or a shape object")
        (name, spec), = services.items()
        return [_Need(name, _service_from_ir(name, spec))]
    if isinstance(need, dict):
        if "fillSpec" in need:                 # a whole obligation was passed
            return _needs_from_fillspec(need["fillSpec"])
        if "reachableServices" in need:        # the fillSpec itself
            return _needs_from_fillspec(need)
        if "methods" in need:                  # the shape object the index stores
            name = need.get("service") or need.get("name") or "Need"
            return [_Need(name, _service_from_ir(name, need))]
    raise RevlError("<need>", 1,
                    "unrecognized need — expected a `service` declaration "
                    "(source), a fill spec, or a service shape object")


# --------------------------------------------------------------- resolve

@dataclass
class _Match:
    entry: RegistryEntry
    key: str                 # the provision key the candidate offers
    service: str             # the provided service name that matched
    need_label: str
    exact: bool              # identical interface vs a compatible widening

    def why(self) -> str:
        fit = "exact interface match" if self.exact \
            else "compatible superset (adds/widens, breaks no call site)"
        return (f"provides `{self.service}` as `{self.key}`: §5-compatible with "
                f"the need ({fit})")

    def rank(self) -> tuple:
        # docs/registry.md §2 ranking, least authority first:
        #   1. smallest declared capability set (unscoped `*` ranks last)
        #   2. tighter interface fit (exact over compatible-with-widening)
        #   3. stronger evidence (a gauntlet dossier over audit-only)
        #   4. smaller source (less for the agent to hold in context)
        e = self.entry
        return (
            1 if e.unbounded else 0,
            len([c for c in e.capabilities if c != "*"]),
            e.emissions,
            0 if self.exact else 1,
            0 if e.evidence == "gauntlet" else 1,
            len(e.source),
            e.name,
        )


def _manifest_provided_keys(manifest: dict | None) -> set[str]:
    """Provision keys the running composition already fills (for the G2 check).

    Accepts either a full IR document or a bare manifest projection: a
    component's `provides` is a bare key list in the manifest projection and a
    {key: service} map on the full document."""
    if not manifest:
        return set()
    running = manifest.get("manifest", manifest)
    keys: set[str] = set()
    for comp in running.get("components") or []:
        prov = comp.get("provides")
        if isinstance(prov, dict):
            keys.update(prov.keys())
        elif isinstance(prov, list):
            keys.update(prov)
    return keys


def _match_entry(entry: RegistryEntry, needs: list[_Need]) -> _Match | None:
    """Would this entry admit for any need? Returns the tightest match, or None.

    The predicate is `_service_compatible(new=provided, old=need,
    providers_retained=False)` — the consumer-subtype regime, because a resolve
    introduces a *fresh* provider (nothing retained provides the key yet), which
    is exactly the hot-swap regime the probe exercised. A returned `None` from
    the gate means admissible; a `_Drift` means it would break a call site
    type-checked against the need, so the candidate is dropped.
    """
    best: _Match | None = None
    for key, service in (entry.provides or {}).items():
        shape = entry.service_shapes.get(service)
        if not shape:
            continue
        provided = _service_from_ir(service, shape)
        for need in needs:
            if _service_compatible(provided, need.decl,
                                   providers_retained=False) is not None:
                continue  # the §5 gate would refuse this candidate for this need
            exact = _service_equal(provided, need.decl)
            match = _Match(entry, key, service, need.label, exact)
            if best is None or (exact and not best.exact):
                best = match
    return best


_ASSUMPTIONS = [
    "index generated from the committed sources; entries added since are not seen",
    "extern classifications inside candidates are trusted, not verified (G8)",
]


def resolve(registry, need, manifest: dict | None = None,
            limit: int = 5) -> dict:
    """Rank the registry's §5-admissible providers for a need (docs/registry.md §2).

    `registry` is a `Registry` or a directory path. `need` is a service
    declaration source, a fill spec, or a shape object. `manifest`, when given,
    upgrades the answer from "compatible somewhere" to "admissible *here*": a
    candidate whose key the running composition already provides is dropped
    (G2 forbids two providers of one key). `source` and `manifest` ride inline
    on every candidate so the loop stays two round-trips.
    """
    if not isinstance(registry, Registry):
        registry = Registry.from_dir(registry)
    needs = _needs(need)
    taken = _manifest_provided_keys(manifest)

    matches: list[_Match] = []
    for entry in registry.entries:
        match = _match_entry(entry, needs)
        if match is None:
            continue
        if match.key in taken:
            # admissible somewhere, but not *here*: the running composition
            # already provides this key, and G2 forbids a second provider.
            continue
        matches.append(match)
    matches.sort(key=_Match.rank)

    assumptions = list(_ASSUMPTIONS)
    if manifest:
        assumptions.append(
            "candidates are additionally admissible against the supplied "
            "manifest: a key the composition already provides is withheld (G2)")

    candidates = []
    for match in matches[:max(0, limit)]:
        entry = match.entry
        candidate = {
            "name": entry.name,
            "source": entry.source,
            "manifest": entry.manifest,
            "why": match.why(),
        }
        if entry.dossier is not None:
            candidate["dossier"] = entry.dossier
        candidates.append(candidate)

    return {
        "ok": True,
        "query": "resolve",
        "question": "who provides a service admissible for this need?",
        "precision": "exact",
        "assumptions": assumptions,
        "candidates": candidates,
    }


__all__ = ["Registry", "RegistryEntry", "build_index", "verify", "resolve",
           "INDEX_VERSION"]
