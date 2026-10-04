"""Assembling a registry's evidence bundles, the second half of publishing
(roadmap item 293).

`build_evidence` lived in `revl.registry`. It produces the runtime-tested
facets by RUNNING each component (`revl.fault`'s sweep and round trip), and
`revl.registry` sits on the compiler's import chain (the policy evaluator reads
its evidence model), so its lazy `import revl.fault` put the fault sweep, the
tier runners, placement and `revl.mcp` on the compiler's import graph (issue
#1780). Moved here unchanged; `revl.registry` keeps the index, the model and the
readers.
"""

from __future__ import annotations

import json
import os

from .registry import (
    EVIDENCE_ATTESTATION,
    EVIDENCE_CAPABILITIES,
    EVIDENCE_FAULT_SWEEP,
    EVIDENCE_INVERSE_ROUNDTRIP,
    EVIDENCE_PROVENANCE,
    _BOUND_FACETS,
    _audit_document,
    _components_dir,
    _facet_hash,
    _normalize_ir_for_attest,
    _provenance_document,
    _read,
    _write_evidence_file,
)


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
    from .boundary import _boundary  # noqa: PLC0415

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


__all__ = ["build_evidence"]
