"""Issue #2642, option B: the affected selection runs under the runtime on the PR.

`root-suite-affected` executes the selection `tools/affected_tests.py` computes
on an interpreter with no cordis-py runtime, so a selected file whose tests
guard on `cordis` skips there. The #2642 receipt makes that skip red, but a red
board is not execution; `frontend-cordis` is the job that executes the guarded
tests, and since issue #1678 it is off pull requests. The issue's own evidence
is the whole shape: the selection was one guarded file, the log read
`2 skipped in 1.10s`, and the check concluded success.

Option B closes it with one advisory, PR-only job. The pins below read ci.yml
rather than a copy of it:

  * the job is the plain pull-request gate, with no `needs:` a pull request can
    skip, so it cannot report `skipping` on a PR (issue #854's failure class);
  * the selection is the selector's answer, never a hardcoded file list;
  * the pinned fork is installed ONLY when `tools/cordis_half.py` says the
    selection names cordis, so a selection with no guarded file pays neither
    the clone nor the runtime (`STATUS none` is the allowed empty answer);
  * a FULL answer is bounded out, not run: the whole guarded suite is 455
    files and 50 minutes on a hosted runner, so the job runs named selections
    only and says so with a notice and a job summary rather than passing
    silently (`root-suite-affected` runs that selection on the PR,
    `frontend-cordis` runs the guarded suite on the merge queue);
  * a named selection provisions the go and java pins `frontend-cordis`
    carries, so a cross-tier probe in the half measures the pinned toolchain
    instead of red-ing the job for its own missing one;
  * the guarded half runs under the pinned interpreter and the run is read back
    by `tools/affected_pytest_receipt.py`, which fails an all-skipped run.

The split itself (pull-request-only, advisory) is pinned where every job is
classified: `tests/test_ci_pr_queue_split_1678.py` and
`tests/test_required_checks_pinned.py`.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
JOB = "cordis-affected"
PR_ONLY = "${{ github.event_name == 'pull_request' }}"
NEEDED = "${{ steps.probe.outputs.needed == '1' }}"
FULL = "${{ steps.select.outputs.full == '1' }}"
#: The condition every provisioning and run step carries: there is a cordis
#: file AND the selector did not answer FULL.
RUNS = ("${{ steps.probe.outputs.needed == '1' && "
        "steps.select.outputs.full != '1' }}")
SELECTION = "${{ steps.select.outputs.pytest }}"


def _norm(cond) -> str:
    return " ".join(str(cond or "").split())


def _job() -> dict:
    jobs = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]
    assert JOB in jobs, (
        f"ci.yml no longer defines {JOB}: a cordis-guarded selected file is "
        "skipped on every pull request again, which is issue #2642"
    )
    return jobs[JOB]


def _steps() -> list[dict]:
    return [step for step in _job().get("steps") or [] if isinstance(step, dict)]


def _one_run_step(needle: str) -> dict:
    found = [step for step in _steps() if needle in (step.get("run") or "")]
    assert len(found) == 1, (
        f"expected exactly one step running {needle!r}, found {len(found)}; the "
        "pin below cannot identify the step it means"
    )
    return found[0]


def test_the_job_is_the_plain_pull_request_gate():
    """`frontend-cordis`'s `!= 'pull_request'` is the shape that created the
    gap, and a `changes`-routed gate is the shape issue #854 forbids on a
    coverage job: a skipped context is indistinguishable from a pass."""
    spec = _job()
    cond = " ".join(str(spec.get("if") or "").split())
    assert cond == PR_ONLY, (
        f"{JOB} no longer carries the plain pull-request gate (found {cond!r}); "
        "a heavier condition moves the guarded selection off the PR board"
    )
    assert not spec.get("needs"), (
        f"{JOB} depends on another job; a skipped dependency would skip this "
        "one too, and the guarded selection would be unexecuted with no red"
    )


def test_the_checkout_keeps_history_for_the_selector_and_the_selected_tests():
    """A shallow clone breaks the selector's ancestry answer and makes the
    real-history tests FAIL by name (issue #1434), so the checkout must keep
    the full history the other affected jobs keep."""
    checkout = [step for step in _steps()
                if str(step.get("uses") or "").startswith("actions/checkout@")]
    assert len(checkout) == 1, f"expected one checkout step, found {len(checkout)}"
    assert (checkout[0].get("with") or {}).get("fetch-depth") == 0, (
        "the checkout is shallow again; the selector and the selected "
        "real-history tests need the full history"
    )


def test_the_selection_is_the_selectors_answer_not_a_hardcoded_list():
    select = _one_run_step("tools/affected_tests.py --format machine")
    assert select.get("id") == "select", "the selection step lost its id"
    script = select["run"]
    # the base comes through env:, never interpolated source
    assert select.get("env", {}).get("BASE_REF") == "${{ github.base_ref }}"
    assert 'git fetch --no-tags origin' in script
    assert '--base "origin/${BASE_REF}"' in script
    # a failed selector must degrade to FULL, never to "run nothing"
    assert "FULL 1" in script and "PYTEST tests/" in script
    # and the selector's answer is what the job publishes and consumes
    assert 'echo "pytest=$nodes" >> "$GITHUB_OUTPUT"' in script
    assert "nodes=\"tests/\"" in script, (
        "the empty-selection fail-safe was removed; an empty node list must "
        "still degrade to the whole tree"
    )
    # the FULL marker is published too, and the whole-suite target is the same
    # bound when the fail-safe produced it rather than the selector
    assert "sed -n 's/^FULL //p'" in script, (
        "the job no longer reads the selector's FULL marker, so it cannot "
        "tell a whole-suite answer from a named one"
    )
    assert 'if [ "$nodes" = "tests/" ]; then' in script, (
        "the whole-suite target is only recognised when the selector labels "
        "it FULL; the empty-selection fail-safe produces the same target"
    )
    assert 'echo "full=${full:-0}" >> "$GITHUB_OUTPUT"' in script
    for step in _steps():
        assert "tests/test_" not in (step.get("run") or ""), (
            "the selection is hardcoded in the workflow; issue #2642's job must "
            "reuse tools/affected_tests.py so every diff gets its own answer"
        )


def test_the_pin_is_installed_only_when_the_selection_names_cordis():
    """The cheap half of option B: `STATUS none` from the filter means nothing
    to install and nothing to run; only a named selection that names cordis
    pays the clone, the setup and the runtime. A FULL selection is bounded out
    (see below), so it pays none of that either."""
    probe = _one_run_step("needed=1")
    assert probe.get("id") == "probe"
    assert probe.get("env", {}).get("SEL_PYTEST") == SELECTION
    script = probe["run"]
    assert "tools/cordis_half.py --select" in script, (
        "the probe no longer asks the filter that runs the selection"
    )
    assert "grep -qx 'STATUS none'" in script
    assert "needed=0" in script and "needed=1" in script
    assert "exit 1" in script, (
        "an unrecognised probe outcome must fail, not fall through to a pass"
    )

    setup = _one_run_step("sh backends/python/setup.sh")
    assert _norm(setup.get("if")) == _norm(RUNS), (
        "the pinned fork is installed unconditionally again; a selection with "
        "no cordis file would pay the clone and the runtime"
    )
    uv = [step for step in _steps()
          if str(step.get("uses") or "").startswith("astral-sh/setup-uv@")]
    assert len(uv) == 1 and _norm(uv[0].get("if")) == _norm(RUNS), (
        "setup-uv must follow the same condition as the pin it provisions"
    )


def test_the_guarded_half_runs_under_the_pin_before_the_receipt():
    run_step = _one_run_step("--python backends/python/.venv/bin/python")
    assert _norm(run_step.get("if")) == _norm(RUNS), (
        "the step that actually runs the guarded tests is skippable by some "
        "other condition; it must run exactly when the probe found work and "
        "the selector did not answer FULL"
    )
    assert run_step.get("env", {}).get("SEL_PYTEST") == SELECTION
    script = run_step["run"]
    assert "tools/cordis_half.py --select" in script
    assert 'tee "$log"' in script, "the run's output is not captured for the receipt"
    assert '--pytest-exit "$rc"' in script
    assert '--selection "$SEL_PYTEST"' in script
    assert script.index("tools/cordis_half.py") < script.index(
        "tools/affected_pytest_receipt.py"), (
        "the receipt is invoked before the guarded run, so it reads nothing"
    )
    assert not run_step.get("continue-on-error"), (
        "the receipt step can no longer fail the job, so an all-skipped "
        "guarded selection would read green"
    )


def test_a_full_selection_is_bounded_out_and_says_so():
    """A FULL answer is the whole suite: on this job's first run its guarded
    half was 455 files, 50 minutes, and 116 go-toolchain failures, a second
    `frontend-cordis` on every PR that touches this workflow (issue #1678's
    cost). So the job runs a NAMED selection only, and the bound is loud: an
    install that still ran, or a green step with no notice, would be issue
    #2642's silent pass again, one layer up."""
    notice = [step for step in _steps() if _norm(step.get("if")) == _norm(FULL)]
    assert len(notice) == 1, (
        "a FULL selection has no dedicated step, so the bound is silent")
    script = notice[0].get("run") or ""
    assert "::notice::" in script, (
        "the FULL skip is not visible in the checks UI")
    assert "GITHUB_STEP_SUMMARY" in script, (
        "the FULL skip writes no job summary, so its green step reads as a run")
    assert "root-suite-affected" in script and "frontend-cordis" in script, (
        "the notice must name where the selection and the guarded suite do run")

    # the provisioning and the run are behind the bound too, not just the
    # notice: a step that still runs on FULL would make the notice a lie.
    for prefix in ("actions/setup-go@", "actions/setup-java@",
                   "astral-sh/setup-uv@"):
        found = [step for step in _steps()
                 if str(step.get("uses") or "").startswith(prefix)]
        assert len(found) == 1, f"expected one {prefix} step, found {len(found)}"
        assert _norm(found[0].get("if")) == _norm(RUNS), (
            f"{prefix} is not bounded out on a FULL selection")
    assert _norm(_one_run_step("sh backends/python/setup.sh").get("if")) \
        == _norm(RUNS)
    assert _norm(_one_run_step("--python backends/python/.venv/bin/python")
                 .get("if")) == _norm(RUNS)

    # anti-vacuity: the bounded condition is NOT the unbounded one, so dropping
    # `full != '1'` reds here rather than passing quietly.
    assert _norm(RUNS) != _norm(NEEDED)
    assert _norm("${{ steps.select.outputs.full != '1' }}") != _norm(RUNS)


def test_a_named_selection_provisions_the_pinned_go_and_java():
    """A named selection can contain cross-tier probes (`test_selfhost_emit_go`
    is in the half), so the job needs the go and java pins `frontend-cordis`
    carries. Without them the first run's 116 failures were all the runner's
    own go toolchain, and the job would red a PR for a toolchain it never
    installed instead of for the change.

    The VERSIONS are pinned, not just the actions: a setup-go with no
    `go-version` (or the runner's default) is the same bug with extra steps,
    so each step must carry exactly what `frontend-cordis` carries."""
    jobs = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]

    def step_using(job: str, prefix: str) -> dict:
        found = [step for step in (jobs[job].get("steps") or [])
                 if str(step.get("uses") or "").startswith(prefix)]
        assert len(found) == 1, (
            f"expected one {prefix} step in {job}, found {len(found)}")
        return found[0]

    for prefix in ("actions/setup-go@", "actions/setup-java@"):
        ours = step_using(JOB, prefix).get("with") or {}
        cover = step_using("frontend-cordis", prefix).get("with") or {}
        assert ours == cover, (
            f"{prefix} does not carry frontend-cordis's pinned versions "
            f"({ours!r} != {cover!r}); a cross-tier probe in the selection "
            "would measure the runner's toolchain, which is the 116-failure "
            "bug this job had")
    assert step_using(JOB, "actions/setup-go@")["with"]["go-version"] == "1.26"
    assert step_using(JOB, "actions/setup-java@")["with"] == {
        "distribution": "temurin", "java-version": "21"}

    required = _norm((_job().get("env") or {}).get("REVL_REQUIRE_TIERS")).split()
    assert "go" in required, (
        "a job that installs go must require it on its pytest steps (issue "
        "#2264), or a lost toolchain skips the go tests and stays green")


def test_the_pr_only_shape_predicate_rejects_the_job_it_replaced():
    """Anti-vacuity: the condition this pin insists on is not the condition
    `frontend-cordis` carries, so a revert to the heavy-job gate reds here."""
    def is_pr_only(cond) -> bool:
        return " ".join(str(cond or "").split()) == PR_ONLY

    assert is_pr_only(_job().get("if"))
    assert not is_pr_only("${{ github.event_name != 'pull_request' }}")
    assert not is_pr_only(
        "${{ github.event_name == 'pull_request' && "
        "needs.changes.outputs.frontend == 'true' }}")
    assert not is_pr_only(None)
