"""The offline tracker snapshot (tools/tracker_snapshot.py, issue #2761 slice 6).

The evolution curriculum reads open issues from a committed file so that it is
computable in a checkout with no network. What is worth testing is the file's
shape, that the extractor keeps only paths and never prose, and that the offline
check fires on each way the file can be wrong. `--write` needs `gh` and the
network, so it is exercised through `snapshot_of` on issues this test supplies.
"""

import json
import subprocess
import sys
from pathlib import Path

from _load_by_path import load_by_path

ROOT = Path(__file__).resolve().parents[1]


def _tool():
    return load_by_path("tracker_snapshot", ROOT / "tools" / "tracker_snapshot.py")


def test_the_committed_snapshot_is_well_formed():
    tool = _tool()
    data = json.loads(tool.SNAPSHOT.read_text(encoding="utf-8"))
    assert tool.problems(data) == []
    assert data["issues"], "an empty snapshot would make the tracker source vacuous"


def test_the_check_cli_exits_zero_on_the_committed_file_and_one_on_a_bad_one(
        tmp_path):
    tool = ROOT / "tools" / "tracker_snapshot.py"
    green = subprocess.run([sys.executable, str(tool), "--check"],
                           capture_output=True, text=True, cwd=ROOT)
    assert green.returncode == 0, green.stderr[-1000:]
    bad = tmp_path / "bad.json"
    bad.write_text('{"issues": [{"number": 1}]}', encoding="utf-8")
    red = subprocess.run([sys.executable, str(tool), "--check", "--file", str(bad)],
                         capture_output=True, text=True, cwd=ROOT)
    assert red.returncode == 1
    assert "TRACKER-SNAPSHOT" in red.stderr


def test_the_extractor_keeps_verifier_paths_and_never_the_body():
    tool = _tool()
    body = ("See tests/test_selfhost_lower.py and tools/evolve_curriculum.py, "
            "then tests/test_selfhost_lower.py again. A path in a URL "
            "https://x/y/tests/test_url.py and src/revl/lower.py are not "
            "verifiers. SECRET PROSE.")
    entry = tool.entry({"number": 7, "title": "  a   title ",
                        "labels": [{"name": "p2"}, {"name": "bug"}],
                        "body": body})
    assert entry == {"number": 7, "title": "a title", "labels": ["bug", "p2"],
                     "verifiers": ["tests/test_selfhost_lower.py",
                                   "tools/evolve_curriculum.py"]}
    assert "SECRET" not in json.dumps(entry)


def test_snapshot_of_orders_by_number_and_records_the_repo():
    tool = _tool()
    data = tool.snapshot_of([
        {"number": 9, "title": "z", "labels": [], "body": ""},
        {"number": 3, "title": "a", "labels": [], "body": None}])
    assert [i["number"] for i in data["issues"]] == [3, 9]
    assert data["repo"] == tool.REPO
    assert tool.problems(data) == []


def test_the_offline_check_fires_on_each_way_the_file_is_wrong():
    tool = _tool()
    ok = {"number": 1, "title": "t", "labels": [], "verifiers": []}
    assert tool.problems({"issues": [ok]}) == []
    assert tool.problems([]) == ["the snapshot has no `issues` list"]
    assert tool.problems({"issues": [ok, ok]}) and \
        "twice" in tool.problems({"issues": [ok, ok]})[0]
    assert any("out of order" in p for p in tool.problems(
        {"issues": [{**ok, "number": 5}, {**ok, "number": 2}]}))
    assert any("no title" in p for p in tool.problems(
        {"issues": [{**ok, "title": ""}]}))
    assert any("verifier" in p for p in tool.problems(
        {"issues": [{**ok, "verifiers": ["src/revl/lower.py"]}]}))
    assert any("not a list of strings" in p for p in tool.problems(
        {"issues": [{**ok, "labels": "p2"}]}))
