"""The release manifest, edition 1 (docs/updates.md).

A GitHub release carries an asset named ``release.json`` next to its build
artifacts. The manifest is data and is validated strictly: an unknown schema,
a missing required field, a tag that does not name its version, or a bad
sha256 is a refusal with a code, never a value the updater quietly guesses
around.

The format is the only wire format shared by the three repositories, so this
module is the single definition of it. ``tools/release_manifest.py`` writes
the same document from a tagged checkout.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from .errors import UpdateError
from .semver import Version, VersionError

SCHEMA = "revl.release/1"

#: Artifact kinds the format admits. ``source`` is a source tree the running
#: gate can compile (the only kind a live apply may use); the others are build
#: products a host fetches and installs its own way.
ARTIFACT_KINDS = ("source", "sdist", "wheel", "bundle", "installer")
#: Preference order when a caller stages a release for a live apply.
UNPACKABLE_KINDS = ("source", "sdist", "bundle", "wheel")

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_URL = re.compile(r"^(?:https?|file)://\S+$")


@dataclass(frozen=True)
class Artifact:
    name: str
    kind: str
    url: str
    sha256: str
    size: int | None = None

    def as_dict(self) -> dict:
        document = {"name": self.name, "kind": self.kind,
                    "url": self.url, "sha256": self.sha256}
        if self.size is not None:
            document["size"] = self.size
        return document


@dataclass(frozen=True)
class ReleaseManifest:
    schema: str
    project: str
    version: str
    tag: str
    commit: str
    artifacts: tuple[Artifact, ...]
    compat: dict
    pins: dict = field(default_factory=dict)
    live: bool = False
    host_changes: bool = False
    channel: str = "stable"
    url: str | None = None
    published_at: str | None = None
    changes: dict | None = None
    notes: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def semver(self) -> Version:
        return Version.parse(self.version)

    def unpackable_artifact(self) -> Artifact | None:
        """The artifact a live or staged apply can unpack, in preference order."""
        for kind in UNPACKABLE_KINDS:
            for artifact in self.artifacts:
                if artifact.kind == kind:
                    return artifact
        return None

    def artifact(self, name: str) -> Artifact | None:
        for artifact in self.artifacts:
            if artifact.name == name:
                return artifact
        return None

    def document(self) -> dict:
        """The canonical form written by ``tools/release_manifest.py``."""
        document: dict = {
            "schema": self.schema,
            "project": self.project,
            "version": self.version,
            "tag": self.tag,
            "commit": self.commit,
            "channel": self.channel,
            "artifacts": [artifact.as_dict() for artifact in self.artifacts],
            "compat": dict(self.compat),
            "live": self.live,
            "hostChanges": self.host_changes,
            "pins": dict(self.pins),
        }
        if self.published_at:
            document["publishedAt"] = self.published_at
        if self.url:
            document["url"] = self.url
        if self.changes is not None:
            document["changes"] = self.changes
        if self.notes is not None:
            document["notes"] = self.notes
        document.update(self.extra)
        return document

    def digest(self) -> str:
        """sha256 of the canonical document: what a plan binds itself to."""
        encoded = json.dumps(self.document(), sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _refuse(code: str, message: str, fix: str | None = None) -> UpdateError:
    return UpdateError(code, message, fix=fix)


def _string(document: dict, key: str, *, refusal: str = "MANIFEST_FIELD",
            where: str = "manifest") -> str:
    # `refusal=`, not `code=`: the updater's refusal codes are its own surface
    # (UpdateError), not compiler diagnostics, and the diagnostic-code scanner
    # reads `code=` as an emission. The compiler's diagnostic vocabulary is not
    # the updater's, and `revl explain` does not answer for these.
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _refuse(refusal, f"`{key}` must be a non-empty string in the {where}")
    return value


def _bool(document: dict, key: str, default: bool = False) -> bool:
    value = document.get(key, default)
    if not isinstance(value, bool):
        raise _refuse("MANIFEST_FIELD",
                      f"`{key}` must be true or false in the manifest")
    return value


def _parse_artifact(entry: object, index: int) -> Artifact:
    if not isinstance(entry, dict):
        raise _refuse("MANIFEST_ARTIFACT",
                      f"artifact {index} must be an object")
    name = _string(entry, "name", refusal="MANIFEST_ARTIFACT",
                   where=f"artifact {index}")
    kind = _string(entry, "kind", refusal="MANIFEST_ARTIFACT",
                   where=f"artifact {index}")
    if kind not in ARTIFACT_KINDS:
        raise _refuse(
            "MANIFEST_ARTIFACT",
            f"artifact {name!r} has kind {kind!r}; expected one of "
            + ", ".join(ARTIFACT_KINDS))
    url = _string(entry, "url", refusal="MANIFEST_ARTIFACT",
                  where=f"artifact {index}")
    if _URL.match(url) is None:
        raise _refuse(
            "MANIFEST_ARTIFACT",
            f"artifact {name!r} has url {url!r}; only http(s) and file URLs "
            "are accepted, so a fetched release is never a relative path")
    digest = _string(entry, "sha256", refusal="MANIFEST_ARTIFACT",
                     where=f"artifact {index}")
    if _HEX64.match(digest) is None:
        raise _refuse(
            "MANIFEST_ARTIFACT",
            f"artifact {name!r} has sha256 {digest!r}; expected 64 lowercase "
            "hex characters")
    size = entry.get("size")
    if size is not None and (not isinstance(size, int) or size < 0):
        raise _refuse("MANIFEST_ARTIFACT",
                      f"artifact {name!r} has size {size!r}; expected a "
                      "non-negative integer")
    return Artifact(name=name, kind=kind, url=url, sha256=digest, size=size)


def _parse_pins(document: object) -> dict:
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise _refuse("MANIFEST_PINS", "`pins` must be an object")
    pins: dict = {}
    for name, value in document.items():
        if not isinstance(name, str) or not name:
            raise _refuse("MANIFEST_PINS",
                          "every pin name must be a non-empty string")
        if value in (None, ""):
            pins[name] = None
        elif isinstance(value, str) and _HEX40.match(value):
            pins[name] = value
        else:
            raise _refuse(
                "MANIFEST_PINS",
                f"pin `{name}` is {value!r}; expected a 40 character commit "
                "sha or null")
    return pins


def _parse_compat(document: object) -> dict:
    if not isinstance(document, dict):
        raise _refuse("MANIFEST_COMPAT", "`compat` must be an object")
    language = _string(document, "language", refusal="MANIFEST_COMPAT",
                       where="compat")
    try:
        Version.parse(language)
    except VersionError as error:
        raise _refuse("MANIFEST_COMPAT", f"compat.language: {error}") from None
    compat: dict = {"language": language}
    for key in ("gateApi", "frontier", "minPython"):
        value = document.get(key)
        if value is not None:
            if not isinstance(value, str) or not value:
                raise _refuse("MANIFEST_COMPAT",
                              f"compat.`{key}` must be a non-empty string")
            compat[key] = value
    revision = document.get("irVersion")
    if revision is not None:
        revisions = revision if isinstance(revision, list) else [revision]
        if not all(isinstance(item, int) and item >= 0 for item in revisions):
            raise _refuse("MANIFEST_COMPAT",
                          "compat.`irVersion` must be a non-negative integer "
                          "or a list of them")
        compat["irVersion"] = revision
    unknown = {key: value for key, value in document.items()
               if key not in compat and key != "language"}
    if unknown:
        compat["extra"] = unknown
    return compat


_KNOWN = frozenset({
    "schema", "project", "version", "tag", "commit", "channel",
    "publishedAt", "url", "artifacts", "compat", "live", "hostChanges",
    "pins", "changes", "notes",
})


def require_remote_artifacts(manifest: ReleaseManifest) -> None:
    """Refuse a fetched manifest that names a local artifact.

    A manifest read from the operator's own disk may point at a local file. A
    manifest that arrived over the network may not: a ``file://`` URL there
    would make the release's publisher choose a file on this host to stage.
    """
    for artifact in manifest.artifacts:
        if not artifact.url.startswith(("http://", "https://")):
            raise _refuse(
                "MANIFEST_URL",
                f"artifact {artifact.name!r} of a fetched manifest names "
                f"{artifact.url!r}; a manifest fetched over the network may "
                "only name http(s) artifacts",
                fix="publish the artifact at an https URL")


def parse_manifest(document: object) -> ReleaseManifest:
    """Validate and parse a release manifest document.

    Accepts the parsed JSON value, or its text/bytes form. Raises
    :class:`~revl.update.errors.UpdateError` with a ``MANIFEST_*`` code.
    Unknown top level fields are preserved under ``extra`` rather than
    refused, so a newer release can add a field without breaking an older
    updater; a field the updater acts on is validated, never ignored.
    """
    if isinstance(document, (bytes, bytearray)):
        document = document.decode("utf-8", errors="replace")
    if isinstance(document, str):
        try:
            document = json.loads(document)
        except json.JSONDecodeError as error:
            raise _refuse("MANIFEST_JSON",
                          f"the release manifest is not JSON: {error}") from None
    if not isinstance(document, dict):
        raise _refuse("MANIFEST_NOT_OBJECT",
                      "the release manifest must be a JSON object")
    schema = document.get("schema")
    if schema != SCHEMA:
        raise _refuse(
            "MANIFEST_SCHEMA",
            f"release manifest schema is {schema!r}; this updater reads "
            f"{SCHEMA!r}. A newer manifest needs a newer updater",
            fix="upgrade the toolchain that reads the release feed")
    project = _string(document, "project", refusal="MANIFEST_PROJECT")
    if _PROJECT.match(project) is None:
        raise _refuse("MANIFEST_PROJECT",
                      f"`project` is {project!r}; expected a repository name "
                      "such as `revl` or `owner/name`")
    version_text = _string(document, "version", refusal="MANIFEST_VERSION")
    try:
        version = Version.parse(version_text)
    except VersionError as error:
        raise _refuse("MANIFEST_VERSION", f"manifest version: {error}") from None
    tag = _string(document, "tag", refusal="MANIFEST_TAG")
    if tag not in (f"v{version}", str(version)):
        raise _refuse(
            "MANIFEST_TAG",
            f"`tag` is {tag!r} but `version` is {version}; a release tag names "
            f"its version (`v{version}`)")
    commit = _string(document, "commit", refusal="MANIFEST_COMMIT")
    if _HEX40.match(commit) is None:
        raise _refuse("MANIFEST_COMMIT",
                      "`commit` must be a 40 character lowercase commit sha")
    artifacts_document = document.get("artifacts")
    if not isinstance(artifacts_document, list) or not artifacts_document:
        raise _refuse("MANIFEST_ARTIFACTS",
                      "`artifacts` must be a non-empty list: a release names "
                      "what it ships and the sha256 of each build")
    artifacts = tuple(
        _parse_artifact(entry, index)
        for index, entry in enumerate(artifacts_document))
    names = [artifact.name for artifact in artifacts]
    if len(set(names)) != len(names):
        raise _refuse("MANIFEST_ARTIFACTS",
                      "two artifacts share a name; a name identifies one file")
    compat = _parse_compat(document.get("compat"))
    pins = _parse_pins(document.get("pins"))
    live = _bool(document, "live")
    host_changes = _bool(document, "hostChanges")
    channel = document.get("channel", "stable")
    if not isinstance(channel, str) or not channel:
        raise _refuse("MANIFEST_FIELD", "`channel` must be a non-empty string")
    url = document.get("url")
    if url is not None and (not isinstance(url, str) or not url):
        raise _refuse("MANIFEST_FIELD", "`url` must be a non-empty string")
    published = document.get("publishedAt")
    if published is not None and not isinstance(published, str):
        raise _refuse("MANIFEST_FIELD", "`publishedAt` must be a string")
    changes = document.get("changes")
    if changes is not None and not isinstance(changes, dict):
        raise _refuse("MANIFEST_FIELD", "`changes` must be an object")
    notes = document.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise _refuse("MANIFEST_FIELD", "`notes` must be a string")
    extra = {key: value for key, value in document.items() if key not in _KNOWN}
    return ReleaseManifest(
        schema=schema, project=project, version=str(version), tag=tag,
        commit=commit, artifacts=artifacts, compat=compat, pins=pins,
        live=live, host_changes=host_changes, channel=channel, url=url,
        published_at=published, changes=changes, notes=notes, extra=extra)
