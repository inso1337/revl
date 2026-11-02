"""Staging a release: download, verify, extract safely (docs/updates.md).

Fetch is the only step that touches the network and writes files for a
candidate update, and it does exactly two things: it puts the release bytes
under the update home, and it proves they are the bytes the manifest names.
Nothing is compiled and nothing is executed here. Extraction is manual and
allowlisted rather than ``tarfile.extractall``: an entry that is absolute,
climbs out with ``..``, carries a drive or backslash, or is a link or device
is a refusal, so a hostile archive cannot write outside its staging directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .errors import UpdateError
from .feed import ResolvedRelease, open_response, \
    source_archive_url
from .manifest import Artifact
from .semver import Version

DEFAULT_HOME = Path.home() / ".revl" / "updates"
MAX_BYTES = 300 * 1024 * 1024
MAX_FILES = 50_000
_USER_AGENT = "revl-update/1"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def update_home(path: str | os.PathLike | None = None) -> Path:
    """The update home: ``REVL_UPDATE_HOME``, else ``~/.revl/updates``."""
    if path is not None:
        return Path(path).expanduser()
    override = os.environ.get("REVL_UPDATE_HOME")
    return Path(override).expanduser() if override else DEFAULT_HOME


@dataclass(frozen=True)
class StagedRelease:
    project: str
    version: str
    tag: str
    commit: str
    directory: Path
    source_root: Path
    files: tuple[str, ...]
    digest: str
    artifact_name: str
    artifact_kind: str
    manifest_digest: str | None
    tag_fallback: bool
    provenance: dict

    def source_paths(self) -> list[str]:
        return [str(self.source_root / name) for name in self.files]

    def as_report(self) -> dict:
        return {
            "project": self.project,
            "version": self.version,
            "tag": self.tag,
            "commit": self.commit,
            "directory": str(self.directory),
            "artifact": {"name": self.artifact_name,
                         "kind": self.artifact_kind,
                         "sha256": self.digest},
            "manifestDigest": self.manifest_digest,
            "tagFallback": self.tag_fallback,
            "sources": {"count": len(self.files), "root": str(self.source_root)},
            "provenance": self.provenance,
        }


def _slug(release: ResolvedRelease, digest: str) -> str:
    project = _SAFE_NAME.sub("-", release.project).strip("-").lower()
    return f"{project}-{release.version}-{digest[:12]}"


def _safe_name(name: str) -> str:
    base = Path(name.replace("\\", "/")).name
    cleaned = _SAFE_NAME.sub("-", base).strip("-")
    return cleaned or "artifact"


def _error(code: str, message: str, *, fix: str | None = None,
           details: dict | None = None) -> UpdateError:
    return UpdateError(code, message, fix=fix, details=details)


def _size_refusal(url: str, expected: int, written: int) -> UpdateError:
    return _error(
        "FETCH_SIZE",
        f"the release artifact at {url} is not the {expected} bytes its "
        f"manifest declares ({'more than ' if written > expected else ''}"
        f"{written} bytes were served)",
        fix="do not retry blindly; the feed or the mirror served different "
            "bytes than the release names")


def _download(url: str, dest: Path, *, token: str | None, timeout: float,
              max_bytes: int = MAX_BYTES,
              expected_size: int | None = None) -> None:
    written = 0
    try:
        # The feed's opener: the token only travels to an allowlisted host, and
        # a cross-host redirect drops it. A manifest may name any http(s) URL,
        # so this is what keeps a private feed's token on the release host.
        with open_response(url, token, timeout=timeout) as response:
            with open(dest, "wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if expected_size is not None and written > expected_size:
                        raise _size_refusal(url, expected_size, written)
                    if written > max_bytes:
                        raise _error(
                            "FETCH_TOO_LARGE",
                            f"the release artifact exceeds the {max_bytes} "
                            "byte staging limit; a release that large is not "
                            "a source tree this updater stages")
                    handle.write(chunk)
        if expected_size is not None and written != expected_size:
            raise _size_refusal(url, expected_size, written)
    except UpdateError:
        raise
    except urllib.error.HTTPError as error:
        raise _error(
            "FETCH_HTTP",
            f"the release artifact at {url} answered HTTP {error.code}",
            details={"status": error.code}) from None
    except (urllib.error.URLError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        raise _error("FETCH_UNREACHABLE",
                     f"the release artifact at {url} could not be reached: "
                     f"{reason}",
                     fix="check the network or the feed mirror") from None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _member_name(name: str) -> str:
    """Validate one archive entry name, or refuse it."""
    if not name or name.startswith("/") or name.startswith("\\"):
        raise _error("STAGE_ARCHIVE_PATH",
                     f"the release archive names an absolute path {name!r}")
    if "\\" in name:
        raise _error(
            "STAGE_ARCHIVE_PATH",
            f"the release archive names {name!r} with a backslash; an archive "
            "entry is a POSIX path, and a backslash is how a Windows host "
            "reads a traversal")
    head, colon, tail = name.partition(":")
    if colon and len(head) == 1 and head.isalpha() and tail.startswith(("/", "\\")):
        raise _error("STAGE_ARCHIVE_PATH",
                     f"the release archive names a drive path {name!r}")
    parts = name.split("/")
    if any(part in ("..", "") for part in parts[:-1]) or parts[-1] == "..":
        raise _error("STAGE_ARCHIVE_PATH",
                     f"the release archive entry {name!r} climbs out of the "
                     "staging directory")
    return name


def _extract_tar(archive: Path, destination: Path,
                 max_bytes: int = MAX_BYTES) -> int:
    count = 0
    written = 0
    with tarfile.open(archive, "r:*") as tar:
        for member in tar:
            name = _member_name(member.name)
            if member.isdir():
                (destination / name).mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise _error(
                    "STAGE_ARCHIVE_ENTRY",
                    f"the release archive contains {name!r}, which is not a "
                    "regular file or directory; links, devices and fifos are "
                    "refused")
            count += 1
            if count > MAX_FILES:
                raise _error("STAGE_TOO_MANY_FILES",
                             f"the release archive contains more than "
                             f"{MAX_FILES} files")
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:  # pragma: no cover - isfile() guarantees a body
                raise _error("STAGE_ARCHIVE_ENTRY",
                             f"the release archive entry {name!r} has no body")
            with source, open(target, "wb") as handle:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise _error(
                            "STAGE_TOO_LARGE",
                            f"the release archive expands past the {max_bytes} "
                            "byte staging limit")
                    handle.write(chunk)
    return count


def _extract_zip(archive: Path, destination: Path,
                 max_bytes: int = MAX_BYTES) -> int:
    count = 0
    written = 0
    with zipfile.ZipFile(archive) as zipped:
        for entry in zipped.infolist():
            name = _member_name(entry.filename)
            mode = (entry.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise _error(
                    "STAGE_ARCHIVE_ENTRY",
                    f"the release archive contains a symlink {name!r}; links "
                    "are refused")
            if entry.is_dir():
                (destination / name).mkdir(parents=True, exist_ok=True)
                continue
            if mode and mode != 0o100000:
                raise _error(
                    "STAGE_ARCHIVE_ENTRY",
                    f"the release archive contains {name!r}, which is not a "
                    "regular file")
            count += 1
            if count > MAX_FILES:
                raise _error("STAGE_TOO_MANY_FILES",
                             f"the release archive contains more than "
                             f"{MAX_FILES} files")
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(entry) as source, open(target, "wb") as handle:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise _error(
                            "STAGE_TOO_LARGE",
                            f"the release archive expands past the {max_bytes} "
                            "byte staging limit")
                    handle.write(chunk)
    return count


def extract_archive(archive: Path, destination: Path,
                    max_bytes: int = MAX_BYTES) -> int:
    """Extract a tar or zip release archive safely, returning the file count.

    ``max_bytes`` bounds the DECOMPRESSED bytes written, not the download: a
    compressed artifact can expand without limit, and a source tree this
    updater stages is small by definition.
    """
    destination.mkdir(parents=True, exist_ok=True)
    if tarfile.is_tarfile(archive):
        return _extract_tar(archive, destination, max_bytes)
    if zipfile.is_zipfile(archive):
        return _extract_zip(archive, destination, max_bytes)
    raise _error(
        "STAGE_ARCHIVE_FORMAT",
        f"the release artifact {archive.name!r} is neither a tar nor a zip "
        "archive, so there is no source tree to stage")


def _walk_files(root: Path) -> tuple[list[Path], bool]:
    """Every regular file under ``root``, never following a link, and whether
    the tree holds a link at all (extraction refuses links, so one found
    later was planted)."""
    files: list[Path] = []
    linked = False
    for directory, dirs, names in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in list(dirs):
            if (base / name).is_symlink():
                linked = True
                dirs.remove(name)
        for name in names:
            path = base / name
            if path.is_symlink():
                linked = True
            elif path.is_file():
                files.append(path)
    return sorted(files), linked


def _source_digests(root: Path) -> dict[str, str]:
    """The sha256 of every regular file under an extracted source tree."""
    files, _linked = _walk_files(root)
    return {path.relative_to(root).as_posix(): _sha256(path)
            for path in files}


def staged_intact(staged: "StagedRelease") -> bool:
    """Whether a staged tree still holds exactly the files extracted from the
    verified archive.

    The archive's sha256 is checked once, at staging; the extracted files are
    what compiles, so a reused staging is trusted only when every file still
    has the digest recorded then, and no file was added or removed. A staging
    that recorded no digests (an older updater's) is not trusted.
    """
    recorded = (staged.provenance.get("sources") or {}).get("files")
    if not isinstance(recorded, dict):
        return False
    try:
        if _walk_files(staged.source_root)[1]:
            return False
        return _source_digests(staged.source_root) == recorded
    except OSError:
        return False


def _rvl_files(root: Path) -> tuple[str, ...]:
    files, _linked = _walk_files(root)
    return tuple(path.relative_to(root).as_posix() for path in files
                 if path.suffix == ".rvl")


def _pick_artifact(release: ResolvedRelease) -> Artifact:
    manifest = release.manifest
    if manifest is None:
        return Artifact(
            name=f"{release.project.rsplit('/', 1)[-1]}-{release.commit[:12]}.tar.gz",
            kind="source",
            url=source_archive_url(release.repo, release.commit),
            sha256="0" * 64)
    artifact = manifest.unpackable_artifact()
    if artifact is None:
        raise _error(
            "STAGE_NO_ARTIFACT",
            f"release {release.version} ships no unpackable source artifact "
            "(the kinds are source, sdist, bundle and wheel), so it cannot be "
            "staged as a source tree",
            fix="publish a source artifact with the release, or apply the "
                "release's installer its own way")
    return artifact


def stage_release(release: ResolvedRelease, *, home_dir: str | os.PathLike | None = None,
                  token: str | None = None, timeout: float = 20.0,
                  api_base_override: str | None = None) -> StagedRelease:
    """Download, verify and extract one release under the update home."""
    root = update_home(home_dir)
    staged_root = root / "staged"
    tmp_root = root / "tmp"
    staged_root.mkdir(parents=True, exist_ok=True)
    tmp_root.mkdir(parents=True, exist_ok=True)
    artifact = _pick_artifact(release)
    url = artifact.url
    if release.tag_fallback and api_base_override:
        url = source_archive_url(release.repo, release.commit,
                                 api_base_override)
    work = tmp_root / f"{_SAFE_NAME.sub('-', release.project)}-{release.version}-" \
                      f"{os.getpid()}-{time.time_ns()}"
    work.mkdir(parents=True)
    artifact_path = work / _safe_name(artifact.name)
    expected_size = (artifact.size if release.manifest is not None else None)
    try:
        _download(url, artifact_path, token=token, timeout=timeout,
                  expected_size=expected_size)
        actual = _sha256(artifact_path)
        if release.manifest is not None and actual != artifact.sha256:
            raise _error(
                "FETCH_DIGEST",
                f"the artifact {artifact.name!r} of release {release.version} "
                f"does not match its manifest: expected sha256 "
                f"{artifact.sha256}, got {actual}",
                fix="do not retry blindly; the feed or the mirror served "
                    "different bytes than the release names")
        extracted = work / "extract"
        extract_archive(artifact_path, extracted)
        entries = [entry for entry in extracted.iterdir()]
        source_root = work / "source"
        if len(entries) == 1 and entries[0].is_dir():
            entries[0].rename(source_root)
        else:
            extracted.rename(source_root)
        files = _rvl_files(source_root)
        manifest_digest = release.manifest.digest() if release.manifest else None
        provenance = {
            "schema": "revl.update.provenance/1",
            "project": release.project,
            "version": release.version,
            "tag": release.tag,
            "commit": release.commit,
            "repo": release.repo,
            "manifestSource": release.manifest_source,
            "manifestDigest": manifest_digest,
            "artifact": {"name": artifact.name, "kind": artifact.kind,
                         "url": url, "sha256": actual},
            "tagFallback": release.tag_fallback,
            "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sources": {"count": len(files),
                        "files": _source_digests(source_root)},
        }
        (work / "provenance.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        final = staged_root / _slug(release, actual)
        if final.exists():
            existing = _read_provenance(final)
            reused = _staged(final, existing)
            if (existing.get("artifact", {}).get("sha256") == actual
                    and staged_intact(reused)):
                shutil.rmtree(work, ignore_errors=True)
                return reused
            shutil.rmtree(final)
        os.replace(work, final)
        return _staged(final, provenance)
    finally:
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)


def _read_provenance(directory: Path) -> dict:
    path = directory / "provenance.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise _error("STAGE_CORRUPT",
                     f"the staging directory {directory} has no readable "
                     f"provenance: {error}") from None
    if not isinstance(document, dict):
        raise _error("STAGE_CORRUPT",
                     f"the staging directory {directory} has a provenance "
                     "document that is not an object")
    return document


def _staged(directory: Path, provenance: dict) -> StagedRelease:
    source_root = directory / "source"
    files = _rvl_files(source_root)
    artifact = provenance.get("artifact") or {}
    return StagedRelease(
        project=str(provenance.get("project", "")),
        version=str(provenance.get("version", "")),
        tag=str(provenance.get("tag", "")),
        commit=str(provenance.get("commit", "")),
        directory=directory, source_root=source_root, files=files,
        digest=str(artifact.get("sha256", "")),
        artifact_name=str(artifact.get("name", "")),
        artifact_kind=str(artifact.get("kind", "")),
        manifest_digest=provenance.get("manifestDigest"),
        tag_fallback=bool(provenance.get("tagFallback")),
        provenance=provenance)


def find_staged(home_dir: str | os.PathLike | None = None,
                project: str | None = None) -> list[StagedRelease]:
    """Every staged release in the update home, newest first."""
    root = update_home(home_dir)
    staged_root = root / "staged"
    if not staged_root.is_dir():
        return []
    found: list[StagedRelease] = []
    for directory in staged_root.iterdir():
        if not directory.is_dir():
            continue
        try:
            provenance = _read_provenance(directory)
            if project is not None and provenance.get("project") != project:
                continue
            found.append(_staged(directory, provenance))
        except UpdateError:
            continue
    def _order(release: "StagedRelease") -> tuple:
        parsed = Version.parse_or_none(release.version)
        # newest first; a version this updater cannot parse sorts last, never
        # first, because a corrupt provenance must not outrank a real one.
        return (release.project, 1 if parsed is not None else 0,
                parsed if parsed is not None else Version.parse("0.0.0"))

    found.sort(key=_order, reverse=True)
    return found
