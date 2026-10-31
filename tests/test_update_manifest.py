"""The release manifest, edition 1 (docs/updates.md)."""

from __future__ import annotations

import json

import pytest

from revl.update import manifest as manifest_module  # noqa: E402
from revl.update.errors import UpdateError  # noqa: E402


def _document(**overrides):
    document = {
        "schema": "revl.release/1",
        "project": "revl",
        "version": "2.1.0",
        "tag": "v2.1.0",
        "commit": "a" * 40,
        "channel": "stable",
        "artifacts": [{
            "name": "revl-2.1.0.tar.gz",
            "kind": "sdist",
            "url": "https://example.invalid/revl-2.1.0.tar.gz",
            "sha256": "b" * 64,
            "size": 10,
        }],
        "compat": {"language": "2.1.0", "gateApi": "1.0.0", "irVersion": 1,
                   "minPython": "3.11"},
        "live": False,
        "hostChanges": True,
        "pins": {"revl": "a" * 40, "cordis": None},
    }
    document.update(overrides)
    return document


def test_a_complete_manifest_parses():
    manifest = manifest_module.parse_manifest(_document())
    assert manifest.project == "revl"
    assert manifest.semver == manifest_module.Version.parse("2.1.0")
    assert manifest.artifacts[0].sha256 == "b" * 64
    assert manifest.compat["gateApi"] == "1.0.0"
    assert manifest.digest() == manifest.digest()


def test_the_digest_changes_with_the_artifact_hash():
    first = manifest_module.parse_manifest(_document())
    changed = _document()
    changed["artifacts"][0]["sha256"] = "c" * 64
    assert manifest_module.parse_manifest(changed).digest() != first.digest()


@pytest.mark.parametrize("mutate, code", [
    (lambda d: d.pop("schema"), "MANIFEST_SCHEMA"),
    (lambda d: d.update({"schema": "revl.release/2"}), "MANIFEST_SCHEMA"),
    (lambda d: d.update({"project": ""}), "MANIFEST_PROJECT"),
    (lambda d: d.update({"version": "two"}), "MANIFEST_VERSION"),
    (lambda d: d.update({"tag": "release-2.1.0"}), "MANIFEST_TAG"),
    (lambda d: d.update({"commit": "ABC"}), "MANIFEST_COMMIT"),
    (lambda d: d.update({"artifacts": []}), "MANIFEST_ARTIFACTS"),
    (lambda d: d["artifacts"][0].update({"sha256": "xyz"}), "MANIFEST_ARTIFACT"),
    (lambda d: d["artifacts"][0].update({"kind": "tarball"}), "MANIFEST_ARTIFACT"),
    (lambda d: d["artifacts"][0].update({"url": "relative/path"}), "MANIFEST_ARTIFACT"),
    (lambda d: d.update({"compat": {}}), "MANIFEST_COMPAT"),
    (lambda d: d.update({"live": "yes"}), "MANIFEST_FIELD"),
    (lambda d: d.update({"pins": {"revl": "short"}}), "MANIFEST_PINS"),
])
def test_a_bad_manifest_is_refused_with_its_code(mutate, code):
    document = _document()
    mutate(document)
    with pytest.raises(UpdateError) as raised:
        manifest_module.parse_manifest(document)
    assert raised.value.code == code


def test_unknown_top_level_fields_are_preserved_not_refused():
    manifest = manifest_module.parse_manifest(_document(releaseNotesUrl="x"))
    assert manifest.extra["releaseNotesUrl"] == "x"
    assert manifest.document()["releaseNotesUrl"] == "x"


def test_the_unpackable_artifact_prefers_source_over_sdist():
    document = _document()
    document["artifacts"] = [{
        "name": "app-2.1.0.tar.gz", "kind": "sdist",
        "url": "https://example.invalid/app.tar.gz", "sha256": "b" * 64},
        {"name": "app-source.tar.gz", "kind": "source",
         "url": "https://example.invalid/src.tar.gz", "sha256": "c" * 64}]
    manifest = manifest_module.parse_manifest(document)
    assert manifest.unpackable_artifact().name == "app-source.tar.gz"


def test_a_manifest_with_only_an_installer_has_no_unpackable_artifact():
    document = _document()
    document["artifacts"] = [{
        "name": "setup.exe", "kind": "installer",
        "url": "https://example.invalid/setup.exe", "sha256": "b" * 64}]
    manifest = manifest_module.parse_manifest(document)
    assert manifest.unpackable_artifact() is None


def test_json_text_is_accepted_and_non_json_is_refused():
    manifest = manifest_module.parse_manifest(json.dumps(_document()))
    assert manifest.version == "2.1.0"
    with pytest.raises(UpdateError) as raised:
        manifest_module.parse_manifest("{not json")
    assert raised.value.code == "MANIFEST_JSON"
