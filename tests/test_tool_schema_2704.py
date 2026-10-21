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
  * the contract reports the operation's DECLARED EFFECT, `emission` and the
    capability spellings of a scoped emission, so a host reading it can tell a
    pure read from one that fires authority;
  * the schema's `"integer"` is unbounded JSON, which the grammar accepts and
    which the i64 tiers cannot carry. That divergence is PRE-EXISTING (the
    return boundary's schema has it too) and is pinned here so the tool form
    cannot be read as a cross-tier range guarantee.

The contract is data: no value refinement is derived here, because a value
check needs the caller's held capability set and lives at the host's call
boundary, not in the compiler (see the module docstring).

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


BARE = """
service Notify {
  emission fn send(to: Str) -> Bool
}
"""


def test_contract_reports_the_declared_effect():
    """A host reading the contract must be able to tell a pure read from one
    that fires authority, and a scoped emission from a bare one. The refinement
    was removed because it checked only a declaration-local capability list and
    had no call boundary; the declaration itself is still reported."""
    pure = tool_schema.tool_contract(compile_source(PLAIN, "tool.rvl"),
                                     "Files", "read")
    assert pure["emission"] is False
    assert pure["capabilities"] == []

    scoped_ir = compile_source(MAIL, "mail.rvl")
    scoped = tool_schema.tool_contract(scoped_ir, "Mail", "send")
    assert scoped["emission"] is True
    assert scoped["capabilities"] == \
        tool_schema.operation_capabilities(scoped_ir, "Mail", "send")
    assert any("mail.send" in cap for cap in scoped["capabilities"])

    bare = tool_schema.tool_contract(compile_source(BARE, "notify.rvl"),
                                     "Notify", "send")
    assert bare["emission"] is True
    # "any capability" is the absence of a scope, reported as empty rather
    # than dropped from the contract
    assert bare["capabilities"] == []


def test_schema_and_grammar_accept_an_unbounded_integer():
    """PRE-EXISTING divergence, pinned so the tool form does not read as a
    cross-tier range guarantee: JSON `integer` is unbounded, so 2^63 is a
    well-formed argument here while go/java/rust carry an i64. The return
    boundary's schema has the same shape."""
    ir = compile_source(PLAIN, "tool.rvl")
    contract = tool_schema.tool_contract(ir, "Files", "read")
    assert contract["schema"]["properties"]["mode"] == {"type": "integer"}
    assert accepts(contract["grammar"]["text"],
                   '{"path": "/tmp/x", "mode": 9223372036854775808}')


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
    assert contract["emission"] is True
    assert any("mail.send" in cap for cap in contract["capabilities"])


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
