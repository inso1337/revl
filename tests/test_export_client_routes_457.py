"""`revl export client` is route-aware — roadmap item 457, artifact 5
(docs/design/457-endpoint-one-definition.md).

A routed operation becomes a typed method that builds the path from its scalar
arguments, puts the remaining arguments in the query or the JSON body per the
bind table, sends a `Bearer` as the `Authorization` header when the operation
declares one, and decodes the response into the canonical-encoding types.
Unrouted operations keep the canonical `POST` (byte-identical to before).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.export_client import export_client  # noqa: E402

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


def _client(tmp_path, source=APP, service="NotesApi") -> str:
    app = tmp_path / "app.rvl"
    app.write_text(source, encoding="utf-8")
    ir = compile_files([str(app)])
    return export_client(ir, service=service)


def test_routed_client_builds_the_path_and_fetches(tmp_path):
    ts = _client(tmp_path)
    # a routed service is a REST client: the constructor takes the base URL
    assert "constructor(private readonly base: string" in ts
    # the path is built from the scalar argument, encoded
    assert "let __path = `/notes/${encodeURIComponent(String(id))}`;" in ts
    assert 'method: "GET"' in ts
    assert "await fetch(`${this.base}${__path}`" in ts


def test_query_and_body_binding(tmp_path):
    ts = _client(tmp_path)
    # the optional query arg is set only when present, then appended
    assert 'if (limit !== null && limit !== undefined) __q.set("limit", String(limit));' in ts
    # the body arg is JSON-serialized with a JSON content type
    assert '__headers["Content-Type"] = "application/json";' in ts
    assert "body: JSON.stringify(note)" in ts


def test_result_return_is_the_tagged_union(tmp_path):
    ts = _client(tmp_path)
    assert '{ "$kind": "Ok", "$value": Note }' in ts
    # the Err value is the {status, code, message} shape the router renders
    assert '"$value": { status: number, code: string, message: string }' in ts
    # the decode reconstructs Ok/Err from the HTTP status
    assert "if (__res.ok) {" in ts


def test_bearer_becomes_the_authorization_header(tmp_path):
    ts = _client(tmp_path, source="""
type Bearer = { token: Opt[Str] }
type Note = { id: Str }
service S {
  route get "/notes/{id}"
  fn get_note(auth: Bearer, id: Str) -> Note
}
component H provides s: S {
  provide s { fn get_note(auth, id) = { id: id } }
}
""", service="S")
    assert '__headers["Authorization"] = `Bearer ${auth.token}`;' in ts
    # the bearer is not a query/body arg — it is transport
    assert "__q.set(\"auth\"" not in ts


def test_unrouted_service_keeps_the_canonical_post(tmp_path):
    """A service with no `route` clause is byte-identical to before: the canonical
    `transport.call` client, no REST machinery."""
    ts = _client(tmp_path, source="""
service Inventory { fn lookup(sku: Str) -> Opt[Str] }
component W provides inv: Inventory {
  let store = effect Map.new() undo store.drop()
  provide inv { fn lookup(sku) = store.get(sku) }
}
""", service="Inventory")
    assert "constructor(private readonly transport: Transport) {}" in ts
    assert 'await this.transport.call("lookup", [sku])' in ts
    assert "fetch(`${this.base}" not in ts


# -- the constructor declares only what a method can read (gap G4) -----------
#
# docs/webapp-competitiveness-report.md filed G4 against this generator: a FULLY
# routed client still declared `private readonly transport`, which no method
# reads, so a consumer compiling the artifact under `noUnusedLocals` got
# `error TS6138` in the one file it is told not to edit
# (`examples/app/frontend/notes.client.ts`). The three tests below are the whole
# rule, stated on all three shapes a service can have.

_MIXED = """\
use "stdlib/http.rvl" { ApiError }

type Note = { id: Str }

service Mixed {
  route get "/notes/{id}"
  fn get_note(id: Str) -> Result[Note, ApiError]

  fn count() -> Int
}

component MixedHost provides mixed: Mixed {
  provide mixed {
    fn get_note(id) = Ok({ id: id })
    fn count() = 0
  }
}
"""


def test_a_fully_routed_client_declares_no_transport(tmp_path):
    """Every operation on `NotesApi` is routed, so every method builds its own
    request from `base` and NOTHING reads a transport. Declaring one anyway is
    an unread private property in a generated artifact — gap G4. The constructor
    takes `base` alone."""
    ts = _client(tmp_path)
    assert "constructor(private readonly base: string) {}" in ts
    assert "private readonly transport" not in ts
    # and the reason it is safe to drop: no method reaches for it.
    assert "this.transport" not in ts


def test_a_mixed_client_still_declares_the_transport_it_reads(tmp_path):
    """The anti-vacuity control for the test above: the parameter is dropped
    because it is UNREAD, not because the generator stopped emitting transports.
    A service with one routed and one unrouted operation still declares it, with
    the unrouted operation reading it."""
    ts = _client(tmp_path, source=_MIXED, service="Mixed")
    assert "private readonly transport: Transport = " in ts
    assert 'await this.transport.call("count", [])' in ts
    # the routed half is unchanged
    assert "private readonly base: string" in ts
    assert "fetch(`${this.base}" in ts


def test_no_generated_client_declares_a_property_no_method_reads(tmp_path):
    """The property G4 is an instance of, checked directly rather than through
    one constructor spelling: every `private readonly <name>` the generator
    declares is read as `this.<name>` somewhere in the same file. Run over all
    three service shapes, so a future parameter cannot reintroduce the gap under
    a different name."""
    import re

    shapes = (
        (APP, "NotesApi"),
        (_MIXED, "Mixed"),
        ("""
service Inventory { fn lookup(sku: Str) -> Opt[Str] }
component W provides inv: Inventory {
  let store = effect Map.new() undo store.drop()
  provide inv { fn lookup(sku) = store.get(sku) }
}
""", "Inventory"),
    )
    for source, service in shapes:
        ts = _client(tmp_path, source=source, service=service)
        declared = set(re.findall(r"private readonly (\w+)", ts))
        assert declared, f"{service}: no parameter properties found at all"
        for name in sorted(declared):
            assert f"this.{name}" in ts, (
                f"{service}Client declares `private readonly {name}` and no "
                f"method reads `this.{name}`: that is `error TS6138` under a "
                "consumer's noUnusedLocals (gap G4)."
            )
