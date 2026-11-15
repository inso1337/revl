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

from revl import authority as authority_graph  # noqa: E402
from revl import query  # noqa: E402
from revl.compiler import compile_files, compile_source  # noqa: E402
from revl.mcp import server  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MESH = os.path.join(ROOT, "tests", "fixtures", "authority_mesh.rvl")
UPDATED = os.path.join(ROOT, "tests", "fixtures", "authority_mesh_updated.rvl")


SPAWN = os.path.join(ROOT, "tests", "fixtures", "model_reach_spawn",
                     "ok_spawner_holds_the_role_reach.rvl")


def _authority(paths, capability="payments.charge", previous=None):
    return query.authority(compile_files(paths), capability, previous=previous)


def _source(text, capability="payments.charge", previous=None, modules=None):
    ir = compile_source(text, "<authority>.rvl", modules=modules)
    return query.authority(ir, capability, previous=previous)


# a component that itself emits a scoped host extern: the crossing is in the
# audit's per-component `externs`, not in its `emissions`
HOST = (
    "model role analyst on_device reaches [*]\n"
    "extern emission[payments.charge] fn charge_host(n: Int) -> Str "
    "= @py { return str(n) }\n"
    "service Answer { emission fn classify(text: Str) -> Str }\n"
    "component Checkout provides checkout: Answer {\n"
    "  route model on classify { * -> analyst }\n"
    "  provide checkout { fn classify(text: Str) = emit charge_host(1) }\n"
    "}\n")


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
    # the new operation is declared on the service, and the role that steers
    # the component now declares the new capability in its reach
    assert delta["added"] == [
        "role:analyst --steers--> capability:payments.refund",
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
    assert "service:Payments --declares--> capability:payments.refund" in (
        delta["added"])


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


# ----------------------------------------------- what the walk must not skip


def test_a_role_reaches_a_scoped_host_extern_its_component_emits():
    """The component emits `charge_host` itself, so the crossing is in the
    audit's `externs` and no service declares it. It used to answer "no model
    role reaches it" with `precision: exact`, or "unknown capability"."""
    result = _source(HOST)
    assert result["ok"]
    assert [row["role"] for row in result["roles"]] == ["analyst"]
    row = result["roles"][0]
    assert row["path"] == ["role:analyst", "component:Checkout",
                           "capability:payments.charge"]
    assert row["hops"][-1]["kind"] == "host"
    assert row["hops"][-1]["externs"] == ["charge_host"]
    assert row["precision"] == "over-approximation"


def test_an_unscoped_extern_is_reached_under_its_own_name():
    source = HOST.replace("emission[payments.charge] fn charge_host",
                          "emission fn send").replace("charge_host(1)",
                                                      "send(1)")
    result = _source(source, "send")
    assert [row["role"] for row in result["roles"]] == ["analyst"]
    assert result["roles"][0]["path"][-1] == "capability:send"


def test_a_pure_extern_is_not_an_authority_edge():
    source = HOST + ("extern pure fn helper(n: Int) -> Int "
                     "= @py { return n }\n")
    graph = _source(source)["graph"]
    assert "capability:helper" not in {
        f"{n['kind']}:{n['name']}" for n in graph["nodes"]}


def test_a_role_that_steers_the_unnameable_may_reach_any_capability():
    """`Checkout` holds `*`: it emits a host extern under an unscoped
    `emission` method, so the checker cannot name what it may cross. The role
    steering it is listed for a capability it cannot be proven to miss, marked
    `unnameable`. A role whose component holds one named capability is not."""
    source = HOST + (
        "model role reader on_device reaches [catalog.read]\n"
        "service Catalog { emission[catalog.read] fn read(id: Str) -> Str }\n"
        "component Browser requires cat: Catalog provides browse: Answer {\n"
        "  route model on classify { * -> reader }\n"
        "  provide browse { fn classify(t: Str) = emit cat.read(t) }\n"
        "}\n")
    named = {row["role"]: row for row in _source(source)["roles"]}
    assert set(named) == {"analyst"}
    assert "unnameable" not in named["analyst"]          # it has a named path

    other = {row["role"]: row for row in _source(source, "catalog.read")["roles"]}
    assert set(other) == {"analyst", "reader"}
    assert other["analyst"]["unnameable"] is True
    assert other["analyst"]["path"][-1] == "capability:*"
    assert "unnameable" not in other["reader"]
    assert "unnameable" in query.render(_source(source, "catalog.read"))


def test_an_unknown_capability_still_names_the_roles_that_may_reach_it():
    source = HOST.replace("emission[payments.charge] fn charge_host",
                          "emission[payments.charge] fn charge_host")
    miss = _source(source, "nothing.here")
    assert miss["ok"] is False
    # the role reaches `*` (the component's provided emission is unscoped)
    assert [row["role"] for row in miss["unnameableRoles"]] == ["analyst"]
    assert "may reach any capability" in query.render(miss)


def test_a_first_class_dispatch_is_an_unnameable_reach():
    """An IR from before issue #2842 can still carry a first-class dispatch.
    The audit lists it as `*`; the graph keeps it rather than dropping it."""
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    from _legacy_emission_ir import value_rule_off  # noqa: PLC0415

    source = (
        "model role analyst on_device reaches [*]\n"
        "extern emission[payments.charge] fn charge(n: Str) -> Str "
        "= @py { return n }\n"
        "fn dispatch(x: Str) -> Str { return [charge][0](x) }\n"
        "service Answer { emission fn classify(text: Str) -> Str }\n"
        "component Checkout provides checkout: Answer {\n"
        "  route model on classify { * -> analyst }\n"
        "  provide checkout { fn classify(text: Str) = emit dispatch(text) }\n"
        "}\n")
    with value_rule_off():
        ir = compile_source(source, "<legacy>.rvl")
    assert [row["role"] for row in query.authority(ir, "payments.charge")[
        "roles"]] == ["analyst"]
    graph = query.authority(ir, "payments.charge")["graph"]
    assert {"from": "component:Checkout", "to": "capability:*",
            "kind": "host", "precision": "over-approximation",
            "externs": ["*"]} in graph["edges"]


def test_a_capability_the_component_holds_but_does_not_emit_is_still_steered():
    """`Worker` holds `shell.exec` through a `requires` it never calls, and the
    role `tool` declares that reach. No crossing names it, yet the role is the
    one that can steer the component there."""
    result = _authority([SPAWN], "shell.exec")
    assert [row["role"] for row in result["roles"]] == ["tool"]
    row = result["roles"][0]
    assert row["path"] == ["role:tool", "capability:shell.exec"]
    assert row["hops"][0]["kind"] == "steers"
    assert row["hops"][0]["components"] == ["Worker"]
    # the capability the role's reach does not cover is not steered
    kinds = {edge["kind"] for edge in result["graph"]["edges"]
             if edge["from"] == "role:tool"
             and edge["to"] == "capability:model.complete"}
    assert "steers" not in kinds


def test_an_audited_crossing_is_the_witness_when_one_exists():
    """`steers` fills gaps only: a path through the component's own audit
    surface is the witness, so the answer names the service it crosses."""
    result = _authority([SPAWN], "model.complete")
    assert result["roles"][0]["path"][-2:] == [
        "service:Completions", "capability:model.complete"]


def test_a_composition_with_no_approximate_edge_is_never_exact():
    """Only a secret binding here, so every edge is exact. An empty answer is
    still no proof: the answer is a may-analysis whatever the edge mix."""
    source = (
        "extern emission[model.complete] fn complete(p: Str) -> Str "
        "= @py { return p }\n"
        "secret openai_key for model.complete\n"
        "service Ops { emission fn go(u: Str) -> Int }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops { fn go(u) { return 0 } }\n}\n")
    result = _source(source, "model.complete")
    assert result["roles"] == []
    assert all(edge["precision"] == "exact"
               for edge in result["graph"]["edges"])
    assert result["precision"] == "over-approximation"
    assert "no path the checker enumerates" in result["precisionNote"]


def test_a_module_joined_with_use_contributes_its_externs():
    lib = ("pub extern emission[payments.charge] fn charge_host(n: Str) -> Str "
           "= @py { return n }\n")
    source = (
        'use "lib.rvl" { charge_host }\n'
        "model role analyst on_device reaches [*]\n"
        "service Answer { emission fn classify(text: Str) -> Str }\n"
        "component Checkout provides checkout: Answer {\n"
        "  route model on classify { * -> analyst }\n"
        "  provide checkout { fn classify(text: Str) = emit charge_host(text) }\n"
        "}\n")
    result = _source(source, modules={"lib.rvl": lib})
    assert [row["role"] for row in result["roles"]] == ["analyst"]


def test_realms_that_provide_one_service_are_one_service_node():
    """The limit the docs state: services join by name, so a role in either
    realm reaches the capability, and there is one `Payments` node."""
    source = (
        "model role ra on_device reaches [payments.charge]\n"
        "model role rb on_device reaches [payments.charge]\n"
        "service Payments { emission[payments.charge] fn charge(n: Int) -> Str }\n"
        "service Answer { emission fn classify(text: Str) -> Str }\n"
        "component AppA requires pay: Payments provides a: Answer {\n"
        '  isolate pay in realm("a")\n'
        "  route model on classify { * -> ra }\n"
        "  provide a { fn classify(text: Str) = emit pay.charge(1) }\n"
        "}\n"
        "component AppB requires pay: Payments provides b: Answer {\n"
        '  isolate pay in realm("b")\n'
        "  route model on classify { * -> rb }\n"
        "  provide b { fn classify(text: Str) = emit pay.charge(1) }\n"
        "}\n")
    result = _source(source)
    assert [row["role"] for row in result["roles"]] == ["ra", "rb"]
    services = [n for n in result["graph"]["nodes"] if n["kind"] == "service"
                and n["name"] == "Payments"]
    assert len(services) == 1


# ------------------------------------------------ what --diff must not miss


def _graph(role_attrs=None, edge_precision="exact"):
    role = {"kind": "role", "name": "r", "residence": "on_device",
            "reachDeclared": True, "opaque": False, "reaches": ["a.b"]}
    role.update(role_attrs or {})
    return {"nodes": [role],
            "edges": [{"from": "component:C", "to": "capability:a.b",
                       "kind": "host", "precision": edge_precision}]}


def test_diff_reports_a_precision_loosening_as_a_widening():
    delta = authority_graph.diff(_graph(), _graph(edge_precision=(
        "over-approximation")))
    assert delta["added"] == [] and delta["removed"] == []
    assert delta["changed"] == [{
        "what": "edge", "id": "component:C --host--> capability:a.b",
        "field": "precision", "before": "exact",
        "after": "over-approximation", "widened": True}]
    assert delta["widened"] is True
    # the tightening is reported and does not fail the gate
    back = authority_graph.diff(_graph(edge_precision="over-approximation"),
                                _graph())
    assert back["changed"][0]["widened"] is False and back["widened"] is False


def test_diff_reports_a_role_that_turns_opaque_as_a_widening():
    delta = authority_graph.diff(
        _graph(), _graph({"reachDeclared": False, "opaque": True,
                          "reaches": []}))
    fields = {row["field"]: row for row in delta["changed"]}
    assert fields["opaque"]["widened"] is True
    assert fields["opaque"]["before"] is False
    assert delta["widened"] is True


def test_diff_reports_a_role_leaving_the_device():
    delta = authority_graph.diff(_graph(),
                                 _graph({"residence": "off_device"}))
    assert delta["changed"] == [{
        "what": "role", "id": "role:r", "field": "residence",
        "before": "on_device", "after": "off_device", "widened": True}]
    assert delta["widened"] is True
    back = authority_graph.diff(_graph({"residence": "off_device"}), _graph())
    assert back["widened"] is False and back["changed"][0]["widened"] is False


def test_diff_sees_a_residence_change_between_two_compositions():
    previous = _source(HOST)
    current = _source(HOST.replace("analyst on_device", "analyst off_device"),
                      previous=previous)
    delta = current["diff"]
    assert [row["field"] for row in delta["changed"]] == ["residence"]
    assert delta["widened"] is True
    assert "WIDENS" in query.render(current)


def test_an_unchanged_composition_reports_no_change():
    previous = _source(HOST)
    delta = _source(HOST, previous=previous)["diff"]
    assert delta["added"] == delta["removed"] == delta["changed"] == []
    assert delta["widened"] is False
