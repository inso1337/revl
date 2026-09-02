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
from dataclasses import dataclass, field
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
    source_hash: str                  # sha256 of `source` as it is ON DISK
    manifest_hash: str
    evidence_bundle: "EvidenceBundle" = None  # the item-293 evidence bundle
    # What `index.json` CLAIMED for this entry, and every place that claim
    # disagreed with the entry's own `component.rvl` when it was recompiled at
    # load time. A non-empty tuple means the index is lying about (or is stale
    # for) this entry; `resolve` refuses such an entry rather than ranking it.
    recorded_source_hash: str = ""
    index_problems: tuple = ()

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
#
# The rank an *unverified* claim carries is the same rank as no claim at all
# (`_UNRANKED`), never better. Every dossier in a registry entry is a file the
# PUBLISHER wrote: on its own it is an assertion, not evidence. If merely
# attaching one lifted a candidate, the cheapest way to rank first would be to
# fabricate the strongest bundle in the registry - so a positive grade only buys
# rank when something outside the publisher stands behind it (see
# `_vouched_facets`: a cryptographically valid attestation binding the dossier's
# hash, or, for the publisher facet, the CALLER's own trust set). An unverified
# claim is still reported honestly - it is simply weightless.
_UNRANKED = 2
_ATTESTATION_RANK = {"valid": 0, "present": _UNRANKED,
                     "unavailable": _UNRANKED, "invalid": 3}
_SWEEP_RANK = {"full": 0, "partial": 1, "unavailable": _UNRANKED}
_INVERSE_RANK = {"pass": 0, "fail": 1, "unavailable": _UNRANKED}
_GAUNTLET_RANK = {"admissible": 0, "present": 1, "unavailable": _UNRANKED}
_PUBLISHER_RANK = {"trusted": 0, "present": _UNRANKED,
                   "unavailable": _UNRANKED}

# The facets whose positive grade is only worth rank when a valid attestation
# binds the dossier (item 290, §6.2). Mapped to the status an unvouched claim is
# RANKED as - the reported status stays the honest grade.
_VOUCH_GATED = ("fault-sweep", "inverse-roundtrip", "gauntlet")


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
    the chosen candidate can say *why* it won. `verified` says, per facet,
    whether anything outside the publisher stood behind that status - the
    summary marks an unverified claim as such, so the `why` an agent reads never
    asserts a self-written file as established fact.
    """
    facets: dict          # {facet: status}
    rank_key: tuple       # deterministic, lower sorts first
    sweep_coverage: tuple | None   # (passed, steps) when a fault sweep is present
    verified: dict = field(default_factory=dict)   # {facet: bool}

    def _mark(self, facet: str, text: str) -> str:
        """`text` as-is when the facet is independently verified, tagged
        `(self-reported, unverified)` when it is the publisher's own say-so."""
        return text if self.verified.get(facet) else f"{text} (self-reported, unverified)"

    def summary(self) -> str:
        """A compact one-line evidence summary, e.g.
        `fault sweep 12/12, attestation valid, gauntlet admissible`."""
        parts: list[str] = []
        if self.sweep_coverage is not None:
            parts.append(self._mark(
                "fault-sweep",
                f"fault sweep {self.sweep_coverage[0]}/{self.sweep_coverage[1]}"))
        elif self.facets.get("fault-sweep") == "unavailable":
            parts.append("fault sweep unavailable")
        att = self.facets.get("attestation")
        if att and att != "unavailable":
            parts.append(self._mark("attestation", f"attestation {att}"))
        if self.facets.get("inverse-roundtrip") == "pass":
            parts.append(self._mark("inverse-roundtrip", "inverse round-trip pass"))
        if self.facets.get("gauntlet") == "admissible":
            parts.append(self._mark("gauntlet", "gauntlet admissible"))
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
                        ir: dict | None,
                        bundle: "EvidenceBundle | None" = None) -> str:
    """Grade an attestation (revl.attest shape).

    Without a key an attestation can only be checked for *well-formedness*
    (`present`); with a key it is cryptographically verified against the rebuilt
    IR and is `valid` or `invalid`. A malformed record is `invalid`, never
    `present` - a resolve must not read a broken attestation as merely
    unverified.

    When the attestation carries per-facet dossier bindings (item 290, §6.2), a
    `valid` grade additionally requires every bound dossier in `bundle` to hash
    to its signed value: a forged or copied dossier riding an honest signature
    grades the whole attestation `invalid`, so it can never vouch for evidence
    it does not cover. An attestation with no bindings is unaffected (the
    dossiers it does not cover simply stay self-attested)."""
    if att is None:
        return "unavailable"
    if not isinstance(att, dict) or "signature" not in att \
            or "composition_hash" not in att:
        return "invalid"
    if key is not None:
        from .attest import verify_attestation  # noqa: PLC0415
        ok, _ = verify_attestation(att, key, ir)
        if not ok:
            return "invalid"
        if bundle is not None and binding_mismatch(att, bundle) is not None:
            return "invalid"
        return "valid"
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


def _vouched_facets(bundle: EvidenceBundle, attestation_status: str) -> frozenset:
    """The facets something OUTSIDE the publisher stands behind.

    A dossier in a registry entry is a file its publisher wrote; reading it as
    proof is reading the claim as the check. The one thing in the bundle that
    binds a dossier to an independent verification is a cryptographically
    **valid** attestation carrying that dossier's hash in its signed payload
    (item 290, §6.2) - and `_attestation_status` already grades an attestation
    `invalid` when any binding fails, so a `valid` grade means every bound
    dossier is byte-for-byte what was signed. Anything else - a bundle with no
    attestation, an attestation nobody had a key to check, an attestation that
    binds nothing - vouches for nothing, and ranks accordingly.
    """
    if attestation_status != "valid":
        return frozenset()
    att = bundle.attestation if isinstance(bundle.attestation, dict) else {}
    bindings = att.get("evidence_bindings")
    if not isinstance(bindings, dict):
        return frozenset()
    return frozenset(bindings) & frozenset(_BOUND_FACETS)


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

    A facet's positive grade only earns rank when it is *verified* - bound by a
    valid attestation, or (for the publisher facet) named in the caller's own
    trust set. An unverified claim is reported at its honest grade and ranked at
    `_UNRANKED`, exactly level with having published nothing: fabricating
    evidence can never lift a candidate above an honest one.
    """
    sweep, coverage = _sweep_status(bundle.fault_sweep)
    attestation = _attestation_status(bundle.attestation, key=key, ir=ir,
                                      bundle=bundle)
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
    vouched = _vouched_facets(bundle, attestation)
    verified = {facet: facet in vouched for facet in _BOUND_FACETS}
    # a malformed attestation is `invalid` on inspection alone; `valid`/`invalid`
    # with a key is a real cryptographic verdict. `present` is the one that was
    # never checked.
    verified["attestation"] = attestation in ("valid", "invalid")
    # trust in a publisher is supplied by the resolve, never self-asserted.
    verified["publisher"] = publisher == "trusted"

    # the ranked spelling of each vouch-gated facet: unverified reads as
    # `unavailable` for ranking only - the reported status above is untouched.
    ranked = {f: (facets[f] if verified.get(f) else "unavailable")
              for f in _VOUCH_GATED}
    passed = coverage[0] if (coverage and verified.get("fault-sweep")) else 0
    rank_key = (
        _SWEEP_RANK[ranked["fault-sweep"]],
        _ATTESTATION_RANK[attestation],
        _PUBLISHER_RANK[publisher],
        _INVERSE_RANK[ranked["inverse-roundtrip"]],
        _GAUNTLET_RANK[ranked["gauntlet"]],
        -passed,                       # more swept steps proven is stronger
    )
    return EvidenceAssessment(
        facets=facets, rank_key=rank_key,
        sweep_coverage=coverage if sweep in ("full", "partial")
        and coverage and coverage[1] > 0 else None,
        verified=verified)


# --------------------------------------------------------------- hashing / io

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# The facets whose dossiers an attestation binds (item 290, §6.2): the runtime-
# tested and enumerated evidence a `attestation valid` clause can root. Keyed by
# facet name (the same names `assess_evidence` reports), mapped to the bundle
# attribute the dossier is loaded into.
_BOUND_FACETS = {
    "fault-sweep": "fault_sweep",
    "inverse-roundtrip": "inverse_roundtrip",
    "gauntlet": "gauntlet",
    "capabilities": "capabilities",
}


def _facet_hash(doc) -> str:
    """The content hash of one evidence dossier, folded into the signed
    attestation payload (item 290, §6.2). Canonical (sorted keys, compact
    separators) so the hash is a pure function of the dossier's data, never of
    its on-disk formatting — the publish side and the admission side compute the
    same value from the same dict."""
    canonical = json.dumps(doc, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def evidence_bindings(bundle: "EvidenceBundle") -> dict:
    """The per-facet dossier hashes for the dossiers a bundle actually carries —
    what `build_evidence` signs into the attestation so admission can prove the
    dossiers were not forged after signing (item 290, §6.2)."""
    out: dict = {}
    for facet, attr in _BOUND_FACETS.items():
        doc = getattr(bundle, attr, None)
        if doc is not None:
            out[facet] = _facet_hash(doc)
    return out


def binding_mismatch(att: dict | None, bundle: "EvidenceBundle") -> str | None:
    """The first bound facet whose dossier in `bundle` does not hash to the
    value signed into `att`, or None when every binding matches (or the
    attestation binds nothing). A mismatch — or a bound dossier that is absent
    from the bundle — is a tamper: the signed payload vouched for bytes that are
    no longer there (item 290, §6.2, exit test 5)."""
    if not isinstance(att, dict):
        return None
    bindings = att.get("evidence_bindings")
    if not isinstance(bindings, dict):
        return None
    for facet, signed_hash in sorted(bindings.items()):
        attr = _BOUND_FACETS.get(facet)
        doc = getattr(bundle, attr, None) if attr else None
        if doc is None:
            return facet          # bound but not present: a dropped dossier
        if _facet_hash(doc) != signed_hash:
            return facet          # present but altered: a forged/copied dossier
    return None


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
        # ONE frontend run, kept as a verdict rather than discarded: the
        # attestation below records what this run measured (item 127 F2). A
        # refusal is re-raised verbatim, so publishing an inadmissible component
        # fails with the compiler's own diagnostic, as before.
        from .attest import run_gate  # noqa: PLC0415
        gate_verdict = run_gate(paths=[str(source_path)],
                                normalize=_normalize_ir_for_attest)
        if gate_verdict.error is not None:
            raise gate_verdict.error
        ir = gate_verdict.ir
        manifest = _audit_document(ir)
        manifest_text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        facets: list[str] = []

        # The dossiers are computed BEFORE the attestation is signed (item 290,
        # §6.2), because the attestation binds their hashes into its signed
        # payload; a dossier written after signing could never be bound. capsule
        # + provenance are always reproducible with no runtime; the runtime-
        # tested facets are honestly skipped (left unavailable) when cordis-py is
        # absent, never faked.
        dossiers: dict[str, tuple[str, dict]] = {}
        dossiers["capabilities"] = (
            EVIDENCE_CAPABILITIES,
            {"kind": "revl.capabilities", "boundary": _boundary(ir)})
        from . import fault  # noqa: PLC0415
        try:
            dossiers["fault-sweep"] = (EVIDENCE_FAULT_SWEEP,
                                       fault.sweep_dossier(ir))
        except (ModuleNotFoundError, ImportError):
            pass  # cordis-py absent: honestly unavailable, never faked
        try:
            dossiers["inverse-roundtrip"] = (EVIDENCE_INVERSE_ROUNDTRIP,
                                             fault.roundtrip_dossier(ir))
        except (ModuleNotFoundError, ImportError):
            pass

        # write the bound dossiers, and collect their hashes for the signature.
        bindings: dict = {}
        for facet, (filename, doc) in sorted(dossiers.items()):
            _write_evidence_file(entry_dir, filename, doc, write=write)
            facets.append(facet)
            if facet in _BOUND_FACETS:
                bindings[facet] = _facet_hash(doc)

        # provenance - reproducible source/build facts (not itself bound: it
        # carries the source/manifest hashes the composition hash already roots).
        _write_evidence_file(
            entry_dir, EVIDENCE_PROVENANCE,
            _provenance_document(source, manifest, manifest_text, publisher),
            write=write)
        facets.append("provenance")

        # attestation - a signed admission record binding the dossier hashes,
        # when a key is supplied (item 127 + item 290, §6.2).
        if key is not None:
            from .attest import make_attestation  # noqa: PLC0415
            att = make_attestation(_normalize_ir_for_attest(ir), bytes(key),
                                   verdict=gate_verdict,
                                   now=now, signer=signer,
                                   evidence_bindings=bindings or None)
            _write_evidence_file(entry_dir, EVIDENCE_ATTESTATION, att,
                                 write=write)
            facets.append("attestation")

        produced[name] = sorted(facets)
    return produced


# --------------------------------------------------------------- read-time verify

# The index fields that carry AUTHORITY - what a resolve ranks and filters on.
# Every one of them is derivable from `component.rvl` by the compiler, so every
# one of them can be checked rather than believed.
_AUTHORITY_FIELDS = ("provides", "requires", "services", "capabilities",
                     "emissions", "sourceHash", "manifestHash")


def _normalized_claim(fieldname: str, value):
    """One index-row field in a shape that compares cleanly against the
    recompiled value (an absent map is `{}`, an absent list is `[]`)."""
    if fieldname in ("provides", "requires", "services"):
        return value or {}
    if fieldname == "capabilities":
        return list(value or [])
    if fieldname == "emissions":
        return int(value or 0)
    return value or ""


def _verified_facts(name: str, source_path: Path, source: str, row: dict,
                    recorded_manifest_text: str, recorded_manifest: dict
                    ) -> tuple[dict, dict, list[str]]:
    """Recompile one entry and return `(facts, manifest, problems)`.

    `facts` is the index row the current compiler DERIVES from the entry's own
    `component.rvl` - the authority a resolve is entitled to act on. `problems`
    names every field where the committed `index.json` claimed something else,
    plus a `manifest.json` that is not byte-reproducible from the source. A
    source that no longer compiles is itself a problem: nothing can vouch for
    what the gate would refuse anyway.
    """
    try:
        ir = compile_files([str(source_path)])
    except RevlError as error:
        return dict(row), recorded_manifest, [
            f"component.rvl does not compile, so nothing in its index row can "
            f"be verified: {error.message}"]
    manifest = _audit_document(ir)
    manifest_text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    facts = _entry_index_row(name, ir, source, manifest, manifest_text)

    problems: list[str] = []
    for fieldname in _AUTHORITY_FIELDS:
        claimed = _normalized_claim(fieldname, row.get(fieldname))
        actual = _normalized_claim(fieldname, facts.get(fieldname))
        if claimed != actual:
            problems.append(
                f"index.json claims {fieldname}="
                f"{json.dumps(claimed, sort_keys=True)} but component.rvl "
                f"compiles to {json.dumps(actual, sort_keys=True)}")
    if recorded_manifest_text and recorded_manifest_text != manifest_text:
        problems.append("manifest.json is not reproducible from component.rvl "
                        "by the current compiler")
    return facts, manifest, problems


# --------------------------------------------------------------- the registry

class Registry:
    """A loaded, git-backed component index (docs/registry.md §1).

    Construct with `Registry.from_dir`. Nothing here runs candidate code:
    index rows and sources are data, and a fetched candidate only ever *runs*
    when it passes the admission gate at `revl_admit`/`revl_swap` time
    (docs/registry.md §4).
    """

    def __init__(self, entries: list[RegistryEntry], *,
                 verified: bool = True) -> None:
        self.entries = entries
        # True when every entry's index row was cross-checked against its own
        # `component.rvl` at load time (the default).
        self.verified = verified

    @classmethod
    def from_dir(cls, registry_dir: str | os.PathLike, *,
                 verify_entries: bool = True) -> "Registry":
        """Load a registry from disk.

        `index.json` is a DISCOVERY list, not an authority. With
        `verify_entries` (the default) every row is cross-checked against the
        entry's own `component.rvl` by recompiling it, and the RECOMPILED facts
        - provides/requires/service shapes/capabilities/emissions - are what the
        entry carries; the index's claims are only compared, never believed. An
        index that says a component reaches no capability while its source
        reaches `*` therefore cannot rank as least-authority: the disagreement
        lands in `index_problems` and `resolve` refuses the entry outright.

        This is `verify`'s regenerate-or-red discipline applied at READ time.
        `verify` is a CI job; a resolve that trusted the index would be the one
        place in the system where the index is believed without being checked.
        """
        registry_dir = Path(registry_dir)
        index_path = registry_dir / INDEX_FILENAME
        rows = (json.loads(_read(index_path)).get("components") or {}
                if index_path.exists() else {})
        comps = _components_dir(registry_dir)
        entries: list[RegistryEntry] = []
        for name, row in sorted(rows.items()):
            entry_dir = comps / name
            source_path = entry_dir / "component.rvl"
            source = _read(source_path)
            manifest_path = entry_dir / "manifest.json"
            recorded_manifest_text = (_read(manifest_path)
                                      if manifest_path.exists() else "")
            manifest = json.loads(recorded_manifest_text) \
                if recorded_manifest_text else {}
            dossier_path = entry_dir / "dossier.json"
            dossier = (json.loads(_read(dossier_path))
                       if dossier_path.exists() else None)

            facts = dict(row)
            problems: list[str] = []
            if verify_entries:
                facts, manifest, problems = _verified_facts(
                    name, source_path, source, row, recorded_manifest_text,
                    manifest)
            else:
                problems = [
                    "index rows were not cross-checked against component.rvl "
                    "(from_dir was called with verify_entries=False)"]

            entries.append(RegistryEntry(
                name=name,
                source=source,
                manifest=manifest,
                dossier=dossier,
                provides=facts.get("provides") or {},
                requires=facts.get("requires") or {},
                service_shapes=facts.get("services") or {},
                capabilities=tuple(facts.get("capabilities") or ()),
                emissions=int(facts.get("emissions") or 0),
                # the truth, always recomputed from the bytes on disk.
                source_hash=_sha256(source),
                manifest_hash=facts.get("manifestHash") or "",
                evidence_bundle=load_evidence_bundle(entry_dir),
                recorded_source_hash=row.get("sourceHash") or "",
                index_problems=tuple(problems),
            ))
        return cls(entries, verified=verify_entries)

    def resolve(self, need, manifest: dict | None = None,
                limit: int = 5, *, verify_required: bool = False,
                key: bytes | None = None, trusted_publishers=(),
                adapt: bool = True, adapt_opt_ins: dict | None = None) -> dict:
        return resolve(self, need, manifest=manifest, limit=limit,
                       verify_required=verify_required, key=key,
                       trusted_publishers=trusted_publishers,
                       adapt=adapt, adapt_opt_ins=adapt_opt_ins)


# --------------------------------------------------------------- the need

@dataclass(frozen=True)
class _Need:
    """A canonical service shape a candidate is filtered against."""
    label: str            # a human tag for the `why`/diagnostics, never matched on
    decl: ServiceDecl     # the shape; `_service_compatible` ignores the name
    # The consumer-side type table, when the need arrived as source. Only the
    # item-296 adapter probe reads it (`compatible_total` resolves nominals on
    # BOTH sides against real tables, design section 2.2 B3); the direct §5
    # filter never needed one. A fill spec or a bare shape object carries no
    # table, and a nominal that cannot be resolved refuses the bridge rather
    # than being bridged permissively - which is the point of the restricted
    # relation.
    types: dict = field(default_factory=dict)


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
        return [_Need(name, _service_from_ir(name, spec),
                      types=doc.get("types") or {})]
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

@dataclass(frozen=True)
class _Bridge:
    """A PROPOSED adapter for a candidate the direct §5 filter refused
    (item 296, design section 3: proposed, never silent).

    The plan and the rendered `.rvl` ride out on the candidate so the author
    commits the declaration and the compiler re-admits it through the ordinary
    gate. `chain_depth` is 1 for a bridge onto ordinary code and n+1 onto a
    committed adapter marking itself depth n (section 6.4).
    """
    plan: object              # adapt.BridgeResult
    source: str               # the rendered section-4 artifact
    derivation: str
    chain_depth: int
    merges: tuple             # the outcome-merge shapes the plan opted into


@dataclass
class _Match:
    entry: RegistryEntry
    key: str                 # the provision key the candidate offers
    service: str             # the provided service name that matched
    need_label: str
    exact: bool              # identical interface vs a compatible widening
    # None for a directly §5-compatible candidate; a proposed adapter for one
    # that is only `compatible-with-adapter` (item 296 slice 3).
    bridge: "_Bridge | None" = None

    def why(self) -> str:
        if self.bridge is not None:
            depth = self.bridge.chain_depth
            parts = [f"provides `{self.service}` as `{self.key}`: NOT directly "
                     f"§5-compatible, but compatible-with-adapter — a safe "
                     f"bridge is proposed (commit the `adapt` source, the "
                     f"compiler re-admits it through the ordinary gate)"]
            if depth > 1:
                parts.append(
                    f"chain depth {depth}: this candidate is ITSELF a "
                    f"synthesized adapter, so the bridge stacks on one already "
                    f"committed; it ranks below a fresh single bridge onto the "
                    f"underlying candidate")
            else:
                parts.append("chain depth 1")
            if self.bridge.merges:
                parts.append(
                    f"the plan merges outcomes ({', '.join(self.bridge.merges)}): "
                    f"any fault-sweep conclusion that errors surface as `Err` is "
                    f"INVERTED behind this bridge, so that evidence class is "
                    f"discounted in the ranking")
            return "; ".join(parts)
        fit = "exact interface match" if self.exact \
            else "compatible superset (adds/widens, breaks no call site)"
        return (f"provides `{self.service}` as `{self.key}`: §5-compatible with "
                f"the need ({fit})")

    def authority_fit_key(self) -> tuple:
        # docs/registry.md §2 ranking, least authority first - the part that
        # sits ABOVE evidence quality:
        #   1. smallest declared capability set (unscoped `*` ranks last)
        #   2. tighter interface fit (exact over compatible-with-widening)
        #   3. direct over adapted, then shallower chain over deeper (item 296
        #      §6.1/§6.4: "the bridge is a cost, not a tie", and "depth only
        #      ever ranks down"). Both sit AT equal authority fit and AHEAD of
        #      evidence, which is exactly where the design puts them.
        # Evidence quality (item 293) breaks the tie among candidates equal here;
        # interface compatibility itself is a hard filter, decided in
        # `_match_entry`, and never a ranking term.
        e = self.entry
        return (
            1 if e.unbounded else 0,
            len([c for c in e.capabilities if c != "*"]),
            e.emissions,
            0 if self.exact else 1,
            0 if self.bridge is None else 1,
            0 if self.bridge is None else self.bridge.chain_depth,
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


# ------------------------------------------- item 296: compatible-with-adapter
#
# A candidate the §5 filter refuses may still be reachable across a SAFE bridge
# (docs/design/296-adapter-synthesis.md). Resolve's job here is the design's
# option (b), "explicit and proposed": report the candidate as
# `compatible-with-adapter`, carry the plan and the rendered `adapt` source, and
# rank it BELOW every directly compatible candidate at equal authority. Nothing
# is wired: the author commits the declaration and the compiler re-admits it
# through the ordinary gate. This module adds ZERO adapter logic of its own -
# the predicate is `adapt.bridge_plan`, exactly as the §5 relation is `lower`'s.


def _compiled_entry_ir(entry: RegistryEntry, ir_cache: dict) -> dict:
    """The entry's own compiled IR, once per resolve. Normalized the way an
    attestation binds it (basenamed `file`/`source`), which leaves `services`
    and `types` untouched, so the same cached document serves both the evidence
    check and the adapter probe's type tables. A source that does not compile
    caches as `{}` - nothing can be proposed against what the gate would refuse
    anyway."""
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
    return ir


def _bridge_entry(entry: RegistryEntry, needs: list[_Need], ir_cache: dict,
                  opt_ins: dict) -> tuple["_Match | None", list[dict]]:
    """Probe `entry` for a proposed adapter. Returns `(match, near_misses)`.

    Only entries whose provided service carries EVERY method name the need
    declares are probed: v1 matches methods by name (design §2.4, §8, no rename
    mapping), so a missing method is never bridgeable, and probing anyway would
    make every unrelated entry in the registry a "near miss".

    A probe that refuses rides out as a near miss - the named clauses at the
    named positions, plus the item-274 `navigate` record - so "fix it by hand"
    starts from the exact positions rather than a diff hunt (design §5).
    """
    from .adapt import (bridge_plan, chain_depth_for,  # noqa: PLC0415
                        derivation_hash, navigate_for_refusals, render_adapter,
                        service_surface)

    near: list[dict] = []
    prov_types: dict | None = None
    for key, service in sorted((entry.provides or {}).items()):
        shape = entry.service_shapes.get(service)
        if not shape:
            continue
        provided = _service_from_ir(service, shape)
        for need in needs:
            if not set(need.decl.methods) <= set(provided.methods):
                continue
            if prov_types is None:
                # only now is the entry's own source worth compiling: the
                # predicate resolves nominals against real type tables, and an
                # entry with no name overlap never reaches this point. Cached,
                # and shared with the evidence check.
                prov_types = _compiled_entry_ir(entry,
                                                ir_cache).get("types") or {}
            result = bridge_plan(need.decl, provided, opt_ins,
                                 req_types=need.types, prov_types=prov_types)
            if not result.ok:
                near.append({
                    "name": entry.name,
                    "key": key,
                    "service": service,
                    "need": need.label,
                    "refusals": [
                        {"method": r.method, "position": r.position,
                         "transformation": r.transformation, "clause": r.clause,
                         "reason": r.reason, "hint": r.hint}
                        for r in result.refusals],
                    "navigate": navigate_for_refusals(result.refusals),
                })
                continue
            derivation = derivation_hash(
                service_surface(need.decl), service_surface(provided),
                entry.source_hash, json.dumps(opt_ins, sort_keys=True))
            depth = chain_depth_for(entry.source)
            carried: list[str] = []
            for method in need.decl.methods.values():
                for cap in (method.capabilities or ()):
                    if cap not in carried:
                        carried.append(cap)
            try:
                rendered = render_adapter(
                    f"{need.decl.name}Adapter", need.decl, provided, opt_ins,
                    provide_key=key, require_key="backing",
                    carried_tokens=tuple(carried), prov_types=prov_types,
                    derivation=derivation, chain_depth=depth)
            except ValueError as error:
                # the slice-1 renderer does not cover every catalogue
                # combination yet, and it RAISES rather than emitting source it
                # cannot render correctly. The plan is still sound and still
                # worth reporting; the artifact is simply not renderable here.
                rendered = ""
                near.append({"name": entry.name, "key": key,
                             "service": service, "need": need.label,
                             "unrenderable": str(error)})
            # One proposal per entry, and the near misses collected for its
            # OTHER keys are dropped with it: "this key of this entry needs a
            # different bridge" is not actionable once one bridge to the entry
            # already works, it is just noise in the answer.
            return _Match(entry, key, service, need.label, False,
                          bridge=_Bridge(plan=result, source=rendered,
                                         derivation=derivation,
                                         chain_depth=depth,
                                         merges=tuple(result.merges))), []
    return None, near


def _adapter_block(match: _Match) -> dict:
    """The `compatible-with-adapter` payload a candidate carries: the verdict,
    the per-method plan, the chain depth, the derivation hash, and the rendered
    section-4 artifact the author commits. Deliberately the same shape
    `revl adapt --check --emit` prints, so a harness reads one record."""
    bridge = match.bridge
    block = {
        "verdict": "compatible-with-adapter",
        "need": match.need_label,
        "candidate": match.service,
        "provideKey": match.key,
        "chainDepth": bridge.chain_depth,
        "merges": list(bridge.merges),
        "derivation": bridge.derivation,
        # Design section 4, "Wiring": the adapter provides the consumer-facing
        # key and binds the candidate under a FRESH alias, so G2 sees exactly
        # one provider of that key. The rename is the author's, on the same
        # commit as the `adapt` declaration - phase 0 is the READ path and
        # writes no manifest.
        "wiring": {
            "requireAlias": "backing",
            "renameCandidateKey": {"from": match.key, "to": "backing"},
            "note": "bind the candidate's provision under `backing` when you "
                    "commit this: the adapter provides `"
                    f"{match.key}` itself, and G2 forbids two providers of one "
                    "key",
        },
        "methods": [
            {"method": mp.method,
             "steps": [{"position": st.position,
                        "transformation": st.transformation,
                        "detail": st.detail,
                        "merge_shape": st.merge_shape}
                       for st in mp.steps]}
            for mp in bridge.plan.methods],
        "applied": False,
        "note": "PROPOSED, never wired: commit this `adapt` source and the "
                "compiler re-admits the whole composition through the ordinary "
                "gate (item 296, design section 3)",
    }
    if bridge.source:
        block["source"] = bridge.source
    return block


def _discount_error_semantics(rank_key: tuple) -> tuple:
    """Item 296 §6.1: a plan containing an outcome merge INVERTS a fault-sweep
    conclusion of the shape "failures surface as `Err`, data is never
    corrupted" - behind the merge, those dutifully surfaced errors are exactly
    what the consumer stops seeing. So the error-semantics evidence class (the
    fault sweep, `assess_evidence`'s leading rank term and its coverage
    tiebreak) is discounted to `unavailable` FOR RANKING when the proposed plan
    merges outcomes. Every other class - inverse-roundtrip, gauntlet,
    attestation, publisher - describes value behavior the bridge leaves
    untouched and keeps full weight. The REPORTED facets are never touched: the
    candidate's evidence is still stated honestly, it just stops buying rank it
    no longer earns.
    """
    discounted = list(rank_key)
    assert len(discounted) == 6, (
        "assess_evidence's rank_key changed shape; the fault-sweep terms this "
        "discounts are no longer at 0 and 5")
    discounted[0] = _SWEEP_RANK["unavailable"]
    discounted[5] = 0                     # -passed, the finer coverage tiebreak
    return tuple(discounted)


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
        ir = _compiled_entry_ir(entry, ir_cache)
    return assess_evidence(bundle, key=key, ir=ir,
                           trusted_publishers=trusted_publishers)


def resolve(registry, need, manifest: dict | None = None,
            limit: int = 5, *, verify_required: bool = False,
            key: bytes | None = None, trusted_publishers=(),
            adapt: bool = True, adapt_opt_ins: dict | None = None) -> dict:
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

    `adapt` (on by default, item 296) additionally reports a candidate the §5
    filter REFUSED as `compatible-with-adapter` when a safe bridge exists to it:
    the candidate carries an `adapter` block with the bridge plan, the rendered
    `adapt` source to commit, the chain depth, and the derivation hash. Nothing
    is wired - the proposal is explicit and the compiler re-admits the committed
    declaration through the ordinary gate (design section 3, "proposed, not
    silent"). Adapted candidates rank strictly BELOW every directly compatible
    one at equal authority fit, a deeper chain below a shallower one, and a plan
    that merges outcomes has the candidate's error-semantics evidence discounted
    (section 6.1/6.4). `adapt_opt_ins` is the author's `D` map, keyed by method
    name exactly as `revl adapt --adapt` takes it: without it the transformations
    that need an opt-in (an outcome merge, a non-canonical default) refuse, and
    the refusal rides out under `nearMisses` naming the position and the clause.

    Two things here are checked rather than trusted, always: an entry whose
    `index.json` row disagrees with its own `component.rvl` is REFUSED (it is
    listed in `refused` with the reason, never ranked), and evidence only earns
    rank when something outside the publisher vouches for it. A registry entry
    cannot improve its standing by writing files about itself.
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
    refused: list[dict] = []
    near_misses: list[dict] = []
    opt_ins = dict(adapt_opt_ins or {})
    for entry in registry.entries:
        if entry.index_problems:
            # the index disagrees with the entry's own source. Ranking it would
            # mean ranking the publisher's claim about its authority instead of
            # its authority - refuse it, and say so, rather than quietly
            # demoting it: a stale index and a lying one look identical here,
            # and both are fixed the same way (regenerate and commit).
            refused.append({"name": entry.name,
                            "reasons": list(entry.index_problems)})
            continue
        match = _match_entry(entry, needs)
        if match is None and adapt:
            # the §5 filter refused it directly. Item 296: is it reachable
            # across a SAFE bridge? A proposal, never a wiring.
            match, near = _bridge_entry(entry, needs, ir_cache, opt_ins)
            near_misses.extend(near)
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

    # least authority, then fit (direct over adapted, shallow chain over deep),
    # then evidence quality, then a stable tiebreak.
    def _rank(pair: tuple) -> tuple:
        match, assessment = pair
        evidence = assessment.rank_key
        if match.bridge is not None and match.bridge.merges:
            # §6.1: the plan merges outcomes, so the fault sweep's
            # errors-surface-as-`Err` conclusion no longer describes what the
            # consumer sees. Discount that class - and only that class.
            evidence = _discount_error_semantics(evidence)
        return (match.authority_fit_key(), evidence,
                len(match.entry.source), match.entry.name)

    graded.sort(key=_rank)

    assumptions = list(_ASSUMPTIONS)
    assumptions.append(
        "among interface-compatible candidates the ranking is by evidence "
        "quality (fault-sweep coverage, valid attestation, trusted publisher, "
        "inverse-roundtrip pass); a missing facet is unavailable, never valid")
    if getattr(registry, "verified", False):
        assumptions.append(
            "every index row was cross-checked against its own component.rvl "
            "by recompiling it; the compiled facts rank, the index's claims "
            "only get compared")
    else:
        assumptions.append(
            "index rows were NOT cross-checked against the component sources: "
            "provides/requires/capabilities/emissions are the publisher's claim")
    assumptions.append(
        "evidence is only worth rank when it is verified - a dossier bound by a "
        "cryptographically valid attestation, or a publisher in the caller's "
        "trust set; an unverified claim ranks level with no claim at all"
        + ("" if key_bytes is not None else
           " (no signing key was supplied, so no attestation could be verified)"))
    if refused:
        assumptions.append(
            f"{len(refused)} entr{'y was' if len(refused) == 1 else 'ies were'} "
            "refused outright: the index row disagrees with the entry's own "
            "component.rvl (see `refused`)")
    if manifest:
        assumptions.append(
            "candidates are additionally admissible against the supplied "
            "manifest: a key the composition already provides is withheld (G2)")
    if verify_required:
        assumptions.append(
            "verify-required: a candidate without a cryptographically valid "
            "attestation was filtered, not merely ranked lower")
    if adapt:
        adapted = [m for m, _ in graded if m.bridge is not None]
        assumptions.append(
            "candidates the §5 filter refused were additionally probed for a "
            "SAFE adapter (item 296): one carrying an `adapter` block is "
            "`compatible-with-adapter`, PROPOSED and never wired - it ranks "
            "below every directly compatible candidate at equal authority, a "
            "deeper chain below a shallower one, and a plan that merges "
            "outcomes has its error-semantics evidence discounted")
        if adapted:
            assumptions.append(
                f"{len(adapted)} candidate"
                f"{'' if len(adapted) == 1 else 's'} need"
                f"{'s' if len(adapted) == 1 else ''} an adapter: the composition "
                "is NOT derivable from the registry source alone until the "
                "`adapt` declaration is committed and re-admitted")
        if not opt_ins:
            assumptions.append(
                "no `adapt` opt-in map was supplied, so every transformation "
                "needing one (an outcome merge, a non-canonical default) "
                "refused; see `nearMisses` for the named positions")
    else:
        assumptions.append(
            "adapter probing was disabled: only directly §5-compatible "
            "candidates were considered")

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
                "verified": assessment.verified,
                "present": list((entry.evidence_bundle or EvidenceBundle())
                                .present()),
            },
        }
        if assessment.sweep_coverage is not None:
            candidate["evidence"]["faultSweepCoverage"] = list(
                assessment.sweep_coverage)
        if match.bridge is not None:
            candidate["adapter"] = _adapter_block(match)
            if match.bridge.merges:
                candidate["evidence"]["discounted"] = ["fault-sweep"]
                candidate["evidence"]["discountReason"] = (
                    "the proposed plan merges outcomes "
                    f"({', '.join(match.bridge.merges)}), inverting any "
                    "fault-sweep conclusion that errors surface as `Err`; the "
                    "facet status above is unchanged, its RANK is discounted")
        if entry.dossier is not None:
            candidate["dossier"] = entry.dossier
        candidates.append(candidate)

    result = {
        "ok": True,
        "query": "resolve",
        "question": "who provides a service admissible for this need?",
        "precision": "exact",
        "assumptions": assumptions,
        "candidates": candidates,
        "refused": refused,
    }
    if adapt:
        # capped like `candidates`: a near miss is a repair to act on, not a
        # log, and an unbounded list would quietly become the bulk of the
        # answer on a large registry. The truncation is stated, never silent.
        cap = max(0, limit)
        result["nearMisses"] = near_misses[:cap]
        if len(near_misses) > cap:
            assumptions.append(
                f"{len(near_misses) - cap} further near-miss adapter "
                f"refusal{'s were' if len(near_misses) - cap != 1 else ' was'} "
                f"omitted: `nearMisses` is capped at `limit` ({cap}) the same "
                "way `candidates` is")
    return result


__all__ = ["Registry", "RegistryEntry", "EvidenceBundle", "EvidenceAssessment",
           "build_index", "build_evidence", "verify", "resolve",
           "load_evidence_bundle", "assess_evidence", "INDEX_VERSION",
           "EVIDENCE_DIRNAME", "EVIDENCE_FILES"]
