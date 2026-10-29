"""Issue #2799: expose the fail-safe pre-merge selection over MCP."""
from __future__ import annotations

import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import server  # noqa: E402


def _call(paths):
    response = server.handle({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "revl_affected_tests", "arguments": {"paths": paths}},
    })
    return response["result"]["structuredContent"]


def _selector(root=ROOT):
    return runpy.run_path(str(Path(root) / "tools" / "affected_tests.py"))


def _git_result(returncode, stdout=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


def _git_at_root(monkeypatch, *, base="merge-base-sha"):
    def run(args, **kwargs):
        if args[1:3] == ["rev-parse", "--show-toplevel"]:
            return _git_result(0, f"{ROOT}\n")
        if args[1] == "merge-base":
            return _git_result(0, f"{base}\n") if base else _git_result(1)
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr(server.subprocess, "run", run)


def _commit_repo(root):
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=root,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "add", "."], cwd=root,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-m", "initial"], cwd=root,
                   capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A self-contained revl worktree. The real-git assertions run here, so they
    need no merge base in the CI checkout's own history."""
    root = tmp_path / "repo"
    (root / "src" / "revl").mkdir(parents=True)
    (root / "tools").mkdir()
    (root / "tests").mkdir()
    (root / "docs").mkdir()
    shutil.copyfile(ROOT / "tools" / "affected_tests.py",
                    root / "tools" / "affected_tests.py")
    (root / "src" / "revl" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "revl" / "foo.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_foo.py").write_text("import revl.foo\n", encoding="utf-8")
    (root / "docs" / "mcp-reference.md").write_text("# mcp\n", encoding="utf-8")
    _commit_repo(root)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server, "_file_roots", lambda: (str(root),))
    return root


def test_verb_is_read_only_and_matches_the_shared_selector(repo):
    before = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo, capture_output=True, text=True, check=True).stdout

    paths = ["docs/mcp-reference.md"]
    result = _call(paths)
    selector = _selector(repo)
    expected = selector["select"](paths, repo)
    expected = selector["add_census_gate"](expected, paths, repo)

    assert result["ok"] is True
    assert result["base"]
    assert result["selection"] == expected
    assert result["machine"] == selector["_emit"](expected, result["base"], "machine")
    tool = next(tool for tool in server.TOOLS if tool["name"] == "revl_affected_tests")
    assert tool["annotations"] == {"readOnlyHint": True, "destructiveHint": False}
    after = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo, capture_output=True, text=True, check=True).stdout
    assert after == before


def test_added_test_that_maps_to_no_touched_module_answers_like_the_cli(repo):
    """The CLI applies `_test_add_delete_override` on top of `select()`, so an
    added test that names and imports nothing the diff touched is FULL. The verb
    must answer what the CLI answers for the same tree (issue #2799 review)."""
    (repo / "src" / "revl" / "foo.py").write_text("VALUE = 2\n", encoding="utf-8")
    (repo / "tests" / "test_unmapped_new.py").write_text(
        "def test_new():\n    pass\n", encoding="utf-8")

    result = _call(["src/revl/foo.py", "tests/test_unmapped_new.py"])
    cli = subprocess.run(
        [sys.executable, str(repo / "tools" / "affected_tests.py"),
         "--format", "machine", "--root", str(repo)],
        cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    assert result["selection"]["full"] is True, result["selection"]
    assert "maps to no touched module" in result["selection"]["reason"]
    assert result["machine"] == cli


def test_missing_worktree_returns_full_with_a_reason(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server, "_file_roots", lambda: (str(tmp_path),))

    result = _call(["src/revl/mcp/server.py"])

    assert result["selection"]["full"] is True
    assert "under the operator-sanctioned roots" in result["selection"]["reason"]
    assert result["machine"].startswith("FULL 1\nREASON ")
    assert "PYTEST tests/" in result["machine"]


def test_missing_merge_base_returns_full_with_a_reason(monkeypatch):
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(server, "_file_roots", lambda: (str(ROOT),))
    _git_at_root(monkeypatch, base=None)

    result = _call(["docs/mcp-reference.md"])

    assert result["selection"]["full"] is True
    assert "no merge base" in result["selection"]["reason"]
    assert result["machine"].startswith("FULL 1\nREASON ")


def test_unmapped_path_returns_full_with_the_selector_reason(repo):
    result = _call(["path-that-has-no-selector-rule.xyz"])

    assert result["selection"]["full"] is True
    assert "unmapped file" in result["selection"]["reason"]


def test_server_uses_the_operator_roots_when_cwd_is_outside_the_worktree(repo):
    result = _call(["docs/mcp-reference.md"])

    assert result["ok"] is True, result
    assert result["selection"]["full"] is False, result


def test_out_of_root_path_fails_safe_to_full(repo):
    result = _call(["/etc/passwd"])

    assert result["selection"]["full"] is True
    assert "outside the operator-sanctioned roots" in result["selection"]["reason"]


def test_deleted_test_path_fails_safe_to_full(repo):
    missing = "tests/test_deleted_affected_selector_2799.py"
    assert not (repo / missing).exists()

    result = _call([missing])

    assert result["selection"]["full"] is True
    assert "possibly deleted" in result["selection"]["reason"]


def test_path_count_is_bounded_before_running_git(monkeypatch):
    monkeypatch.setattr(server, "subprocess", SimpleNamespace(run=lambda *_args, **_kwargs:
                         pytest.fail("git must not run for oversized input")))

    result = _call(["docs/mcp-reference.md"] * (server._AFFECTED_TESTS_MAX_PATHS + 1))

    assert result["selection"]["full"] is True
    assert "more than" in result["selection"]["reason"]


def test_selector_timeout_fails_safe_to_full(monkeypatch):
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(server, "_file_roots", lambda: (str(ROOT),))

    def run(args, **kwargs):
        if args[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return _git_result(0, f"{ROOT}\n")
        if args[:2] == ["git", "merge-base"]:
            return _git_result(0, "merge-base-sha\n")
        if args[1:2] == ["-c"]:
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        raise AssertionError(f"unexpected process: {args}")

    monkeypatch.setattr(server.subprocess, "run", run)

    result = _call(["docs/mcp-reference.md"])

    assert result["selection"]["full"] is True
    assert "timed out" in result["selection"]["reason"]


def test_selector_refreshes_tree_caches_between_calls(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "src" / "revl").mkdir(parents=True)
    (root / "tools").mkdir()
    (root / "tests").mkdir()
    shutil.copyfile(ROOT / "tools" / "affected_tests.py",
                    root / "tools" / "affected_tests.py")
    (root / "src" / "revl" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "revl" / "fresh.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_fresh.py").write_text(
        "import revl.fresh\n", encoding="utf-8")
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=root,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "add", "."], cwd=root,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-m", "initial"], cwd=root,
                   capture_output=True, text=True, check=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server, "_file_roots", lambda: (str(root),))

    first = _call(["src/revl/fresh.py"])
    assert first["selection"]["full"] is False, first
    before_edit = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root, capture_output=True, text=True, check=True).stdout
    assert before_edit == ""

    (root / "src" / "revl" / "__init__.py").write_text(
        "import revl.fresh\n", encoding="utf-8")
    after_edit = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root, capture_output=True, text=True, check=True).stdout
    second = _call(["src/revl/fresh.py"])
    after_call = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root, capture_output=True, text=True, check=True).stdout

    assert second["selection"]["full"] is True, second
    assert "compile-reachable" in second["selection"]["reason"]
    assert after_call == after_edit
