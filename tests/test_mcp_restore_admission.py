"""`revl_restore` admits the same text it loads.

A snapshot handed to `revl_restore` is agent-supplied. Its `files` branch
compiles `files_content` with `dependencies` laid over it (issue #1779: the
text a `use`-reached module had when it was snapshotted). The admission
decision has to cover that same overlaid set under the session's authoring
trust; a decision taken over `files_content` alone admits a harmless copy of a
module while the session loads the `dependencies` copy, host body and all.

The session's `load` is replaced by a recorder, so the test needs no cordis
runtime and nothing that is admitted is booted or run.
"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import server  # noqa: E402
from revl.errors import RevlError  # noqa: E402

MAIN = ('use "dep.rvl" { seven }\n'
        "service Clock { fn now() -> Int }\n"
        "component FixedClock provides clock: Clock {\n"
        "  provide clock { fn now() = seven() }\n"
        "}\n")
HARMLESS_DEP = "pub fn seven() -> Int { return 7 }\n"
PURE_DEP = ("pub fn six() -> Int { return 6 }\n"
            "pub fn seven() -> Int { return six() + 1 }\n")
HOST_DEP = ("pub extern pure fn boom() -> Int = @py { return 7 }\n"
            "pub fn seven() -> Int { return boom() }\n")


@pytest.fixture(autouse=True)
def _fresh_server(tmp_path, monkeypatch):
    """A closed-by-default server rooted at `tmp_path`, whose session records
    the IR it is asked to load instead of booting it."""
    from revl.mcp.session import Session

    before = server.AUTHORING
    session = Session()
    loaded: list = []

    def record_load(ir, config=None, **_kwargs):
        loaded.append(ir)
        return {"loaded": True}

    monkeypatch.setattr(session, "load", record_load)
    monkeypatch.setattr(server, "SESSION", session)
    server.set_runtime_available(True)
    server.set_authoring_trust(host_code=False, granted=None, providers=None,
                               roots=(str(tmp_path),))
    session.recorded_loads = loaded
    yield
    server.AUTHORING = before
    server.set_runtime_available(None)


def _call(name: str, arguments: dict) -> dict:
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}})
    return json.loads(response["result"]["content"][0]["text"])


def _message(payload: dict) -> str:
    return " ".join(d.get("message", "") for d in payload.get("diagnostics") or [])


def _snapshot(tmp_path, files_dep: str, dependencies_dep: str | None) -> dict:
    main = str(tmp_path / "main.rvl")
    dep = str(tmp_path / "dep.rvl")
    sources = {"files": [main],
               "files_content": {main: MAIN, dep: files_dep}}
    if dependencies_dep is not None:
        sources["dependencies"] = {dep: dependencies_dep}
    return {"sources": sources,
            "manifest": {"components": [{"name": "FixedClock",
                                         "provides": ["clock"], "file": main}],
                         "loadOrder": ["FixedClock"]},
            "meta": {"snapshotVersion": 1, "components": ["FixedClock"],
                     "loadOrder": ["FixedClock"], "record": False,
                     "config": {}}}


def _loaded_externs() -> list:
    return [extern.get("name")
            for ir in server.SESSION.recorded_loads
            for extern in ir.get("externs") or []]


def test_a_host_body_in_dependencies_is_refused_by_name(tmp_path):
    """The harmless `files_content` copy of dep.rvl must not admit the
    `dependencies` copy that carries a host body: the refusal is the one the
    same module gets when it sits in `files_content` (G8, no new extern)."""
    payload = _call("revl_restore",
                    {"snapshot": _snapshot(tmp_path, HARMLESS_DEP, HOST_DEP)})
    assert _loaded_externs() == [], (
        "the restore loaded an extern the admission check never saw: "
        + json.dumps(payload)[:400])
    assert payload["ok"] is False, payload
    assert payload.get("restored") is not True
    assert payload.get("authoringTrust") == "untrusted"
    assert "forbids new `extern`" in _message(payload), payload
    assert "G8" in json.dumps(payload["diagnostics"])
    assert server.SESSION.recorded_loads == []


def test_the_restore_compile_itself_refuses_a_dependencies_host_body(
        tmp_path, monkeypatch):
    """The structural sweep before dispatch is one layer; the compile whose IR
    is loaded is the other, and it must hold on its own. With the sweep out of
    the way, the restore's single compile runs under the authoring profile over
    `files_content` plus `dependencies`, and refuses by name."""
    monkeypatch.setattr(server, "_authoring_refusal", lambda _arguments: None)
    payload = _call("revl_restore",
                    {"snapshot": _snapshot(tmp_path, HARMLESS_DEP, HOST_DEP)})
    assert _loaded_externs() == [], (
        "the restore loaded an extern its admission compile never saw: "
        + json.dumps(payload)[:400])
    assert payload["ok"] is False, payload
    assert payload.get("restored") is False
    assert payload.get("reAdmitted") is False
    assert payload.get("authoringTrust") == "untrusted"
    assert "G8" in json.dumps(payload["diagnostics"])
    assert "host" in _message(payload)


def test_the_same_host_body_in_files_content_is_refused_the_same_way(tmp_path):
    """The control the bypass is measured against."""
    payload = _call("revl_restore",
                    {"snapshot": _snapshot(tmp_path, HOST_DEP, None)})
    assert payload["ok"] is False, payload
    assert payload.get("authoringTrust") == "untrusted"
    assert "forbids new `extern`" in _message(payload)
    assert "G8" in json.dumps(payload["diagnostics"])
    assert server.SESSION.recorded_loads == []


def test_dependencies_of_pure_revl_code_still_restore(tmp_path):
    """`dependencies` stay honoured: a dependency text that only adds revl
    code is the text that is admitted and loaded."""
    payload = _call("revl_restore",
                    {"snapshot": _snapshot(tmp_path, HARMLESS_DEP, PURE_DEP)})
    assert payload["ok"] is True, payload
    assert payload.get("restored") is True
    (ir,) = server.SESSION.recorded_loads
    assert ir.get("externs") in (None, [])
    assert "six" in json.dumps(ir), "the dependencies copy is the one loaded"


def test_a_pure_dependency_at_a_provider_path_is_still_snapshot_data(tmp_path):
    """Restore uses the dependency bytes in the snapshot, not a provider map.

    A matching path is not a provider composition or a reason to refuse a
    pure-Revl snapshot dependency.
    """
    dep = str(tmp_path / "dep.rvl")
    server.set_authoring_trust(host_code=False, granted=None,
                               providers={dep: HARMLESS_DEP},
                               roots=(str(tmp_path),))

    payload = _call("revl_restore",
                    {"snapshot": _snapshot(tmp_path, HARMLESS_DEP, PURE_DEP)})

    assert payload["ok"] is True, payload
    (ir,) = server.SESSION.recorded_loads
    assert "six" in json.dumps(ir), "restore must use snapshot dependency bytes"


def test_a_host_body_at_a_provider_path_gets_an_explained_restore_refusal(
        tmp_path, monkeypatch):
    """The snapshot does not inherit provider trust from a matching path.

    Pin the user-facing explanation at the structural precheck and at the
    compile door that produces the loaded IR.
    """
    dep = str(tmp_path / "dep.rvl")
    server.set_authoring_trust(host_code=False, granted=None,
                               providers={dep: HOST_DEP},
                               roots=(str(tmp_path),))
    payload = _call("revl_restore",
                    {"snapshot": _snapshot(tmp_path, HARMLESS_DEP, HOST_DEP)})

    assert payload["ok"] is False, payload
    assert "forbids new `extern`/host-block declarations" in _message(payload), payload
    assert "G8" in json.dumps(payload.get("diagnostics")), payload
    assert "does not merge the live provider map" in payload["note"], payload
    assert "does not make those agent-supplied bytes operator-authored" \
        in payload["note"], payload
    assert server.SESSION.recorded_loads == []

    monkeypatch.setattr(server, "_authoring_refusal", lambda _arguments: None)
    compile_payload = _call(
        "revl_restore",
        {"snapshot": _snapshot(tmp_path, HARMLESS_DEP, HOST_DEP)})
    assert compile_payload["ok"] is False, compile_payload
    assert "forbids reaching host code" in _message(compile_payload), compile_payload
    assert "does not merge the live provider map" in compile_payload["note"]
    assert server.SESSION.recorded_loads == []


def test_live_composition_still_refuses_an_agent_module_at_a_provider_path(
        tmp_path):
    dep = str(tmp_path / "dep.rvl")
    server.set_authoring_trust(host_code=False, granted=None,
                               providers={dep: HARMLESS_DEP},
                               roots=(str(tmp_path),))

    with pytest.raises(RevlError) as refused:
        server.compile_under_authoring(MAIN, None,
                                       modules={dep: PURE_DEP})

    assert "operator-sanctioned provider" in refused.value.message


def test_a_trusted_author_still_restores_a_host_body(tmp_path):
    """Under `--author-trust trusted` (no profile) the restore is the
    operator's compile, byte-identical to before: the extern loads."""
    server.set_authoring_trust(host_code=True)
    payload = _call("revl_restore",
                    {"snapshot": _snapshot(tmp_path, HARMLESS_DEP, HOST_DEP)})
    assert payload["ok"] is True, payload
    assert _loaded_externs() == ["boom"]


def test_the_operator_restore_path_compiles_without_a_profile(tmp_path):
    """`revl mcp serve --restore` and crash recovery call `Session.restore`
    with no profile, so the operator's own snapshot keeps its host code."""
    snap = _snapshot(tmp_path, HOST_DEP, None)
    result = server.SESSION.restore(snap)
    assert result["restored"] is True
    assert _loaded_externs() == ["boom"]
    assert os.path.basename(snap["sources"]["files"][0]) == "main.rvl"
