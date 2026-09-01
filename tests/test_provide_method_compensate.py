"""Compensation (`emit ... compensate ...`) in a PROVIDE-METHOD body — roadmap
item 247 (method-body compensate remainder), the method-body analog of item 247.

Design: docs/design/teardown-contract.md (the two-phase teardown), item 247
(activation-body compensation), item 318/324 (per-tool-call method wiring).

Item 247 made an activation-body `emit ... compensate ...` a first-class
COMPENSATION entry on the frame (`Frame.compensation`): abort-only, discharged
on a clean commit, drained in PHASE 2 after every proof inverse, guarded and
residue-collected. But the METHOD-body site (`backends/python/emit.py`'s
`_method_step`) was left on the PLACEHOLDER lowering: it adopted a bare
`yield lambda: <compensation>` as a sibling `ctx.effect`, i.e. a BRACKET
disposer. A bracket disposer:

  * fires on CLEAN teardown (the offset runs AFTER a successful commit,
    destroying the deliverable the emission was);
  * is interleaved with the proof inverses during the Phase-1 unwind
    (a raising offset can leave a later proof inverse un-run);
  * is unguarded (a raise interrupts the proof unwind mid-drain).

This suite proves the method-body site now routes through
`Frame.compensation_method` — the compensation analog of item 318's
`Frame.transactional_method` — so a per-tool-call `emit ... compensate ...`:

  * DISCHARGES on a clean session/component unload (commit): the offset never
    runs, the emission is the deliverable and survives;
  * FIRES on an ABORT (`Frame.abort()` — item 245's reject seam), in PHASE 2,
    strictly AFTER the method's proof (transactional) inverse, guarded and
    residue-collected.

The transactional inverse and the compensation both append to one on-disk
order log, so the phase ordering ("proof inverse first, compensation second")
is OBSERVED on disk, not inferred.
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

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the two-phase teardown is proven against a live cordis-py "
           "composition — install it with `sh backends/python/setup.sh` and "
           "run under its venv",
)

# A per-tool-call method body with BOTH entry kinds in one call:
#   * `effect stash()` — a witnessed transactional mutation (Phase-1 proof
#     inverse); `unstash` restores the file AND logs 'unstash'.
#   * `emit note(msg) compensate offset(msg)` — the emission deliverable plus
#     its offsetting compensation (Phase 2); `offset` logs 'compensate:<msg>'.
_SOURCE = (
    "type Stash = { path: Str, bak: Str }\n"
    "type FsError = { code: Str }\n"
    "extern pure fn unstash(w: Stash) -> Unit = @py {\n"
    "    import os\n"
    "    with open(os.environ['REVL_ORDER_LOG'], 'a', encoding='utf-8') as f:\n"
    "        f.write('unstash\\n')\n"
    "    if os.path.exists(w['bak']):\n"
    "        os.replace(w['bak'], w['path'])\n"
    "    return\n"
    "}\n"
    "extern witnessed[fs] fn stash(p: Str) -> Result[Stash, FsError]"
    " undo unstash(result) = @py {\n"
    "    import os\n"
    "    bak = p + '.bak'\n"
    "    os.replace(p, bak)\n"
    "    return Ok({'path': p, 'bak': bak})\n"
    "}\n"
    # the emission: a bare boundary crossing, no reversal possible.
    "extern emission fn note(msg: Str) -> Unit = @py { return }\n"
    # the compensation: a best-effort offset that logs 'compensate:<msg>'.
    "extern pure fn offset(msg: Str) -> Unit = @py {\n"
    "    import os\n"
    "    with open(os.environ['REVL_ORDER_LOG'], 'a', encoding='utf-8') as f:\n"
    "        f.write('compensate:' + msg + chr(10))\n"
    "    return\n"
    "}\n"
    # a compensation that logs THEN raises — proves a failed Phase-2 offset is
    # best-effort (continue-and-record), never fails or interrupts the abort.
    "extern pure fn offset_fails(msg: Str) -> Unit = @py {\n"
    "    import os\n"
    "    with open(os.environ['REVL_ORDER_LOG'], 'a', encoding='utf-8') as f:\n"
    "        f.write('compensate:' + msg + chr(10))\n"
    "    raise RuntimeError('offset boom')\n"
    "}\n"
    "service Ops { emission fn run(p: Str, msg: Str) }\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn run(p, msg) {\n"
    "      effect stash(p)\n"
    "      emit note(msg) compensate offset(msg)\n"
    "    }\n"
    "  }\n"
    "}\n"
)

# a variant whose compensation raises, for the residue/best-effort proof.
_SOURCE_FAILS = _SOURCE.replace(
    "emit note(msg) compensate offset(msg)",
    "emit note(msg) compensate offset_fails(msg)")

_BASE = compile_source(_SOURCE, "provide_method_compensate.rvl")
_BASE_FAILS = compile_source(_SOURCE_FAILS, "provide_method_compensate.rvl")


def _ir(base=_BASE) -> dict:
    return copy.deepcopy(base)


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
def target(tmp_path, monkeypatch):
    """The witnessed-stash target file plus the shared order log."""
    p = tmp_path / "artifact.txt"
    p.write_text("deliverable", encoding="utf-8")
    monkeypatch.setenv("REVL_ORDER_LOG", str(tmp_path / "order.log"))
    return str(p)


def _order_lines(tmp_env=None) -> list:
    path = Path(os.environ["REVL_ORDER_LOG"])
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _mutated(path: str) -> bool:
    return not os.path.exists(path) and os.path.exists(path + ".bak")


def _pristine(path: str) -> bool:
    return os.path.exists(path) and not os.path.exists(path + ".bak")


# ---------------------------------------------------------------------------
# 1. clean unload (commit): the compensation DISCHARGES — the offset never runs,
#    the emission is the deliverable and survives. (RED under the placeholder
#    lowering, which fires the bracket disposer on clean teardown.)
# ---------------------------------------------------------------------------

@needs_cordis
def test_method_compensation_discharges_on_clean_unload(target):
    session = _session()
    session.load(_ir())

    frame = _sole_frame(session)
    assert frame._compensations == []          # nothing until a tool call
    assert getattr(frame, "_deferred_compensations", []) == []

    session.call("ops", "run", [target, "go"])
    assert _mutated(target), "the witnessed mutation did not apply on the call"

    # one compensation entry parked, not yet run
    assert len(frame._compensations) == 1
    assert len(getattr(frame, "_deferred_compensations", [])) == 1
    comp = frame._compensations[0]
    assert comp.ran is False and comp.discharged is False

    session.unload()  # clean unload == implicit commit

    # DISCHARGED: the offset never ran, so the order log has NO 'compensate'
    # line. The witnessed mutation persists (the deliverable).
    assert comp.discharged is True
    assert comp.ran is False
    assert comp.fn is None                     # GC'd, same discipline as commit
    assert "compensate:go" not in _order_lines()
    assert _mutated(target), "clean unload wrongly reverted the deliverable"
    assert frame.compensation_residue == []


# ---------------------------------------------------------------------------
# 2. abort: the compensation FIRES in PHASE 2, strictly after the method's
#    transactional proof inverse, and the witnessed mutation reverts.
# ---------------------------------------------------------------------------

@needs_cordis
def test_method_compensation_fires_in_phase2_after_proof_inverse_on_abort(target):
    session = _session()
    session.load(_ir())
    frame = _sole_frame(session)

    session.call("ops", "run", [target, "go"])
    assert _mutated(target)

    comp = frame._compensations[0]
    frame.abort()                              # item 245's reject seam
    session.unload()

    # the order log is the phase-order proof: the transactional inverse's
    # 'unstash' precedes the compensation's 'compensate:go' — Phase 1 (the
    # proof inverse) completed before Phase 2 (the compensation) started.
    assert _order_lines() == ["unstash", "compensate:go"]

    assert comp.ran is True and comp.failed is False and comp.discharged is False
    [trans] = frame._transactional
    assert trans.replayed is True and trans.discharged is False
    assert _pristine(target), "abort did not revert the witnessed mutation"
    assert frame.compensation_residue == []


# ---------------------------------------------------------------------------
# 3. a FAILING compensation: continue-and-record, the abort still succeeds and
#    the proof inverse still ran to completion (guarded).
# ---------------------------------------------------------------------------

@needs_cordis
def test_failing_method_compensation_lands_as_residue_and_abort_succeeds(target):
    session = _session()
    session.load(_ir(_BASE_FAILS))
    frame = _sole_frame(session)

    session.call("ops", "run", [target, "go"])
    comp = frame._compensations[0]

    frame.abort()
    session.unload()                           # must not raise

    # the proof inverse ran BEFORE the failing compensation and is unaffected by
    # its raise; the offset was attempted (its line landed) then recorded.
    assert _order_lines() == ["unstash", "compensate:go"]
    assert _pristine(target), "the guarded compensation failure corrupted the abort"

    assert comp.ran is True and comp.failed is True
    residue = frame.compensation_residue
    assert len(residue) == 1
    assert residue[0]["outcome"] == "failed"
    assert residue[0]["error"]["type"] == "RuntimeError"


# ---------------------------------------------------------------------------
# 4. a method with NO compensation: byte-identity is covered by the goldens;
#    here we simply assert the emission fires and no compensation entry appears.
# ---------------------------------------------------------------------------

@needs_cordis
def test_plain_method_emit_registers_no_compensation(target):
    src = (
        "extern emission fn note(msg: Str) -> Unit = @py { return }\n"
        "service Ops { emission fn run(msg: Str) }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops { fn run(msg) { emit note(msg) } }\n"
        "}\n"
    )
    ir = compile_source(src, "plain.rvl")
    session = _session()
    session.load(ir)
    frame = _sole_frame(session)
    session.call("ops", "run", ["go"])
    assert frame._compensations == []
    assert getattr(frame, "_deferred_compensations", []) == []
    session.unload()
