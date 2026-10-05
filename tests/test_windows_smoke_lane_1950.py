"""The Windows smoke lane stays light and out of the PR path (issue #1950).

`.github/workflows/windows-smoke.yml` runs the paths that broke on Windows
(#1939, #1940, #1944) nightly and on demand. These tests pin the cost and
visibility contract: never on a pull request or a push, a cancel-in-progress
concurrency group, a 20-minute ceiling, and not a required check. They also
pin that a smoke step asserts a PASS by name, so a tier that quietly skips
cannot read as green.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
LANE = ROOT / ".github" / "workflows" / "windows-smoke.yml"


def _doc() -> dict:
    return yaml.safe_load(LANE.read_text(encoding="utf-8"))


def _triggers(doc: dict) -> dict:
    # PyYAML reads the bare key `on` as the boolean True
    return doc.get("on", doc.get(True))


def test_it_runs_nightly_and_on_demand_only():
    triggers = _triggers(_doc())
    assert set(triggers) == {"schedule", "workflow_dispatch"}, sorted(triggers)
    assert triggers["schedule"] == [{"cron": "30 4 * * *"}]


def test_a_new_run_cancels_the_one_in_progress():
    concurrency = _doc()["concurrency"]
    assert concurrency["cancel-in-progress"] is True
    assert "windows-smoke" in concurrency["group"]


def test_one_windows_job_with_a_twenty_minute_ceiling():
    jobs = _doc()["jobs"]
    assert list(jobs) == ["smoke-windows"]
    job = jobs["smoke-windows"]
    assert job["runs-on"] == "windows-latest"
    assert job["timeout-minutes"] == 20


def test_it_is_not_a_required_check():
    """Required contexts are pinned in tests/test_required_checks_pinned.py;
    no name from this lane may appear there."""
    pinned = (ROOT / "tests" / "test_required_checks_pinned.py").read_text(encoding="utf-8")
    assert "smoke-windows" not in pinned and "windows-smoke" not in pinned


def test_each_tier_step_asserts_a_pass_by_name():
    steps = _doc()["jobs"]["smoke-windows"]["steps"]
    runs = {s.get("name", ""): s.get("run", "") for s in steps}
    assert 'grep -q "^\\[ts\\] pass"' in runs["one test on the ts tier"]
    assert 'grep -q "^\\[py\\] pass"' in runs["one test on the py tier"]
    compile_step = runs["compile a source on another drive"]
    assert "$RUNNER_TEMP" in compile_step and "test -s" in compile_step


def test_the_steps_use_the_setup_scripts_own_venv():
    steps = _doc()["jobs"]["smoke-windows"]["steps"]
    text = "\n".join(s.get("run", "") for s in steps)
    assert "sh ./setup.sh" in text
    assert ".venv/Scripts/python.exe" in text
