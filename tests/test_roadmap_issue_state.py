"""Issue #2100: the marker gate learns to read issue STATE, and stays honest.

MEASURED ON MAIN. `docs/v2.0-roadmap.md` had 25 top-level rows carrying the
`🚧 PARTIAL` glyph; 18 of them cited a CLOSED issue and only 7 cited an OPEN
one. Nothing in `tools/` read GitHub issue state at all, so the citation the
gate required (`--require-issue`) was satisfied by an issue that had been shut
months earlier: the row still read as work in progress with nothing left to
follow. The gate's own closing note conceded the hole -- it "did not, and
cannot, check that any landed branch actually closed the finding it is attached
to" -- and that is true of git, but not of the tracker.

WHY `--require-issue` NEVER SAW THOSE 25 ROWS. `items()` derives a row's status
from `rest[:2]`, which is right for "118. ◑ (issue #79) ..." and wrong for
"(issue #1197) **🚧 PARTIAL, ...": there the citation and the bold span come
first, the glyph sits about twenty characters in, and `items()` answers "open".
The rows this issue is about were therefore outside the citation gate's scope
entirely. `inprogress_records` reads the marker where the file writes it.

WHAT THIS FILE PINS.
  1. The record extraction, and that it catches rows `items()` does not.
  2. The one finding: cited issue CLOSED and no successor named.
  3. FAIL-CLOSED. Every way the lookup can go wrong -- no `gh`, no repo, no
     auth, a timeout, a rate limit, a 404, a malformed answer -- leaves the
     issue UNKNOWN. Unknown is never closed, so a broken lookup can neither
     manufacture a finding nor quietly pass: it is reported with a count.
  4. The CLI: the audit reports and exits 0; the gate fires on positive
     evidence; a total lookup failure exits 2 ("environment cannot answer"),
     never 0.

NO NETWORK. Every `gh` here is a shell stub on a private PATH, or a
monkeypatched `subprocess.run`. The suite must pass on a plane.

THIS FILE IS ALSO THE BASE-FAIL EVIDENCE. It loads the tool from
`$RVL_ROADMAP_GATE_DIR` (default: this repo's `tools/`), so it can be pointed
at `origin/main`'s version of the gate to show the invariants failing there:

    mkdir -p /tmp/base2100/tools
    git show origin/main:tools/check_roadmap_markers.py \\
        > /tmp/base2100/tools/check_roadmap_markers.py
    RVL_ROADMAP_GATE_DIR=/tmp/base2100/tools PYTHONPATH=src \\
        /private/tmp/venv-1982/bin/python -m pytest \\
        tests/test_roadmap_issue_state.py -q

On base that run fails: the audit surface does not exist and the gate cannot
be asked the question. On this tree it passes.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from _load_by_path import load_by_path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"

# Point this at another checkout's `tools/` to run these tests against a
# different revision of the gate. That is how the base-fail evidence in the
# module docstring is produced; nothing else in the suite uses it.
GATE_DIR = Path(os.environ.get("RVL_ROADMAP_GATE_DIR", str(TOOLS)))
TOOL = GATE_DIR / "check_roadmap_markers.py"

if str(GATE_DIR) not in sys.path:
    sys.path.insert(0, str(GATE_DIR))
gate = load_by_path("gate_under_test", TOOL)


def _roadmap_text() -> str:
    """The main file plus docs/roadmap-archive/, as the gate reads it. A gate
    from before the archive (`$RVL_ROADMAP_GATE_DIR` pointed at an older
    tools/) has no loader and reads the one file it knew."""
    loader = getattr(gate, "read_roadmap", None)
    if loader is None:
        return ROADMAP.read_text(encoding="utf-8")
    return loader(ROADMAP).text

ROADMAP = REPO_ROOT / "docs" / "v2.0-roadmap.md"

# The argv `.github/workflows/ci.yml`'s `lint` job runs, with `--no-fetch` so a
# test never needs the network. This is the line as it stood BEFORE issue #2100,
# kept verbatim so the additivity is asserted and not assumed.
CI_ARGS = [
    "--check-contradiction",
    "--check-delegation",
    "--check-duplicate-headers",
    "--check-orphan",
    "--require-issue",
    "--check-tier-parity",
    "--head-branch",
    "",
    "--no-fetch",
]

# ... and the line issue #2100 adds. Report only, offline, exit 0.
CI_ARGS_NEW = CI_ARGS[:-1] + ["--audit-successors", "--no-fetch"]

# The API the audit added. Named here so a run against the BASE tool fails as a
# legible list of missing names instead of a collection error.
AUDIT_SURFACE = (
    "INPROGRESS_GLYPHS", "INPROGRESS_ROW_RE", "ISSUE_NUM_RE", "RESIDUAL_RE",
    "GH_STATES", "inprogress_records", "successor_findings", "repo_slug",
    "gh_issue_states", "successor_report",
)


def test_the_audit_surface_exists():
    """Fails on `origin/main`, where issue #2100's audit does not exist yet."""
    missing = [name for name in AUDIT_SURFACE if not hasattr(gate, name)]
    assert not missing, (
        f"issue #2100's audit surface is missing from {TOOL}: {missing}"
    )


def test_the_new_flags_are_accepted():
    """`--help` must list the three new flags; on base argparse refuses them."""
    proc = subprocess.run([sys.executable, str(TOOL), "--help"],
                          capture_output=True, text=True, cwd=REPO_ROOT)
    assert proc.returncode == 0, proc.stderr
    for flag in ("--audit-successors", "--require-successor", "--issue-state"):
        assert flag in proc.stdout, f"{flag} is not a flag of {TOOL}"


# ---------------------------------------------------------------------------
# Records: the glyph read where the file writes it.
# ---------------------------------------------------------------------------

def _roadmap(*items: str) -> str:
    body = "\n".join(items)
    return ("# roadmap\n\n## Open, in rough priority order\n\n" + body + "\n")


# The shape 25 rows on main use: the citation first, then the bold span.
CITED_INFLIGHT = ("523. (issue #1197) **\U0001F6A7 PARTIAL, THE MATRIX LANDED "
                  "(2026-09-20): `tools/tier_guarantees.py` generates the "
                  "block. STILL OPEN: `--check-tier-parity` reports one "
                  "finding.")


def test_items_misses_the_rows_this_audit_exists_for():
    """The bug underneath #2100, pinned so a fix to `items()` cannot hide it.

    `items()` reads `rest[:2]`; on "(issue #1197) **🚧 PARTIAL ..." that is
    "(i". The 25 rows are classified OPEN there and IN-PROGRESS here.
    """
    text = _roadmap(CITED_INFLIGHT)
    assert [it["status"] for it in gate.items(text)] == ["open"]
    recs = gate.inprogress_records(text)
    assert len(recs) == 1
    assert recs[0]["number"] == "523"
    assert recs[0]["primary"] == 1197
    assert recs[0]["kind"] == "in-flight"
    assert recs[0]["residual"] is True


def test_both_in_progress_glyphs_are_read():
    """🚧 and ◑ are both "still work"; the file writes both."""
    text = _roadmap(
        CITED_INFLIGHT,
        "79. \u25d1 (issue #79) DESIGN DONE, one residual left.",
    )
    recs = gate.inprogress_records(text)
    assert [(r["number"], r["kind"]) for r in recs] == [
        ("523", "in-flight"), ("79", "partial"),
    ]
    assert recs[1]["primary"] == 79


def test_a_done_row_is_not_an_in_progress_row():
    """The correction to item 523 must actually take it out of the population."""
    text = _roadmap(
        "523. (issue #1197) **\u2705 LANDED: `--check-tier-parity` is on in CI "
        "and reports nothing."
    )
    assert gate.inprogress_records(text) == []


def test_successors_are_every_other_issue_the_row_names():
    """The row's OWN citation is not its successor; the others are."""
    text = _roadmap(
        "549. (issue #1268) **\U0001F6A7 PARTIAL, in flight. The residual is "
        "tracked in issue #1221 and issue #1768."
    )
    rec = gate.inprogress_records(text)[0]
    assert rec["primary"] == 1268
    assert rec["successors"] == [1221, 1768]


def test_a_bare_hash_is_not_an_issue_citation():
    """`#58` is how this file writes merged PRs, so it cannot be a successor."""
    text = _roadmap(
        "549. (issue #1268) **\U0001F6A7 PARTIAL. Landed in #58, still going."
    )
    assert gate.inprogress_records(text)[0]["successors"] == []


def test_the_residual_column_reads_the_vocabulary_the_rows_use():
    yes = gate.inprogress_records(_roadmap(
        "1. (issue #2) **\U0001F6A7 PARTIAL. STILL OPEN: one finding."
    ))[0]
    no = gate.inprogress_records(_roadmap(
        "1. (issue #2) **\U0001F6A7 PARTIAL. The last blocker cleared "
        "(issue #1572)."
    ))[0]
    assert yes["residual"] is True
    assert no["residual"] is False


# ---------------------------------------------------------------------------
# The finding: closed issue, no successor. Fires on evidence and nothing else.
# ---------------------------------------------------------------------------

def test_closed_issue_with_no_successor_is_a_finding():
    findings = gate.successor_findings(_roadmap(CITED_INFLIGHT), {1197: "closed"})
    assert len(findings) == 1
    assert "item 523" in findings[0]
    assert "#1197" in findings[0]
    assert "CLOSED" in findings[0]


@pytest.mark.parametrize("states", [
    None,           # offline: no evidence was gathered
    {},             # the lookup answered nothing
    {1197: "open"},  # the issue is live: the row is tracking something real
    {999: "closed"},  # a different issue closed; this row's state is UNKNOWN
])
def test_no_finding_without_a_closed_verdict_on_THIS_issue(states):
    assert gate.successor_findings(_roadmap(CITED_INFLIGHT), states) == []


def test_a_named_successor_clears_the_finding():
    text = _roadmap(
        "523. (issue #1197) **\U0001F6A7 PARTIAL, THE MATRIX LANDED. The last "
        "blocker is tracked in issue #1572."
    )
    assert gate.successor_findings(text, {1197: "closed"}) == []


def test_a_row_citing_nothing_is_not_this_gates_finding():
    """It is decidably broken, but that is `--require-issue`'s question.

    Failing it here would redden the gate on main for a reason that has nothing
    to do with issue state, so the audit reports the row instead.
    """
    text = _roadmap("509. \U0001F6A7 **QUORUM: BIND THE IDENTITY OF A CAST.")
    assert gate.inprogress_records(text)[0]["primary"] is None
    assert gate.successor_findings(text, {}) == []


def test_untracked_sections_are_out_of_scope():
    text = ("# roadmap\n\n## Declined, deliberately\n\n"
            "1. (issue #1197) **\U0001F6A7 PARTIAL, kept for the record.**\n")
    assert gate.successor_findings(text, {1197: "closed"}) == []


# ---------------------------------------------------------------------------
# Fail-closed: the lookup. Every failure is UNKNOWN, never CLOSED.
# ---------------------------------------------------------------------------

class _Proc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_no_gh_on_path_is_unknown_not_closed(monkeypatch):
    monkeypatch.setattr(gate.shutil, "which", lambda name: None)
    states, unavailable = gate.gh_issue_states([1197], "inso1337/revl")
    assert states == {}
    assert len(unavailable) == 1
    assert "PATH" in unavailable[0]


def test_no_repo_slug_is_unknown_not_closed(monkeypatch):
    monkeypatch.setattr(gate.shutil, "which", lambda name: "/usr/bin/gh")
    states, unavailable = gate.gh_issue_states([1197], None)
    assert states == {}
    assert "origin" in unavailable[0]


@pytest.mark.parametrize("failure,needle", [
    (lambda *a, **k: _Proc(1, stderr="gh: Not Found (HTTP 404)"), "404"),
    (lambda *a, **k: _Proc(1, stderr="gh: API rate limit exceeded"), "rate limit"),
    (lambda *a, **k: _Proc(0, stdout=""), "not a state"),
    (lambda *a, **k: _Proc(0, stdout="weird"), "not a state"),
])
def test_a_failed_or_malformed_lookup_is_unavailable(monkeypatch, failure, needle):
    monkeypatch.setattr(gate.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(gate.subprocess, "run", failure)
    states, unavailable = gate.gh_issue_states([1197], "inso1337/revl")
    assert states == {}, "a failed lookup must never be read as a state"
    assert len(unavailable) == 1
    assert needle in unavailable[0]


def test_a_timeout_is_unavailable_not_closed(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=0.01)

    monkeypatch.setattr(gate.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(gate.subprocess, "run", boom)
    states, unavailable = gate.gh_issue_states([1197], "inso1337/revl")
    assert states == {}
    assert "did not answer" in unavailable[0]


def test_a_good_answer_is_read(monkeypatch):
    monkeypatch.setattr(gate.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(gate.subprocess, "run",
                        lambda *a, **k: _Proc(0, stdout="OPEN\n"))
    states, unavailable = gate.gh_issue_states([1197], "inso1337/revl")
    assert states == {1197: "open"}
    assert unavailable == []


def test_one_failure_does_not_poison_the_others(monkeypatch):
    """A 404 on one issue leaves THAT issue unknown and the rest answered."""
    def run(cmd, **k):
        if cmd[-3].endswith("/1197"):
            return _Proc(1, stderr="gh: Not Found (HTTP 404)")
        return _Proc(0, stdout="closed")

    monkeypatch.setattr(gate.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(gate.subprocess, "run", run)
    states, unavailable = gate.gh_issue_states([1197, 1205], "inso1337/revl")
    assert states == {1205: "closed"}
    assert len(unavailable) == 1 and "#1197" in unavailable[0]


# ---------------------------------------------------------------------------
# The report: it can never be mistaken for a bare OK.
# ---------------------------------------------------------------------------

def test_the_offline_report_says_unknown_and_says_why():
    report = gate.successor_report(_roadmap(CITED_INFLIGHT), None, [], False)
    assert "state=unknown" in report
    assert "No live state was consulted" in report
    assert "not a pass and not a closed issue" in report
    # No live split is claimed offline: that would be a fabricated measurement.
    assert "Split over" not in report


def test_the_live_report_states_the_split_and_the_gaps():
    text = _roadmap(CITED_INFLIGHT)
    report = gate.successor_report(text, {1197: "closed"}, ["#1197: nope"], True)
    assert "1 cite an OPEN issue" in report or "0 cite an OPEN issue" in report
    assert "1 cite a CLOSED one" in report
    assert "did NOT resolve and are counted as UNKNOWN above, never as CLOSED" \
        in report


def test_the_report_disclaims_the_thing_it_cannot_decide():
    report = gate.successor_report(_roadmap(CITED_INFLIGHT), None, [], False)
    assert "did NOT decide" in report
    assert "not a list to flip" in report


# ---------------------------------------------------------------------------
# The CLI, end to end, with a `gh` stub on a private PATH.
# ---------------------------------------------------------------------------

GH_STUB = """#!/bin/sh
# Stands in for: gh api repos/<owner>/<repo>/issues/<N> --jq .state
# Answers from a directory of one-word files. A missing file is a 404, so one
# lookup can fail while the others answer.
n=${2##*/}
f="$FAKE_GH_ANSWERS/$n"
if [ -f "$f" ]; then cat "$f"; else echo "gh: Not Found (HTTP 404)" >&2; exit 1; fi
"""


def _gh_bin(tmp_path: Path, answers: dict[int, str]) -> Path:
    """A private `gh` on PATH, answering only the issues in `answers`."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "gh"
    stub.write_text(GH_STUB, encoding="utf-8")
    stub.chmod(0o755)
    answer_dir = tmp_path / "answers"
    answer_dir.mkdir(exist_ok=True)
    for number, state in answers.items():
        (answer_dir / str(number)).write_text(state + "\n", encoding="utf-8")
    return bindir


def _run_tool(tmp_path: Path, roadmap: Path, *flags: str,
              bindir: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if bindir is not None:
        env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
        env["FAKE_GH_ANSWERS"] = str(tmp_path / "answers")
    return subprocess.run(
        [sys.executable, str(TOOL), "--roadmap", str(roadmap), *flags],
        cwd=REPO_ROOT, capture_output=True, text=True, env=env,
    )


def _write_roadmap(tmp_path: Path, *items: str) -> Path:
    path = tmp_path / "roadmap.md"
    path.write_text(_roadmap(*items), encoding="utf-8")
    return path


def test_the_audit_flag_reports_and_exits_zero(tmp_path):
    roadmap = _write_roadmap(tmp_path, CITED_INFLIGHT)
    proc = _run_tool(tmp_path, roadmap, "--audit-successors", "--no-fetch")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "top-level row(s) carry an in-progress glyph" in proc.stdout
    assert "state=unknown" in proc.stdout
    assert "No live state was consulted" in proc.stdout


def test_the_gate_is_green_offline_because_it_has_no_evidence(tmp_path):
    """No network, no verdict. The gate must not guess."""
    roadmap = _write_roadmap(tmp_path, CITED_INFLIGHT)
    proc = _run_tool(tmp_path, roadmap, "--require-successor", "--no-fetch")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_gate_fires_on_a_closed_issue_with_no_successor(tmp_path):
    roadmap = _write_roadmap(tmp_path, CITED_INFLIGHT)
    bindir = _gh_bin(tmp_path, {1197: "closed"})
    proc = _run_tool(tmp_path, roadmap, "--require-successor", "--issue-state",
                     "--no-fetch", bindir=bindir)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "--require-successor: 1 in-progress row(s)" in proc.stdout
    assert "cites issue #1197, which is CLOSED" in proc.stdout
    assert "1 cite a CLOSED one" in proc.stdout


def test_the_gate_passes_a_closed_issue_that_names_a_successor(tmp_path):
    roadmap = _write_roadmap(tmp_path, (
        "523. (issue #1197) **\U0001F6A7 PARTIAL. The last blocker is tracked "
        "in issue #1572."
    ))
    bindir = _gh_bin(tmp_path, {1197: "closed", 1572: "open"})
    proc = _run_tool(tmp_path, roadmap, "--require-successor", "--issue-state",
                     "--no-fetch", bindir=bindir)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_total_lookup_failure_exits_two_and_never_zero(tmp_path):
    """No `gh` answer at all is "the environment cannot answer", not a pass.

    This is the fail-closed contract at the CLI boundary: exit 0 would read as
    "no stale rows", which is precisely the claim the run has no evidence for.
    """
    roadmap = _write_roadmap(tmp_path, CITED_INFLIGHT)
    bindir = _gh_bin(tmp_path, {})          # every lookup 404s
    proc = _run_tool(tmp_path, roadmap, "--require-successor", "--issue-state",
                     "--no-fetch", bindir=bindir)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "no issue state could be read" in proc.stderr
    assert "Nothing below is a verdict on any row" in proc.stderr


def test_a_partial_lookup_failure_still_judges_what_it_read(tmp_path):
    """One issue answered, one 404'd: the gate fires on the one it READ and
    reports the other as UNKNOWN -- never as closed."""
    roadmap = _write_roadmap(
        tmp_path,
        "1. (issue #1197) **\U0001F6A7 PARTIAL, in flight. STILL OPEN: one.",
        "2. (issue #1205) **\U0001F6A7 PARTIAL, in flight. STILL OPEN: two.",
    )
    bindir = _gh_bin(tmp_path, {1197: "closed"})   # #1205 is a 404
    proc = _run_tool(tmp_path, roadmap, "--require-successor", "--issue-state",
                     "--no-fetch", bindir=bindir)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "cites issue #1197, which is CLOSED" in proc.stdout
    # The findings block prints above the report; an UNKNOWN must not be in it.
    findings = proc.stdout.split("--audit-successors:")[0]
    assert "#1197" in findings
    assert "#1205" not in findings, "an unknown issue must not become a finding"
    assert "issue=#1205 state=unavailable" in proc.stdout
    assert "#1205: `gh api` exited 1" in proc.stdout
    assert "never as CLOSED" in proc.stdout


# ---------------------------------------------------------------------------
# The real roadmap, offline, and the existing behaviour left alone.
# ---------------------------------------------------------------------------

def test_the_audit_catches_rows_the_citation_gate_cannot_see():
    """On the real roadmap: the 25 rows of issue #2100 are in scope HERE.

    `items()` calls every one of them OPEN, so `--require-issue` never asked
    them anything. Asserted as a lower bound: the roadmap is a living document
    and other lanes add rows to it.
    """
    text = _roadmap_text()
    records = gate.inprogress_records(text)
    inflight = [r for r in records if r["glyph"] in gate.INFLIGHT_GLYPHS]
    assert len(inflight) >= 20, (
        "issue #2100 measured 25 🚧 rows; the audit found "
        f"{len(inflight)}. If rows were legitimately closed out, lower this "
        "bound deliberately and say so."
    )
    statuses = {it["number"]: it["status"] for it in gate.items(text)}
    # The audit's population is a SUPERSET of `items()`'s: it can only add
    # rows to the citation gate's scope, never drop one.
    for number, status in statuses.items():
        if status == "in-flight":
            assert number in {r["number"] for r in inflight}, (
                f"item {number} is in-flight to items() but not to the audit"
            )
    # And it adds plenty: on main 24 of the 25 rows were invisible to items(),
    # because the citation and the bold span come before the glyph.
    missed = [r for r in inflight if statuses.get(r["number"]) != "in-flight"]
    assert len(missed) >= 20, (
        "issue #2100 measured the great majority of the 🚧 rows as invisible "
        f"to the citation gate; the audit found only {len(missed)} such rows"
    )


def test_item_523_is_no_longer_an_in_progress_row():
    """Half 3 of issue #2100: the one provably stale row.

    Item 523 cited issue #1197 and said `--check-tier-parity` was "STILL OPEN"
    and that CI ran the check off. It is on since issue #1572, the check is
    clean, and `make roadmap-check-all` exits 0, so the row's own marker
    contradicted its body. The two REAL residuals (items 534 and 535) are
    deliberately left alone: their residuals are still in the tree.
    """
    text = _roadmap_text()
    assert not [r for r in gate.inprogress_records(text) if r["number"] == "523"]
    row = [it for it in gate.items(text) if it["number"] == "523"]
    assert len(row) == 1
    assert "STILL OPEN" not in row[0]["body"], (
        "item 523 still claims --check-tier-parity is open"
    )
    # The real residuals stay real. If these flip, it must be because the tree
    # changed, not because this gate wanted a greener roadmap.
    for number in ("534", "535"):
        recs = [r for r in gate.inprogress_records(text) if r["number"] == number]
        assert recs and recs[0]["residual"] is True, (
            f"item {number}'s residual is real on main and must stay flagged"
        )


def test_the_existing_ci_argv_is_unchanged_on_this_tree():
    """Additive only: the flags CI already passes still exit 0."""
    proc = subprocess.run(
        [sys.executable, str(TOOL), "--roadmap", str(ROADMAP), *CI_ARGS],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "roadmap markers OK" in proc.stdout
    # The pre-#2100 line prints no audit block: the addition is visible.
    assert "--audit-successors" not in proc.stdout


def test_the_new_ci_argv_audits_and_still_exits_zero():
    """The line CI now runs: same verdict, plus the population."""
    proc = subprocess.run(
        [sys.executable, str(TOOL), "--roadmap", str(ROADMAP), *CI_ARGS_NEW],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "roadmap markers OK" in proc.stdout
    assert "--audit-successors:" in proc.stdout
    assert "No live state was consulted" in proc.stdout


def test_ci_runs_the_audit_but_never_the_live_lookup():
    """The lint job must not gain a network dependency (issue #2100)."""
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8")
    # Comments explain why the live flags are absent; only the executed lines
    # decide it.
    code = "\n".join(line for line in ci.splitlines()
                     if not line.lstrip().startswith("#"))
    assert "--audit-successors" in code
    assert "--issue-state" not in code
    assert "--require-successor" not in code


def test_the_makefile_wires_the_audit_and_keeps_the_live_gate_opt_in():
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    targets: dict[str, str] = {}
    current = None
    for line in makefile.splitlines():
        if line and not line.startswith(("\t", "#", " ")) and ":" in line:
            current = line.split(":", 1)[0].strip()
            targets.setdefault(current, "")
        elif current is not None and line.startswith("\t"):
            targets[current] += line.strip() + " "
    assert "--audit-successors" in targets["roadmap-check"]
    assert "--issue-state" not in targets["roadmap-check"], (
        "the local lint mirror must not touch the network"
    )
    assert "--require-successor --issue-state" in targets["roadmap-issue-state"]
