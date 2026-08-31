"""The MCP bridge: service ⇄ tool projection, and the compiler as a server.

The load-bearing claim under test is that a generated tool description
*cannot lie about side effects*: annotations come from the declaration, and
the checker holds every provider to it as an upper bound (G4 emission
propagation), so no body can exceed what its tool advertises.
"""

import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files, compile_source  # noqa: E402
from revl.diagnostics import classify  # noqa: E402
from revl.mcp.schema import import_tools, json_schema_for, tools_from_ir  # noqa: E402
from revl.mcp.server import handle, serve  # noqa: E402

EXAMPLES = ROOT / "examples"


def _tools(source: str) -> dict:
    return {t["name"]: t for t in tools_from_ir(compile_source(source))}


# ---------------------------------------------------------------- revl -> MCP

READONLY = """
service Cache { fn get(key: Str) -> Opt[Str] }
component C provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache { fn get(key) = store.get(key) }
}
"""


def test_non_emission_projects_read_only():
    tool = _tools(READONLY)["revl.cache.get"]
    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["annotations"]["destructiveHint"] is False
    assert tool["x-revl"]["annotationsDerivedFrom"] == "compiler"


def test_declared_emission_projects_destructive():
    tools = _tools("""
service Bus { emission fn send(msg: Str) -> Int }
component B provides bus: Bus {
  let q = effect Map.new() undo q.drop()
  provide bus { fn send(msg) = 1 }
}
""")
    tool = tools["revl.bus.send"]
    assert tool["annotations"]["destructiveHint"] is True
    assert tool["x-revl"]["classification"] == "emission"


def test_a_declaration_that_understates_its_body_does_not_compile():
    """The rule that makes the projection sound: a service declaration is an
    upper bound on its providers' effects, so the annotation cannot lie."""
    with pytest.raises(RevlError) as excinfo:
        compile_source("""
service Database { emission fn execute(sql: Str) -> Int }
service Cache { fn put(key: Str, value: Str) }
component Lying requires db: Database provides cache: Cache {
  provide cache {
    fn put(key, value) { emit db.execute(key) }
  }
}""")
    assert "declared plain, but this implementation reaches `db.execute`" in str(excinfo.value)


def test_declared_emission_carries_its_provenance():
    """`put` declares the emission its body performs; the projection reports
    both the contract and what the body reaches."""
    tools = {t["name"]: t for t in tools_from_ir(
        compile_files([str(EXAMPLES / "user_cache.rvl")]))}
    put = tools["revl.cache.put"]
    assert put["annotations"]["readOnlyHint"] is False
    assert put["annotations"]["destructiveHint"] is True
    effects = put["x-revl"]["effects"]
    assert effects["reachesEmission"] == ["db.execute"]
    assert effects["boundedByDeclaration"] is True
    assert "db.execute" in put["description"]


def test_read_only_sibling_stays_read_only():
    # the bound is per operation, not per service
    tools = {t["name"]: t for t in tools_from_ir(
        compile_files([str(EXAMPLES / "user_cache.rvl")]))}
    assert tools["revl.cache.get"]["annotations"]["readOnlyHint"] is True


def test_open_world_hint_tracks_extern_reachability():
    tools = _tools("""
extern pure fn shape(x: Str) -> Str = @py { return x }
extern emission fn ship(x: Str) -> Str = @py { return x }
service S { fn quiet(a: Str) -> Str
            emission fn loud(a: Str) -> Str }
component C provides s: S {
  provide s {
    fn quiet(a) = a
    fn loud(a) = ship(a)
  }
}
""")
    assert tools["revl.s.quiet"]["annotations"]["openWorldHint"] is False
    loud = tools["revl.s.loud"]
    assert loud["annotations"]["openWorldHint"] is True
    # an `emission` extern also makes it destructive
    assert loud["annotations"]["destructiveHint"] is True
    assert loud["x-revl"]["effects"]["reachesHostCode"] == ["ship"]


def test_extern_reachability_is_transitive_through_fns():
    tools = _tools("""
extern emission fn ship(x: Str) -> Str = @py { return x }
fn middle(x: Str) -> Str { return ship(x) }
fn outer(x: Str) -> Str { return middle(x) }
service S { emission fn go(a: Str) -> Str }
component C provides s: S {
  provide s { fn go(a) = outer(a) }
}
""")
    assert tools["revl.s.go"]["x-revl"]["effects"]["reachesHostCode"] == ["ship"]


def test_input_schema_from_declared_types():
    tool = _tools(READONLY)["revl.cache.get"]
    assert tool["inputSchema"]["properties"]["key"] == {"type": "string"}
    assert tool["inputSchema"]["required"] == ["key"]
    assert tool["inputSchema"]["additionalProperties"] is False


def test_optional_params_are_not_required():
    tool = _tools("""
service S { fn f(a: Str, b: Opt[Int]) -> Str }
component C provides s: S { provide s { fn f(a, b) = a } }
""")["revl.s.f"]
    assert tool["inputSchema"]["required"] == ["a"]


def test_json_schema_for_containers_and_records():
    types = {"Row": {"kind": "record", "fields": {"id": "Int", "name": "Str"}}}
    assert json_schema_for("List[Str]") == {"type": "array", "items": {"type": "string"}}
    row = json_schema_for("Row", types)
    assert row["type"] == "object" and row["required"] == ["id", "name"]
    assert json_schema_for("Unknown") == {"x-revlType": "Unknown"}


def test_only_provided_keys_are_exposed():
    names = _tools("""
service Database { fn query(sql: Str) -> List[Row] }
service Cache { fn get(key: Str) -> Opt[Str] }
component C requires db: Database provides cache: Cache {
  provide cache { fn get(key) = None }
}
""")
    assert set(names) == {"revl.cache.get"}  # `db` is a requirement, not surface


# ---------------------------------------------------------------- MCP -> revl

MANIFEST = {"tools": [
    {"name": "search.query", "description": "Search",
     "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}},
                     "required": ["q"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "files.write", "description": "Write",
     "inputSchema": {"type": "object",
                     "properties": {"path": {"type": "string"}}, "required": ["path"]},
     "annotations": {"readOnlyHint": False}},
    {"name": "no_annotations", "description": "Unknown behaviour",
     "inputSchema": {"type": "object", "properties": {"x": {"type": "string"}}}},
]}


def test_import_trusts_only_an_explicit_read_only_claim():
    source = import_tools(MANIFEST, service="Tools", key="tools", backend="py")
    assert "fn query(q: Str) -> Str" in source
    assert "emission fn write(path: Str) -> Str" in source
    # absent annotations are not a read-only claim
    assert "emission fn no_annotations" in source


def test_imported_source_compiles_and_lands_on_the_audit_surface(tmp_path):
    source = import_tools(MANIFEST, service="Tools", key="tools", backend="py")
    path = tmp_path / "imported.rvl"
    path.write_text(source, encoding="utf-8")
    ir = compile_files([str(path)])
    classes = {e["name"]: e["class"] for e in ir["externs"]}
    assert classes["mcp_query"] == "pure"
    assert classes["mcp_write"] == "emission"
    assert classes["mcp_no_annotations"] == "emission"


def test_import_sanitizes_tool_names():
    manifest = {"tools": [{"name": "weird-name.with spaces",
                           "inputSchema": {"type": "object", "properties": {}}}]}
    source = import_tools(manifest)
    assert "with_spaces" in source


# ---------------------------------------------------------------- the server

def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def test_initialize_and_tools_list():
    init = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "revl"
    listed = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = {t["name"]: t for t in listed["result"]["tools"]}
    assert set(tools) == {"revl_check", "revl_admit", "revl_plan", "revl_audit",
                          "revl_tools", "revl_grammar", "revl_load", "revl_call",
                          "revl_swap", "revl_rollback", "revl_unload", "revl_state",
                          # the session commit protocol (docs/design/245-session-commit.md)
                          "revl_commit", "revl_commit_confirm", "revl_abort",
                          # the auto-approve policy (docs/design/246-auto-approve.md)
                          "revl_approve",
                          # early revocation of a standing grant (roadmap item 379)
                          "revl_revoke",
                          # generation history + operator undo (docs/generation-history.md)
                          "revl_undo",
                          # component leases: the multi-agent workspace (item 61)
                          "revl_lease",
                          # the repair loop (docs/repair-loop.md, item 62)
                          "revl_repair",
                          # deltas, not documents (docs/mcp-bridge.md, item 50)
                          "revl_edit",
                          # one intent, one call: fused ship (docs/token-economy.md, item 50)
                          "revl_ship",
                          # the proving ground (docs/gauntlet.md)
                          "revl_gauntlet",
                          # the quarantine tier (docs/quarantine-tier.md)
                          "revl_quarantine",
                          # composition persistence (docs/persistence.md)
                          "revl_snapshot", "revl_restore",
                          # composition queries (docs/queries.md)
                          "revl_query_emitters", "revl_query_withdraw",
                          "revl_query_dependents", "revl_query_reach",
                          "revl_query_drift",
                          # live + historical query modes (docs/queries.md §9)
                          "revl_live_query", "revl_history_emitted_between",
                          "revl_history_lifetime",
                          # backwards replay (docs/replay.md)
                          "revl_timeline", "revl_inspect_step", "revl_step_back",
                          "revl_replay_forward", "revl_replay_bisect",
                          # the component registry read path (docs/registry.md)
                          "revl_resolve",
                          # verified canary (docs/verified-canary.md, item 59)
                          "revl_canary",
                          # approval distillation operator surface (item 251)
                          "revl_distillation_offers", "revl_apply_distillation",
                          "revl_revoke_distillation"}
    # inspection tools are read-only; the ones that move a running system say so
    assert tools["revl_check"]["annotations"]["readOnlyHint"] is True
    assert tools["revl_swap"]["annotations"]["destructiveHint"] is True
    assert tools["revl_unload"]["annotations"]["destructiveHint"] is True
    assert tools["revl_query_emitters"]["annotations"]["readOnlyHint"] is True


def test_check_accepts_a_valid_component():
    payload = _call("revl_check", {"source": READONLY})
    assert payload["ok"] is True
    assert payload["loadOrder"] == ["C"]
    assert "boundary" in payload


def test_check_returns_a_structured_diagnostic():
    payload = _call("revl_check", {"source": """
service D { fn q(sql: Str) -> Int }
component P requires db: D {
  let r = effect db.q(42) undo db.q("x")
}"""})
    assert payload["ok"] is False
    diagnostic = payload["diagnostics"][0]
    assert diagnostic["code"] == "T1"
    assert diagnostic["expected"] == "Str"
    assert diagnostic["actual"] == "Int"
    assert diagnostic["guarantee"]  # the agent learns *why*, not just what


def test_check_reports_a_guarantee_violation_with_its_tag():
    payload = _call("revl_check", {"source": """
component Leaky {
  let pool = effect Pool.open("u", 1)
}"""})
    assert payload["ok"] is False
    assert payload["diagnostics"][0]["code"] in ("G4", "SYNTAX")


def test_admit_requires_a_running_manifest():
    payload = _call("revl_admit", {"source": READONLY})
    assert payload["ok"] is False
    assert "manifest" in payload["diagnostics"][0]["message"]


def test_admit_accepts_a_compatible_candidate():
    running = compile_source(READONLY)
    payload = _call("revl_admit", {
        "manifest": running,
        "source": """
service Cache { fn get(key: Str) -> Opt[Str] }
service Log { fn note(m: Str) -> Int }
component Watcher requires cache: Cache provides log: Log {
  provide log { fn note(m) = 1 }
}""",
    })
    assert payload["ok"] is True and payload["admitted"] is True


def test_admit_refuses_interface_drift():
    running = compile_source(READONLY)
    payload = _call("revl_admit", {
        "manifest": running,
        "source": """
service Cache { fn get(key: Str) -> Int }
component Other requires cache: Cache {
  let x = effect Map.new() undo x.drop()
}""",
    })
    assert payload["ok"] is False and payload["admitted"] is False
    assert "differs from the running manifest" in payload["diagnostics"][0]["message"]


def test_admit_refuses_a_duplicate_provider():
    running = compile_source(READONLY)
    payload = _call("revl_admit", {"manifest": running, "source": READONLY.replace(
        "component C ", "component D ")})
    assert payload["admitted"] is False
    assert payload["diagnostics"][0]["code"] == "G2"


# A provider swap ships the candidate providing a key an existing component
# already provides, with `replacing=[that component]`. `_tool_admit` must honor
# `replacing` on the compile it admits against — otherwise the swap reads as a
# duplicate provider (false G2) and every model-route/mock->real swap is
# refused. See roadmap item 100.
def test_admit_honors_replacing_for_a_provider_swap():
    running = compile_source(READONLY)
    payload = _call("revl_admit", {
        "manifest": running,
        "source": READONLY.replace("component C ", "component D "),
        "replacing": ["C"],
    })
    assert payload["ok"] is True and payload["admitted"] is True


def test_admit_still_refuses_a_conflict_against_a_non_replaced_provider():
    running = compile_source(READONLY)
    payload = _call("revl_admit", {
        "manifest": running,
        "source": READONLY.replace("component C ", "component D "),
        "replacing": ["Bogus"],  # names a component that provides nothing here
    })
    assert payload["admitted"] is False
    assert payload["diagnostics"][0]["code"] == "G2"


def test_audit_exposes_the_boundary_surface():
    payload = _call("revl_audit", {"files": [str(EXAMPLES / "user_cache.rvl")]})
    assert payload["ok"] is True
    assert payload["boundary"]["UserCache"]["emissions"] == ["db.execute"]
    assert "G4" in payload["guarantees"]


def test_tools_tool_projects_the_composition():
    payload = _call("revl_tools", {"source": READONLY, "composition": "app"})
    assert payload["ok"] is True
    assert payload["tools"][0]["name"] == "app.cache.get"


def test_grammar_fits_in_a_prompt():
    payload = _call("revl_grammar", {})
    assert payload["ok"] is True
    assert "effect" in payload["grammar"] and "undo" in payload["grammar"]
    assert len(payload["grammar"]) < 4000  # the prompt-sized invariant


def test_unknown_tool_is_a_protocol_error():
    response = handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                       "params": {"name": "nope", "arguments": {}}})
    assert response["error"]["code"] == -32602


def test_notifications_get_no_response():
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tool_result_marks_isError_for_rejections():
    response = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "revl_check",
                                  "arguments": {"source": "component {"}}})
    assert response["result"]["isError"] is True


def test_serve_reads_newline_delimited_jsonrpc():
    stdin = io.StringIO(
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
        "\n"  # blank lines are skipped
        '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
    )
    stdout = io.StringIO()
    assert serve(stdin, stdout) == 0
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [m["id"] for m in lines] == [1, 2]


def test_serve_reports_a_parse_error():
    stdout = io.StringIO()
    serve(io.StringIO("not json\n"), stdout)
    assert json.loads(stdout.getvalue())["error"]["code"] == -32700


# ---------------------------------------------------------------- diagnostics

def test_classify_reads_the_guarantee_tag_from_the_message():
    error = RevlError("f.rvl", 3, "call to emission `db.x` must be marked `emit` (G4)")
    record = classify(error)
    assert record["code"] == "G4"
    assert record["guarantee"].startswith("every mutation")


def test_classify_falls_back_to_shape_patterns():
    assert classify(RevlError("f.rvl", 1, "dependency cycle: A -> B -> A"))["code"] == "G3"
    assert classify(RevlError("f.rvl", 1, "`null` has no type in revl"))["code"] == "T2"


def test_type_errors_carry_expected_and_actual():
    with pytest.raises(RevlError) as excinfo:
        compile_source("fn f() -> Int { return \"nope\" }")
    record = classify(excinfo.value)
    assert (record["expected"], record["actual"]) == ("Int", "Str")


def test_provide_key_mismatch_is_coded_A9_with_a_two_fix_hint():
    """item 153: a provide block keyed on something the `provides` clause never
    announced classifies as A9 (the sibling of A6) and carries a hint naming
    both fixes — rename the block, or add the key to the clause."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(
            "service Skin { fn name() -> Str }\n"
            "component S provides skin1: Skin {\n"
            "  provide skin { fn name() = \"x\" }\n"
            "}\n"
        )
    record = classify(excinfo.value)
    assert record["code"] == "A9"
    assert record["guarantee"].startswith("a provide key is declared")
    assert record["hint"]
    assert "rename" in record["hint"] and "add" in record["hint"]
