"""Composition queries (src/revl/query.py, docs/queries.md).

The fixture chain is Journal -> Store -> Api -> Dashboard, so a withdrawal
cascades two levels past its first dependent and an emission at the far end
is reached from the near end only transitively. Announcer reaches the same
extern through the pure stratum instead.
"""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from revl.compiler import compile_files, compile_source  # noqa: E402
from revl.mcp import server  # noqa: E402
from revl import query  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MESH = os.path.join(ROOT, "tests", "fixtures", "query_mesh.rvl")
TENANTS = os.path.join(ROOT, "examples", "tenants.rvl")


@pytest.fixture(scope="module")
def mesh():
    return compile_files([MESH])


@pytest.fixture(scope="module")
def tenants():
    return compile_files([TENANTS])


def _site_ids(result):
    return {(site["id"], site["distance"]) for site in result["sites"]}


# ------------------------------------------------------- emits-to


def test_emitters_direct_service_emission(mesh):
    result = query.emitters(mesh, "ledger.append")
    assert result["ok"]
    assert ("Store:kv.set", 0) in _site_ids(result)
    # the provider's own body does not "emit to" its own key
    assert "Journal" not in result["components"]


def test_emitters_transitive_through_the_service_seam(mesh):
    """The point of the query: Dashboard never names `host_write`, but a
    call it makes lands three hops away in a body that does."""
    result = query.emitters(mesh, "host_write")
    by_id = {site["id"]: site for site in result["sites"]}

    assert by_id["Journal:ledger.append"]["direct"] is True
    assert by_id["Store:kv.set"]["path"] == ["ledger.append"]
    assert by_id["Api:rep.publish"]["path"] == ["kv.set", "ledger.append"]
    assert by_id["Dashboard:activation"]["path"] == [
        "rep.publish", "kv.set", "ledger.append"]
    assert by_id["Dashboard:activation"]["distance"] == 3
    assert result["components"] == ["Announcer", "Api", "Dashboard", "Journal", "Store"]


def test_emitters_transitive_through_pure_fns(mesh):
    """`say` calls `shout`, which calls `render`, which is pure — but
    `shout` also reaches `host_write`. The fn call graph is walked by the
    checker's own analysis, so the query cannot disagree with the gate."""
    result = query.emitters(mesh, "host_write")
    site = next(s for s in result["sites"] if s["id"] == "Announcer:voice.say")
    assert site["direct"] is True
    assert site["reaches"]["through"] == ["shout"]
    assert site["reaches"]["class"] == "emission"


def test_emitters_by_service_name_and_by_key(mesh):
    by_service = query.emitters(mesh, "Ledger")
    by_key = query.emitters(mesh, "ledger")
    assert _site_ids(by_service) == _site_ids(by_key)
    assert by_service["resolved"][0]["kind"] == "service"
    assert by_key["resolved"][0]["kind"] == "key"


def test_emitters_reports_compensation(mesh):
    result = query.emitters(mesh, "rep.publish")
    site = next(s for s in result["sites"] if s["id"] == "Dashboard:activation")
    assert site["reaches"]["compensated"] is True


def test_emitters_is_declared_conservative(mesh):
    result = query.emitters(mesh, "host_write")
    assert result["precision"] == query.APPROX
    assert any("may-analysis" in a for a in result["assumptions"])
    assert any("opaque" in a for a in result["assumptions"])


def test_emitters_unknown_target_lists_what_exists(mesh):
    result = query.emitters(mesh, "nope")
    assert result["ok"] is False
    assert "host_write" in result["known"] and "ledger" in result["known"]


def test_non_emission_calls_are_not_emitters(mesh):
    """`kv.get` and `rep.status` are plain `fn`s; G4 guarantees they reach
    no emission, so nothing may be attributed to them."""
    assert query.emitters(mesh, "kv.get")["sites"] == []
    assert query.emitters(mesh, "rep.status")["sites"] == []


# ------------------------------------------------------- withdraw


def test_withdrawal_cascades_two_levels_past_the_first_dependent(mesh):
    result = query.withdrawal(mesh, "Journal")
    assert result["precision"] == query.EXACT
    assert [(c["component"], c["depth"]) for c in result["cascade"]] == [
        ("Store", 1), ("Api", 2), ("Dashboard", 3)]
    assert result["cascade"][1]["lostKeys"] == ["kv"]
    assert result["cascade"][1]["provider"] == "Store"
    assert result["breaks"] == 3


def test_withdrawal_order_is_lifo(mesh):
    result = query.withdrawal(mesh, "Journal")
    assert result["withdrawalOrder"] == ["Dashboard", "Api", "Store", "Journal"]
    assert result["survivors"] == ["Announcer"]


def test_withdrawal_reports_every_orphaned_key(mesh):
    result = query.withdrawal(mesh, "Journal")
    assert [(o["key"], o["wasProvidedBy"]) for o in result["orphanedKeys"]] == [
        ("kv", "Store"), ("ledger", "Journal"), ("rep", "Api")]


def test_withdrawing_a_leaf_breaks_nothing(mesh):
    result = query.withdrawal(mesh, "Dashboard")
    assert result["cascade"] == []
    assert result["withdrawalOrder"] == ["Dashboard"]
    assert "Journal" in result["survivors"]


def test_withdrawal_respects_realms(tenants):
    """Two providers, one key, two realms: withdrawing one tenant's store
    must not touch the other tenant's app (G2 is per-(key, realm))."""
    result = query.withdrawal(tenants, "TenantAStore")
    assert [c["component"] for c in result["cascade"]] == ["TenantAApp"]
    assert set(result["survivors"]) == {"TenantBStore", "TenantBApp"}
    assert result["provides"] == [{"key": "kv", "realm": "tenant_a"}]


def test_withdrawal_unknown_component(mesh):
    result = query.withdrawal(mesh, "Ghost")
    assert result["ok"] is False
    assert "Journal" in result["known"]


# ------------------------------------------------------- depends-on


def test_dependents_of_a_key(mesh):
    result = query.dependents(mesh, "kv")
    assert result["precision"] == query.EXACT
    entry = result["keys"][0]
    assert entry["provider"] == "Store" and entry["service"] == "Kv"
    assert [c["component"] for c in entry["consumers"]] == ["Api"]
    assert entry["consumers"][0]["methodsCalled"] == [
        {"method": "set", "emission": True}]


def test_dependents_of_a_service_covers_every_realm(tenants):
    result = query.dependents(tenants, "Kv")
    assert {(k["key"], k["realm"], k["provider"]) for k in result["keys"]} == {
        ("kv", "tenant_a", "TenantAStore"), ("kv", "tenant_b", "TenantBStore")}
    assert result["components"] == ["TenantAApp", "TenantBApp"]


def test_dependents_carries_intercept_metadata(tenants):
    result = query.dependents(tenants, "kv")
    consumer = next(c for k in result["keys"] for c in k["consumers"]
                    if c["component"] == "TenantAApp")
    assert consumer["intercept"] == {"quota": 5, "tags": ["tenant_a"]}


def test_dependents_marks_an_unresolved_key():
    ir = compile_source(
        "service S { emission fn drain(x: Str) }\n"
        "component Orphan requires sink: S { emit sink.drain(\"go\") }\n",
        "orphan.rvl")
    entry = query.dependents(ir, "sink")["keys"][0]
    assert entry["resolved"] is False and entry["provider"] is None
    assert entry["service"] == "S"


# ------------------------------------------------------- reaches


def test_reach_is_transitive_across_the_seam(mesh):
    result = query.reach(mesh, "Dashboard")
    assert result["precision"] == query.APPROX
    assert result["reachedComponents"] == ["Api", "Journal", "Store"]
    reached = {(f.get("key") or f.get("name"), tuple(f["path"]))
               for f in result["surface"]["emissions"]}
    assert ("rep", ()) in reached
    assert ("kv", ("rep.publish",)) in reached
    assert ("host_write", ("rep.publish", "kv.set", "ledger.append")) in reached
    assert result["surface"]["iterationBoundaries"] == 1
    assert result["surface"]["compensated"] == 1
    assert result["complete"] is True


def test_reach_separates_emission_from_plain_host_code(mesh):
    result = query.reach(mesh, "Announcer")
    assert [f["name"] for f in result["surface"]["emissions"]] == ["host_write"]
    assert [f["name"] for f in result["surface"]["externs"]] == ["host_fmt"]
    assert result["surface"]["externs"][0]["class"] == "pure"


def test_reach_admits_where_it_is_blind():
    """A key nothing in this IR provides is a dynamic boundary: the result
    must say it is incomplete rather than imply a clean surface."""
    ir = compile_source(
        "service S { emission fn drain(x: Str) }\n"
        "component Orphan requires sink: S { emit sink.drain(\"go\") }\n",
        "orphan.rvl")
    result = query.reach(ir, "Orphan")
    assert result["complete"] is False
    assert result["unresolvedInjections"] == ["sink"]
    assert any("INCOMPLETE" in a for a in result["assumptions"])


def test_reach_of_a_pure_provider_is_empty(mesh):
    result = query.reach(mesh, "Store")
    # Store's own activation only acquires a reversible Map; its emissions
    # all come from the provide-method, which is the honest place for them
    assert any(f.get("key") == "ledger" for f in result["surface"]["emissions"])
    assert result["providers"] == ["Journal"]


# ------------------------------------------------------- drift


def test_drift_reports_the_current_shape(mesh):
    result = query.drift(mesh, "Kv")
    assert result["precision"] == query.EXACT
    by_name = {m["name"]: m for m in result["methods"]}
    assert by_name["set"]["emission"] is True
    assert by_name["set"]["providers"] == ["Store"]
    assert [s["label"] for s in by_name["set"]["callSites"]] == ["Api.rep.publish"]
    assert by_name["get"]["callSites"] == []


def test_drift_gain_implicates_every_provider(tenants):
    result = query.drift(tenants, "Kv", gains=["del"])
    gain = result["gains"][0]
    assert gain["known"] is False
    assert gain["providersMustImplement"] == ["TenantAStore", "TenantBStore"]
    assert gain["callSites"] == []


def test_drift_loss_implicates_providers_and_call_sites(mesh):
    result = query.drift(mesh, "Kv", losses=["set"])
    loss = result["losses"][0]
    assert loss["known"] is True and loss["emission"] is True
    assert loss["providersMustDrop"] == ["Store"]
    assert [s["label"] for s in loss["callSites"]] == ["Api.rep.publish"]
    assert set(result["impacted"]) == {"Store", "Api"}


def test_drift_of_an_undeclared_method_is_a_no_op(mesh):
    loss = query.drift(mesh, "Kv", losses=["nope"])["losses"][0]
    assert loss["known"] is False
    assert loss["providersMustDrop"] == [] and loss["callSites"] == []


def test_drift_unknown_service(mesh):
    result = query.drift(mesh, "Nope")
    assert result["ok"] is False and "Kv" in result["known"]


# ------------------------------------------------------- shared contract


@pytest.mark.parametrize("call", [
    lambda ir: query.emitters(ir, "host_write"),
    lambda ir: query.withdrawal(ir, "Journal"),
    lambda ir: query.dependents(ir, "kv"),
    lambda ir: query.reach(ir, "Dashboard"),
    lambda ir: query.drift(ir, "Kv"),
])
def test_every_result_states_its_own_precision(mesh, call):
    """An agent acting on a result must be able to tell a proof from a
    guess without reading the docs — so it is a field, not prose."""
    result = call(mesh)
    assert result["precision"] in (query.EXACT, query.APPROX)
    assert result["precisionNote"] and result["assumptions"]
    assert json.dumps(result)  # every result is JSON-serialisable as-is


@pytest.mark.parametrize("call", [
    lambda ir: query.emitters(ir, "host_write"),
    lambda ir: query.withdrawal(ir, "Journal"),
    lambda ir: query.dependents(ir, "kv"),
    lambda ir: query.reach(ir, "Dashboard"),
    lambda ir: query.drift(ir, "Kv", gains=["x"], losses=["set"]),
])
def test_every_result_renders_for_humans(mesh, call):
    rendered = query.render(call(mesh))
    assert "precision:" in rendered and "this answer assumes:" in rendered


def test_render_reports_a_miss(mesh):
    assert "unknown component" in query.render(query.withdrawal(mesh, "Ghost"))


# ------------------------------------------------------- CLI


def _cli(*args):
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"))
    return subprocess.run([sys.executable, "-m", "revl", "query", *args],
                          capture_output=True, text=True, cwd=ROOT, env=env)


def test_cli_renders_for_humans():
    done = _cli("emits-to", "host_write", MESH)
    assert done.returncode == 0
    assert "Dashboard (activation)" in done.stdout
    assert "via rep.publish -> kv.set -> ledger.append" in done.stdout


def test_cli_json_is_the_query_result():
    done = _cli("withdraw", "Journal", MESH, "--json")
    assert done.returncode == 0
    payload = json.loads(done.stdout)
    assert payload["precision"] == "exact"
    assert payload["withdrawalOrder"] == ["Dashboard", "Api", "Store", "Journal"]


def test_cli_drift_takes_gains_and_losses():
    done = _cli("drift", "Kv", MESH, "--gains", "del", "--loses", "set", "--json")
    payload = json.loads(done.stdout)
    assert payload["gains"][0]["method"] == "del"
    assert payload["losses"][0]["providersMustDrop"] == ["Store"]


def test_cli_exits_non_zero_on_a_miss():
    done = _cli("reaches", "Ghost", MESH)
    assert done.returncode == 1
    assert "unknown component" in done.stdout


# ------------------------------------------------------- MCP


def _call_tool(name, arguments):
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}})
    return response["result"]


def test_mcp_advertises_every_query():
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert {"revl_query_emitters", "revl_query_withdraw", "revl_query_dependents",
            "revl_query_reach", "revl_query_drift"} <= names
    for tool in listed["result"]["tools"]:
        assert "handler" not in tool
        if tool["name"].startswith("revl_query_"):
            assert tool["annotations"]["readOnlyHint"] is True


def test_mcp_emitters_returns_the_structured_result():
    result = _call_tool("revl_query_emitters",
                        {"files": [MESH], "target": "host_write"})
    payload = result["structuredContent"]
    assert result["isError"] is False
    assert payload["precision"] == "over-approximation"
    assert "Dashboard" in payload["components"]


def test_mcp_withdraw_over_inline_source():
    """The agent path: a composition that has never been a file."""
    result = _call_tool("revl_query_withdraw", {
        "source": "service S { fn a() -> Int }\n"
                  "component P provides s: S { provide s { fn a() = 1 } }\n"
                  "component C requires s: S { let m = effect Map.new() undo m.drop() }\n",
        "component": "P"})
    payload = result["structuredContent"]
    assert [c["component"] for c in payload["cascade"]] == ["C"]
    assert payload["withdrawalOrder"] == ["C", "P"]


def test_mcp_drift_takes_method_lists():
    result = _call_tool("revl_query_drift",
                        {"files": [MESH], "service": "Kv", "loses": ["set"]})
    assert result["structuredContent"]["losses"][0]["providersMustDrop"] == ["Store"]


def test_mcp_reports_a_miss_as_an_error_result():
    result = _call_tool("revl_query_reach", {"files": [MESH], "component": "Ghost"})
    assert result["isError"] is True
    assert "unknown component" in result["structuredContent"]["error"]


def test_mcp_rejects_a_missing_argument():
    result = _call_tool("revl_query_emitters", {"files": [MESH]})
    assert result["isError"] is True
    assert "`target` is required" in \
        result["structuredContent"]["diagnostics"][0]["message"]


def test_mcp_reports_a_rejected_composition_as_diagnostics():
    result = _call_tool("revl_query_reach",
                        {"source": "component Bad { let x = effect boom() }",
                         "component": "Bad"})
    assert result["isError"] is True
    assert result["structuredContent"]["diagnostics"]


# ------------------------------------------------------- agreement with audit


def test_queries_agree_with_the_audit_surface(mesh):
    """`revl audit` and the query layer read the same composition; where
    they overlap they must not disagree."""
    from revl.__main__ import _boundary

    boundary = _boundary(mesh)
    for name in boundary:
        surface = query.reach(mesh, name)["surface"]
        direct = {f["key"] + "." + f["method"] for f in surface["emissions"]
                  if f["direct"] and f["kind"] == "service"}
        assert set(boundary[name]["emissions"]) <= direct
        assert boundary[name]["compensated"] <= surface["compensated"]
