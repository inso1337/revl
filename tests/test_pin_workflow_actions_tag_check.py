"""`tools/pin_workflow_actions.py --check` fails a pin that is not its tag's commit.

The GitHub API is stubbed, so this is hermetic. The stub answers
`/commits/{sha}` for every sha it is told is a commit (as the real endpoint does
for a commit that lives in a fork of the repository), which is the case the old
check could not tell apart from the pinned release's own commit.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

RIGHT = "a" * 40
WRONG = "b" * 40
TAG_OBJECT = "c" * 40


def _load():
    spec = importlib.util.spec_from_file_location(
        "pin_workflow_actions", ROOT / "tools" / "pin_workflow_actions.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(monkeypatch, tmp_path, capsys, uses, refs, extra_args=()):
    """Run --check over one workflow line against a stubbed API."""
    module = _load()
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "x.yml").write_text(f"jobs:\n  j:\n    steps:\n      - uses: {uses}\n")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "WORKFLOWS", workflows)

    def fake_get(path):
        if path.startswith("/repos/actions/checkout/commits/"):
            return {"sha": path.rsplit("/", 1)[1]}  # any sha is "a commit"
        if path.startswith("/repos/actions/checkout/tags"):
            return []
        return refs.get(path)

    monkeypatch.setattr(module, "_get", fake_get)
    code = module.main(["--check", *extra_args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _tag_ref(tag, obj_type, sha):
    return {f"/repos/actions/checkout/git/ref/tags/{tag}": {"object": {"type": obj_type, "sha": sha}}}


def test_a_pin_that_is_not_the_tags_commit_fails(monkeypatch, tmp_path, capsys):
    refs = _tag_ref("v5.1.0", "commit", RIGHT)
    code, out, err = _run(monkeypatch, tmp_path, capsys, f"actions/checkout@{WRONG}  # v5.1.0", refs)
    assert code == 1
    assert "not the commit of v5.1.0" in err
    assert RIGHT in err
    assert "every pin is a commit" not in out


def test_a_pin_that_is_the_tags_commit_passes(monkeypatch, tmp_path, capsys):
    refs = _tag_ref("v5.1.0", "commit", RIGHT)
    code, out, err = _run(monkeypatch, tmp_path, capsys, f"actions/checkout@{RIGHT}  # v5.1.0", refs)
    assert (code, err) == (0, "")
    assert "1 pin(s) match the commit of the tag" in out


def test_an_annotated_tag_is_peeled_before_the_comparison(monkeypatch, tmp_path, capsys):
    refs = _tag_ref("v5.1.0", "tag", TAG_OBJECT)
    refs[f"/repos/actions/checkout/git/tags/{TAG_OBJECT}"] = {"object": {"type": "commit", "sha": RIGHT}}
    ok = _run(monkeypatch, tmp_path / "ok", capsys, f"actions/checkout@{RIGHT}  # v5.1.0", refs)
    bad = _run(monkeypatch, tmp_path / "bad", capsys, f"actions/checkout@{WRONG}  # v5.1.0", refs)
    assert ok[0] == 0
    assert bad[0] == 1 and "not the commit of v5.1.0" in bad[2]


def test_a_tag_that_does_not_exist_fails(monkeypatch, tmp_path, capsys):
    code, _, err = _run(monkeypatch, tmp_path, capsys, f"actions/checkout@{RIGHT}  # v5.1.0", {})
    assert code == 1
    assert "has no tag v5.1.0" in err


def test_the_exact_tag_is_taken_from_a_two_part_comment(monkeypatch, tmp_path, capsys):
    refs = _tag_ref("v7.6.0", "commit", RIGHT)
    code, out, _ = _run(monkeypatch, tmp_path, capsys, f"actions/checkout@{RIGHT}  # v7 (v7.6.0)", refs)
    assert code == 0 and "1 pin(s) match" in out
    wrong = _run(monkeypatch, tmp_path / "w", capsys, f"actions/checkout@{WRONG}  # v7 (v7.6.0)", refs)
    assert wrong[0] == 1


@pytest.mark.parametrize("comment", ["stable branch @ 2026-08-05", "v4", "no tag here", "v1.2.3-rc1", "v1.2.3.4"])
def test_a_comment_with_no_exact_tag_is_reported_not_failed(monkeypatch, tmp_path, capsys, comment):
    code, out, err = _run(monkeypatch, tmp_path, capsys, f"actions/checkout@{RIGHT}  # {comment}", {})
    assert (code, err) == (0, "")
    assert "unchecked:" in out
    assert "0 pin(s) match" in out


def test_the_failure_does_not_need_strict(monkeypatch, tmp_path, capsys):
    """The old behaviour: a wrong pin failed only under --strict."""
    refs = _tag_ref("v5.1.0", "commit", RIGHT)
    code, _, _ = _run(monkeypatch, tmp_path, capsys, f"actions/checkout@{WRONG}  # v5.1.0", refs)
    assert code == 1
