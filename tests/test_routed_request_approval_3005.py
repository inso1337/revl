"""A routed operation whose argument carries a native variant value can be
approved (issue #3005).

Routed dispatch binds a `Request` parameter as a record holding NATIVE variant
cases (`method` as `Method.Get`, `body` as `Empty`). Under an approval policy the
class-(c) ticket's `argsDigest` canonicalised the argument list with a bare
`json.dumps`, which raised on the native case, so the face answered 500 and the
crossing could never be approved. The digest now canonicalises a native value
through the face's canonical wire encoding (`{"$kind": ..., "$value": ...}`), so
the routed request gets its 403 ticket and the identical re-issue fires once an
operator approves it. JSON-able arguments digest byte for byte as before.

The live tests drive a real cordis-py composition and skip without the pinned
fork; a skip is not a pass.
"""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:  # `runtime`, imported only by cordis tests
    sys.path.insert(0, str(_BACKEND))

from revl import compile_files  # noqa: E402
from revl.mcp.approval import _args_digest  # noqa: E402
from revl.mcp.http_face import HttpComposedServer, _live_decoder  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="booting a composition needs the cordis-py runtime; install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)

APP = '''
use "stdlib/http.rvl" { Request, Response, header }

extern emission[probe] fn probe_fire(msg: Str) -> Str = @py {
    import os
    with open(os.environ["PROBE_SINK_3005"], "a") as f:
        f.write(msg + "\\n")
    return msg
}

fn answer(payload: Str) -> Response {
  return { status: 200, status_text: "OK",
           headers: [header("content-type", "application/json")],
           body: Json(payload) }
}

service Probe {
  route get "/fire"
  emission fn fire(msg: Str) -> Response

  route get "/fire-req"
  emission fn fire_req(req: Request) -> Response

  route post "/post-req"
  emission fn post_req(req: Request) -> Response
}

component ProbeHttp provides probe: Probe {
  provide probe {
    fn fire(msg) { emit probe_fire(msg) return answer("{}") }
    fn fire_req(req) { emit probe_fire(req.path) return answer("{}") }
    fn post_req(req) { emit probe_fire(req.path) return answer("{}") }
  }
}
'''


@pytest.fixture
def face(tmp_path, monkeypatch):
    from revl.mcp.session import Session

    sink = tmp_path / "sink.log"
    monkeypatch.setenv("PROBE_SINK_3005", str(sink))
    app = tmp_path / "probe_3005.rvl"
    app.write_text(APP, encoding="utf-8")
    ir = compile_files([str(app)])
    session = Session()
    session._wal_path = str(tmp_path / "session.wal")
    session.approval_policy = "auto"
    session.load(ir, record=True, origin={"source": APP})
    # as `serve_http` wires it: the live decoder, so a bound `Request` carries
    # NATIVE `method`/`body` cases (the shape that broke the digest)
    built = HttpComposedServer(session, composition="app",
                               decode=_live_decoder(session),
                               decoder_for=_live_decoder)
    built.sink = sink
    yield built
    import runtime as rt

    try:
        if session.loaded and not session.halted:
            session.unload()
    finally:
        rt.clear_estop()
        rt._LIVE_FRAMES.clear()


def _fired(face) -> list:
    if not face.sink.exists():
        return []
    return face.sink.read_text(encoding="utf-8").splitlines()


def _held(face, method, path, body=b""):
    reply = face.dispatch_http(method, path, body, {})
    payload = json.loads(reply.body)
    assert reply.status == 403, (reply.status, payload)
    assert payload["code"] == "pending_approval", payload
    return payload["ticket"]["hash"]


def _ticket(face, ticket_hash) -> dict:
    return face.session._tickets[ticket_hash]


@needs_cordis
def test_a_routed_request_argument_is_held_then_fires_once_approved(face):
    ticket = _held(face, "GET", "/fire-req")
    assert _fired(face) == [], "held, not fired"
    # the identical re-issue asks the same question: the digest is deterministic
    assert _held(face, "GET", "/fire-req") == ticket

    face.session.approve_ticket(ticket)
    reply = face.dispatch_http("GET", "/fire-req", b"", {})
    assert reply.status == 200, (reply.status, reply.body)
    assert _fired(face) == ["/fire-req"], "fired once, on the approved digest"
    # the yes was spent: asking again is a new question with the same ticket
    assert _held(face, "GET", "/fire-req") == ticket
    assert _fired(face) == ["/fire-req"]


@needs_cordis
def test_a_different_request_body_is_a_different_question(face):
    empty = _held(face, "POST", "/post-req")
    text = _held(face, "POST", "/post-req", b"hello")
    other = _held(face, "POST", "/post-req", b"hellp")
    assert len({empty, text, other}) == 3
    # an approval of the bodiless request does not cover one with a body
    face.session.approve_ticket(empty)
    assert _held(face, "POST", "/post-req", b"hello") == text
    assert _fired(face) == []


@needs_cordis
def test_a_scalar_route_digest_is_unchanged(face):
    ticket = _held(face, "GET", "/fire?msg=hi")
    expected = "sha256:" + hashlib.sha256(b'["hi"]').hexdigest()
    assert _ticket(face, ticket)["argsDigest"] == expected
    face.session.approve_ticket(ticket)
    assert face.dispatch_http("GET", "/fire?msg=hi", b"", {}).status == 200
    assert _fired(face) == ["hi"]


# ------------------------------------------------------- the digest, no runtime

class Get:
    __slots__ = ()


class Post:
    __slots__ = ()


class Empty:
    __slots__ = ()


class Text:
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


class _Opaque:
    def __init__(self):
        self.handle = 7


def _req(method, body):
    return {"method": method, "path": "/p", "headers": [], "body": body}


def test_json_able_args_digest_byte_for_byte_as_before():
    for args in ([], ["hi"], [1, True, None, 2.5], [{"b": 1, "a": [1, "x"]}],
                 [{"$kind": "Get"}]):
        canon = json.dumps(list(args), sort_keys=True, separators=(",", ":"))
        assert _args_digest(args) == ("sha256:" + hashlib.sha256(
            canon.encode("utf-8")).hexdigest())


def test_a_native_case_digests_as_its_canonical_wire_encoding():
    native = [_req(Get(), Empty())]
    wire = [_req({"$kind": "Get"}, {"$kind": "Empty"})]
    assert _args_digest(native) == _args_digest(wire)
    assert _args_digest([_req(Get(), Text("x"))]) == _args_digest(
        [_req({"$kind": "Get"}, {"$kind": "Text", "$value": "x"})])


def test_distinct_native_values_never_digest_equal():
    digests = {
        _args_digest([_req(Get(), Empty())]),
        _args_digest([_req(Post(), Empty())]),
        _args_digest([_req(Get(), Text(""))]),
        _args_digest([_req(Get(), Text("a"))]),
        _args_digest([_req(Get(), Text("b"))]),
    }
    assert len(digests) == 5


def test_an_unencodable_argument_is_refused_by_name_not_stringified():
    from revl.mcp.approval import UnencodableTicketArgs

    with pytest.raises(UnencodableTicketArgs, match="_Opaque"):
        _args_digest([_Opaque()])


def test_the_session_refuses_an_unencodable_argument_as_a_session_error():
    from revl.mcp.session import SessionError, _session_args_digest

    with pytest.raises(SessionError, match=r"`probe\.fire`.*_Opaque"):
        _session_args_digest([_Opaque()], "probe", "fire")
