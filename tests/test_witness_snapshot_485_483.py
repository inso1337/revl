"""Bounded witness inspection (#485) + exact-state verdict review (#483).

#485 replaces the harness reaching through `session._owner`, a frame
`_registry` and `_transactional` with a supported, read-only surface:
`SessionOwner.witness_snapshot` / `Session.witness_snapshot` returns an
IMMUTABLE (frozen dataclasses + tuples), BOUNDED copy of the outstanding
witnessed effects — each with a stable identity and a revision digest of its
witness preimage (never the preimage itself, unless the caller is trusted),
distinguishing live / escrowed / settled, plus an opaque snapshot token.

#483 binds the abort review token to the session GENERATION and the EXACT
outstanding witness identities/revisions (not a count + frame names), and adds
the `prepare_verdict("abort")` -> `confirm_verdict(token)` pair whose confirm
REFUSES a state that drifted since the review rather than silently adopting it.

These are pure-Python unit tests over `SessionOwner`/`Session` — no cordis
composition needed: the snapshot and the token are derived from the owner's
own registry/escrow, so lightweight stand-in frames and real `_Transactional`/
`_Compensation` entries exercise the whole surface.
"""

import dataclasses
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
import runtime  # noqa: E402

from revl.mcp.session import Session, SessionError, VerdictReview  # noqa: E402


# ---------------------------------------------------------------------------
# helpers: a stand-in activation frame + real witnessed entries
# ---------------------------------------------------------------------------

class _Frame:
    def __init__(self, name):
        self.name = name
        self._transactional = []
        self._compensations = []
        self._committed = False


def _undo_stash(w):  # a named inverse so `_named_call_method` reads a real name
    return None


def _offset():
    return None


def _transactional(owner, frame, witness, *, escrowed=False, settled=False):
    entry = runtime._Transactional(frame, _undo_stash, witness)
    if settled:
        entry.discharged = True
    if escrowed:
        owner._escrow.append(entry)
    else:
        frame._transactional.append(entry)
    return entry


def _new_owner_with_live(witness=None):
    owner = runtime.SessionOwner()
    owner.session_id = "sess-abc"
    frame = _Frame("UserCache")
    owner._registry.append(frame)
    entry = _transactional(owner, frame, witness or {"path": "/a", "bak": "/a.bak"})
    return owner, frame, entry


def _facade(owner, generation=1):
    s = Session()
    s._driver = object()   # non-None so `_require` passes; unused on these paths
    s._owner = owner
    s._generation = generation
    return s


# ---------------------------------------------------------------------------
# #485 — the snapshot: immutable, bounded, live/escrowed/settled, trust-gated
# ---------------------------------------------------------------------------

def test_snapshot_is_immutable_and_bounded():
    owner, _frame, _entry = _new_owner_with_live()
    snap = owner.witness_snapshot(generation=1)

    assert isinstance(snap, runtime.WitnessSnapshot)
    assert isinstance(snap.effects, tuple)                 # bounded, materialized
    assert snap.bound == runtime._WITNESS_SNAPSHOT_BOUND
    assert snap.truncated is False

    # frozen: a host cannot write back through a snapshot to reach the session
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.token = "tampered"
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.effects[0].revision = "tampered"

    # to_dict is a fresh plain-dict copy (JSON-friendly for the harness)
    d = snap.to_dict()
    d["effects"].append("junk")
    assert len(owner.witness_snapshot(generation=1).effects) == 1


def test_snapshot_bound_truncates_but_token_covers_all(monkeypatch):
    monkeypatch.setattr(runtime, "_WITNESS_SNAPSHOT_BOUND", 2)
    owner = runtime.SessionOwner()
    owner.session_id = "sess-bound"
    frame = _Frame("Big")
    owner._registry.append(frame)
    for i in range(5):
        _transactional(owner, frame, {"i": i})
    snap = owner.witness_snapshot(generation=1)
    assert len(snap.effects) == 2
    assert snap.truncated is True
    # the token still binds every outstanding identity, not just the shown two
    assert len(owner._outstanding_identity(1)) == 5


def test_snapshot_distinguishes_live_escrowed_settled():
    owner = runtime.SessionOwner()
    owner.session_id = "sess-x"
    live_frame = _Frame("Live")
    owner._registry.append(live_frame)
    _transactional(owner, live_frame, {"path": "/live"})           # live
    settled_frame = _Frame("Settled")
    owner._registry.append(settled_frame)
    _transactional(owner, settled_frame, {"path": "/done"}, settled=True)
    escrow_frame = _Frame("Withdrawn")
    _transactional(owner, escrow_frame, {"path": "/held"}, escrowed=True)

    statuses = {e.component: e.status for e in owner.witness_snapshot(1).effects}
    assert statuses == {"Live": "live", "Settled": "settled",
                        "Withdrawn": "escrowed"}


def test_untrusted_snapshot_hides_witness_preimage_trusted_reveals():
    owner, _frame, _entry = _new_owner_with_live({"path": "/secret", "bak": "/s.bak"})

    untrusted = owner.witness_snapshot(1).effects[0]
    assert untrusted.witness is None                     # preimage withheld
    assert untrusted.revision.startswith("rev:sha256:")  # digest still shown

    trusted = owner.witness_snapshot(1, trusted=True).effects[0]
    assert trusted.witness == {"path": "/secret", "bak": "/s.bak"}
    # gating the preimage does not change the identity or the revision digest
    assert trusted.revision == untrusted.revision
    assert trusted.id == untrusted.id


# ---------------------------------------------------------------------------
# #483 — the token binds generation + exact outstanding identities/revisions
# ---------------------------------------------------------------------------

def test_changing_an_outstanding_witness_changes_the_token():
    owner, _frame, entry = _new_owner_with_live({"path": "/a", "bak": "/a.bak"})
    before = owner.witness_snapshot(1).token
    entry.witness = {"path": "/a", "bak": "/a.bak.MOVED"}   # preimage drifts
    after = owner.witness_snapshot(1).token
    assert before != after


def test_adding_an_outstanding_witness_changes_the_token():
    owner, frame, _entry = _new_owner_with_live()
    before = owner.witness_snapshot(1).token
    _transactional(owner, frame, {"path": "/b"})           # a new witnessed call
    assert owner.witness_snapshot(1).token != before


def test_new_generation_changes_the_token():
    owner, _frame, _entry = _new_owner_with_live()
    assert owner.witness_snapshot(1).token != owner.witness_snapshot(2).token


def test_settling_an_entry_drops_it_from_the_outstanding_token():
    owner, _frame, entry = _new_owner_with_live()
    before = owner.witness_snapshot(1).token
    entry.discharged = True                                # no longer outstanding
    assert owner.witness_snapshot(1).token != before


# ---------------------------------------------------------------------------
# #483 — prepare_verdict / confirm_verdict on the Session facade
# ---------------------------------------------------------------------------

def test_prepare_verdict_rejects_non_abort():
    owner, _frame, _entry = _new_owner_with_live()
    s = _facade(owner)
    with pytest.raises(SessionError, match="prepare_verdict supports 'abort'"):
        s.prepare_verdict("commit")


def test_prepare_verdict_summary_and_token():
    owner, frame, _entry = _new_owner_with_live()
    _transactional(owner, _Frame("Held"), {"path": "/h"}, escrowed=True)
    s = _facade(owner)
    review = s.prepare_verdict("abort")
    assert isinstance(review, VerdictReview)
    assert review.token.startswith("revl-verdict:abort:")
    assert review.summary["outstanding"] == 2
    assert review.summary["live"] == 1
    assert review.summary["escrowed"] == 1
    assert review.summary["generation"] == 1


def test_confirm_verdict_refuses_changed_state():
    owner, _frame, entry = _new_owner_with_live()
    s = _facade(owner)
    review = s.prepare_verdict("abort")

    entry.witness = {"path": "/a", "bak": "/a.bak.CHANGED"}   # drift after review
    result = s.confirm_verdict(review.token)

    assert result["confirmed"] is False
    assert result["refused"] is True
    assert "changed since the review" in result["reason"]
    # the refusal hands back a FRESH review token bound to the changed state,
    # never silently adopts it — and the fresh token is not the stale one
    assert result["review"]["token"] != review.token


def test_confirm_verdict_enacts_on_unchanged_state():
    owner, _frame, _entry = _new_owner_with_live()
    s = _facade(owner)
    review = s.prepare_verdict("abort")

    enacted = {}

    def _stub_abort():
        enacted["called"] = True
        return {"aborted": True}

    s.abort = _stub_abort
    result = s.confirm_verdict(review.token)
    assert enacted.get("called") is True
    assert result == {"aborted": True}


def test_confirm_verdict_rejects_a_foreign_token():
    owner, _frame, _entry = _new_owner_with_live()
    s = _facade(owner)
    with pytest.raises(SessionError, match="unrecognized verdict token"):
        s.confirm_verdict("sha256:deadbeef")
