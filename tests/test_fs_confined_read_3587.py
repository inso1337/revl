"""The public confined READ entry point `open_confined_read` (#3587).

`backends/python/revl_fs_workspace.py` had `open_confined_write` but no read
counterpart, so a host extern reading a workspace file without following a
late-swapped symlink had to call the private `_split`/`_open_dirfd` itself.
`open_confined_read(path)` now does the same walk for reading: resolve, walk
to the parent through directory fds with `O_NOFOLLOW`, open the leaf
`O_RDONLY | O_NOFOLLOW | O_NONBLOCK`, then `fstat`.

The swap tests pin the check-to-syscall window deterministically:
`resolve_within` is snapshotted on the real file (what the check admitted),
then the leaf — or an intermediate directory — is replaced with a symlink to
a canary OUTSIDE the root, and only then is `open_confined_read` called with
the snapshot in place of the live resolve, exactly the window a concurrent
writer races into. The fd walk must refuse both; the canary must not be read.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
import revl_fs_workspace as ws  # noqa: E402

_CANARY = "TOP SECRET canary OUTSIDE the workspace root\n"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "artifact.txt").write_text("v1", encoding="utf-8")
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(root))
    return root


@pytest.fixture
def outside(tmp_path):
    d = tmp_path / "outside"
    d.mkdir()
    (d / "canary.txt").write_text(_CANARY, encoding="utf-8")
    return d


def _snapshot_resolve(monkeypatch, real: str):
    """Freeze `resolve_within` at `real` — the state the membership check
    admitted BEFORE the swap lands."""
    monkeypatch.setattr(ws, "resolve_within", lambda path: real)


def test_read_of_a_normal_file_returns_a_readable_fd(workspace):
    fd = ws.open_confined_read("artifact.txt")
    try:
        assert os.read(fd, 64) == b"v1"
    finally:
        os.close(fd)


def test_swapped_leaf_symlink_is_refused_and_not_followed(workspace, outside, monkeypatch):
    target = workspace / "artifact.txt"
    admitted = ws.resolve_within(str(target))  # the check passes on the real file
    # the race window: the leaf becomes a symlink to outside AFTER the check
    os.unlink(target)
    os.symlink(outside / "canary.txt", target)
    _snapshot_resolve(monkeypatch, admitted)
    with pytest.raises(ws.ConfinementError) as exc:
        ws.open_confined_read(str(target))
    assert exc.value.code == "EOUTSIDE"
    # nothing was opened through the link: the canary is untouched and no fd leaked
    assert (outside / "canary.txt").read_text(encoding="utf-8") == _CANARY


def test_swapped_intermediate_directory_is_refused(workspace, outside, monkeypatch):
    sub = workspace / "sub"
    sub.mkdir()
    target = sub / "artifact.txt"
    target.write_text("v1", encoding="utf-8")
    admitted = ws.resolve_within(str(target))
    # the window: an intermediate directory becomes a symlink to an outside
    # directory that also holds artifact.txt (the canary)
    outside_sub = outside / "sub"
    outside_sub.mkdir()
    (outside_sub / "artifact.txt").write_text(_CANARY, encoding="utf-8")
    os.rename(sub, workspace / "sub-real")
    os.symlink(outside_sub, sub)
    _snapshot_resolve(monkeypatch, admitted)
    # the walk meets the swapped component and refuses (EOUTSIDE when the
    # O_NOFOLLOW open sees the link, ENOENT when the walk cannot reach it —
    # the same mapping the write path has); the canary must not be read
    with pytest.raises(ws.FsOpError) as exc:
        ws.open_confined_read(str(target))
    assert exc.value.code in ("EOUTSIDE", "ENOENT")
    assert (outside_sub / "artifact.txt").read_text(encoding="utf-8") == _CANARY


def test_missing_leaf_is_ENOENT(workspace):
    with pytest.raises(ws.FsOpError) as exc:
        ws.open_confined_read("nope.txt")
    assert exc.value.code == "ENOENT"


def test_a_directory_leaf_is_ENOTFILE(workspace):
    (workspace / "adir").mkdir()
    with pytest.raises(ws.FsOpError) as exc:
        ws.open_confined_read("adir")
    assert exc.value.code == "ENOTFILE"


def test_a_path_outside_the_root_is_refused_before_any_open(workspace, outside):
    with pytest.raises(ws.ConfinementError) as exc:
        ws.open_confined_read(str(outside / "canary.txt"))
    assert exc.value.code == "EOUTSIDE"
