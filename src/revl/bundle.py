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
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from ._paths import stdlib_root
from .errors import RevlError
from .hostfile import _contained  # same canonical containment jail hostref uses

# Reuse the item-297 reproduce vocabulary and projections verbatim, no new
# status scheme, no new surface derivation.
from .truc.reproduce import (
    OK, MISMATCH, UNVERIFIED, Check, _policy_of, _short, _surface,
)

# The bundle envelope identity (mirrors the interchange/attest kind+version
# idea: a self-identifying tag plus a MAJOR.MINOR line, additive within MAJOR).
BUNDLE_KIND = "revl.bundle"
BUNDLE_VERSION = "1.0"

# The one-file bundle envelope: the whole multi-file `.revlbundle/` directory
# carried inside ONE self-contained, self-identifying JSON document. Same
# MAJOR.MINOR discipline as the bundle proper (additive within MAJOR). The
# canonical single-file extension a packed bundle is written under.
ONEFILE_KIND = "revl.bundle.onefile"
ONEFILE_VERSION = "1.0"
ONEFILE_SUFFIX = ".revlbundle1"

RUNTIME_MANIFEST = "runtime-manifest.json"
LOCK_NAME = "components.lock"
POLICY_NAME = "policy.json"
ATTESTATION_NAME = "attestation.json"
GAUNTLET_NAME = "gauntlet.json"
TOPOLOGY_NAME = "topology.json"

#: The member `build_bundle` stamps into the staged gauntlet dossier naming the
#: composition it was graded over, and the member `verify` and `deploy.admit`
#: check it against. A dossier without it names no composition, so it cannot be
#: shown to be evidence about the artifact in hand (roadmap 428 F8).
GAUNTLET_IDENTITY = "compositionHash"
#: The one gauntlet verdict that is evidence of admissibility.
GAUNTLET_ADMISSIBLE = "admissible"

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

    return json.dumps(_audit_document(norm_ir),
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
        # `revl bundle` runs on the operator's own machine over the operator's
        # own files: the human running the command IS the author, so this
        # compile carries no MCP authoring trust (the same rule
        # `server.compile_under_authoring` applies to jailed `files`).
        return gauntlet.run(Session(), {"source": source},
                            over_the_transport=False)

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

    from . import attest  # noqa: PLC0415

    # ONE frontend run, and it is the gate run the attestation records. The
    # compile used to happen here and the provenance was thrown away, so
    # `make_attestation` signed a guarantee list nothing had measured; the
    # verdict now travels to the signature (item 127 F2). A compile refusal is
    # re-raised verbatim, so `revl bundle`'s diagnostics are unchanged.
    gate_verdict = attest.run_gate(paths=list(sources), normalize=_canonical_ir)
    if gate_verdict.error is not None and gate_verdict.ir is None:
        raise gate_verdict.error
    ir = gate_verdict.ir
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
    # item 396 option B / 410: a USER host-module ref names a root-relative file
    # that the flat basename copy would not carry, so a bundled user-ref program
    # could not resolve or re-hash the module at verify. Refuse it cleanly (396's
    # stage-4 gap, still open). A STDLIB-kind ref is different: its helper is a
    # versioned FIRST-PARTY install dependency the verifier resolves from ITS OWN
    # install (re-resolve, carry nothing), exactly as verify already re-resolves
    # the bundled `stdlib/fs.rvl` source itself — so a stdlib ref bundles, and the
    # `stdlib refs` verify tier re-hashes it against the pin on the verifier.
    user_ref_externs = sorted(
        e.get("name") for e in (ir.get("externs") or [])
        if any(r.get("root") != "stdlib"
               for r in (e.get("refs") or {}).values()))
    if user_ref_externs:
        raise RevlError(
            sources[0], 0,
            "cannot bundle a composition whose extern references a USER external "
            "host module (`= @backend ref sym from \"path\"`): "
            f"{', '.join(user_ref_externs)}",
            hint="`revl bundle` copies sources flat by basename, so a "
                 "root-relative user ref'd module would not travel with the "
                 "bundle and `revl verify` could not reproduce it. Inline the "
                 "body (`= @backend { ... }`) to bundle for now; carrying user "
                 "ref files through the bundle is the item 396 stage 4 follow-up. "
                 "A STDLIB ref (a shipped module referencing a shipped helper) "
                 "bundles fine — the verifier re-resolves it from its own install "
                 "(item 410).")

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
    audit = _audit_document(norm_ir)
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
    composition_hash = attest.canonical_hash(norm_ir)
    attested = False
    try:
        key = attest.resolve_key(None, env=env)
    except RevlError:
        key = None
    if key is not None:
        signer = env.get(attest.SIGNER_ENV)
        att = attest.make_attestation(norm_ir, key, verdict=gate_verdict,
                                      signer=signer)
        (out_dir / ATTESTATION_NAME).write_text(
            json.dumps(att, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        attested = True

    # gauntlet.json, the item-31 graded dossier (evidence it is admissible).
    #
    # The staged dossier is stamped with the composition it was graded over.
    # Without it the record names no composition, so a real `admissible`
    # dossier produced for a DIFFERENT artifact hashes correctly into the
    # deploy chain and rides along under an honest signature (roadmap 428 F8).
    # With it, `verify` here and `deploy.admit` on the receiving side can both
    # require that the evidence is evidence about the artifact in hand.
    dossier = _gauntlet_dossier(combined_source)
    gauntlet_verdict = None
    if dossier is not None:
        gauntlet_verdict = str(dossier.get("verdict") or "")
        dossier = dict(dossier)
        dossier[GAUNTLET_IDENTITY] = composition_hash
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


# --------------------------------------------------------------- one-file bundle
#
# The multi-file `.revlbundle/` directory is the canonical form; a one-file
# bundle is that SAME directory carried inside one self-contained JSON document,
# so a consumer can hand around a single artifact instead of a tree. It invents
# NO new hash scheme and re-derives nothing: every file's exact bytes travel
# verbatim, so the source, IR, manifest and attestation hashes the directory
# recorded stay bit-for-bit what they were, and unpacking reproduces the tree
# byte-for-byte. Every file a bundle writes is UTF-8 text (`.rvl` source, JSON
# documents, emitted backend source), so a `{relative path: text}` map is a
# lossless, deterministic, human-inspectable envelope, and stays in the plain
# deterministic-JSON discipline the rest of the bundle already follows (no
# timestamps, sorted keys), so a bundle packs to identical bytes every time.


def _relposix(root: Path, path: Path) -> str:
    """The POSIX-slashed path of `path` relative to `root`, the stable key a
    packed file travels under regardless of the host's path separator."""
    return path.relative_to(root).as_posix()


def _pack_document(bundle_dir: Path) -> dict:
    """The one-file envelope for a `.revlbundle/` directory: a self-identifying
    `kind`/`version` header and a `files` map of every file in the tree, keyed by
    its POSIX relative path, valued by its verbatim UTF-8 text. Deterministic:
    the map is emitted sorted, so the same directory packs to the same bytes."""
    files: dict[str, str] = {}
    for path in sorted(p for p in bundle_dir.rglob("*") if p.is_file()):
        files[_relposix(bundle_dir, path)] = path.read_text(encoding="utf-8")
    return {"kind": ONEFILE_KIND, "version": ONEFILE_VERSION, "files": files}


def _onefile_text(doc: dict) -> str:
    """The deterministic on-disk spelling of a one-file bundle envelope."""
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def pack_bundle(bundle_dir: str, out_file: str) -> str:
    """Pack a `.revlbundle/` directory into ONE self-contained file and return
    its path. The inverse of `unpack_bundle`: the two round-trip a bundle
    byte-for-byte, and a packed bundle verifies exactly as the directory does
    (nothing is re-derived, every recorded hash is carried verbatim)."""
    src = Path(bundle_dir)
    if not src.is_dir():
        raise RevlError(bundle_dir, 0, f"not a bundle directory: {bundle_dir}")
    if not (src / RUNTIME_MANIFEST).is_file():
        raise RevlError(bundle_dir, 0,
                        f"not a {BUNDLE_KIND} directory: {RUNTIME_MANIFEST} is "
                        "missing, nothing to pack")
    Path(out_file).write_text(_onefile_text(_pack_document(src)),
                              encoding="utf-8")
    return out_file


def unpack_bundle(one_file: str, out_dir: str) -> str:
    """Expand a one-file bundle back into a `.revlbundle/` directory and return
    its path. Refuses a document that is not a one-file envelope, and jails every
    embedded path inside `out_dir` (a one-file bundle can arrive from anywhere, so
    an absolute or `..`-bearing key that escaped the tree could overwrite a file
    outside it, exactly the containment the stdlib-ref verify tier and hostref
    enforce)."""
    doc = _read_json(Path(one_file))
    if doc.get("kind") != ONEFILE_KIND:
        raise RevlError(one_file, 0,
                        f"not a {ONEFILE_KIND} document (found kind "
                        f"{doc.get('kind')!r})")
    files = doc.get("files")
    if not isinstance(files, dict):
        raise RevlError(one_file, 0,
                        "one-file bundle carries no `files` map, nothing to unpack")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    out_real = os.path.realpath(str(out))
    for rel, text in sorted(files.items()):
        if os.path.isabs(rel) or "\x00" in rel:
            raise RevlError(one_file, 0,
                            f"one-file bundle entry {rel!r} is absolute or "
                            "contains a NUL byte; a bundle path is relative to "
                            "the bundle root, so this is a forged or tampered file")
        target = out / rel
        if not _contained(os.path.realpath(str(target)), out_real):
            raise RevlError(one_file, 0,
                            f"one-file bundle entry {rel!r} escapes the bundle "
                            "root; a forged or tampered file")
        if not isinstance(text, str):
            raise RevlError(one_file, 0,
                            f"one-file bundle entry {rel!r} is not text")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return out_dir


def is_onefile(path: str) -> bool:
    """Whether `path` is a one-file bundle: a readable file (not a directory)
    whose top-level `kind` is the one-file envelope tag. A directory bundle, a
    missing path, or any other file is False."""
    p = Path(path)
    if not p.is_file():
        return False
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("kind") == ONEFILE_KIND
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return False


# --------------------------------------------------------------- verify

def _stdlib_refs_of(ir: dict) -> list[dict]:
    """Every stdlib-kind host-module ref in an IR, as
    `[{"extern", "tier", "path", "sha256"}, ...]`. A user ref (no `"root"`) is
    excluded — those never bundle (build-time refusal), so only stdlib refs reach
    the verify tier below."""
    out: list[dict] = []
    for ext in ir.get("externs") or []:
        for tier, ref in (ext.get("refs") or {}).items():
            if ref.get("root") == "stdlib":
                out.append({"extern": ext.get("name"), "tier": tier,
                            "path": ref["path"], "sha256": ref["sha256"]})
    return out


def _check_stdlib_refs(bundle: Path) -> list[Check]:
    """item 410 verify tier `stdlib refs`: re-resolve each stdlib-kind ref
    against the VERIFYING machine's install root and re-hash against the recorded
    pin (re-resolve, never travel — the helper is a versioned first-party install
    dependency, so a second copy in the bundle would make skew representable). One
    Check per ref: OK (bytes match), MISMATCH (differ — both hashes printed,
    naming the file), or UNVERIFIED (the install lacks the helper on this
    machine). Empty when the bundle carries no stdlib ref, so a ref-free bundle's
    report is byte-identical.

    Reads the RECORDED pin from the bundle's own `ir/ir.json` rather than the
    recompiled IR: the recompile already re-resolves against the same install, so
    comparing recompile-to-recompile would be a tautology. Comparing the recorded
    pin against a fresh re-hash is what catches a bundle built on install A and
    verified on a doctored or version-skewed install B."""
    recorded_ir = _read_json(bundle / "ir" / "ir.json")
    refs = _stdlib_refs_of(recorded_ir)
    if not refs:
        return []
    install_root = Path(str(stdlib_root().parent))
    checks: list[Check] = []
    install_real = os.path.realpath(str(install_root))
    for ref in refs:
        label = f"stdlib ref {ref['path']}#{ref['tier']}"
        raw_path = ref["path"]
        # The ref path comes from the bundle's own recorded ir/ir.json, i.e. from
        # the artifact under verification, so it is attacker-controlled. Gate on
        # realpath-containment BEFORE any read/hash, mirroring the hostref /
        # 410-escape install-origin jail: a `..`-bearing, absolute, or NUL-bearing
        # path that resolves outside the install tree re-hashes a file the bundle
        # never shipped and could report OK against an attacker-chosen sha256.
        if os.path.isabs(raw_path) or "\x00" in raw_path:
            checks.append(Check(
                label, MISMATCH,
                f"the bundled stdlib ref path {raw_path!r} for extern "
                f"`{ref['extern']}` is absolute or contains a NUL byte; a stdlib "
                f"ref is resolved strictly relative to the install root, so this "
                f"is a forged or tampered bundle"))
            continue
        target = install_root / raw_path
        if not _contained(os.path.realpath(str(target)), install_real):
            checks.append(Check(
                label, MISMATCH,
                f"the bundled stdlib ref path {raw_path!r} for extern "
                f"`{ref['extern']}` escapes the install tree "
                f"({install_real}); a ref that resolves outside the install root "
                f"is a forged or tampered bundle"))
            continue
        try:
            # raw file bytes, matching the compile-time pin in hostref.py
            got = hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError:
            checks.append(Check(
                label, UNVERIFIED,
                f"the install lacks the helper {ref['path']} for extern "
                f"`{ref['extern']}` on this machine (nothing to re-hash)"))
            continue
        if got != ref["sha256"]:
            checks.append(Check(
                label, MISMATCH,
                f"the shipped helper {ref['path']} for extern `{ref['extern']}` "
                f"hashes differently than the bundle pinned (a version skew or a "
                f"doctored install)", ref["sha256"], got))
        else:
            checks.append(Check(
                label, OK,
                f"{ref['path']} for extern `{ref['extern']}` re-resolves and "
                f"matches the pin"))
    return checks


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
    rebuilt = _policy_of(_audit_document(norm_ir))
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


def _check_gauntlet(bundle: Path, norm_ir: dict) -> Check:
    """The item-31 gauntlet evidence is present, records an `admissible`
    verdict, and was graded over THIS composition. A missing dossier is `cannot
    verify`; a recorded `rejected` verdict is a MISMATCH (the bundle carries
    evidence it should not have been built); a dossier naming another
    composition, or naming none at all, is a MISMATCH too, since a real
    `admissible` record for a different artifact is not evidence about this
    one (roadmap 428 F8)."""
    path = bundle / GAUNTLET_NAME
    if not path.exists():
        return Check("gauntlet", UNVERIFIED, "no gauntlet dossier recorded")
    try:
        dossier = _read_json(path)
    except RevlError as error:
        return Check("gauntlet", MISMATCH, f"gauntlet dossier is unreadable: {error.message}")
    verdict = str(dossier.get("verdict") or "")
    if verdict != GAUNTLET_ADMISSIBLE:
        return Check("gauntlet", MISMATCH,
                     f"the recorded gauntlet verdict is '{verdict or '(none)'}', not admissible")
    from . import attest  # noqa: PLC0415

    expected = attest.canonical_hash(norm_ir)
    graded = dossier.get(GAUNTLET_IDENTITY)
    if not isinstance(graded, str) or not graded:
        return Check("gauntlet", MISMATCH,
                     f"the gauntlet dossier carries no `{GAUNTLET_IDENTITY}`, so "
                     "it names no composition and is not evidence about this one",
                     "", expected)
    if graded != expected:
        return Check("gauntlet", MISMATCH,
                     "the gauntlet dossier was graded over another composition",
                     graded, expected)
    return Check("gauntlet", OK, "admissible; graded over this composition")


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
    recorded. Accepts either a `.revlbundle/` directory or a one-file bundle (a
    packed directory): a one-file bundle is expanded into a throwaway temp tree
    and verified there, so the two forms produce the SAME report (a one-file
    bundle rebuilds bit-for-bit exactly as the directory it packed). Raises
    `RevlError` only when the bundle cannot be opened at all (a usage/resolution
    failure)."""
    if is_onefile(path):
        tmp = tempfile.mkdtemp(prefix="revl-onefile-verify-")
        try:
            unpack_bundle(path, tmp)
            report = _verify_bundle_dir(tmp, env=env)
            report.name = Path(path).name  # report on the one-file, not the temp
            return report
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return _verify_bundle_dir(path, env=env)


def _verify_bundle_dir(path: str, *, env=None) -> "VerifyReport":
    """Verify a `.revlbundle/` directory in place. Pure: it reads the bundle and
    writes nothing."""
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

    # item 410: re-resolve each stdlib-kind ref against THIS machine's install
    # and re-hash against the recorded pin, BEFORE the recompile — so a skewed or
    # doctored shipped helper reads as "the helper changed", naming the file, and
    # a helper the verifier's install LACKS is an honest UNVERIFIED (SKIP) line
    # rather than a recompile crash. Empty (no line) for a bundle with no stdlib
    # ref, keeping a ref-free report byte-identical.
    report.checks.extend(_check_stdlib_refs(bundle))

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
    report.checks.append(_check_gauntlet(bundle, norm_ir))
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
    one_file = getattr(args, "one_file", None)
    try:
        out = build_bundle(args.files, args.out, backends=backends,
                           topology=args.topology)
        packed = pack_bundle(out, one_file) if one_file else None
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.json:
        manifest = _read_json(Path(out) / RUNTIME_MANIFEST)
        doc = {"bundle": out, "manifest": manifest}
        if packed is not None:
            doc["oneFile"] = packed
        print(json.dumps(doc, indent=2))
    else:
        print(f"wrote bundle {out}")
        if packed is not None:
            print(f"wrote one-file bundle {packed}")
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
