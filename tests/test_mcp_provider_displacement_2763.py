"""Issue #2763: an agent-supplied module may not take the place of an operator
`--provider` module in the MCP composition.

`_compile_candidate` keeps the operator's providers under the agent's own
`modules`, but it compared the two maps by their raw keys while the compile
reads a map keyed by absolute path. A provider registered as `prov.rvl` and an
agent module sent as `/cwd/prov.rvl` (or `./prov.rvl`, or a symlink to the
file) were different keys and the same file, so the agent's text replaced the
provider's in the compile. Now a module that resolves to a provider's file is
refused, with the provider's path in the reason, and nothing compiles.

Every test here compiles only; none needs the cordis-py runtime.
"""

from __future__ import annotations

import os

import pytest

from revl.diagnostics import classify
from revl.errors import RevlError
from revl.mcp import server
from revl.mcp.session import Session

DECLS = "service Tool { fn describe() -> Str }\n"
# the operator's host code, registered through `--provider`
PROVIDER = ("service Ops { emission[notify] fn stash(p: Str) }\n"
            "extern emission[notify] fn notify(p: Str) = @py { return }\n"
            "component OpsProvider provides ops: Ops {\n"
            "  provide ops { fn stash(p) { emit notify(p) } }\n"
            "}\n")
# the agent's text for the same component, aimed at the provider's file: a
# drop-in that compiles in the provider's place (same service key, no host
# code), so the only thing standing between it and the composition is the
# refusal under test
HIJACK = ("service Ops { fn stash(p: Str) -> Str }\n"
          "component OpsProvider provides ops: Ops {\n"
          '  provide ops { fn stash(p) = "mine" }\n'
          "}\n")
CANDIDATE = (DECLS + "component T provides tool: Tool {\n"
             '  provide tool { fn describe() = "t" }\n'
             "}\n")
# an agent module of its own, used by a candidate through its absolute path
LIB = 'pub fn helper() -> Str { return "h" }\n'


@pytest.fixture(autouse=True)
def _provider_registered_by_relative_path(monkeypatch, tmp_path):
    """The operator starts the server in `tmp_path` and registers the provider
    the way `revl mcp serve --provider prov.rvl` stores it: by the path as
    typed, relative to the cwd. Restored afterwards."""
    before = server.AUTHORING
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server, "SESSION", Session())
    (tmp_path / "prov.rvl").write_text(PROVIDER, encoding="utf-8")
    server.set_authoring_trust(host_code=False, granted=None,
                               providers={"prov.rvl": PROVIDER},
                               roots=(str(tmp_path),))
    yield
    server.AUTHORING = before


def _compile(modules: dict) -> dict:
    return server.compile_under_authoring(CANDIDATE, None, modules=modules)


def _handle_tool(name: str, arguments: dict, request_id: int) -> dict:
    response = server.handle({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    return response["result"]["structuredContent"]


@pytest.mark.parametrize("spelling", [
    lambda d: str(d / "prov.rvl"),
    lambda d: os.path.join(".", "prov.rvl"),
], ids=["absolute", "dot-slash"])
def test_a_module_at_the_providers_file_is_refused(tmp_path, spelling):
    key = spelling(tmp_path)
    with pytest.raises(RevlError) as refused:
        _compile({key: HIJACK})
    message = refused.value.message
    assert "provider" in message, message
    assert str(tmp_path / "prov.rvl") in message, message
    assert key in message, message


def test_the_refusal_compiles_nothing_with_the_agents_text(tmp_path):
    """The refusal is the only outcome: no composition exists in which the
    agent's `OpsProvider` stands in for the provider. The refusal comes before
    any compile, so the report check below documents that rather than adding a
    case over the absolute-path test."""
    report: dict = {}
    with pytest.raises(RevlError):
        server.compile_under_authoring(
            CANDIDATE, None, modules={str(tmp_path / "prov.rvl"): HIJACK},
            report=report)
    assert "dependencies" not in report or not any(
        "mine" in text for text in report["dependencies"].values()), report


def test_a_symlink_to_the_providers_file_is_refused(tmp_path):
    link = tmp_path / "other.rvl"
    link.symlink_to(tmp_path / "prov.rvl")
    with pytest.raises(RevlError) as refused:
        _compile({"other.rvl": HIJACK})
    assert str(tmp_path / "prov.rvl") in refused.value.message


def test_the_same_spelling_is_refused_too(tmp_path):
    """Before, an entry at the provider's own key was dropped silently; the
    rule is now one refusal for every spelling of the file."""
    with pytest.raises(RevlError) as refused:
        _compile({"prov.rvl": HIJACK})
    assert str(tmp_path / "prov.rvl") in refused.value.message


def test_an_agent_module_of_its_own_still_compiles_by_absolute_path(tmp_path):
    """Positive control: the absolute-path spelling is refused only when it
    names a provider's file."""
    candidate = (DECLS + f'use "{tmp_path / "lib.rvl"}" {{ helper }}\n'
                 "component T provides tool: Tool {\n"
                 "  provide tool { fn describe() = helper() }\n"
                 "}\n")
    ir = server.compile_under_authoring(
        candidate, None, modules={str(tmp_path / "lib.rvl"): LIB})
    by_name = {c["name"]: c for c in ir["manifest"]["components"]}
    assert by_name["OpsProvider"]["provides"] == ["ops"], by_name
    assert by_name["T"]["provides"] == ["tool"], by_name


def test_the_provider_is_still_composed_under_its_registered_path():
    """Positive control: the operator's provider rides in the composition
    compile as before."""
    ir = _compile({})
    by_name = {c["name"]: c for c in ir["manifest"]["components"]}
    assert by_name["OpsProvider"]["provides"] == ["ops"], by_name


def test_target_derivation_refuses_the_same_module(tmp_path):
    """The operator module's target derivation composes the providers with the
    agent's modules the way the handler's compile does, and refuses the same
    module: a candidate that names a provider's file derives no target set
    (fail closed) instead of deriving one from the agent's text."""
    from revl.mcp import operator

    arguments = {"source": CANDIDATE,
                 "modules": {str(tmp_path / "prov.rvl"): HIJACK}}
    assert operator._compile_candidate(arguments) is None
    # positive control: without the displacing module the derivation runs
    # (this site makes the providers available to `use`, it does not compose
    # them as co-roots, so the candidate's own component is what it derives)
    ir = operator._compile_candidate({"source": CANDIDATE, "modules": {}})
    by_name = {c["name"]: c for c in ir["manifest"]["components"]}
    assert by_name["T"]["provides"] == ["tool"], by_name


def test_a_nul_byte_in_a_module_key_is_a_named_admission_refusal():
    """An invalid path-like module key is refused before realpath can leak an
    internal ValueError through the agent-facing compile diagnostic."""
    with pytest.raises(RevlError) as refused:
        _compile({"bad\0module.rvl": HIJACK})

    record = classify(refused.value)
    assert record["code"] == "REVL", record
    assert record["category"] == "admission", record
    assert "NUL" in record["message"], record


@pytest.mark.parametrize(("tool", "arguments"), [
    ("revl_check", {"files": ["bad\0module.rvl"]}),
    ("revl_check", {"source": CANDIDATE,
                    "modules": {"bad\0module.rvl": LIB}}),
    ("revl_export", {"path": "bad\0module.rvl"}),
    ("revl_restore", {"snapshot": {"sources": {
        "files_content": {"bad\0module.rvl": ""}}}}),
    ("revl_restore", {"snapshot": {"sources": {
        "dependencies": {"bad\0module.rvl": ""}}}}),
    ("revl_edit", {"target": "bad\0module.rvl", "edits": []}),
    ("revl_edit", {"edits": [{"target": "bad\0module.rvl",
                                "anchor": "before", "replacement": "after"}]}),
    ("revl_change", {"edit": {"target": "bad\0module.rvl", "edits": []}}),
    ("revl_change", {"add": {"target": "bad\0module.rvl",
                               "source": CANDIDATE}}),
], ids=["files", "modules", "export-path", "restore-files-content",
        "restore-dependencies", "edit-target", "edit-entry-target",
        "change-edit-target", "change-add-target"])
def test_transport_nul_paths_are_refused_and_the_server_keeps_serving(
    tool, arguments,
):
    refused = _handle_tool(tool, arguments, request_id=1)

    assert refused["ok"] is False, refused
    diagnostic = refused["diagnostics"][0]
    assert diagnostic["category"] == "admission", diagnostic
    assert "NUL" in diagnostic["message"], diagnostic

    answered = _handle_tool("revl_check", {"source": CANDIDATE}, request_id=2)
    assert answered["ok"] is True, answered
