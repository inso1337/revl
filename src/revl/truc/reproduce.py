"""`truc reproduce <component@version>`, deterministic package reproduction
(roadmap item 297).

truc already lets a consumer *verify* a component's source against a recorded
hash (the `sourceHash` in the registry index, the pin in `truc.lock`). But a
verified source is not enough: the thing a downstream consumer actually runs is
the *emitted artifact*, and a source that hashes correctly can still fail to
rebuild into the same manifest, the same policy surface, or the same backend
output if the toolchain drifted or the recorded artifact was tampered with.
`truc reproduce` closes that gap. It takes what was *recorded* for a published
component and rebuilds it through the normal compiler pipeline, then compares
the recomputed hashes tier by tier against the recorded ones:

  * **source**, the recorded `component.rvl` re-hashed against the recorded
    `sourceHash`.
  * **dependency lock**, the `provides`/`requires` surface the compiler reads
    off the rebuilt IR against the recorded lock surface.
  * **IR**, the item-28 audit/interchange document (`registry._audit_document`)
    re-derived from the rebuilt IR, re-hashed against the recorded `manifestHash`
    and compared byte-for-byte against the recorded `manifest.json`.
  * **policy surface**, the boundary the composition crosses (emissions +
    capabilities) read off the rebuilt manifest against the recorded surface.
  * **backend version**, the interchange `schema_version` the recorded manifest
    was stamped with against the current compiler's `INTERCHANGE_VERSION`.
  * **independent pin**, the project's own `truc.lock` pin (and the bytes
    vendored under `trucs/<name>/`) against the registry's source. Every other
    tier compares the registry with itself and therefore cannot see a
    substitution the registry made and then re-indexed; this one can.
  * **attestation**, when the entry carries an
    `evidence/attestation.json`, verified through
    `revl.attest.verify_attestation` (item 127) against the rebuilt IR: the
    signature proves the record is authentic, the hash proves the IR did not
    change since it was signed, and the signed per-facet bindings prove the
    evidence dossiers beside it are the ones that were signed.
  * **emitted artifact**, when the entry records artifact hashes
    (`artifacts.json`), each backend's emitter is re-run over the rebuilt IR and
    the emitted source is re-hashed against the recorded hash.

Every tier reports one of three outcomes, honestly:

  * **OK**, recomputed value equals the recorded one.
  * **MISMATCH**, they differ; the recorded and rebuilt hashes (or versions)
    are both printed so a consumer sees exactly which artifact diverged.
  * **cannot verify**, nothing was recorded for that tier (no attestation, no
    recorded artifact, no version), or the toolchain needed to rebuild it is
    absent. This is honest degradation, not a pass and not a crash: a tier with
    no recorded evidence cannot be a mismatch, and it never silently reads as OK.

An unverifiable tier is not a pass either. A rebuild with no MISMATCH but with
tiers that checked nothing is reported as *partially* reproduced, and
`--strict` exits non-zero on it: "nothing diverged" is a much weaker claim than
"everything agreed", and the two must never be printed the same way.

Nothing here invents a hash scheme. The source/manifest hashes are exactly
`registry._sha256` over exactly the bytes `registry.build_index` records; the IR
document is exactly `registry._audit_document`; the attestation check is exactly
`revl.attest`. `truc reproduce` is a *verifier*: it recompiles and compares, and
it never mutates truc state, the registry, or the lock.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import RevlError

# Tier outcomes.
OK = "OK"
MISMATCH = "MISMATCH"
UNVERIFIED = "cannot verify"

# The tiers, in the order item 297 names them, so the report reads the same
# every run.
TIER_SOURCE = "source"
TIER_ANCHOR = "independent pin"
TIER_LOCK = "dependency lock"
TIER_IR = "IR"
TIER_POLICY = "policy surface"
TIER_BACKEND = "backend version"
TIER_ATTESTATION = "attestation"
TIER_ARTIFACT = "emitted artifact"


def _short(digest: str) -> str:
    """First 16 hex chars of a digest, for the human report. The full value is
    still compared; only the display is truncated."""
    return digest[:16] if digest else "(none)"


@dataclass
class Check:
    """One tier's outcome. `status` is OK / MISMATCH / UNVERIFIED; `detail` is a
    short human line; `recorded`/`rebuilt` carry the two values a MISMATCH shows
    so a consumer sees exactly what diverged."""

    tier: str
    status: str
    detail: str = ""
    recorded: str = ""
    rebuilt: str = ""

    @property
    def is_mismatch(self) -> bool:
        return self.status == MISMATCH


@dataclass
class ReproduceReport:
    name: str
    version: str = ""
    checks: list[Check] = field(default_factory=list)

    @property
    def mismatches(self) -> list[Check]:
        return [c for c in self.checks if c.status == MISMATCH]

    @property
    def unverified(self) -> list[Check]:
        return [c for c in self.checks if c.status == UNVERIFIED]

    @property
    def ok(self) -> bool:
        """Reproduced means: no tier diverged. Unverifiable tiers (nothing
        recorded, or an absent toolchain) do not by themselves fail the rebuild -
        they are reported as such, but any MISMATCH does."""
        return not self.mismatches

    @property
    def fully_verified(self) -> bool:
        """Every tier actually ran and agreed. `ok` alone is NOT proof of
        reproduction: a tier that could not verify anything contributes no
        MISMATCH, so a report with unverifiable tiers passes `ok` while having
        checked less than it claims. The verdict and the exit code under
        `--strict` read this, so a dead tier can never quietly count as a pass.
        """
        return not self.mismatches and not self.unverified

    @property
    def verdict(self) -> str:
        if self.mismatches:
            return "not reproduced"
        return "reproduced" if self.fully_verified else "partially reproduced"


# --------------------------------------------------------------- resolution

def _split_spec(spec: str) -> tuple[str, str]:
    """`name@version` -> (name, version). No `@` means no version pin was asked
    for. Split on the last `@` so a name is never mistaken for a version."""
    if "@" in spec:
        name, _, version = spec.rpartition("@")
        return name, version
    return spec, ""


def _resolve_registry(project_dir: str, registry_flag: str | None) -> str:
    """The registry directory to reproduce against: an explicit `--registry`
    wins; otherwise the default registry declared in the project's `truc.toml`
    (the same resolution `truc add` uses, via the host TOML reader). Raises
    `RevlError` when neither resolves, an honest "where would I even look",
    never a crash."""
    if registry_flag:
        path = os.path.abspath(registry_flag)
        if not os.path.isdir(path):
            raise RevlError(path, 0, f"registry directory does not exist: {path}")
        return path
    toml = Path(project_dir, "truc.toml")
    if not toml.exists():
        raise RevlError(
            str(toml), 0,
            "no truc.toml in this directory and no --registry given: cannot "
            "tell which registry to reproduce against")
    from ._host import toml_manifest  # noqa: PLC0415, reuse truc's TOML reader

    manifest = json.loads(toml_manifest(project_dir))
    reg = manifest.get("registry") or ""
    if not reg or not os.path.isdir(reg):
        raise RevlError(
            str(toml), 0,
            "truc.toml declares no reachable registry (a local registry needs "
            "a [registries] path that exists on disk)")
    return reg


def _load_index_row(registry: str, name: str) -> dict:
    """The registry index row for `name` (the recorded surface hashes + the
    provides/requires lock), or raise if the component is not published there."""
    index_path = Path(registry, "index.json")
    if not index_path.exists():
        raise RevlError(str(index_path), 0,
                        f"registry has no index.json: {registry}")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    row = (index.get("components") or {}).get(name)
    if row is None:
        available = ", ".join(sorted((index.get("components") or {}).keys())) or "(none)"
        raise RevlError(str(index_path), 0,
                        f"component '{name}' is not in this registry "
                        f"(available: {available})")
    row = dict(row)
    row["_indexVersion"] = index.get("indexVersion", "0")
    return row


def _lock_row(project_dir: str, name: str) -> dict | None:
    """The project's `truc.lock` pin for `name`, if this component was `add`ed
    into the composition, the recorded dependency lock. Absent (the component
    is not a vendored truc of this project) returns None; the reproduce still
    runs against the registry row."""
    lock_path = Path(project_dir, "truc.lock")
    if not lock_path.exists():
        return None
    text = lock_path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    try:
        lock = json.loads(text)
    except json.JSONDecodeError:
        return None
    for r in lock.get("trucs") or []:
        if r.get("name") == name:
            return r
    return None


# --------------------------------------------------------------- tier checks

def _check_source(source: str, row: dict) -> Check:
    """Rebuild the source hash from the recorded `component.rvl` bytes and
    compare against the `sourceHash` the registry index recorded. This is a
    registry-internal check; the independent one is `_check_anchor`."""
    from ..registry import _sha256  # noqa: PLC0415, the exact recorded scheme

    rebuilt = _sha256(source)
    recorded = row.get("sourceHash") or ""
    if not recorded:
        return Check(TIER_SOURCE, UNVERIFIED, "no recorded source hash")
    if rebuilt != recorded:
        return Check(TIER_SOURCE, MISMATCH,
                     "recorded source hash and rebuilt source hash differ",
                     recorded, rebuilt)
    # The truc.lock cross-check is NOT part of this tier: every check here
    # compares the registry against itself, so it agrees by construction with a
    # registry that regenerated its own index over substituted source. The
    # independent anchor gets its own tier (`_check_anchor`) so its absence is
    # visible instead of implied.
    return Check(TIER_SOURCE, OK, f"sha256 {_short(rebuilt)}", recorded, rebuilt)


def _check_anchor(project_dir: str, name: str, source: str,
                  lock: dict | None) -> Check:
    """Cross-check the registry's source against something the REGISTRY DID NOT
    WRITE: the project's own `truc.lock` pin, and the bytes vendored under
    `trucs/<name>/`.

    Every other tier compares the registry against itself. Regenerate the index
    over substituted source and all of them reproduce green - which is exactly
    the shape of a supply-chain substitution where the registry is the adversary.
    The lock pin was recorded when the component was added, from a registry the
    project trusted at that moment, so it is the one value here that a later
    registry cannot mint for itself.

    A vendored truc with no pin, or a pin left blank, is a MISMATCH and not a
    shrug: an unpinned dependency is precisely the state a substitution needs.
    A component that is not a truc of this project has no anchor at all, and that
    is reported `cannot verify` - honest about what was and was not checked.
    """
    from ..registry import _sha256  # noqa: PLC0415

    rebuilt = _sha256(source)
    pin = (lock or {}).get("sourceHash") or ""
    vendor_path = Path(project_dir, "trucs", name, "component.rvl")
    vendored = (vendor_path.read_text(encoding="utf-8")
                if vendor_path.exists() else None)

    if lock is None and vendored is None:
        return Check(TIER_ANCHOR, UNVERIFIED,
                     f"'{name}' is not a truc of this project: there is no "
                     "truc.lock pin to check the registry against, so every "
                     "other tier compared the registry only with itself")
    if lock is None:
        return Check(TIER_ANCHOR, MISMATCH,
                     f"trucs/{name}/ is vendored but carries no truc.lock row: "
                     "nothing independent pins these bytes")
    if not pin:
        return Check(TIER_ANCHOR, MISMATCH,
                     f"the truc.lock row for '{name}' carries a blank "
                     "sourceHash: nothing independent pins these bytes")
    if pin != rebuilt:
        return Check(TIER_ANCHOR, MISMATCH,
                     "the truc.lock pin disagrees with the registry's source: "
                     "the published component is not the one this project "
                     "locked (a substituted dependency)",
                     pin, rebuilt)
    if vendored is not None and _sha256(vendored) != pin:
        return Check(TIER_ANCHOR, MISMATCH,
                     f"the bytes vendored at trucs/{name}/component.rvl do not "
                     "match the truc.lock pin",
                     pin, _sha256(vendored))
    where = "pin matches the registry source and the vendored copy" \
        if vendored is not None else "pin matches the registry source"
    return Check(TIER_ANCHOR, OK, f"truc.lock {where}", pin, rebuilt)


def _surface(ir: dict) -> tuple[dict, dict]:
    """The provides/requires surface the compiler reads off an IR, the same
    projection `registry._entry_index_row` records as the lock's dependency
    surface."""
    provides: dict = {}
    requires: dict = {}
    for comp in ir.get("components") or []:
        provides.update(comp.get("provides") or {})
        requires.update(comp.get("requires") or {})
    return provides, requires


def _check_lock(ir: dict, row: dict) -> Check:
    """Rebuild the provides/requires dependency surface off the IR and compare
    against the recorded lock surface in the index row."""
    provides, requires = _surface(ir)
    rec_provides = row.get("provides") or {}
    rec_requires = row.get("requires") or {}
    if provides != rec_provides or requires != rec_requires:
        return Check(
            TIER_LOCK, MISMATCH,
            "the rebuilt provides/requires surface differs from the recorded lock",
            json.dumps({"provides": rec_provides, "requires": rec_requires},
                       sort_keys=True),
            json.dumps({"provides": provides, "requires": requires},
                       sort_keys=True))
    detail = f"provides={json.dumps(provides, sort_keys=True)} " \
             f"requires={json.dumps(requires, sort_keys=True)}"
    return Check(TIER_LOCK, OK, detail)


def _check_ir(manifest_text: str, row: dict, entry_dir: Path) -> Check:
    """Re-derive the manifest (the item-28 audit document) hash from the rebuilt
    IR and compare against the recorded `manifestHash`; also compare byte-for-byte
    against the recorded `manifest.json` when the entry carries one."""
    from ..registry import _sha256  # noqa: PLC0415

    rebuilt = _sha256(manifest_text)
    recorded = row.get("manifestHash") or ""
    if not recorded:
        return Check(TIER_IR, UNVERIFIED, "no recorded manifest hash")
    if rebuilt != recorded:
        return Check(TIER_IR, MISMATCH,
                     "recorded IR (manifest) hash and rebuilt hash differ",
                     recorded, rebuilt)
    committed = entry_dir / "manifest.json"
    if committed.exists() and committed.read_text(encoding="utf-8") != manifest_text:
        return Check(TIER_IR, MISMATCH,
                     "the recorded manifest.json is not byte-reproducible from "
                     "the source by the current compiler",
                     recorded, rebuilt)
    return Check(TIER_IR, OK, f"manifest {_short(rebuilt)}", recorded, rebuilt)


def _policy_of(manifest: dict) -> dict:
    """The boundary a composition crosses, its emissions and capability labels
   , read off the rebuilt manifest. This is the surface G4 admits against, and
    the surface a consumer trusts a `ship` to have frozen."""
    from ..registry import _capabilities_of  # noqa: PLC0415

    caps, emissions = _capabilities_of(manifest.get("boundary") or {})
    return {"capabilities": list(caps), "emissions": emissions}


def _check_policy(manifest: dict, row: dict) -> Check:
    """Rebuild the policy surface (capabilities + emission count) off the IR's
    manifest and compare against the recorded surface in the index row."""
    policy = _policy_of(manifest)
    rec = {"capabilities": row.get("capabilities") or [],
           "emissions": row.get("emissions", 0)}
    if policy != rec:
        return Check(TIER_POLICY, MISMATCH,
                     "the rebuilt policy surface (capabilities/emissions) differs "
                     "from the recorded one",
                     json.dumps(rec, sort_keys=True),
                     json.dumps(policy, sort_keys=True))
    detail = f"emissions={policy['emissions']} " \
             f"capabilities={json.dumps(policy['capabilities'])}"
    return Check(TIER_POLICY, OK, detail)


def _check_backend_version(entry_dir: Path) -> Check:
    """Compare the interchange `schema_version` the RECORDED manifest.json was
    stamped with against the version the current compiler stamps. A drift here
    means the artifact was frozen by a different toolchain surface than the one
    reproducing it, a reported MISMATCH with both versions, never a silent pass.
    Read from the committed manifest.json (the recorded value), not the rebuilt
    one, so the check can actually diverge."""
    from ..interchange import INTERCHANGE_VERSION  # noqa: PLC0415

    committed = entry_dir / "manifest.json"
    if not committed.exists():
        return Check(TIER_BACKEND, UNVERIFIED,
                     "no recorded manifest.json to read a backend version from")
    try:
        recorded = json.loads(committed.read_text(encoding="utf-8")).get("schema_version")
    except json.JSONDecodeError:
        return Check(TIER_BACKEND, UNVERIFIED,
                     "the recorded manifest.json is unreadable")
    if not recorded:
        return Check(TIER_BACKEND, UNVERIFIED,
                     "the recorded manifest carries no schema_version")
    current = INTERCHANGE_VERSION
    if recorded != current:
        return Check(TIER_BACKEND, MISMATCH,
                     "recorded backend (interchange) version differs from the "
                     "current toolchain",
                     f"interchange {recorded}", f"interchange {current}")
    return Check(TIER_BACKEND, OK, f"interchange {current}",
                 f"interchange {recorded}", f"interchange {current}")


def _normalized_ir(ir: dict) -> dict:
    """The canonical (path-normalized) spelling of a compiled IR that an
    attestation binds — a deep copy with every cwd-dependent path reduced to its
    basename, so the composition hash is a pure function of the source and not
    of where the entry was compiled.

    This is `registry._normalize_ir_for_attest` itself, called rather than
    re-implemented. It used to be a near-copy that normalized only
    `manifest.components[].file` and missed `components[].source`, so a
    reproduce could never match an attestation `registry.build_evidence` had
    signed. Nothing caught it because the attestation tier was reading a path
    the publisher never writes: a dead check cannot fail, and it cannot notice
    two definitions drifting apart either. One definition, one hash.
    """
    from ..registry import _normalize_ir_for_attest  # noqa: PLC0415

    return _normalize_ir_for_attest(ir)


def _attestation_path(entry_dir: Path) -> Path | None:
    """Where the entry's attestation actually lives, or None.

    `registry.build_evidence` publishes it at `<entry>/evidence/attestation.json`
    - the item-293 bundle path. This tier used to read `<entry>/attestation.json`,
    a path nothing writes, so for every entry the publish path produces the tier
    was structurally DEAD: it could only ever report "no recorded attestation",
    and an unverifiable tier used to read as a pass. The bundle path is checked
    first; the bare root path is still honoured as the legacy spelling for
    entries published before item 293.
    """
    from ..registry import EVIDENCE_ATTESTATION, EVIDENCE_DIRNAME  # noqa: PLC0415

    for candidate in (entry_dir / EVIDENCE_DIRNAME / EVIDENCE_ATTESTATION,
                      entry_dir / EVIDENCE_ATTESTATION):
        if candidate.exists():
            return candidate
    return None


def _check_attestation(entry_dir: Path, ir: dict, env) -> Check:
    """When the entry carries an attestation, verify it with `revl.attest`
    against the rebuilt IR: the signature proves the record is authentic and
    untampered, the IR-hash proves the composition did not change since it was
    signed, and the signed per-facet bindings (item 290, §6.2) prove the evidence
    dossiers riding alongside it are the ones that were signed. No attestation,
    or no signing key available, is `cannot verify` - honest, not a pass."""
    from .. import attest  # noqa: PLC0415, item 127, the exact attest scheme
    from ..registry import (EVIDENCE_ATTESTATION,  # noqa: PLC0415
                            EVIDENCE_DIRNAME, binding_mismatch,
                            load_evidence_bundle)

    att_path = _attestation_path(entry_dir)
    if att_path is None:
        return Check(TIER_ATTESTATION, UNVERIFIED,
                     "no recorded attestation at "
                     f"{EVIDENCE_DIRNAME}/{EVIDENCE_ATTESTATION}")
    try:
        att = attest.load_attestation(str(att_path))
    except RevlError as error:
        return Check(TIER_ATTESTATION, MISMATCH,
                     f"recorded attestation is unreadable: {error.message}")
    try:
        key = attest.resolve_key(None, env=env)
    except RevlError:
        return Check(TIER_ATTESTATION, UNVERIFIED,
                     "an attestation is recorded but no signing key is available "
                     f"(set {attest.KEY_ENV} or {attest.KEY_FILE_ENV}) to verify it")
    ok, reason = attest.verify_attestation(att, key, ir)
    if not ok:
        return Check(TIER_ATTESTATION, MISMATCH, reason,
                     att.get("composition_hash", ""), attest.canonical_hash(ir))
    facet = binding_mismatch(att, load_evidence_bundle(entry_dir))
    if facet is not None:
        return Check(TIER_ATTESTATION, MISMATCH,
                     f"the signature is authentic, but the '{facet}' dossier it "
                     "binds is missing or no longer hashes to the signed value",
                     att.get("composition_hash", ""), attest.canonical_hash(ir))
    return Check(TIER_ATTESTATION, OK, "authentic; IR matches the signed hash",
                 att.get("composition_hash", ""), attest.canonical_hash(ir))


def _load_artifact_record(entry_dir: Path) -> dict | None:
    """The optional `artifacts.json` an entry may record: per-backend emitted
    artifact hashes (and the tool version that produced them). Absent means the
    published entry recorded no emitted artifact, the very gap this tier reports
    honestly rather than glossing over."""
    path = entry_dir / "artifacts.json"
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    backends = doc.get("backends")
    return backends if isinstance(backends, dict) else None


def _emit_backend_source(backend: str, ir: dict) -> str | None:
    """Re-emit `backend` source from the rebuilt IR through that backend's own
    emitter, or None when the toolchain is absent/does not expose a pure
    `emit(ir)` (the tier then degrades with a reason, mirroring the rest of the
    repo). Never touches the backend package's internals, it calls the same
    top-level `emit` the emit tests use."""
    try:
        module = importlib.import_module(f"backends.{backend}.emit")
    except ImportError:
        return None
    emit = getattr(module, "emit", None)
    if emit is None:
        return None
    return emit(ir)


def _check_artifacts(entry_dir: Path, ir: dict) -> list[Check]:
    """Reproduce each recorded emitted artifact: re-emit the backend source from
    the rebuilt IR and compare its hash against the recorded one. A backend whose
    toolchain is absent is skipped with a reason; a recorded hash that differs is
    a MISMATCH with both hashes. No recorded artifacts at all is one honest
    `cannot verify` line."""
    from ..registry import _sha256  # noqa: PLC0415

    record = _load_artifact_record(entry_dir)
    if not record:
        return [Check(TIER_ARTIFACT, UNVERIFIED, "no recorded emitted artifact")]
    checks: list[Check] = []
    for backend in sorted(record):
        spec = record[backend] or {}
        recorded_hash = spec.get("sourceSha256") or ""
        label = f"{TIER_ARTIFACT} [{backend}]"
        if not recorded_hash:
            checks.append(Check(label, UNVERIFIED,
                                f"{backend}: recorded artifact has no sourceSha256"))
            continue
        emitted = _emit_backend_source(backend, ir)
        if emitted is None:
            checks.append(Check(label, UNVERIFIED,
                                f"{backend} emitter toolchain is not available "
                                "here; artifact not re-emitted"))
            continue
        rebuilt = _sha256(emitted)
        # an optional recorded tool version is a reported backend mismatch.
        rec_tool = spec.get("toolVersion")
        if rec_tool and rec_tool != _toolchain_version():
            checks.append(Check(label, MISMATCH,
                                f"{backend}: recorded tool version differs from "
                                "the current toolchain",
                                str(rec_tool), _toolchain_version()))
            continue
        if rebuilt != recorded_hash:
            checks.append(Check(label, MISMATCH,
                                f"{backend}: re-emitted artifact hash differs from "
                                "the recorded one",
                                recorded_hash, rebuilt))
            continue
        checks.append(Check(label, OK, f"{backend} {_short(rebuilt)}",
                            recorded_hash, rebuilt))
    return checks


def _toolchain_version() -> str:
    """The current revl toolchain version, the pyproject version, read through
    the installed metadata when available, else a stable fallback. Used only to
    compare against a recorded `toolVersion`, never to pin one."""
    try:
        from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415
        try:
            return version("revl")
        except PackageNotFoundError:
            return "2.0.0"
    except ImportError:
        return "2.0.0"


# --------------------------------------------------------------- driver

def reproduce(spec: str, *, project_dir: str = ".", registry: str | None = None,
              env=None) -> ReproduceReport:
    """Reproduce the published component named by `spec` (`name` or
    `name@version`) and return a tier-by-tier report. Pure with respect to truc
    state, it recompiles and compares, and writes nothing."""
    if env is None:
        env = os.environ
    name, requested_version = _split_spec(spec)
    if not name:
        raise RevlError("<reproduce>", 0, "no component named (expected "
                        "`truc reproduce <component@version>`)")

    reg = _resolve_registry(project_dir, registry)
    row = _load_index_row(reg, name)
    entry_dir = Path(reg, "components", name)

    report = ReproduceReport(name=name, version=requested_version)

    # version: the registry index carries no per-component version today, so a
    # requested `@version` is honestly unverifiable rather than a false match.
    recorded_version = row.get("version")
    if requested_version:
        if recorded_version is None:
            report.checks.append(Check(
                "version", UNVERIFIED,
                f"no version is recorded for '{name}' in this registry; "
                f"reproduced the published '{name}' regardless"))
        elif recorded_version != requested_version:
            raise RevlError(str(entry_dir), 0,
                            f"'{name}@{requested_version}' is not published here "
                            f"(recorded version is {recorded_version})")

    source_path = entry_dir / "component.rvl"
    if not source_path.exists():
        raise RevlError(str(source_path), 0,
                        f"the registry index lists '{name}' but its "
                        "component.rvl is missing, the entry is incomplete")
    source = source_path.read_text(encoding="utf-8")
    lock = _lock_row(project_dir, name)

    # source tier, hash the recorded bytes; then the one check the registry
    # cannot satisfy on its own - the project's independent truc.lock pin.
    report.checks.append(_check_source(source, row))
    report.checks.append(_check_anchor(project_dir, name, source, lock))

    # rebuild the IR + manifest through the normal pipeline (frontend-only, no
    # runtime needed, the same path registry.build_index runs).
    from ..compiler import compile_files  # noqa: PLC0415
    from ..registry import _audit_document  # noqa: PLC0415

    try:
        ir = compile_files([str(source_path)])
    except RevlError as error:
        report.checks.append(Check(
            TIER_IR, MISMATCH,
            f"the recorded source no longer admits: {error.message}"))
        return report

    # Snapshot the canonical (path-normalized) IR for the attestation hash
    # BEFORE `_audit_document` mutates `ir`'s file paths in place, so the
    # attestation binds a location-independent IR.
    attest_ir = _normalized_ir(ir)

    manifest = _audit_document(ir)
    manifest_text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"

    report.checks.append(_check_lock(ir, row))
    report.checks.append(_check_ir(manifest_text, row, entry_dir))
    report.checks.append(_check_policy(manifest, row))
    report.checks.append(_check_backend_version(entry_dir))
    report.checks.append(_check_attestation(entry_dir, attest_ir, env))
    report.checks.extend(_check_artifacts(entry_dir, ir))
    return report


def render(report: ReproduceReport) -> str:
    """The human report, one aligned line per tier, then a verdict."""
    title = report.name
    if report.version:
        title = f"{report.name}@{report.version}"
    lines = [f"truc reproduce {title}", ""]
    width = max((len(c.tier) for c in report.checks), default=0)
    for c in report.checks:
        if c.status == OK:
            lines.append(f"  {c.tier.ljust(width)}  OK        {c.detail}")
        elif c.status == MISMATCH:
            lines.append(f"  {c.tier.ljust(width)}  MISMATCH  {c.detail}")
            if c.recorded or c.rebuilt:
                lines.append(f"  {' '.ljust(width)}            recorded "
                             f"{_short(c.recorded)}, rebuilt {_short(c.rebuilt)}")
        else:
            lines.append(f"  {c.tier.ljust(width)}  --        {c.detail}")
    lines.append("")
    n_ok = sum(1 for c in report.checks if c.status == OK)
    n_mismatch = len(report.mismatches)
    n_unver = len(report.unverified)
    if report.fully_verified:
        lines.append(
            f"reproduced: {report.name} rebuilds bit-for-bit to what was "
            f"published ({n_ok} OK, {n_unver} unverifiable, {n_mismatch} mismatch)")
    elif report.ok:
        # No tier diverged, but not every tier ran. Say that plainly: a check
        # that verified nothing is not a check that passed.
        blind = ", ".join(c.tier for c in report.unverified)
        lines.append(
            f"partially reproduced: {report.name} matched every tier that could "
            f"be checked, but {n_unver} could not be verified at all ({blind}) "
            f"- this is not proof of reproduction "
            f"({n_ok} OK, {n_unver} unverifiable, {n_mismatch} mismatch)")
    else:
        diverged = ", ".join(c.tier for c in report.mismatches)
        lines.append(
            f"NOT reproduced: {report.name} diverged on {diverged} "
            f"({n_ok} OK, {n_unver} unverifiable, {n_mismatch} mismatch)")
    return "\n".join(lines)


def run(argv: list[str]) -> int:
    """`truc reproduce <component@version>` entry point. Returns 0 when the
    component reproduces (no tier diverged), 1 on any MISMATCH, 2 on a usage or
    resolution error. With `--strict`, a tier that could verify nothing is not a
    pass either: the exit code is 1 unless every tier actually ran and agreed."""
    parser = argparse.ArgumentParser(
        prog="truc reproduce",
        description="rebuild a published component and verify it is bit-for-bit "
                    "the same as what was published")
    parser.add_argument("component", nargs="?",
                        help="the component to reproduce, `name` or `name@version`")
    parser.add_argument("--registry", metavar="PATH",
                        help="reproduce against this registry directory instead "
                             "of the one declared in truc.toml")
    parser.add_argument("--json", action="store_true",
                        help="print the tier-by-tier report as JSON")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero unless EVERY tier verified: a tier "
                             "that could check nothing is not a pass")
    args = parser.parse_args(argv)

    if not args.component:
        parser.print_usage()
        print("truc reproduce: a component name is required "
              "(`truc reproduce <component@version>`)")
        return 2

    try:
        report = reproduce(args.component, registry=args.registry)
    except RevlError as error:
        print(f"truc reproduce: cannot verify: {error.message}")
        return 2

    if args.json:
        print(json.dumps({
            "name": report.name,
            "version": report.version,
            "reproduced": report.ok,
            "verdict": report.verdict,
            "fullyVerified": report.fully_verified,
            "unverified": [c.tier for c in report.unverified],
            "checks": [
                {"tier": c.tier, "status": c.status, "detail": c.detail,
                 "recorded": c.recorded, "rebuilt": c.rebuilt}
                for c in report.checks
            ],
        }, indent=2))
    else:
        print(render(report))
    if args.strict:
        return 0 if report.fully_verified else 1
    return 0 if report.ok else 1
