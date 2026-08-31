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

# The evidence bundle a component carries alongside its source + manifest
# (roadmap item 293). Each file is the verbatim output of an existing producer,
# assembled - never re-implemented - at publish time:
#
#   attestation.json       revl.attest.make_attestation  (item 127)
#   gauntlet.json          mcp.gauntlet.run dossier       (root dossier.json is
#                          read as this facet when evidence/gauntlet.json absent)
#   fault-sweep.json       fault.sweep_dossier            (item 30/125)
#   inverse-roundtrip.json fault.roundtrip_dossier        (item 26)
#   capabilities.json      __main__._boundary G8 surface  (docs/capabilities.md)
#   provenance.json        assembled source/build provenance (the reproducible
#                          hashes the index records + the interchange toolchain)
#
# A missing file is `unavailable` (ranked below present-and-valid), never read
# as valid; a tampered attestation is `invalid` (ranked at the bottom, or
# filtered when a resolve runs verify-required).
EVIDENCE_DIRNAME = "evidence"
EVIDENCE_ATTESTATION = "attestation.json"
EVIDENCE_GAUNTLET = "gauntlet.json"
EVIDENCE_FAULT_SWEEP = "fault-sweep.json"
EVIDENCE_INVERSE_ROUNDTRIP = "inverse-roundtrip.json"
EVIDENCE_CAPABILITIES = "capabilities.json"
EVIDENCE_PROVENANCE = "provenance.json"
EVIDENCE_FILES = (
    EVIDENCE_ATTESTATION, EVIDENCE_GAUNTLET, EVIDENCE_FAULT_SWEEP,
    EVIDENCE_INVERSE_ROUNDTRIP, EVIDENCE_CAPABILITIES, EVIDENCE_PROVENANCE,
)


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
    evidence_bundle: "EvidenceBundle" = None  # the item-293 evidence bundle

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


# --------------------------------------------------------------- evidence bundle

# Per-facet quality rank: 0 sorts first (best). A missing facet is `unavailable`
# - always below any present-and-valid one, never silently valid; a tampered
# attestation is `invalid`, the single worst rank a facet can carry.
_ATTESTATION_RANK = {"valid": 0, "present": 1, "unavailable": 2, "invalid": 3}
_SWEEP_RANK = {"full": 0, "partial": 1, "unavailable": 2}
_INVERSE_RANK = {"pass": 0, "fail": 1, "unavailable": 2}
_GAUNTLET_RANK = {"admissible": 0, "present": 1, "unavailable": 2}
_PUBLISHER_RANK = {"trusted": 0, "present": 1, "unavailable": 2}


@dataclass(frozen=True)
class EvidenceBundle:
    """The machine-verifiable evidence a registry component carries (item 293).

    Every member is the verbatim JSON a producer already emits (or None when the
    component published none of that facet). Nothing here re-derives evidence:
    the bundle is loaded as data, and its quality is *assessed* - how strong the
    present evidence is - only when a resolve ranks candidates.
    """
    attestation: dict | None = None
    gauntlet: dict | None = None
    fault_sweep: dict | None = None
    inverse_roundtrip: dict | None = None
    capabilities: dict | None = None
    provenance: dict | None = None

    def present(self) -> tuple[str, ...]:
        """The facet names this bundle actually carries, for diagnostics."""
        names = []
        for name, value in (
            ("attestation", self.attestation), ("gauntlet", self.gauntlet),
            ("fault-sweep", self.fault_sweep),
            ("inverse-roundtrip", self.inverse_roundtrip),
            ("capabilities", self.capabilities), ("provenance", self.provenance),
        ):
            if value is not None:
                names.append(name)
        return tuple(names)


@dataclass(frozen=True)
class EvidenceAssessment:
    """The graded quality of one component's evidence, as a resolve sees it.

    `rank_key` sorts better evidence first (lower is better); `facets` is the
    human/agent-readable status of each facet, surfaced in the resolve result so
    the chosen candidate can say *why* it won.
    """
    facets: dict          # {facet: status}
    rank_key: tuple       # deterministic, lower sorts first
    sweep_coverage: tuple | None   # (passed, steps) when a fault sweep is present

    def summary(self) -> str:
        """A compact one-line evidence summary, e.g.
        `fault sweep 12/12, attestation valid, gauntlet admissible`."""
        parts: list[str] = []
        if self.sweep_coverage is not None:
            parts.append(f"fault sweep {self.sweep_coverage[0]}/"
                         f"{self.sweep_coverage[1]}")
        elif self.facets.get("fault-sweep") == "unavailable":
            parts.append("fault sweep unavailable")
        att = self.facets.get("attestation")
        if att and att != "unavailable":
            parts.append(f"attestation {att}")
        if self.facets.get("inverse-roundtrip") == "pass":
            parts.append("inverse round-trip pass")
        if self.facets.get("gauntlet") == "admissible":
            parts.append("gauntlet admissible")
        if self.facets.get("publisher") == "trusted":
            parts.append("trusted publisher")
        return ", ".join(parts) if parts else "no evidence"


def _sweep_status(dossier: dict | None) -> tuple[str, tuple | None]:
    """Grade a fault-sweep dossier (fault.sweep_dossier shape). `full` means the
    sweep ran and every swept step passed (e.g. 12/12); a partial or failed
    sweep is `partial`; absent is `unavailable`. The (passed, steps) coverage
    is carried through for the finer tiebreak and the `why`."""
    if not isinstance(dossier, dict):
        return "unavailable", None
    counts = dossier.get("counts") or {}
    steps = int(counts.get("steps") or 0)
    passed = int(counts.get("passed") or 0)
    coverage = (passed, steps)
    if dossier.get("status") == "passed" and steps > 0 and passed == steps:
        return "full", coverage
    if dossier.get("status") == "passed" and steps == 0:
        # a `passed` dossier with nothing to sweep is honest but weightless:
        # present, but not the full-coverage tier.
        return "partial", coverage
    return "partial", coverage


def _inverse_status(dossier: dict | None) -> str:
    """Grade an inverse-roundtrip dossier (fault.roundtrip_dossier shape)."""
    if not isinstance(dossier, dict):
        return "unavailable"
    return "pass" if dossier.get("status") == "passed" else "fail"


def _gauntlet_status(dossier: dict | None) -> str:
    """Grade a gauntlet dossier (mcp.gauntlet.run shape)."""
    if not isinstance(dossier, dict):
        return "unavailable"
    return "admissible" if dossier.get("verdict") == "admissible" else "present"


def _attestation_status(att: dict | None, *, key: bytes | None,
                        ir: dict | None) -> str:
    """Grade an attestation (revl.attest shape).

    Without a key an attestation can only be checked for *well-formedness*
    (`present`); with a key it is cryptographically verified against the rebuilt
    IR and is `valid` or `invalid`. A malformed record is `invalid`, never
    `present` - a resolve must not read a broken attestation as merely
    unverified."""
    if att is None:
        return "unavailable"
    if not isinstance(att, dict) or "signature" not in att \
            or "composition_hash" not in att:
        return "invalid"
    if key is not None:
        from .attest import verify_attestation  # noqa: PLC0415
        ok, _ = verify_attestation(att, key, ir)
        return "valid" if ok else "invalid"
    return "present"


def _publisher_status(provenance: dict | None,
                      trusted_publishers: frozenset) -> str:
    """Grade the provenance's publisher against the caller's trust set. Trust is
    supplied by the resolve, never self-asserted by the component."""
    if not isinstance(provenance, dict):
        return "unavailable"
    publisher = provenance.get("publisher")
    if publisher and publisher in trusted_publishers:
        return "trusted"
    return "present"


def assess_evidence(bundle: EvidenceBundle, *, key: bytes | None = None,
                    ir: dict | None = None,
                    trusted_publishers: frozenset = frozenset()
                    ) -> EvidenceAssessment:
    """Grade an evidence bundle into a deterministic ranking key + facet report.

    The ordering the key encodes, strongest signal first (docs/registry.md §2.3,
    item 293): fault-sweep coverage, a valid attestation, a trusted publisher,
    an inverse-roundtrip pass, a gauntlet admission - then finer sweep coverage.
    Interface compatibility is decided elsewhere and stays a hard filter; this
    only ranks the already-compatible set.
    """
    sweep, coverage = _sweep_status(bundle.fault_sweep)
    attestation = _attestation_status(bundle.attestation, key=key, ir=ir)
    inverse = _inverse_status(bundle.inverse_roundtrip)
    gauntlet = _gauntlet_status(bundle.gauntlet)
    publisher = _publisher_status(bundle.provenance, trusted_publishers)
    facets = {
        "fault-sweep": sweep,
        "attestation": attestation,
        "inverse-roundtrip": inverse,
        "gauntlet": gauntlet,
        "publisher": publisher,
        "capabilities": "present" if bundle.capabilities is not None
                        else "unavailable",
    }
    passed = coverage[0] if coverage else 0
    rank_key = (
        _SWEEP_RANK[sweep],
        _ATTESTATION_RANK[attestation],
        _PUBLISHER_RANK[publisher],
        _INVERSE_RANK[inverse],
        _GAUNTLET_RANK[gauntlet],
        -passed,                       # more swept steps proven is stronger
    )
    return EvidenceAssessment(
        facets=facets, rank_key=rank_key,
        sweep_coverage=coverage if sweep in ("full", "partial")
        and coverage and coverage[1] > 0 else None)


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


def _read_json(path: Path) -> dict | None:
    """Load a JSON evidence file, or None when it is absent - a missing file is
    `unavailable`, the honest floor, never a fabricated pass."""
    if not path.exists():
        return None
    try:
        return json.loads(_read(path))
    except (json.JSONDecodeError, OSError):
        return None


def load_evidence_bundle(entry_dir: str | os.PathLike) -> EvidenceBundle:
    """Load a component's evidence bundle from ``<entry_dir>/evidence/`` (item
    293). A missing bundle, or a missing facet within it, loads as None and
    grades `unavailable`. For backward compatibility a root ``dossier.json``
    (the gauntlet output earlier entries carried) is read as the gauntlet facet
    when ``evidence/gauntlet.json`` is absent."""
    entry_dir = Path(entry_dir)
    ev = entry_dir / EVIDENCE_DIRNAME
    gauntlet = _read_json(ev / EVIDENCE_GAUNTLET)
    if gauntlet is None:
        gauntlet = _read_json(entry_dir / "dossier.json")
    return EvidenceBundle(
        attestation=_read_json(ev / EVIDENCE_ATTESTATION),
        gauntlet=gauntlet,
        fault_sweep=_read_json(ev / EVIDENCE_FAULT_SWEEP),
        inverse_roundtrip=_read_json(ev / EVIDENCE_INVERSE_ROUNDTRIP),
        capabilities=_read_json(ev / EVIDENCE_CAPABILITIES),
        provenance=_read_json(ev / EVIDENCE_PROVENANCE),
    )


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


# --------------------------------------------------------------- evidence publish

def _write_evidence_file(entry_dir: Path, filename: str, doc: dict,
                         *, write: bool) -> None:
    text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    if write:
        ev = entry_dir / EVIDENCE_DIRNAME
        ev.mkdir(exist_ok=True)
        (ev / filename).write_text(text, encoding="utf-8")


def _provenance_document(source: str, manifest: dict, manifest_text: str,
                         publisher: str | None) -> dict:
    """The source/build provenance for a component - assembled from the
    reproducible facts the registry and interchange already stamp (the same
    `sourceHash` the index records, the manifest hash, and the interchange
    toolchain version), not a novel producer. A consumer can recompute every
    field from the component's own source."""
    from .interchange import INTERCHANGE_VERSION  # noqa: PLC0415

    doc = {
        "kind": "revl.provenance",
        "sourceSha256": _sha256(source),
        "manifestSha256": _sha256(manifest_text),
        "schemaVersion": manifest.get("schema_version"),
        "toolchain": {"interchange": INTERCHANGE_VERSION},
    }
    if publisher:
        doc["publisher"] = publisher
    return doc


def build_evidence(registry_dir: str | os.PathLike, *, key: bytes | None = None,
                   signer: str | None = None, now=None,
                   publisher: str | None = None, write: bool = True) -> dict:
    """Assemble each component's evidence bundle (roadmap item 293) - the second
    half of the publish path, run after `build_index` has written the manifests.

    Nothing here re-implements evidence: it calls the existing producers and
    writes their verbatim output under ``<component>/evidence/``. `capabilities`
    (the G8 boundary) and `provenance` are always reproducible from source and
    are always written; `attestation` is written when a signing `key` is given;
    `fault-sweep` and `inverse-roundtrip` are the runtime-tested facets - written
    when the cordis-py runtime is present, and honestly skipped (left
    `unavailable`) when it is absent, never faked. Returns a per-component map of
    the facets that were assembled.
    """
    from .__main__ import _boundary  # noqa: PLC0415

    comps = _components_dir(registry_dir)
    produced: dict = {}
    for entry_dir in sorted(p for p in comps.iterdir() if p.is_dir()):
        name = entry_dir.name
        source_path = entry_dir / "component.rvl"
        source = _read(source_path)
        ir = compile_files([str(source_path)])
        manifest = _audit_document(ir)
        manifest_text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        facets: list[str] = []

        # capabilities - the G8 boundary surface, reproducible with no runtime.
        _write_evidence_file(entry_dir, EVIDENCE_CAPABILITIES,
                             {"kind": "revl.capabilities",
                              "boundary": _boundary(ir)}, write=write)
        facets.append("capabilities")

        # provenance - reproducible source/build facts.
        _write_evidence_file(
            entry_dir, EVIDENCE_PROVENANCE,
            _provenance_document(source, manifest, manifest_text, publisher),
            write=write)
        facets.append("provenance")

        # attestation - a signed admission record, when a key is supplied.
        if key is not None:
            from .attest import make_attestation  # noqa: PLC0415
            att = make_attestation(_normalize_ir_for_attest(ir), bytes(key),
                                   now=now, signer=signer)
            _write_evidence_file(entry_dir, EVIDENCE_ATTESTATION, att,
                                 write=write)
            facets.append("attestation")

        # fault-sweep - the runtime-tested no-residue sweep (item 30/125).
        from . import fault  # noqa: PLC0415
        try:
            _write_evidence_file(entry_dir, EVIDENCE_FAULT_SWEEP,
                                 fault.sweep_dossier(ir), write=write)
            facets.append("fault-sweep")
        except (ModuleNotFoundError, ImportError):
            pass  # cordis-py absent: honestly unavailable, never faked

        # inverse-roundtrip - verified-effect reversibility (item 26).
        try:
            _write_evidence_file(entry_dir, EVIDENCE_INVERSE_ROUNDTRIP,
                                 fault.roundtrip_dossier(ir), write=write)
            facets.append("inverse-roundtrip")
        except (ModuleNotFoundError, ImportError):
            pass

        produced[name] = facets
    return produced


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
                evidence_bundle=load_evidence_bundle(entry_dir),
            ))
        return cls(entries)

    def resolve(self, need, manifest: dict | None = None,
                limit: int = 5, *, verify_required: bool = False,
                key: bytes | None = None, trusted_publishers=()) -> dict:
        return resolve(self, need, manifest=manifest, limit=limit,
                       verify_required=verify_required, key=key,
                       trusted_publishers=trusted_publishers)


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

    def authority_fit_key(self) -> tuple:
        # docs/registry.md §2 ranking, least authority first - the part that
        # sits ABOVE evidence quality:
        #   1. smallest declared capability set (unscoped `*` ranks last)
        #   2. tighter interface fit (exact over compatible-with-widening)
        # Evidence quality (item 293) breaks the tie among candidates equal here;
        # interface compatibility itself is a hard filter, decided in
        # `_match_entry`, and never a ranking term.
        e = self.entry
        return (
            1 if e.unbounded else 0,
            len([c for c in e.capabilities if c != "*"]),
            e.emissions,
            0 if self.exact else 1,
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


def _normalize_ir_for_attest(ir: dict) -> dict:
    """The IR spelling an attestation binds - each component's `file` reduced to
    its basename, the same normalization `_audit_document` and `truc reproduce`
    apply so an attestation verifies regardless of the path the entry compiled
    from (mirrors reproduce._normalized_ir)."""
    import copy  # noqa: PLC0415

    out = copy.deepcopy(ir)
    for comp in (out.get("manifest") or {}).get("components") or []:
        if comp.get("file"):
            comp["file"] = os.path.basename(comp["file"])
    # The compiled IR also stamps each component with its `source` path, which is
    # cwd-dependent (a full path under `compile_files`, a bare name under
    # `compile_source`). Basename it too so the attested composition hash is a
    # pure function of the source, not of where the entry was compiled.
    for comp in out.get("components") or []:
        if comp.get("source"):
            comp["source"] = os.path.basename(comp["source"])
    return out


def _assess_match(match: "_Match", *, key: bytes | None,
                  trusted_publishers: frozenset,
                  ir_cache: dict) -> EvidenceAssessment:
    """Grade one candidate's evidence for ranking. When a key is supplied, the
    candidate source is compiled once (cached) so the attestation can be
    cryptographically verified against the rebuilt IR - the same honest check
    `truc reproduce` runs, not a trust of the record's own say-so."""
    entry = match.entry
    bundle = entry.evidence_bundle or EvidenceBundle()
    ir = None
    if key is not None and bundle.attestation is not None:
        ir = ir_cache.get(entry.name)
        if ir is None:
            from .compiler import compile_source  # noqa: PLC0415
            try:
                # compile under the stable published filename so the audit-
                # normalized `file` basename (and thus the attested composition
                # hash) matches what `build_evidence` signed from component.rvl.
                ir = _normalize_ir_for_attest(
                    compile_source(entry.source, "component.rvl"))
            except RevlError:
                ir = {}
            ir_cache[entry.name] = ir
    return assess_evidence(bundle, key=key, ir=ir,
                           trusted_publishers=trusted_publishers)


def resolve(registry, need, manifest: dict | None = None,
            limit: int = 5, *, verify_required: bool = False,
            key: bytes | None = None, trusted_publishers=()) -> dict:
    """Rank the registry's §5-admissible providers for a need (docs/registry.md §2).

    `registry` is a `Registry` or a directory path. `need` is a service
    declaration source, a fill spec, or a shape object. `manifest`, when given,
    upgrades the answer from "compatible somewhere" to "admissible *here*": a
    candidate whose key the running composition already provides is dropped
    (G2 forbids two providers of one key). `source` and `manifest` ride inline
    on every candidate so the loop stays two round-trips.

    Among the interface-compatible candidates the ranking is, in order: least
    authority, tightest interface fit, then **evidence quality** (item 293) -
    fault-sweep coverage, a valid attestation, a trusted publisher, an
    inverse-roundtrip pass. Compatibility is a hard filter (an incompatible
    candidate is never returned); evidence only ranks the compatible set, and
    the chosen candidate's evidence is spelled out in its `why`.

    `verify_required=True` (with `key`) turns the evidence check into a gate: a
    candidate without a cryptographically **valid** attestation is filtered, and
    a tampered attestation - which grades `invalid` - never survives. Without
    `verify_required` a missing or unverifiable attestation only ranks a
    candidate lower; it is never silently treated as valid.
    """
    if not isinstance(registry, Registry):
        registry = Registry.from_dir(registry)
    needs = _needs(need)
    taken = _manifest_provided_keys(manifest)
    key_bytes = bytes(key) if key is not None else None
    trusted = frozenset(trusted_publishers or ())
    if verify_required and key_bytes is None:
        raise RevlError(
            "<resolve>", 0,
            "verify_required resolve needs a signing key to verify attestations "
            "(pass key=...)")

    ir_cache: dict = {}
    graded: list[tuple[_Match, EvidenceAssessment]] = []
    for entry in registry.entries:
        match = _match_entry(entry, needs)
        if match is None:
            continue  # interface-incompatible: the hard filter, never ranked in
        if match.key in taken:
            # admissible somewhere, but not *here*: the running composition
            # already provides this key, and G2 forbids a second provider.
            continue
        assessment = _assess_match(match, key=key_bytes,
                                   trusted_publishers=trusted, ir_cache=ir_cache)
        if verify_required and assessment.facets.get("attestation") != "valid":
            # verify-required: only a cryptographically valid attestation admits.
            continue
        graded.append((match, assessment))

    # least authority, then fit, then evidence quality, then a stable tiebreak.
    graded.sort(key=lambda pair: (
        pair[0].authority_fit_key(),
        pair[1].rank_key,
        len(pair[0].entry.source),
        pair[0].entry.name,
    ))

    assumptions = list(_ASSUMPTIONS)
    assumptions.append(
        "among interface-compatible candidates the ranking is by evidence "
        "quality (fault-sweep coverage, valid attestation, trusted publisher, "
        "inverse-roundtrip pass); a missing facet is unavailable, never valid")
    if manifest:
        assumptions.append(
            "candidates are additionally admissible against the supplied "
            "manifest: a key the composition already provides is withheld (G2)")
    if verify_required:
        assumptions.append(
            "verify-required: a candidate without a cryptographically valid "
            "attestation was filtered, not merely ranked lower")

    candidates = []
    for match, assessment in graded[:max(0, limit)]:
        entry = match.entry
        candidate = {
            "name": entry.name,
            "source": entry.source,
            "manifest": entry.manifest,
            "why": f"{match.why()}; evidence: {assessment.summary()}",
            "evidence": {
                "summary": assessment.summary(),
                "facets": assessment.facets,
                "present": list((entry.evidence_bundle or EvidenceBundle())
                                .present()),
            },
        }
        if assessment.sweep_coverage is not None:
            candidate["evidence"]["faultSweepCoverage"] = list(
                assessment.sweep_coverage)
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


__all__ = ["Registry", "RegistryEntry", "EvidenceBundle", "EvidenceAssessment",
           "build_index", "build_evidence", "verify", "resolve",
           "load_evidence_bundle", "assess_evidence", "INDEX_VERSION",
           "EVIDENCE_DIRNAME", "EVIDENCE_FILES"]
