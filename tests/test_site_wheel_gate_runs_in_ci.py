"""The site-wheel drift check must have an automatic owner (issue #252).

`site/vendor/revl-2.0.0-py3-none-any.whl` and its playground twin vendor the
whole of `src/revl`, so they go stale on any change to a bundled module.
`tools/check_site_wheel.py` is deliberately not a per-PR gate — as one it
reddened CI on every source change — and the merge-time assignment it was moved
to had no owner under the actual flow (agents run targeted tests and open a PR;
the pipeline merges on green; nobody runs `make pre-merge`). The wheel was found
stale three times in one day, always by accident.

`.github/workflows/site-wheel.yml` is the owner: it runs the check on every push
to main and weekly, and fails there. This file is what stops that from quietly
regressing in either direction, and neither claim can be made from inside the
workflow itself:

1. The detector still exists and still runs the CHECK form of the tool. A job
   rewritten to `--write` would rebuild the wheel on a throwaway runner and
   report success forever, which is worse than no job at all.
2. It stays OFF the per-PR path. Wiring it into `ci.yml`, or giving this
   workflow a `pull_request` trigger, reintroduces the outage class 110c
   removed it for.

Static, like `tests/test_ts_typecheck_gate_runs_in_ci.py` and
`tests/test_java_javac_gate_runs_in_ci.py`: it reads the workflows as text, so
it needs no toolchain and no PyYAML (not a declared dependency) and rides the
`frontend` job's plain `pytest tests/ -q`. It does NOT build a wheel, so it
costs a PR nothing and cannot itself become the per-PR gate this is about.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
DRIFT = WORKFLOWS / "site-wheel.yml"
CI = WORKFLOWS / "ci.yml"
TOOL = "tools/check_site_wheel.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _run_steps(text: str) -> list[str]:
    return [m.strip() for m in re.findall(r"run:\s*(.+)", text)]


def _triggers(path: Path) -> str:
    """The workflow's `on:` block as YAML, with comment lines removed.

    Comments are stripped because this file's claims are about what the
    workflow DOES, and site-wheel.yml's header comment explains at length why
    it has no `pull_request` trigger — a substring search over the raw text
    would read that explanation as the thing it warns against."""
    head = path.read_text(encoding="utf-8").split("jobs:", 1)[0]
    lines = [ln for ln in head.splitlines() if not ln.lstrip().startswith("#")]
    return "\n".join(lines)


# --- anti-vacuity ---------------------------------------------------------- #
def test_the_checker_still_exists():
    """Everything below is a claim about a workflow that runs this tool. If the
    tool is gone, re-derive this file against whatever replaced it rather than
    passing vacuously."""
    tool = ROOT / TOOL
    assert tool.is_file(), f"{TOOL} is gone"
    src = _text(tool)
    assert "--write" in src, (
        f"{TOOL} no longer offers `--write`, which is the documented fix for "
        "a red drift job."
    )


def test_the_wheel_is_committed_in_both_vendor_dirs():
    """The artifact the drift job is about. Both copies are committed:
    `site/build.py` copies playground/vendor into site/vendor, and the check
    covers both."""
    for vendor in ("playground", "site"):
        found = sorted((ROOT / vendor / "vendor").glob("revl-*.whl"))
        assert found, (
            f"{vendor}/vendor/ has no committed revl wheel. The playground "
            "boots the compiler from it under Pyodide."
        )


# --- half 1: the detector exists and detects ------------------------------- #
def test_a_workflow_owns_the_drift_check():
    assert DRIFT.is_file(), (
        ".github/workflows/site-wheel.yml is gone. The committed wheel is "
        "then the only generated artifact in this repo with no automatic "
        "drift detection, which is issue #252 reopened. If the check moved, "
        "point this test at its new home."
    )


def test_the_drift_job_runs_the_checker_in_check_mode():
    steps = _run_steps(_text(DRIFT))
    checks = [s for s in steps if TOOL in s]
    assert checks, (
        f"site-wheel.yml no longer runs {TOOL}; it detects nothing."
    )
    for step in checks:
        assert "--write" not in step, (
            "site-wheel.yml runs the checker with `--write`. That rebuilds "
            "the wheel on a throwaway runner and throws it away, so the job "
            "passes forever while the committed wheel rots. The job must run "
            "the CHECK form and fail."
        )


def test_the_drift_job_runs_after_a_merge_to_main():
    """Post-merge on main is the whole design: it is where the tree that ships
    to Pages lives, and it is an event no agent has to remember."""
    head = _triggers(DRIFT)
    assert re.search(r"^\s*push:\s*$", head, re.M), (
        "site-wheel.yml no longer triggers on push. A schedule alone means "
        "drift can sit on main for up to a week, and a `workflow_dispatch` "
        "alone is exactly the 'someone remembers to run it' ownership that "
        "issue #252 is about."
    )
    assert re.search(r"branches:\s*\[\s*main\s*\]", head), (
        "site-wheel.yml's push trigger no longer names main."
    )


def test_the_drift_job_does_not_swallow_its_own_failure():
    """`|| true`, `continue-on-error` or an `if: false` turns the owner back
    into a note nobody reads."""
    text = _text(DRIFT)
    assert "continue-on-error" not in text, (
        "site-wheel.yml sets continue-on-error; a drift would then be a green "
        "run with a warning."
    )
    for step in _run_steps(text):
        if TOOL in step:
            assert "|| true" not in step and "|| :" not in step, (
                f"the {TOOL} step swallows its exit status."
            )


# --- half 2: it stays off the per-PR critical path ------------------------- #
def test_the_drift_workflow_never_runs_on_a_pull_request():
    """The reason 110c took this off every push: the wheel vendors all of
    src/revl, so a per-PR gate reds any PR touching a bundled module — an
    outage class, not a defect. A `pull_request` trigger here would also add a
    check name to every PR, and branch protection matches on names."""
    head = _triggers(DRIFT)
    assert "pull_request" not in head, (
        "site-wheel.yml now triggers on pull_request. That puts the wheel "
        "rebuild back on the per-PR critical path, which is the outage class "
        "roadmap 110c removed it for. Post-merge detection on main is the "
        "deliberate trade: drift is found automatically, and no PR is red for "
        "not having rebuilt a deploy artifact."
    )


def test_ci_yml_does_not_gate_pull_requests_on_the_wheel():
    """Same rule for the matrix workflow, which does run on pull_request."""
    for step in _run_steps(_text(CI)):
        assert TOOL not in step, (
            f"ci.yml runs {TOOL}. ci.yml runs on pull_request, so this makes "
            "the wheel a per-PR gate again (issue #252 / roadmap 110c). The "
            "drift check belongs in site-wheel.yml, which is main-only."
        )


def test_the_affected_selector_still_maps_src_revl_to_the_wheel_gate():
    """The local half of the same ownership: `make pre-merge-affected` selects
    the `site-wheel` gate whenever `src/revl/**.py` changes, so an agent that
    runs the affected gate sees the drift before it lands. The CI job is the
    backstop for the flow where nobody runs it, not a replacement."""
    src = _text(ROOT / "tools" / "affected_tests.py")
    assert '"site-wheel"' in src, (
        "tools/affected_tests.py no longer selects a `site-wheel` gate, so "
        "`make pre-merge-affected` no longer checks the wheel on a src/revl "
        "change and the post-merge job is the only detection left."
    )
