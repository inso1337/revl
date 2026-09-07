"""Roadmap item 477 follow-up 2 / issue #624 — restart RECONCILIATION of
declared liveness from durable world state (`reconcileLivenessFromWorld`).

On restart the in-memory expected-liveness map is gone; it described the world
the crashed process believed in. `revl.reconcile.reconcile_liveness_from_world`
rebuilds only the EXPECTED liveness of the restarting composition's
declared-ceiling providers from durable evidence — the WAL, the E-Stop latch,
and a durable causal trace — and reports each conservatively:

  * `expired`    — a durable trace attests a LIVENESS_EXPIRED withdrawal;
  * `halted`     — an armed E-Stop latch covers the world;
  * `unresolved` — everything else (valid/stale/partial/contradictory), which
    MUST be re-observed and is never silently re-adopted as freshly live.

Crash/restart is exercised with valid (activation-complete), stale-belief,
partial (crash mid-activation) and unreadable durable state; a foreign owner
named in residue but not managed here is left untouched; a version-unsupported
WAL fails closed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl import why_runtime as wr  # noqa: E402
from revl.reconcile import reconcile_liveness_from_world  # noqa: E402
from revl.wal import WAL_VERSION  # noqa: E402


HANGABLE = """
service Database { fn query(sql: Str) -> List[Row] }
component PgDatabase provides db: Database liveness 1s {
  config { url: Str = "postgres://localhost/app" }
  let pool = effect Pool.open(config.url, 4) undo pool.close()
  provide db { fn query(sql) = pool.query(sql) }
}
component UserCache requires db: Database provides cache: Cache {
  provide cache { fn get(k) = db.query(k) }
}
service Cache { fn get(k: Str) -> List[Row] }
"""

NO_CEILING = """
service S { fn ping() -> Int }
component P provides s: S { provide s { fn ping() = 1 } }
"""


def _write_wal(tmp_path, *, complete: bool, version=WAL_VERSION,
               extra_records=(), name="run.wal") -> str:
    path = tmp_path / name
    lines = [{"record": "header", "walVersion": version, "generation": 7,
              "guarantee": "x"}]
    lines.extend(extra_records)
    if complete:
        lines.append({"record": "activation-complete", "components": ["PgDatabase"]})
    with open(path, "w", encoding="utf-8") as handle:
        for row in lines:
            handle.write(json.dumps(row) + "\n")
    return str(path)


def _ir():
    return compile_source(HANGABLE, "h.rvl")


# ---------------------------------------------------------------------------
# the managed set: only declared-ceiling providers are ever spoken about
# ---------------------------------------------------------------------------


def test_a_composition_with_no_declared_ceiling_has_nothing_to_reconcile():
    report = reconcile_liveness_from_world(compile_source(NO_CEILING, "p.rvl"))
    assert report["verdict"] == "clean"
    assert report["components"] == []
    assert report["unresolved"] == []


def test_only_ceiling_bearing_providers_appear():
    report = reconcile_liveness_from_world(_ir())  # no evidence at all
    names = [c["component"] for c in report["components"]]
    assert names == ["PgDatabase"]           # UserCache declares no ceiling
    assert report["unresolved"] == ["PgDatabase"]


# ---------------------------------------------------------------------------
# crash/restart with valid, stale-belief and partial durable state
# ---------------------------------------------------------------------------


def test_no_durable_state_leaves_every_provider_unresolved():
    # an absent WAL is a valid nothing-durable input: we know nothing, so the
    # provider must be re-observed — never assumed live.
    report = reconcile_liveness_from_world(_ir(), wal_path="/no/such/file.wal")
    assert report["verdict"] == "unresolved"
    row = report["components"][0]
    assert row["status"] == "unresolved"
    assert "no durable evidence" in row["reason"]


def test_activation_complete_is_a_stale_belief_not_a_re_adoption(tmp_path):
    # the crashed process recorded activation-complete: it BELIEVED the generation
    # live. That belief is stale across the crash — unresolved, never re-adopted.
    wal = _write_wal(tmp_path, complete=True)
    report = reconcile_liveness_from_world(_ir(), wal_path=wal)
    assert report["verdict"] == "unresolved"
    assert report["generation"] == 7
    row = report["components"][0]
    assert row["status"] == "unresolved"
    assert "stale across the crash" in row["reason"]
    # crucially there is no `live` verdict anywhere.
    assert all(c["status"] != "live" for c in report["components"])


def test_crash_before_activation_complete_is_unresolved_partial_state(tmp_path):
    wal = _write_wal(tmp_path, complete=False)
    report = reconcile_liveness_from_world(_ir(), wal_path=wal)
    row = report["components"][0]
    assert row["status"] == "unresolved"
    assert "no activation-complete marker" in row["reason"]


# ---------------------------------------------------------------------------
# the two honest NEGATIVE states: expired (trace) and halted (E-Stop)
# ---------------------------------------------------------------------------


def test_a_durable_trace_expiry_is_reported_expired(tmp_path):
    # a prior process's durable causal trace attests the provider was withdrawn
    # with a LIVENESS_EXPIRED root — dead by expiry, attested not guessed.
    events = [
        wr.make_event(0, 1, wr.WITHDRAW, "PgDatabase", "ACTIVE -> DISPOSED",
                      wr.cause_liveness_expired(ceiling_ms=1000, silent_ms=1500)),
    ]
    trace_path = tmp_path / "run.jsonl"
    wr.write_trace(events, str(trace_path))
    report = reconcile_liveness_from_world(
        _ir(), wal_path=_write_wal(tmp_path, complete=True),
        trace_path=str(trace_path))
    row = report["components"][0]
    assert row["status"] == "expired"
    assert row["silentMs"] == 1500
    assert report["expired"] == ["PgDatabase"]
    assert report["verdict"] == "clean"       # nothing left unresolved


def test_an_armed_estop_latch_reports_halted(tmp_path):
    latch = tmp_path / "run.wal.estop"
    latch.write_text(json.dumps({"halted": True, "operator": "op"}),
                     encoding="utf-8")
    report = reconcile_liveness_from_world(
        _ir(), wal_path=_write_wal(tmp_path, complete=True),
        latch_path=str(latch))
    row = report["components"][0]
    assert row["status"] == "halted"
    assert report["halted"] == ["PgDatabase"]
    assert "durably NOT live" in row["reason"]


# ---------------------------------------------------------------------------
# refusal: a WAL this reader cannot trust fails closed
# ---------------------------------------------------------------------------


def test_a_version_unsupported_wal_fails_closed_to_unresolved(tmp_path):
    wal = _write_wal(tmp_path, complete=True, version=WAL_VERSION + 99)
    report = reconcile_liveness_from_world(_ir(), wal_path=wal)
    assert report["verdict"] == "refused"
    row = report["components"][0]
    assert row["status"] == "unresolved"
    assert "integrity gate" in row["reason"]


# ---------------------------------------------------------------------------
# no cleanup / re-adoption of an owner we do not manage
# ---------------------------------------------------------------------------


def test_a_foreign_owner_in_residue_is_reported_but_left_untouched(tmp_path):
    residue = {"record": "steady-state-residue", "component": "SomeOtherService",
               "crossing": {"component": "SomeOtherService"}}
    wal = _write_wal(tmp_path, complete=True, extra_records=[residue])
    report = reconcile_liveness_from_world(_ir(), wal_path=wal)
    assert report["foreign"] == ["SomeOtherService"]
    # it is never adopted or expired — it is not among the managed components.
    assert all(c["component"] != "SomeOtherService" for c in report["components"])
    assert "SomeOtherService" not in report["expired"]


def test_a_foreign_trace_expiry_never_adopts_an_unmanaged_owner(tmp_path):
    events = [
        wr.make_event(0, 1, wr.WITHDRAW, "Stranger", "ACTIVE -> DISPOSED",
                      wr.cause_liveness_expired(ceiling_ms=1000, silent_ms=1500)),
    ]
    trace_path = tmp_path / "t.jsonl"
    wr.write_trace(events, str(trace_path))
    report = reconcile_liveness_from_world(
        _ir(), wal_path=_write_wal(tmp_path, complete=True),
        trace_path=str(trace_path))
    # PgDatabase is the only managed provider; Stranger's expiry is not adopted.
    assert [c["component"] for c in report["components"]] == ["PgDatabase"]
    assert report["expired"] == []
    assert report["components"][0]["status"] == "unresolved"
