"""tools/landing_witness.py: the baseline's notes are re-measured, not trusted.

Issue #1434. The ancestry ratchet checked the one property of a stranded PR
that never changes and took the note beside it on trust. Five of eight notes
went stale while it stayed green, and #1320's named the wrong files.

Every check here is shown twice: passing on a correct entry and FAILING on a
deliberately broken one. A check that cannot fire is the defect this issue is
about, so a test that only shows the green half would prove nothing.

Most tests drive a SYNTHETIC repository, so they are about the mechanism. The
last group runs against the real baseline and the real merge commits, and
breaks the real entries the way they were actually broken: #1320's wrong
inventory, #1305's stale STRANDED, #1296's drifted file.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lw = _load("revl_test_landing_witness", ROOT / "tools" / "landing_witness.py")
chk = _load("revl_test_check_merged_prs_w",
            ROOT / "tools" / "check_merged_prs_landed.py")
BASELINE = ROOT / "tools" / "merged_pr_landing_baseline.json"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _write(repo: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(body, encoding="utf-8")


def _commit(repo: Path, files: dict[str, str], msg: str) -> str:
    _write(repo, files)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


PIN_TESTS = (
    "def test_one():\n    assert True\n\n\n"
    "def test_two():\n    assert True\n")


@pytest.fixture
def repo(tmp_path) -> dict:
    """A merge that ADDED three files and MODIFIED one, and a main that
    carries all of it, with one added file changed since.

        P    keep.py v1, tests/test_old.py
        M    (on `feature`) adds src/a.py, src/b.py, tests/test_pin.py;
             modifies keep.py to v2
        C    (on main) the same content, except src/b.py has drifted
    """
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    _commit(r, {"keep.py": "v = 1\n",
                "tests/test_old.py": "def test_x():\n    pass\n"}, "base")
    _git(r, "checkout", "-q", "-b", "feature")
    merge = _commit(r, {"src/a.py": "a = 1\n", "src/b.py": "b = 1\n",
                        "tests/test_pin.py": PIN_TESTS,
                        "keep.py": "v = 2\n"}, "the merge (#7)")
    _git(r, "checkout", "-q", "main")
    _commit(r, {"src/a.py": "a = 1\n", "src/b.py": "b = 2  # drifted\n",
                "tests/test_pin.py": PIN_TESTS, "keep.py": "v = 2\n"},
            "carried under another commit (#8)")
    return {"root": r, "merge": merge}


def _good(merge: str) -> dict:
    return {
        "merge": merge,
        "verdict": "CARRIED",
        "added": ["src/a.py", "src/b.py", "tests/test_pin.py"],
        "identical": ["src/a.py", "tests/test_pin.py", "keep.py"],
        "drifted": {"src/b.py": ["tests/test_pin.py::test_two"]},
        "note": "merged into agent/feature. CARRIED by #8: src/a.py, "
                "src/b.py and tests/test_pin.py are on main.",
    }


def _audit(t: dict, entry: dict, mode: str = "collect", num: str = "7"):
    return lw.audit({num: entry}, t["root"], lambda sha: True, mode=mode)


def _only_finding(report, *needles: str) -> None:
    assert report.verified == [] and report.unchecked == [], report
    joined = "\n".join(report.findings)
    for needle in needles:
        assert needle in joined, joined


# ----------------------------------------------------- the correct entry


@pytest.mark.parametrize("mode", ["collect", "run"])
def test_a_true_carried_entry_is_verified(repo, mode):
    r = _audit(repo, _good(repo["merge"]), mode=mode)
    assert r.findings == [] and r.unresolved == [] and r.unchecked == [], r
    assert len(r.verified) == 1
    assert "3 added file(s) accounted for" in r.verified[0]
    assert "3 byte-identical" in r.verified[0]
    assert "1 drifted" in r.verified[0]


# ----------------------------------------------------- 1. a wrong path


def test_a_path_the_merge_never_touched_is_refused(repo):
    e = _good(repo["merge"])
    e["identical"].append("src/zzz.py")
    _only_finding(_audit(repo, e), "src/zzz.py, which this merge did not "
                                   "touch")


def test_a_carried_path_that_is_absent_fails(repo):
    (repo["root"] / "src" / "a.py").unlink()
    _only_finding(_audit(repo, _good(repo["merge"])),
                  "CARRIED claims src/a.py, and it is ABSENT")


# ------------------------------------------ 2. a drift with no drift note


def test_a_drifted_file_claimed_identical_fails(repo):
    """The #1296 shape: a file that has moved since, recorded as if it had
    not. Silence about the drift is the defect."""
    e = _good(repo["merge"])
    e["identical"].append("src/b.py")
    del e["drifted"]["src/b.py"]
    _only_finding(_audit(repo, e),
                  "src/b.py has DRIFTED", "does not say so")


def test_an_identical_file_claimed_drifted_fails(repo):
    """Exact in both directions: a drift claim for an unmoved file is a
    stale record too."""
    e = _good(repo["merge"])
    e["identical"].remove("src/a.py")
    e["drifted"]["src/a.py"] = ["tests/test_pin.py::test_one"]
    _only_finding(_audit(repo, e), "src/a.py, which is byte-IDENTICAL")


def test_a_drift_with_no_test_is_refused_at_parse_time(repo):
    e = _good(repo["merge"])
    e["drifted"]["src/b.py"] = []
    _only_finding(_audit(repo, e), "names no test", "exemption")


# --------------------------------------------- 3. a wrong added set


def test_the_1320_inventory_error_is_caught(repo):
    """#1320's note listed a MODIFIED file as added and left the real added
    file out. Both halves must fire, each naming its file."""
    e = _good(repo["merge"])
    e["added"] = ["src/a.py", "tests/test_pin.py", "keep.py"]
    _only_finding(_audit(repo, e),
                  "`added` lists keep.py, which this merge did not add "
                  "(MODIFIED)",
                  "this merge ADDED src/b.py, and `added` leaves it out")


def test_an_added_file_the_witness_skips_fails(repo):
    """The inventory can be right and the CARRIED claim still silent about
    one of the added files."""
    e = _good(repo["merge"])
    del e["drifted"]["src/b.py"]
    _only_finding(_audit(repo, e), "ADDED src/b.py and the witness neither")


# ------------------------------------ 4. a named test that does not exist


def test_a_pin_the_merge_never_had_fails(repo):
    e = _good(repo["merge"])
    e["drifted"]["src/b.py"] = ["tests/test_pin.py::test_nope"]
    _only_finding(_audit(repo, e), "does not exist in",
                  "not one of the merge's own tests")


def test_a_pin_from_a_file_the_merge_did_not_touch_fails(repo):
    """A drift may not be pinned on somebody else's test."""
    e = _good(repo["merge"])
    e["drifted"]["src/b.py"] = ["tests/test_old.py::test_x"]
    _only_finding(_audit(repo, e), "did not touch tests/test_old.py")


def test_a_pin_the_merge_had_but_the_tree_lost_fails(repo):
    """The merge's copy is not enough: the test has to collect NOW."""
    root = repo["root"]
    (root / "tests" / "test_pin.py").write_text(
        "def test_one():\n    assert True\n", encoding="utf-8")
    e = _good(repo["merge"])
    e["identical"].remove("tests/test_pin.py")
    e["drifted"]["tests/test_pin.py"] = ["tests/test_pin.py::test_one"]
    e["drifted"]["src/b.py"] = ["tests/test_pin.py::test_two"]
    _only_finding(_audit(repo, e),
                  "pins tests/test_pin.py::test_two, which collects no test")


def test_a_whole_file_pin_needs_the_file_unchanged(repo):
    """A whole-file pin says every test in it is the merge's own, which is
    only true while the file is the merge's copy."""
    root = repo["root"]
    e = _good(repo["merge"])
    e["drifted"]["src/b.py"] = ["tests/test_pin.py"]
    assert _audit(repo, e).findings == []
    (root / "tests" / "test_pin.py").write_text(
        PIN_TESTS + "\n\ndef test_three():\n    pass\n", encoding="utf-8")
    e["identical"].remove("tests/test_pin.py")
    e["drifted"]["tests/test_pin.py"] = ["tests/test_pin.py::test_one"]
    _only_finding(_audit(repo, e), "pins the whole of tests/test_pin.py")


@pytest.mark.parametrize("body, word", [
    ("def test_two():\n    assert False\n", "failure"),
    ("import pytest\n\n\n@pytest.mark.skip(reason='x')\n"
     "def test_two():\n    pass\n", "skipped"),
])
def test_run_mode_fails_a_pin_that_does_not_pass(repo, body, word):
    """Collecting proves a test exists; `--pinned run` proves it passes. A
    skip is not a pass."""
    root = repo["root"]
    (root / "tests" / "test_pin.py").write_text(
        "def test_one():\n    assert True\n\n\n" + body, encoding="utf-8")
    e = _good(repo["merge"])
    e["identical"].remove("tests/test_pin.py")
    e["drifted"]["tests/test_pin.py"] = ["tests/test_pin.py::test_one"]
    assert _audit(repo, e, mode="collect").findings == []
    _only_finding(_audit(repo, e, mode="run"), "did not pass", word)


# ------------------------------------------ what it can and cannot check


def test_a_reworked_entry_is_unchecked_not_verified(repo):
    e = {"merge": repo["merge"], "verdict": "REWORKED",
         "added": ["src/a.py", "src/b.py", "tests/test_pin.py"],
         "pinned_by": ["tests/test_pin.py::test_one"],
         "note": "merged into agent/feature. REWORKED: the outcome landed "
                 "under another name in #8."}
    r = _audit(repo, e)
    assert r.findings == [] and r.verified == [], r
    assert len(r.unchecked) == 1 and "NOT verified" in r.unchecked[0]
    e["pinned_by"] = ["tests/test_pin.py::test_nope"]
    _only_finding(_audit(repo, e), "test_nope, which collects no test")


def test_a_stranded_entry_whose_files_landed_fails(repo):
    """The #1305 and #1320 shape: STRANDED, files absent, and they were on
    main the whole time."""
    e = {"merge": repo["merge"], "verdict": "STRANDED",
         "added": ["src/a.py", "src/b.py", "tests/test_pin.py"],
         "absent": ["src/a.py", "src/b.py", "tests/test_pin.py"],
         "note": "merged into agent/feature. STRANDED: none of src/a.py, "
                 "src/b.py or tests/test_pin.py is on main."}
    _only_finding(_audit(repo, e),
                  "STRANDED claims src/a.py is absent, and it is PRESENT, "
                  "byte-identical")
    for rel in e["absent"]:
        (repo["root"] / rel).unlink()
    r = _audit(repo, e)
    assert r.findings == [] and len(r.verified) == 1, r


def test_the_note_must_lead_with_the_recorded_verdict(repo):
    e = _good(repo["merge"])
    e["note"] = "merged into agent/feature. STRANDED: src/a.py is absent."
    _only_finding(_audit(repo, e), "leads with STRANDED, the entry says "
                                   "CARRIED")


def test_the_note_may_not_name_a_path_the_witness_skips(repo):
    """Parsing the paths the note names is what stops the prose claiming a
    file the checker never looks at. A bare file name counts."""
    e = _good(repo["merge"])
    e["identical"].remove("keep.py")
    assert _audit(repo, e).findings == []
    e["note"] += " keep.py came across too."
    _only_finding(_audit(repo, e), "the note names keep.py")


def test_a_bare_prose_entry_is_refused(repo):
    r = _audit(repo, "merged into agent/feature. CARRIED by #8, trust me.")
    _only_finding(r, "bare prose")


def test_a_field_the_verdict_does_not_use_is_refused(repo):
    e = _good(repo["merge"])
    e["pinned_by"] = ["tests/test_pin.py::test_one"]
    _only_finding(_audit(repo, e), "does not carry ['pinned_by']")


def test_an_unresolvable_merge_is_unresolved_not_verified(repo):
    r = lw.audit({"7": _good(repo["merge"])}, repo["root"],
                 lambda sha: False)
    assert r.verified == [] and r.findings == [] and r.unchecked == []
    assert len(r.unresolved) == 1 and "NOT checked" in r.unresolved[0]


def test_a_recorded_merge_sha_github_disagrees_with_is_a_finding():
    entries = {"7": {"merge": "a" * 40}}
    agree = [{"number": 7, "mergeCommit": {"oid": "a" * 40}}]
    differ = [{"number": 7, "mergeCommit": {"oid": "b" * 40}}]
    assert chk.merge_sha_mismatches(agree, entries) == []
    out = chk.merge_sha_mismatches(differ, entries)
    assert len(out) == 1 and "wrong commit" in out[0]


def test_witness_only_exit_status_follows_the_witness(repo, tmp_path,
                                                      capsys):
    base = tmp_path / "baseline.json"
    argv = ["--witness-only", "--no-fetch", "--root", str(repo["root"]),
            "--baseline", str(base)]
    base.write_text(json.dumps({"unreachable": {"7": _good(repo["merge"])}}),
                    encoding="utf-8")
    assert chk.main(argv) == 0
    assert "verified    #7 CARRIED" in capsys.readouterr().out
    broken = _good(repo["merge"])
    broken["added"].remove("src/b.py")
    base.write_text(json.dumps({"unreachable": {"7": broken}}),
                    encoding="utf-8")
    assert chk.main(argv) == 1
    assert "WITNESS     #7 CARRIED" in capsys.readouterr().out


def test_loading_and_running_the_witness_leaves_process_state_alone(repo):
    """The checker loads its sibling by path and shells out to pytest. None
    of that may leak into the process that called it."""
    modules = dict(sys.modules)
    path = list(sys.path)
    cwd = os.getcwd()
    env = dict(os.environ)
    chk.load_witness_module().audit({"7": _good(repo["merge"])},
                                    repo["root"], lambda sha: True)
    assert os.getcwd() == cwd and sys.path == path and dict(os.environ) == env
    assert set(sys.modules) == set(modules)
    assert all(sys.modules[k] is v for k, v in modules.items())


# ------------------------------------------- the real baseline, for real

_REAL = json.loads(BASELINE.read_text(encoding="utf-8"))["unreachable"]


def _have_merges() -> bool:
    return all(chk.object_exists(ROOT, e["merge"]) for e in _REAL.values()
               if isinstance(e, dict) and "merge" in e)


# Skipped only in a clone that does not hold the merge commits, which is
# every shallow checkout. The `merged-prs-landed` CI job has full history and
# runs the same re-measurement with the pinned tests executed, so this skip
# never stands in for that job.
real = pytest.mark.skipif(
    not _have_merges(),
    reason="this clone lacks the baselined merge commits (shallow checkout); "
           "the merged-prs-landed CI job measures the real baseline")


def test_every_real_entry_parses():
    for num, raw in _REAL.items():
        entry, errors = lw.parse_entry(num, raw)
        assert errors == [] and entry is not None, errors


@pytest.fixture(scope="module")
def real_pins():
    entries = [lw.parse_entry(n, raw)[0] for n, raw in _REAL.items()]
    pins = tuple(dict.fromkeys(p for e in entries for p in e.pins()))
    return lw.measure_pins(ROOT, pins, "collect", sys.executable)


def _check_real(num: str, raw: dict, pins) -> tuple[list[str], bool]:
    entry, errors = lw.parse_entry(num, raw)
    assert errors == [], errors
    problems, _, checked = lw.check_entry(
        entry, ROOT, lw.merge_changes(ROOT, entry.merge), pins)
    return problems, checked


@real
def test_the_real_baseline_says_which_entries_it_can_check(real_pins):
    verified, unchecked = set(), set()
    for num, raw in _REAL.items():
        problems, checked = _check_real(num, raw, real_pins)
        assert problems == [], (num, problems)
        (verified if checked else unchecked).add(num)
    assert unchecked == {n for n, e in _REAL.items()
                         if e["verdict"] == "REWORKED"}
    assert verified == set(_REAL) - unchecked
    assert unchecked == {"1308", "1318"}


@real
def test_the_real_1320_inventory_error_is_caught(real_pins):
    """Put back exactly what #1320's old note said: the taint-classes test
    module counted as added, and the ladder-rungs module missing."""
    raw = copy.deepcopy(_REAL["1320"])
    raw["added"].remove("tests/test_ui_ladder_rungs_521.py")
    raw["added"].append("tests/test_ui_taint_classes_521.py")
    problems, _ = _check_real("1320", raw, real_pins)
    joined = "\n".join(problems)
    assert ("lists tests/test_ui_taint_classes_521.py, which this merge did "
            "not add (MODIFIED)") in joined
    assert ("ADDED tests/test_ui_ladder_rungs_521.py, and `added` leaves it "
            "out") in joined


@real
def test_the_real_1305_stale_stranded_claim_is_caught(real_pins):
    raw = copy.deepcopy(_REAL["1305"])
    raw = {"merge": raw["merge"], "verdict": "STRANDED",
           "added": raw["added"], "absent": list(raw["added"]),
           "note": "merged into agent/1198-private-peer-pool. STRANDED: its "
                   "files are absent from main."}
    problems, _ = _check_real("1305", raw, real_pins)
    assert any("src/revl/pool_receipt.py is absent, and it is PRESENT, "
               "byte-identical" in p for p in problems), problems


@real
def test_the_real_1296_drift_cannot_be_hidden(real_pins):
    raw = copy.deepcopy(_REAL["1296"])
    del raw["drifted"]["src/revl/erase_report.py"]
    raw["identical"].append("src/revl/erase_report.py")
    problems, _ = _check_real("1296", raw, real_pins)
    assert any("src/revl/erase_report.py has DRIFTED" in p
               for p in problems), problems


@real
def test_the_real_pins_cannot_name_a_missing_test(real_pins):
    raw = copy.deepcopy(_REAL["1296"])
    raw["drifted"]["src/revl/erase_report.py"] = [
        "tests/test_erase_report.py::test_this_test_does_not_exist"]
    problems, _ = _check_real("1296", raw, real_pins)
    assert any("does not exist in" in p for p in problems), problems


@real
def test_a_wrong_real_path_is_caught(real_pins):
    raw = copy.deepcopy(_REAL["1319"])
    raw["identical"] = ["tests/test_ruleset_module_completeness.py"]
    problems, _ = _check_real("1319", raw, real_pins)
    joined = "\n".join(problems)
    assert "which this merge did not touch" in joined, joined
    assert "the witness neither" in joined, joined


@real
def test_a_real_merge_era_test_deleted_since_is_caught():
    """`test_no_new_guarantee_code_is_registered` was in #1305's merge and is
    gone from the tree. It passes the merge-copy half of the rule, so only
    the collection half can catch it; the control beside it is a merge-era
    test that still exists, measured the same way."""
    gone = ("tests/test_model_council_516.py::"
            "test_no_new_guarantee_code_is_registered")
    kept = "tests/test_model_council_516.py::test_the_flagship_council_compiles"
    pins = lw.measure_pins(ROOT, (gone, kept), "collect", sys.executable)
    for pin, expect_problem in ((gone, True), (kept, False)):
        raw = copy.deepcopy(_REAL["1305"])
        raw["drifted"]["src/revl/model_council.py"] = [pin]
        raw["drifted"]["docs/design/543-model-council.md"] = [kept]
        raw["drifted"]["tests/test_model_council_516.py"] = [kept]
        problems, _ = _check_real("1305", raw, pins)
        assert bool(problems) is expect_problem, (pin, problems)
        if expect_problem:
            assert any("collects no test" in p for p in problems), problems
