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

IMPORTANT: REQUIRED_CHECKS is INTENT, and it is NOT the live enforcement set.
Read against `repos/inso1337/revl/branches/main/protection` on 2026-09-10, the
required status check contexts on `main` are SEVEN:

    lint, backend-python, backend-typescript, backend-wasm, backend-rust,
    backend-java, backend-go

and rulesets are empty. So `frontend`, `frontend-cordis`, `conformance` and
`formal` are marked required HERE and are NOT enforced by the server, and no
unit test can tell the two apart without credentials. That gap is part of why
PR #850 (`e6067cd1`) reached `main` with the root suite uncollected: its
`frontend` job was skipped AND `frontend` is not a required context, so even a
FAILED `frontend` would not have blocked the merge (`site-wheel-drift` and
`frontend-arm64` did fail on it, and both are non-required). Read
REQUIRED_CHECKS as the partition `main` is intended to enforce, read
ENFORCED_TODAY below as the last verified server-side reading, and update both
when branch protection changes. The command is
`gh api repos/inso1337/revl/branches/main/protection`.

NOTE on the "13 required checks" figure in ci.yml's merge-queue comment: this
partition marks 11 jobs intended-required (7 of them enforced) and 7
non-required. The two are reconciled at the branch-protection settings, which
are out of tree; whichever is stale, this test at least makes the job-name side
of the contract explicit and drift-proof. It reads ci.yml as text, so it needs
no PyYAML (not a declared dependency) and rides the frontend job's plain
`pytest tests/ -q`, like tests/test_site_wheel_gate_runs_in_ci.py.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"

# The contexts branch protection on `main` actually enforces, verified against
# the API on 2026-09-10 (`gh api repos/inso1337/revl/branches/main/protection`,
# strict=false, enforce_admins=false, no rulesets). Seven contexts, and none of
# them is a root-suite job.
ENFORCED_TODAY = frozenset({
    "lint",
    "backend-python",
    "backend-typescript",
    "backend-wasm",
    "backend-rust",
    "backend-java",
    "backend-go",
})

# Intended to gate a merge, NOT enforced by the server as of the read above. The
# 3-version root-suite matrix and the two conformance lanes live here, which is
# the more dangerous half of the #854 incident: a job in this set cannot stop a
# merge, and a `skipped` job in this set reads as a `success` anyway.
ASPIRATIONAL = frozenset({
    "frontend",
    "frontend-cordis",
    "conformance",
    "formal",
})

# Every ci.yml job that branch protection is intended to require before a merge.
REQUIRED_CHECKS = ENFORCED_TODAY | ASPIRATIONAL

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
    # Live microVM boot: runs on a hosted `ubuntu-latest` runner (which exposes
    # /dev/kvm), building a small guest kernel + rootfs and booting it. Heavier
    # and slower than the other lanes, so it is run for signal, not a hard merge
    # gate; deliberately kept out of branch protection.
    "sandbox-microvm": "live KVM boot on a hosted runner; not a hard merge gate",
    # Path-filter gate (item, PR #765): decides whether the frontend/cordis/
    # conformance/formal jobs run on a given PR. Pure routing over `git diff`;
    # it gates nothing itself and is never a merge blocker.
    "changes": "path-filter router for the gated frontend jobs; not a gate",
    # Issue #854: the unconditional owner of root-suite coverage. `frontend` and
    # `frontend-cordis` are the fast path for the 3-version matrix, and a diff
    # that misses the fast-path filter used to leave the root suite collected by
    # no job at all. This one runs the selection `tools/affected_tests.py`
    # computes, on one interpreter, for every diff, and has no `if:` so it can
    # never report `skipping`. Kept out of branch protection on purpose for now:
    # it is coverage insurance rather than the gate itself, and a required
    # context must already exist on `main` before it can be required (requiring
    # it before this merges blocks every PR forever on a check no run reports).
    # The follow-up, once this is on `main`, is a read-modify-write of the
    # contexts list:
    #   gh api repos/inso1337/revl/branches/main/protection/required_status_checks
    #   gh api -X PATCH repos/inso1337/revl/branches/main/protection/required_status_checks \
    #     --input - <<< '{"strict":false,"contexts":[...ENFORCED_TODAY...,"root-suite-affected"]}'
    "root-suite-affected": "issue #854 unconditional root-suite coverage; promotion is a branch-protection change, out of tree",
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


# --- intent is not enforcement --------------------------------------------- #
def test_the_enforced_partition_is_not_read_as_the_enforced_reality():
    """Issue #854: a list marked "required" that the server does not enforce is
    the same false-assurance class as a `skipped` job that branch protection
    reads as `success`. Pin the split so REQUIRED_CHECKS cannot be mistaken for
    the live gate, and so a promotion or a demotion has to be made on purpose
    (update ENFORCED_TODAY and the docstring together with the API call)."""
    assert ENFORCED_TODAY | ASPIRATIONAL == REQUIRED_CHECKS
    assert not (ENFORCED_TODAY & ASPIRATIONAL)
    # Verified on 2026-09-10: none of the root-suite matrix is a required
    # context, and neither conformance lane is. If that changes, this reds and
    # forces the split to be re-read from the API rather than guessed.
    for job in ("frontend", "frontend-cordis", "conformance", "formal"):
        assert job in ASPIRATIONAL, f"{job} moved without re-reading the API"
        assert job not in ENFORCED_TODAY, (
            f"{job} is not a required context on main as of 2026-09-10; if it was "
            "promoted, move it to ENFORCED_TODAY and update the docstring"
        )
    assert ENFORCED_TODAY == frozenset({
        "lint", "backend-python", "backend-typescript", "backend-wasm",
        "backend-rust", "backend-java", "backend-go",
    }), "the 7 enforced contexts changed; re-read branch protection and update"


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
