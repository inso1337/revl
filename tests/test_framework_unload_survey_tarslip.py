"""The unload survey's archive guards, after CodeQL `py/tarslip` (alert 87).

`bench/framework_unload_survey.py` downloads npm tarballs and PyPI sdists from
public indexes and extracts them, so it checks every member before trusting it.
The check was `str((root / member.name).resolve()).startswith(str(root))`, which
is a PREFIX test on a resolved path and therefore admits a member that lands in
a **sibling** of the destination: `/tmp/x/foo` is a prefix of `/tmp/x/foobar`,
so a member named `../foobar/pwn.py` under a `/tmp/x/foo` root passed the check
and `tar.extractall(dest)` then wrote it outside the root. The same predicate
guarded the zip side.

Two independent repairs, because either alone leaves a hole:

* the test is now `Path.is_relative_to`, which compares path components and not
  string prefixes;
* `tar.extractall` passes `filter="data"`, so tarfile re-checks the member
  itself and does not honour archive-set setuid bits. That is the backstop for
  a member the loop misjudges, and it is why the setuid test below is a real
  assertion and not a restatement of the loop.

The RED-before claim is per-test and version-dependent, because PEP 706 changed
tarfile's default filter to `data` in **3.14** while CI runs **3.12**:

* reverting only `bench/framework_unload_survey.py` fails the two sibling-escape
  tests on any interpreter — the prefix test is the guard in both;
* it additionally fails the setuid test on 3.12 (CI) but not on 3.14, where
  `extractall` already defaults to `data` and the explicit filter is redundant
  for that property. Measured: 3 failed / 3 passed on 3.12, 2 failed / 4 passed
  on 3.14, and 6 passed after the repair on both.

The explicit `filter="data"` is kept rather than relying on the 3.14 default
because the repository declares `requires-python = ">=3.11"`, where the default
is `fully_trusted` with only a DeprecationWarning.
"""

from __future__ import annotations

import io
import stat
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from _load_by_path import load_by_path  # noqa: E402

SURVEY = load_by_path("framework_unload_survey",
                      ROOT / "bench" / "framework_unload_survey.py")


def _tar_with(members: list[tuple[str, bytes, int]]) -> tarfile.TarFile:
    """An open tar archive holding `members` as (name, payload, mode)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, payload, mode in members:
            info = tarfile.TarInfo(name)
            info.mode = mode
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    buf.seek(0)
    return tarfile.open(fileobj=buf)


def _zip_with(names: list[str]) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, b"pwned\n")
    buf.seek(0)
    return zipfile.ZipFile(buf)


def test_tar_member_cannot_escape_into_a_sibling_directory(tmp_path):
    """The prefix test admitted this: `dest` is `.../foo` and the member
    resolves to `.../foobar/pwn.py`, which shares the string prefix."""
    dest = tmp_path / "foo"
    dest.mkdir()

    with _tar_with([("../foobar/pwn.py", b"pwned\n", 0o644)]) as tar:
        with pytest.raises(SURVEY.SurveyError):
            SURVEY._safe_extract_tar(tar, dest)

    assert not (tmp_path / "foobar" / "pwn.py").exists()


def test_zip_member_cannot_escape_into_a_sibling_directory(tmp_path):
    dest = tmp_path / "foo"
    dest.mkdir()

    with _zip_with(["../foobar/pwn.py"]) as zf:
        with pytest.raises(SURVEY.SurveyError):
            SURVEY._safe_extract_zip(zf, dest)

    assert not (tmp_path / "foobar" / "pwn.py").exists()


def test_tar_member_cannot_escape_by_absolute_path(tmp_path):
    """Caught before the repair too, so this is the non-vacuity guard on the
    sibling tests: the loop still rejects the plain traversal it always did."""
    dest = tmp_path / "foo"
    dest.mkdir()
    outside = tmp_path / "outside"

    with _tar_with([(str(outside / "pwn.py"), b"pwned\n", 0o644)]) as tar:
        with pytest.raises(SURVEY.SurveyError):
            SURVEY._safe_extract_tar(tar, dest)

    assert not (outside / "pwn.py").exists()


def test_archive_set_setuid_does_not_survive_extraction(tmp_path):
    """`filter="data"` is what makes the loop defence-in-depth rather than the
    only guard: with no filter, `extractall` chmods the member's own mode.

    Asserting this on an interpreter whose `extractall` default is already
    `data` (3.14) proves less than it does on 3.12, so the assertion is kept
    for CI's version rather than for the one this was written on.
    """
    dest = tmp_path / "foo"
    dest.mkdir()

    with _tar_with([("bin/tool", b"#!/bin/sh\n", 0o4755)]) as tar:
        SURVEY._safe_extract_tar(tar, dest)

    mode = stat.S_IMODE((dest / "bin" / "tool").stat().st_mode)
    assert not mode & stat.S_ISUID, oct(mode)


def test_a_legitimate_archive_still_extracts(tmp_path):
    """The repair is not a refusal: a normal nested npm-style member lands
    under `dest`, and the sibling directory is never created."""
    dest = tmp_path / "foo"
    dest.mkdir()

    with _tar_with([("package/dist/index.js", b"export {}\n", 0o644),
                    ("./package/package.json", b"{}\n", 0o644)]) as tar:
        SURVEY._safe_extract_tar(tar, dest)

    assert (dest / "package" / "dist" / "index.js").read_bytes() == b"export {}\n"
    assert (dest / "package" / "package.json").read_bytes() == b"{}\n"
    assert not (tmp_path / "foobar").exists()


def test_a_legitimate_zip_still_extracts(tmp_path):
    dest = tmp_path / "foo"
    dest.mkdir()

    with _zip_with(["pkg/mod.py", "pkg/data/inner.txt"]) as zf:
        SURVEY._safe_extract_zip(zf, dest)

    assert (dest / "pkg" / "mod.py").read_bytes() == b"pwned\n"
    assert (dest / "pkg" / "data" / "inner.txt").is_file()
