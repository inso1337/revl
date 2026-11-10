"""Issue #2420: a pre-release tag must never upload to PyPI.

`publish.yml` fires on every `v*` tag, and `check-version` requires the tag to
be "v" plus the pyproject version. A release candidate is therefore tagged
`v2.0.0rc1` (PEP 440), and before this gate existed pushing that tag started
the upload. PyPI has no delete, so the workflow classifies the version before
the irreversible step: `check-version` writes a `channel` output from
`tools/release_channel.py`, and the `publish` job runs only when that output is
`stable`.

The first block pins the classifier. The second pins the workflow wiring,
including the fail-closed shape: the job condition must compare for `stable`,
so an empty or unrecognized output skips the upload rather than publishing
something nobody classified.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from _load_by_path import load_by_path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "publish.yml"
tool = load_by_path("release_channel_tool", ROOT / "tools" / "release_channel.py")


def _workflow() -> dict:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    if True in data and "on" not in data:  # YAML 1.1 reads a bare `on` as True
        data["on"] = data.pop(True)
    return data


@pytest.mark.parametrize("version", [
    "2.0.0",
    "0.3.0",
    "2.0.0.1",
    "2.0.0.post1",
    "2.0.0+build.7",
    "v2.0.0",
])
def test_a_final_release_is_stable(version):
    assert tool.channel(version) == "stable"


@pytest.mark.parametrize("version", [
    "0.3.0rc1",
    "2.0.0rc1",
    "2.0.0a1",
    "2.0.0b2",
    "2.0.0.dev1",
    "2.0.0rc1.post1",
    "2.0.0-rc.1",
    "not-a-version",
    "",
])
def test_a_prerelease_or_unrecognized_version_is_not_stable(version):
    if not version:
        with pytest.raises(ValueError):
            tool.channel(version)
    else:
        assert tool.channel(version) == "prerelease"


def test_the_cli_prints_the_channel(capsys):
    assert tool.main(["0.3.0rc1"]) == 0
    assert capsys.readouterr().out.strip() == "prerelease"
    assert tool.main([]) == 2


def test_check_version_publishes_the_channel_output():
    check = _workflow()["jobs"]["check-version"]
    assert check["outputs"]["channel"] == (
        "${{ steps.version.outputs.channel }}")
    step = next(step for step in check["steps"] if step.get("id") == "version")
    assert 'python3 tools/release_channel.py "$proj"' in step["run"]
    assert '"$GITHUB_OUTPUT"' in step["run"]


def test_the_upload_job_runs_only_for_a_stable_channel():
    publish = _workflow()["jobs"]["publish"]
    condition = publish["if"]
    assert "needs.check-version.outputs.channel" in condition
    assert "'stable'" in condition or '"stable"' in condition
    # Fail closed: equality against `stable`, never an inequality that would
    # also run for a missing or unrecognized output.
    assert "!=" not in condition
    assert any(str(step.get("uses", "")).startswith(
        "pypa/gh-action-pypi-publish") for step in publish["steps"])


def test_no_other_job_can_upload_to_pypi():
    jobs = _workflow()["jobs"]
    uploaders = [
        name for name, job in jobs.items()
        if any(str(step.get("uses", "")).startswith(
            "pypa/gh-action-pypi-publish") for step in job.get("steps", []))
    ]
    assert uploaders == ["publish"], (
        "the PyPI upload must live in the job the stable-channel condition "
        "gates")
