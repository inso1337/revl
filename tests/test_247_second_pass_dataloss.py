"""Second-pass adversarial-review data-loss holes in the item-247 compensation /
witnessed-teardown machinery, and their fixes (F1-F5).

The reversal guarantee is: a witnessed op's undo, or an `emit ... compensate ...`
offset, must ALWAYS run on abort and NEVER on a clean commit. A second-pass
review found five ways an offset/undo was silently LOST on abort. Each test here
reproduces the review's probe against the live cordis-py runtime and proves the
hole is closed. The regression tests re-assert the guarantee's SOUND half (no
offset on a clean commit) and the pre-existing mid-activation LIFO behaviour.

  * F1 (probe1/probe1b) — an ACTIVATION-BODY `emit ... compensate ...` offset is
    LOST on every POST-activation abort (`Frame.abort()` + unload, and
    `Session.abort()`). Root: py disposed the body LIFO with `drain` first, so
    the activation-body compensation disposers enqueued AFTER `drain` had already
    drained Phase 2. Fix: a `begin` sentinel yielded first (disposed LAST) is the
    post-unwind hook, mirroring the ts/go tiers.
  * F2 (probe3) — a hot-swap ORPHANS the escrow: the predecessor generation's
    withdrawn entries escrow into the old owner, which `_install_session_owner`
    then replaced without carrying them over. A pre-swap witnessed mutation was
    neither reverted on abort nor discharged on commit. Fix: transfer the escrow.
  * F3 (probe4) — an aborting `unload` replayed NEITHER the live commit path nor
    the escrow: escrowed entries were dropped un-reverted. Fix: the aborting
    branch marks the escrowed frames and calls `finalize_abort`.
  * F5 (probe5) — the escrow replayed FIFO when no WAL was attached (seq is None,
    the sort key collapsed to a constant): overlapping idempotent-total inverses
    destroyed pre-session data. Fix: a monotonic `stamp` tiebreaker so LIFO holds
    with and without a WAL.

F4 is fixed by F1 (the same lost-offset root cause on the frame-abort path).
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
    reason="the teardown reversal guarantee is proven against a live cordis-py "
           "composition — install it with `sh backends/python/setup.sh` and run "
           "under its venv",
)


def _session():
    from revl.mcp.session import Session
    return Session()


def _sole_frame(session):
    """The single activation frame reachable through the loaded driver."""
    driver = session._driver
    ((_name, fiber),) = driver.fibers.items()
    return driver.runtime._frame_for_ctx(fiber.ctx)


def _sole_registered_frame(session):
    """The one activation frame in the current generation's owner registry."""
    ((frame,),) = (session._owner._registry,)
    return frame


# ===========================================================================
# F1 — an activation-body emit-compensate offset RUNS on a POST-activation
# abort (frame.abort()+unload AND session.abort()), or is surfaced as residue.
# ===========================================================================

# An activation body (NOT a provide-method) that emits a bare crossing and
# registers an offsetting compensation. `offset` appends 'compensate:<msg>' to an
# order log; the activation completes cleanly, so the compensation is only owed
# if the component is aborted LATER — the exact F1 path.
_COMP_EXTERNS = (
    "extern emission fn note(msg: Str) -> Unit = @py { return }\n"
    "extern pure fn offset(msg: Str) -> Unit = @py {\n"
    "    import os\n"
    "    with open(os.environ['REVL_ORDER_LOG'], 'a', encoding='utf-8') as f:\n"
    "        f.write('compensate:' + msg + chr(10))\n"
    "    return\n"
    "}\n"
)
_COMP_BASE = compile_source(_COMP_EXTERNS, "compensate.rvl")


def _activation_compensated_component(name: str) -> dict:
    """A component whose ACTIVATION BODY does `emit note compensate offset` and
    then completes cleanly (no `fail` — the activation commits, the offset is
    owed only on a later abort)."""
    body = [
        {"step": "emit",
         "expr": {"kind": "fn", "name": "note", "args": [{"kind": "lit", "value": "go"}]},
         "compensate": {"kind": "fn", "name": "offset",
                        "args": [{"kind": "lit", "value": "go"}]}},
    ]
    return {"name": name, "source": "compensate.rvl", "config": [],
            "requires": {}, "provides": {}, "body": body}


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


@needs_cordis
def test_F1_probe1_activation_offset_runs_on_frame_abort_then_unload(order_log):
    """probe1: the activation-body offset RUNS on `Frame.abort()` + unload. Pre
    -fix it was silently lost — the order log stayed empty."""
    session = _session()
    session.load(_comp_ir(_activation_compensated_component("CompA")))

    frame = _sole_frame(session)
    [comp] = frame._compensations
    assert comp.ran is False and comp.discharged is False  # activation committed

    frame.abort()          # a session-level reject of the already-activated work
    session.unload()       # aborting teardown

    assert _order_lines(order_log) == ["compensate:go"], (
        "the activation-body emit-compensate offset did NOT run on a "
        "frame.abort()+unload — the F1 data-loss hole")
    assert comp.ran is True and comp.discharged is False
    # the offset landed cleanly, so it is not surfaced as unresolved residue
    assert frame.compensation_residue == []


@needs_cordis
def test_F1_probe1b_activation_offset_runs_on_session_abort(order_log):
    """probe1b: the activation-body offset RUNS on `Session.abort()`."""
    session = _session()
    session.load(_comp_ir(_activation_compensated_component("CompB")))

    frame = _sole_frame(session)
    result = session.abort()

    assert result["aborted"]
    assert _order_lines(order_log) == ["compensate:go"], (
        "the activation-body emit-compensate offset did NOT run on "
        "session.abort() — the F1 data-loss hole")
    assert frame.compensation_residue == []


@needs_cordis
def test_F1_lost_offset_that_cannot_land_is_surfaced_as_residue(order_log, monkeypatch):
    """F1 corollary: if the recovered offset itself RAISES it must surface as
    `unresolved` residue at the abort boundary — never run-and-dropped."""
    source = (
        "extern emission fn note(msg: Str) -> Unit = @py { return }\n"
        "extern pure fn offset_fails(msg: Str) -> Unit = @py {\n"
        "    raise RuntimeError('offset boom')\n"
        "}\n"
    )
    base = compile_source(source, "compensate.rvl")
    ir = copy.deepcopy(base)
    ir["components"] = [{
        "name": "CompFails", "source": "compensate.rvl", "config": [],
        "requires": {}, "provides": {},
        "body": [{"step": "emit",
                  "expr": {"kind": "fn", "name": "note",
                           "args": [{"kind": "lit", "value": "go"}]},
                  "compensate": {"kind": "fn", "name": "offset_fails",
                                 "args": [{"kind": "lit", "value": "go"}]}}]}]
    session = _session()
    session.load(ir)
    report = session.abort()           # must not raise (continue-and-record)

    residue = report.get("compensationResidue")
    assert residue, "a failed post-activation offset was NOT surfaced as residue"
    assert any(r["outcome"] == "failed" and r.get("state") == "unresolved"
               for r in residue)
    assert report["prompts"]["residue"] >= 1


# ===========================================================================
# F2 — a witnessed mutation in gen1, swap to gen2, session.abort() REVERTS the
# pre-swap mutation; commit_confirm discharges its WAL descriptor.
# ===========================================================================

_STASH = (
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
    "service Ops { emission fn stash(p: Str) }\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops { fn stash(p) { effect stash_path(p) } }\n"
    "}\n"
)
_STASH_BASE = compile_source(_STASH, "stash.rvl")


def _stash_ir() -> dict:
    return copy.deepcopy(_STASH_BASE)


@pytest.fixture
def artifact(tmp_path):
    p = tmp_path / "artifact.txt"
    p.write_text("deliverable", encoding="utf-8")
    return str(p)


def _mutated(path: str) -> bool:
    return not os.path.exists(path) and os.path.exists(path + ".bak")


def _pristine(path: str) -> bool:
    return os.path.exists(path) and not os.path.exists(path + ".bak")


@needs_cordis
def test_F2_probe3_preswap_mutation_reverts_on_abort(artifact):
    """probe3: a witnessed mutation made in gen1, then a hot-swap to gen2, then
    `Session.abort()` — the PRE-swap mutation is reverted. Pre-fix the escrow was
    orphaned in the replaced owner and the abort reverted nothing."""
    session = _session()
    session.load(_stash_ir(), record=True)

    session.call("ops", "stash", [artifact])       # PRE-swap witnessed mutation
    assert _mutated(artifact), "pre-swap witnessed mutation did not apply"

    session.swap(_stash_ir())                        # withdraw gen1 -> escrow

    # the escrow was carried onto the successor owner (the fix), not orphaned.
    assert session._owner._escrow, (
        "the pre-swap witnessed entry was orphaned by the swap — the successor "
        "owner has an empty escrow (the F2 data-loss hole)")

    result = session.abort()
    assert result["aborted"]
    assert _pristine(artifact), (
        "abort after a swap did NOT revert the PRE-swap witnessed mutation — it "
        "was made permanent (the F2 escrow-orphaning data-loss bug)")
    assert result["noResidue"], result["checks"]


def _open_wal_once(monkeypatch, wal_path: str) -> None:
    """Open a real WAL before the FIRST activation is instrumented so every
    witnessed crossing gets a durable descriptor + seq, and keep it across the
    swap (open once, never reopen)."""
    real_instrument = replay.Recorder.instrument

    def _open_then_instrument(self, *args, **kwargs):
        if self.wal is None:
            self.open_wal(wal_path, generation=1)
        return real_instrument(self, *args, **kwargs)

    monkeypatch.setattr(replay.Recorder, "instrument", _open_then_instrument)


@needs_cordis
def test_F2_commit_confirm_discharges_the_preswap_wal_descriptor(
        artifact, tmp_path, monkeypatch):
    """probe3, commit half: the pre-swap witnessed descriptor is DISCHARGED on
    commit_confirm (its seq joins the consolidated discharge record), so a later
    `revl recover` skips it instead of rolling back committed work. Pre-fix the
    orphaned escrow's seq was never named and recover would roll it back."""
    wal_path = str(tmp_path / "session.wal")
    _open_wal_once(monkeypatch, wal_path)

    session = _session()
    session.load(_stash_ir(), record=True)
    session.call("ops", "stash", [artifact])
    session.swap(_stash_ir())

    escrow_seqs = sorted(e.seq for e in session._owner._escrow)
    assert escrow_seqs and all(s is not None for s in escrow_seqs), (
        "the transferred escrow lost its WAL seq")

    manifest = session.commit()
    confirm = session.commit_confirm(manifest["hash"])
    assert confirm["committed"] is True
    assert all(s in confirm["discharged"] for s in escrow_seqs), (
        "the pre-swap witnessed descriptor was NOT discharged on commit — a "
        "recover would wrongly roll back the committed pre-swap mutation")
    # committed work persists (the mutation is the deliverable, never reverted)
    assert _mutated(artifact)

    written = replay.WriteAheadLog.read(wal_path)
    discharged = [s for r in written["records"] if r["record"] == "discharge"
                  for s in r["discharged"]]
    assert all(s in discharged for s in escrow_seqs)


# ===========================================================================
# F3 — an aborting `unload` after a withdrawal REVERTS the escrowed mutation.
# ===========================================================================

@needs_cordis
def test_F3_probe4_aborting_unload_reverts_escrowed_mutation(artifact):
    """probe4: a pre-swap witnessed mutation is escrowed by the swap; then a live
    frame is marked aborting and the session is UNLOADED (not the explicit
    `abort` verb). The escrowed mutation is reverted. Pre-fix the aborting unload
    branch replayed neither the commit path nor the escrow, dropping it."""
    session = _session()
    session.load(_stash_ir(), record=True)
    session.call("ops", "stash", [artifact])         # pre-swap mutation
    assert _mutated(artifact)

    session.swap(_stash_ir())                          # withdraw -> escrow
    _sole_registered_frame(session).abort()            # make the unload aborting

    report = session.unload()
    assert report["unloaded"]
    assert _pristine(artifact), (
        "an aborting unload did NOT revert the escrowed witnessed mutation — it "
        "was dropped un-reverted (the F3 data-loss hole)")


# ===========================================================================
# F5 — two overlapping escrowed inverses replay LIFO WITHOUT a WAL.
# ===========================================================================

# setval(path, val) overwrites `path`, capturing the previous content as its
# witness; `restore` writes that previous content back — an idempotent-and-total
# inverse. Two calls to the SAME path OVERLAP: only a LIFO replay lands on the
# pre-session content; a FIFO replay leaves the newer value's stale predecessor.
_SETVAL = (
    "type W = { path: Str, prev: Str, existed: Bool }\n"
    "type E = { code: Str }\n"
    "extern pure fn restore(w: W) -> Unit = @py {\n"
    "    import os\n"
    "    if w['existed']:\n"
    "        with open(w['path'], 'w', encoding='utf-8') as f: f.write(w['prev'])\n"
    "    else:\n"
    "        if os.path.exists(w['path']): os.remove(w['path'])\n"
    "    return\n"
    "}\n"
    "extern witnessed[fs] fn setval(path: Str, val: Str) -> Result[W, E]"
    " undo restore(result) = @py {\n"
    "    import os\n"
    "    existed = os.path.exists(path)\n"
    "    prev = open(path, encoding='utf-8').read() if existed else ''\n"
    "    with open(path, 'w', encoding='utf-8') as f: f.write(val)\n"
    "    return Ok({'path': path, 'prev': prev, 'existed': existed})\n"
    "}\n"
    "service Ops { emission fn write(path: Str, val: Str) }\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops { fn write(path, val) { effect setval(path, val) } }\n"
    "}\n"
)
_SETVAL_BASE = compile_source(_SETVAL, "setval.rvl")


@needs_cordis
def test_F5_probe5_overlapping_escrow_replays_lifo_without_a_wal(tmp_path):
    """probe5: two overlapping witnessed writes are escrowed by a swap, in a NON
    -recorded session (no WAL, so every seq is None). The abort must replay them
    LIFO — restoring the pre-session content. Pre-fix the seq-only sort key
    collapsed to a constant and the stable sort replayed FIFO, leaving the stale
    intermediate value (the item-369 overlap hazard, `noResidue: true`)."""
    path = tmp_path / "f.txt"
    path.write_text("ORIGINAL", encoding="utf-8")
    target = str(path)

    session = _session()
    session.load(copy.deepcopy(_SETVAL_BASE), record=False)   # NO WAL: seq None
    session.call("ops", "write", [target, "ONE"])
    session.call("ops", "write", [target, "TWO"])
    assert path.read_text(encoding="utf-8") == "TWO"

    # confirm the precondition the bug needed: no WAL, so the entries' seqs are
    # None and only the monotonic stamp can order them.
    frame = _sole_registered_frame(session)
    assert [e.seq for e in frame._deferred_transactional] == [None, None]

    session.swap(copy.deepcopy(_SETVAL_BASE))                  # escrow both
    assert len(session._owner._escrow) == 2

    result = session.abort()
    assert result["aborted"]
    assert path.read_text(encoding="utf-8") == "ORIGINAL", (
        "a no-WAL escrow replayed FIFO: the overlapping inverses left the stale "
        "intermediate value instead of the pre-session content (the F5 hazard)")


# ===========================================================================
# REGRESSION — the SOUND half: no offset/undo EVER fires on a clean commit.
# ===========================================================================

@needs_cordis
def test_regression_no_compensate_on_clean_commit(order_log):
    """The reversal guarantee's sound half: an activation-body offset is
    DISCHARGED (never runs) on a clean unload / implicit commit — the `begin`
    sentinel must not change this."""
    session = _session()
    session.load(_comp_ir(_activation_compensated_component("CompClean")))

    frame = _sole_frame(session)
    [comp] = frame._compensations

    session.unload()   # clean successful unload == implicit commit

    assert comp.discharged is True and comp.ran is False
    assert comp.fn is None
    assert _order_lines(order_log) == [], (
        "an offset fired on a CLEAN commit — the deliverable was destroyed")
    assert frame.compensation_residue == []


@needs_cordis
def test_regression_witnessed_mutation_persists_on_clean_commit(artifact):
    """A witnessed mutation is the deliverable: a clean unload discharges its
    inverse (never reverts). The `begin` sentinel must not perturb this."""
    session = _session()
    session.load(_stash_ir())
    session.call("ops", "stash", [artifact])
    assert _mutated(artifact)

    report = session.unload()
    assert report["unloaded"]
    assert _mutated(artifact), "a clean unload wrongly reverted the deliverable"
    assert report["noResidue"], report["checks"]
