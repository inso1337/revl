"""Issue #2704: one derivation from a service operation to a tool contract.

A `validated` emission already derives a JSON Schema and a GBNF grammar from its
RETURN type (items 257/513). A tool offered to a model is the other direction:
a service operation with typed parameters, and what a model may ask the program
to do. This file pins the derivation added for it:

  * `revl grammar --tool Service.operation` writes the argument schema and the
    grammar rendered from that same schema, with a stable digest that changes
    when a parameter type changes;
  * the schema is the closed object the grammar's language describes: a
    well-formed call is accepted and an extra field is rejected;
  * the capability refinement refuses a call whose argument value falls outside
    the declared capability parameter, even though the grammar accepted the
    string (a grammar fixes syntax, not values).

The grammar recogniser is item 513's, imported rather than re-written, so the
tool form is judged by the same GBNF semantics as the return form.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from revl import compile_source  # noqa: E402
from revl import tool_schema  # noqa: E402
from runtime import ResponseValidationError, validate_response  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_RETURN_FORM = _load("decode_grammar_513_return_form",
                     ROOT / "tests" / "test_decode_grammar_513.py")
accepts = _RETURN_FORM.accepts


PLAIN = """
service Files {
  fn read(path: Str, mode: Int) -> Bool
}
"""

MAIL = """
capability mail.send(account: discrete, folder: path)
service Mail {
  emission[mail.send(account="ops", folder=folder)] fn send(to: Str, folder: Str, mode: Int) -> Bool
}
"""


def test_schema_is_a_closed_object_over_the_parameters():
    ir = compile_source(PLAIN, "tool.rvl")
    contract = tool_schema.tool_contract(ir, "Files", "read")
    schema = contract["schema"]
    assert schema == {
        "type": "object",
        "properties": {"path": {"type": "string"}, "mode": {"type": "integer"}},
        "required": ["path", "mode"],
        "additionalProperties": False,
    }


def test_grammar_accepts_a_well_formed_call_and_rejects_an_extra_field():
    ir = compile_source(PLAIN, "tool.rvl")
    grammar = tool_schema.tool_contract(ir, "Files", "read")["grammar"]["text"]
    assert accepts(grammar, '{"path": "/tmp/x", "mode": 3}')
    assert not accepts(grammar, '{"path": "/tmp/x", "mode": 3, "extra": 1}')
    assert not accepts(grammar, '{"mode": 3}')


def test_schema_and_grammar_reject_the_same_extra_field():
    ir = compile_source(PLAIN, "tool.rvl")
    schema = tool_schema.tool_contract(ir, "Files", "read")["schema"]
    good = {"path": "/tmp/x", "mode": 3}
    assert validate_response(good, schema) == good
    with pytest.raises(ResponseValidationError):
        validate_response({**good, "extra": 1}, schema)


def test_digest_is_stable_and_changes_when_a_parameter_type_changes():
    first = tool_schema.tool_contract(compile_source(PLAIN, "tool.rvl"),
                                     "Files", "read")
    second = tool_schema.tool_contract(compile_source(PLAIN, "tool.rvl"),
                                      "Files", "read")
    assert first["grammar"]["digest"] == second["grammar"]["digest"]
    assert first["schema_digest"] == second["schema_digest"]

    changed = PLAIN.replace("mode: Int", "mode: Str")
    other = tool_schema.tool_contract(compile_source(changed, "tool.rvl"),
                                      "Files", "read")
    assert other["grammar"]["digest"] != first["grammar"]["digest"]
    assert other["schema_digest"] != first["schema_digest"]


def test_unknown_operation_is_a_named_refusal():
    ir = compile_source(PLAIN, "tool.rvl")
    with pytest.raises(tool_schema.ToolSchemaError, match="no operation `write`"):
        tool_schema.tool_contract(ir, "Files", "write")
    with pytest.raises(tool_schema.ToolSchemaError, match="no service `Nope`"):
        tool_schema.tool_contract(ir, "Nope", "read")


def test_refinement_admits_a_call_inside_the_held_cone():
    ir = compile_source(MAIL, "mail.rvl")
    declared = tool_schema.operation_capabilities(ir, "Mail", "send")
    declarations = ir.get("capability_declarations")
    call = {"to": "ops@example.test", "folder": "/data/inbox", "mode": 1}
    held = ['mail.send(account="ops", folder="/data")']
    assert tool_schema.refine_arguments(declared, call, held, declarations) == []
    # a bare token covers every value of its cone
    assert tool_schema.refine_arguments(declared, call, ["mail.send"],
                                        declarations) == []


def test_refinement_refuses_a_value_outside_the_held_capability():
    """The grammar accepted both calls: `folder` is a Str either way. Only the
    refinement checks the VALUE against the capability's cone."""
    ir = compile_source(MAIL, "mail.rvl")
    declared = tool_schema.operation_capabilities(ir, "Mail", "send")
    declarations = ir.get("capability_declarations")
    grammar = tool_schema.tool_contract(ir, "Mail", "send")["grammar"]["text"]
    inside = '{"to": "t", "folder": "/data/inbox", "mode": 1}'
    outside = '{"to": "t", "folder": "/etc/passwd", "mode": 1}'
    assert accepts(grammar, inside) and accepts(grammar, outside)

    held = ['mail.send(account="ops", folder="/data")']
    violations = tool_schema.refine_arguments(
        declared, {"to": "t", "folder": "/etc/passwd", "mode": 1}, held,
        declarations)
    assert len(violations) == 1
    assert "not covered" in violations[0]
    assert "folder" in violations[0]
    assert "/etc/passwd" in violations[0]
    # no held capability at all is the same refusal, never a silent pass
    assert tool_schema.refine_arguments(
        declared, {"to": "t", "folder": "/data/inbox", "mode": 1}, [],
        declarations)


def test_refinement_refuses_a_binding_the_call_does_not_supply():
    ir = compile_source(MAIL, "mail.rvl")
    declared = tool_schema.operation_capabilities(ir, "Mail", "send")
    with pytest.raises(tool_schema.ToolSchemaError, match="does not supply"):
        tool_schema.refine_arguments(
            declared, {"to": "t", "mode": 1},
            ['mail.send(account="ops", folder="/data")'],
            ir.get("capability_declarations"))


def test_cli_writes_the_contract(tmp_path):
    source = tmp_path / "mail.rvl"
    source.write_text(MAIL)
    result = subprocess.run(
        [sys.executable, "-m", "revl", "grammar", "--tool", "Mail.send",
         str(source)],
        capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stderr
    contract = json.loads(result.stdout)
    assert contract["service"] == "Mail"
    assert contract["operation"] == "send"
    assert contract["schema"]["additionalProperties"] is False
    assert contract["grammar"]["format"] == "gbnf"
    assert contract["grammar"]["digest"]
    assert contract["json_schema"]["format"] == "json-schema"


def test_cli_refuses_a_tool_without_sources_or_with_a_bad_name(tmp_path):
    no_sources = subprocess.run(
        [sys.executable, "-m", "revl", "grammar", "--tool", "Mail.send"],
        capture_output=True, text=True, cwd=ROOT)
    assert no_sources.returncode == 2
    assert "needs the sources" in no_sources.stderr

    source = tmp_path / "mail.rvl"
    source.write_text(MAIL)
    bad = subprocess.run(
        [sys.executable, "-m", "revl", "grammar", "--tool", "Mail.nope",
         str(source)],
        capture_output=True, text=True, cwd=ROOT)
    assert bad.returncode == 2
    assert "no operation `nope`" in bad.stderr
