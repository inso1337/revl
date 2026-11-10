"""Issue #3115 (regression of #551): a `v*` tag push must not publish unreviewed.

The repository settings are the owner's half: a tag ruleset on `refs/tags/v*`
and a `pypi` environment with a required reviewer and a `v*` deployment rule.
This file pins the workflow half in `.github/workflows/publish.yml`:

* `github-release` declares `environment: pypi`, so the reviewer gate binds to
  the GitHub release the way it already binds to the PyPI upload;
* `check-version` refuses a tag whose commit is not an ancestor of
  `origin/main`, so an account with write access needs a reviewed commit on
  main before a tag can reach a release.

The ancestry check runs before `check-version` writes its `channel` output, so
a tag off main leaves that output empty and both release jobs skip.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "publish.yml"


def _workflow() -> dict:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    if True in data and "on" not in data:  # YAML 1.1 reads a bare `on` as True
        data["on"] = data.pop(True)
    return data


def _environment_name(job: dict) -> str | None:
    environment = job.get("environment")
    if isinstance(environment, str):
        return environment
    if isinstance(environment, dict):
        return environment.get("name")
    return None


def test_every_release_job_waits_for_the_reviewer_gate():
    jobs = _workflow()["jobs"]
    assert _environment_name(jobs["publish"]) == "pypi"
    release = jobs["github-release"]
    assert _environment_name(release) == "pypi", (
        "the GitHub release must wait for the pypi environment too: a tag "
        "push that never passed review must not create an unreviewed release")
    # Name only. A url here would point at a PyPI project this job never
    # uploads to, and the gate binds to the name regardless.
    assert "url" not in release["environment"]


def test_check_version_refuses_a_tag_that_is_not_on_main():
    steps = _workflow()["jobs"]["check-version"]["steps"]
    runs = [str(step.get("run", "")) for step in steps]
    text = "\n".join(runs)
    assert "git fetch --no-tags origin main" in text, (
        "the default checkout fetches only the tag, so main must be fetched")
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' in text, (
        "a tag off main must not reach the release jobs")
    assert '"tag is not on main"' in text and "exit 1" in text
    # The check must precede the channel output: a refusal before `channel` is
    # written leaves it empty, so the release jobs skip instead of running.
    ancestry = next(
        i for i, run in enumerate(runs) if "merge-base --is-ancestor" in run)
    channel = next(
        i for i, run in enumerate(runs) if "release_channel.py" in run)
    assert ancestry < channel
