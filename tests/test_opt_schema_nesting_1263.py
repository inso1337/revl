"""#1263: `json_schema_for` erased `Opt` nesting, so two declared types derived
one schema object.

`Opt[T]` rendered as `{**inner, "nullable": true}`. That folds the OUTER layer
into a flag on the INNER schema, and a flag does not stack: when `T` already
accepted `null`, the outer layer vanished.

    Opt[Str]       ->  {"type": "string", "nullable": true}
    Opt[Opt[Str]]  ->  {"type": "string", "nullable": true}   # the same dict
    Unit           ->  {"type": "null"}
    Opt[Unit]      ->  {"type": "null", "nullable": true}     # the same documents

Item 257's validating boundary derives its schema from the declared return type
and validates the response against it, so two types that derive one schema are
two types the validator cannot tell apart: a response is accepted for the wrong
declared type and nothing on the 257 surface reports it.

The resolution is recorded in docs/design/1263-opt-nesting-at-a-json-boundary.md.
In short: `Opt[Opt[T]]` IS a distinct type in revl's type system (the checker
refuses to pass one where the other is expected, and `?.` builds it), and it is
NOT expressible in a JSON document, which carries one `null` and cannot say
which layer produced it. So the schema is not taught a nesting it cannot carry;
the boundary gate refuses the spelling instead, the way `revl import wit` and
`revl import openapi` already refuse `option<option<T>>` at their own boundaries.

FAILURE DIRECTION of everything in this file: each test below FAILS on the tree
before the fix (the two schemas compare equal, and each refusal compiles
instead of raising) and passes after it. The two `_control_` tests pass on BOTH
trees and exist so a bug that made every `Opt` inexpressible could not turn this
file green.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from revl import RevlError, compile_source  # noqa: E402
from revl.mcp.schema import (  # noqa: E402
    admits_json_null,
    expressibility_reason,
    fully_expressible,
    json_schema_for,
    tools_from_ir,
)

from runtime import _json_schema_error  # noqa: E402


# ------------------------------------------------------------ the derivation

def test_a_nested_opt_and_its_inner_opt_do_not_derive_one_schema():
    """The defect itself. FAILS before the fix: the two dicts compare equal."""
    inner = json_schema_for("Opt[Str]")
    outer = json_schema_for("Opt[Opt[Str]]")
    assert inner != outer, (
        "`Opt[Str]` and `Opt[Opt[Str]]` derive the SAME schema object, so a "
        "validator built on it cannot tell the two declared types apart")
    assert inner == {"type": "string", "nullable": True}, (
        "the single-layer rendering is unchanged; only the ambiguous one moved")


def test_opt_unit_is_not_validated_as_though_it_were_unit():
    """The same erasure in its shortest form. `Unit` has one value and renders
    as `null`; `Opt[Unit]` has two and had `nullable` added to that same `null`,
    so the two accepted EXACTLY the same documents. FAILS before the fix."""
    unit = json_schema_for("Unit")
    assert _json_schema_error(None, unit) is None, "control: `Unit` accepts null"
    assert _json_schema_error("x", unit) is not None, "control: and nothing else"

    opt_unit = json_schema_for("Opt[Unit]")
    probes = (None, "x", 0, 1.5, True, [], {})
    same_acceptance = all(
        (_json_schema_error(v, unit) is None)
        == (_json_schema_error(v, opt_unit) is None) for v in probes)
    assert not (same_acceptance and fully_expressible("Opt[Unit]")), (
        "`Opt[Unit]` derives a schema accepting exactly what `Unit` accepts AND "
        "is admitted at a validating boundary, so the boundary validates one "
        "type as though it were the other")


@pytest.mark.parametrize("type_name", [
    "Opt[Opt[Str]]",
    "Opt[Opt[Opt[Int]]]",
    "Opt[Unit]",
    "List[Opt[Opt[Int]]]",
    "Map[Str, Opt[Unit]]",
])
def test_a_null_ambiguous_opt_is_refused_at_any_depth(type_name):
    """The gate, at the top level and under a `List`/`Map`. FAILS before the fix
    (every one of these was accepted as fully expressible)."""
    assert not fully_expressible(type_name), (
        f"`{type_name}` reaches an `Opt` whose inner type already accepts "
        "`null`, so the outer layer has no JSON document of its own")
    reason = expressibility_reason(type_name)
    assert reason is not None and "accepts `null`" in reason, (
        f"the refusal must name WHY, not just refuse; got {reason!r}")


def test_the_mcp_projection_degrades_rather_than_renders_a_wrong_type():
    """The ungated consumer. `tools_from_ir` runs with `validated=False` and has
    no `fully_expressible` gate in front of it, so it needs the renderer itself
    to stop claiming a nested `Opt` is its inner type. FAILS before the fix: the
    parameter schema came back as a plain nullable string."""
    tools = {t["name"]: t for t in tools_from_ir(compile_source("""
service Cache { fn get(key: Opt[Opt[Str]]) -> Str }
component C provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache { fn get(key) = "hit" }
}
"""))}
    schema = tools["revl.cache.get"]["inputSchema"]["properties"]["key"]
    assert schema == {"x-revlType": "Opt[Opt[Str]]"}, (
        "the MCP projection rendered `Opt[Opt[Str]]` as a nullable string, "
        f"which is a different type; got {schema!r}")


# ----------------------------------------------------- the gated consumers
#
# All three `json_schema_for(..., validated=True)` call sites in `lower.py` sit
# behind `fully_expressible`, so one refusal covers them. Each is pinned here
# because each is a separate surface a user reaches.

_VALIDATED = """
service Model {{ emission validated fn complete(h: Str) -> {ty} }}
"""

_EVENT = """
event Wrapped(key: id) {{ id: Str, payload: {ty} }}
"""

_ROUTE = """
type Body = {{ note: {ty} }}
service Api {{
  route post "/notes"
  emission fn save(body: Body) -> Str
}}
"""


@pytest.mark.parametrize("source,where", [
    (_VALIDATED, "validated emission response"),
    (_EVENT, "event item"),
    (_ROUTE, "routed body"),
])
def test_a_gated_consumer_refuses_a_nested_opt(source, where):
    """FAILS before the fix: each of these compiled, and the boundary shipped a
    schema for a type it was not validating."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(source.format(ty="Opt[Opt[Str]]"), "nested.rvl")
    message = str(excinfo.value)
    assert "Opt[Opt[Str]]" in message, (
        f"the {where} refusal must name the offending type; got: {message}")
    assert "null" in message, (
        f"the {where} refusal must say why; got: {message}")


# ------------------------------------------------------------------ controls
#
# These pass on BOTH trees. Without them, a change that made every `Opt`
# inexpressible would turn every assertion above green while breaking the
# language.

def test_control_a_single_opt_is_still_expressible_and_still_nullable():
    for type_name, expected in [
        ("Opt[Str]", {"type": "string", "nullable": True}),
        ("Opt[Int]", {"type": "integer", "nullable": True}),
        ("List[Opt[Bool]]",
         {"type": "array", "items": {"type": "boolean", "nullable": True}}),
        ("Unit", {"type": "null"}),
    ]:
        assert fully_expressible(type_name), type_name
        assert expressibility_reason(type_name) is None, type_name
        assert json_schema_for(type_name) == expected, type_name


def test_control_a_single_opt_response_still_compiles():
    ir = compile_source(_VALIDATED.format(ty="Opt[Str]"), "single.rvl")
    method = ir["services"]["Model"]["methods"]["complete"]
    assert method["response_schema"] == {"type": "string", "nullable": True}


def test_control_admits_json_null_is_exactly_unit_and_opt():
    """The helper the gate is built on, pinned positively and negatively so the
    refusal cannot quietly widen to types that do not render as `null`."""
    for yes in ("Unit", "Opt[Str]", "Opt[Opt[Int]]", "Opt[Row]"):
        assert admits_json_null(yes), yes
    for no in ("Str", "Int", "Bool", "Float", "Bytes", "Row",
               "List[Opt[Str]]", "Map[Str, Opt[Str]]", "Result[Str, Str]", None):
        assert not admits_json_null(no), no
