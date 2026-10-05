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
import shutil
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
            {"name": "toy", "paths": ["out.txt"], "merge": "theirs",
             "write": [{"run": ["{python}", "gen.py"]}],
             "check": [{"run": ["{python}", "gen.py", "--check"]}]},
            {"name": "part", "paths": ["notes.md"], "merge": "three-way",
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
    more = [{"name": "needs-tool", "paths": ["never.txt"], "merge": "theirs",
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


# rec.py: the OLD layout writes old.json from src.txt; once records/README
# exists (the NEW layout) it writes records/a.jsonl instead
REC = '''import json, os, sys
rows = open("src.txt").read().splitlines()
new = os.path.exists("records/README")
path, want = (("records/a.jsonl", "".join(json.dumps(r) + "\\n" for r in rows))
              if new else ("old.json", json.dumps(rows) + "\\n"))
if "--check" in sys.argv:
    sys.exit(0 if open(path).read() == want else 1)
open(path, "w").write(want)
'''


def _layout_repo(tmp_path, main_moves: bool) -> Path:
    """`feature` changes src.txt and regenerates old.json. `main` changes
    src.txt too and, when `main_moves`, moves the output to the records
    layout: old.json deleted, records/ added. The merge conflicts on old.json
    (modify/delete when main moved it)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "rec.py").write_text(REC, encoding="utf-8")
    (repo / "src.txt").write_text("a\nm\nn\nb\n", encoding="utf-8")
    (repo / "old.md").write_text("generated page 0\n", encoding="utf-8")
    subprocess.run([sys.executable, "rec.py"], cwd=repo, check=True)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "src.txt").write_text("a-feature\nm\nn\nb\n", encoding="utf-8")
    (repo / "old.md").write_text("generated page 1\n", encoding="utf-8")
    subprocess.run([sys.executable, "rec.py"], cwd=repo, check=True)
    _git(repo, "commit", "-q", "-am", "feature")
    _git(repo, "checkout", "-q", "main")
    (repo / "src.txt").write_text("a\nm\nn\nb-main\n", encoding="utf-8")
    if main_moves:
        _git(repo, "rm", "-q", "old.json")
        (repo / "records").mkdir()
        (repo / "records" / "README").write_text("records\n", encoding="utf-8")
        (repo / "old.md").write_text("a hand-written page now\n", encoding="utf-8")
    else:
        (repo / "old.md").write_text("generated page 2\n", encoding="utf-8")
    subprocess.run([sys.executable, "rec.py"], cwd=repo, check=True)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "main")
    _git(repo, "checkout", "-q", "feature")
    assert _git(repo, "merge", "main", check=False).returncode != 0
    assert "old.json" in _unmerged(repo)
    return repo


def _layout_registry(repo: Path, before: dict) -> Path:
    data = {
        "registry": [{"name": "rec", "paths": ["records/*"], "merge": "theirs",
                      "write": [{"run": ["{python}", "rec.py"]}],
                      "check": [{"run": ["{python}", "rec.py", "--check"]}]}],
        "transitions": [{"paths": ["old.json", "old.md"], "marker": "records/README",
                         "group": "rec", "before": before}],
    }
    path = repo.parent / "registry.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_a_file_main_moved_to_a_new_layout_takes_the_deletion_and_regenerates(tmp_path):
    """The one-time modify/delete conflict every branch cut before a layout
    move hits: main's deletion is taken and the new layout regenerated from
    the merged inputs."""
    repo = _layout_repo(tmp_path, main_moves=True)
    result = _tool(repo, _layout_registry(repo, {"group": "rec"}))
    assert result.returncode == 0, result.stdout
    assert not _unmerged(repo)
    assert not (repo / "old.json").exists()
    records = (repo / "records" / "a.jsonl").read_text(encoding="utf-8")
    assert records == '"a-feature"\n"m"\n"n"\n"b-main"\n'
    assert "old.json: main moved it to a new layout (deleted on main)" in result.stdout
    # the page main put in place of a generated file is main's, as written
    assert (repo / "old.md").read_text() == "a hand-written page now\n"
    assert "old.md: main moved it to a new layout (replaced on main)" in result.stdout
    staged = _git(repo, "diff", "--cached", "--name-status").stdout
    assert "records/a.jsonl" in staged


def test_before_main_moves_the_old_file_is_still_its_generators(tmp_path):
    repo = _layout_repo(tmp_path, main_moves=False)
    result = _tool(repo, _layout_registry(repo, {"group": "rec"}))
    assert result.returncode == 0, result.stdout
    assert json.loads((repo / "old.json").read_text()) == ["a-feature", "m", "n", "b-main"]


def test_before_main_moves_a_hand_file_keeps_its_rule(tmp_path):
    repo = _layout_repo(tmp_path, main_moves=False)
    result = _tool(repo, _layout_registry(repo, {"rule": "edit it by hand"}))
    assert result.returncode == 1
    assert "old.json" in _unmerged(repo)
    assert "HAND-MAINTAINED" in result.stdout and "edit it by hand" in result.stdout


# fix.py reads through conflict markers itself (keeps every record once,
# sorted) and its check refuses a record whose budget is still null
FIX = '''import sys
lines = open("led.jsonl").read().splitlines()
keep = sorted({l for l in lines if l and not l.startswith(("<<<<<<<", "=======", ">>>>>>>"))})
if "--check" in sys.argv:
    sys.exit(1 if any("null" in l for l in keep) else 0)
open("led.jsonl", "w").write("".join(l + "\\n" for l in keep))
'''


def _in_place_repo(tmp_path, feature_row: str) -> tuple:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "fix.py").write_text(FIX, encoding="utf-8")
    (repo / "led.jsonl").write_text('["f1", 1]\n', encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    for branch, row in (("feature", feature_row), ("main", '["f3", 3]')):
        _git(repo, "checkout", "-q", *(["-b", "feature"] if branch == "feature" else ["main"]))
        (repo / "led.jsonl").write_text('["f1", 1]\n' + row + "\n", encoding="utf-8")
        _git(repo, "commit", "-q", "-am", branch)
    _git(repo, "checkout", "-q", "feature")
    assert _git(repo, "merge", "main", check=False).returncode != 0
    data = {"registry": [{"name": "led", "paths": ["led.jsonl"], "merge": "in-place",
                          "write": [{"run": ["{python}", "fix.py"]}],
                          "check": [{"run": ["{python}", "fix.py", "--check"]}],
                          "hint": "set the null budget by hand"}]}
    registry = repo.parent / "registry.json"
    registry.write_text(json.dumps(data), encoding="utf-8")
    return repo, registry


def test_an_in_place_generator_resolves_its_own_conflict_markers(tmp_path):
    repo, registry = _in_place_repo(tmp_path, '["f2", 2]')
    result = _tool(repo, registry)
    assert result.returncode == 0, result.stdout
    assert not _unmerged(repo)
    assert (repo / "led.jsonl").read_text() == '["f1", 1]\n["f2", 2]\n["f3", 3]\n'


def test_an_in_place_check_failure_prints_the_hand_step(tmp_path):
    repo, registry = _in_place_repo(tmp_path, '["f2", null]')
    result = _tool(repo, registry)
    assert result.returncode == 1
    assert not _unmerged(repo)                      # resolved and staged...
    assert "FAILED: led check" in result.stdout     # ...but a budget is owed
    assert "set the null budget by hand" in result.stdout


def test_an_in_place_generator_that_did_not_run_leaves_the_conflict(tmp_path):
    repo, registry = _in_place_repo(tmp_path, '["f2", 2]')
    data = json.loads(registry.read_text())
    data["registry"][0]["write"][0]["slow"] = True
    registry.write_text(json.dumps(data), encoding="utf-8")
    result = _tool(repo, registry, "--fast")
    assert result.returncode == 1
    assert "led.jsonl" in _unmerged(repo)
    assert "still has conflict markers" in result.stdout


def test_a_generator_whose_layout_is_absent_does_not_run(repo):
    more = [{"name": "later", "paths": ["later/*.jsonl"], "merge": "in-place",
             "when": "later/README",
             "write": [{"run": ["{python}", "-c", "raise SystemExit(3)"]}],
             "check": [{"run": ["{python}", "-c", "raise SystemExit(3)"]}]}]
    result = _tool(repo, _registry(repo, more=more), "--only", "later")
    assert "predates the layout" in result.stdout
    assert "later write" not in result.stdout and "later check" not in result.stdout


def test_bench_takes_mains_deletion_with_the_flag(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "bench" / "results").mkdir(parents=True)
    (repo / "bench" / "results" / "gone.json").write_text("{}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "bench" / "results" / "gone.json").write_text('{"f": 1}\n', encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "feature")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "rm", "-q", "bench/results/gone.json")
    _git(repo, "commit", "-q", "-m", "main")
    _git(repo, "checkout", "-q", "feature")
    assert _git(repo, "merge", "main", check=False).returncode != 0
    result = _tool(repo, _registry(repo), "--bench", "--no-check")
    assert not _unmerged(repo), result.stdout
    assert not (repo / "bench" / "results" / "gone.json").exists()


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
             if line and not line.startswith((" ", "hand-maintained", "left alone", "layout move"))]
    assert names == ["gate-crates", "grammar", "provenance", "census", "formal",
                     "conformance", "ledger", "docgen"]
    assert "layout move: tests/fixtures/selfhost_uncovered_lines.json -> ledger" in result.stdout
    assert "layout move: docs/census-artifact.json, docs/census-artifact.md -> census" in result.stdout
    assert ("layout move: tests/fixtures/census_crate_reproduction.json, "
            "tests/fixtures/census_crate_reproduction/reproduction.json, "
            "tests/fixtures/census_crate_reproduction/programs.jsonl -> census"
            in result.stdout)
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


def test_a_generator_reruns_the_ones_it_feeds(repo):
    """Regenerating an input reruns the generator that reads it: `--only toy`
    with toy feeding `part` regenerates both."""
    registry = _registry(repo)
    data = json.loads(registry.read_text())
    data["registry"][0]["feeds"] = ["part"]
    registry.write_text(json.dumps(data), encoding="utf-8")
    result = _tool(repo, registry, "--only", "toy", "--no-check")
    assert "part: reads what this run regenerates, so it reruns too" in result.stdout
    assert "part write: " in result.stdout


def test_the_real_provenance_generator_feeds_the_census():
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import regen_generated as tool
    finally:
        sys.path.pop(0)
    names = [g["name"] for g in tool.REGISTRY]
    for group in tool.REGISTRY:
        for fed in group.get("feeds", ()):
            assert names.index(fed) > names.index(group["name"]), (group["name"], fed)
    provenance = next(g for g in tool.REGISTRY if g["name"] == "provenance")
    assert "census" in provenance["feeds"]


# ---------------------------------------------- missing tools (issue #1864)

def _plain_repo(tmp_path) -> Path:
    repo = tmp_path / "plain"
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


def _one_group(repo: Path, group: dict) -> Path:
    path = repo.parent / "registry.json"
    path.write_text(json.dumps({"registry": [group]}), encoding="utf-8")
    return path


def _bare_env(home: Path) -> dict:
    """An environment whose PATH has git and this python and nothing that
    could hold a real `lake`, with HOME pointed at `home`."""
    pybin = home / "pybin"
    pybin.mkdir(exist_ok=True)
    for name in ("python3", "python"):
        link = pybin / name
        if not link.exists():
            link.symlink_to(sys.executable)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GIT_", "VIRTUAL_ENV"))}
    env["HOME"] = str(home)
    env["PATH"] = os.pathsep.join([str(pybin), "/usr/bin", "/bin"])
    return env


def _fake_lake(home: Path, marker: Path, code: int = 0) -> None:
    elan = home / ".elan" / "bin"
    elan.mkdir(parents=True)
    lake = elan / "lake"
    lake.write_text(f"#!/bin/sh\necho fake lake \"$@\" >> {marker}\nexit {code}\n",
                    encoding="utf-8")
    lake.chmod(0o755)


@pytest.mark.skipif(shutil.which("lake", path="/usr/bin:/bin") is not None,
                    reason="a system lake would be found on the bare PATH")
def test_lake_in_the_elan_home_is_found_when_path_lacks_it(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / "lake-ran"
    _fake_lake(home, marker)
    repo = _plain_repo(tmp_path)
    probe = ("import shutil, sys; "
             "sys.exit(0 if shutil.which('lake') else 7)")
    registry = _one_group(repo, {
        "name": "formalish", "paths": ["STATUS.md"], "merge": "three-way",
        "requires": ["lake"],
        "write": [{"run": ["lake", "build"]},
                  # a generator that shells out to lake itself finds it too
                  {"run": ["{python}", "-c", probe]}],
        "check": [{"run": ["lake", "build"]}]})
    result = subprocess.run([sys.executable, str(TOOL), "--registry", str(registry),
                             "--all"], cwd=repo, capture_output=True, text=True,
                            env=_bare_env(home))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKIPPED" not in result.stdout
    assert marker.read_text().count("fake lake build") == 2   # write + check


def test_a_generator_skipped_for_a_missing_tool_fails_the_run(tmp_path):
    repo = _plain_repo(tmp_path)
    registry = _one_group(repo, {
        "name": "needs-tool", "paths": ["never.txt"], "merge": "theirs",
        "requires": ["definitely-not-a-real-tool-1864"],
        "write": [{"run": ["definitely-not-a-real-tool-1864"]}],
        "check": [{"run": ["definitely-not-a-real-tool-1864", "--check"]}]})
    result = _tool(repo, registry, "--all")
    assert result.returncode == 1, result.stdout
    assert "SKIPPED" in result.stdout
    assert "UNCHECKED, so this run fails" in result.stdout
    assert "every check passes" not in result.stdout


def test_allow_skip_accepts_the_skip_and_keeps_the_banner(tmp_path):
    repo = _plain_repo(tmp_path)
    registry = _one_group(repo, {
        "name": "needs-tool", "paths": ["never.txt"], "merge": "theirs",
        "requires": ["definitely-not-a-real-tool-1864"],
        "write": [{"run": ["definitely-not-a-real-tool-1864"]}],
        "check": [{"run": ["definitely-not-a-real-tool-1864", "--check"]}]})
    result = _tool(repo, registry, "--all", "--allow-skip")
    assert result.returncode == 0, result.stdout
    assert "SKIPPED" in result.stdout
    assert "UNCHECKED" not in result.stdout


def test_a_missing_tool_on_one_step_fails_the_run(tmp_path):
    """A step-level `requires` (the census's cargo step) counts as well."""
    repo = _plain_repo(tmp_path)
    registry = _one_group(repo, {
        "name": "census-ish", "paths": ["out.txt"], "merge": "theirs",
        "write": [{"run": ["definitely-not-a-real-tool-1864"],
                   "requires": ["definitely-not-a-real-tool-1864"]}],
        "check": [{"run": ["{python}", "-c", "pass"]}]})
    result = _tool(repo, registry, "--all")
    assert result.returncode == 1, result.stdout
    assert "census-ish write (definitely-not-a-real-tool-1864)" in result.stdout


def test_a_fast_skip_is_the_callers_choice_and_does_not_fail(tmp_path):
    repo = _plain_repo(tmp_path)
    registry = _one_group(repo, {
        "name": "slowish", "paths": ["out.txt"], "merge": "theirs",
        "write": [{"run": ["{python}", "-c", "raise SystemExit(5)"], "slow": True}],
        "check": [{"run": ["{python}", "-c", "pass"]}]})
    result = _tool(repo, registry, "--all", "--fast")
    assert result.returncode == 0, result.stdout
    assert "--fast, so the slow step" in result.stdout


@pytest.mark.skipif(shutil.which("lake", path="/usr/bin:/bin") is not None,
                    reason="a system lake would be found on the bare PATH")
def test_run_gate_finds_lake_in_the_elan_home(tmp_path):
    """formal/scripts/run_gate.sh used `command -v lake` alone, so a machine
    with elan installed and not sourced printed the not-installed SKIP. The
    fake lake fails, which proves the script got past its guard to call it."""
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / "lake-ran"
    _fake_lake(home, marker, code=3)
    result = subprocess.run(["sh", str(ROOT / "formal" / "scripts" / "run_gate.sh")],
                            capture_output=True, text=True, env=_bare_env(home))
    assert "SKIP (loud)" not in result.stdout, result.stdout
    assert marker.read_text().startswith("fake lake build"), result.stdout + result.stderr
    assert result.returncode != 0


# The #1917 shape: the OLD files and the NEW layout live in the SAME directory,
# inside the generator's own `paths` glob (`tests/fixtures/census_crate_
# reproduction/*`). The transition stages main's deletion of the old files,
# and the generator's output staging must not then try to `git add` those
# already-deleted paths, which fails with "pathspec did not match".
DIR_REC = '''import json, os, sys
rows = open("src.txt").read().splitlines()
new = os.path.exists("rec/README.md")
if new:
    path, want = "rec/v-" + str(len(rows)) + ".json", json.dumps(rows) + "\\n"
else:
    path, want = "rec/facts.json", json.dumps(rows) + "\\n"
if "--check" in sys.argv:
    sys.exit(0 if os.path.exists(path) and open(path).read() == want else 1)
open(path, "w").write(want)
if not new:
    open("rec/programs.jsonl", "w").write("".join(json.dumps(r) + "\\n" for r in rows))
'''


def _dir_layout_repo(tmp_path) -> Path:
    """`feature` regenerates the two-file layout in rec/; `main` moves rec/ to
    one file per version beside a README, deleting both old files. The merge
    conflicts on the old files inside the directory the generator owns."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "rec.py").write_text(DIR_REC, encoding="utf-8")
    (repo / "rec").mkdir()
    (repo / "src.txt").write_text("a\nm\nb\n", encoding="utf-8")
    subprocess.run([sys.executable, "rec.py"], cwd=repo, check=True)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "src.txt").write_text("a-feature\nm\nb\n", encoding="utf-8")
    subprocess.run([sys.executable, "rec.py"], cwd=repo, check=True)
    _git(repo, "commit", "-q", "-am", "feature")
    _git(repo, "checkout", "-q", "main")
    (repo / "src.txt").write_text("a\nm\nb-main\nc-main\n", encoding="utf-8")
    _git(repo, "rm", "-q", "rec/facts.json", "rec/programs.jsonl")
    (repo / "rec").mkdir(exist_ok=True)
    (repo / "rec" / "README.md").write_text("one file per version\n")
    subprocess.run([sys.executable, "rec.py"], cwd=repo, check=True)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "main")
    _git(repo, "checkout", "-q", "feature")
    assert _git(repo, "merge", "main", check=False).returncode != 0
    assert {"rec/facts.json", "rec/programs.jsonl"} & _unmerged(repo)
    return repo


def test_a_move_inside_the_generators_own_directory_completes(tmp_path):
    repo = _dir_layout_repo(tmp_path)
    data = {
        "registry": [{"name": "rec", "paths": ["rec/*"], "merge": "theirs",
                      "write": [{"run": ["{python}", "rec.py"]}],
                      "check": [{"run": ["{python}", "rec.py", "--check"]}]}],
        "transitions": [{"paths": ["rec/facts.json", "rec/programs.jsonl"],
                         "marker": "rec/README.md", "group": "rec",
                         "before": {"group": "rec"}}],
    }
    registry = repo.parent / "registry.json"
    registry.write_text(json.dumps(data), encoding="utf-8")
    result = _tool(repo, registry)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "pathspec" not in result.stdout + result.stderr
    assert not _unmerged(repo)
    assert not (repo / "rec" / "facts.json").exists()
    assert not (repo / "rec" / "programs.jsonl").exists()
    staged = _git(repo, "diff", "--cached", "--name-status").stdout
    assert "rec/v-4.json" in staged or (repo / "rec" / "v-4.json").exists()
    # every check ran, so the run did not stop at the staging step
    assert "rec check" in result.stdout
    assert _git(repo, "status", "--porcelain").stdout.strip() == "" or \
        not [l for l in _git(repo, "status", "--porcelain").stdout.splitlines()
             if l[1] != " "]
