"""The release feed: manifest releases and tag fallback (docs/updates.md)."""

from __future__ import annotations

import http.server
import json
import threading

import pytest

from _update_feed_fixture import COMMIT, manifest_for, serve_feed
from revl.update import feed as feed_module
from revl.update.errors import UpdateError
from revl.update.feed import resolve_manifest_source, resolve_release


def test_a_manifest_release_resolves_with_its_fields():
    feed = serve_feed(version="1.2.0", live=True, host_changes=False)
    try:
        release = resolve_release("owner/app", api_base_override=feed.base_url)
    finally:
        feed.close()
    assert release.version == "1.2.0"
    assert release.tag == "v1.2.0"
    assert release.commit == COMMIT
    assert release.manifest is not None
    assert release.manifest.live is True
    assert release.manifest_source == "release"
    assert release.tag_fallback is False
    assert release.manifest.artifacts[0].sha256


def test_a_project_mismatch_is_refused():
    feed = serve_feed()
    try:
        with pytest.raises(UpdateError) as raised:
            resolve_release("owner/app", project="other",
                            api_base_override=feed.base_url)
    finally:
        feed.close()
    assert raised.value.code == "MANIFEST_PROJECT"


def test_a_token_is_sent_and_a_missing_one_is_refused():
    feed = serve_feed(token="s3cret")
    try:
        with pytest.raises(UpdateError) as raised:
            resolve_release("owner/app", api_base_override=feed.base_url)
        assert raised.value.code == "FEED_AUTH"
        release = resolve_release("owner/app", token="s3cret",
                                  api_base_override=feed.base_url)
        assert release.version == "1.2.0"
    finally:
        feed.close()
    assert any(request["authorization"] == "Bearer s3cret"
               for request in feed.state["requests"])


def test_the_stable_channel_ignores_a_prerelease():
    feed = serve_feed(prerelease_version="1.3.0-rc.1")
    try:
        stable = resolve_release("owner/app", api_base_override=feed.base_url)
        edge = resolve_release("owner/app", channel="prerelease",
                               api_base_override=feed.base_url)
    finally:
        feed.close()
    assert stable.version == "1.2.0"
    assert edge.version == "1.3.0-rc.1"
    assert edge.tag_fallback is True


def test_a_repository_without_releases_falls_back_to_its_tags():
    feed = serve_feed(tag_only=True, version="1.2.0")
    try:
        release = resolve_release("owner/app", api_base_override=feed.base_url)
    finally:
        feed.close()
    assert release.tag_fallback is True
    assert release.version == "1.2.0"
    assert release.commit == COMMIT
    assert release.manifest is None


def test_an_empty_feed_is_a_named_refusal():
    feed = serve_feed(tag_only=True, version="not-a-version")
    try:
        with pytest.raises(UpdateError) as raised:
            resolve_release("owner/app", api_base_override=feed.base_url)
    finally:
        feed.close()
    assert raised.value.code == "FEED_NO_RELEASE"


def test_a_manifest_can_be_read_directly_and_is_still_validated(tmp_path):
    document = manifest_for(
        project="app", artifact_url="https://example.invalid/app.tar.gz",
        artifact_sha="b" * 64)
    path = tmp_path / "release.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    release = resolve_manifest_source(str(path), project="app")
    assert release.version == "1.2.0"
    assert release.manifest is not None
    document["schema"] = "revl.release/9"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(UpdateError) as raised:
        resolve_manifest_source(str(path), project="app")
    assert raised.value.code == "MANIFEST_SCHEMA"


def test_an_invalid_api_base_is_refused():
    with pytest.raises(UpdateError) as raised:
        resolve_release("owner/app", api_base_override="ftp://example.invalid")
    assert raised.value.code == "FEED_API_BASE"


def test_the_token_never_travels_to_an_artifact_host_outside_the_allowlist(
        tmp_path):
    """A manifest may name any http(s) artifact URL. The token only goes to
    the feed's own host, so a hostile manifest cannot exfiltrate it."""
    feed = serve_feed(token="s3cret")
    try:
        release = resolve_release("owner/app", token="s3cret",
                                  api_base_override=feed.base_url)
    finally:
        feed.close()
    # the fixture serves the artifact on the same server; a genuinely foreign
    # host is not allowlisted
    import urllib.parse
    port = urllib.parse.urlsplit(feed.base_url).port
    assert feed_module.token_host_allowed(f"http://127.0.0.1:{port}/asset") is True
    # loopback cleartext is allowed; a remote host over http never is
    assert feed_module.token_host_allowed(f"http://localhost:{port}/asset") is True
    assert feed_module.token_host_allowed("https://evil.example/asset") is False
    assert feed_module.token_host_allowed("http://github.com/asset") is False
    assert release.version == "1.2.0"


def _http_server(handler_cls):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_a_cross_host_redirect_drops_the_authorization_header(monkeypatch):
    """urllib forwards request headers on redirect; a redirect from the feed
    host to another host must not carry the bearer token."""
    seen: list = []
    port_b: list[int] = []

    class B(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_GET(self):  # noqa: N802
            seen.append(self.headers.get("Authorization"))
            self.send_response(204)
            self.end_headers()

    class A(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location",
                             f"http://localhost:{port_b[0]}/end")
            self.end_headers()

    server_b = _http_server(B)
    port_b.append(server_b.server_address[1])
    server_a = _http_server(A)
    try:
        monkeypatch.setattr(feed_module, "_ACTIVE_API_HOST", "127.0.0.1")
        with feed_module.open_response(
                f"http://127.0.0.1:{server_a.server_address[1]}/start",
                "s3cret") as response:
            response.read()
    finally:
        server_a.shutdown()
        server_a.server_close()
        server_b.shutdown()
        server_b.server_close()
    assert seen == [None]


def test_a_fetched_manifest_may_not_name_a_local_artifact(tmp_path):
    local = {"name": "app-1.2.0.tar.gz", "kind": "source",
             "url": "file:///etc/hosts", "sha256": "0" * 64, "size": 1}
    feed = serve_feed(manifest_extra={"artifacts": [local]})
    try:
        with pytest.raises(UpdateError) as raised:
            resolve_release("owner/app", api_base_override=feed.base_url)
    finally:
        feed.close()
    assert raised.value.code == "MANIFEST_URL"
    # the operator's own manifest on disk may still name a local artifact
    document = manifest_for(artifact_url="file:///etc/hosts",
                            artifact_sha="0" * 64)
    path = tmp_path / "release.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert resolve_manifest_source(str(path), project="app").manifest
