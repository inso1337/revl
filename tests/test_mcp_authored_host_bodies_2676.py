"""Approval tickets retain candidate host-body provenance on every route.

Issue #2676 covers the source-changing handlers and live call/act tickets that
were not covered by the initial load/swap regression tests.
"""

import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from revl.mcp import edit, repair, server  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="loading and applying a live composition needs cordis-py "
           "(`sh backends/python/setup.sh`, run under backends/python/.venv)")


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
    previous_live_bodies = server._LIVE_HOST_BODIES
    monkeypatch.setattr(server, "SESSION", Session())
    monkeypatch.setattr(server, "_AUTHORED_HOST_BODIES", [])
    monkeypatch.setattr(server, "_LIVE_HOST_BODIES", [])
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
    server._LIVE_HOST_BODIES = previous_live_bodies


def _compiled_candidate():
    modules = {
        os.path.abspath("candidate-helper-2631.rvl"): _CANDIDATE_MODULE,
    }
    report = {}
    ir = server.compile_under_authoring(_CANDIDATE, None,
                                        modules=modules, report=report)
    authored = server._authored_host_bodies(
        ir, _CANDIDATE, modules, report=report)
    return ir, modules, report, authored


def _body_names(bodies):
    return {entry["extern"] for entry in bodies}


def test_live_call_and_act_tickets_use_only_the_running_authored_set(monkeypatch):
    from revl.mcp.approval import ApprovalRequired

    ir, _modules, _report, authored = _compiled_candidate()
    server.SESSION.ir = ir
    server.SESSION.origin = {}
    server._with_authored_host_bodies(server.SESSION.origin, authored)
    server._remember_live_host_bodies()

    def refused(*_args, **_kwargs):
        error = ApprovalRequired({"hash": "sha256:live", "capabilities": []})
        raise error

    monkeypatch.setattr(server.SESSION, "call", refused)
    call_result = server._run_handler(
        "revl_call", {"key": "s", "method": "f", "args": ["x"]})
    assert _body_names(call_result["ticket"]["unreviewedHostCode"]) == {
        "candidate_helper", "candidate_root",
    }

    monkeypatch.setattr(server.SESSION, "act", refused)
    act_result = server._tool_act(
        {"key": "s", "method": "f", "args": ["x"]})
    assert act_result["outcome"] == "ticket"
    assert _body_names(act_result["ticket"]["unreviewedHostCode"]) == {
        "candidate_helper", "candidate_root",
    }


def test_server_side_swap_ticket_uses_the_compile_report(monkeypatch):
    from revl.mcp.approval import ApprovalRequired

    ir, modules, report, authored = _compiled_candidate()
    working = {"source": _CANDIDATE, "modules": modules}
    fake = SimpleNamespace(ir=ir, origin=working, loaded=True)

    def compile_virtual(_vs, *, report=None, **_kwargs):
        if report is not None:
            report.update(report_data)
        return ir

    report_data = dict(report)

    def swap(candidate, **_kwargs):
        error = ApprovalRequired({"hash": "sha256:server-swap",
                                  "capabilities": []})
        error.candidate = candidate
        raise error

    fake.swap = swap
    monkeypatch.setattr(server, "SESSION", fake)
    monkeypatch.setattr(edit, "compile_virtual", compile_virtual)
    monkeypatch.setattr(server, "_realm_removal_refused", lambda *_args: None)
    monkeypatch.setattr(server._authoring_loop, "blast_radius", lambda *_args: {})

    result = server._swap_server_side(())
    assert _body_names(result["ticket"]["unreviewedHostCode"]) == {
        "candidate_helper", "candidate_root",
    }
    assert _body_names(authored) == {"candidate_helper", "candidate_root"}


def test_edit_refusal_ticket_carries_the_exact_candidate_bodies(monkeypatch):
    from revl.mcp.approval import ApprovalRequired

    ir, modules, _report, _authored = _compiled_candidate()
    fake = SimpleNamespace(
        loaded=True,
        ir=ir,
        origin={"source": _CANDIDATE, "modules": modules},
        draft=None,
    )

    def swap(candidate, **_kwargs):
        error = ApprovalRequired({"hash": "sha256:edit", "capabilities": []})
        error.candidate = candidate
        raise error

    fake.swap = swap
    monkeypatch.setattr(server._leases, "check_swap", lambda *_args: None)
    monkeypatch.setattr(server._quarantine, "gate_swap", lambda *_args: None)
    try:
        edit_result = edit.apply_edit(fake, {"edits": [{
            "range": [len(_CANDIDATE), len(_CANDIDATE)],
            "replacement": "// edited\n",
        }]})
    except ApprovalRequired as error:
        caught = error
    else:
        pytest.fail(f"edit did not reach the swap approval gate: {edit_result!r}")

    # The ticket, not the exception attribute, is what the caller receives:
    # the shared handler recovers the candidate's bodies when the edit path
    # attaches them to the exception.
    ticket = server._approval_required(caught)["ticket"]
    assert _body_names(ticket["unreviewedHostCode"]) == {
        "candidate_helper", "candidate_root",
    }
    assert "operator_helper" not in _body_names(ticket["unreviewedHostCode"])


def test_repair_gate_carries_the_candidate_bodies(monkeypatch):
    from revl.mcp import gauntlet

    ir, modules, _report, _authored = _compiled_candidate()
    monkeypatch.setattr(gauntlet, "run", lambda *_args, **_kwargs: {
        "verdict": "admissible",
    })
    monkeypatch.setattr(repair, "_realm_removal_refusal", lambda *_args: None)
    monkeypatch.setattr(repair, "_load_boundary_policy", lambda *_args: None)
    monkeypatch.setattr(repair, "_capabilities_reached", lambda *_args: [])

    _verdicts, gate = repair._run_gates(
        SimpleNamespace(ir=ir), {"source": _CANDIDATE, "modules": modules},
        ir, repair.SelfRepairPolicy(ack_on_widen=False), set())
    assert _body_names(gate["_approval_host_bodies"]) == {
        "candidate_helper", "candidate_root",
    }


def test_restore_stores_provenance_on_the_live_generation_and_history(
        monkeypatch):
    from revl.mcp.persist import _recompile, admitted_name

    _ir, modules, _report, _authored = _compiled_candidate()
    # Snapshots carry operator provider text as a module only so this test can
    # exercise the compiler's per-source report; the attribution still excludes
    # the configured provider path.
    modules = {**modules,
               os.path.abspath("operator-provider-2631.rvl"):
                   _OPERATOR_PROVIDER}
    origin = {"source": _CANDIDATE, "modules": modules}

    def restore(_snap, *, profile=None, report=None):
        ir = _recompile(origin, admitted_name(_snap), profile=profile,
                        report=report)
        server.SESSION.ir = ir
        server.SESSION.origin = dict(origin)
        server.SESSION._history = [{"origin": server.SESSION.origin}]
        return {"restored": True}

    monkeypatch.setattr(server.SESSION, "restore", restore)
    monkeypatch.setattr(server._leases, "check", lambda *_args: None)
    result = server._tool_restore({"snapshot": {"sources": origin}})

    assert result["restored"] is True
    expected = {"candidate_helper", "candidate_root"}
    assert _body_names(server.SESSION.origin[
        server._ORIGIN_AUTHORED_HOST_BODIES]) == expected
    assert server.SESSION._history[-1]["origin"] is server.SESSION.origin
    from revl.mcp.persist import _materialize
    assert server._ORIGIN_AUTHORED_HOST_BODIES not in _materialize(
        server.SESSION._history[-1]["origin"])


def test_restore_approval_ticket_uses_the_snapshot_compile_report(monkeypatch):
    from revl.mcp.approval import ApprovalRequired
    from revl.mcp.persist import _recompile, admitted_name

    _ir, modules, _report, _authored = _compiled_candidate()
    modules = {**modules,
               os.path.abspath("operator-provider-2631.rvl"):
                   _OPERATOR_PROVIDER}
    sources = {"source": _CANDIDATE, "modules": modules}

    def restore(snap, *, profile=None, report=None):
        ir = _recompile(sources, admitted_name(snap), profile=profile,
                        report=report)
        error = ApprovalRequired({"hash": "sha256:restore",
                                  "capabilities": []})
        error.candidate = ir
        raise error

    monkeypatch.setattr(server.SESSION, "restore", restore)
    monkeypatch.setattr(server._leases, "check", lambda *_args: None)
    result = server._tool_restore({"snapshot": {"sources": sources}})

    assert _body_names(result["ticket"]["unreviewedHostCode"]) == {
        "candidate_helper", "candidate_root",
    }


def test_rollback_and_undo_refusals_use_the_target_generation_set(monkeypatch):
    from revl.mcp.approval import ApprovalRequired

    ir, _modules, _report, authored = _compiled_candidate()
    previous = {server._ORIGIN_AUTHORED_HOST_BODIES: authored}
    server.SESSION.previous_origin = previous

    def rollback(*, profile=None):
        error = ApprovalRequired({"hash": "sha256:rollback",
                                  "capabilities": []})
        error.candidate = ir
        raise error

    monkeypatch.setattr(server.SESSION, "rollback", rollback)
    rollback_result = server._tool_rollback({})
    assert _body_names(rollback_result["ticket"]["unreviewedHostCode"]) == {
        "candidate_helper", "candidate_root",
    }

    def undo(_to=None, *, profile=None):
        error = ApprovalRequired({"hash": "sha256:undo", "capabilities": []})
        error.candidate = ir
        error.host_bodies = authored
        raise error

    fake = SimpleNamespace(loaded=True, ir=ir, undo=undo)
    monkeypatch.setattr(server, "SESSION", fake)
    undo_result = server._tool_undo({})
    assert _body_names(undo_result["ticket"]["unreviewedHostCode"]) == {
        "candidate_helper", "candidate_root",
    }


def test_portable_snapshots_do_not_serialize_the_in_memory_provenance():
    from revl.mcp.persist import _materialize

    origin = server._with_authored_host_bodies(
        {"source": _CANDIDATE},
        [{"extern": "candidate_root", "classification": "pure",
          "backends": ["py"]}],
    )
    assert _materialize(origin) == {"source": _CANDIDATE}


@pytest.mark.parametrize("host_code", [False, True], ids=["untrusted", "trusted"])
def test_files_only_load_ticket_matches_host_code_trust(
        monkeypatch, tmp_path, host_code):
    """A files-only host body outside the export area follows the trust mode."""
    from revl.mcp.approval import ApprovalRequired

    path = tmp_path / "operator-source-2676.rvl"
    path.write_text(
        "pub extern pure fn trusted_file_2676(x: Str) -> Str = "
        "@py { return x }\n",
        encoding="utf-8",
    )
    files = [str(path)]
    assert not server._export_jail.in_export_area(str(path))
    server.set_authoring_trust(host_code=host_code, roots=(str(tmp_path),))
    report = {}
    ir = server.compile_under_authoring(None, files, report=report)

    def load_requires_approval(*_args, **_kwargs):
        raise ApprovalRequired({"hash": "sha256:load-2676", "capabilities": []})

    monkeypatch.setattr(server.SESSION, "load", load_requires_approval)
    result = server._boot(
        ir, None, None, {}, True, {"files": files},
        files=files, compile_report=report)

    assert result["approvalRequired"] is True
    names = {entry["extern"] for entry in
             result["ticket"].get("unreviewedHostCode", [])}
    assert names == ({"trusted_file_2676"} if host_code else set())


def test_fork_and_apply_keep_live_authored_host_bodies(monkeypatch):
    """Fork restore and apply-generation history retain disclosure metadata."""
    from revl.mcp.approval import ApprovalRequired
    from revl.mcp.session import Session

    ir, _modules, _report, authored = _compiled_candidate()
    parent = Session()
    parent.origin = server._with_authored_host_bodies(
        {"source": _CANDIDATE}, authored)

    branch = Session()
    branch.ir = ir
    branch.origin = {"source": _CANDIDATE}
    branch._history = [{"generation": 1, "origin": branch.origin}]
    branch._generation = 1
    monkeypatch.setattr(parent, "fork_confirm", lambda _hash: {
        "forked": True, "branchSession": branch,
    })
    monkeypatch.setattr(server, "SESSION", parent)

    forked = server._run_handler("revl_fork_confirm", {"hash": "sha256:fork"})
    assert forked["forked"] is True
    assert server.SESSION is branch
    expected = {"candidate_helper", "candidate_root"}
    assert _body_names(branch.origin[server._ORIGIN_AUTHORED_HOST_BODIES]) == expected

    def refused(*_args, **_kwargs):
        raise ApprovalRequired({"hash": "sha256:branch-call", "capabilities": []})

    monkeypatch.setattr(branch, "call", refused)
    call_result = server._run_handler(
        "revl_call", {"key": "s", "method": "f", "args": ["x"]})
    assert _body_names(call_result["ticket"]["unreviewedHostCode"]) == expected


def test_fork_confirm_ticket_uses_the_parent_generation_bodies(monkeypatch):
    """A branch restore that refuses carries the parent's disclosed set, not
    an empty candidate ticket (the fork path has no compile report of its own)."""
    from revl.mcp.approval import ApprovalRequired
    from revl.mcp.session import Session

    ir, _modules, _report, authored = _compiled_candidate()
    parent = Session()
    parent.ir = ir
    parent.origin = server._with_authored_host_bodies(
        {"source": _CANDIDATE}, authored)

    def fork_confirm(_hash):
        error = ApprovalRequired({"hash": "sha256:fork-approval",
                                  "capabilities": []})
        error.candidate = ir
        raise error

    monkeypatch.setattr(parent, "fork_confirm", fork_confirm)
    monkeypatch.setattr(server, "SESSION", parent)
    result = server._run_handler("revl_fork_confirm", {"hash": "sha256:fork"})
    assert result["approvalRequired"] is True, result
    assert _body_names(result["ticket"]["unreviewedHostCode"]) == {
        "candidate_helper", "candidate_root",
    }


_APPLY_RUNNING = (
    "service Ping { fn ping() -> Int }\n"
    "component Pinger provides ping: Ping {\n"
    "  provide ping { fn ping() = 1 }\n"
    "}\n"
)

_APPLY_CANDIDATE = (
    "service Ping { fn ping() -> Int }\n"
    "extern emission[notify] fn applied_note(msg: Str) = @py { return }\n"
    "component Pinger provides ping: Ping {\n"
    '  emit applied_note("applied")\n'
    "  provide ping { fn ping() = 2 }\n"
    "}\n"
)


@needs_cordis
def test_apply_records_the_resulting_provenance_as_the_live_generation():
    """A plan-artifact apply records the post-apply bodies, not the pre-apply
    origin, so a later call/act ticket names the body the apply installed."""
    from revl import compile_source
    from revl.apply import build_artifact
    from revl.mcp.approval import ApprovalRequired
    from revl.mcp.session import Session
    from revl.plan import plan

    running = compile_source(_APPLY_RUNNING)
    artifact = build_artifact(
        plan(source=_APPLY_CANDIDATE, manifest=running, include_ir=True),
        running)
    session = Session()
    try:
        session.load(running, origin={"source": _APPLY_RUNNING})
        result = session.apply(artifact)
        assert result["applied"] is True, result
        assert set(result["resulting"]) == {"Pinger"}

        server.SESSION = session
        # The apply door replaces the provenance member with the resulting
        # bodies; the admission inputs stay, so snapshot and edit still see
        # what the session holds.
        assert _body_names(session.origin[
            server._ORIGIN_AUTHORED_HOST_BODIES]) == {"applied_note"}
        assert session.origin["source"] == _APPLY_RUNNING

        server._remember_live_host_bodies()
        assert _body_names(server._LIVE_HOST_BODIES) == {"applied_note"}

        ticket = server._approval_required(
            ApprovalRequired({"hash": "sha256:apply", "capabilities": []}))
        assert _body_names(ticket["ticket"]["unreviewedHostCode"]) == {
            "applied_note",
        }
    finally:
        if session.loaded:
            session.unload()
