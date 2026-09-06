"""The required-status-check list is pinned against ci.yml's job names (CI-2).

Branch protection gates a merge on a NAMED list of status checks, and that list
lives in GitHub settings, not in the repo. Nothing in-tree connected it to the
jobs ci.yml actually defines, so the two could drift silently in either
direction:

  * a job RENAMED in ci.yml (or split, or removed) leaves branch protection
    waiting forever on a check name that no run will ever report — the merge
    queue's "waits forever for checks that never run" failure, which the queue's
    own header comment in ci.yml calls out;
  * a NEW job added to ci.yml is non-required by default, so a gate can be added
    to the matrix and never actually block a merge, with nothing to notice.

This file is the pin. It does not talk to the GitHub API (a unit test has no
credentials and CI must stay hermetic); instead it encodes the INTENDED
partition of every ci.yml job into REQUIRED vs deliberately-NOT_REQUIRED and
asserts that partition still covers the jobs on disk EXACTLY. So the moment a job
is renamed, added, or dropped, this test reds and forces the author to make the
required-or-not call on purpose — which is the decision that was previously
implicit.

The four deliberately-non-required jobs (sandbox-container, gate-wasm,
temporal-exit, backend-roots-combined) are named here so that their exclusion is
an ASSERTED choice with a reason, not an oversight. If branch protection is ever
updated, update REQUIRED_CHECKS to match and this file documents the new intent.

NOTE on the "13 required checks" figure in ci.yml's merge-queue comment: this
partition marks 11 jobs required and 4 non-required. The two are reconciled at
the branch-protection settings, which are out of tree; whichever is stale, this
test at least makes the job-name side of the contract explicit and drift-proof.
It reads ci.yml as text, so it needs no PyYAML (not a declared dependency) and
rides the frontend job's plain `pytest tests/ -q`, like
tests/test_site_wheel_gate_runs_in_ci.py.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"

# Every ci.yml job that branch protection is intended to require before a merge.
REQUIRED_CHECKS = frozenset({
    "lint",
    "frontend",
    "backend-python",
    "frontend-cordis",
    "backend-typescript",
    "backend-wasm",
    "backend-rust",
    "backend-java",
    "backend-go",
    "conformance",
    "formal",
})

# Jobs deliberately NOT required. Each exclusion is a choice with a reason, so a
# reader can tell an intentional gap from a forgotten one.
NOT_REQUIRED_CHECKS = {
    # Container/privilege smoke that needs a Docker-capable runner; flaky as a
    # hard merge gate, run for signal not enforcement.
    "sandbox-container": "container smoke; not a hard merge gate",
    # The wasm vector gate is covered for correctness by backend-wasm; this job
    # is an extra cross-check, not the blocking one.
    "gate-wasm": "extra wasm cross-check; backend-wasm is the required gate",
    # Temporal saga exit test spins a dev server; environmental, kept advisory.
    "temporal-exit": "spins a temporal dev server; advisory, not blocking",
    # Aggregate/roots recombination job; informational over the per-tier gates
    # that are themselves required.
    "backend-roots-combined": "aggregate over already-required per-tier gates",
}


def _ci_job_names() -> set[str]:
    """Top-level job ids in ci.yml: keys at exactly two-space indent under the
    `jobs:` block. Job bodies are indented four-plus spaces, and top-level
    sections (on/env/concurrency/permissions) sit at zero indent before `jobs:`,
    so a two-space key after `jobs:` is unambiguously a job id."""
    text = CI.read_text(encoding="utf-8")
    _, _, body = text.partition("\njobs:")
    assert body, "ci.yml has no jobs: block"
    names: set[str] = set()
    for line in body.splitlines():
        m = re.match(r"^  ([A-Za-z0-9][A-Za-z0-9_-]*):\s*(?:#.*)?$", line)
        if m:
            names.add(m.group(1))
    return names


# --- anti-vacuity ---------------------------------------------------------- #
def test_ci_yml_parses_to_a_plausible_job_set():
    jobs = _ci_job_names()
    assert CI.is_file(), "ci.yml is gone; re-derive this pin against its successor"
    # Anchors that must exist for the parse to be trusted at all.
    for anchor in ("lint", "frontend", "conformance"):
        assert anchor in jobs, (
            f"ci.yml job parse looks wrong: {anchor!r} not found in {sorted(jobs)}"
        )
    assert len(jobs) >= 10, f"suspiciously few jobs parsed: {sorted(jobs)}"


# --- the pin: partition covers every job, exactly -------------------------- #
def test_required_and_not_required_partition_every_ci_job():
    jobs = _ci_job_names()
    classified = REQUIRED_CHECKS | set(NOT_REQUIRED_CHECKS)

    unclassified = jobs - classified
    assert not unclassified, (
        "ci.yml defines job(s) that are neither pinned REQUIRED nor listed as "
        f"deliberately non-required: {sorted(unclassified)}.\n"
        "A new job is non-required by default, so it can never block a merge "
        "until someone says so. Add each to REQUIRED_CHECKS (and to branch "
        "protection) or to NOT_REQUIRED_CHECKS with a reason."
    )

    stale = classified - jobs
    assert not stale, (
        "The required-check pin names job(s) that ci.yml no longer defines: "
        f"{sorted(stale)}.\n"
        "Branch protection then waits forever on a check name no run reports "
        "(the merge queue's 'checks that never run' hang). Rename or drop these "
        "in REQUIRED_CHECKS / NOT_REQUIRED_CHECKS to match ci.yml."
    )


def test_required_and_not_required_are_disjoint():
    overlap = REQUIRED_CHECKS & set(NOT_REQUIRED_CHECKS)
    assert not overlap, f"a job is both required and not-required: {sorted(overlap)}"


# --- the deliberate exclusions are asserted, not incidental ---------------- #
def test_the_four_non_required_jobs_are_a_named_choice():
    """CI-2 names these four as deliberately non-required. Pin that they (a) are
    real ci.yml jobs and (b) are on the non-required side, so demoting a required
    gate here, or a typo in a job name, cannot pass unnoticed."""
    jobs = _ci_job_names()
    for job in ("sandbox-container", "gate-wasm", "temporal-exit",
                "backend-roots-combined"):
        assert job in jobs, f"{job} is no longer a ci.yml job; update this pin"
        assert job in NOT_REQUIRED_CHECKS, (
            f"{job} was moved out of the deliberately-non-required set without "
            "updating this pin"
        )
        assert job not in REQUIRED_CHECKS
        assert NOT_REQUIRED_CHECKS[job], f"{job} exclusion has no stated reason"
