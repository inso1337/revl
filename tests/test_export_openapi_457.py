"""`revl export openapi` — roadmap item 457, artifact 4
(docs/design/457-endpoint-one-definition.md).

The inverse of `revl import openapi` over the subset both express: one path item
per routed operation, the bind table as `parameters`/`requestBody`, the derived
schemas under `components/schemas`, `200`/`204` and the `ApiError` responses, and
the compiler's `x-revl-emission` hint. It carries NO `security` requirement (§4):
there is nothing in the `route` clause to derive one from. `revl import openapi`
of the exported document round-trips the service on the subset both express.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, compile_source  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.export_openapi import export_openapi  # noqa: E402
from revl.import_openapi import import_openapi_file  # noqa: E402

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


def _doc(tmp_path) -> dict:
    app = tmp_path / "app.rvl"
    app.write_text(APP, encoding="utf-8")
    ir = compile_files([str(app)])
    return json.loads(export_openapi(ir, service="NotesApi"))


def test_one_path_item_per_routed_operation(tmp_path):
    doc = _doc(tmp_path)
    assert doc["openapi"] == "3.1.0"
    assert doc["info"]["title"] == "NotesApi"
    assert set(doc["paths"]) == {"/notes/{id}", "/notes"}
    assert set(doc["paths"]["/notes/{id}"]) == {"get", "delete"}
    assert set(doc["paths"]["/notes"]) == {"get", "post"}


def test_bind_table_becomes_parameters_and_request_body(tmp_path):
    doc = _doc(tmp_path)
    get = doc["paths"]["/notes/{id}"]["get"]
    assert get["parameters"][0] == {
        "name": "id", "in": "path", "required": True,
        "schema": {"type": "string"}}
    lst = doc["paths"]["/notes"]["get"]
    assert lst["parameters"][0]["in"] == "query"
    assert lst["parameters"][0]["required"] is False
    post = doc["paths"]["/notes"]["post"]
    assert post["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/NewNote"}


def test_schemas_and_emission_hint(tmp_path):
    doc = _doc(tmp_path)
    schemas = doc["components"]["schemas"]
    assert "Note" in schemas and "NewNote" in schemas and "ApiError" in schemas
    assert doc["paths"]["/notes"]["post"]["x-revl-emission"] is True
    assert doc["paths"]["/notes/{id}"]["get"]["x-revl-emission"] is False
    # 200 + the ApiError default error
    assert "200" in doc["paths"]["/notes/{id}"]["get"]["responses"]
    assert "default" in doc["paths"]["/notes/{id}"]["get"]["responses"]
    # Unit -> 204
    assert "204" in doc["paths"]["/notes/{id}"]["delete"]["responses"]


def test_carries_no_security(tmp_path):
    """§4: the document derives NO `securitySchemes` and NO `security` from
    anything — there is nothing in the clause to derive one from."""
    doc = _doc(tmp_path)
    blob = json.dumps(doc).lower()
    assert "securityschemes" not in blob
    assert '"security"' not in blob
    components = doc.get("components", {})
    assert "securitySchemes" not in components


def test_auth_bearing_op_gets_documentation_only_marker(tmp_path):
    app = tmp_path / "auth.rvl"
    app.write_text("""
type Bearer = { token: Opt[Str] }
type Note = { id: Str }
service S { route get "/notes/{id}" fn get_note(auth: Bearer, id: Str) -> Note }
""", encoding="utf-8")
    ir = compile_files([str(app)])
    doc = json.loads(export_openapi(ir, service="S"))
    op = doc["paths"]["/notes/{id}"]["get"]
    assert op["x-revl-auth"] == "bearer"
    # still no security requirement — the marker grants nothing (the word
    # "security" appears only in the documentation-only note's prose)
    assert '"security"' not in json.dumps(doc)
    assert "securitySchemes" not in json.dumps(doc)
    assert "security" not in op  # not an OpenAPI `security` key on the op
    # the bearer is not surfaced as an OpenAPI parameter (it is transport)
    assert all(p["name"] != "auth" for p in op.get("parameters", []))


def test_import_round_trips_the_service_subset(tmp_path):
    """`revl import openapi` of the exported document reconstructs the service on
    the subset both express: operation names, path/query parameters (name+type),
    the body type, the success payload type, and the emission classification."""
    doc_path = tmp_path / "spec.json"
    app = tmp_path / "app.rvl"
    app.write_text(APP, encoding="utf-8")
    ir = compile_files([str(app)])
    doc_path.write_text(export_openapi(ir, service="NotesApi"), encoding="utf-8")

    source = import_openapi_file(str(doc_path), backend="py", service="NotesApi")
    reimported = compile_source(source)["services"]["NotesApi"]["methods"]

    # operation names survive (via operationId)
    assert set(reimported) == {"get_note", "list_notes", "create_note",
                               "delete_note"}
    # path param name + type
    assert reimported["get_note"]["params"] == [{"name": "id", "type": "Str"}]
    # the success payload type is the ok type of the original `Result`
    assert reimported["get_note"]["returns"] == "Note"
    assert reimported["list_notes"]["returns"] == "List[Note]"
    # query optionality survives
    assert reimported["list_notes"]["params"] == [
        {"name": "limit", "type": "Opt[Int]"}]
    # the body type survives (its param is renamed `body` — no name to carry)
    assert reimported["create_note"]["params"] == [
        {"name": "body", "type": "NewNote"}]
    assert reimported["create_note"]["returns"] == "Note"
    # emission classification survives (via x-revl-emission)
    assert reimported["get_note"]["emission"] is False
    assert reimported["create_note"]["emission"] is True
    assert reimported["delete_note"]["emission"] is True


def test_no_routed_operations_is_refused(tmp_path):
    app = tmp_path / "plain.rvl"
    app.write_text("service S { fn f(x: Str) -> Str }", encoding="utf-8")
    ir = compile_files([str(app)])
    with pytest.raises(RevlError) as exc:
        export_openapi(ir, service="S")
    assert "no routed operations" in str(exc.value)


def test_cli_writes_openapi_to_output(tmp_path, capsys):
    app = tmp_path / "app.rvl"
    app.write_text(APP, encoding="utf-8")
    out = tmp_path / "spec.json"
    code = main(["export", "openapi", str(app), "--service", "NotesApi",
                 "-o", str(out)])
    assert code == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["openapi"] == "3.1.0"
    assert "/notes/{id}" in doc["paths"]
