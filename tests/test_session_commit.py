"""The session commit protocol — roadmap item 245, Slice 1 (the py foundation).

Design: docs/design/245-session-commit.md.

Item 318 opened the per-tool-call witnessed position; 245 adds the explicit
SESSION commit/abort verbs that drive it, plus the deferred emission — the
irreversible action that has not happened yet. A session performs three action
classes and ends in exactly one verdict:

  * (a) witnessed-revertible: a per-tool-call fs mutation that persists on commit
    and reverts on abort (the 318 seam, driven here by `session.commit`/`abort`);
  * (b) deferrable: a `deferred` emission that ENQUEUES a descriptor and does not
    fire until the session commit flushes it FIFO, or an abort drops it;
  * (c) immediate: a plain emission that fires at the call.

This suite proves the closed loop end to end through the live cordis-py runtime:

  * on COMMIT: all deferred emissions fire (FIFO), witnessed mutations persist
    (discharged), plain emissions already fired — residue-free, enumerable;
  * on ABORT: witnessed mutations revert (all), deferred emissions never fire,
    plain emissions stand — residue-free;
  * a CRASH in the approved-to-discharged window: `revl recover` treats it
    committed (rolls the discharge forward, replays no inverse);
  * the two-step commit is hash-bound: a stale hash after a post-enumeration
    enqueue is refused.

The witnessed extern is the same rename-with-a-data-witness stand-in
tests/test_provide_method_witnessed.py uses; the deferred and immediate
emissions append a tagged line to a sink file so FIFO order and firing are
observable.
"""

import copy
import importlib.util
import os
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
import replay  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the session commit protocol is proven against a live cordis-py "
           "composition — install it with `sh backends/python/setup.sh` and "
           "run under its venv",
)

# (a) a per-call witnessed rename; (b) a deferred emission that appends
# `deliver:<msg>` to a sink file at FLUSH; (c) an immediate emission that appends
# `announce:<msg>` at the call.
_SOURCE = (
    "type Stash = { path: Str, bak: Str }\n"
    "type FsError = { code: Str }\n"
    "extern pure fn unstash(w: Stash) -> Unit = @py {\n"
    "    import os\n"
    "    if os.path.exists(w['bak']):\n"
    "        os.replace(w['bak'], w['path'])\n"
    "    return\n"
    "}\n"
    "extern witnessed[fs] fn stash_path(p: Str) -> Result[Stash, FsError]"
    " undo unstash(result) = @py {\n"
    "    import os\n"
    "    bak = p + '.bak'\n"
    "    os.replace(p, bak)\n"
    "    return Ok({'path': p, 'bak': bak})\n"
    "}\n"
    "extern emission deferred fn deliver(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('deliver:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "extern emission fn announce(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('announce:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "service Ops {\n"
    "  emission fn stash(p: Str)\n"
    "  emission fn enqueue(sink: Str, msg: Str)\n"
    "  emission fn shout(sink: Str, msg: Str)\n"
    "}\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn stash(p) { effect stash_path(p) }\n"
    "    fn enqueue(sink, msg) { emit deliver(sink, msg) }\n"
    "    fn shout(sink, msg) { emit announce(sink, msg) }\n"
    "  }\n"
    "}\n"
)

_BASE = compile_source(_SOURCE, "session_commit.rvl")


def _ir() -> dict:
    return copy.deepcopy(_BASE)


def _session():
    from revl.mcp.session import Session
    return Session()


def _sole_frame(session):
    driver = session._driver
    ((_name, fiber),) = driver.fibers.items()
    frame = driver.runtime._frame_for_ctx(fiber.ctx)
    if frame is not None:
        return frame
    raise AssertionError("frame not found via ctx")


@pytest.fixture
def files(tmp_path):
    paths = []
    for i in range(3):
        p = tmp_path / f"artifact_{i}.txt"
        p.write_text(f"deliverable {i}", encoding="utf-8")
        paths.append(str(p))
    return paths


@pytest.fixture
def sink(tmp_path):
    return str(tmp_path / "sink.log")


def _lines(sink: str) -> list:
    if not os.path.exists(sink):
        return []
    return Path(sink).read_text(encoding="utf-8").splitlines()


def _mutated(path: str) -> bool:
    return not os.path.exists(path) and os.path.exists(path + ".bak")


def _pristine(path: str) -> bool:
    return os.path.exists(path) and not os.path.exists(path + ".bak")


# ---------------------------------------------------------------------------
# 1. COMMIT: deferred fire FIFO, witnessed persist, plain already out
# ---------------------------------------------------------------------------

@needs_cordis
def test_commit_flushes_deferred_fifo_and_persists_witnessed(files, sink):
    session = _session()
    session.load(_ir())
    frame = _sole_frame(session)

    # class (a) witnessed + (b) deferred, interleaved; (c) immediate fires now.
    for i, path in enumerate(files):
        session.call("ops", "stash", [path])
        assert _mutated(path), "witnessed mutation did not apply on the call"
        session.call("ops", "enqueue", [sink, f"q{i}"])   # deferred: does NOT fire
        session.call("ops", "shout", [sink, f"n{i}"])      # immediate: fires now

    # the deferred emissions have NOT fired; only the immediate ones have
    assert _lines(sink) == ["announce:n0", "announce:n1", "announce:n2"]
    assert len(session._owner._queue) == 3
    assert len(frame._transactional) == 3

    # two-step commit: enumerate, then confirm with the manifest hash
    manifest = session.commit()
    assert [s["group"] for s in manifest["summary"]] == ["deliver.deliver"]
    assert manifest["summary"][0]["count"] == 3
    assert manifest["witnessed"]["count"] == 3

    result = session.commit_confirm(manifest["hash"])
    assert result["committed"]
    assert result["prompts"] == {"commit": 1, "perCall": 0, "residue": 0}

    # every deferred emission fired, in FIFO (program) order, AFTER the immediates
    assert _lines(sink) == [
        "announce:n0", "announce:n1", "announce:n2",
        "deliver:q0", "deliver:q1", "deliver:q2",
    ]
    # the witnessed mutations persisted (discharged), residue-free
    for path in files:
        assert _mutated(path), "commit wrongly reverted a witnessed mutation"
    assert result["noResidue"], result["checks"]


# ---------------------------------------------------------------------------
# 2. ABORT: witnessed revert, deferred never fire, plain stands
# ---------------------------------------------------------------------------

@needs_cordis
def test_abort_reverts_witnessed_and_drops_deferred(files, sink):
    session = _session()
    session.load(_ir())

    for i, path in enumerate(files):
        session.call("ops", "stash", [path])
        session.call("ops", "enqueue", [sink, f"q{i}"])
        session.call("ops", "shout", [sink, f"n{i}"])

    # the immediate emissions are already out and STAY (class c, no compensate)
    assert _lines(sink) == ["announce:n0", "announce:n1", "announce:n2"]

    result = session.abort()
    assert result["aborted"]
    assert result["droppedDeferred"] == 3
    assert result["prompts"]["commit"] == 0

    # every witnessed mutation reverted (all-or-nothing), residue-free
    for path in files:
        assert _pristine(path), "abort did not revert a witnessed mutation"
    assert result["noResidue"], result["checks"]

    # the deferred emissions NEVER fired; the immediates still stand
    assert _lines(sink) == ["announce:n0", "announce:n1", "announce:n2"]


# ---------------------------------------------------------------------------
# 3. the gate target is derived from owner-held state, never the current frame
# ---------------------------------------------------------------------------

@needs_cordis
def test_gate_target_is_the_owner_registry_not_a_current_frame(files, sink):
    session = _session()
    session.load(_ir())
    owner = session._owner

    session.call("ops", "stash", [files[0]])
    session.call("ops", "enqueue", [sink, "q0"])

    # the target the manifest hash binds is (queue, witnessed seqs, registry) —
    # read off owner-held state, with the live frame in the registry
    target = owner._target()
    assert target["registry"] == ["Agent"]
    assert len(target["queue"]) == 1
    assert target["witnessed"] == 1
    # the runtime's per-call current-frame map plays no part in the derivation
    assert owner._registry, "the owner holds the live-frame registry"


# ---------------------------------------------------------------------------
# 4. two-step commit is hash-bound: a post-enumeration enqueue refuses confirm
# ---------------------------------------------------------------------------

@needs_cordis
def test_stale_hash_is_refused_after_a_post_enumeration_enqueue(files, sink):
    session = _session()
    session.load(_ir())

    session.call("ops", "enqueue", [sink, "q0"])
    manifest = session.commit()          # enumerate over a 1-entry queue

    session.call("ops", "enqueue", [sink, "q1"])   # drift: another enqueue
    result = session.commit_confirm(manifest["hash"])
    assert result["committed"] is False and result["refused"] is True
    assert "stale manifest hash" in result["reason"]
    # nothing fired: what the human approved is not a superset of what is queued
    assert _lines(sink) == []
    # the fresh manifest reflects both entries
    assert result["manifest"]["summary"][0]["count"] == 2


# ---------------------------------------------------------------------------
# 5. the WAL enumerates every crossing; the durable record order on commit
# ---------------------------------------------------------------------------

def _wal_before_apply(monkeypatch, wal_path: str) -> None:
    real_instrument = replay.Recorder.instrument

    def _open_then_instrument(self, *args, **kwargs):
        self.open_wal(wal_path, generation=1)
        return real_instrument(self, *args, **kwargs)

    monkeypatch.setattr(replay.Recorder, "instrument", _open_then_instrument)


@needs_cordis
def test_commit_wal_record_order(files, sink, tmp_path, monkeypatch):
    wal_path = str(tmp_path / "commit.wal")
    _wal_before_apply(monkeypatch, wal_path)

    session = _session()
    session.load(_ir(), record=True)
    session.call("ops", "stash", [files[0]])
    session.call("ops", "enqueue", [sink, "q0"])
    session.call("ops", "enqueue", [sink, "q1"])

    # every crossing is enumerated the instant it registers: one witnessed
    # discharge-descriptor and one deferred-emission per call, before commit
    written = replay.WriteAheadLog.read(wal_path)
    kinds = [r["record"] for r in written["records"]]
    assert kinds.count("discharge-descriptor") == 1
    assert kinds.count("deferred-emission") == 2

    manifest = session.commit()
    session.commit_confirm(manifest["hash"])

    written = replay.WriteAheadLog.read(wal_path)
    order = [r["record"] for r in written["records"]
             if r["record"] in ("commit-approved", "flushed", "discharge",
                                 "activation-complete")]
    # Decision 3 durability order: approved, then flushed(s), then discharge,
    # then activation-complete
    assert order == ["commit-approved", "flushed", "flushed", "discharge",
                     "activation-complete"]


# ---------------------------------------------------------------------------
# 6. CRASH in the approved-to-discharged window: recover treats it committed
# ---------------------------------------------------------------------------

def _truncate_after(wal_path: str, out_path: str, stop_record: str,
                    drop: set) -> None:
    """Copy the WAL up to and including the first `stop_record`, dropping any
    record kind in `drop` — a hand-built crash cut."""
    import json
    kept = []
    with open(wal_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if entry.get("record") in drop:
                continue
            kept.append(line)
            if entry.get("record") == stop_record:
                break
    Path(out_path).write_text("\n".join(kept) + "\n", encoding="utf-8")


@needs_cordis
def test_crash_in_approved_window_recovers_as_committed(files, sink, tmp_path,
                                                        monkeypatch):
    from revl import recovery

    wal_path = str(tmp_path / "window.wal")
    _wal_before_apply(monkeypatch, wal_path)

    session = _session()
    session.load(_ir(), record=True)
    session.call("ops", "stash", [files[0]])   # witnessed mutation
    session.call("ops", "enqueue", [sink, "q0"])  # deferred (owed if unflushed)
    manifest = session.commit()
    session.commit_confirm(manifest["hash"])

    # simulate a crash INSIDE the window: keep everything through
    # `commit-approved`, drop the discharge, flushed, and activation-complete
    crashed = str(tmp_path / "crashed.wal")
    _truncate_after(wal_path, crashed, "commit-approved",
                    drop={"discharge", "flushed", "activation-complete"})

    report = recovery.recover(crashed)
    # committed, not rolled back: no witnessed inverse replays
    assert report["verdict"] == "rolled-forward"
    assert report["committed"] is True
    assert report["hash"] == manifest["hash"]
    # the missing discharge was rolled forward (appended), naming the witnessed seq
    assert report["rolledForwardDischarge"], "no discharge rolled forward"
    # the deferred emission had no `flushed` record in the cut -> reported OWED,
    # never auto-fired
    assert len(report["owedFlushes"]) == 1
    assert report["owedFlushes"][0]["outcome"] == "not-attempted"

    # a SECOND recover pass is a no-op: the rolled-forward discharge is durable
    again = recovery.recover(crashed)
    assert again["verdict"] == "rolled-forward"
    assert again["rolledForwardDischarge"] == []


# ---------------------------------------------------------------------------
# 7. an all-(a)/(b) abort is exact-by-construction clean; recover agrees
# ---------------------------------------------------------------------------

@needs_cordis
def test_abort_wal_has_no_commit_approved_and_recovers_dropped_clean(
        files, sink, tmp_path, monkeypatch):
    from revl import recovery

    wal_path = str(tmp_path / "abort.wal")
    _wal_before_apply(monkeypatch, wal_path)

    session = _session()
    session.load(_ir(), record=True)
    session.call("ops", "stash", [files[0]])
    session.call("ops", "enqueue", [sink, "q0"])
    session.call("ops", "enqueue", [sink, "q1"])
    session.abort()

    written = replay.WriteAheadLog.read(wal_path)
    kinds = [r["record"] for r in written["records"]]
    # the absence of `commit-approved` IS the abort verdict; no discharge either
    assert "commit-approved" not in kinds
    assert "discharge" not in kinds
    # the descriptors still enumerate the crossings (residue is enumerable)
    assert kinds.count("deferred-emission") == 2
    assert "aborted" in kinds   # in-process abort wrote its completion record

    # recover reads it as a dropped-clean abort: the two deferred emissions
    # dropped, never fired, counted clean
    report = recovery.recover(wal_path)
    assert report["verdict"] == "rolled-back"
    assert len(report["droppedDeferred"]) == 2
    assert report["abortCompleted"] is True
    assert report["residue"]["clean"] is True
