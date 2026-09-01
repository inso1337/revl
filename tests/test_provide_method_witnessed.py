"""Witnessed effects in a PROVIDE-METHOD body — roadmap item 318, THE H1 gate.

Design: docs/design/243-witnessed-externs.md, docs/design/teardown-contract.md.

243/244/2a proved a witnessed effect in the component ACTIVATION body (runs
once at load). But the real agent use case is a fs mutation that fires from a
provide-METHOD, PER TOOL CALL (`ToolRegistry.call` -> a provide-method body),
after activation. This suite proves that closed loop end to end, from real
`.rvl` source through the live cordis-py runtime:

  * a component provides a service whose method does a witnessed fs mutation;
  * the method is called PER REQUEST (`session.call`), each call registering a
    transactional inverse into the component's activation frame
    (`Frame.transactional_method`);
  * on a clean session/component unload the mutations PERSIST (discharged, the
    deliverable), residue-free by the R4 unload checks;
  * on an ABORT (`Frame.abort()` — the seam item 245's explicit session
    commit/abort UX will drive) every per-call mutation REVERTS, residue-free;
  * the residue is ENUMERABLE: the WAL discharge descriptors name every
    crossing, and a clean commit writes a discharge record over their seqs
    while an abort writes none.

The witnessed extern is the same rename-with-a-data-witness stand-in
tests/test_witnessed_runtime.py uses (item 244's real fs bodies are the py
reference; a rename is enough to exercise the runtime path), extended to take
the target path as a PARAMETER so each per-call invocation mutates a distinct
file — the shape of an agent calling one fs tool repeatedly.
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
    reason="the transactional teardown is proven against a live cordis-py "
           "composition — install it with `sh backends/python/setup.sh` and "
           "run under its venv",
)

# a per-call witnessed mutation: rename the passed path -> path.bak, returning
# the paths as its data witness; the inverse renames it back (idempotent).
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
    # the service method declares the fs capability (`emission fn`), so the
    # per-call witnessed crossing stays visible to a consumer of `Ops`.
    "service Ops { emission fn touch(p: Str) }\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn touch(p) {\n"
    "      effect stash_path(p)\n"
    "    }\n"
    "  }\n"
    "}\n"
)

_BASE = compile_source(_SOURCE, "provide_method_witnessed.rvl")


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
    raise AssertionError("frame not found via ctx (use _capture_frame under "
                         "recording, where the ctx is a non-weakref proxy)")


def _capture_frame(monkeypatch):
    """Capture the live activation `Frame` the first time a per-tool-call
    witnessed effect registers into it. Under `record=True` the apply runs on a
    `_RecordingContext` proxy that is not weakref-keyable, so the frame never
    enters `_FRAME_BY_CTX` and `_frame_for_ctx` cannot find it; this hook is the
    reliable handle to it."""
    import runtime as _rt
    captured: list = []
    real = _rt.Frame.transactional_method

    def _spy(self, undo, witness):
        if self not in captured:
            captured.append(self)
        return real(self, undo, witness)

    monkeypatch.setattr(_rt.Frame, "transactional_method", _spy)
    return captured


@pytest.fixture
def files(tmp_path):
    """Three distinct target files, one per simulated tool call."""
    paths = []
    for i in range(3):
        p = tmp_path / f"artifact_{i}.txt"
        p.write_text(f"deliverable {i}", encoding="utf-8")
        paths.append(str(p))
    return paths


def _mutated(path: str) -> bool:
    """The witnessed rename ran: original gone, backup present."""
    return not os.path.exists(path) and os.path.exists(path + ".bak")


def _pristine(path: str) -> bool:
    """The world is as it started: original present, no backup residue."""
    return os.path.exists(path) and not os.path.exists(path + ".bak")


# ---------------------------------------------------------------------------
# 1. per-tool-call witnessed mutation PERSISTS on a clean unload (commit)
# ---------------------------------------------------------------------------

@needs_cordis
def test_per_tool_call_mutations_persist_on_clean_unload(files):
    session = _session()
    session.load(_ir())

    # activation did nothing; the frame is empty until a tool call fires
    frame = _sole_frame(session)
    assert frame._transactional == []

    # each tool call runs the provide-method, registering ONE transactional
    # inverse into the component's activation frame (per-tool-call H1)
    for path in files:
        session.call("ops", "touch", [path])
        assert _mutated(path), "the witnessed mutation did not apply on the call"

    assert len(frame._transactional) == len(files)
    assert len(frame._deferred_transactional) == len(files)
    assert all(not e.discharged and not e.replayed for e in frame._transactional)

    entries = list(frame._transactional)
    session.unload()  # clean unload == implicit commit

    # the deliverable persists on every path; the inverses discharged + GC'd
    for path in files:
        assert _mutated(path), "clean unload wrongly reverted a per-call mutation"
    for e in entries:
        assert e.discharged and not e.replayed
        assert e.witness is None and e._undo is None


# ---------------------------------------------------------------------------
# 2. per-tool-call witnessed mutation REVERTS on abort, residue-free
# ---------------------------------------------------------------------------

@needs_cordis
def test_per_tool_call_mutations_revert_on_abort(files):
    session = _session()
    session.load(_ir())

    frame = _sole_frame(session)
    for path in files:
        session.call("ops", "touch", [path])
        assert _mutated(path)

    entries = list(frame._transactional)

    # abort the session's work (item 245's reject drives this seam): the next
    # teardown reverts instead of committing
    frame.abort()
    result = session.unload()

    # every per-call mutation reverted, and the teardown left no residue
    for path in files:
        assert _pristine(path), "abort did not revert a per-call mutation"
    for e in entries:
        assert e.replayed and not e.discharged
        assert e.witness is None and e._undo is None
    assert result["noResidue"], f"abort left teardown residue: {result['checks']}"


# ---------------------------------------------------------------------------
# 3. abort is all-or-nothing across independent per-call mutations
# ---------------------------------------------------------------------------

@needs_cordis
def test_abort_reverts_every_call_not_just_the_last(files):
    session = _session()
    session.load(_ir())
    frame = _sole_frame(session)
    for path in files:
        session.call("ops", "touch", [path])

    frame.abort()
    session.unload()

    # all three, in one abort — the activation frame is the shared accumulator
    assert all(_pristine(p) for p in files)


# ---------------------------------------------------------------------------
# 4. the residue is ENUMERABLE: the WAL names every crossing, a commit writes a
#    discharge over their seqs, an abort writes none
# ---------------------------------------------------------------------------

def _wal_before_apply(monkeypatch, wal_path: str) -> None:
    """Open the WAL in the step that immediately precedes activation, mirroring
    tests/test_witnessed_runtime.py (a WAL only attaches to a Timeline when it
    is open AT APPLY TIME)."""
    real_instrument = replay.Recorder.instrument

    def _open_then_instrument(self, *args, **kwargs):
        self.open_wal(wal_path, generation=1)
        return real_instrument(self, *args, **kwargs)

    monkeypatch.setattr(replay.Recorder, "instrument", _open_then_instrument)


@needs_cordis
def test_wal_enumerates_every_per_call_crossing_and_commit_discharges(
        files, tmp_path, monkeypatch):
    wal_path = str(tmp_path / "commit.wal")
    _wal_before_apply(monkeypatch, wal_path)

    session = _session()
    session.load(_ir(), record=True)
    for path in files:
        session.call("ops", "touch", [path])

    # every per-call crossing is durably enumerated the instant it registers —
    # one transactional discharge-descriptor per tool call, well before commit
    written = replay.WriteAheadLog.read(wal_path)
    descriptors = [r for r in written["records"]
                   if r.get("record") == "discharge-descriptor"]
    assert len(descriptors) == len(files)
    assert all(d["entry"] == "transactional" for d in descriptors)
    assert all(d["call"]["method"] == "unstash" for d in descriptors)
    seqs = [d["seq"] for d in descriptors]

    session.unload()  # clean commit

    # the commit writes ONE discharge record naming every crossing's seq: recover
    # will SKIP them (committed, the mutation is the deliverable)
    written = replay.WriteAheadLog.read(wal_path)
    discharges = [r for r in written["records"] if r.get("record") == "discharge"]
    assert discharges, "clean commit wrote no discharge record"
    discharged = {seq for r in discharges for seq in r["discharged"]}
    assert set(seqs) <= discharged


@needs_cordis
def test_wal_writes_no_discharge_on_abort(files, tmp_path, monkeypatch):
    wal_path = str(tmp_path / "abort.wal")
    _wal_before_apply(monkeypatch, wal_path)
    captured = _capture_frame(monkeypatch)

    session = _session()
    session.load(_ir(), record=True)
    for path in files:
        session.call("ops", "touch", [path])

    (frame,) = captured
    frame.abort()
    session.unload()

    written = replay.WriteAheadLog.read(wal_path)
    # the descriptors still enumerate the crossings (residue is enumerable), but
    # no discharge record is written — the inverses were replayed, not committed
    descriptors = [r for r in written["records"]
                   if r.get("record") == "discharge-descriptor"]
    assert len(descriptors) == len(files)
    discharges = [r for r in written["records"] if r.get("record") == "discharge"]
    assert discharges == [], "an aborted teardown must not write a discharge record"
