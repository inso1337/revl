"""Multi-file artifacts for the private peer pool (item 524, issue #1198).

A pool task pins what a peer runs by one digest. For a single file that digest
is the sha256 of its bytes (`pool_dispatch.artifact_digest`). A composition
made of several files needs one digest that covers all of them, and it has to
cover more than their bytes:

* **every file's content**, so an edited file is a different bundle;
* **every file's path**, so a file moved from ``lib/a.rvl`` to ``a.rvl`` is a
  different bundle (a ``use`` resolves by path, so moving a file changes the
  program);
* **every file's mode**, so a file that becomes executable is a different
  bundle;
* **the set itself**, so a bundle with a file added or removed is a different
  bundle.

The manifest
============

A bundle is described by its MANIFEST: one entry per file, ``{"path", "mode",
"digest"}``, where ``digest`` is the sha256 of the file's bytes. The bundle
digest is the sha256 of a domain tag followed by the canonical JSON of the
manifest sorted by path. The tag keeps this digest distinct from every other
hash in the pool protocols.

A task carries the manifest and the files separately. That is what lets the
peer name what is wrong rather than only saying that something is: a manifest
entry with no file is a MISSING file, a file with no manifest entry is an EXTRA
file, and a file whose bytes or mode disagree with its entry is a TAMPERED
file. Each is named by path.

Paths
=====

A path in a bundle names a file the peer will create, so it is constrained
here rather than sanitised at the place it is joined onto a directory. The
rules are the same on both ends, so the operator refuses before sending what
the peer would refuse on arrival:

* relative, ``/``-separated, at most 255 characters and 16 segments;
* each segment is ASCII letters, digits, ``-``, ``_`` or ``.``, is not ``.``
  or ``..``, and does not start with ``-`` (a path is handed to the runner on
  its command line, so a leading ``-`` would read as a flag);
* no two paths are equal ignoring case, and no path is a directory of another,
  so the files cannot collide when they are written out on a case-insensitive
  file system.

A mode is ``0644`` or ``0755``, the two a source tree has. Anything else is a
malformed manifest.

This module is pure data and checks. What a problem MEANS for a task (which
refusal link it is) is `pool_dispatch`'s decision.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Optional, Sequence

from .attest import _canonical_bytes

BUNDLE_KIND = "revl.pool-bundle"
BUNDLE_VERSION = "1.0"

#: Domain tag for the bundle digest, distinct from every signing domain in the
#: pool protocols and from the raw sha256 a single-file artifact is pinned by.
BUNDLE_DOMAIN = b"revl.pool-bundle/v1\x00"

MODE_FILE = "0644"
MODE_EXEC = "0755"
MODES = (MODE_FILE, MODE_EXEC)

MAX_FILES = 256
MAX_PATH = 255
MAX_DEPTH = 16

#: What a received bundle can be wrong about. `pool_dispatch` maps each to one
#: refusal link.
PROBLEM_SHAPE = "shape"
PROBLEM_PATH = "path"
PROBLEM_DIGEST = "digest"
PROBLEM_MISSING = "missing"
PROBLEM_EXTRA = "extra"
PROBLEM_FILE_DIGEST = "file-digest"

_SEGMENT_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


class BundleError(ValueError):
    """An operator-side fault building a bundle from files on disk. ``paths``
    are the offending names as the bundle would spell them, and ``kind`` is
    :data:`PROBLEM_PATH` for a name the peer would refuse or
    :data:`PROBLEM_SHAPE` for anything else."""

    def __init__(self, paths: Sequence[str], reason: str, *,
                 kind: str = PROBLEM_PATH):
        named = ", ".join(paths)
        super().__init__(f"{named}: {reason}" if named else reason)
        self.paths = tuple(paths)
        self.reason = reason
        self.kind = kind


@dataclass(frozen=True)
class Problem:
    """Why a received bundle is refused: a kind, the paths it concerns, and a
    reason a human can act on."""

    kind: str
    paths: tuple[str, ...]
    reason: str


def file_digest(data: bytes) -> str:
    return hashlib.sha256(bytes(data)).hexdigest()


# ---------------------------------------------------------------------------
# paths and manifests
# ---------------------------------------------------------------------------


def path_problem(path: Any) -> str:
    """Why ``path`` may not name a file in a bundle, or "" if it may."""
    if not isinstance(path, str) or not path:
        return "a bundle path must be a non-empty string"
    if len(path) > MAX_PATH:
        return f"a bundle path is at most {MAX_PATH} characters"
    if path.startswith("/") or "\\" in path or ":" in path:
        return ("a bundle path is relative and '/'-separated, with no drive "
                "or backslash")
    segments = path.split("/")
    if len(segments) > MAX_DEPTH:
        return f"a bundle path has at most {MAX_DEPTH} segments"
    for segment in segments:
        if segment in ("", ".", ".."):
            return ("a bundle path has no empty, '.' or '..' segment, because "
                    "it names a file inside the peer's workspace")
        if segment.startswith("-"):
            return ("no segment of a bundle path starts with '-', because the "
                    "path is passed to the runner on its command line")
        if not set(segment) <= _SEGMENT_CHARS:
            return ("a bundle path segment is ASCII letters, digits, '-', '_' "
                    "or '.'")
    return ""


def _collisions(paths: Iterable[str]) -> Optional[tuple[str, str]]:
    """Two paths that would land on the same file, or a file and a directory of
    the same name, once written to a case-insensitive file system."""
    files: dict[str, str] = {}
    dirs: dict[str, str] = {}
    for path in paths:
        folded = path.casefold()
        if folded in files:
            return files[folded], path
        if folded in dirs:
            return dirs[folded], path
        files[folded] = path
        parts = folded.split("/")
        for depth in range(1, len(parts)):
            parent = "/".join(parts[:depth])
            if parent in files:
                return files[parent], path
            dirs.setdefault(parent, path)
    return None


def _entry_problem(entry: Any) -> str:
    if not isinstance(entry, Mapping):
        return "a manifest entry is not an object"
    if set(entry) != {"path", "mode", "digest"}:
        return (f"a manifest entry has exactly path, mode and digest; this one "
                f"has {', '.join(sorted(map(str, entry)))}")
    if entry["mode"] not in MODES:
        return (f"mode {entry['mode']!r} is not one of "
                f"{', '.join(MODES)}")
    digest = entry["digest"]
    if not isinstance(digest, str) or len(digest) != 64 or not all(
            c in "0123456789abcdef" for c in digest):
        return "a manifest digest is 64 lowercase hex characters"
    return ""


def manifest_problem(manifest: Any) -> Optional[Problem]:
    """Why ``manifest`` is not a bundle manifest, or ``None``.

    Checked before the manifest is hashed or any path in it is used, so a
    hostile manifest is refused on its shape and never reaches the file
    system."""
    if not isinstance(manifest, list) or not manifest:
        return Problem(PROBLEM_SHAPE, (), "the manifest is not a non-empty list")
    if len(manifest) > MAX_FILES:
        return Problem(PROBLEM_SHAPE, (),
                       f"a bundle has at most {MAX_FILES} files")
    for entry in manifest:
        path = entry.get("path") if isinstance(entry, Mapping) else None
        why = path_problem(path)
        if why:
            return Problem(PROBLEM_PATH, (str(path),), why)
        why = _entry_problem(entry)
        if why:
            return Problem(PROBLEM_SHAPE, (path,), why)
    clash = _collisions(entry["path"] for entry in manifest)
    if clash:
        return Problem(PROBLEM_PATH, clash,
                       f"{clash[0]!r} and {clash[1]!r} would be the same file "
                       f"or directory on a case-insensitive file system")
    if not any(entry["path"].endswith(".rvl") for entry in manifest):
        return Problem(PROBLEM_SHAPE, (),
                       "a bundle has at least one .rvl file to run")
    return None


def bundle_digest(manifest: Sequence[Mapping[str, Any]]) -> str:
    """The digest a bundle is pinned by: sha256 over the domain tag and the
    canonical JSON of the manifest, sorted by path. The order files were given
    in does not change it; any path, mode or content does."""
    files = sorted(({"path": e["path"], "mode": e["mode"],
                     "digest": e["digest"]} for e in manifest),
                   key=lambda e: e["path"])
    body = {"kind": BUNDLE_KIND, "version": BUNDLE_VERSION, "files": files}
    return hashlib.sha256(BUNDLE_DOMAIN + _canonical_bytes(body)).hexdigest()


# ---------------------------------------------------------------------------
# a bundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BundleFile:
    path: str
    mode: str
    data: bytes

    def entry(self) -> dict:
        return {"path": self.path, "mode": self.mode,
                "digest": file_digest(self.data)}


@dataclass(frozen=True)
class Bundle:
    """The files, sorted by path. Build one with :func:`from_paths` on the
    operator side or :func:`receive` on the peer side; both check every path."""

    files: tuple[BundleFile, ...]

    def manifest(self) -> list[dict]:
        return [f.entry() for f in self.files]

    def digest(self) -> str:
        return bundle_digest(self.manifest())

    def roots(self) -> list[str]:
        """The ``.rvl`` files, in path order: what the runner is handed."""
        return [f.path for f in self.files if f.path.endswith(".rvl")]

    def to_wire(self) -> dict:
        """The manifest and the files, separately, as a task carries them."""
        return {"manifest": self.manifest(),
                "files": [{"path": f.path, "mode": f.mode,
                           "content": base64.b64encode(f.data).decode("ascii")}
                          for f in self.files]}

    def describe(self) -> dict:
        """What a ledger entry, a receipt or a delivery record names."""
        return {"digest": self.digest(), "files": self.manifest()}

    def materialize(self, root: Path) -> None:
        """Write every file under ``root``, which must be a fresh directory.

        Each file is created exclusively and without following a link, so
        nothing already there can redirect a write, and each resolved path is
        checked to be inside ``root`` before it is written."""
        base = Path(root).resolve()
        for f in self.files:
            target = base.joinpath(*f.path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            if base not in target.parent.resolve().parents \
                    and target.parent.resolve() != base:
                raise BundleError((f.path,), "resolves outside the workspace")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(target, flags, 0o600)
            try:
                os.write(fd, f.data)
                os.fchmod(fd, int(f.mode, 8))
            finally:
                os.close(fd)


def bundle_name(given: str, cwd: Optional[Path] = None) -> str:
    """The path a file given on the command line has inside a bundle: relative
    to the working directory, normalised, '/'-separated. Checked by
    :func:`path_problem` afterwards, so ``../x.rvl`` stays ``../x.rvl`` and is
    refused rather than quietly rewritten."""
    base = str(cwd) if cwd is not None else os.getcwd()
    name = os.path.relpath(os.path.abspath(os.path.join(base, given)), base)
    return PurePosixPath(*Path(name).parts).as_posix()


def from_paths(paths: Sequence[str], *, cwd: Optional[Path] = None) -> Bundle:
    """The operator side: read the files, name each by its path relative to
    the working directory, and take its mode from the executable bit.

    Every name is checked before any file is read. Raises
    :class:`BundleError` naming the file for a name the peer would refuse, and
    ``OSError`` for a file that cannot be read."""
    base = Path(cwd) if cwd is not None else Path.cwd()
    names = [bundle_name(given, base) for given in paths]
    for name in names:
        why = path_problem(name)
        if why:
            raise BundleError((name,), why, kind=PROBLEM_PATH)
    files = []
    for given, name in zip(paths, names):
        source = base / given
        mode = MODE_EXEC if source.stat().st_mode & (
            stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH) else MODE_FILE
        files.append(BundleFile(name, mode, source.read_bytes()))
    files.sort(key=lambda f: f.path)
    problem = manifest_problem([f.entry() for f in files])
    if problem is not None:
        raise BundleError(problem.paths, problem.reason, kind=problem.kind)
    return Bundle(tuple(files))


def receive(wire: Any, pinned_digest: str) -> tuple[Optional[Bundle],
                                                     Optional[Problem]]:
    """The peer side: check a carried bundle against the digest the task pins,
    before anything is written or run.

    In this order, each failure naming its paths: the manifest's shape and
    paths; the carried files' shape and paths; the manifest against the pinned
    digest; files the manifest lists and the task does not carry; files the
    task carries and the manifest does not list; and each file's bytes and
    mode against its entry. Never raises."""
    if not isinstance(wire, Mapping) or set(wire) != {"manifest", "files"}:
        return None, Problem(PROBLEM_SHAPE, (),
                             "the bundle is not {manifest, files}")
    manifest = wire["manifest"]
    problem = manifest_problem(manifest)
    if problem is not None:
        return None, problem
    carried = wire["files"]
    if not isinstance(carried, list) or len(carried) > MAX_FILES:
        return None, Problem(PROBLEM_SHAPE, (),
                             f"the files are not a list of at most "
                             f"{MAX_FILES}")
    received: dict[str, tuple[str, bytes]] = {}
    duplicated: list[str] = []
    for item in carried:
        if not isinstance(item, Mapping) \
                or set(item) != {"path", "mode", "content"}:
            return None, Problem(PROBLEM_SHAPE, (),
                                 "a carried file is not {path, mode, content}")
        why = path_problem(item["path"])
        if why:
            return None, Problem(PROBLEM_PATH, (str(item["path"]),), why)
        if not isinstance(item["mode"], str) \
                or not isinstance(item["content"], str):
            return None, Problem(PROBLEM_SHAPE, (item["path"],),
                                 "a carried file's mode and content are "
                                 "strings")
        try:
            data = base64.b64decode(item["content"], validate=True)
        except (binascii.Error, ValueError):
            return None, Problem(PROBLEM_SHAPE, (item["path"],),
                                 "a carried file's content is not base64")
        if item["path"] in received:
            duplicated.append(item["path"])
        received[item["path"]] = (item["mode"], data)
    actual = bundle_digest(manifest)
    if actual != pinned_digest:
        return None, Problem(
            PROBLEM_DIGEST, (),
            f"the manifest hashes to {actual[:16]} and the task pins "
            f"{str(pinned_digest)[:16]}")
    listed = {entry["path"]: entry for entry in manifest}
    missing = sorted(set(listed) - set(received))
    if missing:
        return None, Problem(PROBLEM_MISSING, tuple(missing),
                             f"the manifest lists {', '.join(missing)} and "
                             f"the task does not carry "
                             f"{'it' if len(missing) == 1 else 'them'}")
    extra = sorted(set(received) - set(listed)) + sorted(duplicated)
    if extra:
        return None, Problem(PROBLEM_EXTRA, tuple(extra),
                             f"the task carries {', '.join(extra)} and the "
                             f"manifest does not list "
                             f"{'it' if len(extra) == 1 else 'them'} (or lists "
                             f"it once)")
    wrong = sorted(path for path, (mode, data) in received.items()
                   if file_digest(data) != listed[path]["digest"]
                   or mode != listed[path]["mode"])
    if wrong:
        return None, Problem(PROBLEM_FILE_DIGEST, tuple(wrong),
                             f"{', '.join(wrong)} "
                             f"{'does' if len(wrong) == 1 else 'do'} not "
                             f"match the manifest's digest and mode")
    files = tuple(BundleFile(path, received[path][0], received[path][1])
                  for path in sorted(received))
    return Bundle(files), None


def described_problem(block: Any, artifact_digest: str) -> str:
    """Why a receipt's ``bundle`` block does not describe ``artifact_digest``,
    or "". A receipt that names a bundle names the same digest twice, once as
    its artifact and once over its manifest; the two must agree."""
    if not isinstance(block, Mapping) or set(block) != {"digest", "files"}:
        return "the bundle block is not {digest, files}"
    problem = manifest_problem(block["files"])
    if problem is not None:
        return f"the bundle manifest is malformed: {problem.reason}"
    actual = bundle_digest(block["files"])
    if actual != block["digest"] or actual != artifact_digest:
        return (f"the bundle manifest hashes to {actual[:16]} and the receipt "
                f"names artifact {str(artifact_digest)[:16]}")
    return ""
