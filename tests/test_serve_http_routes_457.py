"""`revl serve --http` honours `route` clauses — roadmap item 457, artifact 1
(docs/design/457-endpoint-one-definition.md).

A provided operation that heads a `route` clause is reachable at its declared
method+path: the router binds every parameter from the path/query/body/header
per the compiler's bind table, VALIDATES each bound input against its derived
schema BEFORE the handler runs (item 257; a failure is `400` naming the field
and the handler is never invoked), then maps the handler's return per the return
rules. This is the exit test (§"Exit test", 1), driven runtime-free with a stub
session — the wire layer is a pure function of the request, exactly as the
canonical fourth-quadrant dispatch is.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.mcp.http_face import HttpComposedServer  # noqa: E402

APP = """\
use "stdlib/http.rvl" { ApiError }

type Note = { id: Str, owner: Str, title: Str, body: Str }
type NewNote = { title: Str, body: Str }

extern pure fn done() -> Unit = @py { return None }

service NotesApi {
  route get "/notes/{id}"
  fn get_note(id: Str) -> Result[Note, ApiError]

  route get "/notes"
  fn list_notes(limit: Opt[Int]) -> Result[List[Note], ApiError]

  route post "/notes"
  emission fn create_note(note: NewNote) -> Result[Note, ApiError]

  route delete "/notes/{id}"
  emission fn delete_note(id: Str) -> Result[Unit, ApiError]
}

component NotesHttp provides notes_api: NotesApi {
  provide notes_api {
    fn get_note(id) = Ok({ id: id, owner: "u", title: "t", body: "b" })
    fn list_notes(limit) = Ok([])
    fn create_note(note) = Ok({ id: "1", owner: "u", title: note.title, body: note.body })
    fn delete_note(id) = Ok(done())
  }
}
"""


class Ok:
    __slots__ = ("value",)

    def __init__(self, value=None):
        self.value = value


class Err:
    __slots__ = ("value",)

    def __init__(self, value=None):
        self.value = value


class _Stub:
    """A runtime-free session: it carries the compiled IR (so the route table is
    built) and returns a canned handler result, recording every call so a test
    can assert the handler was — or was NOT — invoked."""

    def __init__(self, ir, result):
        self.ir = ir
        self._result = result
        self.calls = []

    def call(self, key, method, args, *, raw=False):
        self.calls.append((key, method, args))
        return {"result": self._result, "trace": []}

    def state(self, drain=False):
        return {}


def _face(tmp_path, result):
    app = tmp_path / "app.rvl"
    app.write_text(APP, encoding="utf-8")
    ir = compile_files([str(app)])
    return HttpComposedServer(_Stub(ir, result), composition="revl")


import json  # noqa: E402


def _body(reply):
    return json.loads(reply.body) if reply.body else None


# ---------------------------------------------------------------- exit test (1)

def test_get_by_id_answers_the_note(tmp_path):
    face = _face(tmp_path, Ok({"id": "42", "owner": "u", "title": "t", "body": "b"}))
    reply = face.dispatch_http("GET", "/notes/42", b"", {})
    assert reply.status == 200
    assert _body(reply)["id"] == "42"
    # the path scalar bound and reached the handler
    assert face.session.calls[-1] == ("notes_api", "get_note", ["42"])


def test_bad_query_is_400_naming_the_field(tmp_path):
    face = _face(tmp_path, Ok([]))
    reply = face.dispatch_http("GET", "/notes?limit=x", b"", {})
    assert reply.status == 400
    assert "limit" in json.dumps(_body(reply))
    # the handler never ran
    assert face.session.calls == []


def test_optional_query_absent_is_none(tmp_path):
    face = _face(tmp_path, Ok([]))
    reply = face.dispatch_http("GET", "/notes", b"", {})
    assert reply.status == 200
    assert face.session.calls[-1] == ("notes_api", "list_notes", [None])


def test_body_missing_required_is_400_and_handler_not_invoked(tmp_path):
    face = _face(tmp_path, Ok({"id": "1"}))
    reply = face.dispatch_http("POST", "/notes", b'{"body":"b"}', {})
    assert reply.status == 400
    assert "title" in json.dumps(_body(reply))
    assert face.session.calls == []  # validation is BEFORE the handler


def test_valid_body_reaches_the_handler(tmp_path):
    face = _face(tmp_path, Ok({"id": "1", "owner": "u", "title": "t", "body": "b"}))
    reply = face.dispatch_http("POST", "/notes", b'{"title":"t","body":"b"}', {})
    assert reply.status == 200
    key, op, args = face.session.calls[-1]
    assert (key, op) == ("notes_api", "create_note")
    assert args == [{"title": "t", "body": "b"}]


def test_wrong_method_is_405(tmp_path):
    face = _face(tmp_path, Ok({"id": "1"}))
    reply = face.dispatch_http("PUT", "/notes", b"{}", {})
    assert reply.status == 405


def test_no_route_is_404(tmp_path):
    face = _face(tmp_path, Ok({"id": "1"}))
    reply = face.dispatch_http("GET", "/nothing", b"", {})
    assert reply.status == 404


# ---------------------------------------------------------------- return rules

def test_unit_ok_is_204(tmp_path):
    face = _face(tmp_path, Ok(None))
    reply = face.dispatch_http("DELETE", "/notes/9", b"", {})
    assert reply.status == 204
    assert reply.body == b""


def test_err_maps_status_code_and_message(tmp_path):
    face = _face(tmp_path, Err({"status": 404, "code": "not_found",
                                 "message": "no note 42"}))
    reply = face.dispatch_http("GET", "/notes/42", b"", {})
    assert reply.status == 404
    payload = _body(reply)
    assert payload == {"code": "not_found", "message": "no note 42"}
    # the wire error carries no `status` field (the HTTP code IS the status)
    assert "status" not in payload


# ---------------------------------------------------------------- manifest

def test_manifest_lists_routes(tmp_path):
    face = _face(tmp_path, Ok(None))
    reply = face.dispatch_http("GET", "/", b"", {})
    assert reply.status == 200
    manifest = _body(reply)
    routed = {(r["method"], r["path"]) for r in manifest["routes"]}
    assert ("GET", "/notes/{id}") in routed
    assert ("POST", "/notes") in routed
    # the manifest derives no `security` requirement anywhere
    assert "security" not in json.dumps(manifest).lower()


def test_bearer_binds_from_authorization_header(tmp_path):
    app = tmp_path / "auth.rvl"
    app.write_text("""
type Bearer = { token: Opt[Str] }
type Note = { id: Str }
service S {
  route get "/notes/{id}"
  fn get_note(auth: Bearer, id: Str) -> Note
}
component H provides s: S {
  provide s { fn get_note(auth, id) = { id: id } }
}
""", encoding="utf-8")
    ir = compile_files([str(app)])
    face = HttpComposedServer(_Stub(ir, Ok({"id": "1"})), composition="revl")
    reply = face.dispatch_http("GET", "/notes/7", b"",
                               {"Authorization": "Bearer tok-123"})
    assert reply.status == 200
    # the bearer bound UNTOUCHED as an untrusted claim (a record with the token)
    key, op, args = face.session.calls[-1]
    assert args[0] == {"token": "tok-123"}
    assert args[1] == "7"
