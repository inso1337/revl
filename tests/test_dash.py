"""`revl dash` — the supervisor's cockpit (src/revl/dash.py, docs/dash.md).

The dash is a READ-ONLY live view over a session or a recorded run. These tests
pin the three panes and the one contract:

  * the dependency graph renders a composition's components, realms and service
    seams from the compiled IR (`query.Composition`);
  * a recorded item-27 lifecycle trace renders the causal stream, and a live
    state colors the graph as it stands now (served vs drifted, fiber states);
  * the pending-decisions queue shows a boundary-widening addition (item 21)
    and a policy exception (item 33), each WITH its evidence attached;
  * and building the model mutates nothing it is handed — the read-only
    guarantee the ack/policy/lease/interrupt features assume.

The mesh fixture is the query chain Journal -> Store -> Api -> Dashboard; the
tenants fixture is two components isolated into different realms that both
reach one boundary.
"""

import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from revl.compiler import compile_files, compile_source  # noqa: E402
from revl import dash, why_runtime  # noqa: E402
from revl.audit_diff import audit_report  # noqa: E402
from revl.policy import parse_policy  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MESH = os.path.join(ROOT, "tests", "fixtures", "query_mesh.rvl")
TENANTS = os.path.join(ROOT, "tests", "fixtures", "policy_tenants.rvl")


@pytest.fixture(scope="module")
def mesh():
    return compile_files([MESH])


@pytest.fixture(scope="module")
def tenants():
    return compile_files([TENANTS])


# a widening pair — a second component adds one emission crossing (item 21)
BASE = """
service Database { emission fn execute(sql: Str) -> Int }
service Cache { emission[db] fn put(key: Str, value: Str) }

component PgCache requires db: Database provides cache: Cache {
  provide cache {
    fn put(key, value) { emit db.execute(`INSERT ${key} ${value}`) }
  }
}
"""

WIDER = BASE + """
component Front requires cache: Cache {
  emit cache.put("k", "v")
}
"""


# ------------------------------------------------------------------- graph


def test_graph_renders_components_and_realms(mesh):
    model = dash.build_model(mesh)
    assert model["ok"] and model["readOnly"] is True
    assert model["mode"] == dash.MODE_STATIC
    names = {c["name"] for c in model["graph"]["components"]}
    assert names == {"Journal", "Store", "Api", "Dashboard", "Announcer"}
    # load order preserved from the manifest
    assert model["graph"]["loadOrder"][0] == "Journal"


def test_graph_draws_service_seams(mesh):
    model = dash.build_model(mesh)
    seams = {(s["from"], s["to"], s["key"], s["method"])
             for s in model["graph"]["seams"]}
    # the chain: Dashboard -> Api -> Store -> Journal, each an inter-component
    # service call landing in the provider's method
    assert ("Dashboard", "Api", "rep", "publish") in seams
    assert ("Api", "Store", "kv", "set") in seams
    assert ("Store", "Journal", "ledger", "append") in seams


def test_graph_colors_realms(tenants):
    model = dash.build_model(tenants)
    realms = model["graph"]["realms"]
    assert set(realms) == {"tenantA", "tenantB"}
    assert realms["tenantA"] == ["TenantAJob"]
    assert realms["tenantB"] == ["TenantBJob"]
    # each component carries its non-shared realm tag
    by_name = {c["name"]: c for c in model["graph"]["components"]}
    assert by_name["TenantAJob"]["realms"] == ["tenantA"]


def test_render_is_text_and_labels_read_only(mesh):
    text = dash.render_model(dash.build_model(mesh))
    assert "DEPENDENCY GRAPH" in text
    assert "read-only" in text
    assert "seams" in text
    assert "Dashboard --rep.publish--> Api" in text


# ------------------------------------------------------------------- trace


def _trace_events():
    return [
        why_runtime.make_event(0, 1, why_runtime.LOAD, "Journal",
                               "LOADING -> ACTIVE", why_runtime.cause_boot()),
        why_runtime.make_event(
            1, 1, why_runtime.LOAD, "Store", "LOADING -> ACTIVE",
            why_runtime.cause_requirements([{"component": "Journal",
                                             "key": "ledger"}])),
        why_runtime.make_event(
            2, 1, why_runtime.WITHDRAW, "Journal", "ACTIVE -> DISPOSED",
            why_runtime.cause_trigger("operator withdrew it")),
        why_runtime.make_event(
            3, 1, why_runtime.WITHDRAW, "Store", "ACTIVE -> PENDING",
            why_runtime.cause_provider_withdrawn("Journal", "ledger")),
    ]


def test_recorded_trace_renders_causal_stream(mesh):
    model = dash.build_model(mesh, trace=_trace_events())
    assert model["mode"] == dash.MODE_RECORDED
    events = model["trace"]["events"]
    assert [e["seq"] for e in events] == [0, 1, 2, 3]
    # the cause behind each transition streams with it (item 27)
    withdrew_store = next(e for e in events if e["component"] == "Store"
                          and e["event"] == "withdraw")
    assert "provided by Journal" in withdrew_store["note"]
    text = dash.render_model(model)
    assert "CAUSAL TRACE" in text
    assert "Store ACTIVE -> PENDING" in text


def test_trace_accepts_jsonl_text(mesh):
    jsonl = "\n".join(json.dumps(e) for e in _trace_events())
    model = dash.build_model(mesh, trace=jsonl)
    assert model["trace"]["eventCount"] == 4


def test_recorded_timeline_renders_effects(mesh):
    timeline = {"components": [{"component": "Store", "steps": [
        {"index": 0, "kind": "effect", "label": "cells.insert"},
        {"index": 1, "kind": "emission", "label": "ledger.append",
         "detail": {"key": "ledger", "method": "append"},
         "compensatedBy": None},
    ]}]}
    model = dash.build_model(mesh, timeline=timeline)
    steps = model["trace"]["timeline"]
    assert [s["kind"] for s in steps] == ["effect", "emission"]
    text = dash.render_model(model)
    assert "effect timeline" in text


# -------------------------------------------------------------------- live


def test_live_state_colors_served_and_drifted(mesh):
    live = {"generation": 4, "servedKeys": ["ledger", "kv"],
            "componentStates": {"Journal": "ACTIVE", "Store": "ACTIVE",
                                "Api": "FAILED"}}
    model = dash.build_model(mesh, live_state=live)
    assert model["mode"] == dash.MODE_LIVE
    assert model["generation"] == 4
    by_name = {c["name"]: c for c in model["graph"]["components"]}
    # a served provision vs one whose key is not in servedKeys (drifted)
    journal_ledger = by_name["Journal"]["provides"][0]
    assert journal_ledger["key"] == "ledger" and journal_ledger["servedNow"] is True
    api_rep = by_name["Api"]["provides"][0]
    assert api_rep["servedNow"] is False
    assert by_name["Api"]["state"] == "FAILED"
    assert "rep" in model["graph"]["driftedProvisions"]


# --------------------------------------------------------------- decisions


def test_widening_queue_shows_addition_with_evidence():
    prev = audit_report(compile_source(BASE))
    model = dash.build_model(compile_source(WIDER), prev_audit=prev)
    widening = model["decisions"]["widening"]
    tokens = {w["token"] for w in widening}
    assert "emit:Front:cache.put" in tokens
    row = next(w for w in widening if w["token"] == "emit:Front:cache.put")
    assert row["acknowledged"] is False
    # the evidence decodes the crossing to the component and reach it names
    assert row["evidence"]["component"] == "Front"
    assert row["evidence"]["reach"] == "emission"
    assert model["decisions"]["pending"] == 1


def test_widening_ack_clears_from_pending():
    prev = audit_report(compile_source(BASE))
    model = dash.build_model(compile_source(WIDER), prev_audit=prev,
                             accepted={"emit:Front:cache.put"})
    row = next(w for w in model["decisions"]["widening"]
               if w["token"] == "emit:Front:cache.put")
    assert row["acknowledged"] is True
    assert model["decisions"]["pending"] == 0


def test_policy_exception_queue_carries_why_trace(tenants):
    policy = parse_policy("tenants never reach each other")
    model = dash.build_model(tenants, policy=policy)
    exceptions = model["decisions"]["policy"]
    assert exceptions, "the tenants rule should refuse admission"
    row = exceptions[0]
    assert row["violation"] == "tenant"
    # the why-trace is the approval surface — it names the offending chain
    assert "isolation is not real" in row["message"]
    assert row["evidence"]["trace"] is not None
    assert "bus" in row["evidence"]["trace"]
    assert model["decisions"]["pending"] >= 1
    text = dash.render_model(model)
    assert "PENDING DECISIONS" in text
    assert "policy exceptions" in text


def test_clean_composition_has_empty_queue(mesh):
    model = dash.build_model(mesh)
    assert model["decisions"]["pending"] == 0
    assert dash.render_model(model).rstrip().endswith(
        "no policy exception to rule on.")


# --------------------------------------------------------------- read-only


def test_build_model_mutates_no_input(mesh):
    """The read-only contract: nothing the dash is handed is written back."""
    ir = copy.deepcopy(mesh)
    ir_before = json.dumps(ir, sort_keys=True)

    live = {"generation": 2, "servedKeys": ["ledger"],
            "componentStates": {"Journal": "ACTIVE"}}
    live_before = json.dumps(live, sort_keys=True)
    events = _trace_events()
    events_before = json.dumps(events, sort_keys=True)
    prev = audit_report(compile_source(BASE))
    prev_before = json.dumps(prev, sort_keys=True)

    dash.build_model(ir, live_state=live, trace=events, prev_audit=prev,
                     accepted={"emit:Front:cache.put"})

    assert json.dumps(ir, sort_keys=True) == ir_before
    assert json.dumps(live, sort_keys=True) == live_before
    assert json.dumps(events, sort_keys=True) == events_before
    assert json.dumps(prev, sort_keys=True) == prev_before


class _RecordingSession:
    """A stand-in session that fails the test if the dash calls any mutator.

    Only `ir` and `live_state()` are legal reads; every other attribute access
    (swap, rollback, dispose, apply, ...) raises."""

    _READS = {"ir", "live_state"}

    def __init__(self, ir, live_state):
        object.__setattr__(self, "_ir", ir)
        object.__setattr__(self, "_ls", live_state)

    @property
    def ir(self):
        return self._ir

    def live_state(self):
        return dict(self._ls)

    def __getattr__(self, name):  # any non-read access is a mutation attempt
        raise AssertionError(
            f"dash touched session.{name} — it must be strictly read-only")


def test_dashboard_from_session_is_read_only(mesh):
    live = {"generation": 1, "servedKeys": ["ledger", "kv"],
            "componentStates": {"Journal": "ACTIVE"}}
    session = _RecordingSession(mesh, live)
    board = dash.Dashboard.from_session(session)
    model = board.snapshot()  # must not call any mutator
    assert model["mode"] == dash.MODE_LIVE
    assert model["generation"] == 1
    # a second snapshot re-reads live state, still read-only
    assert board.snapshot()["ok"] is True
    assert "revl dash" in board.render()


# ------------------------------------------------------------------- CLI


def test_cli_dash_smoke(capsys):
    from revl.__main__ import main
    rc = main(["dash", MESH])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DEPENDENCY GRAPH" in out
    assert "read-only" in out


def test_cli_dash_json(capsys):
    from revl.__main__ import main
    rc = main(["dash", MESH, "--json"])
    assert rc == 0
    model = json.loads(capsys.readouterr().out)
    assert model["readOnly"] is True
    assert model["graph"]["seams"]
