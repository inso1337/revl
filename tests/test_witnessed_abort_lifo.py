"""Witnessed inverses must replay in reverse INVOCATION order (LIFO) on abort —
roadmap item 369 (DATA-LOSS correctness hole under the 243/244/246 guarantee).

A witnessed effect is auto-approvable (item 246, class (a), NO prompt) *only*
because abort provably reverts it. That proof holds for a session's witnessed
ops only if their inverses replay LIFO: two ops whose paths OVERLAP must undo
newest-first, or the earlier-registered inverse runs first, finds nothing (every
stdlib/fs.rvl inverse is idempotent-and-total), no-ops, and the later inverse
then undoes into the hole — leaving residue or DESTROYING pre-session data,
silently (abort still reports `noResidue: true`).

The bug (measured on 608358c, py tier): inverses registered from PROVIDE-METHOD
invocations (`Frame.transactional_method` -> `_deferred_transactional`, the seam
every agent tool call fires through) were replayed in REGISTRATION order (FIFO)
by `Frame.drain`, not LIFO. The activation-body path was already LIFO (cordis
unwinds its disposer stack newest-first); only the mid-session deferred drain
was forward. The three reproducers below are the H1/H10 flows from revl-harness,
each a SINGLE component and (the last two) a SINGLE method — this is the frame's
replay order, not cross-component teardown.

Every op here goes through the REAL stdlib/fs.rvl `@py` bodies: the module
source is compiled together with a thin consumer component (same file, so the
parser admits `effect move(...)` on the witnessed externs), whose provide-method
fires one fs mutation per call — the exact shape of an agent calling one fs tool
repeatedly. `Frame.abort()` is the seam item 245's explicit reject drives.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
import replay  # noqa: E402,F401  (registers the cordis-py runtime bridge)
import revl_fs_workspace as ws  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the witnessed transactional teardown is proven against a live "
           "cordis-py composition — install it with `sh backends/python/setup.sh` "
           "and run under its venv",
)

# The REAL stdlib/fs.rvl module, plus a consumer whose provide-methods fire the
# witnessed mutations per tool call. Concatenated into ONE source so the
# witnessed externs are same-file (the cross-module `use` surface is a separate
# frontend slice; the runtime seam under test is identical either way).
_FS_SRC = (_ROOT / "stdlib" / "fs.rvl").read_text(encoding="utf-8")
_CONSUMER = """
service FsOps {
  emission fn mv(a: Str, b: Str)
  emission fn writef(p: Str, c: Str)
  emission fn rmf(p: Str)
  emission fn touchf(p: Str)
}
component Agent provides ops: FsOps {
  provide ops {
    fn mv(a, b) { effect move(a, b) }
    fn writef(p, c) { effect write(p, c) }
    fn rmf(p) { effect rm(p) }
    fn touchf(p) { effect write(p, "") }
  }
}
"""
_BASE = compile_source(_FS_SRC + "\n" + _CONSUMER, "witnessed_abort_lifo.rvl")


def _ir() -> dict:
    return copy.deepcopy(_BASE)


def _session():
    from revl.mcp.session import Session
    return Session()


def _sole_frame(session):
    driver = session._driver
    ((_name, fiber),) = driver.fibers.items()
    return driver.runtime._frame_for_ctx(fiber.ctx)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A session workspace root. The `@py` fs bodies read `REVL_FS_WORKSPACE`;
    every path passed to a mutation is taken relative to it."""
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(root))
    return root


def _drive(session, calls):
    """Fire each `(method, args)` mid-session (per tool call), then ABORT and
    unload — the witnessed inverses replay during the aborting teardown."""
    for method, args in calls:
        session.call("ops", method, args)
    frame = _sole_frame(session)
    frame.abort()
    return session.unload()


def _present(root: Path, names) -> list:
    return [n for n in names if (root / n).exists()]


# ===========================================================================
# Reproducer 1 (revl-harness H10): mv a b ; mv b c ; abort -> a.txt
# One method, one op, two invocations on OVERLAPPING paths. LIFO undoes
# unmove(c->b) then unmove(b->a), landing on a.txt. FIFO undoes b->a first
# (no-op: b absent), then c->b, landing on the observed WRONG b.txt.
# ===========================================================================

@needs_cordis
def test_repro1_chained_move_reverts_to_the_original_name(workspace):
    (workspace / "a.txt").write_text("A", encoding="utf-8")

    session = _session()
    session.load(_ir())
    result = _drive(session, [("mv", ["a.txt", "b.txt"]),
                              ("mv", ["b.txt", "c.txt"])])

    assert _present(workspace, ["a.txt", "b.txt", "c.txt"]) == ["a.txt"], \
        "chained move abort landed on the wrong name (FIFO replay)"
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "A"
    assert result["noResidue"], f"abort left residue: {result['checks']}"


# ===========================================================================
# Reproducer 2 (revl-harness H10): rm a ; touch a ; abort -> a.txt survives.
# DATA LOSS case: FIFO replays unrm (restore parked a) then restore(created)
# (delete the touched a) — destroying pre-session data. LIFO deletes the
# touched file first, then un-parks the original.
# ===========================================================================

@needs_cordis
def test_repro2_rm_then_recreate_preserves_pre_session_data(workspace):
    (workspace / "a.txt").write_text("ORIG", encoding="utf-8")

    session = _session()
    session.load(_ir())
    result = _drive(session, [("rmf", ["a.txt"]),
                              ("touchf", ["a.txt"])])

    assert (workspace / "a.txt").exists(), \
        "abort DESTROYED pre-session data (rm/recreate replayed FIFO)"
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "ORIG"
    assert result["noResidue"], f"abort left residue: {result['checks']}"


# ===========================================================================
# Reproducer 3 (revl-harness H1, already shipping): write V2 ; write V3 ; abort
# -> the pre-session V1. FIFO restores V1 then restores the V2 snapshot over the
# top, leaving an intermediate that NEVER existed before the session.
# ===========================================================================

@needs_cordis
def test_repro3_double_overwrite_reverts_to_pre_session_bytes(workspace):
    (workspace / "a.txt").write_text("V1", encoding="utf-8")

    session = _session()
    session.load(_ir())
    result = _drive(session, [("writef", ["a.txt", "V2"]),
                              ("writef", ["a.txt", "V3"])])

    assert (workspace / "a.txt").read_text(encoding="utf-8") == "V1", \
        "abort left an intermediate version (FIFO replay of the preimage snapshots)"
    assert result["noResidue"], f"abort left residue: {result['checks']}"


# ===========================================================================
# A longer overlapping chain: prove LIFO across more than two invocations.
# ===========================================================================

@needs_cordis
def test_deep_overlapping_write_chain_reverts_to_pre_session(workspace):
    (workspace / "a.txt").write_text("V0", encoding="utf-8")

    session = _session()
    session.load(_ir())
    result = _drive(session, [("writef", ["a.txt", f"V{i}"]) for i in range(1, 6)])

    assert (workspace / "a.txt").read_text(encoding="utf-8") == "V0"
    assert result["noResidue"], f"abort left residue: {result['checks']}"


# ===========================================================================
# 243 §2's "mixed-entry LIFO" claim, TESTED not asserted: a `transactional`
# (witnessed) disposer joins the SAME LIFO disposer stack as every bracket
# (`acquire`) inverse, so on abort they unwind in ONE reverse-registration
# order across both kinds. This exercises the activation-body path (which was
# already correct — cordis unwinds its disposer stack LIFO); it is the
# regression guard proving the item 369 deferred-drain fix preserved it.
#
# Each inverse (the witnessed `undo logmark`, the acquire `undo logmark`)
# appends its label to a shared log, so the log's contents ARE the observed
# teardown order.
# ===========================================================================

_MIXED_LOG_ENV = "REVL_369_MIXED_LOG"

_MIXED_EXTERNS = """
type Mark = { label: Str }
extern pure fn logmark(w: Mark) -> Unit = @py {
    import os
    with open(os.environ[\"REVL_369_MIXED_LOG\"], \"a\", encoding=\"utf-8\") as fh:
        fh.write(w[\"label\"] + \"\\n\")
    return
}
extern witnessed[fs] fn wmark(label: Str) -> Result[Mark, Mark]
    undo logmark(result) = @py {
    return Ok({\"label\": label})
}
extern acquire fn amark(label: Str) -> Mark undo logmark(result) = @py {
    return {\"label\": label}
}
"""
_MIXED_BASE = compile_source(_MIXED_EXTERNS, "mixed_lifo.rvl")


def _wmark_step(label: str) -> dict:
    return {"step": "effect",
            "acquire": {"kind": "fn", "name": "wmark",
                        "args": [{"kind": "lit", "value": label}]}}


def _amark_step(label: str, bind: str) -> dict:
    return {"step": "let-effect", "bind": bind,
            "acquire": {"kind": "fn", "name": "amark",
                        "args": [{"kind": "lit", "value": label}]},
            "undo": {"kind": "fn", "name": "logmark",
                     "args": [{"kind": "name", "id": bind}]}}


@needs_cordis
def test_mixed_bracket_and_transactional_unwind_in_one_lifo_stack(tmp_path, monkeypatch):
    log = tmp_path / "teardown-order.log"
    monkeypatch.setenv(_MIXED_LOG_ENV, str(log))

    # interleave acquire (bracket) and witnessed (transactional) in the
    # activation body, then `fail` so activation aborts mid-body and every
    # inverse replays.
    body = [
        _amark_step("a1", "h1"),
        _wmark_step("w2"),
        _amark_step("a3", "h3"),
        _wmark_step("w4"),
        {"step": "fail", "message": {"kind": "lit", "value": "boom"}},
    ]
    component = {"name": "Mix", "source": "mixed_lifo.rvl", "config": [],
                 "requires": {}, "provides": {}, "body": body}
    ir = copy.deepcopy(_MIXED_BASE)
    ir["components"] = [component]

    session = _session()
    session.load(ir)   # aborts mid-body; the load surfaces the failure
    session.unload()

    order = log.read_text(encoding="utf-8").split() if log.exists() else []
    assert order == ["w4", "a3", "w2", "a1"], \
        "mixed bracket/transactional teardown was not one LIFO stack (243 §2)"
