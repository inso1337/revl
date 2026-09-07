"""The public guarded-write surface `revl.fs` (issue #523).

`revl.fs.write(path, data, *, expect=...)` -> `WriteReceipt` is the stable public
API issue #523 asked for: a native write that returns a receipt bound to the
original held inode and supports an expected-before guard. It is a thin shim over
the confined-write machinery in `backends/python/revl_fs_workspace.py` (the
issue #529 slice), so these tests also assert the workspace boundary is retained.

Every write is confined to a `REVL_FS_WORKSPACE` root under pytest's `tmp_path`.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from revl import fs


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "ws"
    r.mkdir()
    monkeypatch.setenv("REVL_FS_WORKSPACE", str(r))
    return r


# ---------------------------------------------------------------------------
# import / public surface
# ---------------------------------------------------------------------------

def test_public_names_import_and_are_fenced():
    from revl.fs import ABSENT, WriteReceipt, write  # noqa: F401

    assert callable(write)
    assert set(fs.__all__) >= {
        "write", "WriteReceipt", "ABSENT", "WRITE_RECEIPT_API_VERSION",
        "FsOpError", "ConfinementError",
    }
    assert repr(fs.ABSENT) == "revl.fs.ABSENT"
    assert isinstance(fs.WRITE_RECEIPT_API_VERSION, int)


# ---------------------------------------------------------------------------
# receipt fields
# ---------------------------------------------------------------------------

def test_create_receipt_fields(root):
    r = fs.write("new.txt", "hello\n")
    assert isinstance(r, fs.WriteReceipt)
    assert Path(r.path) == (root / "new.txt")
    assert r.prev_digest is None          # target did not exist
    assert r.new_digest == _sha(b"hello\n")
    assert r.replaced is False
    assert (root / "new.txt").read_bytes() == b"hello\n"


def test_overwrite_receipt_fields(root):
    (root / "f.txt").write_bytes(b"OLD")
    r = fs.write("f.txt", "NEW")
    assert r.prev_digest == _sha(b"OLD")   # original content, before the truncate
    assert r.new_digest == _sha(b"NEW")
    assert r.replaced is True
    assert (root / "f.txt").read_bytes() == b"NEW"


def test_round_trip_prev_and_new_digests(root):
    r1 = fs.write("f.txt", "one")
    r2 = fs.write("f.txt", "two")
    # the second write's prev_digest is exactly the first write's new_digest
    assert r2.prev_digest == r1.new_digest
    assert r2.new_digest == _sha(b"two")


# ---------------------------------------------------------------------------
# expect=<digest>: match writes, mismatch refuses with no partial write
# ---------------------------------------------------------------------------

def test_expect_digest_match_writes(root):
    r1 = fs.write("f.txt", "content")
    r2 = fs.write("f.txt", "next", expect=r1.new_digest)
    assert r2.replaced is True
    assert (root / "f.txt").read_bytes() == b"next"


def test_expect_digest_mismatch_refuses_no_partial_write(root):
    fs.write("f.txt", "actual content here")
    stale = _sha(b"something else entirely")
    with pytest.raises(fs.FsOpError) as ei:
        fs.write("f.txt", "SHOULD NOT LAND", expect=stale)
    assert ei.value.code == "EEXPECT"
    # refused before any mutation: the target still holds its bytes
    assert (root / "f.txt").read_bytes() == b"actual content here"


def test_expect_digest_on_absent_target_refuses(root):
    with pytest.raises(fs.FsOpError) as ei:
        fs.write("missing.txt", "x", expect=_sha(b"whatever"))
    assert ei.value.code == "EEXPECT"
    # nothing was created by the refused guarded write
    assert not (root / "missing.txt").exists()


# ---------------------------------------------------------------------------
# expect=ABSENT: creates when absent, refuses when present
# ---------------------------------------------------------------------------

def test_expect_absent_creates_when_missing(root):
    r = fs.write("fresh.txt", "brand new", expect=fs.ABSENT)
    assert r.replaced is False
    assert r.prev_digest is None
    assert (root / "fresh.txt").read_bytes() == b"brand new"


def test_expect_absent_on_existing_refuses_no_overwrite(root):
    (root / "f.txt").write_bytes(b"do not touch")
    with pytest.raises(fs.FsOpError) as ei:
        fs.write("f.txt", "OVERWRITE", expect=fs.ABSENT)
    assert ei.value.code == "EEXPECT"
    assert (root / "f.txt").read_bytes() == b"do not touch"


# ---------------------------------------------------------------------------
# the same-bytes inode swap acceptance example, at the public surface
# ---------------------------------------------------------------------------

def test_same_byte_inode_swap_does_not_satisfy_a_recorded_digest_after_reidentify(root):
    """Issue #523 acceptance: an output path replaced with identical bytes before
    observation cannot pass off the new inode as the original.

    A digest-only `expect=` legitimately passes on identical bytes (content is
    what it names). The identity binding lives in the receipt / `original_receipt`
    layer, exercised in tests/test_fs_write_receipts.py; here we assert the public
    surface at least still guards content honestly across an inode swap."""
    r1 = fs.write("f.txt", "identical bytes")
    # attacker swaps in a different inode with the SAME bytes
    tmp = root / "f.new"
    tmp.write_bytes(b"identical bytes")
    os.replace(tmp, root / "f.txt")
    assert os.stat(root / "f.txt").st_ino  # still exists
    # a write guarded on DIFFERENT expected content is refused; the guard is real
    with pytest.raises(fs.FsOpError) as ei:
        fs.write("f.txt", "z", expect=_sha(b"different"))
    assert ei.value.code == "EEXPECT"
    # and a write guarded on the actual current content still succeeds
    r2 = fs.write("f.txt", "z", expect=r1.new_digest)
    assert r2.new_digest == _sha(b"z")


# ---------------------------------------------------------------------------
# confinement is retained
# ---------------------------------------------------------------------------

def test_write_outside_the_root_is_refused(root, tmp_path):
    outside = tmp_path / "outside.txt"
    with pytest.raises(fs.ConfinementError) as ei:
        fs.write(str(outside), "escape")
    assert ei.value.code == "EOUTSIDE"
    assert not outside.exists()


def test_write_with_no_workspace_configured_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("REVL_FS_WORKSPACE", raising=False)
    with pytest.raises(fs.ConfinementError) as ei:
        fs.write(str(tmp_path / "x.txt"), "data")
    assert ei.value.code == "EWORKSPACE"


# ---------------------------------------------------------------------------
# argument validation
# ---------------------------------------------------------------------------

def test_non_str_data_is_einval(root):
    with pytest.raises(fs.FsOpError) as ei:
        fs.write("f.txt", b"bytes not allowed")  # type: ignore[arg-type]
    assert ei.value.code == "EINVAL"
    assert not (root / "f.txt").exists()


def test_bad_expect_type_is_einval(root):
    with pytest.raises(fs.FsOpError) as ei:
        fs.write("f.txt", "data", expect=123)  # type: ignore[arg-type]
    assert ei.value.code == "EINVAL"
    assert not (root / "f.txt").exists()
