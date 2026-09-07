"""Witnessed native write-receipt binding (issue #623).

The follow-up to #523/#606: #606 shipped the direct `revl.fs.write` shim with a
public receipt, but left the witnessed/WAL machinery unwired — the receipt was
never bound onto the witness the transactional effect carries, so
`witness_snapshot` / `prepare_verdict` could not consume the ORIGINAL held-target
facts. This suite exercises the opt-in `revl.fs.write_witnessed` / `write_all`
surface and the binding it performs:

* the ORIGINAL receipt + preimage are captured from the held descriptor and
  bound onto the WAL-serializable witness the effect entry holds;
* the existing `SessionOwner.witness_snapshot` + `Session.prepare_verdict`
  surfaces CONSUME those bound facts (never a reconstructed parallel inventory);
* a stale receipt/preimage drifts the verdict review token (refused confirm);
* a multi-write batch reports an accurate success/failed/unknown/unattempted
  inventory with no automatic retry;
* a durable witness whose receipt version is unreadable is refused;
* the legacy `revl.fs.write` opt-out is byte-identical (binds nothing).

Pure-Python unit tests: a real `runtime.SessionOwner` + `runtime.Frame` supply
the witnessed session (as the harness does), every write confined to a
`REVL_FS_WORKSPACE` root under pytest's `tmp_path`.
"""

from __future__ import annotations

import hashlib
import os
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
import runtime  # noqa: E402
import revl_fs_workspace as ws  # noqa: E402

from revl import fs  # noqa: E402
from revl.mcp.session import Session  # noqa: E402


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "ws"
    r.mkdir()
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(r))
    return r


class _FakeWal:
    """The smallest WAL that gives `Frame.transactional` a durable seq to bind."""

    def __init__(self):
        self.records = []

    def record_discharge_descriptor(self, kind, **kw):
        seq = len(self.records)
        self.records.append(dict(kw, kind=kind, seq=seq))
        return {"seq": seq}

    def record_fence(self, seq):   # only hit by an undeclared-idempotent inverse
        self.records.append({"kind": "fence", "seq": seq})


@pytest.fixture
def session(monkeypatch):
    """A live witnessed session: a registered `SessionOwner` + one live `Frame`
    with a WAL attached, torn down afterwards so global owner state never leaks.
    Yields `(owner, frame, wal)`."""
    owner = runtime.SessionOwner()
    owner.session_id = "sess-623"
    runtime.set_session_owner(owner)
    wal = _FakeWal()
    tl = types.SimpleNamespace(_wal=wal)
    ctx = types.SimpleNamespace(_revl_timeline=tl)
    frame = runtime.Frame(ctx, "UserCache")   # auto-registers into owner
    try:
        yield owner, frame, wal
    finally:
        runtime.clear_session_owner()


# ---------------------------------------------------------------------------
# public surface
# ---------------------------------------------------------------------------

def test_witnessed_public_names_exported():
    for name in ("write", "write_witnessed", "write_all", "WitnessedWriteReceipt",
                 "WriteOutcome", "WITNESSED_WRITE_API_VERSION",
                 "OUTCOME_SUCCESS", "OUTCOME_FAILED", "OUTCOME_UNKNOWN",
                 "OUTCOME_UNATTEMPTED"):
        assert name in fs.__all__
    assert fs.WITNESSED_WRITE_API_VERSION == ws.WITNESS_RECEIPT_VERSION


# ---------------------------------------------------------------------------
# original identity / preimage binding, before the first host observation
# ---------------------------------------------------------------------------

def test_created_write_binds_receipt_onto_effect_witness(root, session):
    owner, frame, wal = session
    r = fs.write_witnessed("new.txt", "hello\n")
    assert isinstance(r, fs.WitnessedWriteReceipt)
    assert Path(r.path) == (root / "new.txt")
    assert r.replaced is False
    assert r.outcome == "success"
    assert r.new_digest == _sha(b"hello\n")

    # one transactional effect on the frame, and its witness carries the receipt
    assert len(frame._transactional) == 1
    entry = frame._transactional[0]
    st = os.stat(root / "new.txt")
    assert entry.witness["receipt"]["ino"] == st.st_ino
    assert entry.witness["receipt"]["created"] is True
    assert entry.witness["version"] == ws.WITNESS_RECEIPT_VERSION
    # durable WAL identity: the entry took a WAL seq, and the effect id names it
    assert entry.seq is not None
    assert r.effect_id == owner._entry_identity(entry)
    assert f"seq{entry.seq}" in r.effect_id


def test_replaced_write_binds_original_inode_and_preimage(root, session):
    owner, frame, wal = session
    p = root / "f.txt"
    p.write_text("original\n")
    original_ino = os.stat(p).st_ino

    r = fs.write_witnessed("f.txt", "new\n")
    assert r.replaced is True
    assert r.prev_digest == _sha(b"original\n")

    entry = frame._transactional[0]
    receipt = entry.witness["receipt"]
    # the receipt names the ORIGINAL held inode, captured before the truncate
    assert receipt["ino"] == original_ino
    assert receipt["digest"] == _sha(b"original\n")
    assert receipt["created"] is False
    # the preimage snapshot exists so the inverse can restore the bytes
    assert entry.witness["preimage"]
    assert p.read_text() == "new\n"


def test_witnessed_restore_inverse_reverts_on_abort(root, session):
    owner, frame, wal = session
    p = root / "f.txt"
    p.write_text("v1\n")
    fs.write_witnessed("f.txt", "v2\n")
    assert p.read_text() == "v2\n"
    entry = frame._transactional[0]

    # abort: the frame did not commit, so disposing the entry replays restore
    frame._committed = False
    entry()
    assert p.read_text() == "v1\n"        # original bytes back
    assert entry.replayed is True


def test_witnessed_created_inverse_deletes_on_abort(root, session):
    owner, frame, wal = session
    fs.write_witnessed("fresh.txt", "x\n")
    p = root / "fresh.txt"
    assert p.exists()
    entry = frame._transactional[0]
    frame._committed = False
    entry()
    assert not p.exists()                 # created target removed


# ---------------------------------------------------------------------------
# expect= semantics preserved on the witnessed surface
# ---------------------------------------------------------------------------

def test_witnessed_expect_absent_and_digest(root, session):
    owner, frame, wal = session
    r1 = fs.write_witnessed("a.txt", "one\n", expect=fs.ABSENT)
    assert r1.replaced is False
    # a second ABSENT write is refused, nothing bound, target unchanged
    with pytest.raises(fs.FsOpError) as ei:
        fs.write_witnessed("a.txt", "two\n", expect=fs.ABSENT)
    assert ei.value.code == "EEXPECT"
    assert (root / "a.txt").read_text() == "one\n"
    assert len(frame._transactional) == 1     # the refusal bound nothing

    # a matching digest guard binds a second effect
    r2 = fs.write_witnessed("a.txt", "three\n", expect=r1.new_digest)
    assert r2.replaced is True
    assert len(frame._transactional) == 2


def test_witnessed_digest_drift_refused_binds_nothing(root, session):
    owner, frame, wal = session
    (root / "d.txt").write_text("real\n")
    with pytest.raises(fs.FsOpError) as ei:
        fs.write_witnessed("d.txt", "x\n", expect=_sha(b"stale\n"))
    assert ei.value.code == "EEXPECT"
    assert (root / "d.txt").read_text() == "real\n"
    assert frame._transactional == []


# ---------------------------------------------------------------------------
# consumer impact: no witnessed session -> refused, not silently unbound
# ---------------------------------------------------------------------------

def test_write_witnessed_refuses_without_session(root):
    runtime.clear_session_owner()
    with pytest.raises(fs.FsOpError) as ei:
        fs.write_witnessed("x.txt", "data\n")
    assert ei.value.code == "EUNWITNESSED"
    assert not (root / "x.txt").exists()      # refused before any mutation


def test_explicit_bind_hook_used(root):
    runtime.clear_session_owner()
    bound = []
    fs.write_witnessed("y.txt", "data\n", bind=lambda w: bound.append(w) or "eff#1")
    assert len(bound) == 1
    assert bound[0]["receipt"]["created"] is True
    assert (root / "y.txt").read_text() == "data\n"


# ---------------------------------------------------------------------------
# witness inspection / verdict APIs CONSUME the bound receipt (not a rebuild)
# ---------------------------------------------------------------------------

def test_witness_snapshot_exposes_receipt_and_gates_preimage(root, session):
    owner, frame, wal = session
    (root / "f.txt").write_text("orig\n")
    fs.write_witnessed("f.txt", "new\n")

    snap = owner.witness_snapshot(1)          # untrusted
    eff = snap.effects[0]
    # the ORIGINAL receipt identity crosses even to an untrusted caller
    assert eff.receipt is not None
    assert eff.receipt["digest"] == _sha(b"orig\n")
    assert eff.outcome == "success"
    # ...but the sensitive preimage stays off the untrusted witness
    assert eff.witness is None
    assert "preimage" not in (eff.receipt or {})

    trusted = owner.witness_snapshot(1, trusted=True)
    assert trusted.effects[0].witness["preimage"]   # trusted sees the preimage


def test_prepare_verdict_summary_reports_outcome_inventory(root, session):
    owner, frame, wal = session
    fs.write_witnessed("a.txt", "1\n")
    fs.write_witnessed("b.txt", "2\n")

    sess = Session()
    sess._driver = object()
    sess._owner = owner
    sess._generation = 1
    review = sess.prepare_verdict("abort")
    summ = review.summary
    assert summ["receiptsBound"] == 2
    assert summ["outcomes"]["success"] == 2
    assert summ["outcomes"]["failed"] == 0
    assert summ["outcomes"]["unattempted"] == 0
    # the effects carry the bound receipts the summary counted
    assert all(e["receipt"] is not None for e in summ["effects"])


def test_stale_receipt_drifts_verdict_token(root, session):
    owner, frame, wal = session
    fs.write_witnessed("a.txt", "1\n")

    sess = Session()
    sess._driver = object()
    sess._owner = owner
    sess._generation = 1
    review = sess.prepare_verdict("abort")

    # a NEW witnessed write changes the outstanding identities/revisions, so the
    # earlier review token no longer matches — confirm is refused, not adopted.
    fs.write_witnessed("b.txt", "2\n")
    out = sess.confirm_verdict(review.token)
    assert out["confirmed"] is False
    assert out["refused"] is True


# ---------------------------------------------------------------------------
# multi-write partial-failure inventory (requirement 5)
# ---------------------------------------------------------------------------

def test_write_all_partial_failure_inventory(root, session):
    owner, frame, wal = session
    # second write is refused (expect a digest that is not present), which stops
    # the batch: first succeeds, second fails, third never runs.
    specs = [
        ("one.txt", "1\n"),
        ("two.txt", "2\n", _sha(b"not-there\n")),   # EEXPECT: target absent
        ("three.txt", "3\n"),
    ]
    inv = fs.write_all(specs)
    assert [r.outcome for r in inv["results"]] == [
        "success", "failed", "unattempted"]
    assert inv["succeeded"] == 1
    assert inv["failed"] == 1
    assert inv["unattempted"] == 1
    assert inv["attempted"] == 2
    # no automatic retry/recovery: the failed + unattempted targets do not exist,
    # and the succeeded one is bound (its fate is the session verdict's).
    assert (root / "one.txt").exists()
    assert not (root / "two.txt").exists()
    assert not (root / "three.txt").exists()
    assert len(frame._transactional) == 1
    assert inv["results"][1].error.code == "EEXPECT"


def test_write_all_unknown_when_binder_fails_after_landing(root, session):
    owner, frame, wal = session

    def _boom(_w):
        raise RuntimeError("binder blew up after the bytes landed")

    inv = fs.write_all([("a.txt", "1\n"), ("b.txt", "2\n")], bind=_boom)
    # the first landing's bind failed -> outcome is UNKNOWN (undetermined),
    # distinct from a clean fail; the second never runs.
    assert [r.outcome for r in inv["results"]] == ["unknown", "unattempted"]
    assert inv["unknown"] == 1
    assert inv["unattempted"] == 1
    # the bytes DID land (the write was not the failure) — honest uncertainty
    assert (root / "a.txt").read_text() == "1\n"


def test_write_all_bound_rejects_oversized_batch():
    over = ws.MAX_WITNESS_BATCH_WRITES + 1
    with pytest.raises(fs.FsOpError) as ei:
        fs.write_all([("f%d.txt" % i, "x") for i in range(over)],
                     bind=lambda w: None)
    assert ei.value.code == "EBOUNDS"


# ---------------------------------------------------------------------------
# durable version refusal
# ---------------------------------------------------------------------------

def test_read_bound_receipt_refuses_unknown_version():
    good = {"path": "/p", "receipt": {"ino": 1}, "version": ws.WITNESS_RECEIPT_VERSION}
    assert ws.read_bound_receipt(good) == {"ino": 1}

    future = dict(good, version=ws.WITNESS_RECEIPT_VERSION + 1)
    with pytest.raises(ws.FsOpError) as ei:
        ws.read_bound_receipt(future)
    assert ei.value.code == "EVERSION"


def test_legacy_witness_has_no_version_and_is_accepted():
    legacy = {"path": "/p", "preimage": "", "created": False}
    ws.refuse_unknown_receipt_version(legacy)     # no raise
    assert ws.read_bound_receipt(legacy) is None
    assert ws.witness_outcome(legacy) == "success"


def test_restore_inverse_refuses_unknown_version(root, session):
    with pytest.raises(fs.FsOpError) as ei:
        fs._witnessed_restore(
            {"path": str(root / "z.txt"), "created": True,
             "version": ws.WITNESS_RECEIPT_VERSION + 1})
    assert ei.value.code == "EVERSION"


# ---------------------------------------------------------------------------
# legacy opt-out is byte-identical: plain write binds nothing
# ---------------------------------------------------------------------------

def test_legacy_write_binds_nothing(root, session):
    owner, frame, wal = session
    r = fs.write("plain.txt", "data\n")
    assert isinstance(r, fs.WriteReceipt)
    assert not isinstance(r, fs.WitnessedWriteReceipt)
    # the legacy surface never touches the witnessed machinery
    assert frame._transactional == []
    assert wal.records == []
    snap = owner.witness_snapshot(1)
    assert snap.effects == ()


def test_legacy_write_still_works_without_any_session(root):
    runtime.clear_session_owner()
    r = fs.write("solo.txt", "x\n")
    assert (root / "solo.txt").read_text() == "x\n"
    assert r.new_digest == _sha(b"x\n")
