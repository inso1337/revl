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


# --------------------------------------------------------------------------- #
# The mock-world lifecycle runner (`revl test --mock-requires`)
# --------------------------------------------------------------------------- #

def _mock_world(source: str) -> tuple[tuple[int, int], str]:
    from revl import mocks  # noqa: PLC0415

    lines: list[str] = []
    result = mocks.run_mock_requires(compile_source(source, "m.rvl"),
                                     out=lines.append)
    return result, "\n".join(lines)


def test_a_lifecycle_call_reaches_an_isolated_provision():
    """On main the `call` step read `root.get("ops")` and failed with "loaded
    but not ACTIVE" while `Worker` was ACTIVE in realm `wa`."""
    result, out = _mock_world(_ISOLATED + """
lifecycle test "isolated call" {
  load Worker
  let r = call ops.run("x")
  assert r == "x"
  unload Worker
  assert no_residue
}
""")
    assert result == (0, 1), out


def test_an_isolated_requirement_is_mocked_in_its_realm():
    """`App` requires `db` in realm `wa`. On main the auto-mock check read
    `root.get("db")`, found nothing, and plugged the mock into the SHARED
    realm, where `App` never sees it, so `App` stayed PENDING."""
    result, out = _mock_world("""
service Database { fn ping() -> Bool }
service Api { fn up() -> Bool }
component App requires db: Database provides api: Api {
  isolate db in realm("wa")
  provide api { fn up() { return db.ping() } }
}
lifecycle test "isolated requirement" {
  load App
  let u = call api.up()
  unload App
  assert no_residue
}
""")
    assert result == (0, 1), out


def test_a_lifecycle_call_on_a_two_realm_key_is_refused_by_name():
    result, out = _mock_world(_TWO_REALMS + """
lifecycle test "two realms" {
  load TenantAStore
  load TenantBStore
  let r = call kv.get("who")
  unload TenantBStore
  unload TenantAStore
}
""")
    assert result == (1, 1), out
    assert "`TenantAStore` in realm `tenant_a`" in out
    assert "not ACTIVE" not in out


# --------------------------------------------------------------------------- #
# The placement process runner (`revl run --placement`)
# --------------------------------------------------------------------------- #

def _placement(tmp_path, source: str, toml: str) -> str:
    """Boot one placement through the conductor with THIS interpreter (which
    has cordis-py, per the module skip) and return its whole trace."""
    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    rvl = tmp_path / "app.rvl"
    rvl.write_text(source, encoding="utf-8")
    placement = tmp_path / "app.toml"
    placement.write_text(toml, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "revl", "run", str(rvl),
         "--placement", str(placement), "--once"],
        capture_output=True, text=True, timeout=300,
        stdin=subprocess.DEVNULL, cwd=str(tmp_path),
        env={**os.environ, "REVL_PY": sys.executable,
             "PYTHONPATH": str(ROOT / "src")})
    trace = result.stdout + result.stderr
    assert result.returncode == 0, trace
    return trace


def test_a_process_holds_two_tenants_of_one_key(tmp_path):
    """On main the runner plugged every component with `root.plugin`, which
    drops `isolate`, so both stores provided `kv` in the shared realm: the
    second FAILED with `service "kv" has been registered at <TenantAStore>`,
    left residue, and the probe silently answered from tenant A. Both now
    activate in their realms, and the probe on the two-realm key is refused
    naming both."""
    trace = _placement(tmp_path, _TWO_REALMS,
                       '[processes.only]\n'
                       'components = ["TenantAStore", "TenantBStore"]\n'
                       "probe = [\"kv.get('k')\"]\n")
    assert "load  | TenantAStore    | state=ACTIVE" in trace, trace
    assert "load  | TenantBStore    | state=ACTIVE" in trace, trace
    assert "has been registered" not in trace, trace
    assert "`TenantBStore` in realm `tenant_b`" in trace, trace
    assert "=> 'a:k'" not in trace, trace
    assert "[only] residue no residue" in trace, trace


def test_a_probe_and_a_seam_reach_an_isolated_provision(tmp_path):
    """Regression guard, passes on main too, where isolation was dropped: with
    isolation now honoured, a probe on an isolated key still resolves, and a
    seam still carries an isolated key from its provider process to a
    consumer isolated into the same realm in another process."""
    trace = _placement(tmp_path, _ISOLATED + """
service Api { fn go(x: Str) -> Str }
component Front requires ops: Ops provides api: Api {
  isolate ops in realm("wa")
  provide api { fn go(x) { return ops.run(x) } }
}
""", '[processes.back]\ncomponents = ["Worker"]\n'
     "probe = [\"ops.run('p')\"]\n\n"
     '[processes.front]\ncomponents = ["Front"]\n'
     "probe = [\"api.go('s')\"]\n")
    assert "[back] probe | ops.run('p')    | => 'p'" in trace, trace
    assert "[front] probe | api.go('s')     | => 's'" in trace, trace
    assert "[back] residue no residue" in trace, trace
    assert "[front] residue no residue" in trace, trace
