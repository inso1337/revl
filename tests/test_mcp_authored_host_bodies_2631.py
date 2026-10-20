"""Approval metadata attributes host bodies to the source that declared them.

An MCP composition includes operator-sanctioned provider modules as well as the
candidate sent by the agent. Those provider bodies are part of the merged IR,
but must not be described on the approval ticket as agent-authored host code
(issue #2631).
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from revl.mcp import server  # noqa: E402


_OPERATOR_PROVIDER = (
    "pub extern pure fn operator_helper(x: Str) -> Str = @py { return x }\n"
)

_CANDIDATE_MODULE = (
    "pub extern pure fn candidate_helper(x: Str) -> Str = @py { return x }\n"
)

_CANDIDATE = (
    'use "operator-provider-2631.rvl" { operator_helper }\n'
    'use "candidate-helper-2631.rvl" { candidate_helper }\n'
    "extern pure fn candidate_root(x: Str) -> Str = @py { return x }\n"
    "service S { fn f(x: Str) -> Str }\n"
    "component C provides s: S {\n"
    "  provide s { fn f(x) = operator_helper(candidate_helper(candidate_root(x))) }\n"
    "}\n"
)


@pytest.fixture(autouse=True)
def _trusted_server(monkeypatch, tmp_path):
    from revl.mcp.session import Session

    previous_authoring = server.AUTHORING
    previous_bodies = server._AUTHORED_HOST_BODIES
    monkeypatch.setattr(server, "SESSION", Session())
    monkeypatch.setattr(server, "_AUTHORED_HOST_BODIES", [])
    server.set_authoring_trust(
        host_code=True,
        granted=None,
        providers={os.path.abspath("operator-provider-2631.rvl"):
                   _OPERATOR_PROVIDER},
        roots=(str(tmp_path),),
    )
    yield
    server.AUTHORING = previous_authoring
    server._AUTHORED_HOST_BODIES = previous_bodies


def test_candidate_host_body_metadata_excludes_operator_provider_bodies():
    """The provider body remains in the composition but is not agent code."""
    candidate_module = os.path.abspath("candidate-helper-2631.rvl")

    ir = server._compile(_CANDIDATE, None,
                         {candidate_module: _CANDIDATE_MODULE})

    merged = {entry["name"] for entry in ir["externs"] if entry.get("bodies")}
    authored = {entry["extern"] for entry in server._AUTHORED_HOST_BODIES}
    assert merged == {"candidate_helper", "candidate_root", "operator_helper"}
    assert authored == {"candidate_helper", "candidate_root"}


def test_file_candidate_host_body_ticket_uses_compiled_source_paths(
        monkeypatch, tmp_path):
    """The load ticket includes the exported file, not operator files."""
    export_dir = tmp_path / ".revl" / "export"
    export_dir.mkdir(parents=True)
    candidate_file = export_dir / "candidate-export-2631.rvl"
    candidate_file.write_text(
        'pub extern pure fn candidate_file(x: Str) -> Str = '
        '@py { return x }\n',
        encoding="utf-8",
    )
    operator_file = tmp_path / "operator-file-2631.rvl"
    operator_file.write_text(
        'pub extern pure fn operator_file(x: Str) -> Str = '
        '@py { return x }\n',
        encoding="utf-8",
    )
    files = [str(candidate_file), str(operator_file)]
    report = {}
    # `revl mcp serve` disables the CLI export rule before compiling the source
    # it serves. Keep this test on that production path rather than treating the
    # export as an operator-authored CLI input.
    with server._export_jail.cli_rule(off=True):
        ir = server.compile_under_authoring(None, files, report=report)

    assert {entry["name"] for entry in ir["externs"] if entry.get("bodies")} == {
        "candidate_file",
        "operator_file",
        "operator_helper",
    }
    from revl.mcp.approval import ApprovalRequired

    ticket = {"hash": "sha256:file-test", "capabilities": []}

    def load_requires_approval(*_args, **_kwargs):
        error = ApprovalRequired(ticket)
        error.candidate = ir
        raise error

    monkeypatch.setattr(server.SESSION, "load", load_requires_approval)
    result = server._boot(ir, None, None, None, True, {}, files=files,
                          compile_report=report)

    assert result["approvalRequired"] is True
    ticket_bodies = result["ticket"]["unreviewedHostCode"]
    assert {entry["extern"] for entry in ticket_bodies} == {"candidate_file"}
    assert "not a bound on what the bodies do" in \
        result["ticket"]["unreviewedHostCodeWarning"]


def test_load_approval_ticket_names_only_candidate_host_bodies(monkeypatch):
    """The load path uses the same per-source report when it shapes a ticket."""
    from revl.mcp.approval import ApprovalRequired

    candidate_module = os.path.abspath("candidate-helper-2631.rvl")
    modules = {candidate_module: _CANDIDATE_MODULE}
    report = {}
    ir = server.compile_under_authoring(_CANDIDATE, None, modules=modules,
                                        report=report)
    ticket = {"hash": "sha256:test", "capabilities": []}

    def load_requires_approval(*_args, **_kwargs):
        error = ApprovalRequired(ticket)
        error.candidate = ir
        raise error

    monkeypatch.setattr(server.SESSION, "load", load_requires_approval)
    result = server._boot(ir, _CANDIDATE, modules, None, True, {},
                          compile_report=report)

    assert result["approvalRequired"] is True
    assert {entry["extern"] for entry in result["ticket"]["unreviewedHostCode"]} == {
        "candidate_helper",
        "candidate_root",
    }
    assert "not a bound on what the bodies do" in \
        result["ticket"]["unreviewedHostCodeWarning"]


def test_virtual_draft_boot_passes_host_source_provenance(monkeypatch):
    """A held draft's ticket receives the report from its virtual compile."""
    from revl.mcp import draft, edit
    from revl.mcp.approval import ApprovalRequired

    candidate_module = os.path.abspath("candidate-helper-2631.rvl")
    provider_module = os.path.abspath("operator-provider-2631.rvl")
    modules = {
        candidate_module: _CANDIDATE_MODULE,
        provider_module: _OPERATOR_PROVIDER,
    }
    report = {}
    working_set = {"source": _CANDIDATE, "modules": modules}
    ir = edit.compile_virtual(working_set, report=report)
    assert report["host_extern_sources"]

    ticket = {"hash": "sha256:draft-test", "capabilities": []}

    def load_requires_approval(*_args, **_kwargs):
        error = ApprovalRequired(ticket)
        error.candidate = ir
        raise error

    monkeypatch.setattr(server.SESSION, "load", load_requires_approval)
    server.SESSION.pending_draft = {
        "vs": working_set,
        "config": None,
        "record": True,
    }
    result = draft.boot_held(server.SESSION, {}, server._boot_draft)

    assert result["approvalRequired"] is True
    assert result["draft"] is True
    assert result["booted"] is False
    assert {entry["extern"] for entry in result["ticket"]["unreviewedHostCode"]} == {
        "candidate_helper",
        "candidate_root",
    }
    assert "not a bound on what the bodies do" in \
        result["ticket"]["unreviewedHostCodeWarning"]
