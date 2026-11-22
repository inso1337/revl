"""`revl_restore` applies `--bind-realm` bindings in every authoring trust mode,
as `revl_load` does (issue #3510): the restore's boot compile and its gate
profile both carry the operator's realm bindings, so `realm(?tenant)` resolves
instead of being refused as an unbound placeholder."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import server  # noqa: E402

SRC = """service S { fn q() -> Str }
component C provides s: S {
  isolate s in realm(?tenant)
  provide s { fn q() = "ok" }
}
"""
BINDING = (("tenant", "acme"),)


@pytest.fixture(autouse=True)
def _fresh_server(tmp_path, monkeypatch):
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
    session.recorded_loads = loaded
    yield
    server.AUTHORING = before
    server.set_runtime_available(None)


def _call(name: str, arguments: dict) -> dict:
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}})
    return json.loads(response["result"]["content"][0]["text"])


def _snapshot() -> dict:
    return {"sources": {"source": SRC},
            "manifest": {"components": [{"name": "C", "provides": ["s"]}],
                         "loadOrder": ["C"]},
            "meta": {"snapshotVersion": 1, "components": ["C"],
                     "loadOrder": ["C"], "record": False, "config": {}}}


def _realms(ir: dict) -> str:
    return json.dumps(ir)


def test_trusted_load_binds_the_realm(tmp_path):
    server.set_authoring_trust(host_code=True, realm_bindings=BINDING,
                               roots=(str(tmp_path),))
    payload = _call("revl_load", {"source": SRC})
    assert payload.get("ok") is True, payload
    assert '"acme"' in _realms(server.SESSION.recorded_loads[-1])


def test_trusted_restore_binds_the_realm(tmp_path):
    server.set_authoring_trust(host_code=True, realm_bindings=BINDING,
                               roots=(str(tmp_path),))
    payload = _call("revl_restore", {"snapshot": _snapshot()})
    assert payload.get("ok") is True, payload
    assert payload.get("restored") is True, payload
    assert '"acme"' in _realms(server.SESSION.recorded_loads[-1])


def test_trusted_restore_gate_profile_carries_the_bindings(tmp_path):
    server.set_authoring_trust(host_code=True, realm_bindings=BINDING,
                               roots=(str(tmp_path),))
    profile = server._restore_gate_profile()
    assert profile is not None and profile.realm_bindings == BINDING


@pytest.mark.parametrize("granted", [None, ["x"]])
def test_untrusted_restore_still_binds_the_realm(tmp_path, granted):
    server.set_authoring_trust(host_code=False, granted=granted,
                               realm_bindings=BINDING, roots=(str(tmp_path),))
    payload = _call("revl_restore", {"snapshot": _snapshot()})
    assert payload.get("restored") is True, payload
    assert '"acme"' in _realms(server.SESSION.recorded_loads[-1])
    profile = server._restore_gate_profile()
    assert profile is not None and profile.realm_bindings == BINDING


def test_no_bindings_leaves_the_gate_profile_unset(tmp_path):
    server.set_authoring_trust(host_code=True, roots=(str(tmp_path),))
    assert server._restore_gate_profile() is None
