"""Issue #2872: the push-to-main workflows do not hold the runner pool twice.

A push to main starts several workflows against one runner pool. Only a
concurrency group bounds a workflow, so each workflow that runs on a branch push
must declare one, and the groups must not interfere:

* The group is keyed on the ref (a newer push to the same ref supersedes the
  older tip's run) or is one fixed name (`pages`, which serialises its own
  deployments).
* No two workflows share a group name. GitHub concurrency groups are
  repository-wide and hold one running and ONE pending run; two workflows in
  one group would cancel each other's pending run for the SAME tip, which makes
  a security or packaging check silently unreachable. A shared group across
  workflows is therefore the hazard this file guards against, not a goal.
* A ref-keyed group also names its workflow, so the ref alone is never the
  whole key.

Tag-only triggers (`publish.yml`) are a release, not a branch push, and are out
of scope: a release must never be superseded.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
EXPR = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


def _load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if True in data and "on" not in data:  # YAML 1.1 reads a bare `on` as True
        data["on"] = data.pop(True)
    return data


def _all() -> dict[str, dict]:
    return {p.name: _load(p) for p in sorted(WORKFLOWS.glob("*.yml"))}


def _branch_push(data: dict) -> bool:
    on = data["on"]
    if isinstance(on, str):
        return on == "push"
    if isinstance(on, list):
        return "push" in on
    if "push" not in on:
        return False
    push = on["push"] or {}
    return "branches" in push or "tags" not in push


def _group(data: dict) -> str | None:
    conc = data.get("concurrency")
    if isinstance(conc, dict):
        return conc.get("group")
    return conc


def _resolved(name: str, data: dict) -> str:
    """The group with `github.workflow` replaced by the workflow's name."""
    group = _group(data)
    workflow = data.get("name", f".github/workflows/{name}")  # GitHub's default
    return EXPR.sub(
        lambda m: workflow if m.group(1) == "github.workflow"
        else "${{ " + m.group(1) + " }}",
        group).lower()  # group names compare case-insensitively


PUSHERS = sorted(n for n, d in _all().items() if _branch_push(d))


def test_the_expected_workflows_run_on_a_branch_push():
    assert PUSHERS == ["arm64-smoke.yml", "ci.yml", "codeql.yml", "pages.yml",
                       "site-wheel.yml"]


def test_x64_smoke_workflow_stays_deleted():
    """x64-smoke had no registered runner, so its jobs only queued (issue #2872).

    Re-adding it is a deliberate change: register a runner for its labels
    (`self-hosted,X64,azure,light`) and change this test in the same commit.
    """
    assert not (WORKFLOWS / "x64-smoke.yml").exists()
    assert "x64-smoke.yml" not in _all()


@pytest.mark.parametrize("name", PUSHERS)
def test_push_workflow_declares_a_ref_or_fixed_group(name):
    data = _all()[name]
    group = _group(data)
    assert group, f"{name} runs on a push to main with no concurrency group"
    exprs = EXPR.findall(group)
    assert all(e in ("github.ref", "github.workflow") for e in exprs), (name, group)
    if exprs:
        assert "github.ref" in exprs, (name, group)
        literal = EXPR.sub("", group).strip("-_ ")
        assert literal or "github.workflow" in exprs, (
            f"{name}: group {group!r} is the bare ref, which another workflow "
            "could share")


def test_no_two_workflows_share_a_group():
    seen: dict[str, str] = {}
    for name, data in _all().items():
        if _group(data) is None:
            continue
        key = _resolved(name, data)
        assert key not in seen, (
            f"{name} and {seen[key]} share concurrency group {key!r}: GitHub "
            "keeps one pending run per group, so they would cancel each "
            "other's run of the same tip")
        seen[key] = name


def test_site_wheel_supersedes_an_older_tip():
    """Nothing consumes its output (pages builds its own wheel), so cancelling is safe."""
    conc = _all()["site-wheel.yml"]["concurrency"]
    assert conc["cancel-in-progress"] is True
