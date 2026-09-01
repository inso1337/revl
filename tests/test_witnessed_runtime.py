"""Witnessed-inverse externs — roadmap item 243, SLICE 2a (py runtime seam).

Design: docs/design/243-witnessed-externs.md.

The one thing to get right, proven at RUNTIME here: a `witnessed` effect is a
TRANSACTION, not an `acquire` bracket. Its declared inverse replays on ABORT
ONLY; on a clean successful unload it is DISCHARGED (skipped, witness GC'd) and
the mutation — the deliverable — PERSISTS. A bracket, by contrast, replays on a
clean unload too. These tests drive real cordis-py compositions over a real
temp file and assert the observable difference three ways:

  * witnessed + clean unload   -> inverse does NOT replay, mutation persists
  * witnessed + mid-body abort -> inverse DOES replay, mutation reverted (A8),
                                  residue-free
  * acquire   + clean unload   -> inverse replays (bracket unchanged)

The witnessed call site is hand-built at the IR level: Slice 1 landed the
`witnessed` extern + its transactional IR descriptor, and the effect-position
call-site *surface* is a later slice (it needs the lowerer, out of Slice 2a's
scope). Slice 2a is the backend that consumes the IR the future lowerer will
emit, so the test crafts that IR directly — a standard `effect`/`let-effect`
step whose acquisition calls a witnessed extern, which is exactly the shape
emit.py keys the transactional registration off.

The toy witnessed extern renames a file (its `Ok` witness carries the paths) and
the inverse renames it back — a stand-in for item 244's real fs bodies, enough
to exercise the runtime path. The target path is passed through an env var
rather than component config so the harness stays a pure IR fixture (config
resolution is a separate cordis path, not what these tests exercise).
"""

import copy
import importlib.util
import os
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source

# bridge-slice tests (below) drive `replay.WriteAheadLog`/`revl.recovery`
# directly over a live cordis session, mirroring tests/test_witnessed_wal_recover.py
# and tests/test_crash_recovery.py's own sys.path bootstrap for `backends/python`.
_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
import replay  # noqa: E402
from revl.recovery import DictWorld, recover  # noqa: E402

# ---------------------------------------------------------------------------
# live runtime gate (mirrors tests/test_apply.py / test_crash_recovery.py)
# ---------------------------------------------------------------------------

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the transactional teardown is proven against a live cordis-py "
           "composition — install it with `sh backends/python/setup.sh` and "
           "run under its venv",
)

_TARGET_ENV = "REVL_WIT_TARGET"


# ---------------------------------------------------------------------------
# the toy witnessed effect: a file rename with a data witness + named inverse
# ---------------------------------------------------------------------------

_EXTERNS = (
    "type Stash = { path: Str, bak: Str }\n"
    "type FsError = { code: Str }\n"
    # the inverse: a named, WAL-reconstructible restore over the data witness.
    # Idempotent (rule 5) — a second replay is a no-op once the file is back.
    "extern pure fn unstash(w: Stash) -> Unit = @py {\n"
    "    import os\n"
    "    if os.path.exists(w['bak']):\n"
    "        os.replace(w['bak'], w['path'])\n"
    "    return\n"
    "}\n"
    # the witnessed mutation: rename path -> path.bak, returning the paths as
    # its data witness. Ok-conditional at the call site: an Err would register
    # nothing; the toy path always succeeds under the test's setup.
    "extern witnessed[fs] fn stash() -> Result[Stash, FsError]"
    " undo unstash(result) = @py {\n"
    "    import os\n"
    "    path = os.environ['REVL_WIT_TARGET']\n"
    "    bak = path + '.bak'\n"
    "    os.replace(path, bak)\n"
    "    return Ok({'path': path, 'bak': bak})\n"
    "}\n"
    # the BRACKET contrast: same mutation, but an `acquire` extern whose undo
    # replays on EVERY teardown. Emitted as a plain bracket, not transactional.
    "extern acquire fn stash_acq() -> Stash undo unstash(result) = @py {\n"
    "    import os\n"
    "    path = os.environ['REVL_WIT_TARGET']\n"
    "    bak = path + '.bak'\n"
    "    os.replace(path, bak)\n"
    "    return {'path': path, 'bak': bak}\n"
    "}\n"
)

_BASE = compile_source(_EXTERNS, "witnessed.rvl")


def _witnessed_component(name: str, abort: bool) -> dict:
    """A component whose activation performs the witnessed effect, optionally
    followed by a `fail` step so activation aborts mid-body."""
    body = [{"step": "effect",
             "acquire": {"kind": "fn", "name": "stash", "args": []}}]
    if abort:
        body.append({"step": "fail", "message": {"kind": "lit", "value": "boom"}})
    return {"name": name, "source": "witnessed.rvl", "config": [],
            "requires": {}, "provides": {}, "body": body}


def _acquire_component(name: str) -> dict:
    """A component whose activation performs the SAME mutation through an
    `acquire` bracket: a site-spelled undo that replays on clean unload too."""
    body = [{
        "step": "let-effect", "bind": "h",
        "acquire": {"kind": "fn", "name": "stash_acq", "args": []},
        "undo": {"kind": "fn", "name": "unstash", "args": [{"kind": "name", "id": "h"}]},
    }]
    return {"name": name, "source": "witnessed.rvl", "config": [],
            "requires": {}, "provides": {}, "body": body}


def _ir(component: dict) -> dict:
    ir = copy.deepcopy(_BASE)
    ir["components"] = [component]
    return ir


@pytest.fixture
def target(tmp_path, monkeypatch):
    path = tmp_path / "artifact.txt"
    path.write_text("the deliverable", encoding="utf-8")
    monkeypatch.setenv(_TARGET_ENV, str(path))
    return str(path)


def _session():
    from revl.mcp.session import Session
    return Session()


def _sole_frame(session):
    """The single activation frame of the loaded composition, for introspection
    of its transactional entries."""
    driver = session._driver
    ((_name, fiber),) = driver.fibers.items()
    return driver.runtime._frame_for_ctx(fiber.ctx)


# ---------------------------------------------------------------------------
# 1. witnessed + clean unload: the mutation PERSISTS, the inverse is discharged
# ---------------------------------------------------------------------------

@needs_cordis
def test_witnessed_persists_on_clean_unload(target):
    bak = target + ".bak"
    session = _session()
    session.load(_ir(_witnessed_component("StashOk", abort=False)))

    # activation ran the mutation: original gone, backup present
    assert not os.path.exists(target)
    assert os.path.exists(bak)

    # the frame registered exactly one transactional entry, not yet resolved
    frame = _sole_frame(session)
    assert len(frame._transactional) == 1
    entry = frame._transactional[0]
    assert entry.discharged is False and entry.replayed is False

    session.unload()  # clean successful unload == implicit commit

    # the inverse did NOT replay: the mutation is the deliverable and persists
    assert not os.path.exists(target), "clean unload wrongly restored the file"
    assert os.path.exists(bak)
    # discharged, and the witness GC'd (dropped) — no live rollback state
    assert entry.discharged is True
    assert entry.replayed is False
    assert entry.witness is None
    assert entry._undo is None


# ---------------------------------------------------------------------------
# 2. witnessed + mid-activation abort: the inverse REPLAYS, mutation reverted
# ---------------------------------------------------------------------------

@needs_cordis
def test_witnessed_reverts_on_abort(target):
    bak = target + ".bak"
    session = _session()

    # the same witnessed effect, but a later step fails -> activation never
    # commits, so its transactional inverse replays on the unwind (A8). cordis
    # surfaces the mid-activation failure by landing the fiber FAILED (the
    # revert has already run by the time the state report comes back).
    report = session.load(_ir(_witnessed_component("StashAbort", abort=True)))
    assert report["components"] == [{"name": "StashAbort", "state": "FAILED"}]

    assert os.path.exists(target), "abort did not replay the inverse — mutation stuck"
    assert open(target).read() == "the deliverable"
    # residue-free: the backup the mutation created was cleaned up by the revert
    assert not os.path.exists(bak), "abort left rollback residue (A8 violation)"


# ---------------------------------------------------------------------------
# 3. acquire + clean unload: the bracket STILL reverts (unchanged)
# ---------------------------------------------------------------------------

@needs_cordis
def test_acquire_bracket_reverts_on_clean_unload(target):
    bak = target + ".bak"
    session = _session()
    session.load(_ir(_acquire_component("Acq")))

    assert not os.path.exists(target)  # acquired: mutation applied
    assert os.path.exists(bak)

    session.unload()  # clean unload

    # the bracket inverse replays on a clean unload — the file is restored.
    # This is exactly what a witnessed effect must NOT do (test 1), proving the
    # two entry kinds are distinct at runtime.
    assert os.path.exists(target), "acquire bracket failed to revert on clean unload"
    assert not os.path.exists(bak)
    assert open(target).read() == "the deliverable"


# ---------------------------------------------------------------------------
# emit seam (no cordis needed): the witnessed call site compiles to a
# transactional registration, the bracket compiles to a plain disposer.
# ---------------------------------------------------------------------------

def _emit_backend():
    spec = importlib.util.spec_from_file_location(
        "py_emit_witnessed", "backends/python/emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_witnessed_call_site_emits_ok_conditional_transactional():
    emit = _emit_backend()
    body = emit.emit(_ir(_witnessed_component("StashOk", abort=False)))
    # the mutation runs, then the DECLARED inverse (unstash) registers as a
    # transactional entry — only on the Ok branch, carrying the Ok payload.
    assert "isinstance(_revl_wit1, Ok)" in body
    assert "_revl_frame.transactional((lambda result: unstash(result)), _revl_wit1.value)" in body
    # it is NOT a bracket: no `yield lambda:` disposer for the witnessed step
    # (the only yields are the transactional one and the frame drain).
    assert "yield lambda:" not in body
    # Ok/Err are present because the witnessed extern returns Result
    assert "class Ok:" in body


def test_bracket_still_emits_a_plain_disposer():
    emit = _emit_backend()
    body = emit.emit(_ir(_acquire_component("Acq")))
    # the acquire bracket keeps its site-spelled, always-replaying disposer
    assert "h = stash_acq()" in body
    assert "yield lambda: unstash(h)" in body
    # and never becomes a transactional entry
    assert "transactional(" not in body


def test_non_witnessed_program_is_untouched():
    # a program with no witnessed extern must emit byte-identically to before:
    # neither the transactional call nor the witnessed-forced Result classes
    # appear, and no component even sees the witnessed table.
    emit = _emit_backend()
    ir = compile_source(
        "component C { effect Map.new() undo Map.new() }\n", "plain.rvl")
    body = emit.emit(ir)
    assert "transactional(" not in body
    assert "_revl_frame.transactional" not in body


# ---------------------------------------------------------------------------
# bridge slice (item 243, "(i.b) runtime wiring"): Frame wired to the WAL/
# recover foundation (backends/python/replay.py `WriteAheadLog`, src/revl/
# recovery.py `recover`). The same committed-vs-aborted crash-safety proof
# tests/test_witnessed_wal_recover.py established directly against the WAL
# API, now driven through the REAL `Frame.transactional`/`Frame.drain` over a
# live cordis-py activation.
# ---------------------------------------------------------------------------


def _wal_before_apply(monkeypatch, wal_path: str) -> None:
    """`Session.load` builds a fresh `replay.Recorder` and instruments the
    emitted module in one call (`Session._prepare_module`), with no seam
    in between to open the WAL — and `Recorder._wrap_apply` only attaches a
    WAL to a component's `Timeline` when `recorder.wal is not None` AT APPLY
    TIME. So hook `Recorder.instrument`, the step that immediately precedes
    activation, to open the WAL first."""
    real_instrument = replay.Recorder.instrument

    def _open_then_instrument(self, *args, **kwargs):
        self.open_wal(wal_path, generation=1)
        return real_instrument(self, *args, **kwargs)

    monkeypatch.setattr(replay.Recorder, "instrument", _open_then_instrument)


@needs_cordis
def test_a_committed_transactional_writes_a_durable_discharge_record_at_drain(
        target, tmp_path, monkeypatch):
    """The crash-safety proof through the REAL `Frame.drain`: a WAL is
    attached to a live session, the witnessed effect commits cleanly (a clean
    `session.unload()`), and `Frame.drain` must write the durable discharge
    record BEFORE that unload finishes — so a simulated crash right after (no
    `activation-complete` marker, exactly a `kill -9` between the commit and
    the marker) still leaves `revl recover` SKIPPING the already-committed
    mutation instead of wrongly rolling it back."""
    wal_path = str(tmp_path / "commit.wal")
    _wal_before_apply(monkeypatch, wal_path)

    session = _session()
    session.load(_ir(_witnessed_component("StashOk", abort=False)), record=True)

    # the descriptor is durable the instant `Frame.transactional` registers
    # the entry — well before the activation ever commits (each WAL write is
    # flushed + fsync'd, so it is already readable here, mid-session)
    written = replay.WriteAheadLog.read(wal_path)
    [descriptor] = [r for r in written["records"]
                    if r.get("record") == "discharge-descriptor"]
    assert descriptor["entry"] == "transactional"
    # the named-call inverse: the declared undo's name, recovered from the
    # registered closure (no lower.py call-site metadata exists yet)
    assert descriptor["call"]["method"] == "unstash"
    seq = descriptor["seq"]

    session.unload()                     # clean unload: Frame.drain runs
    session.recorder.wal.close()         # <-- simulate the crash: no
                                          #     activation-complete marker

    loaded = replay.WriteAheadLog.read(wal_path)
    assert loaded["complete"] is False
    [discharge] = [r for r in loaded["records"] if r.get("record") == "discharge"]
    assert discharge["discharged"] == [seq]

    report = recover(wal_path)
    assert report["verdict"] == "rolled-back"
    # the committed seq is SKIPPED — not replayed — because its discharge
    # record is durable
    assert [s["seq"] for s in report["dischargedSkipped"]] == [seq]
    assert report["transactionalRolledBack"] == []
    referent = DictWorld().key(descriptor["call"])
    assert referent in report["residue"]["worldRemaining"]
    assert report["residue"]["clean"] is True
    assert "committed transactional" in report["residue"]["proof"]


@needs_cordis
def test_an_aborted_transactional_wal_descriptor_replays_on_recover(
        target, tmp_path, monkeypatch):
    """The contrast: the same WAL wiring, but the activation ABORTS
    mid-body. Registration (inside `Frame.transactional`, on the `Ok`
    branch) still ran and its descriptor is durable, but `Frame.drain`
    never runs on an abort — so no discharge record exists for it, and
    `revl recover` reconstructs and replays the declared inverse."""
    wal_path = str(tmp_path / "abort.wal")
    _wal_before_apply(monkeypatch, wal_path)

    session = _session()
    loaded_report = session.load(
        _ir(_witnessed_component("StashAbort", abort=True)), record=True)
    assert loaded_report["components"] == [{"name": "StashAbort", "state": "FAILED"}]

    session.recorder.wal.close()  # <-- simulate the crash: no activation-complete

    loaded = replay.WriteAheadLog.read(wal_path)
    assert loaded["complete"] is False
    [descriptor] = [r for r in loaded["records"]
                    if r.get("record") == "discharge-descriptor"]
    assert descriptor["entry"] == "transactional"
    # the abort unwind already replayed the inverse in-process (test 2 above)
    # but never reached `Frame.drain` — no discharge record was ever written
    assert not [r for r in loaded["records"] if r.get("record") == "discharge"]
    # item 309 §3a option (a), the headline scenario: this inverse is UNDECLARED
    # (no `undo idempotent`), so the abort's Phase-1 apply fsync-appended a
    # `replay-fence` for its seq BEFORE running it. That fence is what makes
    # at-most-once hold across abort-then-crash.
    assert [r["seq"] for r in loaded["records"]
            if r.get("record") == "replay-fence"] == [descriptor["seq"]]

    report = recover(wal_path)
    assert report["verdict"] == "rolled-back"
    # recover finds the seq undischarged AND fenced: it does NOT re-apply (the
    # abort already applied it once in-process), and defers the honest fenced
    # residue to a human. Without the abort-path fence this DOUBLE-applied.
    assert report["transactionalRolledBack"] == []
    assert [f["seq"] for f in report["fencedDeferred"]] == [descriptor["seq"]]
    assert report["dischargedSkipped"] == []
    assert report["residue"]["clean"] is False
    [res] = [r for r in report["residue"]["outstanding"]
             if r["kind"] == "fenced-residue"]
    assert "outcome unknown, will not re-run" in res["error"]["message"]


# ---------------------------------------------------------------------------
# item 247 (docs/design/teardown-contract.md): compensation is now a
# first-class COMPENSATION entry on the SAME per-activation LIFO stack as
# bracket and transactional entries (`Frame.compensation`), not a bare
# disposer yielded straight into cordis. These tests drive real cordis-py
# compositions proving the three load-bearing behaviors the contract's
# two-phase model requires:
#
#   * clean commit  -> the compensation is DISCHARGED, never runs (the
#                       emission was the deliverable)
#   * abort         -> Phase 1 (every bracket/transactional inverse in this
#                       activation) completes IN FULL before Phase 2 (the
#                       compensation) starts — proven by an on-disk order log
#                       both the transactional inverse and the compensation
#                       append to
#   * a failing compensation -> continue-and-record (`compensation-residue`);
#                       the abort still succeeds, it never raises out
#
# The externs below are a second toy fixture (not `_EXTERNS` above): the
# witnessed `stash`/`unstash` pair is reused verbatim for the transactional
# side of the phase-order proof, plus an emission `note`/compensating
# `offset` pair that appends to an on-disk order log so registration order
# ("which ran first") is directly observable, not inferred.
# ---------------------------------------------------------------------------

_COMP_EXTERNS = (
    "type Stash = { path: Str, bak: Str }\n"
    "type FsError = { code: Str }\n"
    # the transactional inverse: restores the file AND appends 'unstash' to
    # the order log, so its position relative to the compensation is directly
    # observable on disk after the abort.
    "extern pure fn unstash(w: Stash) -> Unit = @py {\n"
    "    import os\n"
    "    with open(os.environ['REVL_ORDER_LOG'], 'a', encoding='utf-8') as f:\n"
    "        f.write('unstash\\n')\n"
    "    if os.path.exists(w['bak']):\n"
    "        os.replace(w['bak'], w['path'])\n"
    "    return\n"
    "}\n"
    "extern witnessed[fs] fn stash() -> Result[Stash, FsError]"
    " undo unstash(result) = @py {\n"
    "    import os\n"
    "    path = os.environ['REVL_WIT_TARGET']\n"
    "    bak = path + '.bak'\n"
    "    os.replace(path, bak)\n"
    "    return Ok({'path': path, 'bak': bak})\n"
    "}\n"
    # the emission: a bare crossing, no reversal possible.
    "extern emission fn note(msg: Str) -> Unit = @py {\n"
    "    return\n"
    "}\n"
    # the compensation: a best-effort offset that appends 'compensate:<msg>'
    # to the same order log.
    "extern pure fn offset(msg: Str) -> Unit = @py {\n"
    "    import os\n"
    "    with open(os.environ['REVL_ORDER_LOG'], 'a', encoding='utf-8') as f:\n"
    "        f.write('compensate:' + msg + chr(10))\n"
    "    return\n"
    "}\n"
    # the contrast: a compensation that logs, THEN raises — proves a failed
    # offset still lands as residue and never blocks/fails the abort.
    "extern pure fn offset_fails(msg: Str) -> Unit = @py {\n"
    "    import os\n"
    "    with open(os.environ['REVL_ORDER_LOG'], 'a', encoding='utf-8') as f:\n"
    "        f.write('compensate:' + msg + chr(10))\n"
    "    raise RuntimeError('offset boom')\n"
    "}\n"
    # the bound-rule fixture: logs, then sleeps long enough that a small
    # `REVL_COMPENSATION_BUDGET_MS` is provably exhausted by the time the
    # NEXT (older) compensation in Phase 2 is checked.
    "extern pure fn offset_slow(msg: Str) -> Unit = @py {\n"
    "    import os, time\n"
    "    with open(os.environ['REVL_ORDER_LOG'], 'a', encoding='utf-8') as f:\n"
    "        f.write('compensate:' + msg + chr(10))\n"
    "    time.sleep(0.05)\n"
    "    return\n"
    "}\n"
)

_COMP_BASE = compile_source(_COMP_EXTERNS, "compensate.rvl")


def _compensated_component(name: str, *, abort: bool, offset_fails: bool = False) -> dict:
    """A witnessed `stash` (transactional, Phase 1) followed by an
    `emit ... compensate ...` saga (Phase 2), optionally followed by a `fail`
    so the activation aborts instead of committing cleanly."""
    offset_name = "offset_fails" if offset_fails else "offset"
    body = [
        {"step": "effect", "acquire": {"kind": "fn", "name": "stash", "args": []}},
        {"step": "emit",
         "expr": {"kind": "fn", "name": "note", "args": [{"kind": "lit", "value": "go"}]},
         "compensate": {"kind": "fn", "name": offset_name, "args": [{"kind": "lit", "value": "go"}]}},
    ]
    if abort:
        body.append({"step": "fail", "message": {"kind": "lit", "value": "boom"}})
    return {"name": name, "source": "compensate.rvl", "config": [], "requires": {}, "provides": {},
            "body": body}


def _comp_ir(component: dict) -> dict:
    ir = copy.deepcopy(_COMP_BASE)
    ir["components"] = [component]
    return ir


@pytest.fixture
def order_log(tmp_path, monkeypatch):
    path = tmp_path / "order.log"
    monkeypatch.setenv("REVL_ORDER_LOG", str(path))
    return path


def _order_lines(path) -> list:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


# ---------------------------------------------------------------------------
# 4. compensation + clean unload: DISCHARGED, never runs
# ---------------------------------------------------------------------------

@needs_cordis
def test_compensation_discharges_on_clean_commit(target, order_log):
    session = _session()
    session.load(_comp_ir(_compensated_component("CompClean", abort=False)))

    frame = _sole_frame(session)
    assert len(frame._compensations) == 1
    entry = frame._compensations[0]
    assert entry.ran is False and entry.discharged is False  # not yet unloaded

    session.unload()  # clean successful unload == implicit commit

    # discharged: the compensation never ran, the order log stays empty
    assert entry.discharged is True
    assert entry.ran is False
    assert entry.fn is None  # dropped, same GC discipline as _Transactional
    assert _order_lines(order_log) == []
    assert frame.compensation_residue == []


# ---------------------------------------------------------------------------
# 5. compensation + abort: PHASE 2, strictly after every Phase-1 inverse
# ---------------------------------------------------------------------------

@needs_cordis
def test_compensation_runs_in_phase2_after_transactional_on_abort(target, order_log):
    bak = target + ".bak"
    session = _session()

    report = session.load(_comp_ir(_compensated_component("CompAbort", abort=True)))
    assert report["components"] == [{"name": "CompAbort", "state": "FAILED"}]

    # Phase 1 fully reverted the transactional witnessed effect: the file is
    # back and residue-free, exactly like test_witnessed_reverts_on_abort.
    assert os.path.exists(target)
    assert not os.path.exists(bak)

    # the order log is the phase-order proof: the transactional inverse's
    # 'unstash' line precedes the compensation's 'compensate:go' line —
    # Phase 1 (bracket/transactional) completed in full before Phase 2
    # (compensation) started, not interleaved LIFO (the old, pre-247
    # ordering the TCK A5 case predates).
    assert _order_lines(order_log) == ["unstash", "compensate:go"]

    frame = _sole_frame(session)
    [comp] = frame._compensations
    assert comp.ran is True and comp.failed is False and comp.discharged is False
    [trans] = frame._transactional
    assert trans.replayed is True and trans.discharged is False
    assert frame.compensation_residue == []


# ---------------------------------------------------------------------------
# 6. a FAILING compensation: continue-and-record, the abort still succeeds
# ---------------------------------------------------------------------------

@needs_cordis
def test_a_failed_compensation_lands_as_residue_and_abort_still_succeeds(target, order_log):
    session = _session()

    # must not raise: a failed Phase-2 offset is best-effort, never fails
    # the abort (teardown-contract.md's continue-and-record rule).
    report = session.load(_comp_ir(
        _compensated_component("CompFails", abort=True, offset_fails=True)))
    assert report["components"] == [{"name": "CompFails", "state": "FAILED"}]

    # Phase 1 still ran to completion, unaffected by the Phase-2 failure that
    # comes after it.
    assert os.path.exists(target)

    # the compensation was ATTEMPTED (its log line landed) before it raised —
    # continue-and-record, not skip-and-hide.
    assert _order_lines(order_log) == ["unstash", "compensate:go"]

    frame = _sole_frame(session)
    [comp] = frame._compensations
    assert comp.ran is True and comp.failed is True
    assert "offset boom" in str(comp.error)

    [residue] = frame.compensation_residue
    assert residue["kind"] == "compensation-residue"
    assert residue["outcome"] == "failed"
    assert residue["attemptedFlag"] is True
    assert residue["error"]["type"] == "RuntimeError"
    assert "offset boom" in residue["error"]["message"]


# ---------------------------------------------------------------------------
# 7. emit seam (no cordis needed): compensate compiles through
# `Frame.compensation`, not a bare disposer; non-compensation programs are
# byte-identical (unaffected by this slice).
# ---------------------------------------------------------------------------

def test_compensate_call_site_emits_through_frame_compensation():
    emit = _emit_backend()
    body = emit.emit(_comp_ir(_compensated_component("CompClean", abort=False)))
    assert ("yield _revl_frame.compensation(lambda: offset('go'))" in body
            or "yield _revl_frame.compensation(lambda: offset(\"go\"))" in body)
    # not a bare disposer: no plain `yield lambda:` for the compensate step
    # (the witnessed step's own transactional yield is also absent here,
    # since `stash`'s registration itself is `_revl_frame.transactional`,
    # never a bare lambda either)
    assert "yield lambda:" not in body


def test_a_program_with_no_compensation_still_emits_byte_identically():
    """A program with no `compensate` clause must be untouched by this
    slice: still a bare disposer, never routed through `Frame.compensation`."""
    emit = _emit_backend()
    ir = compile_source(
        "service Bus { emission fn send(msg: Str) -> Int }\n"
        "component C requires bus: Bus { emit bus.send(\"hi\") }\n",
        "nocompensate.rvl")
    body = emit.emit(ir)
    assert "compensation(" not in body
    assert "_revl_frame.compensation" not in body


# ---------------------------------------------------------------------------
# 8. bridge slice: the compensation's WAL discharge-descriptor + its seq
# joining the SAME commit-path discharge record as transactional entries.
# ---------------------------------------------------------------------------

@needs_cordis
def test_a_committed_compensation_writes_a_durable_discharge_record_at_drain(
        target, order_log, tmp_path, monkeypatch):
    wal_path = str(tmp_path / "comp-commit.wal")
    _wal_before_apply(monkeypatch, wal_path)

    session = _session()
    session.load(_comp_ir(_compensated_component("CompWalClean", abort=False)), record=True)

    written = replay.WriteAheadLog.read(wal_path)
    [descriptor] = [r for r in written["records"]
                    if r.get("record") == "discharge-descriptor" and r.get("entry") == "compensation"]
    assert descriptor["call"]["method"] == "offset"
    seq = descriptor["seq"]

    session.unload()  # clean unload: Frame.drain runs
    session.recorder.wal.close()

    loaded = replay.WriteAheadLog.read(wal_path)
    [discharge] = [r for r in loaded["records"] if r.get("record") == "discharge"]
    assert seq in discharge["discharged"]


@needs_cordis
def test_an_aborted_compensation_wal_descriptor_carries_no_discharge_record(
        target, order_log, tmp_path, monkeypatch):
    wal_path = str(tmp_path / "comp-abort.wal")
    _wal_before_apply(monkeypatch, wal_path)

    session = _session()
    report = session.load(
        _comp_ir(_compensated_component("CompWalAbort", abort=True)), record=True)
    assert report["components"] == [{"name": "CompWalAbort", "state": "FAILED"}]

    session.recorder.wal.close()

    loaded = replay.WriteAheadLog.read(wal_path)
    [descriptor] = [r for r in loaded["records"]
                    if r.get("record") == "discharge-descriptor" and r.get("entry") == "compensation"]
    # the abort already ran the compensation best-effort in-process (test 5
    # above) but never reached `Frame.drain` — no discharge record for it,
    # so `revl recover` would still find and re-issue this descriptor
    # (through recovery.py's dedicated `apply_compensation` path).
    assert not [r for r in loaded["records"] if r.get("record") == "discharge"]
    assert descriptor["entry"] == "compensation"


# ---------------------------------------------------------------------------
# 9. the Phase-2 bound rule (docs/design/teardown-contract.md's config
# surface, `REVL_COMPENSATION_BUDGET_MS`): checked BETWEEN compensations,
# read once at activation. `0` means unbounded; a small positive value
# proves a later (older) compensation is skipped, recorded, never silently
# dropped, once the budget is exhausted by an earlier one.
# ---------------------------------------------------------------------------

def _two_compensations_component(name: str) -> dict:
    """Two `emit ... compensate ...` sagas plus a `fail`. Phase 2 is LIFO —
    newest first — so the SECOND-registered saga's compensation
    (`offset_slow`, which sleeps 50ms) is checked and runs FIRST; the
    FIRST-registered saga's compensation (`offset`) is checked SECOND, after
    `offset_slow` has already blown a small budget."""
    body = [
        {"step": "emit",
         "expr": {"kind": "fn", "name": "note", "args": [{"kind": "lit", "value": "first"}]},
         "compensate": {"kind": "fn", "name": "offset", "args": [{"kind": "lit", "value": "first"}]}},
        {"step": "emit",
         "expr": {"kind": "fn", "name": "note", "args": [{"kind": "lit", "value": "second"}]},
         "compensate": {"kind": "fn", "name": "offset_slow", "args": [{"kind": "lit", "value": "second"}]}},
        {"step": "fail", "message": {"kind": "lit", "value": "boom"}},
    ]
    return {"name": name, "source": "compensate.rvl", "config": [], "requires": {}, "provides": {},
            "body": body}


@needs_cordis
def test_a_tiny_budget_skips_and_records_a_later_compensation(order_log, monkeypatch):
    monkeypatch.setenv("REVL_COMPENSATION_BUDGET_MS", "10")
    session = _session()

    report = session.load(_comp_ir(_two_compensations_component("TinyBudget")))
    assert report["components"] == [{"name": "TinyBudget", "state": "FAILED"}]

    # the newer (offset_slow) compensation ran; the older (offset) one was
    # skipped once its budget check found the deadline already past.
    assert _order_lines(order_log) == ["compensate:second"]

    frame = _sole_frame(session)
    first_entry, second_entry = frame._compensations  # registration order
    assert second_entry.ran is True and second_entry.failed is False
    assert first_entry.ran is False and first_entry.failed is False

    [residue] = frame.compensation_residue
    assert residue["kind"] == "compensation-residue"
    assert residue["outcome"] == "not-attempted"
    assert residue["attemptedFlag"] is False
    assert residue["error"]["type"] == "deadline-expired"


@needs_cordis
def test_a_zero_budget_means_unbounded(order_log, monkeypatch):
    monkeypatch.setenv("REVL_COMPENSATION_BUDGET_MS", "0")
    session = _session()

    report = session.load(_comp_ir(_two_compensations_component("ZeroBudget")))
    assert report["components"] == [{"name": "ZeroBudget", "state": "FAILED"}]

    # `0` means no bound: both compensations ran despite the 50ms sleep.
    assert _order_lines(order_log) == ["compensate:second", "compensate:first"]
    frame = _sole_frame(session)
    assert all(entry.ran for entry in frame._compensations)
    assert frame.compensation_residue == []


def test_read_bound_seconds_env_parsing(monkeypatch):
    """Pure unit coverage of the env-var contract (docs/design/
    teardown-contract.md's "Budget values"): unset -> default; explicit ms
    -> seconds; `0` -> unbounded (`None`); unparsable -> default, never a
    crash over a malformed host env var."""
    sys.path.insert(0, str(_ROOT / "backends" / "python"))
    import runtime as runtime_mod

    monkeypatch.delenv("REVL_COMPENSATION_BUDGET_MS", raising=False)
    assert runtime_mod._read_bound_seconds("REVL_COMPENSATION_BUDGET_MS", 5000) == 5.0

    monkeypatch.setenv("REVL_COMPENSATION_BUDGET_MS", "250")
    assert runtime_mod._read_bound_seconds("REVL_COMPENSATION_BUDGET_MS", 5000) == 0.25

    monkeypatch.setenv("REVL_COMPENSATION_BUDGET_MS", "0")
    assert runtime_mod._read_bound_seconds("REVL_COMPENSATION_BUDGET_MS", 5000) is None

    monkeypatch.setenv("REVL_COMPENSATION_BUDGET_MS", "not-a-number")
    assert runtime_mod._read_bound_seconds("REVL_COMPENSATION_BUDGET_MS", 5000) == 5.0
