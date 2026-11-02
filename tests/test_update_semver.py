"""The update mechanism's semantic versions (docs/updates.md)."""

from __future__ import annotations

import pytest

from _load_by_path import load_by_path

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
semver = load_by_path("update_semver", ROOT / "src" / "revl" / "update" / "semver.py")


def test_parse_accepts_a_v_prefix_and_build_metadata():
    version = semver.Version.parse("v2.1.0+build.7")
    assert str(version) == "2.1.0+build.7"
    assert version.stable


def test_a_prerelease_sorts_below_its_release():
    alpha = semver.Version.parse("1.0.0-alpha")
    beta = semver.Version.parse("1.0.0-alpha.1")
    release = semver.Version.parse("1.0.0")
    assert alpha < beta < release
    assert not alpha.stable


def test_numeric_prerelease_identifiers_compare_numerically():
    assert semver.Version.parse("1.0.0-rc.9") < semver.Version.parse("1.0.0-rc.10")


def test_patch_minor_major_precedence():
    assert semver.Version.parse("1.0.1") > semver.Version.parse("1.0.0")
    assert semver.Version.parse("1.1.0") > semver.Version.parse("1.0.9")
    assert semver.Version.parse("2.0.0") > semver.Version.parse("1.99.99")


def test_bump_kind_names_the_smallest_breaking_component():
    assert semver.bump_kind(semver.Version.parse("1.2.3"),
                            semver.Version.parse("2.0.0")) == "major"
    assert semver.bump_kind(semver.Version.parse("1.2.3"),
                            semver.Version.parse("1.3.0")) == "minor"
    assert semver.bump_kind(semver.Version.parse("1.2.3"),
                            semver.Version.parse("1.2.4")) == "patch"
    assert semver.bump_kind(semver.Version.parse("1.2.3"),
                            semver.Version.parse("1.2.3")) == "none"
    assert semver.bump_kind(semver.Version.parse("2.0.0"),
                            semver.Version.parse("1.9.9")) == "none"


def test_an_invalid_version_is_refused():
    with pytest.raises(semver.VersionError):
        semver.Version.parse("2.0")
    with pytest.raises(semver.VersionError):
        semver.Version.parse("v2.0.0-")
    assert semver.Version.parse_or_none("not-a-version") is None
