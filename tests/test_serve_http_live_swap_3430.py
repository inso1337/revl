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
    as `run --watch` refuses a change to what boot-bound hosts serve (#1569),
    and a plan of it says so;
  * a route binding a typed `Request` answers before a swap, after it and after
    an undo: the face rebuilds the request through the module that runs now;
  * an unload then a cold load, or a restore, of a successor that moves a
    route is refused as the swap is, and of the same surface is not;
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
from revl.mcp.http_face import (HttpComposedServer, build_face,  # noqa: E402
                                build_http_server)
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
    grants = {"ops": "swap, undo, call, load, unload, restore, snapshot",
              "reader": "lease"}
    lines = []
    for token, verbs in grants.items():
        digest = hashlib.sha256(SECRETS[token].encode("utf-8")).hexdigest()
        lines.append(f"operator {token} key sha256:{digest}")
        lines.append(f"operator {token} may {verbs} on *")
    return "\n".join(lines) + "\n"


PROFILE = _profile()


def _source(version: int, *, route: str = "/version") -> str:
    # `m` binds a typed `Request`: its handler matches on the `Method` and
    # `Body` cases the face rebuilds through the running module
    return f'''
use "stdlib/http.rvl" {{ Request, Response, ok_text }}

service Info {{
  route get "{route}"
  fn version() -> Int
  fn size() -> Int
  route post "/m"
  fn m(req: Request) -> Response
}}
component Agent provides info: Info {{
  provide info {{
    fn version() = {version}
    fn size() = 0
    fn m(req) {{
      let verb = match req.method {{
        Get => "get", Post => "post", Put => "put",
        Patch => "patch", Delete => "delete", Head => "head",
      }}
      let text = match req.body {{ Empty => "", Text(s) => s, Json(s) => s }}
      return ok_text("v{version}:" + verb + ":" + text)
    }}
  }}
}}
'''


# the in-memory `use` the sources make, as an MCP client sends it in `modules`
MODULES = {"stdlib/http.rvl":
           (ROOT / "stdlib" / "http.rvl").read_text(encoding="utf-8")}


def _compile(source: str) -> dict:
    return compile_source(source, "info_3430.rvl", modules=MODULES)


def _carried(source: str) -> dict:
    return {"source": source, "modules": MODULES}


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


def _post(port, path, body: bytes):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    conn.request("POST", path, body=body,
                 headers={"Content-Type": "text/plain"})
    response = conn.getresponse()
    payload = response.read()
    conn.close()
    return response.status, payload.decode("utf-8", "replace")


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
        self.session.load(_compile(V1), record=True, origin=_carried(V1))
        # the face `serve_http` builds: the live decoder, and its rebuild per
        # generation, are what a typed `Request` route needs
        self.face = build_face(self.session, composition="app", public=PUBLIC)
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


def _typed(stack):
    return _post(stack.app_port, "/m", b"hi")


# ---------------------------------------------------------------- runtime-free

class _IrOnly:
    """A session that carries only the compiled IR: the surface is a pure
    function of it."""

    def __init__(self, ir):
        self.ir = ir
        self.served_surface_check = None


def _face(source):
    session = _IrOnly(_compile(source))
    face = HttpComposedServer(session, composition="app", public=PUBLIC)
    assert session.served_surface_check == face.surface_change
    return face


def _change(face, source):
    return face.surface_change(_compile(source))


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


def test_a_type_named_like_a_path_segment_or_a_parameter_is_not_named():
    """A type is part of the surface for a type position only: a path segment
    or a parameter that shares its name does not pull it in, while the same
    type in the operation's return does."""
    def echo(fields, returns="Int", value="1"):
        return f"""
type Echo = {{ {fields} }}
type Note = {{ {fields} }}
service Info {{
  route get "/Echo"
  fn card(Note: Int) -> {returns}
}}
component Agent provides info: Info {{
  provide info {{ fn card(Note) = {value} }}
}}
"""
    face = _face(echo("a: Int"))
    assert _change(face, echo("a: Int, b: Opt[Int]")) is None
    typed = _face(echo("a: Int", "Note", "{ a: Note }"))
    assert "changes `info.card`" in _change(
        typed, echo("a: Int, b: Opt[Int]", "Note", "{ a: Note, b: None }"))


# ---------------------------------------------------------------- the update

def _refused_for_the_surface(answer, operation="info.version"):
    """A refusal for the served surface; a plan's `ok` says only that the plan
    was computed, so for a plan read `admissible: false` instead."""
    assert answer.get("admissible", answer["ok"]) is False, answer
    assert answer["code"] == "SERVED_SURFACE_CHANGED", answer
    diagnostic = answer["diagnostics"][0]
    assert diagnostic["code"] == "SERVED_SURFACE_CHANGED", answer
    assert f"changes `{operation}`" in diagnostic["message"], answer
    assert "restart `revl serve`" in diagnostic["message"], answer


@needs_cordis
def test_a_served_composition_takes_a_new_version_without_a_restart(stack):
    assert _version(stack) == (200, 1)
    assert _typed(stack) == (200, "v1:post:hi")
    generation = stack.session._generation

    # plan: the rehearsal against the running composition, nothing applied
    plan = stack.op("revl_plan", _carried(V2))
    assert plan.get("against") == "session", plan
    assert plan["admissible"] is True, plan
    assert _version(stack) == (200, 1)

    # swap: the face answers from the new generation on its next request,
    # through the new module for a typed `Request` too
    swapped = stack.op("revl_swap", _carried(V2))
    assert swapped["ok"] is True and swapped.get("swapped") is not False, swapped
    assert stack.face.session._generation > generation
    assert _version(stack) == (200, 2)
    assert _typed(stack) == (200, "v2:post:hi")

    # a candidate admission refuses changes nothing the face serves
    refused = stack.op("revl_swap", _carried(REFUSED))
    assert refused["ok"] is False and refused.get("swapped") is False, refused
    assert _version(stack) == (200, 2)
    assert _typed(stack) == (200, "v2:post:hi")

    # undo: the first behaviour is back
    undone = stack.op("revl_undo")
    assert undone["ok"] is True, undone
    assert _version(stack) == (200, 1)
    assert _typed(stack) == (200, "v1:post:hi")


@needs_cordis
def test_a_swap_that_moves_a_served_route_is_refused_by_name(stack):
    generation = stack.session._generation
    # the plan says what the swap will answer
    plan = stack.op("revl_plan", _carried(MOVED))
    _refused_for_the_surface(plan)
    refused = stack.op("revl_swap", _carried(MOVED))
    _refused_for_the_surface(refused)
    assert refused["swapped"] is False, refused
    assert stack.face.session._generation == generation, "nothing swapped"
    assert _version(stack) == (200, 1)
    assert _get(stack.app_port, "/v")[0] == 404


@needs_cordis
def test_an_undo_to_another_surface_is_refused_with_its_code(stack):
    """The face was built on V1; a generation with another surface reached
    some other way (here: the session's own swap, past the face's check) is
    one an undo back to it cannot serve. The undo's refusal carries the code
    as the swap's does."""
    check, stack.session.served_surface_check = \
        stack.session.served_surface_check, None
    try:
        stack.session.swap(_compile(MOVED), origin=_carried(MOVED),
                           agent_authored=False)
        stack.session.swap(_compile(V2), origin=_carried(V2),
                           agent_authored=False)
    finally:
        stack.session.served_surface_check = check
    assert _version(stack) == (200, 2)
    generation = stack.session._generation
    undone = stack.op("revl_undo")
    _refused_for_the_surface(undone)
    assert stack.session._generation == generation, "nothing undone"
    assert _version(stack) == (200, 2)


@needs_cordis
def test_an_unload_then_a_load_answers_to_the_served_surface(stack):
    assert stack.op("revl_unload", {"force": True})["ok"] is True
    # a cold load that moves the route is refused as the swap is; nothing loads
    refused = stack.op("revl_load", _carried(MOVED))
    _refused_for_the_surface(refused)
    assert stack.face.session.loaded is False
    # a cold load of the same surface (a new body) is the face's to serve
    loaded = stack.op("revl_load", _carried(V2))
    assert loaded["ok"] is True, loaded
    assert _version(stack) == (200, 2)
    assert _typed(stack) == (200, "v2:post:hi")


@needs_cordis
def test_a_restore_answers_to_the_served_surface(stack, tmp_path):
    from revl.mcp.session import Session

    # a snapshot of a composition with another surface, from another session
    # with the served one's recording posture, so only the surface differs
    other = Session()
    other._wal_path = str(tmp_path / "other.wal")
    other.load(_compile(MOVED), record=True, origin=_carried(MOVED))
    moved = other.snapshot()
    other.unload()
    same = stack.op("revl_snapshot")
    assert same.get("ok") is not False, same
    same = same.get("snapshot", same)

    assert stack.op("revl_unload", {"force": True})["ok"] is True
    refused = stack.op("revl_restore", {"snapshot": moved})
    _refused_for_the_surface(refused)
    assert stack.face.session.loaded is False
    restored = stack.op("revl_restore", {"snapshot": same})
    assert restored["ok"] is True, restored
    assert _version(stack) == (200, 1)
    assert _typed(stack) == (200, "v1:post:hi")


@needs_cordis
def test_an_operator_without_the_swap_grant_cannot_swap(stack):
    refused = stack.op("revl_swap", _carried(V2), who="reader")
    assert refused["ok"] is False, refused
    assert refused["authorized"] is False, refused
    assert refused["authority"] == {"operator": "reader", "verb": "swap",
                                    "allowed": False}, refused
    assert _version(stack) == (200, 1)
