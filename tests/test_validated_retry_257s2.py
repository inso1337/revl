"""Item 257, Slice 2: the declared `retry N` validation-retry budget.

Slice 1 landed the `validated` modifier, the `fully_expressible` refusal, the
tagged-union rendering, and validate-on-response (the typed
`ResponseValidationError`, already `retryable=True`). Slice 2 adds:

  * parser/lower: the `retry N` clause (a positive literal) on a `validated`
    emission, legal only alongside `validated`, byte-identical when absent.
  * emit.py/runtime.py: the read-with-a-cost retry LOOP (§5.2) that re-fires
    ONLY the completion call on a `ResponseValidationError`, up to N times, then
    surfaces the typed fault. It composes with teardown and never double-emits a
    downstream effect (§5.3), because the seam is at the forward crossing before
    the value binds.
  * cardinality.py (the 257-review HIGH fix): a `validated retry N` crossing's
    contribution is multiplied by `N + 1` (the crossing can fire up to N+1
    times), modeled as a STATIC MULTIPLIER on the single crossing node - not a
    loop, not a recursion - so item 260's bounded-iteration recognizer is not
    involved and the `<= N + 1` ceiling is exact by construction.
"""

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from revl import RevlError, compile_source  # noqa: E402
from revl.cardinality import cardinality  # noqa: E402
from revl.mcp.schema import json_schema_for  # noqa: E402

import emit  # noqa: E402
from runtime import (  # noqa: E402
    ResponseValidationError,
    TransientError,
    validate_retry,
    validate_retry_async,
)


# --------------------------------------------------------------------------
# shared: the flagship response type and its derived boundary schema
# --------------------------------------------------------------------------

CALL = {"kind": "record", "fields": {"tool": "Str", "args": "Str"}}
AGENT_TURN = {
    "kind": "variant",
    "cases": [
        {"name": "Final", "payload": "Str"},
        {"name": "ToolCalls", "payload": "List[Call]"},
    ],
}
FLAGSHIP = {"Call": CALL, "AgentTurn": AGENT_TURN}
FLAGSHIP_SCHEMA = json_schema_for("AgentTurn", FLAGSHIP, validated=True)


class Final:
    def __init__(self, value):
        self.value = value


class ToolCalls:
    def __init__(self, value):
        self.value = value


CTORS = {"Final": Final, "ToolCalls": ToolCalls}

_GOOD_FINAL = {"tag": "Final", "value": "done"}
_GOOD_TOOLS = {"tag": "ToolCalls", "value": [{"tool": "grep", "args": "x"}]}
_MALFORMED = {"tag": "Nope"}


# A component whose provide body has a STRAIGHT-LINE validated emission, so the
# crossing counts exactly (no recursion/loop between it and the boundary). The
# retry budget is a parameter of the source.
def _program(*, validated: bool, retry: int | None, cap: str = "model") -> str:
    mods = []
    if validated:
        mods.append("validated")
    if retry is not None:
        mods.append(f"retry {retry}")
    modifier = (" ".join(mods) + " ") if mods else ""
    return f"""
type Call = {{ tool: Str, args: Str }}
type AgentTurn = Final(Str) | ToolCalls(List[Call])
service Model {{ emission[{cap}] {modifier}fn complete(h: List[Str]) -> AgentTurn }}
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


# --------------------------------------------------------------------------
# the retry LOOP: re-fire only the completion, bounded by the declared budget
# --------------------------------------------------------------------------

def test_malformed_twice_then_valid_succeeds_after_retries():
    calls = {"n": 0}

    def make():
        calls["n"] += 1
        return _MALFORMED if calls["n"] < 3 else _GOOD_FINAL

    turn = validate_retry(make, 2, FLAGSHIP_SCHEMA, "Agent.run", CTORS)
    assert isinstance(turn, Final) and turn.value == "done"
    # exactly three crossings: the first plus the two declared retries.
    assert calls["n"] == 3


def test_malformed_all_three_times_surfaces_the_typed_fault():
    calls = {"n": 0}

    def make():
        calls["n"] += 1
        return _MALFORMED

    with pytest.raises(ResponseValidationError):
        validate_retry(make, 2, FLAGSHIP_SCHEMA, "Agent.run", CTORS)
    # the budget is a hard ceiling: 1 + 2 = 3 attempts, then a terminal fault.
    assert calls["n"] == 3


def test_retry_zero_is_one_terminal_attempt():
    calls = {"n": 0}

    def make():
        calls["n"] += 1
        return _MALFORMED

    with pytest.raises(ResponseValidationError):
        validate_retry(make, 0, FLAGSHIP_SCHEMA, "Agent.run", CTORS)
    assert calls["n"] == 1


def test_only_the_validation_fault_is_retried_not_a_transient():
    # §5.1: the read-with-a-cost retry is keyed on ONE fault kind. A completion
    # is not idempotent, so a TransientError (item 44's kind) is NOT retried by
    # this loop; it propagates on the first attempt.
    calls = {"n": 0}

    def make():
        calls["n"] += 1
        raise TransientError("dropped")

    with pytest.raises(TransientError):
        validate_retry(make, 2, FLAGSHIP_SCHEMA, "Agent.run", CTORS)
    assert calls["n"] == 1


def test_a_non_validation_host_error_is_never_retried():
    calls = {"n": 0}

    def make():
        calls["n"] += 1
        raise ValueError("real error")

    with pytest.raises(ValueError):
        validate_retry(make, 2, FLAGSHIP_SCHEMA, "Agent.run", CTORS)
    assert calls["n"] == 1


def test_async_retry_re_issues_a_fresh_coroutine_each_attempt():
    calls = {"n": 0}

    async def make():
        calls["n"] += 1
        return _MALFORMED if calls["n"] < 2 else _GOOD_TOOLS

    turn = asyncio.run(
        validate_retry_async(make, 1, FLAGSHIP_SCHEMA, "Agent.run", CTORS))
    assert isinstance(turn, ToolCalls)
    assert calls["n"] == 2


def test_async_budget_exhaustion_is_terminal():
    calls = {"n": 0}

    async def make():
        calls["n"] += 1
        return _MALFORMED

    with pytest.raises(ResponseValidationError):
        asyncio.run(
            validate_retry_async(make, 1, FLAGSHIP_SCHEMA, "Agent.run", CTORS))
    assert calls["n"] == 2


# --------------------------------------------------------------------------
# the N+1 cardinality (the 257-review HIGH fix)
# --------------------------------------------------------------------------

def test_validated_retry_two_reports_model_ceiling_three():
    """The HIGH fix: a `validated retry 2` model emission's crossing can fire up
    to 3 times per activation, so cardinality reports `model <= 3` (not `<= 1`),
    by the STATIC N+1 multiplier - exact and by construction."""
    card = cardinality(compile_source(
        _program(validated=True, retry=2), "t.rvl"))
    assert card["Agent"]["verdict"] == "bounded"
    assert card["Agent"]["per_capability"]["model"] == {
        "bound": 3, "kind": "bounded"}


def test_no_retry_validated_emission_reports_ceiling_one():
    """A `validated` emission with no `retry` clause is one attempt: `<= 1`."""
    card = cardinality(compile_source(
        _program(validated=True, retry=None), "t.rvl"))
    assert card["Agent"]["per_capability"]["model"] == {
        "bound": 1, "kind": "bounded"}


def test_cardinality_is_identical_with_and_without_the_feature_when_no_retry():
    """A non-validated twin and a validated-no-retry emission both count `<= 1`:
    the multiplier is 1 unless a positive `retry` is declared."""
    plain = cardinality(compile_source(
        _program(validated=False, retry=None), "t.rvl"))
    valid = cardinality(compile_source(
        _program(validated=True, retry=None), "t.rvl"))
    assert plain["Agent"]["per_capability"]["model"]["bound"] == 1
    assert valid["Agent"]["per_capability"]["model"]["bound"] == 1


def test_retry_multiplier_scales_with_n():
    for n, expected in ((1, 2), (3, 4), (5, 6)):
        card = cardinality(compile_source(
            _program(validated=True, retry=n), "t.rvl"))
        assert card["Agent"]["per_capability"]["model"]["bound"] == expected, n


# --------------------------------------------------------------------------
# the emit seam: the loop wraps ONLY the completion; downstream is outside it
# --------------------------------------------------------------------------

def test_emit_wraps_only_the_completion_call_in_the_retry_thunk():
    code = emit.emit(compile_source(_program(validated=True, retry=2), "t.rvl"))
    # the retry helper is imported and the completion call is the thunk body.
    assert "validate_retry as _revl_validate_retry" in code
    assert "_revl_validate_retry(lambda: _revl_ctx.model.complete(['p'])" in code
    # the budget rides the call site as the literal N.
    assert "_revl_ctx.model.complete(['p']), 2," in code


def test_downstream_effect_is_not_inside_the_retry_loop():
    """§5.3 / attack 3: the seam is at the forward crossing, so a downstream
    effect is a SEPARATE statement after the bind, textually OUTSIDE the retry
    thunk. A retry re-fires the completion and nothing downstream."""
    src = """
type Call = { tool: Str, args: Str }
type AgentTurn = Final(Str) | ToolCalls(List[Call])
service Model { emission[model] validated retry 2 fn complete(h: List[Str]) -> AgentTurn }
service Sink { emission[audit] fn note(s: Str) -> Int }
service Loop { emission fn run(p: Str) -> Int }
component Agent requires model: Model, sink: Sink provides agent: Loop {
  provide agent {
    fn run(session_id) {
      let t = emit model.complete(["p"])
      let logged = emit sink.note("after")
      return match t { Final(a) => logged, ToolCalls(c) => 2 }
    }
  }
}
"""
    code = emit.emit(compile_source(src, "t.rvl"))
    # the retry thunk names ONLY the model completion, never the downstream
    # `sink.note` crossing.
    thunk_line = next(l for l in code.splitlines()
                      if "_revl_validate_retry(lambda:" in l)
    assert "sink.note" not in thunk_line
    assert "model.complete" in thunk_line
    # and the downstream emit is its own separate rendered statement.
    assert any("sink.note('after')" in l and "_revl_validate_retry" not in l
               for l in code.splitlines())


def test_retry_that_fails_never_resumes_the_body_so_downstream_never_fires():
    """A retry that ultimately FAILS unwinds cleanly: the seam raises before the
    value binds, so the body never resumes, no downstream `emit` fires (no
    double-emit), and nothing is registered from the malformed response for
    teardown to revert (G5/G7 are not in play - the fault is at a forward
    crossing, not in unwind). Modeled at the seam: `make_call` is the ONLY thing
    in the loop; a downstream step runs only if the loop returns."""
    crossings = {"n": 0}
    downstream = {"n": 0}

    def make():
        crossings["n"] += 1
        return _MALFORMED

    def body():
        # the body resumes (and its downstream emit fires) ONLY after the seam
        # returns a validated value.
        turn = validate_retry(make, 2, FLAGSHIP_SCHEMA, "Agent.run", CTORS)
        downstream["n"] += 1
        return turn

    with pytest.raises(ResponseValidationError):
        body()
    # the crossing is honestly re-incurred per attempt (1 + 2 = 3), each a real
    # provider call, never one crossing replayed...
    assert crossings["n"] == 3
    # ...and the downstream effect fired ZERO times: the body never resumed.
    assert downstream["n"] == 0


def test_retry_that_succeeds_fires_downstream_exactly_once():
    crossings = {"n": 0}
    downstream = {"n": 0}

    def make():
        crossings["n"] += 1
        return _MALFORMED if crossings["n"] < 3 else _GOOD_FINAL

    def body():
        turn = validate_retry(make, 2, FLAGSHIP_SCHEMA, "Agent.run", CTORS)
        downstream["n"] += 1
        return turn

    turn = body()
    assert isinstance(turn, Final)
    assert crossings["n"] == 3       # three provider calls
    assert downstream["n"] == 1      # the downstream effect fires exactly once


# --------------------------------------------------------------------------
# the general (non-model) case: any validated emission gets the same treatment
# --------------------------------------------------------------------------

def test_non_model_validated_retry_gets_the_same_multiplier_and_loop():
    """§2's general rule: a NON-model `validated ... retry N` emission (here a
    `net` capability) gets the same N+1 cardinality multiplier and the same
    retry loop. Nothing about the mechanism is model-specific."""
    ir = compile_source(_program(validated=True, retry=2, cap="net"), "t.rvl")
    method = ir["services"]["Model"]["methods"]["complete"]
    assert method["validated"] is True and method["retry"] == 2
    card = cardinality(ir)
    assert card["Agent"]["per_capability"]["net"] == {"bound": 3,
                                                      "kind": "bounded"}
    code = emit.emit(ir)
    assert "_revl_validate_retry(lambda: _revl_ctx.model.complete(['p']), 2," in code


# --------------------------------------------------------------------------
# byte-identical: no-retry / non-validated is unchanged (IR + emit + cardinality)
# --------------------------------------------------------------------------

def test_no_retry_ir_has_no_retry_key():
    method = compile_source(
        _program(validated=True, retry=None), "t.rvl"
    )["services"]["Model"]["methods"]["complete"]
    assert "retry" not in method  # additive: absent unless a clause was written


def test_no_retry_validated_emit_is_the_slice1_seam_byte_identical():
    """A `validated` emission with no `retry` renders the Slice-1
    `_revl_validate(...)` seam verbatim - the retry loop appears only when a
    positive budget is declared."""
    no_retry = emit.emit(compile_source(
        _program(validated=True, retry=None), "t.rvl"))
    assert "_revl_validate(_revl_ctx.model.complete(['p'])" in no_retry
    assert "validate_retry" not in no_retry
    assert "_revl_validate_retry" not in no_retry


def test_non_validated_ir_and_emit_are_byte_identical_to_pre_feature():
    ir = compile_source(_program(validated=False, retry=None), "t.rvl")
    method = ir["services"]["Model"]["methods"]["complete"]
    assert "validated" not in method
    assert "retry" not in method
    assert "response_schema" not in method
    code = emit.emit(ir)
    assert "validate_response" not in code
    assert "validate_retry" not in code
    assert "model.complete(['p'])" in code


# --------------------------------------------------------------------------
# refusals: `retry` is legal only alongside `validated`, N a positive literal
# --------------------------------------------------------------------------

def test_retry_without_validated_is_refused_on_a_method():
    with pytest.raises(RevlError) as exc:
        compile_source(
            "service Model { emission[model] retry 2 fn c(h: List[Str]) -> Str }",
            "t.rvl")
    assert "not `validated`" in str(exc.value)


def test_retry_without_validated_is_refused_on_an_extern():
    with pytest.raises(RevlError) as exc:
        compile_source(
            "extern emission[model] retry 2 fn c(h: List[Str]) -> Str = @py { pass }",
            "t.rvl")
    assert "not `validated`" in str(exc.value)


def test_retry_zero_is_refused_positive_literal_required():
    with pytest.raises(RevlError) as exc:
        compile_source(
            "service Model { emission[model] validated retry 0 fn c(h: List[Str]) -> Str }",
            "t.rvl")
    assert "positive integer" in str(exc.value)


def test_negative_retry_is_refused():
    # `retry -1`: the `-` is not part of an int literal in the modifier slot, so
    # the parser refuses a non-integer budget.
    with pytest.raises(RevlError):
        compile_source(
            "service Model { emission[model] validated retry x fn c(h: List[Str]) -> Str }",
            "t.rvl")


# --------------------------------------------------------------------------
# the extern spelling carries the budget onto the IR crossing
# --------------------------------------------------------------------------

def test_extern_validated_retry_carries_the_budget():
    ir = compile_source("""
type Call = { tool: Str, args: Str }
type AgentTurn = Final(Str) | ToolCalls(List[Call])
extern emission[model] validated retry 3 async fn complete(ctx: List[Str]) -> AgentTurn
  = @py { pass }
""", "t.rvl")
    entry = next(e for e in ir["externs"] if e["name"] == "complete")
    assert entry["validated"] is True
    assert entry["retry"] == 3
    assert entry["async"] is True


def test_extern_no_retry_has_no_retry_key():
    ir = compile_source("""
type Call = { tool: Str, args: Str }
type AgentTurn = Final(Str) | ToolCalls(List[Call])
extern emission[model] validated fn complete(ctx: List[Str]) -> AgentTurn
  = @py { pass }
""", "t.rvl")
    entry = next(e for e in ir["externs"] if e["name"] == "complete")
    assert "retry" not in entry
