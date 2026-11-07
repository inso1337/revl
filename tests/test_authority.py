"""The authority graph query (src/revl/authority.py, docs/queries.md).

The fixture has two model roles, each steering one component: `analyst`
routes `Checkout`, which crosses `Payments.charge`, and `reader` routes
`Browser`, which crosses only `Catalog.read`. A secret is bound to
`payments.charge`, so the graph carries all five node kinds.
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from revl import query  # noqa: E402
from revl.compiler import compile_files  # noqa: E402
from revl.mcp import server  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MESH = os.path.join(ROOT, "tests", "fixtures", "authority_mesh.rvl")
UPDATED = os.path.join(ROOT, "tests", "fixtures", "authority_mesh_updated.rvl")


def _authority(paths, capability="payments.charge", previous=None):
    return query.authority(compile_files(paths), capability, previous=previous)


# ------------------------------------------------------------- the answer


def test_a_role_reaches_the_capability_with_one_witness_path():
    result = _authority([MESH])
    assert result["ok"]
    roles = {row["role"]: row for row in result["roles"]}
    assert set(roles) == {"analyst"}
    analyst = roles["analyst"]
    assert analyst["path"] == [
        "role:analyst", "component:Checkout", "service:Payments",
        "capability:payments.charge"]
    # "through one service": the witness names the one service the path crosses
    assert sum(part.startswith("service:") for part in analyst["path"]) == 1
    assert analyst["precision"] == "over-approximation"


def test_a_role_that_cannot_reach_the_capability_is_absent():
    assert "reader" not in {row["role"]
                            for row in _authority([MESH])["roles"]}
    other = _authority([MESH], "catalog.read")
    assert {row["role"] for row in other["roles"]} == {"reader"}
    assert other["roles"][0]["path"] == [
        "role:reader", "component:Browser", "service:Catalog",
        "capability:catalog.read"]


def test_the_graph_carries_all_five_node_kinds_and_the_secret_binding():
    result = _authority([MESH])
    graph = result["graph"]
    assert {node["kind"] for node in graph["nodes"]} == {
        "role", "component", "service", "capability", "secret"}
    assert {"from": "secret:api_key", "to": "capability:payments.charge",
            "kind": "binds", "precision": "exact"} in graph["edges"]
    assert result["secrets"] == ["api_key"]


def test_nodes_and_edges_are_sorted_and_the_output_is_deterministic():
    first = json.dumps(_authority([MESH]), indent=2)
    second = json.dumps(_authority([MESH]), indent=2)
    assert first == second
    graph = json.loads(first)["graph"]
    nodes = [(node["kind"], node["name"]) for node in graph["nodes"]]
    edges = [(edge["from"], edge["kind"], edge["to"])
             for edge in graph["edges"]]
    assert nodes == sorted(nodes)
    assert edges == sorted(edges)


def test_an_unknown_capability_lists_the_known_ones():
    result = _authority([MESH], "payments.unknown")
    assert result["ok"] is False
    assert "unknown capability" in result["error"]
    assert "payments.charge" in result["known"]


# ------------------------------------------------------------------ diff


def test_diff_reports_the_edge_a_pending_update_adds():
    previous = _authority([MESH])
    current = _authority([UPDATED], "payments.refund", previous=previous)
    assert current["ok"]
    delta = current["diff"]
    assert delta["widened"] is True
    assert delta["added"] == [
        "service:Payments --declares--> capability:payments.refund"]
    assert delta["removed"] == []
    # the new operation is reachable through the same component, so the role
    # that steers it now reaches the new capability too
    assert [row["role"] for row in current["roles"]] == ["analyst"]
    assert current["roles"][0]["path"][-1] == "capability:payments.refund"


def test_a_previous_document_that_is_not_an_authority_graph_is_refused():
    result = _authority([MESH], previous={"boundary": {}})
    assert result["ok"] is False
    assert "not an authority graph document" in result["error"]


# ------------------------------------------------------------- transports


def _cli(*args):
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"))
    return subprocess.run([sys.executable, "-m", "revl", "query", "authority",
                           *args],
                          capture_output=True, text=True, cwd=ROOT, env=env)


def test_cli_authority_json_is_the_query_result(tmp_path):
    done = _cli("payments.charge", MESH, "--json")
    assert done.returncode == 0
    payload = json.loads(done.stdout)
    assert payload["query"] == "authority"
    assert [row["role"] for row in payload["roles"]] == ["analyst"]

    previous = tmp_path / "previous.json"
    previous.write_text(done.stdout)
    done = _cli("payments.refund", UPDATED, "--diff", str(previous), "--json")
    assert done.returncode == 0
    delta = json.loads(done.stdout)["diff"]
    assert delta["added"] == [
        "service:Payments --declares--> capability:payments.refund"]


def test_cli_renders_the_witness_path_and_the_diff():
    done = _cli("payments.charge", MESH)
    assert done.returncode == 0
    assert "role:analyst -> component:Checkout -> service:Payments" in done.stdout
    assert "secrets bound to this capability: api_key" in done.stdout


def test_mcp_advertises_and_answers_authority(all_mcp_tools):
    listed = server.handle({"jsonrpc": "2.0", "id": 1,
                            "method": "tools/list"})
    tools = {tool["name"]: tool for tool in listed["result"]["tools"]}
    assert "revl_query_authority" in tools
    assert tools["revl_query_authority"]["annotations"]["readOnlyHint"] is True

    response = server.handle({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "revl_query_authority",
                   "arguments": {"files": [MESH],
                                 "target": "payments.charge"}}})
    result = response["result"]
    assert result["isError"] is False
    assert [row["role"] for row in result["structuredContent"]["roles"]] == [
        "analyst"]
