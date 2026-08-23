"""Hole-directed generation: the fill spec (docs/holes.md §8).

An open hole is an obligation, and an obligation says *that* the agent owes an
expression of some type. A fill spec says what the agent has to *work with* at
that position — everything the checker already knew standing there:

  * the expected type the fill must meet;
  * the emission upper bound (may a fill here cross the boundary, within which
    named bound — the G4 question);
  * the in-scope bindings, with their types; and
  * the reachable service signatures a fill may call.

Each field is read straight off the compiled IR — no new inference — so a wrong
answer to the obligation is largely unrepresentable before it is written.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.mcp import fillspec  # noqa: E402
from revl.mcp.server import handle  # noqa: E402


def _specs(source: str) -> list[dict]:
    return fillspec.enrich(compile_source(source))


def _by_line(specs: list[dict]) -> dict[int, dict]:
    return {s["line"]: s for s in specs}


# A component whose provide-method has config, a parameter, a preceding `let`
# bound from an injected dependency, and a hole for its result.
RICH = (
    "service Db { fn q(sql: Str) -> Str }\n"                         # 1
    "service Cache { fn get(key: Str) -> Str }\n"                    # 2
    "component C requires db: Db provides c: Cache {\n"             # 3
    "  config { ttl: Int, name: Str }\n"                            # 4
    "  provide c {\n"                                               # 5
    "    fn get(key) {\n"                                           # 6
    "      let raw = db.q(key)\n"                                   # 7
    '      let out = hole[Str] "look it up"\n'                       # 8
    "      return out\n"                                            # 9
    "    }\n"                                                       # 10
    "  }\n"                                                         # 11
    "}\n")                                                          # 12


def test_fill_spec_carries_the_expected_type():
    spec = _specs(RICH)[0]
    assert spec["expected"] == "Str"
    assert spec["fillSpec"]["expected"] == "Str"


def test_fill_spec_lists_in_scope_bindings_with_their_types():
    spec = _specs(RICH)[0]["fillSpec"]
    bindings = {b["name"]: b["type"] for b in spec["bindings"]}
    # config fields, the method parameter (typed from the service), and the
    # preceding `let` (typed from the dependency's declared return) — and
    # nothing from after the hole.
    assert bindings == {
        "ttl": "Int", "name": "Str", "key": "Str", "raw": "Str"}


def test_fill_spec_names_reachable_service_signatures():
    spec = _specs(RICH)[0]["fillSpec"]
    reachable = spec["reachableServices"]
    assert reachable == [{
        "service": "Db", "method": "q", "signature": "q(sql: Str) -> Str",
        "instance": "db", "emission": False}]


def test_a_hole_in_a_non_emission_method_may_not_emit():
    cap = _specs(RICH)[0]["fillSpec"]["capability"]
    assert cap["mayEmit"] is False
    assert cap["bound"] == []


EMITTING = (
    "service Logger { emission[log] fn write(msg: Str) -> Int }\n"   # 1
    "component W provides w: Logger {\n"                            # 2
    "  provide w {\n"                                               # 3
    "    fn write(msg) {\n"                                         # 4
    '      let code = hole[Int] "compute a code"\n'                  # 5
    "      return code\n"                                           # 6
    "    }\n"                                                       # 7
    "  }\n"                                                         # 8
    "}\n")                                                          # 9


def test_a_hole_in_an_emission_scoped_method_reports_its_bound():
    cap = _specs(EMITTING)[0]["fillSpec"]["capability"]
    assert cap["mayEmit"] is True
    # the bound is the method's named capability set — `emission[log]`.
    assert cap["bound"] == ["log"]


def test_a_bare_emission_method_is_unbounded():
    source = EMITTING.replace("emission[log]", "emission")
    cap = _specs(source)[0]["fillSpec"]["capability"]
    assert cap["mayEmit"] is True
    # bare `emission` names no boundary: the bound is "any" (None), not empty.
    assert cap["bound"] is None


def test_a_hole_in_a_pure_function_may_not_emit():
    source = (
        'fn score(n: Int) -> Int {\n'
        '  let m = hole[Int] "count something"\n'
        '  return m\n'
        '}\n'
        'service S { fn go() -> Int }\n'
        'component C provides s: S { provide s { fn go() = 1 } }\n')
    spec = _specs(source)[0]["fillSpec"]
    assert spec["capability"]["mayEmit"] is False
    assert {b["name"]: b["type"] for b in spec["bindings"]} == {"n": "Int"}


def test_a_component_setup_hole_is_a_pure_position():
    source = (
        "service Cache { fn get(key: Str) -> Str }\n"
        "component C provides c: Cache {\n"
        '  let m = effect hole[Int] "the pool" undo hole[Int] "release it"\n'
        "  provide c { fn get(key) = key }\n"
        "}\n")
    for spec in _specs(source):
        assert spec["fillSpec"]["capability"]["mayEmit"] is False


def test_check_response_carries_a_fill_spec_end_to_end():
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "revl_check",
                                  "arguments": {"source": RICH}}})
    result = response["result"]["structuredContent"]
    assert result["ok"] is True
    assert len(result["holes"]) == 1
    fill = result["holes"][0]["fillSpec"]
    assert fill["expected"] == "Str"
    assert fill["reachableServices"][0]["signature"] == "q(sql: Str) -> Str"
    # the base obligation fields survive unchanged next to the new fillSpec.
    assert result["holes"][0]["code"] == "T3"
    assert result["holes"][0]["category"] == "hole"


def test_finished_code_reports_no_holes():
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "revl_check", "arguments": {"source":
                                  "service Cache { fn get(key: Str) -> Str }\n"
                                  "component C provides c: Cache {\n"
                                  "  provide c { fn get(key) = key }\n"
                                  "}\n"}}})
    assert response["result"]["structuredContent"]["holes"] == []


def test_bindings_thread_forward_only_past_the_hole():
    # a binding declared *after* the hole must not appear in its scope.
    source = (
        "service Cache { fn get(key: Str) -> Str }\n"
        "component C provides c: Cache {\n"
        "  provide c {\n"
        "    fn get(key) {\n"
        '      let before = key\n'
        '      let mid = hole[Str] "fill"\n'
        '      let after = key\n'
        "      return mid\n"
        "    }\n"
        "  }\n"
        "}\n")
    spec = _specs(source)[0]["fillSpec"]
    names = {b["name"] for b in spec["bindings"]}
    assert "before" in names and "key" in names
    assert "after" not in names


def test_a_sample_fill_spec_is_json_serializable():
    # the whole payload must round-trip as JSON for the MCP transport.
    json.dumps(_specs(RICH))
