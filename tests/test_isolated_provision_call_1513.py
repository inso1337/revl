"""Issue #1513: `Session.call` and MCP `revl_call` reach a realm-isolated provision.

A provider placed with `isolate ops in realm("wa")` publishes `ops` in realm
`wa`, not in the shared root realm. `_Driver._namespace` read only the shared
realm (`root.get(key)`), so the call found `None` and reported the provider as
"inactive" while `state()`'s `providedKeys` (item 372, realm-aware) listed the
same key as provided.

Decision, from docs/design-v2-realms.md: a realm decides WHICH provider
satisfies a key for the components that name it (Def. 28-29, multi-tenancy,
testing, sandboxes). It is not a visibility wall against the host: the
manifest renders isolated keys as `key@realm` because an orchestrator needs a
multi-tenant composition legible, and the approval class map already
classifies a call on a key provided in one realm against that provider and
stamps the ticket with its realm. So the caller outside every realm reaches a
key in its placement realm when the realm is unique. A key isolated into two
or more realms has no single provider, and the refusal names every
`(provider, realm)` rather than saying "inactive".
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl import compile_source  # noqa: E402

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="calling a live composition needs cordis-py "
           "(`sh backends/python/setup.sh`, run under backends/python/.venv)",
)

# the issue's minimal reproducer
_ISOLATED = """
service Ops { fn run(x: Str) -> Str }
component Worker provides ops: Ops {
  isolate ops in realm("wa")
  provide ops { fn run(x) = x }
}
"""

# the same key in two realms (examples/tenants.rvl, trimmed): both providers
# are live, and neither is THE provider of `kv` for a caller outside the realms
_TWO_REALMS = """
service Kv { fn get(k: Str) -> Str }
component TenantAStore provides kv: Kv {
  isolate kv in realm("tenant_a")
  provide kv { fn get(k) = "a:" + k }
}
component TenantBStore provides kv: Kv {
  isolate kv in realm("tenant_b")
  provide kv { fn get(k) = "b:" + k }
}
"""

# an isolated provider left PENDING: its `dep` is required in realm `wa`,
# but the only `dep` provider lives in the shared realm
_ISOLATED_PENDING = """
service Ops { fn run(x: Str) -> Str }
service Dep { fn get() -> Str }
component DepProvider provides dep: Dep {
  provide dep { fn get() = "d" }
}
component Worker requires dep: Dep provides ops: Ops {
  isolate ops in realm("wa")
  isolate dep in realm("wa")
  provide ops { fn run(x) = x }
}
"""

# an isolated provider whose method crosses a class-(c) emission
_ISOLATED_EMISSION = """
service Ops { emission[notify] fn stash(p: Str) }
extern emission[notify] fn notify(p: Str) = @py { return }
component Worker provides ops: Ops {
  isolate ops in realm("wa")
  provide ops { fn stash(p) { emit notify(p) } }
}
"""

# item 162's router: the shared-realm provider of `worker` fans out to
# workers isolated in w1/w2. The shared provision answers first.
_ROUTED = """
service Worker { fn call(r: Str) -> Str }
component W1 provides worker: Worker { isolate worker in realm("w1") provide worker { fn call(r) = "w1:" + r } }
component W2 provides worker: Worker { isolate worker in realm("w2") provide worker { fn call(r) = "w2:" + r } }
component RoundRobin requires worker: Worker provides worker: Worker {
  isolate worker in realms("w1", "w2") strategy(round_robin)
}
"""


@pytest.fixture
def session():
    from revl.mcp.session import Session  # noqa: PLC0415

    live = Session()
    try:
        yield live
    finally:
        if live.loaded:
            live.unload()


def _load(session, source: str, **kwargs) -> dict:
    return session.load(compile_source(source, "app.rvl"), **kwargs)


# --------------------------------------------------------------------------- #
# Session.call
# --------------------------------------------------------------------------- #

def test_session_call_reaches_a_provision_isolated_in_one_realm(session):
    """The issue's reproducer. On main: `providedKeys` said `['ops']` and the
    call said "its provider is inactive"."""
    state = _load(session, _ISOLATED)
    assert state["providedKeys"] == ["ops"]
    assert session.call("ops", "run", ["x"])["result"] == "x"


def test_every_served_key_surface_agrees_on_an_isolated_key(session):
    """`state()`, the apply drift check (`_provided_keys` and the live
    fingerprint), `live_state()` and the REPL namespace all read the same key
    as served. On main every one but `state()` read it as absent."""
    _load(session, _ISOLATED)
    assert session.state()["providedKeys"] == ["ops"]
    assert session._provided_keys() == ["ops"]
    assert session.live_state()["servedKeys"] == ["ops"]
    assert session._live_fingerprint()["provisions"] == [
        {"key": "ops", "provider": "Worker"}]
    assert session._driver._namespace()["ops"] is not None


def test_a_key_isolated_in_two_realms_is_refused_by_name(session):
    """Both providers are live, so "inactive" would be false. The refusal
    names each provider and its realm."""
    _load(session, _TWO_REALMS)
    from revl.mcp.session import SessionError  # noqa: PLC0415

    with pytest.raises(SessionError) as refused:
        session.call("kv", "get", ["who"])
    message = str(refused.value)
    assert "inactive" not in message
    assert "`TenantAStore` in realm `tenant_a`" in message
    assert "`TenantBStore` in realm `tenant_b`" in message
    assert "provided in 2 realms" in message
    # both provisions are still served; only the call has no single target
    assert session._live_fingerprint()["provisions"] == [
        {"key": "kv", "provider": "TenantAStore"},
        {"key": "kv", "provider": "TenantBStore"}]


def test_an_inactive_isolated_provider_is_named_with_its_realm(session):
    """When the isolated provider really is inactive, the message says so and
    names the provider and the realm it is isolated in."""
    _load(session, _ISOLATED_PENDING)
    from revl.mcp.session import SessionError  # noqa: PLC0415

    with pytest.raises(SessionError) as refused:
        session.call("ops", "run", ["x"])
    assert "provider `Worker`, isolated in realm `wa`, is inactive" \
        in str(refused.value)


def test_the_approval_ticket_names_the_isolated_provider_and_its_realm(session):
    """The approval decision classifies the call against the provider the call
    reaches. On main the call never got that far."""
    from revl.mcp.approval import ApprovalRequired  # noqa: PLC0415

    session.approval_policy = "auto"
    _load(session, _ISOLATED_EMISSION, record=True)
    with pytest.raises(ApprovalRequired) as held:
        session.call("ops", "stash", ["x"])
    ticket = held.value.ticket
    assert ticket["component"] == "Worker"
    assert ticket["key"] == "ops"
    assert ticket["realm"] == "wa"
    assert ticket["classCCapabilities"] == ["notify"]


def test_a_shared_realm_router_still_answers_for_its_key(session):
    """Regression guard, passes on main too: a key with a shared-realm
    provider resolves there, so a router keeps fanning out across its worker
    realms instead of the call being refused as a two-realm key."""
    _load(session, _ROUTED)
    seen = [session.call("worker", "call", ["x"])["result"] for _ in range(2)]
    assert seen == ["w1:x", "w2:x"]


# --------------------------------------------------------------------------- #
# MCP revl_call, over the transport
# --------------------------------------------------------------------------- #

@pytest.fixture
def transport(tmp_path):
    """A default-closed server rooted at `tmp_path`. Realm placements come
    from an operator file inside the root (inline agent source may not name a
    realm), and every assertion reads the JSON payload off the wire."""
    from revl.mcp import server  # noqa: PLC0415
    from revl.mcp.session import Session  # noqa: PLC0415

    before, before_session = server.AUTHORING, server.SESSION
    server.SESSION = Session()
    server.set_authoring_trust(host_code=False, granted=None,
                               roots=(str(tmp_path),))

    def call(name: str, arguments: dict) -> dict:
        response = server.handle({"jsonrpc": "2.0", "id": 1,
                                  "method": "tools/call",
                                  "params": {"name": name,
                                             "arguments": arguments}})
        return json.loads(response["result"]["content"][0]["text"])

    try:
        yield call
    finally:
        if server.SESSION.loaded:
            server.SESSION.unload()
        server.AUTHORING = before
        server.SESSION = before_session


def _file(tmp_path, source: str) -> str:
    path = tmp_path / "app.rvl"
    path.write_text(source, encoding="utf-8")
    return str(path)


def test_revl_call_reaches_a_provision_isolated_in_one_realm(transport,
                                                             tmp_path):
    loaded = transport("revl_load", {"files": [_file(tmp_path, _ISOLATED)]})
    assert loaded["ok"] is True, loaded
    payload = transport("revl_call", {"key": "ops", "method": "run",
                                      "args": ["x"]})
    assert payload["ok"] is True, payload
    assert payload["result"] == "x"


def test_revl_call_refuses_a_two_realm_key_by_name(transport, tmp_path):
    loaded = transport("revl_load", {"files": [_file(tmp_path, _TWO_REALMS)]})
    assert loaded["ok"] is True, loaded
    payload = transport("revl_call", {"key": "kv", "method": "get",
                                      "args": ["who"]})
    assert payload["ok"] is False
    text = json.dumps(payload)
    assert "inactive" not in text
    assert "`TenantAStore` in realm `tenant_a`" in text
    assert "`TenantBStore` in realm `tenant_b`" in text
