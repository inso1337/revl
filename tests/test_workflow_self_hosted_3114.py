"""Issue #3114: a pull request's code never reaches the persistent self-hosted runner.

A workflow run for a pull request takes the workflow file from the PR's merge
commit, so a PR can change any job to `runs-on: [self-hosted, ...]` and run its
steps on the persistent box. The repository settings only gate who may start a
run; nothing in the tree stopped such an edit from merging. This file is that
check, so the edit reds on the PR that makes it.

A workflow may name a self-hosted runner only when every trigger it has fires
from code a maintainer already merged:

* `push` limited to `branches: [main]` (no tags, no other branch, no
  `branches-ignore`), `schedule` and `workflow_dispatch` qualify;
* `pull_request`, `pull_request_target`, `pull_request_review*`,
  `issue_comment`, `workflow_call` (a PR-triggered caller reaches it) and every
  other event do not.

A `runs-on` that is not a literal list of labels (an expression, a matrix
value, a runner group) cannot be shown to avoid the box, so it is treated as
self-hosted in a workflow a PR can trigger.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

#: GitHub-hosted runner labels. Anything else (including `self-hosted`, a
#: custom label, a group, an expression) may be the persistent box.
HOSTED = {"ubuntu-latest", "ubuntu-24.04", "ubuntu-22.04", "ubuntu-24.04-arm",
          "ubuntu-22.04-arm", "windows-latest", "windows-2025", "windows-2022",
          "macos-latest", "macos-15", "macos-14", "macos-13"}

#: Events that fire from a merged ref only, when constrained as described below.
MERGED_ONLY = {"push", "schedule", "workflow_dispatch"}


def _load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if True in data and "on" not in data:  # YAML 1.1 reads a bare `on` as True
        data["on"] = data.pop(True)
    return data


def _triggers(data: dict) -> dict:
    on = data["on"]
    if isinstance(on, str):
        return {on: {}}
    if isinstance(on, list):
        return {e: {} for e in on}
    return {e: (v or {}) for e, v in on.items()}


def _trigger_is_merged_only(event: str, cfg: dict) -> bool:
    if event not in MERGED_ONLY:
        return False
    if event == "push":
        return cfg.get("branches") == ["main"] and not (
            set(cfg) & {"tags", "tags-ignore", "branches-ignore"})
    return True


def _labels(runs_on) -> list | None:
    """The literal labels of a `runs-on`, or None when not a literal list."""
    if isinstance(runs_on, str):
        runs_on = [runs_on]
    if isinstance(runs_on, list) and all(isinstance(x, str) for x in runs_on):
        if not any("${{" in x for x in runs_on):
            return runs_on
    return None


def _may_be_self_hosted(runs_on) -> bool:
    labels = _labels(runs_on)
    if labels is None:
        return True
    return not (len(labels) == 1 and labels[0] in HOSTED)


def violations(data: dict) -> list[str]:
    """Jobs that may run on the box although a PR can trigger the workflow."""
    triggers = _triggers(data)
    open_events = sorted(e for e, c in triggers.items()
                         if not _trigger_is_merged_only(e, c))
    if not open_events:
        return []
    return sorted(
        f"{job}: runs-on {job_data['runs-on']!r} may be self-hosted but "
        f"{', '.join(open_events)} can trigger this workflow"
        for job, job_data in (data.get("jobs") or {}).items()
        if "runs-on" in job_data and _may_be_self_hosted(job_data["runs-on"]))


def _all() -> dict[str, dict]:
    return {p.name: _load(p) for p in sorted(WORKFLOWS.glob("*.yml"))}


@pytest.mark.parametrize("name", sorted(_all()))
def test_no_pr_reachable_job_may_run_on_the_self_hosted_runner(name):
    assert violations(_all()[name]) == []


def test_the_only_self_hosted_job_is_arm64_smoke():
    named = {n: [j for j, jd in (d.get("jobs") or {}).items()
                 if "runs-on" in jd and _may_be_self_hosted(jd["runs-on"])]
             for n, d in _all().items()}
    assert {n: j for n, j in named.items() if j} == {
        "arm64-smoke.yml": ["frontend-arm64"]}


def test_arm64_smoke_is_merged_only():
    data = _all()["arm64-smoke.yml"]
    assert {e: _trigger_is_merged_only(e, c)
            for e, c in _triggers(data).items()} == {
        "push": True, "schedule": True, "workflow_dispatch": True}


def _wf(on, runs_on):
    return {"on": on, "jobs": {"j": {"runs-on": runs_on, "steps": []}}}


@pytest.mark.parametrize("on", [
    "pull_request",
    ["push", "pull_request"],
    {"pull_request_target": {}},
    {"issue_comment": {}},
    {"workflow_call": {}},
    {"push": {"branches": ["main", "release/*"]}},
    {"push": {"branches": ["main"], "tags": ["v*"]}},
    {"push": {"tags": ["v*"]}},
    {"push": None},
    {"push": {"branches-ignore": ["dev"]}},
])
@pytest.mark.parametrize("runs_on", [
    ["self-hosted", "linux", "arm64"],
    "self-hosted",
    {"group": "box"},
    "${{ matrix.os }}",
    ["${{ vars.RUNNER }}"],
    "my-custom-label",
    ["ubuntu-latest", "self-hosted"],
])
def test_a_pr_reachable_self_hosted_job_is_flagged(on, runs_on):
    assert violations(_wf(on, runs_on)) != []


@pytest.mark.parametrize("on", [
    "pull_request",
    {"pull_request": {"paths": ["pyproject.toml"]}},
    ["push", "pull_request"],
])
@pytest.mark.parametrize("runs_on", [
    "ubuntu-latest", ["ubuntu-24.04-arm"], "windows-latest"])
def test_a_hosted_job_in_a_pr_workflow_is_fine(on, runs_on):
    assert violations(_wf(on, runs_on)) == []


def test_a_merged_only_workflow_may_use_the_box():
    on = {"push": {"branches": ["main"]}, "schedule": [{"cron": "0 0 * * *"}],
          "workflow_dispatch": None}
    assert violations(_wf(on, ["self-hosted", "linux", "arm64"])) == []
