"""A restore's lease derivation compiles under the boot's realm bindings
(issue #3509), as the operator gate's does: a bound `realm(?tenant)` is
decidable, so a restore is refused only when it touches a leased component.
An unbound placeholder stays undecidable and refuses against any lease."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import leases as L  # noqa: E402
from revl.mcp import server  # noqa: E402
from revl.mcp.leases import LeaseBook  # noqa: E402
from revl.mcp.operator import Grant, Operator  # noqa: E402
from revl.policy import parse_policy  # noqa: E402

SRC = """service S { fn q() -> Str }
component C provides s: S {
  isolate s in realm(?tenant)
  provide s { fn q() = "ok" }
}
"""
BINDING = (("tenant", "acme"),)
ENFORCING = parse_policy("leases enforced")


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
    session.sandbox = ENFORCING
    session.operator = Operator("alice", (Grant(("restore",), ("*",), True),))
    session.leases = LeaseBook()
    server.set_runtime_available(True)
    session.recorded_loads = loaded
    yield session
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


def _bind(tmp_path, bindings=BINDING):
    server.set_authoring_trust(host_code=True, realm_bindings=bindings,
                               roots=(str(tmp_path),))


def test_bound_restore_is_admitted_beside_an_unrelated_lease(
        tmp_path, _fresh_server):
    _bind(tmp_path)
    _fresh_server.leases.claim("Unrelated", "bob", ttl=600)
    payload = _call("revl_restore", {"snapshot": _snapshot()})
    assert payload.get("restored") is True, payload
    assert '"acme"' in json.dumps(_fresh_server.recorded_loads[-1])


def test_bound_restore_is_refused_on_a_component_it_touches(
        tmp_path, _fresh_server):
    _bind(tmp_path)
    _fresh_server.leases.claim("C", "bob", ttl=600)
    payload = _call("revl_restore", {"snapshot": _snapshot()})
    assert payload.get("ok") is False, payload
    assert payload["lease"]["heldBy"] == "bob"
    assert payload["lease"]["component"] == "C"
    assert not _fresh_server.recorded_loads


def test_unbound_placeholder_stays_undecidable_and_refuses(
        tmp_path, _fresh_server):
    _bind(tmp_path, bindings=())
    _fresh_server.leases.claim("Unrelated", "bob", ttl=600)
    payload = _call("revl_restore", {"snapshot": _snapshot()})
    assert payload.get("ok") is False, payload
    assert payload["lease"]["heldBy"] == "bob"
    assert not _fresh_server.recorded_loads


def test_check_derives_the_restore_under_the_given_profile(
        tmp_path, _fresh_server):
    _bind(tmp_path)
    _fresh_server.leases.claim("Unrelated", "bob", ttl=600)
    args = {"snapshot": _snapshot()}
    assert L.check(_fresh_server, "restore", args) is not None
    profile = server._restore_gate_profile()
    assert L.check(_fresh_server, "restore", args,
                   restore_profile=profile) is None
