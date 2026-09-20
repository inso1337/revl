"""Item 513, slices 2 and 4: the provider seam, and the second dialect.

Slice 1 derived a decoding grammar, bound it in the IR and left it inert: no
tier read it. These two slices are the half that makes it consumable, and the
half that decides what happens when a provider ignores it.

The decision this file pins (design note §9) is conditional verification:

  * a provider that never takes the constraint CLAIMS nothing. It is validated
    exactly as item 257 already validated it, and the crossing gains no new way
    to be refused. This matters more than it looks: the grammar pins a member
    order the validator is blind to, so holding every provider to it would turn
    a schema-valid-but-differently-ordered completion into a refusal, which is
    the false reject §5 exists to forbid.
  * a provider that DOES take it, through `revl_constrain`, is claiming to have
    constrained this decode with that exact artifact. That claim is checked, and
    a false one is a named `GrammarNotHonouredError` rather than a silent
    acceptance.

The differential that makes this non-vacuous is `test_the_claim_is_what_changes
_the_verdict`: one value, two runs, opposite verdicts, and the only difference
is whether a claim was made.

Slice 4's dialect is pinned by agreement rather than by assertion: for a corpus
that holds both verdicts, the JSON Schema handed to a structured-output provider
accepts exactly the values whose canonical rendering the GBNF accepts. A second
dialect that described a different language would let a provider's CHOICE of
dialect change whether a completion is legal.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from revl import compile_source  # noqa: E402
from revl.decode_grammar import (  # noqa: E402
    JSON_SCHEMA_FORMAT,
    decode_grammar_for,
    json_schema_grammar_for,
)
from revl.mcp.schema import json_schema_for  # noqa: E402

import emit  # noqa: E402
import runtime as rt  # noqa: E402
from runtime import (  # noqa: E402
    GrammarNotHonouredError,
    ResponseValidationError,
    register_grammars,
    revl_constrain,
    revl_decode_grammar,
    validate_response,
    validate_retry,
)

from test_decode_grammar_513 import accepts  # noqa: E402


CALL = {"kind": "record", "fields": {"tool": "Str", "args": "Str"}}
AGENT_TURN = {
    "kind": "variant",
    "cases": [
        {"name": "Final", "payload": "Str"},
        {"name": "ToolCalls", "payload": "List[Call]"},
    ],
}
FLAGSHIP = {"Call": CALL, "AgentTurn": AGENT_TURN}
SCHEMA = json_schema_for("AgentTurn", FLAGSHIP, validated=True)

#: what the emitter composes into the registry: the IR's `response_grammar`
#: plus the second dialect, which is a pure function of `response_schema`.
ENTRY = dict(decode_grammar_for(SCHEMA))
ENTRY["wire_schema"] = json_schema_grammar_for(SCHEMA)

KEY = "Test.complete"

#: canonical: members in the order the grammar pins.
CANONICAL = {"tag": "Final", "value": "hi"}
#: schema-valid and grammar-illegal: the same value, members transposed. This is
#: the ONE observable difference between the two derivations, which is why it is
#: the value every claim test turns on.
REORDERED = {"value": "hi", "tag": "Final"}


@pytest.fixture(autouse=True)
def _registry():
    """A clean registry and no leftover claim per test. The claim register is
    fiber-local state the seam clears itself; resetting it here means a failure
    below is this test's, not the previous one's."""
    register_grammars({KEY: ENTRY})
    rt._revl_grammar_claim.set(None)
    yield
    rt._revl_grammar_claim.set(None)


# --------------------------------------------------------------------------
# the seam: looking is not taking
# --------------------------------------------------------------------------

def test_a_provider_can_read_the_stated_grammar():
    grammar = revl_decode_grammar(KEY)
    assert grammar["format"] == "gbnf"
    assert grammar["digest"] == ENTRY["digest"]


def test_an_unknown_crossing_states_nothing():
    assert revl_decode_grammar("No.such") is None
    assert revl_constrain("No.such", ("gbnf",)) is None


def test_reading_is_not_claiming():
    """A provider that inspects the grammar -- to log it, to decide whether it
    can honour it, to cache a compiled artifact under its digest -- has not
    promised anything, so the verdict is unchanged."""
    revl_decode_grammar(KEY)
    assert validate_response(REORDERED, SCHEMA, "w", None, KEY) == REORDERED


def test_taking_it_names_the_dialect_and_the_artifact():
    dialect, artifact, digest = revl_constrain(KEY, ("gbnf",))
    assert dialect == "gbnf"
    assert artifact == ENTRY["text"]
    assert digest == ENTRY["digest"]


def test_a_dialect_the_crossing_cannot_supply_is_not_taken():
    """And taking nothing must leave no claim behind: a provider that asked for
    a dialect it did not get has promised nothing."""
    assert revl_constrain(KEY, ("ebnf", "lark")) is None
    assert validate_response(REORDERED, SCHEMA, "w", None, KEY) == REORDERED


def test_the_first_supplied_dialect_wins():
    assert revl_constrain(KEY, ("ebnf", "json-schema", "gbnf"))[0] == "json-schema"


# --------------------------------------------------------------------------
# the decision: an unclaimed grammar changes nothing, a claimed one is checked
# --------------------------------------------------------------------------

def test_the_claim_is_what_changes_the_verdict():
    """The differential. One value, two runs, opposite verdicts, and the only
    difference between the runs is whether the provider claimed to have
    constrained the decode. Without this the feature would be either a refusal
    nobody can avoid or a grammar nobody has to honour."""
    assert validate_response(REORDERED, SCHEMA, "w", None, KEY) == REORDERED

    revl_constrain(KEY, ("gbnf",))
    with pytest.raises(GrammarNotHonouredError) as exc:
        validate_response(REORDERED, SCHEMA, "w", None, KEY)
    assert "pins the members" in str(exc.value)
    assert "['tag', 'value']" in str(exc.value)


def test_an_honoured_claim_passes():
    revl_constrain(KEY, ("gbnf",))
    assert validate_response(CANONICAL, SCHEMA, "w", None, KEY) == CANONICAL


def test_the_refusal_is_a_response_fault_so_it_rides_the_retry_loop():
    """`GrammarNotHonouredError` is a `ResponseValidationError` on purpose: a
    re-issued completion may be honoured, so the existing budget applies and the
    body observes the same terminal fault on exhaustion."""
    assert issubclass(GrammarNotHonouredError, ResponseValidationError)
    assert GrammarNotHonouredError.retryable is True


def test_a_claim_naming_another_grammar_is_refused():
    """Worse than not constraining at all: the caller would read the crossing as
    pinned to a type it was not pinned to."""
    rt._revl_grammar_claim.set("f" * 64)
    with pytest.raises(GrammarNotHonouredError) as exc:
        validate_response(CANONICAL, SCHEMA, "w", None, KEY)
    assert "is not the grammar this crossing states" in str(exc.value)


def test_a_malformed_response_is_reported_as_malformed_not_as_a_broken_claim():
    """The schema check runs first. A provider that took the constraint and
    returned prose has two problems, and naming the second one first would send
    the reader to the wrong place."""
    revl_constrain(KEY, ("gbnf",))
    with pytest.raises(ResponseValidationError) as exc:
        validate_response({"tag": "Nope"}, SCHEMA, "w", None, KEY)
    assert not isinstance(exc.value, GrammarNotHonouredError)


def test_a_crossing_with_no_stated_grammar_ignores_a_claim():
    revl_constrain(KEY, ("gbnf",))
    assert validate_response(REORDERED, SCHEMA, "w", None, None) == REORDERED


# --------------------------------------------------------------------------
# a claim is spent by the completion it was made for
# --------------------------------------------------------------------------

def test_a_claim_does_not_outlive_its_completion():
    revl_constrain(KEY, ("gbnf",))
    validate_response(CANONICAL, SCHEMA, "w", None, KEY)
    # the next crossing made no claim, so the reordered value is accepted
    assert validate_response(REORDERED, SCHEMA, "w", None, KEY) == REORDERED


def test_a_claim_is_cleared_even_when_the_schema_check_fails():
    revl_constrain(KEY, ("gbnf",))
    with pytest.raises(ResponseValidationError):
        validate_response({"tag": "Nope"}, SCHEMA, "w", None, KEY)
    assert validate_response(REORDERED, SCHEMA, "w", None, KEY) == REORDERED


def test_the_retry_loop_re_issues_an_unhonoured_completion():
    """Each attempt makes its own claim, and the budget is item 257's."""
    calls = {"n": 0}

    def make():
        calls["n"] += 1
        revl_constrain(KEY, ("gbnf",))
        return REORDERED if calls["n"] < 3 else CANONICAL

    assert validate_retry(make, 3, SCHEMA, "w", None, None,
                          grammar=KEY) == CANONICAL
    assert calls["n"] == 3


def test_the_retry_loop_surfaces_the_named_fault_on_exhaustion():
    def make():
        revl_constrain(KEY, ("gbnf",))
        return REORDERED

    with pytest.raises(GrammarNotHonouredError):
        validate_retry(make, 1, SCHEMA, "w", None, None, grammar=KEY)


# --------------------------------------------------------------------------
# the checkable delta: exactly member presence and member order
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value,honoured", [
    ({"tag": "Final", "value": "hi"}, True),
    ({"value": "hi", "tag": "Final"}, False),
    ({"tag": "ToolCalls", "value": []}, True),
    ({"tag": "ToolCalls", "value": [{"tool": "a", "args": "b"}]}, True),
    # the reorder is nested inside a list item, where the grammar pins it just
    # as hard as at the top
    ({"tag": "ToolCalls", "value": [{"args": "b", "tool": "a"}]}, False),
    ({"tag": "ToolCalls", "value": [{"tool": "a", "args": "b"},
                                    {"args": "d", "tool": "c"}]}, False),
])
def test_the_order_check_agrees_with_the_grammar(value, honoured):
    """The check is the value-observable part of "inside the grammar", so it
    must agree with the recogniser on a value's canonical rendering."""
    assert (rt.grammar_honoured_error(value, SCHEMA) is None) == honoured
    assert accepts(ENTRY["text"], json.dumps(value)) == honoured


def test_the_order_check_is_blind_to_what_it_cannot_see():
    """Stated so it is not mistaken for a proof. Whitespace, number spelling and
    string escaping are gone by the time the provider hands revl a decoded
    value, so a claim that survives this check is necessary, not sufficient."""
    pretty = json.loads(json.dumps(CANONICAL))
    assert rt.grammar_honoured_error(pretty, SCHEMA) is None
    # the same value rendered two ways the grammar treats differently is one
    # value here: nothing downstream of `json.loads` can tell them apart
    assert json.loads(json.dumps(CANONICAL, indent=4)) == pretty


def test_a_map_value_has_no_pinned_key_order():
    """A `Map[Str, V]` renders as an open object whose keys the grammar does not
    name, so there is no order to pin and no refusal to make."""
    schema = json_schema_for("Map[Str, Int]", {}, validated=True)
    assert rt.grammar_honoured_error({"b": 2, "a": 1}, schema) is None


# --------------------------------------------------------------------------
# slice 4: the second dialect describes the same language
# --------------------------------------------------------------------------

DIALECT_CORPUS = [
    {"tag": "Final", "value": "the answer"},
    {"tag": "ToolCalls", "value": []},
    {"tag": "ToolCalls", "value": [{"tool": "search", "args": "{}"}]},
    {"tag": "Final"},
    {"tag": "Final", "value": 7},
    {"tag": "Finall", "value": "x"},
    {"tag": "Final", "value": "x", "note": "sure!"},
    {"value": "x"},
    {"tag": "ToolCalls", "value": [{"tool": "a"}]},
    {"tag": "ToolCalls", "value": [{"tool": "a", "args": "b", "why": "c"}]},
    "the answer",
    [{"tag": "Final", "value": "x"}],
]


@pytest.mark.parametrize("value", DIALECT_CORPUS,
                         ids=lambda v: json.dumps(v)[:40])
def test_the_two_dialects_describe_the_same_language(value):
    """A provider's CHOICE of dialect must not change whether a completion is
    legal. Checked on the canonical rendering, which is the only rendering the
    GBNF admits for a given value and the only thing the JSON Schema can speak
    about at all."""
    wire = json_schema_grammar_for(SCHEMA)["schema"]
    by_schema = rt._json_schema_error(value, wire, "$") is None
    by_grammar = accepts(ENTRY["text"], json.dumps(value))
    assert by_schema == by_grammar, f"the dialects disagree on {value!r}"


def test_the_dialect_corpus_is_not_one_sided():
    wire = json_schema_grammar_for(SCHEMA)["schema"]
    verdicts = [rt._json_schema_error(v, wire, "$") is None
                for v in DIALECT_CORPUS]
    assert verdicts.count(True) == 3
    assert verdicts.count(False) == 9


def test_the_wire_schema_closes_a_nested_record_the_validator_leaves_open():
    """The reason the rewrite exists. Item 257's schema closes only the variant
    arms, so a chatty extra member inside a `Call` validates; the GBNF does not
    admit it. Handing a provider the un-rewritten schema would let an HONOURING
    provider produce a value outside the grammar revl stated."""
    chatty = {"tag": "ToolCalls", "value": [{"tool": "a", "args": "b", "why": "c"}]}
    assert rt._json_schema_error(chatty, SCHEMA, "$") is None
    wire = json_schema_grammar_for(SCHEMA)["schema"]
    assert rt._json_schema_error(chatty, wire, "$") is not None
    assert not accepts(ENTRY["text"], json.dumps(chatty))


def test_nullable_becomes_a_union_the_converter_understands():
    """`nullable` is an OpenAPI keyword. A converter that drops it turns an
    `Opt[Str]` into a plain string and the constrained decode can no longer
    produce the one value the `Opt` was written for."""
    wire = json_schema_grammar_for(
        json_schema_for("Opt[Str]", {}, validated=True))["schema"]
    assert wire == {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert "nullable" not in json.dumps(wire)


def test_nullable_inside_a_record_is_rewritten_too():
    types = {"Row": {"kind": "record",
                     "fields": {"id": "Str", "note": "Opt[Str]"}}}
    wire = json_schema_grammar_for(
        json_schema_for("Row", types, validated=True))["schema"]
    assert wire["properties"]["note"] == {
        "anyOf": [{"type": "string"}, {"type": "null"}]}
    assert wire["additionalProperties"] is False
    assert wire["required"] == ["id", "note"]


def test_the_two_dialects_have_different_digests():
    """So a claim names the artifact that was actually used. Claiming one
    dialect while having constrained with the other is detectable."""
    wire = json_schema_grammar_for(SCHEMA)
    assert wire["format"] == JSON_SCHEMA_FORMAT
    assert wire["digest"] != decode_grammar_for(SCHEMA)["digest"]


def test_the_json_schema_dialect_promises_no_member_order():
    """And is therefore NOT held to one. JSON Schema does not describe member
    order, so refusing a provider that honoured exactly what it was handed would
    be a false reject -- the same mistake §5 refuses on the GBNF side."""
    revl_constrain(KEY, ("json-schema",))
    assert validate_response(REORDERED, SCHEMA, "w", None, KEY) == REORDERED


def test_a_json_schema_claim_is_judged_against_the_artifact_it_was_handed():
    """The residue that skipping would have been fail-open. The wire schema is
    strictly tighter than the one item 257 validates against, so a completion
    that passes the validator can still be outside what the provider took."""
    chatty = {"tag": "ToolCalls", "value": [{"tool": "a", "args": "b", "why": "c"}]}
    # nobody claimed: accepted, exactly as before this slice
    assert validate_response(chatty, SCHEMA, "w", None, KEY) == chatty
    # a json-schema claim: refused, because the wire schema closes that record
    revl_constrain(KEY, ("json-schema",))
    with pytest.raises(GrammarNotHonouredError):
        validate_response(chatty, SCHEMA, "w", None, KEY)
    # and a gbnf claim refuses it too, for the same reason in the other dialect
    revl_constrain(KEY, ("gbnf",))
    with pytest.raises(GrammarNotHonouredError):
        validate_response(chatty, SCHEMA, "w", None, KEY)


def test_the_second_dialect_adds_no_ir():
    """It is a pure function of `response_schema`, which the crossing already
    carries, so the compiler binds nothing new for it."""
    ir = compile_source(_program(validated=True), "t.rvl")
    method = ir["services"]["Model"]["methods"]["complete"]
    assert set(method["response_grammar"]) == {"format", "root", "text", "digest"}
    assert json_schema_grammar_for(method["response_schema"])["schema"] \
        == json_schema_grammar_for(SCHEMA)["schema"]


# --------------------------------------------------------------------------
# the emitted module: one registry, and nothing at all without a validated
# crossing
# --------------------------------------------------------------------------

def _program(*, validated: bool) -> str:
    modifier = "validated " if validated else ""
    return f"""
type Call = {{ tool: Str, args: Str }}
type AgentTurn = Final(Str) | ToolCalls(List[Call])
service Model {{ emission[model] {modifier}fn complete(h: List[Str]) -> AgentTurn }}
service Loop {{ emission fn run(p: Str) -> Int }}
component Agent requires model: Model provides agent: Loop {{
  provide agent {{
    fn run(session_id) {{
      let t = emit model.complete(["p"])
      return match t {{ Final(a) => 1, ToolCalls(c) => 2 }}
    }}
  }}
}}
"""


def test_the_module_registers_its_stated_grammars_under_the_crossing_key():
    code = emit.emit(compile_source(_program(validated=True), "t.rvl"))
    assert "_revl_register_grammars({'Model.complete':" in code
    assert "register_grammars as _revl_register_grammars" in code
    # and the call site refers to the registry by key, not by a second copy of
    # the grammar text
    assert "'Model.complete')" in code
    assert code.count("root ::= ") == 1


def test_a_document_with_no_validated_crossing_registers_nothing():
    """Byte-identity: a program compiled before this slice is unchanged by it."""
    code = emit.emit(compile_source(_program(validated=False), "t.rvl"))
    assert "register_grammars" not in code
    assert "_revl_validate" not in code


def test_a_validated_extern_is_not_registered():
    """This tier validates a service-method crossing and not an extern's return,
    so an extern that took the constraint would make a claim nothing judges. An
    unjudgeable claim is worse than no claim, so the seam offers none."""
    src = """
type Call = { tool: Str, args: Str }
type AgentTurn = Final(Str) | ToolCalls(List[Call])
extern emission validated fn complete(h: Str) -> AgentTurn = @py {
  return {"tag": "Final", "value": h}
}
"""
    ir = compile_source(src, "g.rvl")
    assert ir["externs"][0]["response_grammar"]        # the IR still carries it
    assert emit._grammar_registry(ir.get("services") or {}) == {}


def test_the_registry_carries_both_dialects():
    ir = compile_source(_program(validated=True), "t.rvl")
    entry = emit._grammar_registry(ir["services"])["Model.complete"]
    assert entry["format"] == "gbnf"
    assert entry["wire_schema"]["format"] == JSON_SCHEMA_FORMAT
    assert entry["wire_schema"]["digest"] != entry["digest"]
