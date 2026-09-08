"""The exemplary revl web application — the BORING HALF (item 462, issue #725).

Slice 1 of the 462 milestone (design docs/design/525-exemplary-web-app.md): a
small typed notes CRUD service written the obvious way over the primitives that
now exist — the `route` clause (item 457) over stdlib/http.rvl (item 456), and
the model-store persistence pattern (item 465, examples/model_store.rvl). No
differentiator and no frontend here; those are later slices.

The checks mirror the split the suite already uses for an exemplary example:

* frontend/IR assertions ALWAYS run — the app compiles, the three endpoints
  carry a derived `route` table, the store is model-store shaped (`create` is an
  emission lowered to a revertible effect whose inverse is the `remove`
  counterpart), and the source carries ZERO routing sentinels / emitter
  workarounds (525 acceptance bar 2 / item 457 exit §5);

* HTTP-routing assertions ALWAYS run, runtime-free through a stub session, the
  way tests/test_serve_http_routes_457.py drives the router: validation 400s
  (naming the field, handler never invoked), 404 for an unmatched path, 405 for
  a matched path with the wrong method, and the success/`ApiError` return
  mapping — status and outcome read through the typed contract, never prose; and

* the lifecycle assertion runs on the real cordis-py runtime when it is
  installed (`sh backends/python/setup.sh`), proving create/get/list actually
  persist and that teardown reverts residue-free — the same driver harness
  tests/test_model_store_752.py uses.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.mcp.http_face import HttpComposedServer  # noqa: E402

APP = ROOT / "examples" / "app" / "notes.rvl"
CORDIS_PY = ROOT / "backends" / "python" / ".venv" / "bin" / "python"


# --------------------------------------------------------------- frontend / IR

def test_app_compiles_with_a_derived_route_table():
    ir = compile_files([str(APP)])
    ops = ir["services"]["NotesApi"]["methods"]
    routed = {op: m["route"] for op, m in ops.items() if m.get("route")}
    assert (routed["get_note"]["method"], routed["get_note"]["path"]) == ("get", "/notes/{id}")
    assert (routed["list_notes"]["method"], routed["list_notes"]["path"]) == ("get", "/notes")
    assert (routed["create_note"]["method"], routed["create_note"]["path"]) == ("post", "/notes")
    # the path scalar, the optional query and the record body are each bound and
    # given a derived schema — the validation the router runs before the handler.
    assert routed["get_note"]["bind"]["id"]["kind"] == "path"
    assert routed["list_notes"]["bind"]["limit"]["kind"] == "query"
    assert routed["list_notes"]["bind"]["limit"]["optional"] is True
    assert routed["create_note"]["bind"]["note"]["kind"] == "body"
    # status/outcome is read from the return TYPE (Result[T, ApiError]).
    assert routed["get_note"]["response"] == {"kind": "result", "ok": "Note"}


def test_model_declares_the_row_types():
    ir = compile_files([str(APP)])
    assert "Note" in ir["types"]
    assert "NewNote" in ir["types"]


def test_store_is_model_store_shaped_typed_crud():
    """Persistence is typed CRUD over the declared row: `create` is an emission
    (a witnessed mutation); `get`/`all`/`size` are pure reads. No SQL."""
    ir = compile_files([str(APP)])
    store = ir["services"]["NoteStore"]["methods"]
    assert store["create"]["emission"] is True
    assert store["get"]["emission"] is False
    assert store["all"]["emission"] is False
    assert store["get"]["returns"] == "Opt[Note]"
    assert store["all"]["returns"] == "List[Note]"
    assert "sql" not in json.dumps(ir).lower()


def test_create_lowers_to_a_revertible_effect_with_the_remove_inverse():
    """The 525/model-store recovery story: every persistence mutation is a
    revertible effect whose inverse is the natural CRUD counterpart — `create`
    (an insert) carries `undo remove`, so a row written on activation reverts in
    LIFO order on teardown/divert, residue-free."""
    ir = compile_files([str(APP)])
    comp = next(c for c in ir["components"] if c["name"] == "MemoryStore")
    provide = next(s for s in comp["body"]
                   if s.get("step") == "provide" and s.get("name") == "store")
    create = next(m for m in provide["methods"] if m["name"] == "create")
    effects = [s for s in create["body"] if s.get("step") == "effect"]
    assert len(effects) == 1, "create must be exactly one revertible effect"
    assert effects[0]["acquire"]["method"] == "insert"
    assert effects[0]["undo"]["method"] == "remove"


def test_zero_sentinels_and_no_emitter_workarounds_in_the_app_source():
    """525 acceptance bar 2 / item 457 exit §5, counted with the same line scan
    tests/test_http_stdlib.py uses: the routing sentinels (`""`, `"::empty::"`)
    and the named emitter workarounds (`maybe_run`/`maybe_ship`) may appear only
    in explanatory prose, never on a code line. The boring half needs none of
    them; every place one would be required is a gap filed against 456–461
    (docs/design/525-webapp-slice1-gaps.md), not absorbed here."""
    forbidden = ('""', "::empty::", "maybe_run", "maybe_ship")
    for n, raw in enumerate(APP.read_text(encoding="utf-8").splitlines(), 1):
        code = raw.split("//", 1)[0]
        if not code.strip():
            continue
        for tok in forbidden:
            assert tok not in code, f"sentinel/workaround {tok!r} on line {n}: {raw!r}"


# --------------------------------------------------------------- HTTP routing

# The canonical-encoding result wrappers the face recognises by class NAME
# (`Ok`/`Err`), exactly as tests/test_serve_http_routes_457.py drives them.
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
    can assert the handler was — or was NOT — invoked (the shape
    tests/test_serve_http_routes_457.py uses)."""

    def __init__(self, ir, result):
        self.ir = ir
        self._result = result
        self.calls = []

    def call(self, key, method, args, *, raw=False):
        self.calls.append((key, method, args))
        return {"result": self._result, "trace": []}

    def state(self, drain=False):
        return {}


def _face(result):
    ir = compile_files([str(APP)])
    return HttpComposedServer(_Stub(ir, result), composition="revl")


def _body(reply):
    return json.loads(reply.body) if reply.body else None


def test_get_by_id_reaches_the_handler_and_answers_200():
    face = _face(Ok({"id": "note-0", "title": "t", "body": "b"}))
    reply = face.dispatch_http("GET", "/notes/note-0", b"", {})
    assert reply.status == 200
    assert _body(reply)["id"] == "note-0"
    assert face.session.calls[-1] == ("notes_api", "get_note", ["note-0"])


def test_bad_query_is_400_naming_the_field_handler_not_invoked():
    face = _face(Ok([]))
    reply = face.dispatch_http("GET", "/notes?limit=x", b"", {})
    assert reply.status == 400
    assert "limit" in json.dumps(_body(reply))
    assert face.session.calls == []


def test_absent_optional_query_binds_none():
    face = _face(Ok([]))
    reply = face.dispatch_http("GET", "/notes", b"", {})
    assert reply.status == 200
    assert face.session.calls[-1] == ("notes_api", "list_notes", [None])


def test_body_missing_required_is_400_and_handler_not_invoked():
    face = _face(Ok({"id": "note-0"}))
    reply = face.dispatch_http("POST", "/notes", b'{"body":"b"}', {})
    assert reply.status == 400
    assert "title" in json.dumps(_body(reply))
    assert face.session.calls == []  # validation is BEFORE the handler


def test_valid_body_reaches_the_handler():
    face = _face(Ok({"id": "note-0", "title": "t", "body": "b"}))
    reply = face.dispatch_http("POST", "/notes", b'{"title":"t","body":"b"}', {})
    assert reply.status == 200
    key, op, args = face.session.calls[-1]
    assert (key, op) == ("notes_api", "create_note")
    assert args == [{"title": "t", "body": "b"}]


def test_wrong_method_is_405():
    face = _face(Ok({}))
    reply = face.dispatch_http("PUT", "/notes", b"{}", {})
    assert reply.status == 405


def test_unmatched_path_is_404():
    face = _face(Ok({}))
    reply = face.dispatch_http("GET", "/nothing", b"", {})
    assert reply.status == 404


def test_err_maps_status_code_and_message_from_the_type():
    face = _face(Err({"status": 404, "code": "not_found",
                       "message": "no note with that id"}))
    reply = face.dispatch_http("GET", "/notes/x", b"", {})
    assert reply.status == 404
    payload = _body(reply)
    assert payload == {"code": "not_found", "message": "no note with that id"}
    assert "status" not in payload  # the HTTP code IS the status, not a field


def test_manifest_lists_the_routes_and_derives_no_security():
    face = _face(Ok(None))
    reply = face.dispatch_http("GET", "/", b"", {})
    assert reply.status == 200
    manifest = _body(reply)
    routed = {(r["method"], r["path"]) for r in manifest["routes"]}
    assert ("GET", "/notes/{id}") in routed
    assert ("GET", "/notes") in routed
    assert ("POST", "/notes") in routed
    assert "security" not in json.dumps(manifest).lower()


# --------------------------------------------------------------- runtime (gated)

@pytest.mark.skipif(
    not CORDIS_PY.exists(),
    reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")
def test_crud_persists_and_reverts_residue_free_on_the_runtime():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [str(CORDIS_PY), "-m", "revl", "test", str(APP)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS create then get returns the stored note" in result.stdout
    assert "PASS get of an absent id is a typed 404 ApiError" in result.stdout
    assert "PASS list returns created rows and a reloaded store is empty" in result.stdout
    # the differentiated half's baseline lifecycle test (the hot-swap legs need
    # `Session.swap`, so they live in tests/test_app_hotswap_725.py, not here).
    assert ("PASS ranker records engagement, scores by strategy, reverts "
            "residue-free" in result.stdout)
    assert "[py] pass: 4 test(s) passed" in result.stdout
