"""A composition served with `revl serve --http` takes a new version through its
operator listener, without a restart (issue #3430).

The operator listener (issue #1553) puts the MCP HTTP transport on the face's
session, so the compiler server's `revl_plan`, `revl_swap` and `revl_undo` reach
the session the face is serving. The claims under test, on one live stack:

  * a plan over the operator listener names the change and applies nothing;
  * a swap of a successor with one changed operation body is answered by the
    face on its next request, with no restart;
  * a candidate admission refuses leaves the face answering as before;
  * an undo brings the first behaviour back;
  * a swap that changes the served surface itself (a route) is refused by name,
    as `run --watch` refuses a change to what boot-bound hosts serve (#1569);
  * an operator without the `swap` grant cannot swap.

The live tests drive a real cordis-py composition and skip without the pinned
fork; a skip is not a pass.
"""

import hashlib
import http.client
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:  # `runtime`, imported only by cordis tests
    sys.path.insert(0, str(_BACKEND))

from revl import compile_source  # noqa: E402
from revl.mcp import server  # noqa: E402
from revl.mcp.http_face import HttpComposedServer, build_http_server  # noqa: E402
from revl.mcp.http_guard import Exposure  # noqa: E402
from revl.mcp.http_transport import PROTOCOL_VERSION  # noqa: E402
from revl.mcp.operator import parse_profile  # noqa: E402
from revl.mcp.operator_listener import OperatorListener  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="booting a composition needs the cordis-py runtime; install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)

SECRETS = {"ops": "ops-secret-3430", "reader": "reader-secret-3430"}


def _profile() -> str:
    grants = {"ops": "swap, undo, call", "reader": "lease"}
    lines = []
    for token, verbs in grants.items():
        digest = hashlib.sha256(SECRETS[token].encode("utf-8")).hexdigest()
        lines.append(f"operator {token} key sha256:{digest}")
        lines.append(f"operator {token} may {verbs} on *")
    return "\n".join(lines) + "\n"


PROFILE = _profile()


def _source(version: int, *, route: str = "/version") -> str:
    return f'''
service Info {{
  route get "{route}"
  fn version() -> Int
  fn size() -> Int
}}
component Agent provides info: Info {{
  provide info {{
    fn version() = {version}
    fn size() = 0
  }}
}}
'''


V1 = _source(1)
V2 = _source(2)
# admission refuses it: the checker finds `version` returning a Str where its
# service declares an Int, so the candidate never reaches the running composition
REFUSED = V2.replace("fn version() = 2", 'fn version() = "three"')
assert REFUSED != V2
# the operation body is unchanged; only the route the face serves it at moves
MOVED = _source(1, route="/v")

PUBLIC = {("info", "size")}


# ---------------------------------------------------------------- clients

def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    conn.request("GET", path)
    response = conn.getresponse()
    payload = response.read()
    conn.close()
    return response.status, (json.loads(payload) if payload else None)


def _operator(port, who, name, arguments=None):
    """An operator's verdict (`structuredContent`) for one MCP tool call."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": name, "arguments": arguments or {},
        "_meta": {"io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                  "io.modelcontextprotocol/clientCapabilities": {}}}}
    conn.request("POST", "/mcp", body=json.dumps(message).encode("utf-8"),
                 headers={"Content-Type": "application/json",
                          "Accept": "application/json, text/event-stream",
                          "MCP-Protocol-Version": PROTOCOL_VERSION,
                          "Mcp-Method": "tools/call", "Mcp-Name": name,
                          "Authorization": f"Bearer {SECRETS[who]}"})
    response = conn.getresponse()
    payload = response.read()
    conn.close()
    assert response.status == 200, (response.status, payload)
    return json.loads(payload)["result"]["structuredContent"]


# ---------------------------------------------------------------- the stack

class _Stack:
    """A live face and its operator listener, as `serve_http` wires them."""

    def __init__(self, tmp_path):
        from revl.mcp.session import Session

        self.session = Session()
        self.session._wal_path = str(tmp_path / "session.wal")
        self.session.approval_policy = "auto"
        self.session.load(compile_source(V1, "info_3430.rvl"), record=True,
                          origin={"source": V1})
        self.face = HttpComposedServer(self.session, composition="app",
                                       public=PUBLIC)
        self.httpd = build_http_server(self.face, "127.0.0.1", 0)
        self._serving = threading.Thread(target=self.httpd.serve_forever,
                                         daemon=True)
        self._serving.start()
        self.listener = OperatorListener(
            self.face, exposure=Exposure("127.0.0.1", 0),
            app_exposure=self.httpd.exposure, registry=parse_profile(PROFILE),
            server_module=server)
        self.listener.start()
        self.app_port = self.httpd.server_address[1]
        self.op_port = self.listener.exposure.port_in_use

    def op(self, name, arguments=None, who="ops"):
        return _operator(self.op_port, who, name, arguments)

    def close(self):
        import runtime as rt

        self.listener.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self._serving.join(timeout=5)
        try:
            live = self.face.session
            if live.loaded and not live.halted:
                live.unload()
        finally:
            rt.clear_estop()
            rt._LIVE_FRAMES.clear()


@pytest.fixture
def stack(tmp_path, monkeypatch):
    from revl.mcp.leases import LeaseBook
    from revl.mcp.session import Session

    placeholder = Session()
    placeholder.leases = LeaseBook()
    monkeypatch.setattr(server, "SESSION", placeholder)
    built = _Stack(tmp_path)
    yield built
    built.close()
    assert server.SESSION is placeholder, "the listener put the server back"


def _version(stack):
    return _get(stack.app_port, "/version")


# ---------------------------------------------------------------- runtime-free

class _IrOnly:
    """A session that carries only the compiled IR: the surface is a pure
    function of it."""

    def __init__(self, ir):
        self.ir = ir
        self.served_surface_check = None


def _face(source):
    session = _IrOnly(compile_source(source, "info_3430.rvl"))
    face = HttpComposedServer(session, composition="app", public=PUBLIC)
    assert session.served_surface_check == face.surface_change
    return face


def _change(face, source):
    return face.surface_change(compile_source(source, "info_3430.rvl"))


def test_an_operation_body_change_keeps_the_served_surface():
    assert _change(_face(V1), V2) is None
    assert _change(_face(V1), V1) is None


def test_a_moved_route_and_a_changed_public_signature_are_named():
    face = _face(V1)
    assert "changes `info.version`" in _change(face, MOVED)
    signature = V1.replace("fn size() -> Int", "fn size(n: Int) -> Int") \
                  .replace("fn size() = 0", "fn size(n) = n")
    assert "changes `info.size`" in _change(face, signature)
    dropped = V1.replace('  route get "/version"\n', "")
    assert "drops `info.version`" in _change(face, dropped)


def test_a_type_the_surface_names_is_part_of_it():
    def typed(fields, extra=""):
        return f"""
type Card = {{ {fields} }}
{extra}
service Info {{
  route get "/card"
  fn card() -> Card
}}
component Agent provides info: Info {{
  provide info {{ fn card() = {{ n: 1 }} }}
}}
"""
    face = _face(typed("n: Int"))
    assert _change(face, typed("n: Int", "type Other = { m: Str }")) is None
    assert "changes `info.card`" in _change(face, typed("n: Int, m: Opt[Str]")
                                            .replace("{ n: 1 }", "{ n: 1, m: None }"))


# ---------------------------------------------------------------- the update

@needs_cordis
def test_a_served_composition_takes_a_new_version_without_a_restart(stack):
    assert _version(stack) == (200, 1)
    generation = stack.session._generation

    # plan: the rehearsal against the running composition, nothing applied
    plan = stack.op("revl_plan", {"source": V2})
    assert plan.get("against") == "session", plan
    assert _version(stack) == (200, 1)

    # swap: the face answers from the new generation on its next request
    swapped = stack.op("revl_swap", {"source": V2})
    assert swapped["ok"] is True and swapped.get("swapped") is not False, swapped
    assert stack.face.session._generation > generation
    assert _version(stack) == (200, 2)

    # a candidate admission refuses changes nothing the face serves
    refused = stack.op("revl_swap", {"source": REFUSED})
    assert refused["ok"] is False and refused.get("swapped") is False, refused
    assert _version(stack) == (200, 2)

    # undo: the first behaviour is back
    undone = stack.op("revl_undo")
    assert undone["ok"] is True, undone
    assert _version(stack) == (200, 1)


@needs_cordis
def test_a_swap_that_moves_a_served_route_is_refused_by_name(stack):
    generation = stack.session._generation
    refused = stack.op("revl_swap", {"source": MOVED})
    assert refused["ok"] is False, refused
    assert refused["code"] == "SERVED_SURFACE_CHANGED", refused
    message = refused["diagnostics"][0]["message"]
    assert "changes `info.version`" in message and "restart `revl serve`" in \
        message, message
    assert stack.face.session._generation == generation, "nothing swapped"
    assert _version(stack) == (200, 1)
    assert _get(stack.app_port, "/v")[0] == 404


@needs_cordis
def test_an_operator_without_the_swap_grant_cannot_swap(stack):
    refused = stack.op("revl_swap", {"source": V2}, who="reader")
    assert refused["ok"] is False, refused
    assert _version(stack) == (200, 1)
