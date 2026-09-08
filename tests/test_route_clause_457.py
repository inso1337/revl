"""The `route` clause and its bind table — roadmap item 457, Slice 1
(docs/design/457-endpoint-one-definition.md).

An endpoint is a service operation with a `route <method> "<path>"` clause: one
contextual production, no lexer change, no new keyword outside that position. The
clause resolves at compile time to a bind table (which parameter is a path
scalar / query / body / bearer / request) and a return classification, refusing —
naming the operation and the parameter — anything the projection cannot express
deterministically. This module pins the clause surface and every refusal.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.errors import RevlError, RevlErrors  # noqa: E402

# the exemplary declaration from the design note (§"The declaration"), minus the
# S2 auth stdlib: one service with a get/list/create/delete over a `Note`.
EXEMPLARY = """\
use "stdlib/http.rvl" { ApiError }

type Note = { id: Str, owner: Str, title: Str, body: Str }
type NewNote = { title: Str, body: Str }

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
"""


def _compile(tmp_path, source: str) -> dict:
    app = tmp_path / "app.rvl"
    app.write_text(source, encoding="utf-8")
    return compile_files([str(app)])


def _errmsg(exc) -> str:
    err = exc.value
    if isinstance(err, RevlErrors):
        return "\n".join(str(e) for e in err.errors)
    return str(err)


# ---------------------------------------------------------------- the surface

def test_exemplary_declares_a_typed_route(tmp_path):
    """462's exemplary app can declare a typed route: the clause compiles and the
    bind table + return classification ride the IR."""
    ir = _compile(tmp_path, EXEMPLARY)
    methods = ir["services"]["NotesApi"]["methods"]

    get_note = methods["get_note"]["route"]
    assert get_note["method"] == "get"
    assert get_note["path"] == "/notes/{id}"
    assert get_note["bind"]["id"]["kind"] == "path"
    assert get_note["bind"]["id"]["type"] == "Str"
    assert get_note["response"] == {"kind": "result", "ok": "Note"}

    list_notes = methods["list_notes"]["route"]
    assert list_notes["bind"]["limit"]["kind"] == "query"
    assert list_notes["bind"]["limit"]["optional"] is True

    create = methods["create_note"]["route"]
    assert create["method"] == "post"
    assert create["bind"]["note"]["kind"] == "body"
    assert create["bind"]["note"]["type"] == "NewNote"
    # the body schema is derived (item 257), so the boundary validates it
    assert create["bind"]["note"]["schema"]["type"] == "object"

    delete = methods["delete_note"]["route"]
    assert delete["response"] == {"kind": "result", "ok": "Unit"}


def test_route_is_contextual_not_a_keyword(tmp_path):
    """`route` outside the leading service-operation position is an ordinary
    name: no lexer change, so a program using `route` as a value is unaffected."""
    ir = _compile(tmp_path, """
type R = { route: Str }
fn pick(route: Str) -> Str { return route }
service S { fn f(route: Str) -> Str }
""")
    # `route` parsed as a field name, a parameter name and a fn name — no route IR
    assert "route" not in ir["services"]["S"]["methods"]["f"]


def test_unrouted_service_is_byte_identical(tmp_path):
    """A service with no `route` clause carries no `route` key (byte-identical)."""
    ir = _compile(tmp_path, "service S { fn f(x: Str) -> Str }")
    assert "route" not in ir["services"]["S"]["methods"]["f"]


def test_response_return_is_handler_owned(tmp_path):
    ir = _compile(tmp_path, """
use "stdlib/http.rvl" { Response }
service S { route get "/r" fn f() -> Response }
""")
    assert ir["services"]["S"]["methods"]["f"]["route"]["response"] == {
        "kind": "response"}


def test_plain_return_is_a_200(tmp_path):
    ir = _compile(tmp_path, """
type R = { a: Str }
service S { route get "/r/{id}" fn f(id: Str) -> R }
""")
    assert ir["services"]["S"]["methods"]["f"]["route"]["response"] == {
        "kind": "plain", "type": "R"}


# ---------------------------------------------------------------- the method set

def test_route_needs_a_known_method(tmp_path):
    with pytest.raises((RevlError, RevlErrors)) as exc:
        _compile(tmp_path, 'service S { route fetch "/x" fn f() -> Str }')
    assert "HTTP method" in _errmsg(exc)


def test_route_needs_a_path_string(tmp_path):
    with pytest.raises((RevlError, RevlErrors)) as exc:
        _compile(tmp_path, "service S { route get fn f() -> Str }")
    assert "path string" in _errmsg(exc)


# ---------------------------------------------------------------- bind refusals

def test_path_placeholder_must_be_a_parameter(tmp_path):
    with pytest.raises((RevlError, RevlErrors)) as exc:
        _compile(tmp_path, 'service S { route get "/x/{id}" fn f(name: Str) -> Str }')
    msg = _errmsg(exc)
    assert "S.f" in msg and "{id}" in msg


def test_path_parameter_must_be_scalar(tmp_path):
    with pytest.raises((RevlError, RevlErrors)) as exc:
        _compile(tmp_path, """
type R = { a: Str }
service S { route get "/x/{id}" fn f(id: R) -> Str }
""")
    assert "not a scalar" in _errmsg(exc)


def test_query_parameter_must_be_scalar_on_safe_verb(tmp_path):
    with pytest.raises((RevlError, RevlErrors)) as exc:
        _compile(tmp_path, """
type R = { a: Str }
service S { route get "/x" fn f(q: R) -> Str }
""")
    assert "query parameter `q`" in _errmsg(exc)


def test_one_record_body_on_unsafe_verb(tmp_path):
    with pytest.raises((RevlError, RevlErrors)) as exc:
        _compile(tmp_path, """
type R = { a: Str }
type Q = { b: Str }
service S { route post "/x" emission fn f(x: R, y: Q) -> Str }
""")
    msg = _errmsg(exc)
    assert "body candidates" in msg and "x, y" in msg


def test_body_must_be_a_record(tmp_path):
    with pytest.raises((RevlError, RevlErrors)) as exc:
        _compile(tmp_path, 'service S { route post "/x" emission fn f(b: Str) -> Str }')
    assert "not a record" in _errmsg(exc)


# ---------------------------------------------------------------- return refusal

def test_non_apierror_error_is_refused(tmp_path):
    with pytest.raises((RevlError, RevlErrors)) as exc:
        _compile(tmp_path, 'service S { route get "/x" fn f() -> Result[Str, Str] }')
    assert "needs a status" in _errmsg(exc)


# ---------------------------------------------------------------- authorization hook

def test_bearer_marks_the_operation_but_grants_nothing(tmp_path):
    """The S1 authorization HOOK: a `Bearer` parameter binds from the header and
    marks the op `auth: "bearer"`, so a later slice's `Auth` machinery slots in.
    The router grants nothing — no auth vocabulary rides the clause."""
    ir = _compile(tmp_path, """
type Bearer = { token: Opt[Str] }
type Note = { id: Str }
service S {
  route get "/notes/{id}"
  fn get_note(auth: Bearer, id: Str) -> Note
}
""")
    route = ir["services"]["S"]["methods"]["get_note"]["route"]
    assert route["auth"] == "bearer"
    assert route["bind"]["auth"]["kind"] == "header"
    # a route with no Bearer parameter carries no auth marker
    ir2 = _compile(tmp_path, """
type Note = { id: Str }
service S { route get "/notes/{id}" fn get_note(id: Str) -> Note }
""")
    assert "auth" not in ir2["services"]["S"]["methods"]["get_note"]["route"]


def test_at_most_one_bearer(tmp_path):
    with pytest.raises((RevlError, RevlErrors)) as exc:
        _compile(tmp_path, """
type Bearer = { token: Opt[Str] }
service S { route get "/x" fn f(a: Bearer, b: Bearer) -> Str }
""")
    assert "at most one" in _errmsg(exc)


# ---------------------------------------------------------------- stdlib ApiError

def test_apierror_and_constructors_are_in_http_stdlib(tmp_path):
    ir = _compile(tmp_path, """
use "stdlib/http.rvl" { ApiError, bad_request, unauthorized, forbidden,
                        not_found_error, conflict }
fn e1() -> ApiError { return bad_request("bad") }
fn e2() -> ApiError { return unauthorized("no") }
fn e3() -> ApiError { return forbidden("no") }
fn e4() -> ApiError { return not_found_error("gone") }
fn e5() -> ApiError { return conflict("dup") }
""")
    assert ir["types"]["ApiError"]["kind"] == "record"
    assert set(ir["types"]["ApiError"]["fields"]) == {"status", "code", "message"}
