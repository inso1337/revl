"""Evidence-carrying registry components, ranked by evidence quality at resolve
(roadmap item 293).

A registry component carries a machine-verifiable *evidence bundle* alongside
its source and manifest - the verbatim outputs of the existing producers
(attestation, gauntlet, fault sweep, inverse round-trip, capabilities,
provenance). Among the interface-*compatible* candidates for a need,
`revl_resolve` ranks by evidence quality; interface compatibility stays a hard
filter, and a missing or invalid evidence file is never read as valid.

These are frontend-only: the bundles are authored in the producers' real shapes
(a fault-sweep dossier is `fault.sweep_dossier`'s shape; an attestation is a real
`revl.attest.make_attestation`, pure stdlib), so nothing here needs the cordis
runtime and it all runs in the ordinary `pytest tests/` suite.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from revl import registry  # noqa: E402
from revl import attest  # noqa: E402
from revl.compiler import compile_files  # noqa: E402

# A fixture signing key - obviously a test key, never a real secret. It only
# signs the throwaway attestations these tests build in tmp registries.
KEY = b"revl-registry-293-evidence-fixture-key"
NOW = "2026-01-01T00:00:00+00:00"

# A Database need whose service is declared under a different name, so a match
# is structural (the same discipline test_registry_resolve uses).
DB_NEED = """
service Store {
  fn query(sql: Str) -> List[Row]
  emission fn execute(sql: Str) -> Int
}
"""

# Two interface-identical Database providers (same surface, no capabilities), so
# they tie on authority and fit and evidence quality alone decides the winner.
_DB_BODY = """
service Database {
  fn query(sql: Str) -> List[Row]
  emission fn execute(sql: Str) -> Int
}

component %s provides db: Database {
  let pool = effect Pool.open("db://", 4) undo pool.close()
  provide db {
    fn query(sql)   = pool.query(sql)
    fn execute(sql) = pool.execute(sql)
  }
}
"""

# A Database provider that drops `execute` - interface-INCOMPATIBLE with the
# need (it breaks the need's `execute` call site), so it must never be chosen
# however strong its evidence.
_DB_READONLY = """
service Database {
  fn query(sql: Str) -> List[Row]
}

component ReadOnlyDb provides db: Database {
  let pool = effect Pool.open("db://", 4) undo pool.close()
  provide db {
    fn query(sql) = pool.query(sql)
  }
}
"""


def _fault_sweep(passed: int, steps: int) -> dict:
    """A fault-sweep dossier in `fault.sweep_dossier`'s real shape."""
    status = "passed" if passed == steps else "failed"
    return {
        "kind": "tested", "status": status, "roadmapItem": 30,
        "title": "fault sweep at every step", "tier": "py",
        "counts": {"components": 1, "steps": steps, "passed": passed,
                   "failed": steps - passed, "unreachable": 0},
        "components": [], "unreachable": [],
    }


def _inverse_pass() -> dict:
    return {
        "kind": "tested", "status": "passed", "roadmapItem": 26,
        "title": "verified-effect inverse round-trips", "tier": "py",
        "counts": {"effects": 1, "passed": 1, "failed": 0, "rounds": 16},
        "components": [],
    }


def _attestation_for(comp_dir: str, *, tamper: bool = False) -> dict:
    """A real attestation for the component at `comp_dir`, signed over its
    rebuilt IR with the fixture key. `tamper` flips a signed field so the
    signature no longer matches - the `invalid` grade."""
    ir = registry._normalize_ir_for_attest(
        compile_files([os.path.join(comp_dir, "component.rvl")]))
    att = attest.make_attestation(ir, KEY, now=NOW, signer="revl-ci")
    if tamper:
        att = dict(att)
        att["verdict"] = "tampered"   # signed field altered after signing
    return att


def _write(comp_dir: str, name: str, source: str) -> None:
    os.makedirs(comp_dir, exist_ok=True)
    with open(os.path.join(comp_dir, "component.rvl"), "w", encoding="utf-8") as fh:
        fh.write(source)


def _put_evidence(comp_dir: str, files: dict) -> None:
    ev = os.path.join(comp_dir, registry.EVIDENCE_DIRNAME)
    os.makedirs(ev, exist_ok=True)
    for filename, doc in files.items():
        with open(os.path.join(ev, filename), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, indent=2, sort_keys=True) + "\n")


def _build_registry(tmp_path, components: dict) -> str:
    """`components` maps name -> (source, evidence_files_dict). Publish them:
    write sources, regenerate the index/manifests, then drop each evidence
    bundle into `<component>/evidence/`."""
    reg = os.path.join(str(tmp_path), "registry")
    comps = os.path.join(reg, "components")
    os.makedirs(comps, exist_ok=True)
    for name, (source, _evidence) in components.items():
        _write(os.path.join(comps, name), name, source)
    registry.build_index(reg)                       # manifests + index.json
    for name, (_source, evidence) in components.items():
        comp_dir = os.path.join(comps, name)
        # attestation entries are (marker) resolved against the built component
        resolved = {}
        for filename, doc in (evidence or {}).items():
            if doc == "ATTEST":
                resolved[filename] = _attestation_for(comp_dir)
            elif doc == "ATTEST_TAMPERED":
                resolved[filename] = _attestation_for(comp_dir, tamper=True)
            else:
                resolved[filename] = doc
        if resolved:
            _put_evidence(comp_dir, resolved)
    return reg


def _names(result: dict) -> list:
    return [c["name"] for c in result["candidates"]]


# --------------------------------------------------------------- exit tests

def test_published_component_carries_the_evidence_bundle(tmp_path):
    """The publish path (`build_index` + `build_evidence`) writes a machine-
    verifiable evidence bundle next to the source - the always-reproducible
    facets, plus an attestation when a signing key is supplied."""
    reg = os.path.join(str(tmp_path), "registry")
    comps = os.path.join(reg, "components")
    _write(os.path.join(comps, "db_x"), "db_x", _DB_BODY % "DbX")
    registry.build_index(reg)
    registry.build_evidence(reg, key=KEY, now=NOW, signer="revl-ci",
                            publisher="revl-ci")
    ev = os.path.join(comps, "db_x", registry.EVIDENCE_DIRNAME)
    # capabilities + provenance + attestation are reproducible with no runtime.
    for facet in (registry.EVIDENCE_CAPABILITIES, registry.EVIDENCE_PROVENANCE,
                  registry.EVIDENCE_ATTESTATION):
        assert os.path.exists(os.path.join(ev, facet)), facet
    # the committed attestation is the producer's real output and verifies.
    att = json.loads(open(os.path.join(ev, registry.EVIDENCE_ATTESTATION)).read())
    ir = registry._normalize_ir_for_attest(
        compile_files([os.path.join(comps, "db_x", "component.rvl")]))
    ok, _ = attest.verify_attestation(att, KEY, ir)
    assert ok
    # provenance is assembled from the reproducible index/interchange facts.
    prov = json.loads(open(os.path.join(ev, registry.EVIDENCE_PROVENANCE)).read())
    assert prov["sourceSha256"] and prov["publisher"] == "revl-ci"


def test_resolve_prefers_the_higher_evidence_compatible_candidate(tmp_path):
    """Two interface-compatible providers, tied on authority and fit; the one
    with a fault sweep and a valid attestation wins, and the result names WHY."""
    reg = _build_registry(tmp_path, {
        "db_hi": (_DB_BODY % "DbHi", {
            registry.EVIDENCE_FAULT_SWEEP: _fault_sweep(12, 12),
            registry.EVIDENCE_ATTESTATION: "ATTEST",
        }),
        "db_lo": (_DB_BODY % "DbLo", {}),   # no evidence -> unavailable
    })
    result = registry.resolve(reg, DB_NEED)
    assert _names(result) == ["db_hi", "db_lo"]
    top = result["candidates"][0]
    # the winner's `why` spells out its evidence.
    assert "fault sweep 12/12" in top["why"]
    assert top["evidence"]["facets"]["fault-sweep"] == "full"
    assert top["evidence"]["faultSweepCoverage"] == [12, 12]
    # the loser is honestly graded unavailable, not silently valid.
    lo = result["candidates"][1]
    assert lo["evidence"]["facets"]["fault-sweep"] == "unavailable"
    assert lo["evidence"]["facets"]["attestation"] == "unavailable"


def test_incompatible_candidate_is_never_chosen_however_strong_its_evidence(tmp_path):
    """Interface compatibility is a hard filter: a provider that breaks the
    need's call site is excluded even with the best evidence in the registry,
    and a compatible provider with NO evidence is still returned."""
    reg = _build_registry(tmp_path, {
        "db_ok": (_DB_BODY % "DbOk", {}),               # compatible, no evidence
        "db_bad": (_DB_READONLY, {                       # incompatible, top evidence
            registry.EVIDENCE_FAULT_SWEEP: _fault_sweep(12, 12),
            registry.EVIDENCE_INVERSE_ROUNDTRIP: _inverse_pass(),
            registry.EVIDENCE_ATTESTATION: "ATTEST",
        }),
    })
    names = _names(registry.resolve(reg, DB_NEED))
    assert "db_ok" in names
    assert "db_bad" not in names


def test_missing_and_invalid_evidence_rank_below_valid(tmp_path):
    """A valid attestation outranks a present-but-unverifiable one, which
    outranks a missing one, which outranks a tampered (invalid) one - a strict
    honest ordering among interface-equal candidates."""
    reg = _build_registry(tmp_path, {
        "db_valid": (_DB_BODY % "DbValid", {
            registry.EVIDENCE_ATTESTATION: "ATTEST"}),
        "db_missing": (_DB_BODY % "DbMissing", {}),
        "db_tampered": (_DB_BODY % "DbTampered", {
            registry.EVIDENCE_ATTESTATION: "ATTEST_TAMPERED"}),
    })
    # With the key, the attestations are cryptographically graded.
    names = _names(registry.resolve(reg, DB_NEED, key=KEY))
    assert names.index("db_valid") < names.index("db_missing")
    assert names.index("db_missing") < names.index("db_tampered")


def test_verify_required_filters_an_invalid_attestation(tmp_path):
    """In verify-required mode only a candidate with a cryptographically valid
    attestation admits: the tampered one and the one with no attestation are
    filtered, not merely ranked lower."""
    reg = _build_registry(tmp_path, {
        "db_valid": (_DB_BODY % "DbValid", {
            registry.EVIDENCE_ATTESTATION: "ATTEST"}),
        "db_missing": (_DB_BODY % "DbMissing", {}),
        "db_tampered": (_DB_BODY % "DbTampered", {
            registry.EVIDENCE_ATTESTATION: "ATTEST_TAMPERED"}),
    })
    result = registry.resolve(reg, DB_NEED, verify_required=True, key=KEY)
    assert _names(result) == ["db_valid"]
    assert any("verify-required" in a for a in result["assumptions"])
    assert result["candidates"][0]["evidence"]["facets"]["attestation"] == "valid"


def test_trusted_publisher_lifts_a_candidate_over_an_untrusted_one(tmp_path):
    """The provenance publisher is a ranking signal, and trust is supplied by
    the resolver, never self-asserted: with the two tied on all else, the
    trusted publisher's component ranks first."""
    reg = _build_registry(tmp_path, {
        "db_trusted": (_DB_BODY % "DbTrusted", {
            registry.EVIDENCE_PROVENANCE: {"kind": "revl.provenance",
                                           "publisher": "acme-ci"}}),
        "db_other": (_DB_BODY % "DbOther", {
            registry.EVIDENCE_PROVENANCE: {"kind": "revl.provenance",
                                           "publisher": "somebody-else"}}),
    })
    names = _names(registry.resolve(reg, DB_NEED,
                                    trusted_publishers=("acme-ci",)))
    assert names.index("db_trusted") < names.index("db_other")
