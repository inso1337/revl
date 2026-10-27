"""The pre-PR merge-ref check distinguishes main-side failures (issue #2848)."""

from __future__ import annotations

import subprocess
import sys
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MERGE_REF = ROOT / "tools" / "pre_pr_merge_ref.py"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=False
    )
    if proc.returncode:
        raise AssertionError(
            f"git {' '.join(args)} failed in {cwd}:\n{proc.stdout}{proc.stderr}"
        )
    return proc


def _configure(repo: Path) -> None:
    _git(repo, "config", "user.name", "Merge-ref test")
    _git(repo, "config", "user.email", "merge-ref@example.invalid")


def _repository(tmp_path: Path, *, main_guard: bool, conflict: bool = False) -> Path:
    remote = tmp_path / "origin.git"
    repo = tmp_path / "branch"
    updater = tmp_path / "main-update"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    _git(tmp_path, "init", "--initial-branch=main", str(repo))
    _configure(repo)

    tools = repo / "tools"
    tools.mkdir()
    check = tools / "pre_pr.sh"
    check.write_text(
        "#!/bin/sh\necho 'pre-PR checks passed'\n",
        encoding="utf-8",
    )
    (tools / "census_artifact.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "if '--verify' not in sys.argv or '--strict' not in sys.argv:\n"
        "    raise SystemExit(2)\n"
        "if Path('.venv').exists():\n"
        "    raise SystemExit('in-tree environment leaked into census check')\n"
        "print('census reproduction current')\n",
        encoding="utf-8",
    )
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_read_text_encoding_guard.py").write_text(
        "from pathlib import Path\n"
        "\n"
        "def test_no_main_side_guard_site():\n"
        "    marker = 'main-side' + '-guard'\n"
        "    offenders = [\n"
        "        str(path) for path in Path('.').rglob('*.py')\n"
        "        if marker in path.read_text(encoding='utf-8')\n"
        "    ]\n"
        "    assert not offenders, 'main-side guard site: ' + ', '.join(offenders)\n",
        encoding="utf-8",
    )
    (tests / "test_open_encoding_guard.py").write_text(
        "def test_open_encoding_guard_passes():\n    assert True\n",
        encoding="utf-8",
    )
    (tests / "test_affected_tests.py").write_text(
        "def test_selector_mapping_pin_passes():\n    assert True\n",
        encoding="utf-8",
    )
    source = repo / "src" / "revl"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    _git(repo, "switch", "-c", "feature")
    if conflict:
        (repo / "README.md").write_text("branch change\n", encoding="utf-8")
        _git(repo, "add", "README.md")
    else:
        (repo / "feature.txt").write_text("branch-only change\n", encoding="utf-8")
        _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "branch change")
    ignored_env = repo / ".venv"
    ignored_env.mkdir()
    (ignored_env / "generated.txt").write_text("local environment\n", encoding="utf-8")

    _git(tmp_path, "clone", str(remote), str(updater))
    _configure(updater)
    if main_guard:
        main_source = updater / "src" / "revl" / "main_side_guard.py"
        main_source.write_text("# main-side-guard\n", encoding="utf-8")
    elif conflict:
        (updater / "README.md").write_text("main change\n", encoding="utf-8")
    else:
        (updater / "README.md").write_text("main update\n", encoding="utf-8")
    _git(updater, "add", ".")
    _git(updater, "commit", "-m", "advance main")
    _git(updater, "push", "origin", "main")
    return repo


def _run_merge_ref(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MERGE_REF)],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def test_pre_pr_script_dispatches_merge_ref_to_helper(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    shutil.copy2(ROOT / "tools" / "pre_pr.sh", tools / "pre_pr.sh")
    (tools / "pre_pr_merge_ref.py").write_text(
        "import sys\nprint('merge-ref helper called')\nsys.exit(23)\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        ["bash", str(tools / "pre_pr.sh"), "--merge-ref"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 23
    assert "merge-ref helper called" in proc.stdout


def test_merge_ref_reports_only_a_main_side_guard_failure(tmp_path):
    repo = _repository(tmp_path, main_guard=True)

    proc = _run_merge_ref(repo)

    assert proc.returncode == 1
    assert "branch-tree checks passed" in proc.stdout
    assert "merged-tree checks failed" in proc.stdout
    assert "New merged-tree finding: encoding guards failed." in proc.stdout
    assert "main-side guard site: src/revl/main_side_guard.py" in proc.stdout
    assert "pre-PR checks passed" not in proc.stdout
    worktrees = _git(repo, "worktree", "list", "--porcelain").stdout
    assert worktrees.count("worktree ") == 1


def test_merge_ref_passes_when_current_main_adds_no_failing_guard(tmp_path):
    repo = _repository(tmp_path, main_guard=False)

    proc = _run_merge_ref(repo)

    assert proc.returncode == 0
    assert "branch-tree checks passed" in proc.stdout
    assert "merged-tree checks passed" in proc.stdout
    assert "no new findings" in proc.stdout
    worktrees = _git(repo, "worktree", "list", "--porcelain").stdout
    assert worktrees.count("worktree ") == 1


def test_merge_ref_reports_conflicts_and_removes_the_temporary_worktree(tmp_path):
    repo = _repository(tmp_path, main_guard=False, conflict=True)

    proc = _run_merge_ref(repo)

    assert proc.returncode == 2
    assert "branch-tree checks passed" in proc.stdout
    assert "does not merge cleanly into feature" in proc.stderr
    assert "CONFLICT" in proc.stderr
    worktrees = _git(repo, "worktree", "list", "--porcelain").stdout
    assert worktrees.count("worktree ") == 1


def test_merge_ref_refuses_a_dirty_branch_before_fetching(tmp_path):
    repo = _repository(tmp_path, main_guard=False)
    (repo / "uncommitted.txt").write_text("uncommitted\n", encoding="utf-8")

    proc = _run_merge_ref(repo)

    assert proc.returncode == 2
    assert "commit or remove worktree changes first" in proc.stderr
    assert "fetching current origin/main" not in proc.stdout
    worktrees = _git(repo, "worktree", "list", "--porcelain").stdout
    assert worktrees.count("worktree ") == 1
