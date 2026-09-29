"""`revl serve` serves a public surface, and never decodes an authority value.

Item 569 slice B1 (docs/design/569-serve-http-caller-identity.md), issues #1502
and #1503. Both faces used to serve every provided key of every component and
to decode every parameter from the request, so:

  * an anonymous `POST /revl/store/get [{"subject": "alice"}, "n1"]` forged the
    `Principal` only `Auth.validate` may mint and read alice's note (#1502);
  * an anonymous `POST /revl/admission/admit` chose the `Trusted[List[Str]]`
    grant list of `stdlib/admit.rvl` and admitted code (#1503).

Pinned here: an operation with an authority parameter is withheld from both
faces and refused by name; the HTTP face serves the routed operations plus an
explicitly public set, and its manifest lists exactly that; routed endpoints
behave as before. The stub-session tests run with no runtime; the live test
needs the cordis-py runtime.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.mcp.composed import ComposedServer  # noqa: E402
from revl.mcp.http_face import HttpComposedServer  # noqa: E402
from revl.mcp.surface import (  # noqa: E402
    AUTHORITY_CATEGORY, declared_param_types, withheld_operations,
)

AUTH = str(ROOT / "stdlib" / "auth.rvl")
ADMIT = str(ROOT / "stdlib" / "admit.rvl")

# The shape docs/design/457-endpoint-one-definition.md prescribes: a
# user-scoped store that takes a `Principal`, reached only through a routed
# handler that runs `auth.validate`. The store returns the note to its owner.
NOTES_APP = """\
use "stdlib/http.rvl" { ApiError }
use "stdlib/auth.rvl" { Auth, Bearer }

type Note = { id: Str, owner: Str, body: Str }

extern pure fn owned_note(who: Principal, id: Str) -> Result[Note, ApiError] = @py {
    subject = who.get("subject") if isinstance(who, dict) else None
    notes = {"n1": {"id": "n1", "owner": "alice", "body": "alice's note"}}
    n = notes.get(id)
    if n is None or n["owner"] != subject:
        return Err({"status": 404, "code": "not_found", "message": "no such note"})
    return Ok(n)
}

service NoteStore {
  fn get(who: Principal, id: Str) -> Result[Note, ApiError]
}

component Store provides store: NoteStore {
  provide store {
    fn get(who, id) = owned_note(who, id)
  }
}

service NotesApi {
  route get "/notes/{id}"
  fn get_note(bearer: Bearer, id: Str) -> Result[Note, ApiError]
}

component NotesHttp requires auth: Auth, store: NoteStore
                    provides notes_api: NotesApi {
  provide notes_api {
    fn get_note(bearer, id) {
      return match auth.validate(bearer) {
        Err(e) => Err(e),
        Ok(who) => store.get(who, id),
      }
    }
  }
}
"""

TURN = ("service Turn { fn run() -> Str }\n"
        "component T provides turn: Turn { provide turn { fn run() = \"x\" } }\n")
FORGED = {"subject": "alice", "minted_by": "the request body"}


class Ok:  # an emitted `Result` case, as the face recognises it by name
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


class _Stub:
    """A Session stand-in: the compiled IR, and a record of every call."""

    def __init__(self, ir, result=None):
        self.ir = ir
        self.calls = []
        self._result = result

    def call(self, key, method, args, *, raw=False):
        self.calls.append((key, method, args))
        return {"result": self._result, "trace": []}

    def state(self, drain: bool = False):
        return {"loaded": True}


def _files(tmp_path, source: str, *extra: str) -> list[str]:
    app = tmp_path / "app.rvl"
    app.write_text(source, encoding="utf-8")
    return [str(app), *extra]


def _notes(tmp_path):
    files = _files(tmp_path, NOTES_APP, AUTH)
    return compile_files(files), declared_param_types(files)


def _admit():
    return compile_files([ADMIT]), declared_param_types([ADMIT])


def _post(face, path, args):
    return face.dispatch_http("POST", path, json.dumps(args).encode(), {})


def _refusal(reply) -> str:
    payload = json.loads(reply.body)
    assert payload["ok"] is False
    diagnostic = payload["diagnostics"][0]
    assert diagnostic["category"] == AUTHORITY_CATEGORY
    return diagnostic["message"]


def _mcp_call(server, name, arguments):
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments}})


def _mcp_tools(server) -> set:
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return {tool["name"] for tool in reply["result"]["tools"]}


# ------------------------------------------------ #1502: the forged principal

def test_forged_principal_on_the_canonical_path_is_refused_by_name(tmp_path):
    ir, declared = _notes(tmp_path)
    stub = _Stub(ir, Ok({"id": "n1"}))
    face = HttpComposedServer(stub, declared=declared)
    reply = _post(face, "/revl/store/get", [FORGED, "n1"])
    assert reply.status == 403
    message = _refusal(reply)
    assert "`store.get`" in message
    assert "`who`" in message and "`Principal`" in message
    assert "Auth.validate" in message
    assert stub.calls == []


def test_forged_principal_is_refused_even_when_declared_public(tmp_path):
    # an explicit public marking never un-withholds an authority parameter
    ir, declared = _notes(tmp_path)
    stub = _Stub(ir)
    face = HttpComposedServer(stub, declared=declared,
                              public={("store", "get")})
    reply = _post(face, "/revl/store/get", [FORGED, "n1"])
    assert reply.status == 403
    assert stub.calls == []


def test_forged_principal_over_mcp_is_refused_by_name(tmp_path):
    ir, declared = _notes(tmp_path)
    stub = _Stub(ir)
    server = ComposedServer(stub, declared=declared)
    assert "revl.store.get" not in _mcp_tools(server)
    reply = _mcp_call(server, "revl.store.get", {"who": FORGED, "id": "n1"})
    assert reply["error"]["code"] == -32602
    assert "`who`" in reply["error"]["message"]
    assert "`Principal`" in reply["error"]["message"]
    assert stub.calls == []


def test_a_principal_inside_a_record_is_withheld_with_no_declared_types(tmp_path):
    # read from the IR alone: a record that carries a Principal is one
    source = ("type Box = { who: Principal, id: Str }\n"
              "service S { fn get(b: Box) -> Str }\n"
              "component C provides s: S { provide s { fn get(b) = b.id } }\n")
    ir = compile_files(_files(tmp_path, source))
    withheld = withheld_operations(ir)
    assert set(withheld) == {("s", "get")}
    assert withheld[("s", "get")].type == "Box"


# ------------------------------------------ #1503: the caller-chosen grant

def test_admission_admit_over_http_is_refused_by_name():
    ir, declared = _admit()
    stub = _Stub(ir)
    face = HttpComposedServer(stub, declared=declared,
                              public={("admission", "admit")})
    reply = _post(face, "/revl/admission/admit", [TURN, ["Ops"]])
    assert reply.status == 403
    message = _refusal(reply)
    assert "`admission.admit`" in message
    assert "`granted`" in message and "`Trusted[List[Str]]`" in message
    assert stub.calls == []


def test_admission_admit_over_mcp_is_refused_by_name():
    ir, declared = _admit()
    stub = _Stub(ir)
    server = ComposedServer(stub, declared=declared)
    assert "revl.admission.admit" not in _mcp_tools(server)
    reply = _mcp_call(server, "revl.admission.admit",
                      {"source": TURN, "granted": ["Ops"]})
    assert "`granted`" in reply["error"]["message"]
    assert stub.calls == []


def test_a_trusted_param_on_a_routed_operation_is_refused(tmp_path):
    # the route bind table decodes a `Trusted[Str]` path segment; the face
    # refuses the route by name instead of binding it
    source = ('service S {\n  route get "/grant/{name}"\n'
              "  fn grant(name: Trusted[Str]) -> Str\n}\n"
              "component C provides s: S { provide s { fn grant(name) = name } }\n")
    files = _files(tmp_path, source)
    stub = _Stub(compile_files(files), "granted")
    face = HttpComposedServer(stub, declared=declared_param_types(files))
    reply = face.dispatch_http("GET", "/grant/admin", b"", {})
    assert reply.status == 403
    assert "`Trusted[Str]`" in _refusal(reply)
    assert stub.calls == []
    manifest = json.loads(face.dispatch_http("GET", "/", b"", {}).body)
    assert manifest["routes"] == []


def test_declared_types_follow_a_named_import(tmp_path):
    # the `Trusted[...]` is declared in an imported module, not the root
    (tmp_path / "grant.rvl").write_text(
        "service Grant { fn grant(names: Trusted[List[Str]]) -> Str }\n",
        encoding="utf-8")
    source = ('use "grant.rvl" { Grant }\n'
              "component C provides g: Grant { provide g { fn grant(names) = \"x\" } }\n")
    files = _files(tmp_path, source)
    declared = declared_param_types(files)
    assert declared["Grant"]["grant"]["names"] == "Trusted[List[Str]]"
    withheld = withheld_operations(compile_files(files), declared)
    assert set(withheld) == {("g", "grant")}


# ------------------------------------------------------ the public surface

def test_manifest_lists_only_the_public_surface(tmp_path):
    ir, declared = _notes(tmp_path)
    face = HttpComposedServer(_Stub(ir), declared=declared)
    manifest = json.loads(face.dispatch_http("GET", "/", b"", {}).body)
    assert manifest["operations"] == []
    assert manifest["routes"] == [{"method": "GET", "path": "/notes/{id}",
                                   "key": "notes_api", "operation": "get_note",
                                   "auth": "bearer"}]
    text = json.dumps(manifest)
    for internal in ("store", "validate", "auth/"):
        assert internal not in text


def test_an_unrouted_internal_operation_is_not_served(tmp_path):
    ir, declared = _notes(tmp_path)
    stub = _Stub(ir)
    face = HttpComposedServer(stub, declared=declared)
    reply = _post(face, "/revl/auth/validate", [{"token": "alice"}])
    assert reply.status == 404
    assert "public surface" in json.loads(reply.body)["diagnostics"][0]["message"]
    # the routed operation is not on the canonical path either
    assert _post(face, "/revl/notes_api/get_note",
                 [{"token": "alice"}, "n1"]).status == 404
    assert stub.calls == []


def test_the_public_set_serves_the_canonical_path(tmp_path):
    ir, declared = _notes(tmp_path)
    stub = _Stub(ir, "ok")
    face = HttpComposedServer(stub, declared=declared,
                              public={("auth", "validate")})
    reply = _post(face, "/revl/auth/validate", [{"token": "alice"}])
    assert reply.status == 200
    assert stub.calls == [("auth", "validate", [{"token": "alice"}])]
    manifest = json.loads(face.dispatch_http("GET", "/", b"", {}).body)
    assert [op["path"] for op in manifest["operations"]] == ["/revl/auth/validate"]


def test_the_routed_endpoint_behaves_as_before(tmp_path):
    ir, declared = _notes(tmp_path)
    stub = _Stub(ir, Ok({"id": "n1", "owner": "alice", "body": "b"}))
    face = HttpComposedServer(stub, declared=declared)
    reply = face.dispatch_http("GET", "/notes/n1", b"",
                               {"Authorization": "Bearer alice"})
    assert reply.status == 200
    assert json.loads(reply.body) == {"id": "n1", "owner": "alice", "body": "b"}
    assert stub.calls == [("notes_api", "get_note", [{"token": "alice"}, "n1"])]
    # the bearer is still bound untouched, a missing one as no claim
    face.dispatch_http("GET", "/notes/n2", b"", {})
    assert stub.calls[-1] == ("notes_api", "get_note", [{"token": None}, "n2"])


# ------------------------------------------------------ CLI wiring

def _capture(monkeypatch, module: str, name: str) -> dict:
    seen: dict = {}

    def fake(ir, config=None, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(f"revl.mcp.{module}.{name}", fake)
    return seen


@pytest.mark.parametrize("flag, module, name", [
    ("--http", "http_face", "serve_http"),
    ("--mcp", "composed", "serve_composition"),
])
def test_serve_hands_both_faces_the_declared_types(monkeypatch, flag, module,
                                                   name):
    seen = _capture(monkeypatch, module, name)
    assert main(["serve", flag, ADMIT]) == 0
    assert seen["declared"]["Admission"]["admit"]["granted"] == \
        "Trusted[List[Str]]"


# ------------------------------------------------------ live (needs runtime)

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="standing a live composition up needs the cordis-py runtime; install "
           "it with `sh backends/python/setup.sh`")


@needs_runtime
def test_live_forged_principal_is_refused_and_the_route_still_serves(tmp_path):
    from revl.mcp.session import Session

    ir, declared = _notes(tmp_path)
    session = Session()
    session.load(ir, {}, origin=None)
    try:
        face = HttpComposedServer(session, declared=declared)
        forged = _post(face, "/revl/store/get", [FORGED, "n1"])
        assert forged.status == 403
        assert face.dispatch_http("GET", "/notes/n1", b"", {}).status == 401
        # the py auth stub maps a bearer token to that subject
        owner = face.dispatch_http("GET", "/notes/n1", b"",
                                   {"Authorization": "Bearer alice"})
        assert owner.status == 200
        assert json.loads(owner.body)["owner"] == "alice"
        other = face.dispatch_http("GET", "/notes/n1", b"",
                                   {"Authorization": "Bearer mallory"})
        assert other.status == 404
    finally:
        session.unload()

