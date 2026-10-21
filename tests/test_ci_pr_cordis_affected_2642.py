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

import os
import shutil
import subprocess
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
#: The shell GitHub Actions runs a `run:` step with on ubuntu-latest: bash is
#: the default when it is present (the documented fallback is `sh` only when
#: bash is not). The probe step carries `set -o pipefail`, bash-only syntax, so
#: the extracted body has to be executed by bash too: Ubuntu's `sh` is dash,
#: which dies at that line before reaching the branch this file pins.
PRODUCTION_SHELL = "bash"


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
    assert '--log "$log"' in script, (
        "the receipt is not handed the captured log, so it reads back nothing"
    )
    assert "|| true" not in script, (
        "a `|| true` swallows a failing guarded run or receipt, which is the "
        "silent pass issue #2642 is about"
    )
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


def _probe_script() -> str:
    return _one_run_step("needed=1")["run"]


def _probe_repo(tmp_path: Path, driver: str | None = None) -> Path:
    """A throwaway tree the probe step can run in: the real driver (or a stub),
    one test that names cordis and one that does not."""
    tree = tmp_path / "repo"
    (tree / "tools").mkdir(parents=True)
    (tree / "tests").mkdir()
    (tree / "tools" / "cordis_half.py").write_text(
        driver if driver is not None else
        (ROOT / "tools" / "cordis_half.py").read_text(encoding="utf-8"),
        encoding="utf-8")
    (tree / "tests" / "test_plain_thing.py").write_text(
        "def test_y():\n    pass\n", encoding="utf-8")
    (tree / "tests" / "test_names_cordis.py").write_text(
        "CORDIS = 'cordis'\n", encoding="utf-8")
    return tree


def _run_probe(tmp_path: Path, tree: Path, selection: str):
    """The probe step's own shell, run against `tree` under the shell the
    workflow runs it with.

    That is `bash`, the ubuntu-latest default for a `run:` step, not `sh`:
    the probe body opens with `set -euo pipefail`, and Ubuntu's `sh` is dash,
    which rejects `-o pipefail` at that line. Executing the extracted body
    under `sh` measured this test's own shell rather than production's and
    failed before the branch under test ran."""
    out = tmp_path / "github_output"
    out.write_text("", encoding="utf-8")
    shell = shutil.which(PRODUCTION_SHELL)
    assert shell is not None, (
        f"{PRODUCTION_SHELL} is not on PATH: the probe step runs under bash "
        "and carries `set -o pipefail`, so the extracted body cannot be "
        "executed faithfully by any other shell")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "SEL_PYTEST": selection, "GITHUB_OUTPUT": str(out)}
    done = subprocess.run([shell, "-c", _probe_script()], cwd=tree, env=env,
                          capture_output=True, text=True, timeout=120,
                          check=False)
    return done, out.read_text(encoding="utf-8")


def test_the_probe_false_path_is_a_visible_licensed_skip():
    """`STATUS none` is the only answer that skips the pin, the install, the
    run and the receipt, so it has to be exactly that answer and not a
    fall-through from another one, and the skip has to be visible: a green job
    whose log does not say why reads as "the guarded half ran and passed"
    (issue #2642's own failure). The negative answer is licensed by the
    structural invariant in tests/test_env_gated_skips_run_somewhere.py (every
    runtime-gated file names cordis, the token the driver selects on), and the
    notice says so where the checks UI shows it."""
    script = _probe_script()
    assert script.count('echo "needed=0" >> "$GITHUB_OUTPUT"') == 1, (
        "needed=0 is set somewhere other than the one accepted negative answer"
    )
    assert script.count('echo "needed=1" >> "$GITHUB_OUTPUT"') == 1
    accept = script.index("grep -qx 'STATUS none'")
    needed0 = script.index("needed=0")
    assert accept < needed0, (
        "the probe accepts the negative answer before reading it")
    assert script.index('if [ "$rc" = "0" ]') < needed0, (
        "the negative answer is accepted without requiring the driver's exit 0")
    notice = [line for line in script.splitlines() if "::notice::" in line]
    assert len(notice) == 1, (
        "the probe-false skip is invisible in the checks UI")
    assert "test_env_gated_skips_run_somewhere.py" in notice[0], (
        "the skip does not name the invariant that licenses the negative "
        "answer, so nothing ties the two together")
    assert "skipped" in notice[0]
    assert needed0 < script.index("::notice::") < script.index("elif"), (
        "the notice is not inside the accepted negative-answer branch")
    error = script.index("::error::")
    assert "exit 1" in script[error:], (
        "an unrecognised probe outcome falls through instead of failing")


def test_the_probe_false_path_skip_is_driven_by_the_real_shell(tmp_path):
    """The probe's shell is executed, not just read: a selection with no
    cordis file takes the visible skip, a selection that names one is
    needed=1 (no pin on a fresh tree: the driver's STATUS missing), and an
    answer that is neither is a hard error, never a skip.

    It runs under `bash`, the shell GitHub Actions gives a `run:` step on
    ubuntu-latest, and executes the step's text unmodified, `set -euo
    pipefail` included. That option is bash-only syntax: Ubuntu's `sh` is
    dash, which exits at that line, so `sh -c` ran the body under a shell
    production never uses and measured the test's own shell instead."""
    tree = _probe_repo(tmp_path / "plain")
    done, out = _run_probe(tmp_path, tree, "tests/test_plain_thing.py")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "needed=0" in out.splitlines(), out
    assert "::notice::" in done.stdout
    assert "skipped" in done.stdout

    done, out = _run_probe(tmp_path, tree, "tests/test_names_cordis.py")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "needed=1" in out.splitlines(), out
    assert "::notice::" not in done.stdout

    stub = _probe_repo(tmp_path / "stub",
                       driver='print("STATUS error unexpected answer")\n')
    done, out = _run_probe(tmp_path, stub, "tests/test_plain_thing.py")
    assert done.returncode != 0, (
        "the probe accepted an answer that is neither STATUS none nor exit 10")
    assert "::error::" in done.stdout
    assert "needed=" not in out


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
