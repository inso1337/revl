"""An approval-WAL candidate that exists but is not writable must not be chosen
(issue #2237).

``os.makedirs(..., exist_ok=True)`` succeeds on an existing directory without
checking writability, so ``resolve_wal_dir`` used to return such a directory as
durable and every later WAL write failed with a bare ``PermissionError``.
"""
from __future__ import annotations

import os

import pytest

from revl.wal import resolve_wal_dir, wal_dir_candidates

pytestmark = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores directory mode bits, so a 0o500 directory is still writable",
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("REVL_WAL_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    return tmp_path


def _lock(path):
    os.makedirs(path, mode=0o700)
    os.chmod(path, 0o500)


def _unlock_all(root):
    for dirpath, dirnames, _ in os.walk(root):
        for name in dirnames:
            os.chmod(os.path.join(dirpath, name), 0o700)


def test_unwritable_first_candidate_is_skipped(home):
    candidates = wal_dir_candidates()
    first = candidates[0]
    _lock(first)
    try:
        res = resolve_wal_dir()
        assert res.directory != first
        assert first in [path for path, _ in res.attempts]
        if len(candidates) > 1:
            assert res.directory == candidates[1]
            assert res.durable
            assert os.access(res.directory, os.W_OK)
        else:
            # Linux without XDG_STATE_HOME has a single durable candidate.
            assert not res.durable
    finally:
        _unlock_all(home)


def test_unwritable_xdg_candidate_falls_back_to_next(home, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "state"))
    candidates = wal_dir_candidates()
    assert len(candidates) == 2
    _lock(candidates[0])
    try:
        res = resolve_wal_dir()
        assert res.directory == candidates[1]
        assert res.durable
        assert [path for path, _ in res.attempts] == [candidates[0]]
        assert "not writable" in res.attempts[0][1]
    finally:
        _unlock_all(home)


def test_writable_candidate_is_unchanged_and_leaves_no_probe(home):
    res = resolve_wal_dir()
    assert res.durable
    assert res.directory == wal_dir_candidates()[0]
    assert res.attempts == ()
    assert os.listdir(res.directory) == []


def test_failed_probe_cleanup_does_not_reject_a_writable_candidate(home, monkeypatch):
    real_unlink = os.unlink

    def flaky_unlink(path, *args, **kwargs):
        if ".revl-probe-" in os.fspath(path):
            raise PermissionError(13, "unlink denied", path)
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", flaky_unlink)
    res = resolve_wal_dir()
    assert res.durable
    assert res.directory == wal_dir_candidates()[0]
    assert res.attempts == ()
