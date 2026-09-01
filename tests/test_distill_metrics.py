"""The approval-distillation TIME AXIS — roadmap item 251, Slice 3.

Slices 1+2 landed the pure distiller and the resource-scoped recording /
enforcement / operator surface. Slice 3 adds the persisted time axis to
`Session.approval_metrics`: the prompts-per-session series and the
`distillationImpact` (before / after per applied rule) plus the irreducible
floor, all folded from the WAL (which outlives a session) rather than the
in-memory single-session ledger. These tests pin:

  * the prompts-per-session series is READ FROM THE WAL and has a stable,
    deterministic time ordering (first-appearance order over the append log);
  * `distillationImpact` reports the before / after prompt counts per applied
    rule, computed from the WAL's `distillation-applied` records and the prompt
    events with the runtime coverage predicate, plus the computed floor;
  * a session with an applied rule shows the prompt reduction the rule achieved;
  * a denial counts as a prompt in the series (a human decision either way);
  * off-policy the metric block returns None (byte-identical, as today).

Pure over synthetic WAL fixtures: no cordis runtime is needed for the time-axis
fold, so this suite runs anywhere the distiller does.
"""

import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import replay  # noqa: E402

from revl.mcp.session import Session  # noqa: E402

RULE = "component billing:* may auto-approve kv.get"


def _granted(session, cap="kv.get", component="billing:invoice"):
    return {"record": "approval-granted", "component": component,
            "session": session, "operator": "op1", "realm": "billing",
            "classCCapabilities": [cap]}


def _write_wal(path, records):
    """Write `records` into a real approval WAL file, in order, as the runtime
    would, so the metric folds genuine on-disk ledger bytes (not a mock)."""
    wal = replay.WriteAheadLog(str(path)).open()
    try:
        for rec in records:
            kind = rec["record"]
            entry = {k: v for k, v in rec.items() if k != "record"}
            if kind == "approval-granted":
                wal.record_approval_granted(entry)
            elif kind == "distillation-applied":
                wal.record_distillation_applied(entry)
            elif kind == "distillation-revoked":
                wal.record_distillation_revoked(entry)
            else:
                wal._write(dict(rec))
    finally:
        wal.close()
    return str(path)


def _session_at(path):
    """A Session whose approval WAL points at `path`, with a policy configured so
    `approval_metrics` returns the metric block (off-policy it returns None)."""
    s = Session()
    s.approval_policy = "auto"
    s.recorder = types.SimpleNamespace(wal=types.SimpleNamespace(path=path))
    return s


def _multi_session_ledger():
    """Two sessions of three matching prompts each, then the rule is applied, then
    a third session raises one prompt of a DIFFERENT (irreducible) shape."""
    return (
        [_granted("s0") for _ in range(3)]
        + [_granted("s1") for _ in range(3)]
        + [{"record": "distillation-applied", "rule": RULE,
            "reviewedBy": "op1", "distilledBy": "op1", "appliedAt": 1000}]
        + [_granted("s2", cap="fs.read")]
    )


# ---------------------------------------------------------------------------
# the prompts-per-session series is read from the WAL, deterministically
# ---------------------------------------------------------------------------

def test_series_is_read_from_the_wal_and_deterministic(tmp_path):
    path = _write_wal(tmp_path / "a.wal", _multi_session_ledger())
    session = _session_at(path)

    metrics = session.approval_metrics()
    series = metrics["promptsPerSessionSeries"]
    # ordered by first appearance in the append log, each session's prompt count.
    assert series == [
        {"session": "s0", "prompts": 3},
        {"session": "s1", "prompts": 3},
        {"session": "s2", "prompts": 1},
    ]
    # deterministic: a second fold of the same WAL is byte-for-byte identical.
    assert _session_at(path).approval_metrics()["promptsPerSessionSeries"] \
        == series


def test_series_reflects_the_wal_not_the_in_memory_session(tmp_path):
    """The series is the CROSS-session ledger on disk, not this live session's
    `_approval_records` (which would only ever show one session)."""
    path = _write_wal(tmp_path / "b.wal", _multi_session_ledger())
    session = _session_at(path)
    sessions = {e["session"] for e in
                session.approval_metrics()["promptsPerSessionSeries"]}
    assert sessions == {"s0", "s1", "s2"}
    assert session._session_id not in sessions   # the live id never granted


def test_a_denial_counts_as_a_prompt_in_the_series(tmp_path):
    ledger = [_granted("s0"),
              {"record": "approval-denied", "component": "billing:invoice",
               "session": "s0", "operator": "op1", "realm": "billing",
               "classCCapabilities": ["kv.get"]}]
    path = _write_wal(tmp_path / "c.wal", ledger)
    series = _session_at(path).approval_metrics()["promptsPerSessionSeries"]
    assert series == [{"session": "s0", "prompts": 2}]  # the yes and the no


# ---------------------------------------------------------------------------
# distillationImpact: before / after per applied rule, and the floor
# ---------------------------------------------------------------------------

def test_impact_reports_before_after_and_reduction_per_applied_rule(tmp_path):
    path = _write_wal(tmp_path / "d.wal", _multi_session_ledger())
    impact = _session_at(path).approval_metrics()["distillationImpact"]

    assert len(impact["perRule"]) == 1
    entry = impact["perRule"][0]
    assert entry["rule"] == RULE
    # six matching prompts fell BEFORE the apply, none after (the rule now
    # auto-approves them, so they write no prompt) — the reduction it achieved.
    assert entry["before"] == 6
    assert entry["after"] == 0
    assert entry["reduced"] == 6
    assert entry["appliedAt"] == 1000


def test_impact_after_count_excludes_prompts_the_rule_would_not_cover(tmp_path):
    """A prompt after the apply that the rule does NOT cover (a different shape)
    is not attributed to the rule — `after` counts only matching prompts, so the
    reduction stays honest."""
    ledger = (
        [_granted("s0") for _ in range(3)]
        + [_granted("s1") for _ in range(3)]
        + [{"record": "distillation-applied", "rule": RULE,
            "reviewedBy": "op1", "distilledBy": "op1", "appliedAt": 500}]
        + [_granted("s2", cap="fs.read")]     # a different shape, after apply
    )
    path = _write_wal(tmp_path / "e.wal", ledger)
    entry = _session_at(path).approval_metrics()["distillationImpact"]["perRule"][0]
    assert entry["before"] == 6 and entry["after"] == 0


def test_floor_is_the_count_of_shape_keys_that_cannot_distill(tmp_path):
    path = _write_wal(tmp_path / "f.wal", _multi_session_ledger())
    impact = _session_at(path).approval_metrics()["distillationImpact"]
    # kv.get settles (6 grants / 2 sessions / one operator) so it is NOT the
    # floor; the lone fs.read shape is seen once — below threshold, irreducible.
    assert impact["floor"] == 1


def test_session_with_an_applied_rule_shows_the_reduction(tmp_path):
    """The design headline as an assertion: an applied rule's session shows the
    prompt reduction the rule achieved (before > after)."""
    path = _write_wal(tmp_path / "g.wal", _multi_session_ledger())
    entry = _session_at(path).approval_metrics()["distillationImpact"]["perRule"][0]
    assert entry["before"] > entry["after"]
    assert entry["reduced"] == entry["before"] - entry["after"]


# ---------------------------------------------------------------------------
# off-policy byte-identity and graceful degradation
# ---------------------------------------------------------------------------

def test_off_policy_metric_block_is_none(tmp_path):
    """With no policy configured the metric block returns None, exactly as before
    Slice 3 — the time-axis fields never leak off-policy (byte-identical)."""
    session = Session()
    session.approval_policy = None
    assert session.approval_metrics() is None


def test_on_policy_with_no_wal_degrades_to_empty_series():
    """A policy configured but no WAL open yields an empty series and a zero
    floor rather than crashing state()."""
    session = Session()
    session.approval_policy = "auto"
    metrics = session.approval_metrics()
    assert metrics["promptsPerSessionSeries"] == []
    assert metrics["distillationImpact"] == {"floor": 0, "perRule": []}
