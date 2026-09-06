"""Original native write receipts + expected-before guards (issue #523).

The workspace jail (`backends/python/revl_fs_workspace.py`) already holds the
target fd across the snapshot and the write (`open_confined_write`) and proves
the landed name still points at the written inode after the fact
(`confirm_landed`, roadmap 431(b)). Issue #523 asks for the OTHER direction: an
opt-in check, BEFORE any content mutation, that the held descriptor matches what
the consumer recorded, plus a receipt whose identity is captured from the
original held descriptor so a same-bytes inode swap performed before the first
host observation cannot forge it.

This suite exercises the opt-in `original_receipt` / `expect_existing` helpers
against the guard module directly (no compiler, no cordis backend needed): the
module is the whole surface under test, and every mutation is confined to a
`REVL_FS_WORKSPACE` root under pytest's `tmp_path`.

The helpers are deliberately NOT wired into `stdlib/fs.rvl` yet (issue #523
requirement 7: legacy execution unchanged; public API names deferred until the
contract is mapped onto #500/#498), so this file is their only caller.
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


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A configured session workspace root."""
    r = tmp_path / "ws"
    r.mkdir()
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(r))
    return r


def _open(root: Path, name: str) -> ws.WriteHandle:
    return ws.open_confined_write(ws.resolve_within(str(root / name)))


# ---------------------------------------------------------------------------
# original_receipt: facts come from the held descriptor
# ---------------------------------------------------------------------------

def test_receipt_captures_original_identity_size_and_digest(root):
    p = root / "f.txt"
    p.write_bytes(b"hello world\n")
    st = os.stat(p)

    handle = _open(root, "f.txt")
    try:
        r = ws.original_receipt(handle)
    finally:
        ws.close_handle(handle)

    assert (r["dev"], r["ino"]) == (st.st_dev, st.st_ino)
    assert r["size"] == len(b"hello world\n")
    assert r["created"] is False
    import hashlib
    assert r["digest"] == "sha256:" + hashlib.sha256(b"hello world\n").hexdigest()


def test_receipt_for_a_created_target_is_fresh_and_empty(root):
    handle = _open(root, "new.txt")  # did not exist: open_confined_write creates it
    try:
        assert handle.created is True
        r = ws.original_receipt(handle)
    finally:
        ws.close_handle(handle)
    assert r["created"] is True
    assert r["size"] == 0
    assert r["digest"] == "sha256:" + __import__("hashlib").sha256(b"").hexdigest()


def test_receipt_reads_original_bytes_before_write_through(root):
    p = root / "f.txt"
    p.write_bytes(b"ORIGINAL")
    handle = _open(root, "f.txt")
    try:
        before = ws.original_receipt(handle)["digest"]
        ws.write_through(handle, "REPLACED")
        # the receipt taken before the write named the ORIGINAL content
        import hashlib
        assert before == "sha256:" + hashlib.sha256(b"ORIGINAL").hexdigest()
    finally:
        ws.close_handle(handle)
    assert p.read_bytes() == b"REPLACED"


# ---------------------------------------------------------------------------
# expect_existing: match passes, drift refuses, empty is EINVAL
# ---------------------------------------------------------------------------

def test_expect_existing_matches_recorded_facts(root):
    p = root / "f.txt"
    p.write_bytes(b"content")
    st = os.stat(p)
    handle = _open(root, "f.txt")
    try:
        # a full expectation recorded from the same target passes, and leaves
        # the target free to be written
        ws.expect_existing(handle, {
            "dev": st.st_dev, "ino": st.st_ino, "size": st.st_size,
            "digest": "sha256:" + __import__("hashlib").sha256(b"content").hexdigest(),
        })
        ws.write_through(handle, "next")
    finally:
        ws.close_handle(handle)
    assert p.read_bytes() == b"next"


def test_expect_existing_refuses_size_drift(root):
    p = root / "f.txt"
    p.write_bytes(b"now longer than before")
    handle = _open(root, "f.txt")
    try:
        with pytest.raises(ws.FsOpError) as ei:
            ws.expect_existing(handle, {"size": 3})
        assert ei.value.code == "EEXPECT"
        # refused before any mutation: target still holds its bytes
        assert p.read_bytes() == b"now longer than before"
    finally:
        ws.close_handle(handle)


def test_expect_existing_refuses_digest_drift(root):
    p = root / "f.txt"
    p.write_bytes(b"actual bytes")
    handle = _open(root, "f.txt")
    try:
        with pytest.raises(ws.FsOpError) as ei:
            ws.expect_existing(handle, {"digest": "sha256:" + "0" * 64})
        assert ei.value.code == "EEXPECT"
    finally:
        ws.close_handle(handle)


def test_expect_existing_empty_expectation_is_einval(root):
    (root / "f.txt").write_bytes(b"x")
    handle = _open(root, "f.txt")
    try:
        with pytest.raises(ws.FsOpError) as ei:
            ws.expect_existing(handle, {})
        assert ei.value.code == "EINVAL"
    finally:
        ws.close_handle(handle)


# ---------------------------------------------------------------------------
# the acceptance example: a same-bytes replacement cannot forge the receipt
# ---------------------------------------------------------------------------

def test_same_byte_inode_swap_does_not_satisfy_the_original_receipt(root):
    """Issue #523 acceptance: 'Snapshot or output path replaced with identical
    bytes before host observation: original receipt facts cannot be replaced by
    the new inode's facts.'

    A consumer records a receipt for the original target (its `(dev, ino)`). An
    attacker then replaces the file with a DIFFERENT inode holding IDENTICAL
    bytes. A digest-only check would pass, but the recorded identity is bound to
    the original inode, so an `expect_existing` against the recorded `ino`
    detects the swap even though the bytes are unchanged."""
    p = root / "f.txt"
    p.write_bytes(b"identical bytes")

    # consumer records the original receipt (identity + content)
    h0 = _open(root, "f.txt")
    try:
        recorded = ws.original_receipt(h0)
    finally:
        ws.close_handle(h0)

    # attacker swaps in a new inode with the SAME bytes
    tmp = root / "f.new"
    tmp.write_bytes(b"identical bytes")
    os.replace(tmp, p)
    assert p.read_bytes() == b"identical bytes"
    assert os.stat(p).st_ino != recorded["ino"]

    # the new descriptor: content matches, identity does not
    h1 = _open(root, "f.txt")
    try:
        # digest-only would be fooled (same bytes) ...
        ws.expect_existing(h1, {"digest": recorded["digest"]})
        # ... but the recorded identity catches the swap
        with pytest.raises(ws.FsOpError) as ei:
            ws.expect_existing(h1, {"ino": recorded["ino"]})
        assert ei.value.code == "EEXPECT"
    finally:
        ws.close_handle(h1)


def test_helpers_are_not_wired_into_the_stdlib_bodies(root):
    """Requirement 7: legacy execution is unchanged. The opt-in helpers must not
    be referenced by any `@py` body in `stdlib/fs.rvl`, so a caller who does not
    request the capability runs exactly the code that ran before."""
    text = (_ROOT / "stdlib" / "fs.rvl").read_text(encoding="utf-8")
    for helper in ("original_receipt", "expect_existing", "_digest_held"):
        assert helper not in text, \
            f"{helper} leaked into stdlib/fs.rvl; #523 slice 1 stays opt-in"
