"""Issue #2849: a red-before / green-after receipt that cannot be faked.

The two failures the issue names are pinned here as tool behaviour. A node
that passes on the base ref must be refused (no red-before), a base ref that
cannot be exported must be refused (no evidence), and a head side that does
not pass must be refused (no green-after). The receipt names the commits, the
diff range, the overlaid head test files and the exact pytest commands, so a
reviewer reads the evidence instead of re-asking for hand-typed counts.

The runs use small throwaway git repositories with a real pytest subprocess,
so the test exercises the archive, the overlay, both runs and the summary
reader rather than a mock of them.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from _load_by_path import load_by_path

ROOT = Path(__file__).resolve().parents[1]
receipt = load_by_path(
    "revl_evidence_receipt", ROOT / "tools" / "evidence_receipt.py")

_DOUBLE = "from calc import double\n\n\ndef test_double():\n    assert double(3) == 6\n"
_WRONG = "def double(x):\n    return x + 1\n"
_RIGHT = "def double(x):\n    return x * 2\n"
_FEATURE = "VALUE = 6\n"
_TEST_FEATURE = (
    "from feature import VALUE\n\n\ndef test_value():\n"
    "    assert VALUE == 6\n")
_SKIP_FEATURE = (
    "import pytest\nfrom feature import VALUE\n\n\ndef test_value():\n"
    "    assert VALUE == 6\n    pytest.skip('not yet')\n")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True).stdout


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=Evidence Test",
         "-c", "user.email=evidence@example.invalid",
         "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _repo(tmp_path: Path, base_files: dict[str, str],
          head_files: dict[str, str]) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    base = _commit(repo, base_files, "base")
    head = _commit(repo, head_files, "head")
    return repo, base, head


_GEN_TEST = (
    "from gen import VALUE\n\n\ndef test_gen():\n    assert VALUE == 1\n")
_LONG_CAUSE = ("fixture store missing at /var/data/" + "segment-" * 30
               + "END")
_LOADER_BROKEN = (
    "def load():\n"
    f"    raise FileNotFoundError({_LONG_CAUSE!r})\n")
_LOADER_FIXED = "def load():\n    return 1\n"
_TEST_LOADER = (
    "from loader import load\n\n\ndef test_load():\n"
    "    assert load() == 1\n")
_SLEEP_ALWAYS = (
    "import time\n\n\ndef test_double():\n    time.sleep(60)\n")
_CALC_FROM_GEN = "from gen import K\n\n\ndef double(x):\n    return x * K\n"
_SLEEP_WHEN_RIGHT = (
    "import time\n\nfrom calc import double\n\n\ndef test_double():\n"
    "    if double(3) == 6:\n        time.sleep(60)\n"
    "    assert double(3) == 6\n")


def _run(repo: Path, base: str, out: Path | None = None,
         node: str = "tests/test_double.py::test_double",
         extra: list[str] | None = None) -> int:
    argv = ["--repo", str(repo), "--base", base, "--node", node]
    if out is not None:
        argv += ["--out", str(out)]
    return receipt.main(argv + (extra or []))


def _receipt_from(capsys) -> str:
    return capsys.readouterr().out


def test_receipt_names_the_failing_base_line_and_the_passing_head(
        tmp_path, capsys):
    repo, base, head = _repo(
        tmp_path, {"calc.py": _WRONG},
        {"calc.py": _RIGHT, "tests/test_double.py": _DOUBLE})
    out = tmp_path / "receipt.md"

    code = _run(repo, base, out=out)
    text = _receipt_from(capsys)

    assert code == 0
    assert out.read_text(encoding="utf-8").strip() == text.strip()
    assert "Red-before and green-after receipt" in text
    assert "base result: FAILED" in text
    assert "head result: PASSED" in text
    assert "FAILED tests/test_double.py::test_double" in text
    assert "1 passed" in text
    assert base[:12] in text and head[:12] in text
    assert "2 files changed" in text
    overlay = [line for line in text.splitlines()
               if line.startswith("- base export:")]
    assert len(overlay) == 1
    assert "tests/test_double.py" in overlay[0]
    # The implementation stays at the base ref: only test files are overlaid.
    assert "calc.py" not in overlay[0]


def test_a_node_that_passes_on_base_is_refused_as_no_red_before(
        tmp_path, capsys):
    repo, base, _head = _repo(
        tmp_path, {"calc.py": _RIGHT},
        {"calc.py": _RIGHT, "tests/test_double.py": _DOUBLE})
    out = tmp_path / "receipt.md"

    code = _run(repo, base, out=out)
    captured = capsys.readouterr()

    assert code == 1
    assert "NO RED-BEFORE" in captured.err
    assert "passed on base" in captured.err
    assert not out.exists()


def test_a_node_that_does_not_pass_on_head_is_refused_as_not_green(
        tmp_path, capsys):
    repo, base, _head = _repo(
        tmp_path, {"calc.py": _WRONG},
        {"calc.py": _WRONG, "tests/test_double.py": _DOUBLE})
    out = tmp_path / "receipt.md"

    code = _run(repo, base, out=out)

    captured = capsys.readouterr()
    assert code == 1
    assert "NOT GREEN" in captured.err
    assert "did not pass on head" in captured.err
    assert not out.exists()


def test_an_unbuildable_base_export_is_refused(tmp_path, capsys):
    repo, _base, _head = _repo(
        tmp_path, {"calc.py": _WRONG},
        {"calc.py": _RIGHT, "tests/test_double.py": _DOUBLE})

    code = _run(repo, "does-not-exist")

    captured = capsys.readouterr()
    assert code == 2
    assert "cannot resolve base" in captured.err


def test_a_collection_error_on_base_is_still_a_red(tmp_path, capsys):
    """pytest 9 exits 4 for a collection error on a ``node::test`` selection.

    That is the canonical new-API shape: the head test file imports a module
    the base ref does not have. The summary says ``1 error``; the tool must
    read that as the red-before rather than refuse the run.
    """
    repo, base, _head = _repo(
        tmp_path, {"calc.py": _WRONG},
        {"calc.py": _WRONG, "feature.py": _FEATURE,
         "tests/test_feature.py": _TEST_FEATURE})

    code = _run(repo, base, node="tests/test_feature.py::test_value")
    text = _receipt_from(capsys)

    assert code == 0
    assert "base result: ERROR" in text
    assert "ModuleNotFoundError: No module named 'feature'" in text
    assert "head result: PASSED" in text


def test_a_skipping_head_is_not_green(tmp_path, capsys):
    repo, base, _head = _repo(
        tmp_path, {"calc.py": _WRONG},
        {"calc.py": _WRONG, "feature.py": _FEATURE,
         "tests/test_feature.py": _SKIP_FEATURE})
    out = tmp_path / "receipt.md"

    code = _run(repo, base, out=out, node="tests/test_feature.py::test_value")

    captured = capsys.readouterr()
    assert code == 1
    assert "NOT GREEN" in captured.err
    assert "skipped on head" in captured.err
    assert not out.exists()


def test_two_nodes_are_refused(tmp_path, capsys):
    repo, base, _head = _repo(
        tmp_path, {"calc.py": _WRONG},
        {"calc.py": _RIGHT, "tests/test_double.py": _DOUBLE})

    code = receipt.main([
        "--repo", str(repo), "--base", base,
        "--node", "tests/test_double.py",
        "--node", "tests/test_double.py::test_double"])

    captured = capsys.readouterr()
    assert code == 2
    assert "one receipt names one node" in captured.err


def test_a_failed_archive_is_reported_as_an_unbuildable_export(tmp_path):
    repo, _base, _head = _repo(
        tmp_path, {"calc.py": _WRONG},
        {"calc.py": _RIGHT, "tests/test_double.py": _DOUBLE})

    with pytest.raises(receipt.Refused) as info:
        receipt._git_archive(repo, "0" * 40)

    assert "base export cannot be built" in str(info.value)


def test_uncommitted_tracked_changes_are_refused(tmp_path, capsys):
    repo, base, _head = _repo(
        tmp_path, {"calc.py": _WRONG},
        {"calc.py": _RIGHT, "tests/test_double.py": _DOUBLE})
    (repo / "calc.py").write_text(_WRONG, encoding="utf-8")

    code = _run(repo, base)

    captured = capsys.readouterr()
    assert code == 2
    assert "uncommitted or untracked changes" in captured.err


def test_an_untracked_module_is_refused(tmp_path, capsys):
    """Untracked code is not the commit the receipt names, so it is refused."""
    repo, base, _head = _repo(
        tmp_path, {"calc.py": _WRONG},
        {"calc.py": _RIGHT, "tests/test_double.py": _DOUBLE})
    (repo / "src").mkdir()
    (repo / "src" / "helper.py").write_text("WHY = 1\n", encoding="utf-8")

    code = _run(repo, base)

    captured = capsys.readouterr()
    assert code == 2
    assert "untracked changes" in captured.err
    assert "src/helper.py" in captured.err


def test_summary_reader_does_not_read_a_skip_as_a_pass():
    counts = receipt.parse_summary("2 skipped in 1.10s\n")
    assert counts.found is True
    assert counts.skipped == 2
    assert counts.passed == 0
    assert counts.executed == 0


def test_an_xfail_on_base_is_not_a_red_before():
    counts = receipt.Counts(found=True, xfailed=1,
                            summary_line="1 xfailed in 0.21s")
    run = receipt.Run(("python", "-m", "pytest", "node"), Path("."), 0,
                      "1 xfailed in 0.21s", counts)

    with pytest.raises(receipt.NoRedBefore) as info:
        receipt._check_base(run, "origin/main", "0" * 40)

    assert "xfail is not a red-before" in str(info.value)


def test_summary_reader_reads_a_collection_error():
    counts = receipt.parse_summary(
        "ERROR tests/test_double.py\n"
        "=============================== 1 error in 0.32s "
        "===============================\n")
    assert counts.found is True
    assert counts.errors == 1


def test_an_ignored_module_only_in_the_worktree_is_not_a_green_after(
        tmp_path, capsys):
    """Both sides see only committed files.

    The test imports a gitignored ``gen.py`` that exists only in the live
    worktree, and the head commit changes nothing the test reads (a README).
    The base export cannot import it; if the head side ran in the worktree it
    could, and a difference in environment would read as red-before and
    green-after. Run in the head export, the head side is red too.
    """
    repo, base, _head = _repo(
        tmp_path,
        {".gitignore": "gen.py\n", "tests/test_gen.py": _GEN_TEST},
        {"README.md": "notes\n"})
    (repo / "gen.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""
    out = tmp_path / "receipt.md"

    code = _run(repo, base, out=out, node="tests/test_gen.py::test_gen")

    captured = capsys.readouterr()
    assert code == 1
    assert "NOT GREEN" in captured.err
    assert "did not pass on head" in captured.err
    assert "head result: PASSED" not in captured.out
    assert not out.exists()


def test_a_long_cause_is_carried_whole_into_the_receipt(
        tmp_path, capsys, monkeypatch):
    """pytest cuts its short summary at the terminal width; the cause stays."""
    monkeypatch.setenv("COLUMNS", "80")
    # pytest leaves the summary uncut on CI; this test must guard both.
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("BUILD_NUMBER", raising=False)
    repo, base, _head = _repo(
        tmp_path, {"loader.py": _LOADER_BROKEN},
        {"loader.py": _LOADER_FIXED, "tests/test_loader.py": _TEST_LOADER})

    code = _run(repo, base, node="tests/test_loader.py::test_load")
    text = _receipt_from(capsys)

    assert code == 0
    assert len(_LONG_CAUSE) > 200
    assert f"FileNotFoundError: {_LONG_CAUSE}" in text


def test_a_base_run_that_outlives_the_timeout_is_refused(tmp_path, capsys):
    """A hanging run is stopped and refused, never a receipt."""
    repo, base, _head = _repo(
        tmp_path, {"calc.py": _WRONG},
        {"calc.py": _RIGHT, "tests/test_double.py": _SLEEP_ALWAYS})
    out = tmp_path / "receipt.md"

    started = time.monotonic()
    code = _run(repo, base, out=out, extra=["--timeout", "1"])
    elapsed = time.monotonic() - started

    captured = capsys.readouterr()
    assert code == 2
    assert "TIMED OUT" in captured.err
    assert "the base run" in captured.err
    assert "Red-before" not in captured.out
    assert not out.exists()
    assert elapsed < 30


def test_a_head_run_that_outlives_the_timeout_is_refused(
        tmp_path, capsys, monkeypatch):
    """The timeout reaches the head run too.

    Both runs must receive the ``--timeout`` value; that is recorded. The base
    run (a quick red) is then given 120 seconds so a loaded machine cannot
    make it the run that times out, and the head run keeps the 2 seconds.
    """
    repo, base, _head = _repo(
        tmp_path, {"calc.py": _WRONG},
        {"calc.py": _RIGHT, "tests/test_double.py": _SLEEP_WHEN_RIGHT})
    out = tmp_path / "receipt.md"
    real_run = receipt._run_pytest
    asked: dict[str, float] = {}

    def run(*args, timeout, side, **kwargs):
        asked[side] = timeout
        if side == "base":
            timeout = 120.0
        return real_run(*args, timeout=timeout, side=side, **kwargs)

    monkeypatch.setattr(receipt, "_run_pytest", run)

    code = _run(repo, base, out=out, extra=["--timeout", "2"])

    captured = capsys.readouterr()
    assert asked == {"base": 2.0, "head": 2.0}
    assert code == 2
    assert "TIMED OUT" in captured.err
    assert "the head run" in captured.err
    assert "Red-before" not in captured.out
    assert not out.exists()


def test_an_ambient_pythonpath_into_the_worktree_is_dropped(
        tmp_path, capsys, monkeypatch):
    """A PYTHONPATH entry naming the repository cannot reach the worktree.

    The head's tracked ``calc.py`` imports a gitignored ``gen.py`` that exists
    only in the worktree. With the repository on the ambient PYTHONPATH, a
    name the head export lacks would fall through to the worktree and the
    head would pass, though the commit errors in a clean checkout.
    """
    repo, base, _head = _repo(
        tmp_path, {".gitignore": "gen.py\n", "calc.py": _WRONG},
        {"calc.py": _CALC_FROM_GEN, "tests/test_double.py": _DOUBLE})
    (repo / "gen.py").write_text("K = 2\n", encoding="utf-8")
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""
    ambient = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join(filter(None, [str(repo), ambient])))
    out = tmp_path / "receipt.md"

    code = _run(repo, base, out=out)

    captured = capsys.readouterr()
    assert code == 1
    assert "NOT GREEN" in captured.err
    assert "head result: PASSED" not in captured.out
    assert not out.exists()


def test_an_ambient_pythonpath_outside_the_repository_is_kept(
        tmp_path, capsys, monkeypatch):
    """Entries outside the repository (a third-party runtime) pass through."""
    repo, base, _head = _repo(
        tmp_path, {"calc.py": _WRONG},
        {"calc.py": _CALC_FROM_GEN, "tests/test_double.py": _DOUBLE})
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "gen.py").write_text("K = 2\n", encoding="utf-8")
    ambient = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join(filter(None, [str(vendor), ambient])))

    code = _run(repo, base)

    text = _receipt_from(capsys)
    assert code == 0
    assert "base result: FAILED" in text
    assert "head result: PASSED" in text
