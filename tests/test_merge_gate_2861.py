"""tools/merge_gate.py: a pull request with a red advisory shard cannot land.

Issue #2861. The four `root-suite-affected` shards are not required contexts,
so #2620 merged with `root-suite-affected (4)` red while every required
context was green. The rule now lives at the merge boundary, in the tool the
merge goes through, and these tests drive that tool end to end against a
SIMULATED pull request: the fake `gh` below answers `pr view`, the branch tip
and the workflow run record, and records every call, so "the merge was
refused" is asserted as "`gh pr merge` was never called", not as a message.

The negative controls matter as much as the refusals: a skipped job, a
superseded red run and an all-green rollup must still merge, or the gate would
be a wall rather than a rule.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "revl_merge_gate", ROOT / "tools" / "merge_gate.py")
mg = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = mg
_SPEC.loader.exec_module(mg)

HEAD = "a" * 40
TIP = "b" * 40
OLD_TIP = "c" * 40
REQUIRED = ["lint", "backend-python", "backend-typescript", "backend-wasm",
            "backend-rust", "backend-java", "backend-go"]
SHARDS = [f"root-suite-affected ({k})" for k in (1, 2, 3, 4)]


def node(name, conclusion="SUCCESS", run=100, status="COMPLETED",
         started="2026-11-01T10:00:00Z", workflow="ci"):
    return {
        "__typename": "CheckRun", "name": name, "workflowName": workflow,
        "status": status, "conclusion": conclusion if status == "COMPLETED" else "",
        "startedAt": started, "completedAt": started,
        "detailsUrl": f"https://github.com/inso1337/revl/actions/runs/{run}/job/1",
    }


def green_rollup():
    return [node(n) for n in REQUIRED + SHARDS + ["changes", "held-out"]]


class FakeGh:
    """The three reads the gate makes, plus a log of every call."""

    def __init__(self, rollup, tip=TIP, run_bases=None, head=HEAD,
                 state="OPEN", moved_to=None):
        self.rollup, self.tip, self.head, self.state = rollup, tip, head, state
        self.run_bases = run_bases if run_bases is not None else {}
        self.moved_to = moved_to  # the tip on the second read, if the base moves
        self.calls = []
        self.tip_reads = 0

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["pr", "view"]:
            return json.dumps({
                "headRefOid": self.head, "baseRefName": "main",
                "state": self.state, "statusCheckRollup": self.rollup})
        if args[0] == "api" and "/branches/main" in args[1]:
            self.tip_reads += 1
            if self.moved_to and self.tip_reads > 1:
                return self.moved_to + "\n"
            return self.tip + "\n"
        if args[0] == "api" and "/actions/runs/" in args[1]:
            rid = int(args[1].rsplit("/", 1)[1])
            base = self.run_bases.get(rid, self.tip)
            prs = [] if base is None else [
                {"number": 7, "base": {"ref": "main", "sha": base}}]
            return json.dumps({"event": "pull_request", "pull_requests": prs})
        if args[:2] == ["pr", "merge"]:
            return ""
        raise AssertionError(f"unexpected gh call {args}")

    @property
    def merged(self):
        return [c for c in self.calls if c[:2] == ["pr", "merge"]]


def gate(fake, *argv):
    out, err = io.StringIO(), io.StringIO()
    code = mg.main(list(argv), gh=fake, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_all_green_merges_with_the_judged_head_pinned():
    gh = FakeGh(green_rollup())
    code, out, _ = gate(gh, "merge", "7", "--merge")
    assert code == 0 and "ALLOWED" in out
    assert gh.merged == [["pr", "merge", "7", "--repo", "inso1337/revl",
                          "--match-head-commit", HEAD, "--merge"]]


def test_red_root_suite_shard_with_every_required_context_green_is_refused():
    """The #2620 shape: required rollup green, shard 4 red."""
    roll = green_rollup()
    roll = [node("root-suite-affected (4)", "FAILURE") if n["name"] == SHARDS[3]
            else n for n in roll]
    gh = FakeGh(roll)
    code, out, _ = gate(gh, "merge", "7", "--merge")
    assert code == 1
    assert "REFUSED" in out and "ci / root-suite-affected (4)" in out
    assert "FAILURE" in out
    assert gh.merged == [], "gh pr merge must never be called on a refusal"
    # the other three shards and the required contexts are not blamed
    assert "root-suite-affected (1)" not in out and "lint" not in out


def test_the_rule_covers_every_advisory_job_not_just_the_shards():
    for name in ("held-out", "gate-wasm", "some-job-added-next-month"):
        roll = green_rollup() + [node(name, "FAILURE")]
        gh = FakeGh(roll)
        code, out, _ = gate(gh, "merge", "7", "--merge")
        assert code == 1 and f"ci / {name}" in out and gh.merged == []


@pytest.mark.parametrize("conclusion", ["TIMED_OUT", "STARTUP_FAILURE",
                                        "ACTION_REQUIRED"])
def test_every_red_conclusion_refuses(conclusion):
    gh = FakeGh(green_rollup() + [node("extra", conclusion)])
    assert gate(gh, "check", "7")[0] == 1


def test_a_cancelled_run_is_not_green_and_not_absent():
    gh = FakeGh(green_rollup() + [node("root-suite-affected (2)", "CANCELLED",
                                       run=101, started="2026-11-01T11:00:00Z")])
    code, out, _ = gate(gh, "merge", "7")
    assert code == 1 and "cancelled" in out and gh.merged == []


def test_an_unfinished_run_waits():
    roll = green_rollup() + [node("root-suite-affected (3)", status="IN_PROGRESS",
                                  run=101, started="2026-11-01T11:00:00Z")]
    gh = FakeGh(roll)
    code, out, _ = gate(gh, "merge", "7")
    assert code == 1 and "not finished" in out and gh.merged == []


def test_required_green_from_an_old_base_does_not_count():
    """Stale: the run recorded an older base than the current tip."""
    gh = FakeGh(green_rollup(), run_bases={100: OLD_TIP})
    code, out, _ = gate(gh, "merge", "7")
    assert code == 1 and "stale" in out and OLD_TIP[:12] in out
    assert gh.merged == []


def test_a_run_that_recorded_no_base_is_refused():
    gh = FakeGh(green_rollup(), run_bases={100: None})
    code, out, _ = gate(gh, "check", "7")
    assert code == 1 and "no base commit" in out


def test_a_rerun_supersedes_the_red_run_it_repeats():
    roll = green_rollup() + [
        node("root-suite-affected (4)", "FAILURE", run=100,
             started="2026-11-01T09:00:00Z"),
    ]
    # the green shard 4 in green_rollup started at 10:00, so it is the newer run
    gh = FakeGh(roll)
    code, out, _ = gate(gh, "merge", "7", "--merge")
    assert code == 0 and len(gh.merged) == 1


def test_a_newer_red_run_supersedes_an_older_green_one():
    roll = green_rollup() + [
        node("root-suite-affected (4)", "FAILURE", run=102,
             started="2026-11-01T12:00:00Z")]
    gh = FakeGh(roll)
    code, out, _ = gate(gh, "merge", "7")
    assert code == 1 and "root-suite-affected (4)" in out and gh.merged == []


def test_a_skipped_job_is_not_a_check_that_ran():
    roll = green_rollup() + [node("conformance", "SKIPPED")]
    gh = FakeGh(roll)
    code, out, _ = gate(gh, "merge", "7")
    assert code == 0 and len(gh.merged) == 1


def test_a_red_legacy_status_context_refuses():
    roll = green_rollup() + [{"__typename": "StatusContext",
                              "context": "ci/legacy", "state": "FAILURE"}]
    gh = FakeGh(roll)
    code, out, _ = gate(gh, "check", "7")
    assert code == 1 and "ci/legacy" in out


def test_a_waiver_needs_a_reason_and_is_printed():
    roll = green_rollup() + [node("census-artifact", "FAILURE")]
    gh = FakeGh(roll)
    code, out, _ = gate(gh, "merge", "7", "--merge", "--waive",
                        "ci / census-artifact=stale ref, green on main")
    assert code == 0 and "waived: ci / census-artifact" in out
    assert len(gh.merged) == 1
    gh = FakeGh(roll)
    code, _, err = gate(gh, "merge", "7", "--waive", "ci / census-artifact=")
    assert code == 2 and gh.merged == []


def test_a_waiver_that_matches_nothing_does_not_pass():
    gh = FakeGh(green_rollup())
    code, out, _ = gate(gh, "merge", "7", "--waive", "ci / typo=because")
    assert code == 1 and "no offending context" in out and gh.merged == []


@pytest.mark.parametrize("flags", [
    ["--auto"], ["--admin"], ["--match-head-commit=x"],
    ["--match-head-commit", "x"], ["-R", "other/repo"],
    ["--", "--auto"], ["--", "-R", "other/repo"],
])
def test_flags_that_defeat_the_gate_are_refused_before_anything_is_read(flags):
    gh = FakeGh(green_rollup())
    code, _, err = gate(gh, "merge", "7", *flags)
    assert code == 2, err
    assert gh.calls == []


def test_repo_is_the_gates_own_option_so_judge_and_merge_agree():
    """`--repo` is read by the gate, never passed through a second time: the
    repository judged is the repository merged."""
    gh = FakeGh(green_rollup())
    code, _, _ = gate(gh, "merge", "7", "--repo", "other/repo", "--merge")
    assert code == 0
    assert all("other/repo" in " ".join(c) for c in gh.calls
               if c[:2] in (["pr", "view"], ["pr", "merge"]))
    assert gh.merged[0].count("--repo") == 1


@pytest.mark.parametrize("flags", [["--rep", "x"], ["--unknown"], ["--subject"],
                                   ["--merge=1"]])
def test_only_the_allowlisted_merge_flags_pass_through(flags):
    gh = FakeGh(green_rollup())
    code, _, err = gate(gh, "merge", "7", *flags)
    assert code == 2 and gh.calls == [], err


def test_message_flags_pass_through_with_their_values():
    gh = FakeGh(green_rollup())
    code, _, _ = gate(gh, "merge", "7", "--squash", "--subject", "s", "--body=b",
                      "--delete-branch")
    assert code == 0
    assert gh.merged[0][7:] == ["--squash", "--subject", "s", "--body=b",
                                "--delete-branch"]


@pytest.mark.parametrize("rollup", [
    [], [node(n, "SKIPPED") for n in ("changes", "lint")]])
def test_no_check_at_all_is_refused(rollup):
    """Right after a push the workflows have not registered; nothing
    certifies the head, so it must not merge."""
    gh = FakeGh(rollup)
    code, out, _ = gate(gh, "merge", "7", "--merge")
    assert code == 1 and "(no checks)" in out and gh.merged == []


def test_a_queued_rerun_with_gh_zero_start_time_beats_an_older_green():
    """gh reports a queued run's startedAt as 0001-01-01T00:00:00Z, which
    sorts as the oldest run and would let the old green stand."""
    queued = node("root-suite-affected (4)", status="QUEUED", run=101,
                  started="0001-01-01T00:00:00Z")
    gh = FakeGh(green_rollup() + [queued])
    code, out, _ = gate(gh, "merge", "7")
    assert code == 1 and "not finished" in out and gh.merged == []
    # and in the other list order
    gh = FakeGh([queued] + green_rollup())
    assert gate(gh, "check", "7")[0] == 1


def test_an_unknown_legacy_status_state_is_not_a_pass():
    roll = green_rollup() + [{"__typename": "StatusContext",
                              "context": "ci/legacy", "state": "WEIRD"}]
    assert gate(FakeGh(roll), "check", "7")[0] == 1
    roll = green_rollup() + [{"__typename": "StatusContext",
                              "context": "ci/legacy", "state": "SUCCESS"}]
    assert gate(FakeGh(roll), "check", "7")[0] == 0


@pytest.mark.parametrize("reverse", [False, True])
def test_supersession_does_not_depend_on_list_order(reverse):
    red_old = node("root-suite-affected (4)", "FAILURE", run=90,
                   started="2026-11-01T09:00:00Z")
    base = [n for n in green_rollup() if n["name"] != SHARDS[3]]
    green_new = node("root-suite-affected (4)", run=91,
                     started="2026-11-01T10:00:00Z")
    roll = base + [red_old, green_new]
    gh = FakeGh(roll[::-1] if reverse else roll)
    assert gate(gh, "check", "7")[0] == 0
    red_new = node("root-suite-affected (4)", "FAILURE", run=92,
                   started="2026-11-01T11:00:00Z")
    roll = base + [green_new, red_new]
    gh = FakeGh(roll[::-1] if reverse else roll)
    assert gate(gh, "check", "7")[0] == 1


def test_a_same_start_time_tie_is_broken_by_the_newer_run_id():
    same = "2026-11-01T10:00:00Z"
    base = [n for n in green_rollup() if n["name"] != SHARDS[3]]
    for red_id, green_id, code in ((200, 201, 0), (201, 200, 1)):
        roll = base + [node(SHARDS[3], "FAILURE", run=red_id, started=same),
                       node(SHARDS[3], run=green_id, started=same)]
        assert gate(FakeGh(roll), "check", "7")[0] == code


def test_only_the_stale_shard_is_named():
    roll = [node(n, run=(300 if n == SHARDS[1] else 100)) for n in
            REQUIRED + SHARDS]
    gh = FakeGh(roll, run_bases={300: OLD_TIP})
    code, out, _ = gate(gh, "check", "7")
    assert code == 1
    assert "root-suite-affected (2)" in out and "stale" in out
    assert "root-suite-affected (1)" not in out and "lint" not in out


def test_a_stale_run_superseded_by_a_fresh_one_passes():
    stale = node(SHARDS[1], run=300, started="2026-11-01T09:00:00Z")
    fresh = node(SHARDS[1], run=301, started="2026-11-01T12:00:00Z")
    roll = [n for n in green_rollup() if n["name"] != SHARDS[1]] + [stale, fresh]
    gh = FakeGh(roll, run_bases={300: OLD_TIP, 301: TIP})
    assert gate(gh, "check", "7")[0] == 0


def test_a_stale_check_can_be_waived_by_name_with_a_reason():
    gh = FakeGh(green_rollup(), run_bases={100: OLD_TIP})
    waive = []
    for n in REQUIRED + SHARDS + ["changes", "held-out"]:
        waive += ["--waive", f"ci / {n}=base moved by an unrelated docs commit"]
    code, out, _ = gate(gh, "merge", "7", "--merge", *waive)
    assert code == 0 and "waived: ci / lint" in out


def test_a_skipped_newest_run_supersedes_an_older_red():
    """Pinned on purpose: a re-run the `changes` job now skips is not a check
    that ran, so the red it replaced no longer stands."""
    roll = [n for n in green_rollup() if n["name"] != SHARDS[3]] + [
        node(SHARDS[3], "FAILURE", run=90, started="2026-11-01T09:00:00Z"),
        node(SHARDS[3], "SKIPPED", run=91, started="2026-11-01T11:00:00Z")]
    assert gate(FakeGh(roll), "check", "7")[0] == 0


def test_a_duplicate_waiver_is_an_error():
    gh = FakeGh(green_rollup())
    code, _, err = gate(gh, "check", "7", "--waive", "a=x", "--waive", "a=y")
    assert code == 2


def test_an_unparseable_run_record_is_an_error_not_a_refusal():
    class Bad(FakeGh):
        def __call__(self, args):
            if args[0] == "api" and "/actions/runs/" in args[1]:
                return "not json"
            return super().__call__(args)
    code, _, err = gate(Bad(green_rollup()), "merge", "7")
    assert code == 2 and "unparseable" in err


def test_a_base_that_moves_during_the_read_is_not_merged():
    gh = FakeGh(green_rollup(), moved_to="d" * 40)
    code, _, err = gate(gh, "merge", "7", "--merge")
    assert code == 1 and "moved" in err and gh.merged == []


def test_a_closed_pr_is_an_error_not_a_pass():
    gh = FakeGh(green_rollup(), state="MERGED")
    assert gate(gh, "merge", "7")[0] == 2


def test_check_never_merges():
    gh = FakeGh(green_rollup())
    assert gate(gh, "check", "7")[0] == 0 and gh.merged == []


def test_unreadable_pr_is_an_error():
    def boom(args):
        raise mg.GateError("no such pr")
    assert gate(boom, "check", "7")[0] == 2


def _stub_gh(tmp_path, rollup):
    """A real executable standing in for `gh`, so the CLI entry point, the
    REVL_MERGE_GH selection and the subprocess path are exercised, not just
    the in-process seam."""
    log = tmp_path / "calls.log"
    script = tmp_path / "gh-stub"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, sys
        args = sys.argv[1:]
        open({str(log)!r}, "a").write(" ".join(args) + "\\n")
        if args[:2] == ["pr", "view"]:
            print(json.dumps({{"headRefOid": {HEAD!r}, "baseRefName": "main",
                "state": "OPEN", "statusCheckRollup": {rollup!r}}}))
        elif args[0] == "api" and "/branches/main" in args[1]:
            print({TIP!r})
        elif args[0] == "api" and "/actions/runs/" in args[1]:
            print(json.dumps({{"pull_requests": [{{"number": 7,
                "base": {{"sha": {TIP!r}}}}}]}}))
        elif args[:2] == ["pr", "merge"]:
            pass
        else:
            sys.exit(9)
        """))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, log


def _cli(script, *argv):
    env = dict(os.environ, REVL_MERGE_GH=str(script))
    return subprocess.run(
        [sys.executable, str(ROOT / "tools" / "merge_gate.py"), *argv],
        capture_output=True, text=True, env=env, check=False)


def test_cli_refuses_a_red_shard_and_never_calls_pr_merge(tmp_path):
    roll = [node("root-suite-affected (4)", "FAILURE") if n["name"] == SHARDS[3]
            else n for n in green_rollup()]
    script, log = _stub_gh(tmp_path, roll)
    proc = _cli(script, "merge", "7", "--merge")
    assert proc.returncode == 1, proc.stderr
    assert "ci / root-suite-affected (4)" in proc.stdout
    assert "pr merge" not in log.read_text()


def test_cli_merges_when_green(tmp_path):
    script, log = _stub_gh(tmp_path, green_rollup())
    proc = _cli(script, "merge", "7", "--squash")
    assert proc.returncode == 0, proc.stderr
    assert f"pr merge 7 --repo inso1337/revl --match-head-commit {HEAD} --squash" \
        in log.read_text()


def test_cli_a_moved_base_and_a_waiver_through_the_subprocess_path(tmp_path):
    roll = [node("census-artifact", "FAILURE")] + green_rollup()
    script, log = _stub_gh(tmp_path, roll)
    proc = _cli(script, "merge", "7", "--merge")
    assert proc.returncode == 1 and "ci / census-artifact" in proc.stdout
    assert "pr merge" not in log.read_text()
    proc = _cli(script, "merge", "7", "--merge", "--waive",
                "ci / census-artifact=stale ref, green on main")
    assert proc.returncode == 0, proc.stderr
    assert "waived: ci / census-artifact" in proc.stdout
    assert "pr merge 7" in log.read_text()


def test_the_merge_procedure_documents_the_gate():
    text = (ROOT / "docs" / "process.md").read_text(encoding="utf-8")
    merging = text.split("\n## Merging\n", 1)[1].split("\n## ", 1)[0]
    assert "tools/merge_gate.py" in merging
    assert "--auto" in merging, "the auto-merge exclusion must be stated"
