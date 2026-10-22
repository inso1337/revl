"""Issue #2799: expose the fail-safe pre-merge selection over MCP."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

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


def _selector():
    return server._load_affected_tests_selector(
        str(ROOT / "tools" / "affected_tests.py"))


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


def test_verb_is_read_only_and_matches_the_shared_selector(monkeypatch):
    monkeypatch.chdir(ROOT)
    _git_at_root(monkeypatch)

    result = _call(["docs/mcp-reference.md"])
    selector = _selector()
    expected = selector.select(["docs/mcp-reference.md"], ROOT)

    assert result["ok"] is True
    assert result["base"] == "merge-base-sha"
    assert result["selection"] == expected
    assert result["machine"] == selector._emit(expected, "merge-base-sha", "machine")
    tool = next(tool for tool in server.TOOLS if tool["name"] == "revl_affected_tests")
    assert tool["annotations"] == {"readOnlyHint": True, "destructiveHint": False}


def test_missing_worktree_returns_full_with_a_reason(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = _call(["src/revl/mcp/server.py"])

    assert result["selection"]["full"] is True
    assert "not inside a git worktree" in result["selection"]["reason"]
    assert result["machine"].startswith("FULL 1\nREASON ")
    assert "PYTEST tests/" in result["machine"]


def test_missing_merge_base_returns_full_with_a_reason(monkeypatch):
    monkeypatch.chdir(ROOT)
    _git_at_root(monkeypatch, base=None)

    result = _call(["docs/mcp-reference.md"])

    assert result["selection"]["full"] is True
    assert "no merge base" in result["selection"]["reason"]
    assert result["machine"].startswith("FULL 1\nREASON ")


def test_unmapped_path_returns_full_with_the_selector_reason(monkeypatch):
    monkeypatch.chdir(ROOT)
    _git_at_root(monkeypatch)

    result = _call(["path-that-has-no-selector-rule.xyz"])

    assert result["selection"]["full"] is True
    assert "unmapped file" in result["selection"]["reason"]
