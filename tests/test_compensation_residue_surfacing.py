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
import sys
from pathlib import Path

import pytest

from revl import query
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

    # the residue NAMES the crossing it was offsetting (design Decision 2).
    r = residue[0]
    assert r["component"] == "Agent"
    assert r["method"] == "offset_fails"


@needs_cordis
def test_clean_compensated_session_reports_no_residue():
    """A session that commits cleanly discharges the offset (never runs it) and
    surfaces an EMPTY residue — the `compensated` state, no `unresolved`."""
    session = _session()
    session.load(_ir(compile_source(_SOURCE_CLEAN, "residue_surfacing.rvl")))
    session.call("ops", "run", ["go"])

    report = session.commit()             # enumerate
    confirm = session.commit_confirm(report["hash"])

    assert confirm["committed"] is True
    assert confirm["compensationResidue"] == []
    assert confirm["prompts"]["residue"] == 0


# ---------------------------------------------------------------------------
# The audit surface (revl.query) — the three states, PURE (no cordis needed).
# ---------------------------------------------------------------------------

def test_query_classify_compensation_three_states():
    crossings = [
        {"component": "A", "compensated": False},   # bare
        {"component": "A", "compensated": True},    # compensated (offset landed)
        {"component": "B", "compensated": True},    # compensated -> unresolved
    ]
    residue = [{"kind": "compensation-residue", "state": "unresolved",
                "component": "B", "method": "offset_fails", "seq": 3,
                "outcome": "failed",
                "error": {"type": "RuntimeError", "message": "offset boom"}}]

    out = query.classify_compensation(crossings, residue)

    assert out["states"] == {"bare": 1, "compensated": 1, "unresolved": 1}
    assert out["bare"][0]["residueState"] == "bare"
    assert out["compensated"][0]["component"] == "A"
    assert out["compensated"][0]["residueState"] == "compensated"
    [un] = out["unresolved"]
    assert un["component"] == "B"
    assert un["residueState"] == "unresolved"
    assert un["residue"]["error"]["message"] == "offset boom"


def test_query_classify_compensation_byte_inert_without_residue():
    """No residue -> every attached offset stays `compensated`, `unresolved`
    empty. The pre-247 two-state split, unchanged."""
    crossings = [{"component": "A", "compensated": True},
                 {"component": "A", "compensated": False}]
    out = query.classify_compensation(crossings, residue=None)
    assert out["states"] == {"bare": 1, "compensated": 1, "unresolved": 0}
    assert out["unresolved"] == []


def test_query_compensation_audit_surfaces_standalone_residue():
    """With only residue and no recording, the unresolved fact is still fully
    enumerated — never silently swallowed."""
    residue = [{"kind": "compensation-residue", "state": "unresolved",
                "component": "Agent", "method": "offset_fails", "seq": 1,
                "outcome": "failed",
                "error": {"type": "RuntimeError", "message": "offset boom"}}]
    out = query.compensation_audit(timeline=None, residue=residue)
    assert out["ok"] is True
    assert out["states"]["unresolved"] == 1
    assert out["unresolved"][0]["residue"]["method"] == "offset_fails"


# ---------------------------------------------------------------------------
# The erase-report surface — the third state threads through, PURE.
# ---------------------------------------------------------------------------

_REALM_SOURCE = (
    "extern emission fn note(msg: Str) -> Unit = @py { return }\n"
    "extern pure fn offset(msg: Str) -> Unit = @py { return }\n"
    "service Ops { emission fn run(msg: Str) }\n"
    "component Agent provides ops: Ops {\n"
    "  isolate ops in realm(\"wa\")\n"
    "  provide ops {\n"
    "    fn run(msg) { emit note(msg) compensate offset(msg) }\n"
    "  }\n"
    "}\n"
)


def test_erase_report_threads_unresolved_state():
    from revl import erase_report
    ir = compile_source(_REALM_SOURCE, "erase_residue.rvl")
    realms = erase_report.realms_of(ir)
    realm = next(r for r in realms if r)  # the isolated realm, not shared

    residue = [{"kind": "compensation-residue", "state": "unresolved",
                "component": "Agent", "method": "offset", "seq": 1,
                "outcome": "failed",
                "error": {"type": "RuntimeError", "message": "offset boom"}}]

    # without residue: the static two-state surface, no unresolved.
    plain = erase_report.build_report(ir, realm, prove_residue=False)
    assert plain["boundaryCrossings"]["unresolvedCount"] == 0
    assert plain["summary"]["unresolvedCrossings"] == 0

    # with residue: the compensated crossing lifts into `unresolved`.
    withres = erase_report.build_report(
        ir, realm, prove_residue=False, compensation_residue=residue)
    assert withres["boundaryCrossings"]["unresolvedCount"] == 1
    assert withres["summary"]["unresolvedCrossings"] == 1
    text = erase_report.render(withres)
    assert "UNRESOLVED" in text
