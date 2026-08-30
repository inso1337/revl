"""item 396 option B: resolve and verify an extern's host-module REFERENCE.

`= @backend ref sym from "path"` binds an extern to a symbol imported from an
EXISTING host module, wrapping code as-is instead of pasting it. This module
owns the two halves that keep that sound:

COMPILE TIME (`resolve_refs`): the written path resolves relative to the
declaring `.rvl` file, but the JAIL is the ROOT tree — the resolved realpath
must sit inside the root compile file's directory tree, because the file must
ALSO be reachable at deploy time through one import root. The emitted import
specifier is derived from the resolved path RELATIVE TO THAT ROOT, and a content
hash of the file's bytes is PINNED into the IR. A module resolved from OUTSIDE
the root tree (the stdlib search path, a `REVL_IMPORT_PATH` entry) may not
declare a `ref`. `..` segments ARE allowed (a `src/plugins/x.rvl` may ref
`../host/engine.py`) precisely because the root-tree containment check is what
holds; absolute paths are refused. Containment uses the same canonical
`commonpath`-over-realpath rule as option A's jail (hostfile.py), same hardlink
residual.

DEPLOY TIME (`plug_refs`, the py run driver): the compiler checked a FILE; the
artifact imports a dotted NAME resolved by the host import machinery of whatever
process loads it. That boundary is a TOCTOU the compiler cannot reach across.
The pinned hash makes it CHECKABLE: at plug the driver resolves the module spec
the interpreter would actually import — WITHOUT executing any parent
`__init__.py` (a recursive `PathFinder.find_spec` walk, never
`importlib.util.find_spec`, which imports parents) — hashes the file behind it,
and REFUSES a mismatch. It also EVICTS stale `sys.modules` entries derived from a
ref so a re-emit/replug in one process runs the NEW code, not a gen-N module
python cached process-wide. The import root is APPENDED to `sys.path` (never
prepended: a project file must not be able to SHADOW a trusted runtime module),
and only when refs are present.

Residuals, stated rather than claimed away: (1) a parent `__init__.py` that
mutates its own `__path__` at runtime can diverge from the static spec walk;
(2) a plug-to-first-call TOCTOU window remains (import machinery is not atomic);
(3) the hash pin is one file deep — `engine.py`'s own `import helpers` is
unhashed/unjailed/invisible to `revl audit`; (4) the appended root grants NEW
importability of every project module to every host body (append only prevents
SHADOWING of already-importable names).
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import keyword
import os
import sys

from .errors import RevlError
from .hostfile import _contained, _normcase  # same canonical containment jail

# The tiers on which "import a source symbol from a checked file" is native:
# py and ts each have a first-class notion of a module AT a path, so an import
# specifier can be derived from the file the compiler checked. go/rust/java lack
# a file-addressable module primitive and wasm cannot import a file at all; a
# `ref` on those four is refused at compile (lower.py), never faked. A tier joins
# this set only behind its own design note, mirroring `_CONFIG_INJECTION_TIERS`.
EXTERN_REF_TIERS = frozenset({"py", "ts"})


# ---------------------------------------------------------------------------
# compile-time resolution + the root-tree jail
# ---------------------------------------------------------------------------

def _reject_bad_written_path(written: str, backend: str, decl_name: str,
                             filename: str, line: int) -> None:
    """Refuse the one textual escape (an absolute path) and the empty path
    BEFORE any filesystem access. Unlike option A, `..` is ALLOWED here: the
    root-tree containment check on the resolved realpath is what holds, so a
    `src/plugins/x.rvl` may legitimately ref `../host/engine.py`."""
    if not written:
        raise RevlError(
            filename, line,
            f"empty @{backend} host-module ref path for extern `{decl_name}`")
    if os.path.isabs(written):
        raise RevlError(
            filename, line,
            f"@{backend} host-module ref path {written!r} for extern "
            f"`{decl_name}` is absolute",
            hint="a ref path is resolved relative to the declaring .rvl file's "
                 "directory and must sit inside the root compile tree; absolute "
                 "paths are refused (item 396 option B jail)")


def dotted_module(rel_path: str) -> str:
    """The py import specifier derived from a root-relative ref path:
    `src/host/engine.py` -> `src.host.engine`. Shared shape with the emitter
    (which re-derives it self-contained, having no `revl` import)."""
    stem = rel_path[:-3] if rel_path.endswith(".py") else rel_path
    return stem.replace("/", ".")


def _pick_root(child_real: str, root_dirs: list[str]) -> str | None:
    """The deepest (most specific) root directory that CONTAINS `child_real`,
    so the derived relative path is the shortest and the dotted specifier is
    unambiguous. `None` when the file sits outside every root tree — the ref is
    refused, because its module could not be rooted at deploy time."""
    best: str | None = None
    best_len = -1
    for root in root_dirs:
        root_real = os.path.realpath(root)
        if _contained(child_real, root_real):
            n = len(_normcase(root_real))
            if n > best_len:
                best, best_len = root_real, n
    return best


def _validate_specifier(rel_path: str, backend: str, decl_name: str,
                        filename: str, line: int) -> None:  # noqa: PLR0913
    """Refuse a ref whose resolved path cannot become a legal import specifier
    on the tier: py needs identifier path components under a `.py` file; ts needs
    an extension-ful path (node >= 23.6 native type stripping resolves only
    extension-ful ESM specifiers)."""
    if backend == "py":
        if not rel_path.endswith(".py"):
            raise RevlError(
                filename, line,
                f"@py host-module ref for extern `{decl_name}` resolves to "
                f"{rel_path!r}, which is not a `.py` file",
                hint="a @py ref imports a symbol from a python module; the "
                     "referenced file must be a `.py` module (item 396)")
        for part in dotted_module(rel_path).split("."):
            if not part.isidentifier() or keyword.iskeyword(part):
                raise RevlError(
                    filename, line,
                    f"@py host-module ref for extern `{decl_name}` resolves to "
                    f"{rel_path!r}, whose path component {part!r} is not a valid "
                    f"python module identifier",
                    hint="the import specifier is derived from the root-relative "
                         "path (`a/b/c.py` -> `a.b.c`), so every component must "
                         "be a valid identifier (item 396)")
    elif backend == "ts":
        # extension-ful, always: node's native type stripping resolves only
        # extension-ful ESM specifiers, so an extensionless ref path is refused.
        _, ext = os.path.splitext(rel_path)
        if not ext:
            raise RevlError(
                filename, line,
                f"@ts host-module ref for extern `{decl_name}` resolves to "
                f"{rel_path!r}, which has no file extension",
                hint="node's native type stripping (>= 23.6) resolves only "
                     "extension-ful ESM specifiers, so a @ts ref path must keep "
                     "its real extension, e.g. `host/engine.ts` (item 396)")


def _record(node, decl_name: str, rel_path: str, digest: str,
            filename: str) -> None:
    node.rel_path = rel_path.replace(os.sep, "/")
    node.sha256 = digest
    _validate_specifier(node.rel_path, node.backend, decl_name, filename,
                        node.line)


def resolve_refs(program, module_dir: str, root_dirs: list[str],
                 sources: dict[str, str], is_virtual: bool) -> None:
    """Resolve every `HostRef` node in `program`'s externs against the ROOT-tree
    jail, pinning `rel_path` (relative to the containing root) and `sha256` onto
    each node. A virtual module resolves through `sources` only (no disk read);
    a real module reads the file's bytes from disk. Refuses a ref that resolves
    outside every root tree, or whose path cannot become a legal tier specifier.
    """
    from .parser import HostRef

    real_roots = [os.path.realpath(r) for r in root_dirs]
    for ext in program.externs:
        for body in ext.bodies:
            if not isinstance(body, HostRef):
                continue
            _reject_bad_written_path(body.path, body.backend, ext.name,
                                     program.filename, body.line)
            if is_virtual:
                _resolve_ref_memory(body, ext.name, module_dir, sources,
                                    real_roots, program.filename)
            else:
                _resolve_ref_disk(body, ext.name, module_dir, real_roots,
                                  program.filename)


def _resolve_ref_disk(body, decl_name, module_dir, real_roots, filename):
    candidate = os.path.join(module_dir, body.path)
    if not os.path.exists(candidate):
        raise RevlError(
            filename, body.line,
            f"@{body.backend} host-module ref {body.path!r} for extern "
            f"`{decl_name}` not found (resolved to {candidate})",
            hint="a ref path is resolved relative to the declaring .rvl file's "
                 "directory (item 396 option B)")
    if not os.path.isfile(candidate):
        raise RevlError(
            filename, body.line,
            f"@{body.backend} host-module ref {body.path!r} for extern "
            f"`{decl_name}` is not a regular file (resolved to {candidate})")
    real = os.path.realpath(candidate)
    root = _pick_root(real, real_roots)
    if root is None:
        raise RevlError(
            filename, body.line,
            f"@{body.backend} host-module ref {body.path!r} for extern "
            f"`{decl_name}` resolves OUTSIDE the root compile tree ({real} is "
            f"not inside {', '.join(real_roots) or '(no root)'})",
            hint="option B's file must be reachable at deploy time through one "
                 "import root, so the ref must sit inside the root compile "
                 "file's directory tree. A module resolved from the stdlib or "
                 "REVL_IMPORT_PATH may not declare a ref — inline the body "
                 "(`= @backend { ... }`) or splice it (`= @backend file`) "
                 "instead (item 396 option B jail)")
    with open(real, "rb") as fh:
        data = fh.read()
    rel = os.path.relpath(real, root)
    _record(body, decl_name, rel, hashlib.sha256(data).hexdigest(), filename)


def _resolve_ref_memory(body, decl_name, module_dir, sources, real_roots,
                        filename):
    key = os.path.abspath(os.path.join(module_dir, body.path))
    root = _pick_root(key, [os.path.abspath(r) for r in real_roots])
    if root is None:
        raise RevlError(
            filename, body.line,
            f"@{body.backend} host-module ref {body.path!r} for extern "
            f"`{decl_name}` resolves outside the root compile tree ({key})",
            hint="item 396 option B jail")
    if key not in sources:
        raise RevlError(
            filename, body.line,
            f"@{body.backend} host-module ref {body.path!r} for extern "
            f"`{decl_name}` is not in the in-memory sources map (resolved key "
            f"{key})",
            hint="an in-memory compile reads nothing from disk, so a ref'd "
                 "module must be supplied through the same `modules=`/`sources=` "
                 "map (item 396)")
    data = sources[key].encode("utf-8")
    rel = os.path.relpath(key, root)
    _record(body, decl_name, rel, hashlib.sha256(data).hexdigest(), filename)


def program_has_ref(program) -> bool:
    """True if any extern still carries a `HostRef` (used by the loaderless
    `compile_source` refusal, mirroring `program_has_body_file`)."""
    from .parser import HostRef
    return any(isinstance(body, HostRef)
               for ext in program.externs for body in ext.bodies)


# ---------------------------------------------------------------------------
# deploy-time verification for the py run driver (`plug_refs`)
# ---------------------------------------------------------------------------

def ir_refs(ir: dict) -> list[dict]:
    """Every py ref carried by an IR document, as
    `[{"extern", "dotted", "symbol", "path", "sha256"}, ...]`."""
    out: list[dict] = []
    for ext in ir.get("externs") or []:
        ref = (ext.get("refs") or {}).get("py")
        if ref is not None:
            out.append({
                "extern": ext.get("name"),
                "dotted": dotted_module(ref["path"]),
                "symbol": ref["symbol"],
                "path": ref["path"],
                "sha256": ref["sha256"],
            })
    return out


def resolve_ref_spec_origin(dotted: str, search_paths: list[str]) -> str | None:
    """The file `dotted` would import from, WITHOUT executing any parent
    `__init__.py`. `importlib.util.find_spec` imports and runs the parent
    packages at plug — the exact host-code-at-load point option B forbids — so
    this walks `importlib.machinery.PathFinder.find_spec` level by level, feeding
    each level the parent spec's `submodule_search_locations` and never
    importing. Returns the leaf module's `origin` (its file), or `None` when the
    name does not resolve on these paths.

    Residual (stated): a parent `__init__` that mutates its own `__path__` at
    RUNTIME can make the real import diverge from this static walk; the walk sees
    the on-disk package layout, not a runtime-mutated one.
    """
    parts = dotted.split(".")
    paths: list[str] | None = list(search_paths)
    spec = None
    for i in range(len(parts)):
        fullname = ".".join(parts[:i + 1])
        spec = importlib.machinery.PathFinder.find_spec(fullname, paths)
        if spec is None:
            return None
        if i < len(parts) - 1:
            locs = spec.submodule_search_locations
            if not locs:
                return None
            paths = list(locs)
    return spec.origin if spec is not None else None


def _file_sha256(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def _module_file(mod) -> str | None:
    f = getattr(mod, "__file__", None)
    if f:
        return f
    paths = getattr(mod, "__path__", None)
    if paths:
        try:
            return os.path.join(list(paths)[0], "__init__.py")
        except (OSError, IndexError):
            return None
    return None


def plug_refs(ir: dict, root_dirs: list[str]) -> None:
    """Prepare a ref-carrying IR for execution, at plug, before any extern is
    first called. No-op when the IR carries no py ref, so a ref-free program's
    driver behaviour is byte-identical (it never touches `sys.path`).

    1. APPEND each root dir to `sys.path` (never prepend — a project file must
       not shadow a trusted runtime module the whole process over).
    2. For each ref: resolve the file the interpreter would import (no parent
       execution), and REFUSE a hash mismatch against the IR's pin, naming both
       the expected and the resolved path. A missing MODULE is left to the
       thunk's first-call ImportError (a late, loud error), matching the design.
    3. EVICT from `sys.modules` every ref-derived dotted name (and project-tree
       ancestor packages) so a re-emit/replug in one process runs the NEW code
       rather than a stale gen-N module python cached process-wide.
    """
    refs = ir_refs(ir)
    if not refs:
        return
    real_roots = [os.path.realpath(r) for r in root_dirs]
    for root in root_dirs:
        if root not in sys.path:
            sys.path.append(root)
    search = list(sys.path)
    for ref in refs:
        origin = resolve_ref_spec_origin(ref["dotted"], search)
        if origin is not None and os.path.isfile(origin):
            # Drop any stale cached bytecode for the ref'd source. Python's
            # source-mtime pyc invalidation has 1-SECOND granularity, so a
            # same-second edit of the same byte-length (the item-380 class this
            # fix exists to kill) would serve a stale `.pyc` even after the
            # sys.modules eviction below — and the hash check reads SOURCE bytes,
            # so it would pass while stale bytecode ran. Forcing a recompile from
            # the (hash-verified) source closes that.
            _drop_pyc(origin)
            got = _file_sha256(origin)
            if got is not None and got != ref["sha256"]:
                raise RevlError(
                    "<plug>", 0,
                    f"@py host-module ref for extern `{ref['extern']}` does not "
                    f"match the file pinned at compile: expected sha256 "
                    f"{ref['sha256']} for {ref['path']!r}, but the module "
                    f"`{ref['dotted']}` resolves to {origin} (sha256 {got})",
                    hint="the referenced file was edited, swapped, or SHADOWED "
                         "by an earlier importable module of the same dotted "
                         "name since compile (the appended `sys.path` means an "
                         "already-importable module wins). Recompile against the "
                         "deployed file, or restore the reviewed one (item 396 "
                         "option B deploy contract)")
        _evict_ref(ref["dotted"], real_roots)


def _drop_pyc(source_path: str) -> None:
    """Remove the cached bytecode for a source file, so the next import
    recompiles from source rather than trusting a mtime-stale `.pyc`."""
    try:
        pyc = importlib.util.cache_from_source(source_path)
    except (NotImplementedError, ValueError):
        return
    try:
        os.remove(pyc)
    except OSError:
        pass


def _evict_ref(dotted: str, real_roots: list[str]) -> None:
    """Drop from `sys.modules` the ref's dotted name and each ancestor package
    whose file sits inside a root tree — the modules made importable only by the
    appended root, so a fresh generation re-imports the current bytes. Their
    stale `.pyc` is dropped too (same 1-second-granularity concern). Trusted
    runtime modules (importable without the appended root) resolve OUTSIDE every
    root and are never evicted."""
    parts = dotted.split(".")
    for i in range(len(parts), 0, -1):
        name = ".".join(parts[:i])
        mod = sys.modules.get(name)
        if mod is None:
            continue
        mf = _module_file(mod)
        if mf is None:
            continue
        real = os.path.realpath(mf)
        if any(_contained(real, r) for r in real_roots):
            del sys.modules[name]
            _drop_pyc(real)
