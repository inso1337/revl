"""A GitHub-shaped release feed over a throwaway local HTTP server.

The update tests must not touch the network, and they must exercise the real
resolver path (releases list, manifest asset download, tag fallback), not a
stubbed function. This fixture serves the handful of REST routes the resolver
reads, in the shape GitHub answers them, and records every request so a test
can assert a token was sent or that no second request was made.
"""

from __future__ import annotations

import hashlib
import http.server
import io
import json
import tarfile
import threading
import urllib.parse
from dataclasses import dataclass, field

COMMIT = "a" * 40


def tar_gz(files: dict[str, bytes], top: str = "release-1.2.0") -> bytes:
    """An in-memory tar.gz with a single top level directory."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, body in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def source_tar(version: str = "1.2.0", component: str = "app") -> bytes:
    source = """service Counter { fn value() -> Int }

component CounterImpl provides counter: Counter {
  let s = effect Map.new() undo s.drop()
  provide counter { fn value() = 42 }
}
"""
    return tar_gz({f"{component}.rvl": source.encode(),
                   "README.md": b"fixture release\n"},
                  top=f"{component}-{version}")


def manifest_for(*, repo: str = "owner/app", project: str = "app",
                 version: str = "1.2.0", commit: str = COMMIT,
                 artifact_url: str, artifact_sha: str,
                 artifact_name: str = "app-1.2.0.tar.gz",
                 artifact_kind: str = "source", live: bool = True,
                 artifact_size: int = 1,
                 host_changes: bool = False, extra: dict | None = None) -> dict:
    document = {
        "schema": "revl.release/1",
        "project": project,
        "version": version,
        "tag": f"v{version}",
        "commit": commit,
        "channel": "stable",
        "artifacts": [{
            "name": artifact_name,
            "kind": artifact_kind,
            "url": artifact_url,
            "sha256": artifact_sha,
            "size": artifact_size,
        }],
        "compat": {"language": "2.0.0", "gateApi": "1.0.0", "irVersion": 1,
                   "minPython": "3.11"},
        "live": live,
        "hostChanges": host_changes,
        "pins": {"revl": commit},
    }
    if extra:
        document.update(extra)
    return document


@dataclass
class FixtureFeed:
    base_url: str
    server: http.server.ThreadingHTTPServer
    state: dict = field(default_factory=dict)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def serve_feed(*, repo: str = "owner/app", version: str = "1.2.0",
               with_manifest: bool = True, tag_only: bool = False,
               prerelease_version: str | None = None,
               project: str | None = None,
               commit: str = COMMIT,
               token: str | None = None,
               artifact_bytes: bytes | None = None,
               artifact_override: bytes | None = None,
               live: bool = True, host_changes: bool = False,
               manifest_extra: dict | None = None) -> FixtureFeed:
    """Start a feed serving one (optionally one prerelease) release.

    ``artifact_override`` serves bytes that differ from the manifest hash, the
    digest-mismatch case. ``tag_only`` publishes no releases and only tags.
    """
    files = source_tar(version)
    artifact = files if artifact_bytes is None else artifact_bytes
    state: dict = {"requests": [], "token": token,
                   "artifact": artifact_override or artifact,
                   "commit": commit, "version": version}
    project = project or repo.rsplit("/", 1)[-1]
    artifact_name = f"{project}-{version}.tar.gz"

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):  # silence the test output
            return

        def _json(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _bytes(self, body):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - http.server's interface
            path = urllib.parse.urlsplit(self.path).path
            state["requests"].append(
                {"path": path,
                 "authorization": self.headers.get("Authorization")})
            if state["token"] is not None:
                if self.headers.get("Authorization") != f"Bearer {state['token']}":
                    self._json({"message": "Bad credentials"}, status=401)
                    return
            base = f"http://127.0.0.1:{server.server_address[1]}"
            prefix = f"/repos/{repo}"
            if path == f"{prefix}/releases":
                if tag_only or not with_manifest:
                    self._json([])
                    return
                releases = [{
                    "tag_name": f"v{version}",
                    "prerelease": False,
                    "draft": False,
                    "html_url": f"https://example.invalid/{version}",
                    "published_at": "2026-10-31T00:00:00Z",
                    "assets": ([{
                        "name": "release.json",
                        "url": f"{base}/assets/release.json",
                        "browser_download_url": f"{base}/assets/release.json",
                    }] if with_manifest else []),
                }]
                if prerelease_version:
                    releases.append({
                        "tag_name": f"v{prerelease_version}",
                        "prerelease": True,
                        "draft": False,
                        "assets": [],
                    })
                self._json(releases)
                return
            if path == f"{prefix}/tags":
                tags = [{"name": f"v{version}", "commit": {"sha": commit}}]
                if prerelease_version:
                    tags.append({"name": f"v{prerelease_version}",
                                 "commit": {"sha": "b" * 40}})
                self._json(tags)
                return
            if path.startswith(f"{prefix}/tarball/"):
                self._bytes(state["artifact"])
                return
            if path == "/assets/release.json":
                digest = hashlib.sha256(state["artifact"]).hexdigest()
                document = manifest_for(
                    repo=repo, project=project, version=version, commit=commit,
                    artifact_url=f"{base}/assets/{artifact_name}",
                    artifact_sha=digest, artifact_name=artifact_name,
                    artifact_size=len(state["artifact"]),
                    live=live, host_changes=host_changes,
                    extra=manifest_extra)
                self._bytes(json.dumps(document).encode())
                return
            if path.startswith("/assets/"):
                self._bytes(state["artifact"])
                return
            self._json({"message": "Not Found"}, status=404)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return FixtureFeed(base_url=f"http://127.0.0.1:{server.server_address[1]}",
                       server=server, state=state)
