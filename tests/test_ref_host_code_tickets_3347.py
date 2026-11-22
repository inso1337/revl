"""Issue #3347: an extern backed by a host-module ref is host code a ticket names.

An extern whose `@py` host code is `ref <symbol> from "<path>"` has no entry in
`bodies`, only in `refs`, so the disclosure record used to skip it: a load
ticket's `unreviewedHostCode`, a swap ticket's `newHostCode` and
`runningHostCode`, and the applied-generation provenance all said nothing for
it. A ref is now listed like a body, with the pinned sha256 as its digest and
the ref's path and symbol beside it; the pin says which bytes run, not that
anyone reviewed them.
"""

from __future__ import annotations

import hashlib

import pytest

from revl.compiler import compile_files
from revl.mcp import server
from revl.mcp.session import (_applied_generation_origin, _applied_host_bodies,
                              AUTHORED_HOST_BODIES_KEY, host_body_entries)

_ROOT = (
    "extern pure fn engine(x: Str) -> Str\n"
    '    = @py ref do_engine from "host/engine.py"\n'
    "service S { fn f(x: Str) -> Str }\n"
    "component C provides s: S {\n"
    "  provide s { fn f(x) = engine(x) }\n"
    "}\n"
)


def _host(text: str) -> str:
    return f"def do_engine(x):\n    return {text}\n"


def _write(tmp_path, host_text: str) -> str:
    (tmp_path / "host").mkdir(exist_ok=True)
    (tmp_path / "host" / "engine.py").write_text(host_text, encoding="utf-8")
    root = tmp_path / "root-3347.rvl"
    root.write_text(_ROOT, encoding="utf-8")
    return str(root)


def _pin(host_text: str) -> str:
    return hashlib.sha256(host_text.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def _trusted_server(monkeypatch, tmp_path):
    from revl.mcp.session import Session

    previous_authoring = server.AUTHORING
    monkeypatch.setattr(server, "SESSION", Session())
    monkeypatch.setattr(server, "_AUTHORED_HOST_BODIES", [])
    monkeypatch.setattr(server, "_LIVE_HOST_BODIES", [])
    server.set_authoring_trust(host_code=True, roots=(str(tmp_path),))
    yield
    server.AUTHORING = previous_authoring


def test_a_load_ticket_names_a_ref_backed_extern(monkeypatch, tmp_path):
    """The load ticket's `unreviewedHostCode` carries the ref extern, its
    backend, the pinned sha256 as the digest, and the ref's path and symbol."""
    from revl.mcp.approval import ApprovalRequired

    host_text = _host("x")
    files = [_write(tmp_path, host_text)]
    report = {}
    ir = server.compile_under_authoring(None, files, report=report)

    def load_requires_approval(*_args, **_kwargs):
        raise ApprovalRequired({"hash": "sha256:load-3347", "capabilities": []})

    monkeypatch.setattr(server.SESSION, "load", load_requires_approval)
    result = server._boot(ir, None, None, {}, True, {"files": files},
                          files=files, compile_report=report)

    assert result["approvalRequired"] is True, result
    assert result["ticket"].get("unreviewedHostCode") == [{
        "extern": "engine",
        "classification": "pure",
        "backends": ["py"],
        "digests": {"py": _pin(host_text)},
        "refs": {"py": {"path": "host/engine.py", "symbol": "do_engine"}},
    }]


def test_a_swap_that_moves_only_the_pin_is_new_host_code(tmp_path):
    """Two generations whose ref names the same path and symbol but whose host
    file changed: the candidate's entry is in `newHostCode`, and the running
    one in `runningHostCode` keeps the old pin."""
    old_text, new_text = _host("x"), _host('x + "!"')
    running = compile_files([_write(tmp_path, old_text)])
    candidate = compile_files([_write(tmp_path, new_text)])

    fields = server._host_code_fields(host_body_entries(candidate))
    server._add_replacement_host_code(fields, candidate, running)

    assert [entry["extern"] for entry in fields["newHostCode"]] == ["engine"]
    assert fields["newHostCode"][0]["digests"] == {"py": _pin(new_text)}
    assert [entry["digests"] for entry in fields["runningHostCode"]] == [
        {"py": _pin(old_text)}]

    unchanged = server._host_code_fields(host_body_entries(running))
    server._add_replacement_host_code(unchanged, running, running)
    assert unchanged["newHostCode"] == []


def test_applied_generation_provenance_tracks_the_ref_pin(tmp_path):
    """`_applied_host_bodies` and the applied origin list a ref whose pin
    moved, and leave out one the apply did not touch."""
    old_text, new_text = _host("x"), _host('x + "?"')
    running = compile_files([_write(tmp_path, old_text)])
    resulting = compile_files([_write(tmp_path, new_text)])

    applied = _applied_host_bodies(running, resulting)
    assert [(e["extern"], e["digests"]) for e in applied] == [
        ("engine", {"py": _pin(new_text)})]
    assert _applied_host_bodies(running, running) == []

    origin = _applied_generation_origin({}, running, resulting)
    assert [e["digests"] for e in origin[AUTHORED_HOST_BODIES_KEY]] == [
        {"py": _pin(new_text)}]
    carried = _applied_generation_origin(
        {AUTHORED_HOST_BODIES_KEY: host_body_entries(running)},
        running, running)
    assert [e["extern"] for e in carried[AUTHORED_HOST_BODIES_KEY]] == [
        "engine"]


def test_a_body_only_record_keeps_its_shape(tmp_path):
    """An extern with no ref still produces the four-field record, so the
    #3173 digest records are byte-identical."""
    root = tmp_path / "body-3347.rvl"
    root.write_text("extern pure fn inline(x: Str) -> Str = @py { return x }\n",
                    encoding="utf-8")
    entries = host_body_entries(compile_files([str(root)]))
    assert [set(entry) for entry in entries] == [
        {"extern", "classification", "backends", "digests"}]


def test_a_plan_lists_a_ref_backed_extern_as_candidate_host_code(tmp_path):
    """`revl plan` over the caller's files names the ref extern in
    `candidateHostBodies`, with the pin as its digest."""
    from revl.plan import plan

    host_text = _host("x")
    result = plan(files=[_write(tmp_path, host_text)], include_ir=True)
    assert result["admissible"] is True, result
    assert [(e["extern"], e["digests"], e.get("refs"))
            for e in result["candidateHostBodies"]] == [
        ("engine", {"py": _pin(host_text)},
         {"py": {"path": "host/engine.py", "symbol": "do_engine"}})]
