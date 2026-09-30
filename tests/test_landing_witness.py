"""tools/landing_witness.py: the baseline's notes are re-measured, not trusted.

Issue #1434. The ancestry ratchet checked the one property of a stranded PR
that never changes and took the note beside it on trust. Five of eight notes
went stale while it stayed green, and #1320's named the wrong files.

Every check here is shown twice: passing on a correct entry and FAILING on a
deliberately broken one. A check that cannot fire is the defect this issue is
about, so a test that only shows the green half would prove nothing.

The byte witness is measured at `carried_at`, the commit that brought the
work in, not at HEAD: carrying is a historical fact, and a later edit to a
carried file must cost nobody anything. Both halves of that are tested too.

Most tests drive a SYNTHETIC repository, so they are about the mechanism. The
last group runs against the real baseline and the real merge commits, and
breaks the real entries the way they were actually broken: #1320's wrong
inventory, #1305's stale STRANDED.
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
    carried all of it, one added file already modified.

        P    keep.py v1, tests/test_old.py
        M    (on `feature`) adds src/a.py, src/b.py, tests/test_pin.py;
             modifies keep.py to v2
        C    (on main, from P) the same content, except src/b.py differs:
             it was carried already modified. This is `carried_at`.
    """
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    base = _commit(r, {"keep.py": "v = 1\n",
                       "tests/test_old.py": "def test_x():\n    pass\n"},
                   "base")
    _git(r, "checkout", "-q", "-b", "feature")
    merge = _commit(r, {"src/a.py": "a = 1\n", "src/b.py": "b = 1\n",
                        "tests/test_pin.py": PIN_TESTS,
                        "keep.py": "v = 2\n"}, "the merge (#7)")
    _git(r, "checkout", "-q", "main")
    carried = _commit(r, {"src/a.py": "a = 1\n", "src/b.py": "b = 2\n",
                          "tests/test_pin.py": PIN_TESTS,
                          "keep.py": "v = 2\n"},
                      "carried under another commit (#8)")
    return {"root": r, "merge": merge, "carried": carried, "base": base}


def _good(t: dict) -> dict:
    return {
        "merge": t["merge"],
        "verdict": "CARRIED",
        "carried_at": t["carried"],
        "added": ["src/a.py", "src/b.py", "tests/test_pin.py"],
        "identical": ["src/a.py", "tests/test_pin.py", "keep.py"],
        "drifted": {"src/b.py": ["tests/test_pin.py::test_two"]},
        "note": "merged into agent/feature. CARRIED by #8: src/a.py, "
                "src/b.py and tests/test_pin.py are on main.",
    }


def _audit(t: dict, entry, mode: str = "collect", num: str = "7"):
    return lw.audit({num: entry}, t["root"], lambda sha: True, mode=mode)


def _only_finding(report, *needles: str) -> None:
    assert report.verified == [] and report.unchecked == [], report
    joined = "\n".join(report.findings)
    for needle in needles:
        assert needle in joined, joined


def _verified(report) -> str:
    assert report.findings == [] and report.unresolved == [], report
    assert report.unchecked == [] and len(report.verified) == 1, report
    return report.verified[0]


# ----------------------------------------------------- the correct entry


@pytest.mark.parametrize("mode", ["collect", "run"])
def test_a_true_carried_entry_is_verified(repo, mode):
    line = _verified(_audit(repo, _good(repo), mode=mode))
    assert "3 added file(s) accounted for" in line
    assert "3 byte-identical" in line
    assert "1 carried modified" in line


# ------------------------------- later edits cost nothing (the carried_at rule)


def test_a_later_edit_to_every_carried_file_costs_nothing(repo):
    """Carrying is a historical fact. Once the work was on main, the code
    moving on does not make the claim false, and an unrelated PR must not be
    made to amend a ledger about somebody else's merge."""
    _commit(repo["root"], {"src/a.py": "a = 99\n", "src/b.py": "b = 99\n",
                           "keep.py": "v = 99\n",
                           "tests/test_pin.py": PIN_TESTS + "\n# edited\n"},
            "ordinary later work")
    _verified(_audit(repo, _good(repo)))


def test_a_carried_file_later_removed_is_reported_not_failed(repo):
    (repo["root"] / "src" / "a.py").unlink()
    _git(repo["root"], "commit", "-q", "-am", "remove a")
    line = _verified(_audit(repo, _good(repo)))
    assert "Since REMOVED from the tree (reported, not failed): src/a.py" \
        in line


# ----------------------------------------------------- 1. a wrong path


def test_a_path_the_merge_never_touched_is_refused(repo):
    e = _good(repo)
    e["identical"].append("src/zzz.py")
    _only_finding(_audit(repo, e), "src/zzz.py, which this merge did not "
                                   "touch")


def test_a_carried_at_that_predates_the_carrying_fails(repo):
    """Name the wrong commit and the files are not there to compare. The
    check is pinned to the moment the note describes, so a note wrong about
    that moment still fails."""
    e = _good(repo)
    e["carried_at"] = repo["base"]
    _only_finding(_audit(repo, e), "CARRIED claims src/a.py, and it is "
                                   "ABSENT at carried_at")


def test_a_carried_at_that_is_not_history_of_the_tree_fails(repo):
    r = repo["root"]
    _git(r, "checkout", "-q", "-b", "elsewhere", repo["base"])
    stray = _commit(r, {"src/a.py": "a = 1\n", "src/b.py": "b = 2\n",
                        "tests/test_pin.py": PIN_TESTS, "keep.py": "v = 2\n"},
                    "never reaches main")
    _git(r, "checkout", "-q", "main")
    e = _good(repo)
    e["carried_at"] = stray
    _only_finding(_audit(repo, e), "is not an ancestor of the tree under "
                                   "test")


def test_a_shallow_clone_names_the_missing_history_instead_of_a_verdict(
        repo, tmp_path):
    """The `root-suite-affected` red, in miniature. A FULL clone followed by
    `git fetch --depth=1` of main grafts main's tip, so `carried_at` (older)
    reads as not an ancestor while every object is still present. The
    witness must say the checkout is shallow, not that nothing was carried.
    The control: the same clone, unshallowed, verifies."""
    r = repo["root"]
    _commit(r, {"later.py": "x = 1\n"}, "main moves on past carried_at")
    copy = tmp_path / "copy"
    subprocess.run(["git", "clone", "-q", "--no-local", str(r), str(copy)],
                   check=True, capture_output=True)
    _git(copy, "fetch", "-q", "--no-tags", "--depth=1", "origin",
         "+refs/heads/main:refs/remotes/origin/main")
    assert lw.is_shallow(copy)
    assert subprocess.run(["git", "-C", str(copy), "cat-file", "-e",
                           f"{repo['carried']}^{{commit}}"]).returncode == 0
    t = dict(repo, root=copy)
    report = _audit(t, _good(repo))
    _only_finding(report, "the checkout is shallow")
    assert "is not an ancestor" not in "\n".join(report.findings)

    _git(copy, "fetch", "-q", "--unshallow", "origin")
    assert not lw.is_shallow(copy)
    _verified(_audit(t, _good(repo)))


def test_a_carried_at_that_contains_the_merge_itself_fails(repo):
    """Comparing the merge against a commit that includes it is a tautology.
    (The ancestry audit would also fire; this one says why the witness is
    worthless.)"""
    r = repo["root"]
    _git(r, "merge", "-q", "--no-ff", "-m", "merge the stranded branch",
         "feature", "-X", "theirs")
    e = _good(repo)
    e["carried_at"] = _git(r, "rev-parse", "HEAD")
    _only_finding(_audit(repo, e), "contains the merge")


def test_a_carried_entry_without_carried_at_is_refused(repo):
    e = _good(repo)
    del e["carried_at"]
    _only_finding(_audit(repo, e), "needs `carried_at`")


# ------------------------------------------ 2. a drift with no drift note


def test_a_modified_carry_claimed_identical_fails(repo):
    """The file differed from the merge's copy at the moment it was carried,
    and the entry says nothing about it."""
    e = _good(repo)
    e["identical"].append("src/b.py")
    del e["drifted"]["src/b.py"]
    _only_finding(_audit(repo, e),
                  "src/b.py at carried_at", "DIFFERS", "does not say so")


def test_an_identical_carry_claimed_drifted_fails(repo):
    """Exact in both directions."""
    e = _good(repo)
    e["identical"].remove("src/a.py")
    e["drifted"]["src/a.py"] = ["tests/test_pin.py::test_one"]
    _only_finding(_audit(repo, e), "src/a.py, which at carried_at",
                  "byte-IDENTICAL")


def test_a_drift_with_no_test_is_refused_at_parse_time(repo):
    e = _good(repo)
    e["drifted"]["src/b.py"] = []
    _only_finding(_audit(repo, e), "names no test", "exemption")


# --------------------------------------------- 3. a wrong added set


def test_the_1320_inventory_error_is_caught(repo):
    """#1320's note listed a MODIFIED file as added and left the real added
    file out. Both halves must fire, each naming its file."""
    e = _good(repo)
    e["added"] = ["src/a.py", "tests/test_pin.py", "keep.py"]
    _only_finding(_audit(repo, e),
                  "`added` lists keep.py, which this merge did not add "
                  "(MODIFIED)",
                  "this merge ADDED src/b.py, and `added` leaves it out")


def test_an_added_file_the_witness_skips_fails(repo):
    """The inventory can be right and the CARRIED claim still silent about
    one of the added files."""
    e = _good(repo)
    del e["drifted"]["src/b.py"]
    _only_finding(_audit(repo, e), "ADDED src/b.py and the witness neither")


# ------------------------------------ 4. a named test that does not exist


def test_a_pin_the_merge_never_had_fails(repo):
    e = _good(repo)
    e["drifted"]["src/b.py"] = ["tests/test_pin.py::test_nope"]
    _only_finding(_audit(repo, e), "does not exist in",
                  "not one of the merge's own tests")


def test_a_pin_from_a_file_the_merge_did_not_touch_fails(repo):
    """A drift may not be pinned on somebody else's test."""
    e = _good(repo)
    e["drifted"]["src/b.py"] = ["tests/test_old.py::test_x"]
    _only_finding(_audit(repo, e), "did not touch tests/test_old.py")


def test_a_pin_the_merge_had_but_the_tree_lost_fails(repo):
    """The merge's copy is not enough: the test has to collect NOW."""
    (repo["root"] / "tests" / "test_pin.py").write_text(
        "def test_one():\n    assert True\n", encoding="utf-8")
    _only_finding(_audit(repo, _good(repo)),
                  "pins tests/test_pin.py::test_two, which collects no test")


def test_a_whole_file_pin_needs_the_merge_copy_at_carried_at(repo):
    """A whole-file pin says every test in it was the merge's own, which is
    only true if that file was the merge's copy when it was carried."""
    e = _good(repo)
    e["drifted"]["src/b.py"] = ["tests/test_pin.py"]
    _verified(_audit(repo, e))
    r = repo["root"]
    later = _commit(r, {"tests/test_pin.py": PIN_TESTS
                        + "\n\ndef test_three():\n    pass\n"},
                    "a test file carried with an extra test")
    e["carried_at"] = later
    e["identical"].remove("tests/test_pin.py")
    e["drifted"]["tests/test_pin.py"] = ["tests/test_pin.py::test_one"]
    _only_finding(_audit(repo, e), "pins the whole of tests/test_pin.py, "
                                   "which was not the merge's copy")


@pytest.mark.parametrize("body, word", [
    ("def test_two():\n    assert False\n", "failure"),
    ("import pytest\n\n\n@pytest.mark.skip(reason='x')\n"
     "def test_two():\n    pass\n", "skipped"),
])
def test_run_mode_fails_a_pin_that_does_not_pass(repo, body, word):
    """Collecting proves a test exists; `--pinned run` proves it passes. A
    skip is not a pass. The pins run in the working tree, so this is the
    one part of a CARRIED witness that reads the present."""
    (repo["root"] / "tests" / "test_pin.py").write_text(
        "def test_one():\n    assert True\n\n\n" + body, encoding="utf-8")
    e = _good(repo)
    _verified(_audit(repo, e, mode="collect"))
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
    main the whole time. A stranding is a claim about NOW, so this one is
    measured in the tree under test."""
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
    e = _good(repo)
    e["note"] = "merged into agent/feature. STRANDED: src/a.py is absent."
    _only_finding(_audit(repo, e), "leads with STRANDED, the entry says "
                                   "CARRIED")


def test_the_note_may_not_name_a_path_the_witness_skips(repo):
    """Parsing the paths the note names is what stops the prose claiming a
    file the checker never looks at. A bare file name counts."""
    e = _good(repo)
    e["identical"].remove("keep.py")
    _verified(_audit(repo, e))
    e["note"] += " keep.py came across too."
    _only_finding(_audit(repo, e), "the note names keep.py")


def test_a_bare_prose_entry_is_refused(repo):
    r = _audit(repo, "merged into agent/feature. CARRIED by #8, trust me.")
    _only_finding(r, "bare prose")


def test_a_field_the_verdict_does_not_use_is_refused(repo):
    e = _good(repo)
    e["pinned_by"] = ["tests/test_pin.py::test_one"]
    _only_finding(_audit(repo, e), "does not carry ['pinned_by']")


def test_an_unresolvable_commit_is_unresolved_not_verified(repo):
    for missing in (repo["merge"], repo["carried"]):
        r = lw.audit({"7": _good(repo)}, repo["root"],
                     lambda sha, gone=missing: sha != gone)
        assert r.verified == [] and r.findings == [] and r.unchecked == []
        assert len(r.unresolved) == 1 and "NOT checked" in r.unresolved[0]
        assert missing[:12] in r.unresolved[0]


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
    base.write_text(json.dumps({"unreachable": {"7": _good(repo)}}),
                    encoding="utf-8")
    assert chk.main(argv) == 0
    assert "verified    #7 CARRIED" in capsys.readouterr().out
    broken = _good(repo)
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
    chk.load_witness_module().audit({"7": _good(repo)},
                                    repo["root"], lambda sha: True)
    assert os.getcwd() == cwd and sys.path == path and dict(os.environ) == env
    assert set(sys.modules) == set(modules)
    assert all(sys.modules[k] is v for k, v in modules.items())


# ------------------------------------------- the real baseline, for real

_REAL = json.loads(BASELINE.read_text(encoding="utf-8"))["unreachable"]


def _history_problem() -> str | None:
    """Why this clone cannot answer the real baseline's questions, or None.

    Both halves matter. A clone can hold every object and still be shallow:
    `git fetch --depth=1` into a full clone grafts the fetched tip, and every
    commit older than it then reads as "not an ancestor". That is how the
    `root-suite-affected` job, which checks out with `fetch-depth: 0`, once
    reported six real entries as carried by nothing."""
    if lw.is_shallow(ROOT):
        return lw.SHALLOW
    shas = [e.get(k) for e in _REAL.values() if isinstance(e, dict)
            for k in ("merge", "carried_at")]
    missing = [s for s in shas if s and not chk.object_exists(ROOT, s)]
    if missing:
        return ("this test needs full git history; the clone lacks "
                + ", ".join(s[:12] for s in missing))
    return None


@pytest.fixture
def full_history():
    """FAIL, by name, where the history is missing. Not a skip: a skip reads
    as a pass, and the thing these tests check is exactly what a clone
    without history cannot see. Every CI job that runs the root suite checks
    out with `fetch-depth: 0` for this reason."""
    problem = _history_problem()
    if problem is not None:
        pytest.fail(problem, pytrace=False)


real = pytest.mark.usefixtures("full_history")


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
def test_the_real_1320_modified_carry_cannot_be_hidden(real_pins):
    """`src/revl/lower.py` was carried already modified. Recording it as
    identical must fail."""
    raw = copy.deepcopy(_REAL["1320"])
    del raw["drifted"]["src/revl/lower.py"]
    raw["identical"].append("src/revl/lower.py")
    problems, _ = _check_real("1320", raw, real_pins)
    assert any("src/revl/lower.py at carried_at" in p and "DIFFERS" in p
               for p in problems), problems


@real
def test_the_real_pins_cannot_name_a_missing_test(real_pins):
    raw = copy.deepcopy(_REAL["1305"])
    raw["drifted"]["src/revl/model_council.py"] = [
        "tests/test_model_council_516.py::test_this_test_does_not_exist"]
    problems, _ = _check_real("1305", raw, real_pins)
    assert any("does not exist in" in p for p in problems), problems


@real
def test_a_real_carried_at_before_the_carrying_fails(real_pins):
    """#1305's work arrived in e35a18a9. Its parent predates that, so a note
    naming it is wrong about the moment it describes."""
    raw = copy.deepcopy(_REAL["1305"])
    raw["carried_at"] = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", raw["carried_at"] + "^1"],
        check=True, capture_output=True, text=True).stdout.strip()
    problems, _ = _check_real("1305", raw, real_pins)
    assert any("src/revl/pool_receipt.py, and it is ABSENT at carried_at"
               in p for p in problems), problems


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


# ------------------------------------------- after a history rewrite
#
# The baseline's `merge` is a copy of GitHub's merge commit id and its
# `carried_at` names a commit on main, so both were written before a rewrite
# and name commits a rewritten clone does not have. With the rewrite map
# present they are translated like GitHub's own ids.


def _rewrite_everything(r: Path) -> dict[str, str]:
    """Replay every commit on every branch with a new author, as a rewrite
    does. Returns old id -> new id."""
    _git(r, "config", "user.name", "rewritten")
    mapping: dict[str, str] = {}
    for old in _git(r, "rev-list", "--reverse", "--topo-order",
                    "--branches").splitlines():
        tree = _git(r, "rev-parse", f"{old}^{{tree}}")
        msg = _git(r, "log", "-1", "--format=%B", old)
        args = ["commit-tree", tree, "-m", msg]
        for parent in _git(r, "log", "-1", "--format=%P", old).split():
            args += ["-p", mapping[parent]]
        mapping[old] = _git(r, *args)
    for ref in _git(r, "for-each-ref", "--format=%(refname)",
                    "refs/heads").splitlines():
        _git(r, "update-ref", ref, mapping[_git(r, "rev-parse", ref)])
    _git(r, "reset", "-q", "--hard")
    return mapping


@pytest.fixture
def rewritten(repo, tmp_path) -> dict:
    """`repo` rewritten and cloned: the clone holds only the new ids, and
    the baseline entry still records the old ones, as the real file will."""
    mapping = _rewrite_everything(repo["root"])
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--no-local", str(repo["root"]),
                    str(clone)], check=True, capture_output=True)
    for old in (repo["merge"], repo["carried"]):
        assert mapping[old] != old
        assert not chk.object_exists(clone, old)
    tsv = tmp_path / "history_rewrite_map.tsv"
    tsv.write_text("".join(f"{o}\t{n}\n" for o, n in mapping.items()),
                   encoding="utf-8")
    base = tmp_path / "baseline.json"
    base.write_text(json.dumps({"unreachable": {"7": _good(repo)}}),
                    encoding="utf-8")
    prs = tmp_path / "prs.json"
    prs.write_text(json.dumps([{"number": 7, "title": "the merge",
                                "baseRefName": "feature",
                                "mergeCommit": {"oid": repo["merge"]}}]),
                   encoding="utf-8")
    return {"clone": clone, "map": tsv, "baseline": base, "prs": prs,
            "mapping": mapping, **repo}


def test_the_witness_is_measured_through_the_rewrite_map(rewritten, capsys):
    t = rewritten
    argv = ["--witness-only", "--no-fetch", "--root", str(t["clone"]),
            "--baseline", str(t["baseline"])]
    # without the map the recorded ids are gone: unanswered, not a pass
    assert chk.main(argv + ["--rewrite-map", str(t["map"]) + ".absent"]) == 2
    assert "UNRESOLVED  #7" in capsys.readouterr().out
    assert chk.main(argv + ["--rewrite-map", str(t["map"])]) == 0
    out = capsys.readouterr().out
    assert "verified    #7 CARRIED" in out
    assert "translated 2 recorded baseline commit id(s)" in out


def test_the_full_run_translates_github_and_the_baseline_alike(rewritten,
                                                              capsys):
    """GitHub's id and the baseline's copy of it are both pre-rewrite. Both
    are translated, so the ancestry audit reads the entry as known, the
    MERGE SHA cross-check agrees, and the witness verifies."""
    t = rewritten
    argv = ["--from-json", str(t["prs"]), "--no-fetch",
            "--root", str(t["clone"]), "--main-ref", "origin/main",
            "--baseline", str(t["baseline"])]
    assert chk.main(argv + ["--rewrite-map", str(t["map"]) + ".absent"]) == 2
    capsys.readouterr()
    assert chk.main(argv + ["--rewrite-map", str(t["map"])]) == 0
    out = capsys.readouterr().out
    assert "known       #7:" in out and "MERGE SHA" not in out
    assert "translated 1 GitHub merge commit id(s)" in out
    assert "verified    #7 CARRIED" in out


def test_the_merge_sha_cross_check_compares_translated_ids():
    old, new, other = "a" * 40, "b" * 40, "c" * 40
    rewrite = chk.RewriteMap({old: new}, Path("m.tsv"))
    entries, changed = chk.translate_entries({"7": {"merge": old}}, rewrite)
    assert entries == {"7": {"merge": new}} and changed == 1
    github = [{"number": 7, "mergeCommit": {"oid": old}}]
    assert chk.merge_sha_mismatches(github, entries, rewrite) == []
    # translating only one side is the defect this guards against
    assert len(chk.merge_sha_mismatches(github, entries)) == 1
    # and a real disagreement survives translation
    wrong = [{"number": 7, "mergeCommit": {"oid": other}}]
    out = chk.merge_sha_mismatches(wrong, entries, rewrite)
    assert len(out) == 1 and "wrong commit" in out[0]


def test_translate_entries_leaves_what_it_cannot_read_alone():
    rewrite = chk.RewriteMap({"a" * 40: "b" * 40}, Path("m.tsv"))
    raw = {"1": "bare prose", "2": {"merge": 5, "carried_at": "a" * 40},
           "3": {"verdict": "REWORKED"}}
    out, changed = chk.translate_entries(raw, rewrite)
    assert out == {"1": "bare prose", "2": {"merge": 5, "carried_at": "b" * 40},
                   "3": {"verdict": "REWORKED"}}
    assert changed == 1
    assert raw["2"]["carried_at"] == "a" * 40  # the input is not mutated
