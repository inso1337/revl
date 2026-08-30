"""Compensation-residue surfacing — roadmap item 247 gap 2 (the third audit
state, `docs/design/247-compensate.md` Decision 2 / Slice 3).

The compensate RUNTIME core landed (item 247): a failed Phase-2 offset is
collected into `Frame.compensation_residue`, guarded, best-effort, the abort
still succeeds. But that residue had ZERO consumers outside the runtime — the
design's promised THIRD audit state (`bare` / `compensated` / `unresolved`) did
not exist on the audit surface (`revl.query` / `revl.erase_report`), and no
item-246 session-boundary report read it. "Never silently swallowed" was only
true in the sense that an in-memory list grew.

This suite proves the surfacing:

  * a session that ABORTS where a compensation FAILS surfaces `unresolved`
    residue at the 246 session boundary (the abort report), and bumps the
    residue prompt — not silently swallowed;
  * the audit surface (`revl.query.compensation_audit`) classifies each
    crossing into the three states from the residue;
  * a clean-compensated session reports `compensated`, a no-compensation
    crossing reports `bare`.

The runtime-boundary halves need a live cordis-py composition; the pure
audit-surface classifier does not.
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
    reason="the abort/session-boundary proof is driven against a live "
           "cordis-py composition — install it with `sh backends/python/setup.sh`",
)

# One provide-method that crosses the boundary with an offset that RAISES, so
# the Phase-2 compensation fails and lands as residue. Mirrors the fixture in
# tests/test_provide_method_compensate.py.
_SOURCE_FAILS = (
    "extern emission fn note(msg: Str) -> Unit = @py { return }\n"
    "extern pure fn offset_fails(msg: Str) -> Unit = @py {\n"
    "    raise RuntimeError('offset boom')\n"
    "}\n"
    "service Ops { emission fn run(msg: Str) }\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn run(msg) {\n"
    "      emit note(msg) compensate offset_fails(msg)\n"
    "    }\n"
    "  }\n"
    "}\n"
)

_SOURCE_CLEAN = _SOURCE_FAILS.replace(
    "extern pure fn offset_fails(msg: Str) -> Unit = @py {\n"
    "    raise RuntimeError('offset boom')\n"
    "}\n",
    "extern pure fn offset(msg: Str) -> Unit = @py { return }\n",
).replace("compensate offset_fails(msg)", "compensate offset(msg)")

_BASE_FAILS = compile_source(_SOURCE_FAILS, "residue_surfacing.rvl")


def _ir(base=_BASE_FAILS) -> dict:
    return copy.deepcopy(base)


def _session():
    from revl.mcp.session import Session
    return Session()


# ---------------------------------------------------------------------------
# The session boundary (246): an aborted session whose compensation FAILED
# surfaces `unresolved` residue in the abort report, not silently swallowed.
# ---------------------------------------------------------------------------

@needs_cordis
def test_aborted_session_surfaces_unresolved_compensation_residue():
    session = _session()
    session.load(_ir())
    session.call("ops", "run", ["go"])

    report = session.abort()

    # the residue is a first-class field of the session-boundary report.
    residue = report.get("compensationResidue")
    assert residue, "the abort report did not surface the compensation residue"
    assert any(r["outcome"] == "failed" for r in residue)
    assert any(r.get("state") == "unresolved" for r in residue)

    # and it counts as a residue prompt (item 246, the same channel a
    # flush-residue uses), so prompts-per-session reflects it.
    assert report["prompts"]["residue"] >= 1
