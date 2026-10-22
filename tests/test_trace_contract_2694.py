"""Issue #2694: the model crossing records the contract it was held to.

A `validated` emission is checked against a schema derived from its return
type and, since item 513, constrained by a decoding grammar whose digest the
compiler pins; the role it names also carries the declared device placement.
The trace recorded the model id, tokens, cost, latency, attempts, downstream
writes and the verifying G-rule, but none of the three values that say WHICH
contract the completion was held to, so a reviewer could not tell after the
fact. This adds `schemaDigest`, `grammarDigest` and `residence` to the model
hop, each tagged with the record's existing provenance vocabulary.

Where the values come from, stated because the labels are not proof:

* `schemaDigest` is a sha256 over the canonical bytes of the crossing's IR
  `response_schema` (revl-controlled: the compiler derived it, the host never
  rewrites it).
* `grammarDigest` is copied verbatim from the IR's `response_grammar.digest`,
  the value item 513 pinned over the grammar text.
* `residence` is read off the IR's `manifest.model_reach` row for the role the
  crossing's `model.<role>` capability token names (revl-controlled: it is a
  source declaration, not a host claim). A crossing with no role, or no
  grammar, omits the field rather than writing an empty value.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

import runtime as rt  # noqa: E402
from revl import run  # noqa: E402
from revl import trace as tr  # noqa: E402
from revl import why_runtime as wr  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.decode_grammar import schema_digest  # noqa: E402

ROLE_PROGRAM = """
model role local on_device reaches []
type AgentTurn = Final(Str) | ToolCalls(List[Call])
type Call = { tool: Str, args: Str }
service Model { emission[model.local] validated fn complete(h: Str) -> AgentTurn }
service Loop { emission fn run(p: Str) -> Str }
component LoopComp requires model: Model provides work: Loop {
  provide work { fn run(session_id) { let r = emit model.complete("x") return "ok" } }
}
"""

# The same validated crossing with no `model.<role>` capability token: it still
# carries a schema and a grammar, and no residence.
PLAIN_PROGRAM = """
type AgentTurn = Final(Str) | ToolCalls(List[Call])
type Call = { tool: Str, args: Str }
service Model { emission validated fn complete(h: Str) -> AgentTurn }
service Loop { emission fn run(p: Str) -> Str }
component LoopComp requires model: Model provides work: Loop {
  provide work { fn run(session_id) { let r = emit model.complete("x") return "ok" } }
}
"""


@pytest.fixture(autouse=True)
def _fresh_run_state():
    rt.revl_reset_run_trace_state()
    yield
    rt.revl_reset_run_trace_state()


def _driver(ir):
    driver = run._Driver.__new__(run._Driver)
    driver.runtime = rt
    driver.ir = ir
    driver._events = []
    driver._seq = 0
    driver.generation = 1
    return driver


def _hop(driver, *, component="LoopComp", service="Model", method="complete"):
    """A recorded model hop: run the real item-257 seam so the crossing has an
    observation, resolve the contract from the driver's IR, assemble the hop."""
    rt.validate_retry(lambda: {"tag": "Final", "value": "ok"}, budget=0,
                      schema={}, where="Model")
    contract = driver._crossing_contract(component, service, method)
    return driver._model_crossing_payload(args=["prompt"], contract=contract)


def test_schema_digest_matches_the_ir_schema():
    """The recorded `schemaDigest` is the digest of the IR value for THIS
    crossing, not a second derivation and not a host claim."""
    ir = compile_source(ROLE_PROGRAM, "role.rvl")
    op = ir["services"]["Model"]["methods"]["complete"]
    hop = _hop(_driver(ir))
    assert hop["schemaDigest"] == {
        "digest": schema_digest(op["response_schema"]),
        "provenance": "revl-controlled",
    }
    # deterministic: the same schema bytes always give the same digest
    assert schema_digest(json.loads(json.dumps(op["response_schema"]))) \
        == hop["schemaDigest"]["digest"]


def test_grammar_digest_equals_the_one_the_compiler_pinned():
    ir = compile_source(ROLE_PROGRAM, "role.rvl")
    op = ir["services"]["Model"]["methods"]["complete"]
    hop = _hop(_driver(ir))
    assert hop["grammarDigest"] == {
        "digest": op["response_grammar"]["digest"],
        "provenance": "revl-controlled",
    }


def test_a_crossing_with_no_grammar_omits_the_field():
    """Absent, never an empty value: an unvalidated crossing has no response
    grammar, and the payload says so by omission."""
    ir = compile_source(ROLE_PROGRAM, "role.rvl")
    driver = _driver(ir)
    assert driver._crossing_contract("LoopComp", "Loop", "run") == {}
    hop = _hop(driver, component="LoopComp", service="Loop", method="run")
    assert "grammarDigest" not in hop
    assert "schemaDigest" not in hop
    # and the assembler itself never fabricates an empty digest
    bare = rt.revl_model_hop(model="m", tokens_in=1, tokens_out=1, cost=None,
                             latency_seconds=0.1, attempts=1, attempt_ceiling=1,
                             verified_by=[])
    assert "grammarDigest" not in bare and "schemaDigest" not in bare


def test_residence_is_the_roles_declared_placement():
    ir = compile_source(ROLE_PROGRAM, "role.rvl")
    hop = _hop(_driver(ir))
    assert hop["residence"] == {"value": "on_device",
                                "provenance": "revl-controlled"}


def test_a_crossing_with_no_role_omits_residence():
    ir = compile_source(PLAIN_PROGRAM, "plain.rvl")
    op = ir["services"]["Model"]["methods"]["complete"]
    hop = _hop(_driver(ir))
    assert "residence" not in hop
    # the schema and grammar are still recorded: the role is the only absent
    # fact here, so this cannot pass by omitting the whole contract
    assert hop["schemaDigest"]["digest"] == schema_digest(op["response_schema"])
    assert hop["grammarDigest"]["digest"] == op["response_grammar"]["digest"]


def test_the_reader_projects_the_contract_fields():
    """`revl trace --json` surfaces the three fields with their provenance, so
    the reviewer sees them without decoding the raw record."""
    ir = compile_source(ROLE_PROGRAM, "role.rvl")
    driver = _driver(ir)
    hop = _hop(driver)
    driver._record_emit("LoopComp", "model", "Model.complete",
                        wr.cause_trigger("decision point"), llm=hop)
    doc = tr.compute_trace(driver._events)
    got = doc["modelHops"][0]
    assert got["schemaDigest"]["provenance"] == "revl-controlled"
    assert got["grammarDigest"]["digest"] == hop["grammarDigest"]["digest"]
    assert got["residence"] == {"value": "on_device",
                                "provenance": "revl-controlled"}


def test_the_trace_schema_version_is_bumped():
    """The new fields ride a version bump: a v2-only reader is told the record
    it is looking at is v3, rather than reading new keys as if v2 defined
    them."""
    assert wr.SCHEMA_VERSION == 3
    ev = wr.make_emit_event(0, 1, "C", "model", "Model.complete",
                            wr.cause_trigger("crossed"))
    assert ev["v"] == 3
