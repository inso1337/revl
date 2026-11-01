"""The release feed: GitHub releases, manifest asset, tag fallback (docs/updates.md).

A feed is one repository plus one channel. The resolver reads GitHub's REST
API with the standard library only, because the updater must work on a bare
interpreter. Three failures are distinguished rather than collapsed into one
message: the feed is unreachable, the token is missing or refused, and the
feed answered but has no release this updater can read.

The manifest wins over tags. A repository without a release manifest still
resolves through its newest semantic tag, and the result says so
(``tagFallback``), because a tag fallback carries no declared sha256, no
compatibility statement and no live flag: it is a check result, never an
automatic update.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .errors import UpdateError
from .manifest import (ReleaseManifest, parse_manifest,
                       require_remote_artifacts)
from .semver import Version

DEFAULT_API = "https://api.github.com"
MANIFEST_ASSET = "release.json"
USER_AGENT = "revl-update/1"


@dataclass(frozen=True)
class ResolvedRelease:
    """One release a feed resolved, manifest or tag fallback."""

    repo: str
    project: str
    version: str
    tag: str
    commit: str
    manifest: ReleaseManifest | None
    manifest_source: str  # "release" or "tag"
    release_url: str | None = None
    published_at: str | None = None

    @property
    def tag_fallback(self) -> bool:
        return self.manifest is None

    def as_report(self) -> dict:
        report = {
            "repo": self.repo,
            "project": self.project,
            "version": self.version,
            "tag": self.tag,
            "commit": self.commit,
            "tagFallback": self.tag_fallback,
            "manifestSource": self.manifest_source,
        }
        if self.release_url:
            report["url"] = self.release_url
        if self.published_at:
            report["publishedAt"] = self.published_at
        if self.manifest is not None:
            report["live"] = self.manifest.live
            report["hostChanges"] = self.manifest.host_changes
            report["channel"] = self.manifest.channel
            report["artifacts"] = [artifact.as_dict()
                                   for artifact in self.manifest.artifacts]
        return report


def api_base(value: str | None = None) -> str:
    """The REST base, overridable for a mirror or a test fixture server."""
    base = value or os.environ.get("REVL_UPDATE_API_BASE") or DEFAULT_API
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise UpdateError(
            "FEED_API_BASE",
            f"the GitHub API base {base!r} is not an http(s) URL")
    return base.rstrip("/")


#: Hosts a release token may travel to. GitHub's own hosts plus the configured
#: API base (a mirror, or a test fixture server). A manifest may name any
#: http(s) URL for an artifact; the token is not attached to a host outside
#: this set, and a redirect to another host is followed WITHOUT it.
_TOKEN_HOSTS = frozenset({"api.github.com", "github.com",
                          "objects.githubusercontent.com",
                          "codeload.github.com"})


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


#: The host of the feed being resolved. A mirror (or a test fixture server) is
#: an allowed token host for its own requests, which is what lets a private
#: mirror work without allowlisting every artifact host it names.
_ACTIVE_API_HOST: str | None = None


def _token_hosts() -> frozenset[str]:
    hosts = set(_TOKEN_HOSTS)
    if _ACTIVE_API_HOST:
        hosts.add(_ACTIVE_API_HOST)
    try:
        hosts.add(_host(api_base()))
    except UpdateError:
        pass
    return frozenset(hosts)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def token_host_allowed(url: str) -> bool:
    """Whether the configured token may be sent to ``url``.

    The host must be allowlisted AND the transport secure: https, or http to a
    loopback host (a local mirror or a test fixture server). A manifest that
    names ``http://github.com/...`` must not receive a bearer token in
    cleartext, however familiar the hostname looks.
    """
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https":
        return host in _token_hosts()
    # cleartext is only acceptable to the loopback interface: a local mirror
    # or a test fixture server, never a remote host however familiar its name.
    return parsed.scheme == "http" and host in _LOOPBACK_HOSTS


class _NoCrossHostAuth(urllib.request.HTTPRedirectHandler):
    """A redirect to another host never carries the Authorization header.

    urllib forwards request headers on redirect unless the handler removes
    them; a manifest that names a URL which redirects off the release host
    would otherwise hand a private-feed token to whoever runs that host.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _host(new.full_url) != _host(req.full_url):
            try:
                new.remove_header("Authorization")
            except KeyError:
                pass
        return new


_OPENER = urllib.request.build_opener(_NoCrossHostAuth)


def open_response(url: str, token: str | None, *, accept: str | None = None,
                  timeout: float = 20.0):
    """Open ``url`` with the token only where it may travel, and no redirect
    forwarding it. Callers own the returned response."""
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    if token and token_host_allowed(url):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    return _OPENER.open(request, timeout=timeout)


def _token(token: str | None, token_env: str | None = None) -> str | None:
    if token:
        return token
    if token_env:
        value = os.environ.get(token_env)
        if value:
            return value
    return None


def _request(url: str, token: str | None, *, accept: str,
             timeout: float) -> bytes:
    try:
        with open_response(url, token, accept=accept, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        remaining = error.headers.get("X-RateLimit-Remaining") if error.headers else None
        details = {"status": error.code}
        if remaining is not None:
            details["rateLimitRemaining"] = remaining
        if error.code in (401, 403):
            raise UpdateError(
                "FEED_AUTH",
                f"the release feed refused the request (HTTP {error.code}). "
                "A private repository needs a token, and an exhausted API "
                "limit needs the next hour",
                fix="set the configured token environment variable, or point "
                    "the feed at a public mirror",
                details=details) from None
        if error.code == 404:
            raise UpdateError(
                "FEED_NOT_FOUND",
                f"the release feed {url} does not exist (HTTP 404); a private "
                "repository answers the same way without a token",
                fix="check the feed repository name and the token",
                details=details) from None
        raise UpdateError(
            "FEED_HTTP",
            f"the release feed answered HTTP {error.code} for {url}",
            details=details) from None
    except (urllib.error.URLError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        raise UpdateError(
            "FEED_UNREACHABLE",
            f"the release feed at {url} could not be reached: {reason}",
            fix="check the network or point the feed at a reachable mirror"
        ) from None


def _get_json(url: str, token: str | None, *, timeout: float) -> object:
    body = _request(url, token, accept="application/vnd.github+json",
                    timeout=timeout)
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpdateError(
            "FEED_RESPONSE",
            f"the release feed at {url} did not answer JSON: {error}") from None


def _get_bytes(url: str, token: str | None, *, timeout: float,
               accept: str = "application/octet-stream") -> bytes:
    return _request(url, token, accept=accept, timeout=timeout)


def _tag_version(name: object) -> Version | None:
    if not isinstance(name, str):
        return None
    return Version.parse_or_none(name)


def _select(candidates: list[tuple[Version, object]], channel: str):
    stable = [(version, item) for version, item in candidates if version.stable]
    pool = candidates if channel != "stable" else stable
    if not pool:
        return None
    return max(pool, key=lambda pair: pair[0])


def _expected_project(repo: str, project: str | None) -> str:
    return project or repo.rsplit("/", 1)[-1]


def _manifest_from_release(release: dict, *, repo: str, project: str,
                           token: str | None, timeout: float,
                           base: str) -> ResolvedRelease:
    tag = release.get("tag_name")
    version = _tag_version(tag)
    if version is None:
        raise UpdateError(
            "FEED_NO_RELEASE",
            f"the newest release of {repo} is tagged {tag!r}, which is not a "
            "semantic version, so this updater cannot order it")
    assets = release.get("assets") or []
    manifest_asset = next(
        (asset for asset in assets
         if isinstance(asset, dict) and asset.get("name") == MANIFEST_ASSET),
        None)
    if manifest_asset is None:
        return _tag_fallback(repo, tag, str(version), project=project,
                             token=token, timeout=timeout, base=base,
                             release_url=release.get("html_url"),
                             published_at=release.get("published_at"))
    url = manifest_asset.get("browser_download_url") if not token else None
    url = url or manifest_asset.get("url")
    if not url:
        raise UpdateError(
            "FEED_RESPONSE",
            f"release {tag} of {repo} names a {MANIFEST_ASSET} asset with no "
            "download URL")
    document = _get_bytes(url, token, timeout=timeout,
                          accept="application/octet-stream")
    manifest = parse_manifest(document)
    require_remote_artifacts(manifest)
    expected = _expected_project(repo, project)
    if manifest.project != expected:
        raise UpdateError(
            "MANIFEST_PROJECT",
            f"the manifest on release {tag} names project "
            f"{manifest.project!r}, not {expected!r}: a feed may only ship its "
            "own project",
            fix="point the feed at the repository the release belongs to")
    if str(version) != manifest.version:
        raise UpdateError(
            "MANIFEST_VERSION",
            f"the release is tagged {tag} but its manifest declares version "
            f"{manifest.version}")
    return ResolvedRelease(
        repo=repo, project=manifest.project, version=manifest.version,
        tag=manifest.tag, commit=manifest.commit, manifest=manifest,
        manifest_source="release", release_url=release.get("html_url"),
        published_at=release.get("published_at"))


def _tag_fallback(repo: str, tag: str, version: str, *, project: str,
                  token: str | None, timeout: float, base: str,
                  release_url: str | None = None,
                  published_at: str | None = None) -> ResolvedRelease:
    tags = _get_json(f"{base}/repos/{repo}/tags?per_page=100",
                     token, timeout=timeout)
    if not isinstance(tags, list):
        raise UpdateError("FEED_RESPONSE",
                          f"the tag list of {repo} is not a list")
    match = next((entry for entry in tags
                  if isinstance(entry, dict) and entry.get("name") == tag), None)
    if match is None:
        raise UpdateError(
            "FEED_TAG_MISSING",
            f"release {tag} of {repo} has no {MANIFEST_ASSET}, and its tag is "
            "not in the tag list either, so its commit cannot be pinned",
            fix="publish a release.json asset on the release")
    commit = ((match.get("commit") or {}).get("sha") or "").lower()
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise UpdateError("FEED_TAG_MISSING",
                          f"tag {tag} of {repo} names no commit sha")
    return ResolvedRelease(
        repo=repo, project=_expected_project(repo, project), version=version,
        tag=tag, commit=commit, manifest=None, manifest_source="tag",
        release_url=release_url, published_at=published_at)


def resolve_release(repo: str, *, channel: str = "stable",
                    project: str | None = None, token: str | None = None,
                    token_env: str | None = None, api_base_override: str | None = None,
                    manifest_source: str | None = None,
                    timeout: float = 20.0) -> ResolvedRelease:
    """Resolve the newest release of ``repo`` on ``channel``.

    ``manifest_source`` (a URL or a path) bypasses the feed entirely and reads
    that manifest; it exists for an operator mirror and for tests, and it still
    passes the same validation.
    """
    global _ACTIVE_API_HOST
    base = api_base(api_base_override)
    _ACTIVE_API_HOST = _host(base)
    token = _token(token, token_env)
    if manifest_source is not None:
        return resolve_manifest_source(manifest_source, repo=repo,
                                       project=project, token=token,
                                       timeout=timeout)
    url = f"{base}/repos/{repo}/releases?per_page=100"
    releases = _get_json(url, token, timeout=timeout)
    if not isinstance(releases, list):
        raise UpdateError("FEED_RESPONSE",
                          f"the release list of {repo} is not a list")
    candidates: list[tuple[Version, object]] = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        if channel == "stable" and release.get("prerelease"):
            continue
        version = _tag_version(release.get("tag_name"))
        if version is not None:
            candidates.append((version, release))
    selected = _select(candidates, channel)
    if selected is not None:
        return _manifest_from_release(selected[1], repo=repo,
                                      project=_expected_project(repo, project),
                                      token=token, timeout=timeout,
                                      base=base)
    return _tag_fallback_from_tags(repo, channel=channel, project=project,
                                   token=token, timeout=timeout, base=base)


def _tag_fallback_from_tags(repo: str, *, channel: str, project: str,
                            token: str | None, timeout: float,
                            base: str) -> ResolvedRelease:
    tags = _get_json(f"{base}/repos/{repo}/tags?per_page=100", token,
                     timeout=timeout)
    if not isinstance(tags, list):
        raise UpdateError("FEED_RESPONSE", f"the tag list of {repo} is not a list")
    candidates: list[tuple[Version, dict]] = []
    for entry in tags:
        if not isinstance(entry, dict):
            continue
        version = _tag_version(entry.get("name"))
        if version is not None:
            candidates.append((version, entry))
    selected = _select(candidates, channel)
    if selected is None:
        raise UpdateError(
            "FEED_NO_RELEASE",
            f"{repo} has no release and no semantic tag, so there is nothing "
            "to check against (or it does not publish a release manifest)",
            fix="publish a release manifest, or check a repository that does")
    version, entry = selected
    commit = ((entry.get("commit") or {}).get("sha") or "").lower()
    if len(commit) != 40 or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise UpdateError("FEED_TAG_MISSING",
                          f"tag {entry.get('name')} of {repo} names no commit")
    return ResolvedRelease(
        repo=repo, project=_expected_project(repo, project),
        version=str(version), tag=entry.get("name"), commit=commit,
        manifest=None, manifest_source="tag")


def resolve_manifest_source(source: str, *, repo: str | None = None,
                            project: str | None = None, token: str | None = None,
                            timeout: float = 20.0) -> ResolvedRelease:
    """Read a manifest from a URL or a path and wrap it as a resolved release."""
    remote = source.startswith(("http://", "https://"))
    if remote:
        document = _get_bytes(source, token, timeout=timeout)
    else:
        path = (Path(urllib.parse.urlparse(source).path)
                if source.startswith("file://") else Path(source))
        try:
            document = path.read_bytes()
        except OSError as error:
            raise UpdateError(
                "MANIFEST_UNREADABLE",
                f"the manifest {source} cannot be read: "
                f"{error.strerror or error}",
                fix="pass --manifest a path or URL that names a release "
                    "manifest") from None
    manifest = parse_manifest(document)
    if remote:
        require_remote_artifacts(manifest)
    expected = project or manifest.project
    if manifest.project != expected:
        raise UpdateError(
            "MANIFEST_PROJECT",
            f"the manifest names project {manifest.project!r}, not "
            f"{expected!r}",
            fix="pass the project the manifest declares, or point --manifest "
                "at the release's own manifest")
    return ResolvedRelease(
        repo=repo or manifest.project, project=manifest.project,
        version=manifest.version, tag=manifest.tag, commit=manifest.commit,
        manifest=manifest, manifest_source="release",
        release_url=manifest.url, published_at=manifest.published_at)


def source_archive_url(repo: str, commit: str,
                       api_base_override: str | None = None) -> str:
    """The GitHub source archive of a commit, the tag fallback artifact."""
    return f"{api_base(api_base_override)}/repos/{repo}/tarball/{commit}"
