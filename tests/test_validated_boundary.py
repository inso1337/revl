"""Item 257, Slice 1: the typed model boundary (completions with a checked shape).

The guarantee under test is that a `validated` emission's response is checked
revl-side against a schema DERIVED FROM ITS RETURN TYPE, so a malformed
completion is a typed fault at one named place rather than a silent wrong turn.

Three layers are pinned here:

  * schema.py: the `fully_expressible` gate (§3.3) and the tagged-union
                 rendering (§3.2): the primary soundness backstop.
  * lower.py: the `validated` modifier, the compile-time refusal of an
                 unexpressible response type, and the qualifier-stripped
                 derivation (§3.1, HIGH-2).
  * emit.py: `_revl_validate`: validate-on-response, ADT construction on
                 success, a typed fault on failure (retry is Slice 2).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from revl import RevlError, compile_source  # noqa: E402
from revl.mcp.schema import (  # noqa: E402
    fully_expressible,
    has_revl_stub,
    json_schema_for,
)

import emit  # noqa: E402
from runtime import ResponseValidationError, validate_response  # noqa: E402


# A valid program whose Model emission returns the flagship `AgentTurn`, and its
# non-validated twin (identical but for the `validated` modifier), used to pin
# the compile/lower/emit behaviour end to end.
def _program(validated: bool) -> str:
    modifier = "validated " if validated else ""
    return f"""
type Call = {{ tool: Str, args: Str }}
type AgentTurn = Final(Str) | ToolCalls(List[Call])

fn run_loop(ctx: List[Str], step: (List[Str]) -> AgentTurn, n: Int) -> Str {{
  if (n <= 0) {{ return "max" }}
  return match step(ctx) {{
    Final(answer)    => answer,
    ToolCalls(calls) => run_loop(ctx.push("t"), step, n - 1),
  }}
}}

service Model {{ emission {modifier}fn complete(h: List[Str]) -> AgentTurn }}
service Loop {{ emission fn run(p: Str) -> Str }}
component Agent requires model: Model provides agent: Loop {{
  config {{ max_steps: Int = 8 }}
  provide agent {{
    fn run(session_id) {{
      let msgs = ["prompt"]
      return run_loop(msgs, msgs2 => emit model.complete(msgs2), config.max_steps)
    }}
  }}
}}
"""

# The flagship response type and a few degradations, as a `types` dict in the
# lowered shape `json_schema_for` / `fully_expressible` read.
CALL = {"kind": "record", "fields": {"tool": "Str", "args": "Str"}}
AGENT_TURN = {
    "kind": "variant",
    "cases": [
        {"name": "Final", "payload": "Str"},
        {"name": "ToolCalls", "payload": "List[Call]"},
    ],
}
FLAGSHIP = {"Call": CALL, "AgentTurn": AGENT_TURN}

# a recursive ADT: Json = Null | Arr(List[Json]) | Obj(Map[Str, Json])
JSON_TYPES = {
    "Json": {
        "kind": "variant",
        "cases": [
            {"name": "Null", "payload": None},
            {"name": "Arr", "payload": "List[Json]"},
            {"name": "Obj", "payload": "Map[Str, Json]"},
        ],
    }
}

# a mutually-recursive pair: Tree = Leaf | Branch(Forest); Forest = Nil | More(Tree)
MUTUAL_TYPES = {
    "Tree": {"kind": "variant", "cases": [
        {"name": "Leaf", "payload": None},
        {"name": "Branch", "payload": "Forest"},
    ]},
    "Forest": {"kind": "variant", "cases": [
        {"name": "Nil", "payload": None},
        {"name": "More", "payload": "Tree"},
    ]},
}


# ------------------------------------------------------------ fully_expressible

def test_flagship_is_fully_expressible():
    assert fully_expressible("AgentTurn", FLAGSHIP) is True


def test_untagged_result_is_refused():
    # both the disjoint-arms case and the identical-arms case (which would
    # reject every valid value under oneOf's exactly-one rule).
    assert fully_expressible("Result[Int, Str]", {}) is False
    assert fully_expressible("Result[Str, Str]", {}) is False


def test_non_str_key_map_is_refused_str_key_accepted():
    assert fully_expressible("Map[Int, Str]", {}) is False
    assert fully_expressible("Map[Str, Str]", {}) is True


def test_unknown_nominal_is_refused():
    assert fully_expressible("Foo", {}) is False


def test_cyclic_adt_is_refused_without_recursing_forever():
    # the `seen` set must TERMINATE the walk on a self-recursive ADT; a
    # derive-then-scan gate would stack-overflow here.
    assert fully_expressible("Json", JSON_TYPES) is False


def test_mutually_recursive_pair_is_refused():
    assert fully_expressible("Tree", MUTUAL_TYPES) is False
    assert fully_expressible("Forest", MUTUAL_TYPES) is False


def test_degradations_are_refused_at_depth():
    # the predicate is not top-level-only: each degradation nested inside a
    # List, a record field, a Map value, and a variant payload is refused.
    at_depth = {
        "InList": {"kind": "record", "fields": {"xs": "List[Result[Int, Str]]"}},
        "InField": {"kind": "record", "fields": {"m": "Map[Int, Str]"}},
        "InMapValue": {"kind": "record", "fields": {"m": "Map[Str, Result[Int, Str]]"}},
        "InPayload": {"kind": "variant", "cases": [
            {"name": "Wrap", "payload": "Map[Int, Str]"},
        ]},
        "InListOfMap": {"kind": "record", "fields": {"xs": "List[Map[Int, Str]]"}},
    }
    for name in at_depth:
        assert fully_expressible(name, at_depth) is False, name


def test_expressible_composites_accepted_at_depth():
    ok = {
        "Rec": {"kind": "record", "fields": {"a": "Str", "b": "List[Int]",
                                             "c": "Map[Str, Bool]", "d": "Opt[Int]"}},
        "V": {"kind": "variant", "cases": [
            {"name": "A", "payload": "Rec"},
            {"name": "B", "payload": None},
            {"name": "C", "payload": "List[Rec]"},
        ]},
    }
    assert fully_expressible("V", ok) is True
    assert fully_expressible("Rec", ok) is True


# ------------------------------------------------------------ tagged rendering

def test_payload_variant_renders_discriminated_union():
    schema = json_schema_for("AgentTurn", FLAGSHIP, validated=True)
    arms = {a["properties"]["tag"]["const"]: a for a in schema["oneOf"]}
    assert set(arms) == {"Final", "ToolCalls"}
    final = arms["Final"]
    assert final["additionalProperties"] is False
    assert final["required"] == ["tag", "value"]
    assert final["properties"]["value"] == {"type": "string"}
    tool_calls = arms["ToolCalls"]
    assert tool_calls["properties"]["value"]["type"] == "array"
    # the payload record derives structurally under `value`
    item = tool_calls["properties"]["value"]["items"]
    assert item["type"] == "object" and item["required"] == ["args", "tool"]
    # a const tag + additionalProperties:false leaves no unconstrained stub
    assert not has_revl_stub(schema)


def test_tagged_union_prevents_wrong_constructor_dispatch():
    # A `Final`-shaped value must not satisfy the `ToolCalls` arm and vice
    # versa: the discriminator makes the arms mutually exclusive.
    schema = json_schema_for("AgentTurn", FLAGSHIP, validated=True)
    arms = {a["properties"]["tag"]["const"]: a for a in schema["oneOf"]}
    # the tags are pinned by const, so a value tagged "Final" can match only
    # the Final arm (additionalProperties:false forbids a stray "value" key on
    # a value the wrong arm would accept).
    assert arms["Final"]["properties"]["tag"] == {"const": "Final"}
    assert arms["ToolCalls"]["properties"]["tag"] == {"const": "ToolCalls"}


def test_all_nullary_validated_variant_renders_tagged_not_enum():
    # MEDIUM-1: a validated all-nullary variant is tagged, so adding a payload
    # case never re-encodes the cases beside it.
    step = {"Step": {"kind": "variant", "cases": [
        {"name": "Ready", "payload": None},
        {"name": "Done", "payload": None},
    ]}}
    validated = json_schema_for("Step", step, validated=True)
    tags = {a["properties"]["tag"]["const"] for a in validated["oneOf"]}
    assert tags == {"Ready", "Done"}
    for arm in validated["oneOf"]:
        assert arm["required"] == ["tag"] and arm["additionalProperties"] is False
    # but the plain MCP projection keeps the compact enum
    assert json_schema_for("Step", step) == {"enum": ["Ready", "Done"]}


def test_payload_variant_is_tagged_in_plain_mcp_projection_too():
    # MEDIUM-2: a payload-carrying variant can no longer degrade to the old
    # `x-revlType` stub even for plain (non-validated) MCP projection.
    schema = json_schema_for("AgentTurn", FLAGSHIP)  # validated=False
    assert "oneOf" in schema and not has_revl_stub(schema)


def test_recursive_adt_mcp_projection_terminates_with_stub():
    # the renderer threads `seen` so MCP projection of a recursive ADT does not
    # recurse forever; it falls back to the honest stub on the back-edge.
    schema = json_schema_for("Json", JSON_TYPES)  # validated=False, MCP path
    assert has_revl_stub(schema)  # honest degradation, no crash


def test_non_variant_projection_is_unchanged():
    # byte-identity for the shapes that must not move.
    assert json_schema_for("List[Str]") == {"type": "array", "items": {"type": "string"}}
    assert json_schema_for("Map[Str, Int]") == {
        "type": "object", "additionalProperties": {"type": "integer"}}
    row = json_schema_for("Call", FLAGSHIP)
    assert row == {"type": "object",
                   "properties": {"tool": {"type": "string"}, "args": {"type": "string"}},
                   "required": ["args", "tool"]}


# -------------------------------------------------------- compile-time refusal

def _compile_service_return(return_type: str, *, validated: bool = True,
                            extra_types: str = "") -> None:
    modifier = "validated " if validated else ""
    compile_source(f"""
{extra_types}
service S {{ emission {modifier}fn op(x: Str) -> {return_type} }}
component C requires s: S provides out: Out {{
  provide out {{ fn go(p) {{ let _ = emit s.op(p) return "ok" }} }}
}}
service Out {{ emission fn go(p: Str) -> Str }}
""")


def test_unexpressible_return_types_are_compile_errors():
    JSON = ("type Json = JNull | JArr(List[Json]) | JObj(Map[Str, Json])")
    cases = [
        ("Foo", ""),                                  # unknown nominal
        ("Result[Int, Str]", ""),                     # untagged Result
        ("Result[Str, Str]", ""),                     # identical-arms Result
        ("Map[Int, Str]", ""),                        # non-Str-key Map
        ("List[Result[Int, Str]]", ""),               # degradation at depth
        ("Json", JSON),                               # cyclic ADT
    ]
    for return_type, extra in cases:
        with pytest.raises(RevlError) as exc:
            _compile_service_return(return_type, validated=True, extra_types=extra)
        assert "validated" in str(exc.value).lower(), return_type


def test_cyclic_return_type_refuses_without_crashing():
    # the compile TERMINATES (a RevlError, not a RecursionError); the `seen`
    # set prevents the non-terminating derivation a derive-then-scan gate hits.
    JSON = "type Json = JNull | JArr(List[Json]) | JObj(Map[Str, Json])"
    with pytest.raises(RevlError) as exc:
        _compile_service_return("Json", validated=True, extra_types=JSON)
    assert "recursive" in str(exc.value).lower()


def test_same_types_compile_fine_without_validated():
    # the SAME degradations on a non-validated emission compile unchanged.
    for return_type in ("Result[Int, Str]", "Map[Int, Str]"):
        _compile_service_return(return_type, validated=False)


def test_validated_unit_return_is_refused():
    with pytest.raises(RevlError) as exc:
        compile_source("""
service S { emission validated fn op(x: Str) -> Unit }
component C requires s: S provides out: Out {
  provide out { fn go(p) { let _ = emit s.op(p) return "ok" } }
}
service Out { emission fn go(p: Str) -> Str }
""")
    assert "unit" in str(exc.value).lower()


# ----------------------------------------------------- qualifier-stripping (4b)

def test_untrusted_return_derives_same_union_as_bare():
    bare = compile_source(_program(validated=True))
    tainted = compile_source(_program(validated=True).replace(
        "-> AgentTurn }", "-> Untrusted[AgentTurn] }"))
    bare_schema = bare["services"]["Model"]["methods"]["complete"]["response_schema"]
    tainted_schema = tainted["services"]["Model"]["methods"]["complete"]["response_schema"]
    assert tainted_schema == bare_schema
    assert "oneOf" in bare_schema  # and it is the tagged union, not a stub


# ---------------------------------------------------------- validate_response

FLAGSHIP_SCHEMA = json_schema_for("AgentTurn", FLAGSHIP, validated=True)


class _Final:
    def __init__(self, value):
        self.value = value


class _ToolCalls:
    def __init__(self, value):
        self.value = value


CTORS = {"Final": _Final, "ToolCalls": _ToolCalls}


def test_valid_response_round_trips_to_the_correct_constructor():
    turn = validate_response({"tag": "Final", "value": "done"},
                             FLAGSHIP_SCHEMA, "Agent.run", CTORS)
    assert isinstance(turn, _Final) and turn.value == "done"

    calls = [{"tool": "grep", "args": "x"}]
    turn2 = validate_response({"tag": "ToolCalls", "value": calls},
                              FLAGSHIP_SCHEMA, "Agent.run", CTORS)
    assert isinstance(turn2, _ToolCalls) and turn2.value == calls


def test_malformed_response_raises_the_typed_fault():
    for bad in (
        {"value": "x"},                                   # missing tag
        {"tag": "Nope", "value": "x"},                    # unknown tag
        {"tag": "ToolCalls", "value": "not-an-array"},    # wrong payload type
        {"tag": "Final", "value": "x", "extra": 1},       # additionalProperties
        {"tag": "ToolCalls", "value": [{"tool": "grep"}]},  # missing record field
    ):
        with pytest.raises(ResponseValidationError):
            validate_response(bad, FLAGSHIP_SCHEMA, "Agent.run", CTORS)


def test_no_wrong_constructor_dispatch():
    # a `ToolCalls`-shaped payload (an array) must not validate as `Final`, and
    # a `Final`-shaped payload (a string) must not validate as `ToolCalls`.
    with pytest.raises(ResponseValidationError):
        validate_response({"tag": "Final", "value": [{"tool": "g", "args": ""}]},
                          FLAGSHIP_SCHEMA, "w", CTORS)
    with pytest.raises(ResponseValidationError):
        validate_response({"tag": "ToolCalls", "value": "done"},
                          FLAGSHIP_SCHEMA, "w", CTORS)


def test_fault_is_classified_retryable():
    # the retry BUDGET is Slice 2, but the classification rides the fault now.
    exc = ResponseValidationError("x")
    assert exc.retryable is True


# --------------------------------------------------------------- the emit seam

def test_emit_wraps_validated_call_and_is_byte_identical_when_absent():
    validated_ir = compile_source(_program(validated=True))
    plain_ir = compile_source(_program(validated=False))

    validated_code = emit.emit(validated_ir)
    plain_code = emit.emit(plain_ir)

    # the validated call site is wrapped in the seam with the ctor map;
    assert "_revl_validate(_revl_ctx.model.complete(msgs2)" in validated_code
    assert "{'Final': Final, 'ToolCalls': ToolCalls}" in validated_code
    assert "validate_response as _revl_validate" in validated_code
    # the non-validated twin neither imports nor calls the seam.
    assert "_revl_validate" not in plain_code
    assert "validate_response" not in plain_code
    assert "model.complete(msgs2)" in plain_code


def test_non_validated_ir_is_byte_identical_to_pre_feature():
    # a program with no `validated` emission carries no `validated`/schema keys.
    plain_ir = compile_source(_program(validated=False))
    method = plain_ir["services"]["Model"]["methods"]["complete"]
    assert "validated" not in method
    assert "response_schema" not in method


# ----------------------------------------------------- the extern spelling

_EXTERN_TYPES = """
type Call = { tool: Str, args: Str }
type AgentTurn = Final(Str) | ToolCalls(List[Call])
"""


def test_extern_validated_emission_carries_flag_and_schema():
    ir = compile_source(_EXTERN_TYPES + """
extern emission[model] validated async fn complete(ctx: List[Str]) -> AgentTurn
  = @py { pass }
""")
    entry = next(e for e in ir["externs"] if e["name"] == "complete")
    assert entry["validated"] is True and entry["async"] is True
    assert "oneOf" in entry["response_schema"]


def test_extern_untrusted_return_derives_same_union():
    bare = compile_source(_EXTERN_TYPES + """
extern emission[model] validated fn complete(ctx: List[Str]) -> AgentTurn
  = @py { pass }
""")
    tainted = compile_source(_EXTERN_TYPES + """
extern emission[model] validated fn complete(ctx: List[Str]) -> Untrusted[AgentTurn]
  = @py { pass }
""")
    b = next(e for e in bare["externs"] if e["name"] == "complete")
    t = next(e for e in tainted["externs"] if e["name"] == "complete")
    assert t["response_schema"] == b["response_schema"]


def test_validated_is_emission_only_on_externs():
    with pytest.raises(RevlError) as exc:
        compile_source("extern pure validated fn f(x: Str) -> Str = @py { pass }")
    assert "not an emission" in str(exc.value)


def test_non_validated_extern_ir_has_no_validated_keys():
    ir = compile_source(_EXTERN_TYPES + """
extern emission[model] fn complete(ctx: List[Str]) -> AgentTurn = @py { pass }
""")
    entry = next(e for e in ir["externs"] if e["name"] == "complete")
    assert "validated" not in entry and "response_schema" not in entry
