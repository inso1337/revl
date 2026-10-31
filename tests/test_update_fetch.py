"""Staging a release: verify, extract safely, record provenance (docs/updates.md)."""

from __future__ import annotations

import dataclasses
import io
import json
import tarfile
import zipfile

import pytest

from _update_feed_fixture import serve_feed, source_tar
from revl.update.errors import UpdateError
from revl.update.fetch import extract_archive, find_staged, stage_release
from revl.update.feed import resolve_release


def _malicious_tar(entries: list[tarfile.TarInfo]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for info in entries:
            if info.isdir():
                tar.addfile(info)
            else:
                tar.addfile(info, io.BytesIO(b"payload"))
    return buffer.getvalue()


def _entry(name: str, *, kind: str = "file") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    if kind == "symlink":
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
    elif kind == "dir":
        info.type = tarfile.DIRTYPE
    else:
        info.size = len(b"payload")
    return info


def test_a_manifest_release_is_downloaded_verified_and_staged(tmp_path):
    feed = serve_feed(version="1.2.0")
    try:
        release = resolve_release("owner/app", api_base_override=feed.base_url)
        staged = stage_release(release, home_dir=tmp_path, token=None)
    finally:
        feed.close()
    assert staged.version == "1.2.0"
    assert staged.directory.is_dir()
    assert staged.files == ("app.rvl",)
    assert staged.provenance["artifact"]["sha256"] == staged.digest
    assert staged.manifest_digest == release.manifest.digest()
    found = find_staged(tmp_path, project="app")
    assert [entry.version for entry in found] == ["1.2.0"]


def test_staging_the_same_release_twice_reuses_the_directory(tmp_path):
    feed = serve_feed()
    try:
        release = resolve_release("owner/app", api_base_override=feed.base_url)
        first = stage_release(release, home_dir=tmp_path)
        second = stage_release(release, home_dir=tmp_path)
    finally:
        feed.close()
    assert first.directory == second.directory
    assert len(list((tmp_path / "staged").iterdir())) == 1


def test_a_hash_mismatch_is_refused_and_nothing_is_staged(tmp_path):
    feed = serve_feed()
    try:
        release = resolve_release("owner/app", api_base_override=feed.base_url)
        artifact = dataclasses.replace(release.manifest.artifacts[0],
                                       sha256="0" * 64)
        corrupt = dataclasses.replace(
            release, manifest=dataclasses.replace(
                release.manifest, artifacts=(artifact,)))
        with pytest.raises(UpdateError) as raised:
            stage_release(corrupt, home_dir=tmp_path)
    finally:
        feed.close()
    assert raised.value.code == "FETCH_DIGEST"
    assert not (tmp_path / "staged").exists() or \
        not list((tmp_path / "staged").iterdir())


def test_a_tag_fallback_stages_the_commit_archive(tmp_path):
    feed = serve_feed(tag_only=True)
    try:
        release = resolve_release("owner/app", api_base_override=feed.base_url)
        staged = stage_release(release, home_dir=tmp_path,
                               api_base_override=feed.base_url)
    finally:
        feed.close()
    assert release.tag_fallback
    assert staged.tag_fallback is True
    assert staged.files == ("app.rvl",)
    assert staged.manifest_digest is None


@pytest.mark.parametrize("name", ["../escape.rvl", "/abs/escape.rvl",
                                  "a\\b.rvl", "C:/escape.rvl"])
def test_an_archive_path_that_could_escape_is_refused(tmp_path, name):
    archive = tmp_path / "evil.tar.gz"
    archive.write_bytes(_malicious_tar([_entry(name)]))
    with pytest.raises(UpdateError) as raised:
        extract_archive(archive, tmp_path / "out")
    assert raised.value.code in ("STAGE_ARCHIVE_PATH",)


def test_a_symlink_in_the_archive_is_refused(tmp_path):
    archive = tmp_path / "link.tar.gz"
    archive.write_bytes(_malicious_tar([_entry("safe.rvl"),
                                        _entry("link", kind="symlink")]))
    with pytest.raises(UpdateError) as raised:
        extract_archive(archive, tmp_path / "out")
    assert raised.value.code == "STAGE_ARCHIVE_ENTRY"


def test_a_zip_path_that_climbs_out_is_refused(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("../escape.rvl", b"x")
    with pytest.raises(UpdateError) as raised:
        extract_archive(archive, tmp_path / "out")
    assert raised.value.code == "STAGE_ARCHIVE_PATH"


def test_an_unknown_archive_format_is_refused(tmp_path):
    archive = tmp_path / "artifact.bin"
    archive.write_bytes(b"not an archive")
    with pytest.raises(UpdateError) as raised:
        extract_archive(archive, tmp_path / "out")
    assert raised.value.code == "STAGE_ARCHIVE_FORMAT"


def test_extract_archive_accepts_a_plain_tar(tmp_path):
    archive = tmp_path / "plain.tar.gz"
    archive.write_bytes(source_tar())
    count = extract_archive(archive, tmp_path / "out")
    assert count == 2
    assert (tmp_path / "out" / "app-1.2.0" / "app.rvl").is_file()


def test_extraction_bounds_the_decompressed_bytes(tmp_path):
    archive = tmp_path / "big.tar.gz"
    archive.write_bytes(source_tar())
    with pytest.raises(UpdateError) as raised:
        extract_archive(archive, tmp_path / "out", max_bytes=10)
    assert raised.value.code == "STAGE_TOO_LARGE"


def test_a_single_letter_entry_is_not_a_drive_path(tmp_path):
    archive = tmp_path / "single.tar.gz"
    archive.write_bytes(_malicious_tar([_entry("a")]))
    count = extract_archive(archive, tmp_path / "out")
    assert count == 1
    assert (tmp_path / "out" / "a").is_file()


def test_staged_releases_are_ordered_by_version_not_by_string(tmp_path):
    root = tmp_path / "staged"
    for version in ("10.0.0", "2.0.0"):
        directory = root / f"app-{version}-abcdef123456"
        (directory / "source").mkdir(parents=True)
        (directory / "source" / "app.rvl").write_text("service S {}", encoding="utf-8")
        (directory / "provenance.json").write_text(json.dumps({
            "schema": "revl.update.provenance/1", "project": "app",
            "version": version, "tag": f"v{version}", "commit": "a" * 40,
            "artifact": {"name": "x.tar.gz", "kind": "source",
                         "sha256": "b" * 64},
            "manifestDigest": None, "tagFallback": True,
        }), encoding="utf-8")
    found = find_staged(tmp_path, project="app")
    assert [release.version for release in found[:2]] == ["10.0.0", "2.0.0"]
