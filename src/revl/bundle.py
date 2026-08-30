"""`revl bundle` / `revl verify`, the reproducible production bundle
(roadmap item 305).

A revl composition proves a great deal at compile time, its source hashes,
its admitted IR, its dependency surface, its capability policy, a signed
attestation (item 127), a graded gauntlet dossier (item 31). Until now those
proofs lived in different places: the registry index, an `attest` file, an
`audit --json` document, a truc lock. `revl bundle` assembles all of it into
ONE directory a consumer can carry and check offline:

    app.revlbundle/
      source/                 the .rvl sources, verbatim
      ir/ir.json             the compiled IR (path-normalized, deterministic)
      ir/manifest.json       the item-28 audit/interchange document
      components.lock         the provides/requires dependency surface + hashes
      emitted/<backend>/...   each backend's emitted artifact
      policy.json             the capability/emission surface the boundary crosses
      attestation.json        the item-127 signed record (when a key is available)
      gauntlet.json           the item-31 graded dossier (evidence it was admitted)
      topology.json           the placement map (only when one is provided)
      runtime-manifest.json   the bundle's own manifest, the recorded surface

`revl verify app.revlbundle` recompiles the bundled source through the normal
pipeline and compares, tier by tier, against what the bundle recorded:

  * **source**, the committed source bytes re-hash to the recorded hash.
  * **IR**, the recompiled audit document is byte-reproducible from the source
    by the current compiler, and hashes to the recorded value.
  * **dependency lock**, the rebuilt provides/requires surface equals the lock.
  * **policy surface**, the rebuilt capabilities/emissions equal policy.json.
  * **emitted [backend]**, each backend re-emits byte-for-byte to the committed
    artifact (the emitted output *corresponds to* that backend).
  * **backend version**, the recorded interchange schema version matches the
    current toolchain.
  * **attestation**, when present and a key is available, the item-127
    signature is authentic and binds the rebuilt IR (item 127).
  * **gauntlet**, the evidence is present and records an `admissible` verdict.
  * **topology**, a placement map, when the bundle carries one.
  * **reproducible**, the bundle rebuilds bit-for-bit (the aggregate).

Every tier reports one of three outcomes, honestly (the same vocabulary item
297 established, and the same `Check` type reused verbatim):

  * **OK**, recomputed value equals the recorded one.
  * **MISMATCH**, they differ; both values are printed so a consumer sees what
    diverged.
  * **cannot verify**, nothing was recorded for that tier (no attestation, no
    topology), or the toolchain needed to check it is absent. Honest
    degradation, not a pass and not a crash.

Nothing here invents a hash scheme. Source and manifest hashes are exactly
`registry._sha256`; the IR document is exactly `registry._audit_document`; the
composition hash and the attestation are exactly `revl.attest` (item 127); the
dependency surface and policy projections are exactly the item-297
`truc.reproduce` primitives, imported, not re-derived. `revl bundle` writes the
bundle; `revl verify` recompiles and compares. Neither touches truc state, the
registry, or any lock.

Design decisions (documented in docs/bundle.md):

  * **Manifest schema**, `runtime-manifest.json` is a plain, versioned JSON
    document (`kind`/`version`), deterministic (no timestamps), so the whole
    bundle rebuilds bit-for-bit. Evidence that carries its own timestamp
    (attestation, gauntlet) lives in its own file and is verified by signature
    or verdict, never by byte-reproduction.
  * **Signing**, the attestation reuses item 127's HMAC-SHA256 scheme and its
    key resolution (`REVL_ATTEST_KEY` / `REVL_ATTEST_KEY_FILE` / `--key`). When
    no key is available the bundle is still produced, minus `attestation.json`;
    `verify` then reports the attestation tier as `cannot verify`, never a pass.
  * **Backends**, every backend whose emitter is a pure in-repo function is
    emitted by default; `--backend` narrows the set. An emitter that refuses
    this IR (e.g. wasm on floats) is recorded as skipped, not a bundle failure.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

from .errors import RevlError

# Reuse the item-297 reproduce vocabulary and projections verbatim, no new
# status scheme, no new surface derivation.
from .truc.reproduce import (
    OK, MISMATCH, UNVERIFIED, Check, _policy_of, _short, _surface,
)

# The bundle envelope identity (mirrors the interchange/attest kind+version
# idea: a self-identifying tag plus a MAJOR.MINOR line, additive within MAJOR).
BUNDLE_KIND = "revl.bundle"
BUNDLE_VERSION = "1.0"

RUNTIME_MANIFEST = "runtime-manifest.json"
LOCK_NAME = "components.lock"
POLICY_NAME = "policy.json"
ATTESTATION_NAME = "attestation.json"
GAUNTLET_NAME = "gauntlet.json"
TOPOLOGY_NAME = "topology.json"

# Backend directory names (under backends/), the emitter package name is the
# canonical label the bundle records emitted artifacts under.
DEFAULT_BACKENDS = ("python", "typescript", "rust", "java", "go", "wasm")

# The single-file emitters write one artifact under this name; the wasm emitter
# returns a {module: text} map and names each file <module>.wat itself.
_SINGLE_FILE = {
    "python": "components.py",
    "typescript": "components.ts",
    "rust": "components.rs",
    "java": "Components.java",
    "go": "components.go",
}

_BACKENDS_ROOT = Path(__file__).resolve().parent.parent.parent / "backends"


# --------------------------------------------------------------- IR canonical form

def _canonical_ir(ir: dict) -> dict:
    """A deep copy of the compiled IR with every cwd-dependent source path
    reduced to its basename, the same path normalization
    `registry._audit_document` and `truc.reproduce._normalized_ir` apply, extended
    to the top-level `components[*].source`/`file` the compiler stamps with a
    path relative to the working directory. A bundle is built and verified from
    two different directories (the input tree, then the bundle's own `source/`),
    so unless these paths are normalized the recompiled IR, and the attestation
    hash taken over it, would differ by location alone. Everything the bundle
    derives comes from this form, so a bundle built in one directory verifies
    from another."""
    out = copy.deepcopy(ir)
    for comp in out.get("components") or []:
        for field in ("source", "file"):
            if comp.get(field):
                comp[field] = os.path.basename(comp[field])
    for comp in (out.get("manifest") or {}).get("components") or []:
        if comp.get("file"):
            comp["file"] = os.path.basename(comp["file"])
    return out


def _ir_text(norm_ir: dict) -> str:
    """The deterministic on-disk spelling of the compiled IR document."""
    return json.dumps(norm_ir, indent=2, sort_keys=True) + "\n"


def _manifest_text(norm_ir: dict) -> str:
    """The item-28 audit/interchange document for the IR, in the exact byte
    spelling `registry.build_index` records (so a bundle's manifest.json is the
    same document a registry entry would carry)."""
    from .registry import _audit_document  # noqa: PLC0415, the exact recorded scheme

    return json.dumps(_audit_document(copy.deepcopy(norm_ir)),
                      indent=2, sort_keys=True) + "\n"


# --------------------------------------------------------------- backend emit

_EMITTERS: dict = {}


def _emitter(backend: str):
    """Load a backend emitter module under a unique name (a bare `import emit`
    collides across backends). Mirrors `revl.test._emitter`."""
    if backend not in _EMITTERS:
        path = _BACKENDS_ROOT / backend / "emit.py"
        if not path.exists():
            return None
        spec = importlib.util.spec_from_file_location(f"revl_bundle_{backend}_emit", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _EMITTERS[backend] = module
    return _EMITTERS[backend]


def _emit_files(backend: str, norm_ir: dict) -> dict[str, str] | None:
    """Emit `backend` source from the IR as a {filename: text} map, or None when
    the emitter is absent or refuses this IR. A single-string emitter is wrapped
    under the backend's conventional filename; the wasm emitter already returns a
    per-module map, whose keys become `<module>.wat`."""
    module = _emitter(backend)
    if module is None or not hasattr(module, "emit"):
        return None
    try:
        emitted = module.emit(copy.deepcopy(norm_ir))
    except Exception:  # noqa: BLE0001, an emitter that refuses this IR is a skip, not a crash
        return None
    if isinstance(emitted, dict):
        return {f"{name}.wat": text for name, text in sorted(emitted.items())}
    if isinstance(emitted, str):
        return {_SINGLE_FILE.get(backend, f"components.{backend}"): emitted}
    return None


# --------------------------------------------------------------- lock / policy

def _lock_document(source_hash: str, manifest_hash: str, norm_ir: dict) -> dict:
    """The `components.lock` surface: the provides/requires dependency edges the
    compiler reads off the IR (the item-297 `_surface` projection, verbatim),
    pinned alongside the source and manifest hashes so a consumer knows exactly
    what is locked."""
    provides, requires = _surface(norm_ir)
    return {
        "kind": "revl.components.lock",
        "version": BUNDLE_VERSION,
        "provides": provides,
        "requires": requires,
        "sourceHash": source_hash,
        "manifestHash": manifest_hash,
    }


# --------------------------------------------------------------- build

def _sha256(text: str) -> str:
    """The recorded hash scheme, verbatim from `registry._sha256`."""
    from .registry import _sha256 as reg_sha256  # noqa: PLC0415

    return reg_sha256(text)


def _load_topology(path: str) -> dict:
    """Read a placement/topology map (TOML or JSON) into a plain dict, normalized
    to a stable JSON spelling for the bundle. A `.toml` map is read through the
    same host reader `revl run --placement` uses."""
    text = Path(path).read_text(encoding="utf-8")
    if path.endswith(".toml"):
        try:
            import tomllib  # noqa: PLC0415
        except ModuleNotFoundError as error:  # pragma: no cover, Python < 3.11
            raise RevlError(path, 0,
                            "reading a .toml topology needs Python 3.11+ tomllib; "
                            "pass a .json placement map instead") from error
        return tomllib.loads(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise RevlError(path, 0, f"topology is not valid JSON: {error}") from error


def _gauntlet_dossier(source: str) -> dict | None:
    """The item-31 gauntlet dossier for the source, the graded evidence that the
    composition is admissible (boots and unloads clean). Boots the candidate in a
    throwaway `Session` on a worker thread (the gauntlet drives its own asyncio
    loop, which a bare call from an already-running loop would refuse). Returns
    None when the gauntlet machinery is unavailable, honest degradation, the
    bundle is still produced without gauntlet evidence."""
    import concurrent.futures  # noqa: PLC0415

    try:
        from .mcp import gauntlet  # noqa: PLC0415
        from .mcp.session import Session  # noqa: PLC0415
    except ImportError:
        return None

    # Emitting/booting the py tier needs the cordis-py runtime on the path.
    backend_py = str(_BACKENDS_ROOT / "python")
    if backend_py not in sys.path:
        sys.path.insert(0, backend_py)

    def _grade() -> dict:
        return gauntlet.run(Session(), {"source": source})

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_grade).result()
    except Exception:  # noqa: BLE0001, the gauntlet never raises by contract; be safe anyway
        return None


def build_bundle(sources: list[str], out: str, *, backends=DEFAULT_BACKENDS,
                 topology: str | None = None, env=None) -> str:
    """Assemble a `.revlbundle` directory from `sources` and return its path.

    Compiles the sources once through the normal pipeline, refuses a draft (open
    holes are not admitted, a bundle is a production artifact), then writes the
    source, IR, manifest, lock, per-backend emitted artifacts, policy, evidence
    (attestation when a key is available, gauntlet dossier), an optional topology
    map, and the bundle's own runtime-manifest. Everything reproducible is
    written deterministically so `verify` can rebuild it bit-for-bit."""
    if env is None:
        env = os.environ
    if not sources:
        raise RevlError("<bundle>", 0, "no source files given to bundle")

    from .compiler import compile_files  # noqa: PLC0415

    try:
        ir = compile_files(list(sources))
    except RevlError:
        raise
    holes = ir.get("holes") or []
    if holes:
        raise RevlError(
            sources[0], 0,
            f"composition has {len(holes)} open hole(s), it is a draft, not "
            "admitted, and cannot be bundled (fill the holes, docs/holes.md)")
    # item 396 stage 4 (interim clean refusal): `revl bundle` copies sources
    # flat by basename (below) and `revl verify` recompiles the bundled source,
    # so a root-relative external host-body file would not travel with the
    # bundle and verify could not reproduce it. Refuse cleanly, naming the gap,
    # rather than emit a bundle that cannot verify. Full body-file bundle
    # support (carrying the files under their root-relative paths and
    # re-hashing them in verify) is the follow-up.
    file_externs = sorted(
        e.get("name") for e in (ir.get("externs") or []) if e.get("body_files"))
    if file_externs:
        raise RevlError(
            sources[0], 0,
            "cannot bundle a composition whose extern splices an external "
            "host-body file (`= @backend file \"path\"`): "
            f"{', '.join(file_externs)}",
            hint="`revl bundle` copies sources flat by basename, so a "
                 "root-relative body file would not travel with the bundle and "
                 "`revl verify` could not reproduce it. Inline the body "
                 "(`= @backend { ... }`) to bundle for now; carrying body files "
                 "through the bundle is the item 396 stage 4 follow-up.")
    # item 396 option B (interim clean refusal, same stage-4 gap): a host-module
    # ref names a root-relative file that the flat basename copy would not carry,
    # so a bundled ref program could not resolve or re-hash the module at verify.
    # Refuse cleanly naming the gap rather than emit a bundle that cannot verify.
    ref_externs = sorted(
        e.get("name") for e in (ir.get("externs") or []) if e.get("refs"))
    if ref_externs:
        raise RevlError(
            sources[0], 0,
            "cannot bundle a composition whose extern references an external "
            "host module (`= @backend ref sym from \"path\"`): "
            f"{', '.join(ref_externs)}",
            hint="`revl bundle` copies sources flat by basename, so a "
                 "root-relative ref'd module would not travel with the bundle "
                 "and `revl verify` could not reproduce it. Inline the body "
                 "(`= @backend { ... }`) to bundle for now; carrying ref files "
                 "through the bundle is the item 396 stage 4 follow-up.")

    norm_ir = _canonical_ir(ir)
    ir_text = _ir_text(norm_ir)
    manifest_text = _manifest_text(norm_ir)

    out_dir = Path(out)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "source").mkdir(parents=True)
    (out_dir / "ir").mkdir()
    (out_dir / "emitted").mkdir()

    # source/, verbatim copies, keyed by basename, plus their recorded hashes.
    source_records = []
    combined_source = ""
    for path in sources:
        name = os.path.basename(path)
        text = Path(path).read_text(encoding="utf-8")
        (out_dir / "source" / name).write_text(text, encoding="utf-8")
        source_records.append({"name": name, "sha256": _sha256(text)})
        combined_source += text

    # ir/, the compiled IR and the audit/interchange manifest.
    (out_dir / "ir" / "ir.json").write_text(ir_text, encoding="utf-8")
    (out_dir / "ir" / "manifest.json").write_text(manifest_text, encoding="utf-8")

    source_hash = _sha256(combined_source)
    manifest_hash = _sha256(manifest_text)

    # components.lock, the dependency surface + hashes.
    lock = _lock_document(source_hash, manifest_hash, norm_ir)
    (out_dir / LOCK_NAME).write_text(
        json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # policy.json, the capability/emission surface the boundary crosses.
    from .registry import _audit_document  # noqa: PLC0415
    audit = _audit_document(copy.deepcopy(norm_ir))
    policy = _policy_of(audit)
    (out_dir / POLICY_NAME).write_text(
        json.dumps(policy, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # emitted/<backend>/, each backend's artifact, with recorded file hashes.
    backend_records: dict = {}
    skipped: dict = {}
    for backend in backends:
        files = _emit_files(backend, norm_ir)
        if files is None:
            skipped[backend] = "emitter unavailable or refused this IR"
            continue
        backend_dir = out_dir / "emitted" / backend
        backend_dir.mkdir()
        recs = []
        for filename in sorted(files):
            text = files[filename]
            (backend_dir / filename).write_text(text, encoding="utf-8")
            recs.append({"name": filename, "sha256": _sha256(text)})
        backend_records[backend] = {"files": recs}

    # attestation.json, the item-127 signed record, when a key is available.
    from . import attest  # noqa: PLC0415
    composition_hash = attest.canonical_hash(norm_ir)
    attested = False
    try:
        key = attest.resolve_key(None, env=env)
    except RevlError:
        key = None
    if key is not None:
        signer = env.get(attest.SIGNER_ENV)
        att = attest.make_attestation(norm_ir, key, signer=signer)
        (out_dir / ATTESTATION_NAME).write_text(
            json.dumps(att, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        attested = True

    # gauntlet.json, the item-31 graded dossier (evidence it is admissible).
    dossier = _gauntlet_dossier(combined_source)
    gauntlet_verdict = None
    if dossier is not None:
        gauntlet_verdict = str(dossier.get("verdict") or "")
        (out_dir / GAUNTLET_NAME).write_text(
            json.dumps(dossier, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # topology.json, only when a placement map is supplied.
    has_topology = False
    if topology is not None:
        topo = _load_topology(topology)
        (out_dir / TOPOLOGY_NAME).write_text(
            json.dumps(topo, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        has_topology = True

    # runtime-manifest.json, the bundle's own manifest (the recorded surface).
    from .interchange import INTERCHANGE_VERSION  # noqa: PLC0415
    manifest_doc = {
        "kind": BUNDLE_KIND,
        "version": BUNDLE_VERSION,
        "toolVersion": _toolchain_version(),
        "schemaVersion": INTERCHANGE_VERSION,
        "source": {"files": source_records, "sha256": source_hash},
        "ir": {
            "sha256": _sha256(ir_text),
            "manifestSha256": manifest_hash,
            "compositionHash": composition_hash,
        },
        "backends": backend_records,
        "skippedBackends": skipped,
        "policy": policy,
        "evidence": {"attestation": attested, "gauntlet": gauntlet_verdict},
        "topology": has_topology,
    }
    (out_dir / RUNTIME_MANIFEST).write_text(
        json.dumps(manifest_doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(out_dir)


def _toolchain_version() -> str:
    """The current revl toolchain version, the pyproject version through the
    installed metadata when present, else a stable fallback (verbatim from
    `truc.reproduce._toolchain_version`)."""
    from .truc.reproduce import _toolchain_version as tv  # noqa: PLC0415

    return tv()


# --------------------------------------------------------------- verify

def _read_json(path: Path) -> dict:
    """Read a JSON document from a bundle, raising a bundle-shaped error rather
    than a bare JSON exception."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RevlError(str(path), 0, f"bundle is missing {path.name}") from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise RevlError(str(path), 0, f"bundle's {path.name} is unreadable: {error}") from error


def _check_source(bundle: Path, manifest: dict) -> tuple[Check, list[str]]:
    """The committed source bytes re-hash to what the manifest recorded. Returns
    the check and the ordered list of source basenames for the recompile."""
    records = (manifest.get("source") or {}).get("files") or []
    names = [r.get("name") for r in records]
    for rec in records:
        path = bundle / "source" / rec["name"]
        if not path.exists():
            return Check("source", MISMATCH,
                         f"recorded source {rec['name']} is missing from the bundle"), names
        rebuilt = _sha256(path.read_text(encoding="utf-8"))
        if rebuilt != rec.get("sha256"):
            return Check("source", MISMATCH,
                         f"{rec['name']} bytes differ from the recorded hash",
                         rec.get("sha256", ""), rebuilt), names
    if not records:
        return Check("source", UNVERIFIED, "no source files recorded"), names
    return Check("source", OK, f"{len(records)} file(s) match their hashes"), names


def _check_ir(bundle: Path, norm_ir: dict, manifest: dict) -> Check:
    """The recompiled audit document is byte-reproducible from the source and
    hashes to the recorded value; the committed ir/manifest.json matches too."""
    rebuilt_text = _manifest_text(norm_ir)
    rebuilt_hash = _sha256(rebuilt_text)
    recorded_hash = (manifest.get("ir") or {}).get("manifestSha256") or ""
    if not recorded_hash:
        return Check("IR", UNVERIFIED, "no recorded manifest hash")
    if rebuilt_hash != recorded_hash:
        return Check("IR", MISMATCH,
                     "recompiled IR (manifest) hash differs from the recorded hash",
                     recorded_hash, rebuilt_hash)
    committed = (bundle / "ir" / "manifest.json")
    if committed.exists() and committed.read_text(encoding="utf-8") != rebuilt_text:
        return Check("IR", MISMATCH,
                     "the committed manifest.json is not byte-reproducible from "
                     "the source by the current compiler", recorded_hash, rebuilt_hash)
    return Check("IR", OK, f"manifest {_short(rebuilt_hash)}", recorded_hash, rebuilt_hash)


def _check_lock(bundle: Path, norm_ir: dict) -> Check:
    """The rebuilt provides/requires surface equals the committed lock."""
    lock = _read_json(bundle / LOCK_NAME)
    provides, requires = _surface(norm_ir)
    rec_provides = lock.get("provides") or {}
    rec_requires = lock.get("requires") or {}
    if provides != rec_provides or requires != rec_requires:
        return Check("dependency lock", MISMATCH,
                     "the rebuilt provides/requires surface differs from the lock",
                     json.dumps({"provides": rec_provides, "requires": rec_requires}, sort_keys=True),
                     json.dumps({"provides": provides, "requires": requires}, sort_keys=True))
    return Check("dependency lock", OK,
                 f"provides={json.dumps(provides, sort_keys=True)} "
                 f"requires={json.dumps(requires, sort_keys=True)}")


def _check_policy(bundle: Path, norm_ir: dict) -> Check:
    """The rebuilt capability/emission surface equals policy.json."""
    from .registry import _audit_document  # noqa: PLC0415

    recorded = _read_json(bundle / POLICY_NAME)
    rebuilt = _policy_of(_audit_document(copy.deepcopy(norm_ir)))
    if rebuilt != recorded:
        return Check("policy surface", MISMATCH,
                     "the rebuilt capabilities/emissions differ from policy.json",
                     json.dumps(recorded, sort_keys=True),
                     json.dumps(rebuilt, sort_keys=True))
    return Check("policy surface", OK,
                 f"emissions={rebuilt.get('emissions')} "
                 f"capabilities={json.dumps(rebuilt.get('capabilities'))}")


def _check_emitted(bundle: Path, norm_ir: dict, manifest: dict) -> list[Check]:
    """Each recorded backend re-emits byte-for-byte to its committed artifact -
    the emitted output corresponds to that backend's own emitter run over the
    rebuilt IR. No backends recorded is one honest `cannot verify` line."""
    records = manifest.get("backends") or {}
    if not records:
        return [Check("emitted artifact", UNVERIFIED, "no emitted artifacts recorded")]
    checks: list[Check] = []
    for backend in sorted(records):
        label = f"emitted [{backend}]"
        files = _emit_files(backend, norm_ir)
        if files is None:
            checks.append(Check(label, UNVERIFIED,
                                f"{backend} emitter is unavailable here; artifact not re-emitted"))
            continue
        recorded = {r["name"]: r.get("sha256") for r in records[backend].get("files") or []}
        rebuilt = {name: _sha256(text) for name, text in files.items()}
        if rebuilt != recorded:
            checks.append(Check(label, MISMATCH,
                                f"{backend}: re-emitted artifact differs from the committed one",
                                json.dumps(recorded, sort_keys=True),
                                json.dumps(rebuilt, sort_keys=True)))
            continue
        # the committed files must also match byte-for-byte (not only by hash).
        drift = _emitted_bytes_drift(bundle, backend, files)
        if drift:
            checks.append(Check(label, MISMATCH,
                                f"{backend}: {drift} is not byte-reproducible from the IR"))
            continue
        checks.append(Check(label, OK, f"{backend} {len(files)} file(s) reproduce"))
    return checks


def _emitted_bytes_drift(bundle: Path, backend: str, files: dict[str, str]) -> str | None:
    """The first committed emitted file whose bytes differ from a fresh emit, or
    None when every file reproduces exactly."""
    backend_dir = bundle / "emitted" / backend
    for name, text in sorted(files.items()):
        path = backend_dir / name
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            return name
    return None


def _check_backend_version(manifest: dict) -> Check:
    """The recorded interchange schema version matches the current toolchain."""
    from .interchange import INTERCHANGE_VERSION  # noqa: PLC0415

    recorded = manifest.get("schemaVersion")
    if not recorded:
        return Check("backend version", UNVERIFIED, "no recorded schema version")
    if recorded != INTERCHANGE_VERSION:
        return Check("backend version", MISMATCH,
                     "recorded interchange version differs from the current toolchain",
                     f"interchange {recorded}", f"interchange {INTERCHANGE_VERSION}")
    return Check("backend version", OK, f"interchange {INTERCHANGE_VERSION}")


def _check_attestation(bundle: Path, norm_ir: dict, env) -> Check:
    """When the bundle carries an attestation, verify it with `revl.attest`
    against the rebuilt IR, the signature proves it is authentic, the hash
    proves the composition did not change. No attestation, or no key, is
    `cannot verify`, honest, never a pass."""
    from . import attest  # noqa: PLC0415

    att_path = bundle / ATTESTATION_NAME
    if not att_path.exists():
        return Check("attestation", UNVERIFIED, "no attestation recorded in the bundle")
    try:
        att = attest.load_attestation(str(att_path))
    except RevlError as error:
        return Check("attestation", MISMATCH, f"attestation is unreadable: {error.message}")
    try:
        key = attest.resolve_key(None, env=env)
    except RevlError:
        return Check("attestation", UNVERIFIED,
                     "an attestation is present but no signing key is available "
                     f"(set {attest.KEY_ENV} or {attest.KEY_FILE_ENV}) to verify it")
    ok, reason = attest.verify_attestation(att, key, norm_ir)
    if not ok:
        return Check("attestation", MISMATCH, reason,
                     att.get("composition_hash", ""), attest.canonical_hash(norm_ir))
    return Check("attestation", OK, "authentic; IR matches the signed hash",
                 att.get("composition_hash", ""), attest.canonical_hash(norm_ir))


def _check_gauntlet(bundle: Path) -> Check:
    """The item-31 gauntlet evidence is present and records an `admissible`
    verdict. A missing dossier is `cannot verify`; a recorded `rejected` verdict
    is a MISMATCH (the bundle carries evidence it should not have been built)."""
    path = bundle / GAUNTLET_NAME
    if not path.exists():
        return Check("gauntlet", UNVERIFIED, "no gauntlet dossier recorded")
    try:
        dossier = _read_json(path)
    except RevlError as error:
        return Check("gauntlet", MISMATCH, f"gauntlet dossier is unreadable: {error.message}")
    verdict = str(dossier.get("verdict") or "")
    if verdict != "admissible":
        return Check("gauntlet", MISMATCH,
                     f"the recorded gauntlet verdict is '{verdict or '(none)'}', not admissible")
    return Check("gauntlet", OK, "admissible")


def _check_topology(bundle: Path, manifest: dict) -> Check:
    """A placement map, when the bundle carries one. The manifest and the file
    must agree on presence; a present-but-unreadable topology is a MISMATCH."""
    recorded = bool(manifest.get("topology"))
    present = (bundle / TOPOLOGY_NAME).exists()
    if not recorded and not present:
        return Check("topology", UNVERIFIED, "no topology provided")
    if recorded != present:
        return Check("topology", MISMATCH,
                     "runtime-manifest and the bundle disagree on whether a "
                     "topology is present")
    try:
        _read_json(bundle / TOPOLOGY_NAME)
    except RevlError as error:
        return Check("topology", MISMATCH, f"topology is unreadable: {error.message}")
    return Check("topology", OK, "placement map present and readable")


def verify_bundle(path: str, *, env=None) -> "VerifyReport":
    """Recompile a bundle's source and check every tier against what the bundle
    recorded. Pure: it reads the bundle and writes nothing. Raises `RevlError`
    only when the bundle cannot be opened at all (a usage/resolution failure)."""
    if env is None:
        env = os.environ
    bundle = Path(path)
    if not bundle.is_dir():
        raise RevlError(path, 0, f"not a bundle directory: {path}")
    manifest = _read_json(bundle / RUNTIME_MANIFEST)
    if manifest.get("kind") != BUNDLE_KIND:
        raise RevlError(str(bundle / RUNTIME_MANIFEST), 0,
                        f"not a {BUNDLE_KIND} manifest (found kind "
                        f"{manifest.get('kind')!r})")

    report = VerifyReport(name=bundle.name)

    source_check, names = _check_source(bundle, manifest)
    report.checks.append(source_check)

    # recompile from the committed source, in the recorded order.
    source_paths = [str(bundle / "source" / n) for n in names if n]
    missing = [p for p in source_paths if not Path(p).exists()]
    if not source_paths or missing:
        report.checks.append(Check(
            "reproducible", MISMATCH,
            "the bundle's source is incomplete; nothing could be recompiled"))
        return report

    from .compiler import compile_files  # noqa: PLC0415
    try:
        ir = compile_files(source_paths)
    except RevlError as error:
        report.checks.append(Check(
            "reproducible", MISMATCH,
            f"the bundled source no longer admits: {error.message}"))
        return report
    norm_ir = _canonical_ir(ir)

    report.checks.append(_check_ir(bundle, norm_ir, manifest))
    report.checks.append(_check_lock(bundle, norm_ir))
    report.checks.append(_check_policy(bundle, norm_ir))
    report.checks.extend(_check_emitted(bundle, norm_ir, manifest))
    report.checks.append(_check_backend_version(manifest))
    report.checks.append(_check_attestation(bundle, norm_ir, env))
    report.checks.append(_check_gauntlet(bundle))
    report.checks.append(_check_topology(bundle, manifest))

    # reproducible, the aggregate: no reproducible tier diverged. A mismatch on
    # source/IR/emitted already fails the report; this line states the verdict.
    reproducible = not any(
        c.is_mismatch for c in report.checks
        if c.tier in ("source", "IR") or c.tier.startswith("emitted"))
    report.checks.append(Check(
        "reproducible", OK if reproducible else MISMATCH,
        "the bundle rebuilds bit-for-bit" if reproducible
        else "the bundle does not rebuild bit-for-bit"))
    return report


class VerifyReport:
    """A bundle verify report, the same tier-by-tier shape item 297 uses. `ok`
    is true when no tier diverged; `cannot verify` tiers never fail it."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.checks: list[Check] = []

    @property
    def mismatches(self) -> list[Check]:
        return [c for c in self.checks if c.status == MISMATCH]

    @property
    def unverified(self) -> list[Check]:
        return [c for c in self.checks if c.status == UNVERIFIED]

    @property
    def ok(self) -> bool:
        return not self.mismatches


def render(report: VerifyReport) -> str:
    """The human report, one aligned line per tier, then a verdict. Mirrors
    `truc.reproduce.render`."""
    lines = [f"revl verify {report.name}", ""]
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
    if report.ok:
        lines.append(
            f"verified: {report.name} rebuilds bit-for-bit to what was bundled "
            f"({n_ok} OK, {n_unver} unverifiable, {n_mismatch} mismatch)")
    else:
        diverged = ", ".join(c.tier for c in report.mismatches)
        lines.append(
            f"NOT verified: {report.name} diverged on {diverged} "
            f"({n_ok} OK, {n_unver} unverifiable, {n_mismatch} mismatch)")
    return "\n".join(lines)


# --------------------------------------------------------------- CLI handlers

def run_bundle(args) -> int:
    """`revl bundle <sources...> --out DIR`, assemble a reproducible bundle.
    Exits 0 on success, 1 on a compile/draft refusal, 2 on a usage error."""
    if not args.out:
        print("error: revl bundle needs --out DIR", file=sys.stderr)
        return 2
    backends = tuple(args.backend) if args.backend else DEFAULT_BACKENDS
    try:
        out = build_bundle(args.files, args.out, backends=backends,
                           topology=args.topology)
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.json:
        manifest = _read_json(Path(out) / RUNTIME_MANIFEST)
        print(json.dumps({"bundle": out, "manifest": manifest}, indent=2))
    else:
        print(f"wrote bundle {out}")
    return 0


def run_verify(args) -> int:
    """`revl verify <bundle>`, recompile and check every tier. Exits 0 when the
    bundle reproduces (no MISMATCH), 1 on any MISMATCH, 2 on a bundle that cannot
    be opened."""
    try:
        report = verify_bundle(args.bundle)
    except RevlError as error:
        print(f"revl verify: cannot verify: {error.message}",
              file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({
            "bundle": report.name,
            "verified": report.ok,
            "checks": [
                {"tier": c.tier, "status": c.status, "detail": c.detail,
                 "recorded": c.recorded, "rebuilt": c.rebuilt}
                for c in report.checks
            ],
        }, indent=2))
    else:
        print(render(report))
    return 0 if report.ok else 1
