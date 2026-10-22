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
    emitted by default; `--backend` narrows the set. An emitter that is ABSENT
    here is omitted quietly and the bundle still exits 0: nothing was decided
    about the composition. An emitter that REFUSES this IR (e.g. wasm on floats)
    is a different fact and is recorded as one: its own diagnostic goes into
    `refusedBackends` in the runtime-manifest, is printed in the emitter's own
    words, and `revl bundle` exits `REFUSED_EXIT`. The bundle is still written,
    because a bundle that documents which tiers refused and why is more useful
    to ship than no bundle at all (issue #1400).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import posixpath
import shutil
import stat
import sys
import tempfile
import unicodedata
from pathlib import Path

from ._paths import stdlib_root
from .errors import RevlError
from .hostfile import _contained  # same canonical containment jail hostref uses
from .refusal import refusal_class

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

#: `revl bundle`'s exit code for "the bundle was written, and at least one tier
#: REFUSED this composition" (issue #1400). It is not 0, because a bundle
#: missing a tier the author asked for is not a success and a CI step must not
#: read it as one; it is not 1 either, because nothing failed to be produced,
#: the artifact is on disk and usable for the tiers that did emit.
#:
#: It is deliberately not 3. Across `revl run --backend <tier>` 3 already means
#: "that tier's toolchain is absent, skipped with a reason", which is the case
#: this issue exists to keep DISTINCT from a refusal, and which stays exit 0
#: here (an emitter that is not installed decided nothing about this IR).
REFUSED_EXIT = 4

RUNTIME_MANIFEST = "runtime-manifest.json"
#: The runtime-manifest member recording every host file that travels in the
#: bundle's `source/` tree (a spliced body file, a user module a `ref` points
#: at), each with the sha256 of its bytes (issue #2700).
HOST_FILES_KEY = "hostFiles"
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
    from another.

    The rewriting itself is `attest.path_normalized_ir`, called rather than
    re-implemented: `attest.canonical_hash` applies the same normalization at
    the hashing boundary, and a bundle that normalized differently from the
    hash it signs would be back to two definitions of one identity (issue
    #1276). The deep copy stays HERE because callers hand this document to
    `registry._audit_document`, which rewrites file paths IN PLACE; the shared
    normalizer returns its argument untouched when there is nothing to rewrite,
    which would otherwise expose the caller's own IR to that mutation."""
    from . import attest  # noqa: PLC0415

    return attest.path_normalized_ir(copy.deepcopy(ir))


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

def _unshipped_backend(backend: str) -> str | None:
    """The refusal sentence when `backend` is not one of the backends this
    toolchain ships, else None (issue #2596). A bundle's manifest names its
    backends, and that manifest is the bundle's text and not the operator's, so
    a name is checked against the shipped list before it is used for anything."""
    if backend in DEFAULT_BACKENDS:
        return None
    return (f"backend {backend!r} is not a backend this toolchain ships "
            f"(known: {', '.join(DEFAULT_BACKENDS)}); it was not imported")


def _emitter(backend: str):
    """The backend's emitter module, or None when `_BACKENDS_ROOT` has none.

    A name that is not a shipped backend is refused here, before any path is
    built from it, so no caller can load an emitter the toolchain does not ship
    (issue #2596).

    A bare `import emit` collides across backends, so this is the one module
    every loader in the process shares (`placement.backend_emit_module`, under
    the name `revl_<backend>_emit`), and its `EmitError` is the class the
    tier's other callers catch (issue #2204)."""
    unshipped = _unshipped_backend(backend)
    if unshipped is not None:
        raise RevlError("<bundle>", 0, unshipped)
    if not (_BACKENDS_ROOT / backend / "emit.py").exists():
        return None
    from .placement import backend_emit_module  # noqa: PLC0415 - off the compile graph

    return backend_emit_module(backend, root=_BACKENDS_ROOT)


# The three-way emitter outcome (absent / refused / faulted) is decided the same
# way by every entry point that loads an emitter, so it is decided in one place
# (issue #1406). `_refusal_class` stays as the name this module's tests reach
# for; `refusal_class` is the same function.
_refusal_class = refusal_class


def _emit_files(backend: str, norm_ir: dict) -> tuple[dict[str, str] | None, str | None]:
    """Emit `backend` source from the IR, as `(files, refusal)`.

    Three outcomes, kept apart on purpose (issue #1400):

      * `({filename: text}, None)`, the emitter produced an artifact. A
        single-string emitter is wrapped under the backend's conventional
        filename; the wasm emitter already returns a per-module map, whose keys
        become `<module>.wat`.
      * `(None, None)`, the emitter is ABSENT here (no `backends/<backend>/
        emit.py`, or a module with no `emit`). Nothing was decided about this
        IR; the backend is omitted quietly and the bundle still succeeds.
      * `(None, "<sentence>")`, the emitter REFUSED this IR, and the sentence is
        the emitter's own diagnostic verbatim.

    These used to collapse into one `None`, which is the whole of issue #1400:
    a tier that said by name why it could not lower a construct was recorded as
    if its toolchain were simply missing, and `revl bundle` exited 0.

    A fault INSIDE an emitter is neither of those and is not caught here: it
    propagates, exactly as it does through the `revl run` / `revl test` /
    emitter-CLI boundaries (issue #1393). The previous blanket `except
    Exception` swallowed a genuine emitter crash into the same silent omission,
    so a bundle could be missing a tier because the emitter had a bug and say
    nothing at all."""
    module = _emitter(backend)
    if module is None or not hasattr(module, "emit"):
        return None, None
    refusal_class = _refusal_class(module)
    refusals = (refusal_class,) if refusal_class is not None else ()
    try:
        emitted = module.emit(copy.deepcopy(norm_ir))
    except refusals as error:
        return None, str(error)
    if isinstance(emitted, dict):
        return {f"{name}.wat": text for name, text in sorted(emitted.items())}, None
    if isinstance(emitted, str):
        return {_SINGLE_FILE.get(backend, f"components.{backend}"): emitted}, None
    return None, None


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


def _gauntlet_dossier(paths: list[str]) -> dict | None:
    """The item-31 gauntlet dossier for the composition, the graded evidence that
    it is admissible (boots and unloads clean). Boots the candidate in a
    throwaway `Session` on a worker thread (the gauntlet drives its own asyncio
    loop, which a bare call from an already-running loop would refuse). Returns
    None when the gauntlet machinery is unavailable, honest degradation, the
    bundle is still produced without gauntlet evidence.

    Grades over the source FILES, the same root set the compile ran, NOT a text
    concatenation of them: a multi-module program's `use "other.rvl"` resolves
    only when the modules stay distinct files on disk, so a concatenated blob
    graded a real multi-module composition as `rejected` (the import could not
    be found) and turned every multi-module bundle red at the gauntlet tier.
    Passing `files` lets the gauntlet resolve imports exactly as the bundle's own
    compile did, and for a single source it grades that one file unchanged."""
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
        return gauntlet.run(Session(), {"files": list(paths)},
                            over_the_transport=False)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_grade).result()
    except Exception:  # noqa: BLE0001, the gauntlet never raises by contract; be safe anyway
        return None


# --------------------------------------------------------------- host files
#
# An extern body can be spliced from a file (`= @py file "helpers.py"`) or point
# at a user host module (`= @py ref sym from "host/engine.py"`). The IR pins the
# sha256 of each file's bytes. A bundle carries the file under `source/`, at the
# path the declaring module resolves it from, and `revl verify` re-hashes it
# against that pin (issue #2700).
#
# Stricter than the compiler on purpose: the compiler lets a module ref a file
# anywhere in the root tree. A bundle flattens its sources into one `source/`
# directory, so only a file inside the declaring module's own directory tree can
# keep the path it resolves from. Anything else is refused at bundle time.


def _host_pins_of(ir: dict) -> list[dict]:
    """Every file pin a USER-origin extern body carries in an IR, as
    `[{"extern", "tier", "kind", "path", "sha256"}, ...]`, sorted. `kind` is
    `body` for a spliced file and `ref` for a user module. A stdlib-kind ref is
    excluded: the verifier re-resolves it from its own install.

    The IR may be a bundle's own text, so its shape is not trusted: an extern,
    a `body_files` or `refs` map, or one of their entries that is not the
    expected kind of JSON value raises `RevlError` (verify turns that into a
    MISMATCH), never an `AttributeError`."""
    def _bad(what: str) -> RevlError:
        return RevlError("<bundle>", 0, f"malformed host-file pins: {what}")

    if not isinstance(ir, dict):
        raise _bad("the IR is not an object")
    externs = ir.get("externs") or []
    if not isinstance(externs, list):
        raise _bad("`externs` is not a list")
    pins: list[dict] = []
    for ext in externs:
        if not isinstance(ext, dict):
            raise _bad("an extern entry is not an object")
        name = ext.get("name")
        for field, kind in (("body_files", "body"), ("refs", "ref")):
            entries = ext.get(field) or {}
            if not isinstance(entries, dict):
                raise _bad(f"`{field}` of extern {name!r} is not an object")
            for tier, rec in entries.items():
                if not isinstance(rec, dict):
                    raise _bad(f"`{field}` entry {tier!r} of extern {name!r} "
                               "is not an object")
                if kind == "ref" and rec.get("root") == "stdlib":
                    continue
                pins.append({"extern": name, "tier": tier, "kind": kind,
                             "path": rec.get("path"),
                             "sha256": rec.get("sha256")})
    return sorted(pins, key=lambda p: (str(p["extern"]), str(p["tier"]),
                                       p["kind"]))


def _clean_pin_path(path) -> str:
    """The pinned path as a normalized POSIX string (a `./x.py` spelling is the
    same file as `x.py`). Anything that is not text stays as is and fails the
    comparisons that follow."""
    if not isinstance(path, str):
        return str(path)
    return posixpath.normpath(path.replace("\\", "/"))


def _declared_host_files(sources: list[str]) -> dict[tuple, list[tuple[str, str]]]:
    """`{(extern, tier, kind): [(module_dir, path as written), ...]}` read off
    the bundled sources' own declarations. The IR records the pin but not the
    directory it resolves from, and that directory is the jail."""
    from .parser import HostBodyFile, HostRef, parse_text  # noqa: PLC0415

    declared: dict[tuple, list[tuple[str, str]]] = {}
    for path in sources:
        program = parse_text(Path(path).read_text(encoding="utf-8"), path)
        module_dir = os.path.dirname(os.path.abspath(path))
        for ext in program.externs:
            for body in ext.bodies:
                if isinstance(body, HostBodyFile):
                    kind = "body"
                elif isinstance(body, HostRef):
                    kind = "ref"
                else:
                    continue
                declared.setdefault((ext.name, body.backend, kind), []).append(
                    (module_dir, body.path))
    return declared


def _read_host_file(module_dir: str, written: str, extern: str) -> tuple[str, bytes]:
    """Read one host file for the bundle: `(path relative to module_dir, bytes)`.

    The file must sit inside `module_dir`. The text is checked first (no
    absolute path, no NUL, no `..`), then the file is opened once and the
    containment is checked against the real path of that opened handle, so a
    symlink out of the directory is refused and the file checked is the file
    read."""
    from .hostfile import _read_all, _real_path_of_fd  # noqa: PLC0415

    where = "<bundle>"
    parts = written.replace("\\", "/").split("/")
    if (not written or os.path.isabs(written) or "\x00" in written
            or ".." in parts):
        raise RevlError(
            where, 0,
            f"cannot bundle extern `{extern}`: host file {written!r} is empty, "
            "absolute, or leaves its module's directory",
            hint="a bundled host file must sit inside the directory of the "
                 ".rvl file that names it.")
    rel = posixpath.normpath("/".join(parts))
    root_real = os.path.realpath(module_dir)
    candidate = os.path.join(module_dir, rel)
    try:
        # O_NONBLOCK: opening a named pipe must not wait for a writer; the
        # regular-file check below then refuses it.
        fd = os.open(candidate, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except (OSError, ValueError) as error:
        raise RevlError(where, 0,
                        f"cannot bundle extern `{extern}`: host file "
                        f"{written!r} cannot be opened: {error}") from error
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RevlError(where, 0,
                            f"cannot bundle extern `{extern}`: host file "
                            f"{written!r} is not a regular file")
        if not _contained(_real_path_of_fd(fd, candidate), root_real):
            raise RevlError(
                where, 0,
                f"cannot bundle extern `{extern}`: host file {written!r} "
                "resolves outside its module's directory",
                hint="a bundle never reads a host file from outside the "
                     "directory of the .rvl file that names it.")
        return rel, _read_all(fd)
    finally:
        os.close(fd)


def _path_key(rel: str) -> str:
    """The spelling two paths are compared under for a collision: Unicode
    normalized and case folded, because a bundle can be unpacked on a file
    system that treats `Helpers.py` and `helpers.py` as one file."""
    return unicodedata.normalize("NFC", rel).casefold()


def _check_layout(pairs: set[tuple[str, bytes]], source_names: set[str],
                  where: str) -> None:
    """Refuse a set of distinct `(path, bytes)` host files that cannot all live
    in the flat `source/` tree next to the bundled sources: one path with
    different bytes, two spellings of one path, a file with the name of a
    bundled source, or a file that is also another file's directory. Runs
    before the output directory is touched."""
    taken: dict[str, tuple[str, bool]] = {
        _path_key(name): (name, True) for name in source_names}
    dirs: dict[str, str] = {}
    for rel, _data in sorted(pairs):
        key = _path_key(rel)
        other = taken.get(key)
        if other is not None:
            what = ("has the name of a bundled source" if other[1]
                    else "is named by another module with different contents"
                    if other[0] == rel else
                    f"has the same path as {other[0]!r} on a case-folding or "
                    "normalizing file system")
            raise RevlError(where, 0,
                            f"cannot bundle host file {rel!r}: it {what}")
        taken[key] = (rel, False)
        parts = key.split("/")
        for i in range(1, len(parts)):
            dirs.setdefault("/".join(parts[:i]), rel)
    for key, (rel, _is_source) in taken.items():
        if key in dirs:
            raise RevlError(
                where, 0,
                f"cannot bundle host file {rel!r}: it is also the directory "
                f"of {dirs[key]!r} in the bundle's `source/` tree")


def _collect_host_files(ir: dict, sources: list[str]) -> list[dict]:
    """Read every file the IR pins for a USER extern body, checked against the
    pin. Returns one `{"rel", "data", "extern", "tier", "kind", "sha256"}` per
    pin, so a file shared by two externs is recorded once per extern. Raises
    `RevlError` when a pinned file is not declared by a bundled source, changed
    since the compile, would lose its resolved path in the flat `source/` tree,
    or cannot share that tree with the other files."""
    pins = _host_pins_of(ir)
    if not pins:
        return []
    declared = _declared_host_files(sources)
    items: list[dict] = []
    for pin in pins:
        label = f"`{pin['extern']}` ({pin['tier']})"
        candidates = declared.get((pin["extern"], pin["tier"], pin["kind"]))
        if not candidates:
            raise RevlError(
                sources[0], 0,
                f"cannot bundle extern {label}: its host file "
                f"{pin['path']!r} is declared by a module that is not among "
                "the bundled sources",
                hint="list every module that declares a host file on the "
                     "`revl bundle` command line.")
        found = None
        for module_dir, written in candidates:
            rel, data = _read_host_file(module_dir, written, pin["extern"])
            if hashlib.sha256(data).hexdigest() == pin["sha256"]:
                found = (rel, data)
                break
        if found is None:
            raise RevlError(
                sources[0], 0,
                f"cannot bundle extern {label}: host file {pin['path']!r} "
                "changed since it was compiled")
        rel, data = found
        if rel != _clean_pin_path(pin["path"]):
            raise RevlError(
                sources[0], 0,
                f"cannot bundle extern {label}: host file {pin['path']!r} "
                f"would resolve as {rel!r} from the flat bundle `source/` tree",
                hint="keep the file inside the directory of the .rvl file that "
                     "names it, and that file in the compile root.")
        items.append({"rel": rel, "data": data, "extern": pin["extern"],
                     "tier": pin["tier"], "kind": pin["kind"],
                     "sha256": pin["sha256"]})
    _check_layout({(item["rel"], item["data"]) for item in items},
                  {os.path.basename(p) for p in sources}, sources[0])
    return items


def _write_host_files(source_dir: Path, host_files: list[dict]) -> list[dict]:
    """Write the collected host files under `source_dir`, byte for byte (a file
    shared by several externs once), and return one runtime-manifest record per
    pin."""
    records: list[dict] = []
    root_real = os.path.realpath(str(source_dir))
    written: set[str] = set()
    for item in host_files:
        if item["rel"] not in written:
            dest = source_dir / item["rel"]
            if not _contained(os.path.realpath(str(dest)), root_real):
                raise RevlError("<bundle>", 0,
                                f"host file {item['rel']!r} escapes the bundle")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(item["data"])
            written.add(item["rel"])
        records.append({"path": f"source/{item['rel']}",
                        "sha256": item["sha256"], "extern": item["extern"],
                        "tier": item["tier"], "kind": item["kind"]})
    return records


def build_bundle(sources: list[str], out: str, *, backends=DEFAULT_BACKENDS,
                 topology: str | None = None, force: bool = False, env=None) -> str:
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
    # Every requested backend is checked before the output directory is
    # touched, so a refusal leaves an existing bundle exactly as it was
    # (issue #2596).
    for backend in backends:
        unshipped = _unshipped_backend(backend)
        if unshipped is not None:
            raise RevlError("<bundle>", 0, unshipped)

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
    # issue #2700: a file-spliced body and a USER host-module ref name files
    # that must travel with the bundle, under their declaring module's
    # directory, so `revl verify` can recompile from the bundle alone and
    # re-hash them against the pins in the IR. Everything is read and checked
    # here, before the output directory is touched, so a refusal leaves an
    # existing bundle exactly as it was. A STDLIB-kind ref carries nothing: the
    # verifier re-resolves it from its own install (item 410).
    host_files = _collect_host_files(ir, sources)

    norm_ir = _canonical_ir(ir)
    ir_text = _ir_text(norm_ir)
    manifest_text = _manifest_text(norm_ir)

    out_dir = Path(out)
    if out_dir.exists():
        # Guard against silently deleting an existing non-empty directory (a
        # `--out` pointed at unrelated files is data loss). An empty directory,
        # or an explicit `--force`, is fine to clear and rewrite.
        if not out_dir.is_dir():
            raise RevlError(
                "<bundle>", 0,
                f"--out path already exists and is not a directory: {out_dir}",
                hint="point --out at a bundle directory (e.g. app.revlbundle) "
                     "or a fresh path.")
        if any(out_dir.iterdir()) and not force:
            raise RevlError(
                "<bundle>", 0,
                f"--out directory is not empty: {out_dir}",
                hint="bundling would delete its current contents; pass --force "
                     "to overwrite it, or choose an empty/fresh --out path.")
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
    host_file_records = _write_host_files(out_dir / "source", host_files)

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
    # An omitted backend is recorded under `skippedBackends` either way, but the
    # reason is no longer one sentence covering two different facts: a refusal
    # also lands in `refusedBackends`, carrying the emitter's own diagnostic, and
    # that map is what makes `revl bundle` exit non-zero (issue #1400).
    backend_records: dict = {}
    skipped: dict = {}
    refused: dict = {}
    for backend in backends:
        files, refusal = _emit_files(backend, norm_ir)
        if files is None:
            if refusal is None:
                skipped[backend] = "emitter unavailable here"
            else:
                skipped[backend] = f"emitter refused this IR: {refusal}"
                refused[backend] = refusal
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
    dossier = _gauntlet_dossier(sources)
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
        "refusedBackends": refused,
        "policy": policy,
        "evidence": {"attestation": attested, "gauntlet": gauntlet_verdict},
        "topology": has_topology,
        # additive: present only when a body file or user module ref travels,
        # so every other bundle's manifest is byte-identical (issue #2700).
        **({HOST_FILES_KEY: host_file_records} if host_file_records else {}),
    }
    (out_dir / RUNTIME_MANIFEST).write_text(
        json.dumps(manifest_doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(out_dir)


def _toolchain_version() -> str:
    """The current revl toolchain version, `revl._version.version()`."""
    from ._version import version  # noqa: PLC0415

    return version()


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
        files[_relposix(bundle_dir, path)] = path.read_bytes().decode("utf-8")
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
        # bytes, not write_text: a carried host file is pinned by the hash of
        # its exact bytes, which a newline translation would change.
        target.write_bytes(text.encode("utf-8"))
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
    excluded, those carry their file in `source/` and are checked by
    `_check_host_files`, so only stdlib refs reach the verify tier below."""
    out: list[dict] = []
    # The IR is the bundle's own text. A malformed shape is skipped here and
    # reported as a MISMATCH by `_check_host_files`, which reads the same IR.
    externs = ir.get("externs") if isinstance(ir, dict) else None
    for ext in externs if isinstance(externs, list) else []:
        refs = ext.get("refs") if isinstance(ext, dict) else None
        for tier, ref in (refs.items() if isinstance(refs, dict) else []):
            if (isinstance(ref, dict) and ref.get("root") == "stdlib"
                    and isinstance(ref.get("path"), str)
                    and isinstance(ref.get("sha256"), str)):
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


def _check_host_files(bundle: Path, manifest: dict) -> list[Check]:
    """issue #2700 verify tier `host file`: every file a user extern body pins
    in the RECORDED `ir/ir.json` (a spliced body file, a user module a `ref`
    names) is read from the bundle's own `source/` tree and re-hashed against
    that pin. One Check per file: OK, or MISMATCH naming the file (missing,
    outside `source/`, or bytes that differ). A second Check compares the
    manifest's `hostFiles` record with those pins, so a record that was dropped
    or edited is not a pass. Empty when the bundle pins no file, so every other
    bundle's report is byte-identical.

    The pins and the manifest are the bundle's own text, so each path is
    treated as hostile: it is jailed to the realpath of `source/` before any
    read."""
    recorded_ir = _read_json(bundle / "ir" / "ir.json")
    try:
        pins = _host_pins_of(recorded_ir)
    except RevlError as error:
        return [Check("host files", MISMATCH, error.message)]
    recorded = manifest.get(HOST_FILES_KEY)
    if not pins and not recorded:
        return []
    checks: list[Check] = []
    source_real = os.path.realpath(str(bundle / "source"))

    def _entry(p: dict) -> tuple:
        return (p.get("extern"), p.get("tier"), p.get("kind"),
                p.get("path"), p.get("sha256"))

    want = sorted((_entry({**p, "path": f"source/{_clean_pin_path(p['path'])}"})
                   for p in pins), key=repr)
    have = sorted((_entry(r) for r in recorded if isinstance(r, dict)),
                  key=repr) \
        if isinstance(recorded, list) else None
    if have != want:
        checks.append(Check(
            "host files", MISMATCH,
            "the runtime-manifest's host-file record differs from the file "
            "pins in ir/ir.json"))
    for pin in pins:
        raw = pin["path"]
        label = f"host file {raw}"
        who = f"extern `{pin['extern']}` ({pin['tier']})"
        if (not isinstance(raw, str) or not raw or os.path.isabs(raw)
                or "\x00" in raw or not isinstance(pin["sha256"], str)):
            checks.append(Check(
                label, MISMATCH,
                f"the pinned path {raw!r} for {who} is not a relative path; a "
                "forged or tampered bundle"))
            continue
        target = bundle / "source" / _clean_pin_path(raw)
        if not _contained(os.path.realpath(str(target)), source_real):
            checks.append(Check(
                label, MISMATCH,
                f"the pinned path {raw!r} for {who} escapes the bundle's "
                "source tree; a forged or tampered bundle"))
            continue
        try:
            got = hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError:
            checks.append(Check(
                label, MISMATCH,
                f"{raw} for {who} is missing from the bundle"))
            continue
        if got != pin["sha256"]:
            checks.append(Check(
                label, MISMATCH,
                f"{raw} for {who} hashes differently than the bundle pinned",
                pin["sha256"], got))
        else:
            checks.append(Check(label, OK,
                                f"{raw} for {who} matches the pin"))
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
        unshipped = _unshipped_backend(backend)
        if unshipped is not None:
            # The bundle claims an artifact for a backend this toolchain does
            # not ship. That is a divergence in what it claims, not an
            # unverifiable tier, and nothing was loaded for it (issue #2596).
            checks.append(Check(label, MISMATCH, unshipped))
            continue
        files, refusal = _emit_files(backend, norm_ir)
        if refusal is not None:
            # The bundle committed an artifact for this tier and the tier now
            # refuses the rebuilt IR. That is a divergence in what the bundle
            # claims, not an unverifiable tier, so it is a MISMATCH carrying the
            # emitter's own sentence.
            checks.append(Check(label, MISMATCH,
                                f"{backend}: the emitter now refuses this IR, so the "
                                f"committed artifact cannot be rebuilt: {refusal}"))
            continue
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


def _check_refused(norm_ir: dict, manifest: dict) -> list[Check]:
    """Each RECORDED refusal is re-checked: that tier still refuses the rebuilt
    IR, and refuses it with the same sentence.

    A recorded refusal is part of the surface the bundle asserts, so it is
    checked like every other part of it. Without this line the record would be
    a comment: a bundle could claim a tier refused while that tier emits fine
    today, and nothing would say so. Empty for a bundle that recorded no
    refusal, which keeps a refusal-free report byte-identical to what it was."""
    recorded = manifest.get("refusedBackends") or {}
    checks: list[Check] = []
    for backend in sorted(recorded):
        label = f"refused [{backend}]"
        was = str(recorded[backend])
        unshipped = _unshipped_backend(backend)
        if unshipped is not None:
            # The same manifest-supplied name as the emitted tier (issue #2596).
            checks.append(Check(label, MISMATCH, unshipped, was, ""))
            continue
        files, refusal = _emit_files(backend, norm_ir)
        if refusal is None and files is None:
            checks.append(Check(
                label, UNVERIFIED,
                f"{backend} emitter is unavailable here; the recorded refusal "
                "could not be re-checked"))
        elif refusal is None:
            checks.append(Check(
                label, MISMATCH,
                f"{backend} no longer refuses this IR; the recorded refusal is stale",
                was, "the emitter produced an artifact"))
        elif refusal != was:
            checks.append(Check(
                label, MISMATCH,
                f"{backend} refuses this IR with a different diagnostic",
                was, refusal))
        else:
            checks.append(Check(label, OK, f"{backend} still refuses: {was}"))
    return checks


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

    # issue #2700: re-hash each spliced body file and user module the IR pins
    # against the copy carried in `source/`, before the recompile, so a changed
    # file reads as "this file changed" and names it.
    report.checks.extend(_check_host_files(bundle, manifest))

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
    report.checks.extend(_check_refused(norm_ir, manifest))
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
    Exits 0 on success, 1 on a compile/draft refusal, 2 on a usage error, and
    REFUSED_EXIT when the bundle was written but at least one tier refused it."""
    if not args.out:
        print("error: revl bundle needs --out DIR", file=sys.stderr)
        return 2
    backends = tuple(args.backend) if args.backend else DEFAULT_BACKENDS
    one_file = getattr(args, "one_file", None)
    try:
        out = build_bundle(args.files, args.out, backends=backends,
                           topology=args.topology,
                           force=getattr(args, "force", False))
        packed = pack_bundle(out, one_file) if one_file else None
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    manifest = _read_json(Path(out) / RUNTIME_MANIFEST)
    if args.json:
        doc = {"bundle": out, "manifest": manifest}
        if packed is not None:
            doc["oneFile"] = packed
        print(json.dumps(doc, indent=2))
    else:
        print(f"wrote bundle {out}")
        if packed is not None:
            print(f"wrote one-file bundle {packed}")

    # A refusal is reported on stderr in the emitter's own words, on every
    # output mode: `--json` carries it inside the manifest, but a human reading
    # a piped run would otherwise see nothing (issue #1400).
    refused = manifest.get("refusedBackends") or {}
    if refused:
        # stdout is block-buffered when it is not a terminal, so without this the
        # refusal reaches a log file ABOVE the `wrote bundle` line it qualifies.
        sys.stdout.flush()
    for backend in sorted(refused):
        print(f"refused: the {backend} tier refused this composition, so the "
              f"bundle carries no {backend} artifact:\n"
              f"         {refused[backend]}", file=sys.stderr)
    if refused:
        names = ", ".join(sorted(refused))
        print(f"bundle written WITHOUT {len(refused)} of {len(backends)} "
              f"backend(s): {names}", file=sys.stderr)
        return REFUSED_EXIT
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
