"""Duplicate/collision diagnostic quality (roadmap 393 + 394, revl-harness
F-H39.2 / F-H39.3).

393: two modules each *privately* declare `extern pure fn si_now_ms()` with
DIFFERENT signatures. Composing them used to fall through the private-namespace
rename and surface as `si_now_ms is not a declared requirement of SelfImprove`
(the G1 missing-requirement path), sending the author to add a `requires`
clause for something that is not a service. The collision must instead be
reported AS a duplicate extern, naming both declaring files and the signature
mismatch.

394: the same stdlib file reached under two `use` spellings (a search-path
spelling and a vendored byte-identical copy) loads as two modules -> a
`duplicate function` whose message named neither resolved path, reading as a
bug in revl's own stdlib. The diagnostic must name BOTH resolved absolute
paths (and, when it is the two-copies-of-one-file case, say so).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files  # noqa: E402


# --------------------------------------------------------------------------
# 393 — duplicate PRIVATE extern reports a duplicate, not a missing requirement
# --------------------------------------------------------------------------
def _write_393(tmp_path: Path) -> list[str]:
    (tmp_path / "a.rvl").write_text(
        "extern pure fn si_now_ms() -> Str = @python { return \"0\" }\n"
        "service Clock {\n"
        "  fn now() -> Str\n"
        "}\n"
        "component SelfImprove provides clock: Clock {\n"
        "  provide clock {\n"
        "    fn now() = si_now_ms()\n"
        "  }\n"
        "}\n"
    )
    (tmp_path / "b.rvl").write_text(
        "extern pure fn si_now_ms() -> Int = @python { return 0 }\n"
        "pub fn stamp_b() -> Int { return si_now_ms() }\n"
    )
    (tmp_path / "root.rvl").write_text('use "./b.rvl" { stamp_b }\n')
    return [str(tmp_path / "a.rvl"), str(tmp_path / "root.rvl")]


def test_393_duplicate_private_extern_is_a_duplicate(tmp_path):
    paths = _write_393(tmp_path)
    with pytest.raises(RevlError) as exc:
        compile_files(paths)
    msg = str(exc.value)
    # NOT the misleading missing-requirement error
    assert "is not a declared requirement" not in msg, msg
    # a duplicate, naming the symbol
    assert "duplicate extern" in msg, msg
    assert "si_now_ms" in msg, msg
    # both declaring files named
    assert "a.rvl" in msg and "b.rvl" in msg, msg
    # the signature mismatch spelled out
    assert "-> Str" in msg and "-> Int" in msg, msg


# --------------------------------------------------------------------------
# 394 — duplicate function names BOTH resolved absolute paths
# --------------------------------------------------------------------------
_STR_LIB = (
    "pub fn is_space(c: Str) -> Bool { return c == \" \" }\n"
    "pub fn trim(s: Str) -> Str { return s }\n"
)


def _write_394(tmp_path: Path) -> tuple[str, Path, Path]:
    # a search-path root holding the "real" stdlib copy...
    search_root = tmp_path / "sdk"
    (search_root / "stdlib").mkdir(parents=True)
    real = search_root / "stdlib" / "str.rvl"
    real.write_text(_STR_LIB)
    # ...and a byte-identical vendored copy inside the consumer project.
    proj = tmp_path / "proj"
    (proj / "stdlib").mkdir(parents=True)
    vendored = proj / "stdlib" / "str.rvl"
    vendored.write_text(_STR_LIB)
    (proj / "src").mkdir()
    (proj / "src" / "root.rvl").write_text(
        'use "stdlib/str.rvl" { is_space }\n'      # via REVL_IMPORT_PATH
        'use "../stdlib/str.rvl" { trim }\n'       # vendored copy, relative
        "pub fn go(s: Str) -> Bool { return is_space(s) }\n"
    )
    return str(proj / "src" / "root.rvl"), real, vendored


def test_394_duplicate_function_names_both_absolute_paths(tmp_path, monkeypatch):
    root, real, vendored = _write_394(tmp_path)
    monkeypatch.setenv("REVL_IMPORT_PATH", str(tmp_path / "sdk"))
    with pytest.raises(RevlError) as exc:
        compile_files([root])
    msg = str(exc.value)
    assert "duplicate function" in msg, msg
    assert "is_space" in msg, msg
    # BOTH resolved absolute paths named — not just the one that lost
    assert str(real.resolve()) in msg, msg
    assert str(vendored.resolve()) in msg, msg


def test_394_two_copies_of_one_file_is_called_out(tmp_path, monkeypatch):
    """The specific two-byte-identical-copies case is named as such."""
    root, real, vendored = _write_394(tmp_path)
    monkeypatch.setenv("REVL_IMPORT_PATH", str(tmp_path / "sdk"))
    with pytest.raises(RevlError) as exc:
        compile_files([root])
    msg = str(exc.value).lower()
    assert "identical" in msg or "same file" in msg or "two copies" in msg, msg
