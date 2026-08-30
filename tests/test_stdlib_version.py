"""The stdlib version stamp — drift detection for vendored stdlib copies
(roadmap item 389).

A consumer (the revl-harness) holds byte-COPIES of ``stdlib/{json,value,str}.rvl``.
When item 104 added ``value_is_object`` upstream the compiler began recommending
a fix (``value_is_object``) that did not exist in the consumer's copied tree, and
nothing noticed. The stamp closes that gap: a single ``pub fn stdlib_version()``
travels with every copy of the stdlib, and the compiler knows the version it
ships. A consumer can read its own copy's stamp and compare it to the compiler's
``EXPECTED_STDLIB_VERSION`` to detect that its vendored stdlib has drifted.

This suite proves:
  * the in-repo ``stdlib/version.rvl`` stamp matches the compiler's expected
    version (they can never silently disagree inside the checkout);
  * ``read_stamp`` reads the literal out of a stdlib tree, and returns None for a
    tree that predates the stamp;
  * a DRIFTED vendored copy (an older stamp, or no stamp at all) is DETECTED by
    ``check_drift``;
  * ``revl doctor`` warns when the resolved stdlib's stamp differs from the
    compiler's expected version;
  * ``stdlib_version()`` runs cross-tier (py + ts) and returns the current stamp.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.stdlib_version import (  # noqa: E402
    EXPECTED_STDLIB_VERSION,
    check_drift,
    parse_stamp,
    read_stamp,
    resolve_loaded_stdlib_dir,
)
from revl._paths import stdlib_root  # noqa: E402

STDLIB = stdlib_root()


# ---------------------------------------------------------------- in-repo stamp

def test_expected_version_is_nonempty_and_not_unknown():
    assert EXPECTED_STDLIB_VERSION
    assert isinstance(EXPECTED_STDLIB_VERSION, str)
    # "unknown" is the broken-install fallback; the checkout must read a real
    # stamp out of its own stdlib/version.rvl.
    assert EXPECTED_STDLIB_VERSION != "unknown"


def test_repo_stamp_matches_expected():
    # The compiler and its own bundled stdlib can never silently disagree: the
    # expected version IS the stamp read from stdlib/version.rvl, so no drift is
    # reported against the checkout's own tree.
    assert read_stamp(STDLIB) == EXPECTED_STDLIB_VERSION
    assert check_drift(STDLIB) is None


def test_version_module_exists_and_is_pure_revl():
    source = (STDLIB / "version.rvl").read_text(encoding="utf-8")
    assert "pub fn stdlib_version() -> Str" in source
    # pure revl — no per-tier extern bodies — so it lowers on every tier for a
    # consumer. (The prose "@py" in the doc comment is fine; a real body would
    # be an `extern ... = @py {`.)
    assert "extern" not in source
    assert "= @py" not in source and "= @ts" not in source


# ------------------------------------------------------------------- parse/read

def test_parse_stamp_double_quote_and_backtick():
    assert parse_stamp('pub fn stdlib_version() -> Str { return "7" }') == "7"
    assert parse_stamp("pub fn stdlib_version() -> Str {\n  return `12`\n}") == "12"


def test_parse_stamp_none_when_absent():
    assert parse_stamp("pub fn something_else() -> Str { return \"1\" }") is None
    assert parse_stamp("") is None


def test_read_stamp_none_for_tree_without_version_module(tmp_path):
    # A vendored copy that predates the stamp: it has json/value/str but no
    # version.rvl at all.
    (tmp_path / "value.rvl").write_text("pub fn x() -> Str { return \"\" }",
                                        encoding="utf-8")
    assert read_stamp(tmp_path) is None


# ------------------------------------------------------------- drift detection

def _vendor(tmp_path, version: str | None) -> Path:
    """A stand-in vendored stdlib tree, optionally carrying a version stamp."""
    tree = tmp_path / "vendored" / "stdlib"
    tree.mkdir(parents=True)
    if version is not None:
        (tree / "version.rvl").write_text(
            f"pub fn stdlib_version() -> Str {{ return \"{version}\" }}\n",
            encoding="utf-8")
    return tree


def test_drift_detected_for_older_copy(tmp_path):
    # EXPECTED is an integer counter (e.g. "1"); simulate a copy one behind.
    older = str(int(EXPECTED_STDLIB_VERSION) - 1)
    tree = _vendor(tmp_path, older)
    warning = check_drift(tree, EXPECTED_STDLIB_VERSION)
    assert warning is not None
    assert older in warning and EXPECTED_STDLIB_VERSION in warning
    assert "older" in warning


def test_drift_detected_for_newer_copy(tmp_path):
    newer = str(int(EXPECTED_STDLIB_VERSION) + 1)
    tree = _vendor(tmp_path, newer)
    warning = check_drift(tree, EXPECTED_STDLIB_VERSION)
    assert warning is not None
    assert "newer" in warning


def test_drift_detected_for_unstamped_copy(tmp_path):
    # The item-104 case: the harness copied stdlib before the stamp existed.
    tree = _vendor(tmp_path, None)
    warning = check_drift(tree, EXPECTED_STDLIB_VERSION)
    assert warning is not None
    assert "no version stamp" in warning


def test_no_drift_for_matching_copy(tmp_path):
    tree = _vendor(tmp_path, EXPECTED_STDLIB_VERSION)
    assert check_drift(tree, EXPECTED_STDLIB_VERSION) is None


# ------------------------------------------- resolver picks up a vendored copy

def test_resolver_prefers_vendored_stdlib_on_import_path(tmp_path, monkeypatch):
    tree = _vendor(tmp_path, "999")  # tree is <root>/vendored/stdlib
    monkeypatch.setenv("REVL_IMPORT_PATH", str(tmp_path / "vendored"))
    assert resolve_loaded_stdlib_dir() == tree
    assert read_stamp(resolve_loaded_stdlib_dir()) == "999"


def test_resolver_falls_back_to_bundled(monkeypatch):
    monkeypatch.delenv("REVL_IMPORT_PATH", raising=False)
    assert resolve_loaded_stdlib_dir() == STDLIB


# ------------------------------------------------------------- revl doctor probe

def test_doctor_reports_ok_for_bundled_stdlib(monkeypatch):
    monkeypatch.delenv("REVL_IMPORT_PATH", raising=False)
    from revl.doctor import check_stdlib_version, OK
    check = check_stdlib_version(prober=None)
    assert check.status == OK
    assert check.version == EXPECTED_STDLIB_VERSION


def test_doctor_warns_on_drifted_vendored_stdlib(tmp_path, monkeypatch):
    from revl.doctor import check_stdlib_version, WARN
    _vendor(tmp_path, "0")  # older than any real stamp (>=1)
    monkeypatch.setenv("REVL_IMPORT_PATH", str(tmp_path / "vendored"))
    check = check_stdlib_version(prober=None)
    assert check.status == WARN
    assert "stdlib drift" in check.detail
    # the report still names the version the compiler expects
    assert check.version == EXPECTED_STDLIB_VERSION


def test_doctor_warns_on_unstamped_vendored_stdlib(tmp_path, monkeypatch):
    # The precise item-104 shape: a vendored stdlib holding real modules
    # (value.rvl) that was copied BEFORE the stamp existed, so it has no
    # version.rvl. This must NOT silently resolve to the bundled stamp and read
    # OK — the resolver picks the partial vendored root and the drift check
    # flags the missing stamp.
    from revl.doctor import check_stdlib_version, WARN
    tree = tmp_path / "vendored" / "stdlib"
    tree.mkdir(parents=True)
    (tree / "value.rvl").write_text(
        "pub fn value_kind() -> Str { return \"record\" }\n", encoding="utf-8")
    # deliberately NO version.rvl
    monkeypatch.setenv("REVL_IMPORT_PATH", str(tmp_path / "vendored"))
    assert resolve_loaded_stdlib_dir() == tree
    check = check_stdlib_version(prober=None)
    assert check.status == WARN
    assert "no version stamp" in check.detail


# --------------------------------------------------------- cross-tier execution

_STAMP_PROBE = (
    'use "stdlib/version.rvl" { stdlib_version }\n'
    'test "stamp is the current version" '
    '{ assert stdlib_version() == "%s" }\n'
) % EXPECTED_STDLIB_VERSION


@pytest.mark.parametrize("tier", ["py", "ts"])
def test_stdlib_version_runs_cross_tier(tier, tmp_path):
    # A `use` needs a real source path so the import resolver can find
    # stdlib/version.rvl via the search-path fallback (roadmap 319); a bare
    # string has no importer directory. Write the probe to disk and compile it.
    from revl import compile_files
    from revl.test import RUNNERS
    main = tmp_path / "stamp_probe.rvl"
    main.write_text(_STAMP_PROBE, encoding="utf-8")
    status, message = RUNNERS[tier](compile_files([str(main)]))
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} failed: {message}"
