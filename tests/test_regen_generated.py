"""`tools/regen_generated.py` on a synthetic two-branch conflict (issue #1784).

Each test builds a throwaway git repository with a toy generator
(`gen.py`, which writes `out.txt` from `src.txt`) and a registry pointing the
tool at it, then merges two branches that both regenerated the output. The
tool must:

* take main's side for a WHOLLY generated file and regenerate it from the
  merged inputs, then run the generator's check;
* for a file that is only PARTLY generated, regenerate the region and merge
  the hand-written text three ways, so neither side's prose is dropped and a
  real prose conflict stays one;
* leave a hand-maintained file in conflict and print its rule;
* leave `bench/results/` alone unless `--bench` is passed;
* skip a generator whose tool is missing, loudly, leaving its files alone.

The real registry is only listed here (`--list`): driving the real generators
is what the tool's users do, and each of them has its own tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "regen_generated.py"

# out.txt is "generated: " + every line of src.txt, upper-cased; --check
# fails unless out.txt is exactly that.
GEN = '''import hashlib, sys
raw = open("src.txt").read()
digest = hashlib.sha256(raw.encode()).hexdigest()[:12]
want = "generated from " + digest + "\\n" + "".join(
    line.upper() + "\\n" for line in raw.splitlines())
if "--check" in sys.argv:
    sys.exit(0 if open("out.txt").read() == want else 1)
open("out.txt", "w").write(want)
'''

# notes.md is hand prose around a generated region the toy generator rewrites
PART = '''import hashlib, re, sys
raw = open("src.txt").read()
digest = hashlib.sha256(raw.encode()).hexdigest()[:12]
text = open("notes.md").read()
block = ("<!-- gen -->\\n" + str(len(raw.splitlines())) + " lines, " + digest
         + "\\n<!-- /gen -->")
new = re.sub(r"<!-- gen -->.*?<!-- /gen -->", block, text, flags=re.S)
if "--check" in sys.argv:
    sys.exit(0 if new == text else 1)
open("notes.md", "w").write(new)
'''

BASE_NOTES = "# notes\n\nintro\n\n<!-- gen -->\n<!-- /gen -->\n\noutro\n"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for key in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"):
        env.pop(key, None)
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, env=env, check=check)


def _registry(repo: Path, **extra) -> Path:
    data = {
        "registry": [
            {"name": "toy", "paths": ["out.txt"], "whole": True,
             "write": [{"run": ["{python}", "gen.py"]}],
             "check": [{"run": ["{python}", "gen.py", "--check"]}]},
            {"name": "part", "paths": ["notes.md"], "whole": False,
             "write": [{"run": ["{python}", "part.py"]}],
             "check": [{"run": ["{python}", "part.py", "--check"]}]},
        ],
        "hand": [{"paths": ["ledger.json"], "rule": "move the counts by hand"}],
        "bench": ["bench/results/*"],
    }
    data["registry"] += extra.get("more", [])
    path = repo.parent / "registry.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _regen(repo: Path, cwd: Path) -> None:
    subprocess.run([sys.executable, "gen.py"], cwd=cwd, check=True)
    subprocess.run([sys.executable, "part.py"], cwd=cwd, check=True)


@pytest.fixture
def repo(tmp_path):
    """A repo whose `main` and `feature` both changed `src.txt` (different
    lines) and both regenerated, so the merge conflicts in the outputs."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "gen.py").write_text(GEN, encoding="utf-8")
    (repo / "part.py").write_text(PART, encoding="utf-8")
    (repo / "src.txt").write_text("a\nb\nc\nd\n", encoding="utf-8")
    (repo / "notes.md").write_text(BASE_NOTES, encoding="utf-8")
    (repo / "ledger.json").write_text('{"f": 1}\n', encoding="utf-8")
    (repo / "bench" / "results").mkdir(parents=True)
    (repo / "bench" / "results" / "r.json").write_text("{}\n", encoding="utf-8")
    _regen(repo, repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")

    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "src.txt").write_text("a-feature\nb\nc\nd\n", encoding="utf-8")
    (repo / "ledger.json").write_text('{"f": 2}\n', encoding="utf-8")
    (repo / "bench" / "results" / "r.json").write_text('{"feature": 1}\n', encoding="utf-8")
    _regen(repo, repo)
    _git(repo, "commit", "-q", "-am", "feature")

    _git(repo, "checkout", "-q", "main")
    (repo / "src.txt").write_text("a\nb\nc\nd-main\ne\n", encoding="utf-8")
    (repo / "ledger.json").write_text('{"f": 3}\n', encoding="utf-8")
    (repo / "bench" / "results" / "r.json").write_text('{"main": 1}\n', encoding="utf-8")
    _regen(repo, repo)
    _git(repo, "commit", "-q", "-am", "main")

    _git(repo, "checkout", "-q", "feature")
    merged = _git(repo, "merge", "main", check=False)
    assert merged.returncode != 0, "the fixture must produce a conflict"
    return repo


def _tool(repo: Path, registry: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TOOL), "--registry", str(registry),
                           *args], cwd=repo, capture_output=True, text=True)


def _unmerged(repo: Path) -> set:
    return set(_git(repo, "diff", "--name-only", "--diff-filter=U").stdout.split())


def test_the_fixture_conflicts_in_the_generated_files(repo):
    assert {"out.txt", "notes.md", "ledger.json",
            "bench/results/r.json"} <= _unmerged(repo)
    assert "src.txt" not in _unmerged(repo)        # the inputs merged cleanly


def test_a_generated_conflict_is_regenerated_from_the_merged_inputs(repo):
    result = _tool(repo, _registry(repo))
    out = (repo / "out.txt").read_text(encoding="utf-8")
    assert out.splitlines()[1:] == ["A-FEATURE", "B", "C", "D-MAIN", "E"], result.stdout
    assert out.startswith("generated from "), out
    assert "out.txt" not in _unmerged(repo)
    assert "<<<<<<<" not in out
    assert "toy check: " in result.stdout             # its check ran
    staged = _git(repo, "diff", "--cached", "--name-only").stdout.split()
    assert "out.txt" in staged


def test_a_partly_generated_file_regenerates_when_nothing_of_the_branch_is_lost(repo):
    _tool(repo, _registry(repo))
    notes = (repo / "notes.md").read_text(encoding="utf-8")
    assert "<!-- gen -->\n5 lines, " in notes
    assert "notes.md" not in _unmerged(repo)


def _prose_repo(tmp_path, feature_notes: str, main_notes: str) -> Path:
    """Both sides change src.txt (so the generated block conflicts) and edit
    the hand-written prose of notes.md as given."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "gen.py").write_text(GEN, encoding="utf-8")
    (repo / "part.py").write_text(PART, encoding="utf-8")
    (repo / "src.txt").write_text("a\n", encoding="utf-8")
    (repo / "notes.md").write_text(BASE_NOTES, encoding="utf-8")
    _regen(repo, repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    for branch, src, notes in (("feature", "a\nb\n", feature_notes),
                               ("main", "z\na\n", main_notes)):
        if branch == "feature":
            _git(repo, "checkout", "-q", "-b", "feature")
        else:
            _git(repo, "checkout", "-q", "main")
        (repo / "src.txt").write_text(src, encoding="utf-8")
        (repo / "notes.md").write_text(notes, encoding="utf-8")
        _regen(repo, repo)
        _git(repo, "commit", "-q", "-am", branch)
    _git(repo, "checkout", "-q", "feature")
    assert _git(repo, "merge", "main", check=False).returncode != 0
    assert "notes.md" in _unmerged(repo)
    return repo


def test_prose_both_sides_edited_apart_merges_with_a_fresh_region(tmp_path):
    """Each side's prose survives, and the region is the merged inputs' one."""
    repo = _prose_repo(
        tmp_path,
        BASE_NOTES.replace("intro", "intro, from the feature"),
        BASE_NOTES.replace("outro", "outro, from main"))
    result = _tool(repo, _registry(repo), "--only", "part", "--no-check")
    text = (repo / "notes.md").read_text(encoding="utf-8")
    assert "notes.md" not in _unmerged(repo), result.stdout
    assert "intro, from the feature" in text and "outro, from main" in text
    assert "<<<<<<<" not in text
    assert "3 lines, " in text          # z, a, b: the merged src.txt


def test_prose_both_sides_edited_in_one_place_stays_a_real_conflict(tmp_path):
    repo = _prose_repo(
        tmp_path,
        BASE_NOTES.replace("intro", "feature prose"),
        BASE_NOTES.replace("intro", "main prose"))
    result = _tool(repo, _registry(repo), "--no-check")
    assert result.returncode == 1
    assert "notes.md" in _unmerged(repo)
    text = (repo / "notes.md").read_text(encoding="utf-8")
    assert "feature prose" in text and "main prose" in text and "<<<<<<<" in text
    # ...and only the prose conflicts: the region is already fresh, once
    assert text.count("<!-- gen -->") == 1 and "3 lines, " in text
    assert "only PART of it is generated" in result.stdout


def test_a_hand_maintained_file_is_left_in_conflict_with_its_rule(repo):
    result = _tool(repo, _registry(repo))
    assert result.returncode == 1
    assert "ledger.json" in _unmerged(repo)
    assert "HAND-MAINTAINED" in result.stdout and "move the counts by hand" in result.stdout


def test_bench_results_are_left_alone_unless_asked(repo):
    result = _tool(repo, _registry(repo))
    assert "bench/results/r.json" in _unmerged(repo)
    assert "pass --bench" in result.stdout


def test_bench_results_take_mains_side_with_the_flag(repo):
    _tool(repo, _registry(repo), "--bench")
    assert "bench/results/r.json" not in _unmerged(repo)
    assert json.loads((repo / "bench" / "results" / "r.json").read_text()) == {"main": 1}


def test_a_generator_whose_tool_is_missing_is_skipped_loudly(repo):
    more = [{"name": "needs-tool", "paths": ["never.txt"], "whole": True,
             "requires": ["definitely-not-a-real-tool-1784"],
             "write": [{"run": ["definitely-not-a-real-tool-1784"]}],
             "check": [{"run": ["definitely-not-a-real-tool-1784", "--check"]}]}]
    result = _tool(repo, _registry(repo, more=more), "--all")
    assert "SKIPPED" in result.stdout
    assert "definitely-not-a-real-tool-1784 is not installed" in result.stdout


def test_a_failing_check_is_reported(repo):
    # a hand edit inside the generated output after the tool ran is what the
    # check exists to catch
    registry = _registry(repo)
    _tool(repo, registry)
    (repo / "out.txt").write_text("hand edit\n", encoding="utf-8")
    result = _tool(repo, registry, "--only", "part")
    assert result.returncode == 1
    assert "FAILED: toy check" in result.stdout


def test_outside_a_merge_it_needs_an_explicit_selection(tmp_path):
    repo = tmp_path / "plain"
    repo.mkdir()
    _git(repo, "init", "-q")
    result = _tool(repo, _registry(repo))
    assert result.returncode == 2 and "no merge in progress" in result.stdout


def test_the_real_registry_lists_every_generator_in_dependency_order():
    result = subprocess.run([sys.executable, str(TOOL), "--list"], cwd=ROOT,
                            capture_output=True, text=True, check=True)
    names = [line.split(" ", 1)[0] for line in result.stdout.splitlines()
             if line and not line.startswith((" ", "hand-maintained", "left alone"))]
    assert names == ["gate-crates", "grammar", "provenance", "census", "formal",
                     "conformance", "docgen"]
    assert "tests/fixtures/selfhost_uncovered_lines.json" in result.stdout
    assert "left alone unless --bench: bench/results/*" in result.stdout


def test_the_real_registry_names_tools_and_files_that_exist():
    """A renamed generator or a moved output must fail here, not in a PR."""
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import regen_generated as tool
    finally:
        sys.path.pop(0)
    for group in tool.REGISTRY:
        for step in group["write"] + group["check"]:
            script = step["run"][1] if step["run"][0] == "{python}" else None
            if script and script.endswith(".py"):
                assert (ROOT / script).is_file(), f"{group['name']}: {script}"
        for glob in group["paths"]:
            if "*" not in glob:
                assert (ROOT / glob).is_file(), f"{group['name']}: {glob}"
    for entry in tool.HAND:
        for path in entry["paths"]:
            assert (ROOT / path).is_file(), path
