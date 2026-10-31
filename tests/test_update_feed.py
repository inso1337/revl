"""The release feed: manifest releases and tag fallback (docs/updates.md)."""

from __future__ import annotations

import json

import pytest

from _update_feed_fixture import COMMIT, manifest_for, serve_feed
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
