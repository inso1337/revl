"""`revl serve --http` — the SERVER face `revl export client` pairs with.

Item 424 gap (c) slice C1 (docs/design/424-dsh-language-gaps.md, D-424c.6): the
same fourth-quadrant projection `revl serve --mcp` makes of a booted
composition, on HTTP instead of stdio. Each provided operation is
`POST /<composition>/<key>/<op>`, and the request/response bodies are the
CANONICAL value encoding the four bridges speak (docs/interop-bridge.md), so a
value round-trips to the placement bridge — and to a generated `export client`
TS client — by construction.

Gated the way `test_mcp_serve.py` gates: the wire layer (routing, decode,
`_encode_value`, error mapping) is a pure function of the request and runs
everywhere, including over a real loopback socket with a stub session; only
standing a LIVE composition up needs the cordis-py runtime, so that test carries
`@needs_runtime`.
"""

import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.gate import gate_version  # noqa: E402
from revl.mcp.http_face import (  # noqa: E402
    HttpComposedServer, build_http_server, _encode_value,
)
from revl.mcp.session import Session  # noqa: E402

CACHE = """
service Cache { fn get(key: Str) -> Opt[Str]
                fn size() -> Int }
component MemCache provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache { fn get(key) = store.get(key)
                  fn size() = 0 }
}
"""

BUS = """
service Bus { emission fn send(msg: Str) -> Int
              fn depth() -> Int }
component B provides bus: Bus {
  let q = effect Map.new() undo q.drop()
  provide bus { fn send(msg) = 1
                fn depth() = 0 }
}
"""

NEEDS_CONFIG = """
service Store { fn get(key: Str) -> Opt[Str] }
component DB provides store: Store {
  config { url: Str }
  provide store { fn get(key) = key }
}
"""


class _StubSession:
    """Just enough of a Session for the runtime-free wire tests: it carries the
    compiled IR (so the projection runs) and returns a canned `result` instead
    of booting a cordis Context. `result_value` is what the operation 'returns'."""

    def __init__(self, source: str, result_value=0):
        self.ir = compile_source(source)
        self.calls: list[tuple] = []
        self._result_value = result_value

    def call(self, key, method, args):
        self.calls.append((key, method, args))
        return {"result": self._result_value, "trace": []}

    def state(self, drain: bool = False):
        return {"loaded": True}


def _face(source: str, **kw) -> tuple[HttpComposedServer, _StubSession]:
    session = _StubSession(source, **kw)
    return HttpComposedServer(session, composition="app"), session


# ------------------------------------------------------ the manifest (GET /)

def test_manifest_lists_operations_with_http_paths():
    face, _ = _face(CACHE)
    status, body = face.dispatch("GET", "/")
    assert status == 200
    paths = {op["path"] for op in body["operations"]}
    assert paths == {"/app/cache/get", "/app/cache/size"}
    assert body["composition"] == "app"


def test_manifest_carries_the_frontier_and_no_safety_claim():
    face, _ = _face(CACHE)
    _, body = face.dispatch("GET", "/")
    # item 338: the covered surface this face was projected under, first-class
    assert body["frontier"] == gate_version()["frontier"]
    assert body["frontier"]
    # D-424c.8: LOCAL contract only, no verified-remote badge
    note = body["note"].lower()
    assert "local contract only" in note
    assert "no claim" in note or "makes no claim" in note
    assert "no verified-remote badge" in note or "verified-remote" in note


def test_manifest_carries_the_compiler_derived_hints():
    # the fourth-quadrant guarantee rides HTTP too: read-only/emission is the
    # compiler's classification, not an author's assertion
    face, _ = _face(BUS)
    _, body = face.dispatch("GET", "/")
    by_path = {op["path"]: op for op in body["operations"]}
    assert by_path["/app/bus/send"]["emission"] is True
    assert by_path["/app/bus/send"]["readOnly"] is False
    assert by_path["/app/bus/depth"]["readOnly"] is True


# ------------------------------------------------------ POST dispatch

def test_post_routes_positional_args_to_session_call():
    face, session = _face(CACHE, result_value="hit")
    status, body = face.dispatch("POST", "/app/cache/get", b'["k9"]')
    assert status == 200
    assert session.calls == [("cache", "get", ["k9"])]
    # the placement bridge's exact reply shape
    assert body == {"ok": True, "value": "hit"}


def test_post_accepts_the_args_object_form_and_empty_body():
    face, session = _face(CACHE)
    face.dispatch("POST", "/app/cache/get", b'{"args": ["k"]}')
    assert session.calls[-1] == ("cache", "get", ["k"])
    # a no-argument call: an empty body is the empty argument list
    face.dispatch("POST", "/app/cache/size", b"")
    assert session.calls[-1] == ("cache", "size", [])


def test_unknown_route_is_404_and_names_the_manifest():
    face, _ = _face(CACHE)
    status, body = face.dispatch("POST", "/app/cache/nope", b"[]")
    assert status == 404
    assert body["ok"] is False
    assert "GET /" in body["diagnostics"][0]["message"]


def test_wrong_method_on_an_operation_is_405():
    face, _ = _face(CACHE)
    status, _ = face.dispatch("GET", "/app/cache/size")
    assert status == 405


def test_malformed_and_overlong_bodies_are_400():
    face, _ = _face(CACHE)
    status, body = face.dispatch("POST", "/app/cache/get", b"not json")
    assert status == 400
    assert "not JSON" in body["diagnostics"][0]["message"]
    # get(key) takes one argument; two is refused before the session is touched
    status, body = face.dispatch("POST", "/app/cache/get", b'["a", "b"]')
    assert status == 400
    assert "too many arguments" in body["diagnostics"][0]["message"]


def test_session_error_is_400_with_diagnostics():
    from revl.mcp.session import SessionError

    class _Bad(_StubSession):
        def call(self, key, method, args):
            raise SessionError("key 'cache' is not one the admitted turn provides")

    face = HttpComposedServer(_Bad(CACHE), composition="app")
    status, body = face.dispatch("POST", "/app/cache/get", b'["k"]')
    assert status == 400
    assert body["ok"] is False
    assert "admitted turn" in body["diagnostics"][0]["message"]


def test_class_c_crossing_is_403_with_the_ticket_not_an_opaque_fault():
    """A class-(c) crossing needs a human yes: fail-closed (nothing fired), the
    ticket handed back so an operator can mint the yes. This wire, like the MCP
    one, has no approve verb of its own."""
    from revl.mcp.approval import ApprovalRequired

    class _Raising(_StubSession):
        def call(self, key, method, args):
            raise ApprovalRequired({"key": key, "method": method,
                                    "hash": "sha256:deadbeef",
                                    "capabilities": ["send"]})

    face = HttpComposedServer(_Raising(BUS), composition="app")
    status, body = face.dispatch("POST", "/app/bus/send", b'["hi"]')
    assert status == 403
    assert body["approvalRequired"] is True
    assert body["ticket"]["hash"] == "sha256:deadbeef"
    assert "raised" not in body


# ------------------------------------------------------ canonical encoding

@dataclass
class _Item:
    sku: str
    qty: int


class _Found:  # an emitted single-payload case: slots-only, a `value` payload
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


class _Missing:  # an emitted nullary case: slots-only, no payload
    __slots__ = ()


def test_encoding_is_byte_identical_to_the_placement_bridge():
    """The load-bearing round-trip claim: the value bytes this face emits are
    the bytes the placement bridge emits, because it is the same encoder."""
    spec = importlib.util.spec_from_file_location(
        "revl_test_bridge_httpface", ROOT / "backends" / "python" / "bridge.py")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)

    for value in [
        None, True, 3, "s", [1, 2, 3],            # scalars, Opt None, list
        {"a": 1, "b": [2, 3]},                    # a record (dict)
        _Item("abc", 5),                          # a dataclass record
        _Found(_Item("x", 1)),                    # a payload-carrying ADT case
        _Missing(),                               # a nullary ADT case
        _Found([_Missing(), _Found("deep")]),     # nested
    ]:
        assert _encode_value(value) == bridge._encode_value(value)

    # the shapes the `export client` TS types name
    assert _encode_value(_Found(_Item("x", 1))) == {
        "$kind": "_Found", "$value": {"sku": "x", "qty": 1}}
    assert _encode_value(_Missing()) == {"$kind": "_Missing"}


def test_an_opaque_value_is_refused_fail_closed_not_shipped_as_a_dead_tag():
    class _Opaque:  # carries a per-instance __dict__: not a value, not a case
        pass

    with pytest.raises(TypeError) as exc:
        _encode_value(_Opaque())
    assert "cannot marshal" in str(exc.value)


# ------------------------------------------------------ over a real socket

def test_over_a_real_loopback_socket_with_a_stub_session():
    """The whole transport, end to end, on a real socket — no cordis. Proves
    routing + decode + `_encode_value` on the actual `http.server` plumbing."""
    face, session = _face(CACHE, result_value="row")
    httpd = build_http_server(face, "127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(base + "/") as resp:
            manifest = json.loads(resp.read())
        assert {op["path"] for op in manifest["operations"]} == {
            "/app/cache/get", "/app/cache/size"}

        req = urllib.request.Request(
            base + "/app/cache/get", data=b'["k9"]', method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
            assert json.loads(resp.read()) == {"ok": True, "value": "row"}
        assert session.calls == [("cache", "get", ["k9"])]

        # an unknown route is a real 404 on the wire
        bad = urllib.request.Request(base + "/app/cache/nope", data=b"[]",
                                     method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(bad)
        assert exc.value.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


# ------------------------------------------------------ CLI plumbing

def test_serve_with_no_transport_names_both(tmp_path, capsys):
    app = tmp_path / "cache.rvl"
    app.write_text(CACHE, encoding="utf-8")
    code = main(["serve", str(app)])
    assert code == 2
    err = capsys.readouterr().err
    assert "--mcp" in err and "--http" in err


def test_http_shares_the_config_preflight(tmp_path, capsys):
    # the admission-and-config preflight is transport-agnostic: a missing
    # required field refuses the boot before any runtime is imported, over --http
    # exactly as over --mcp (works with no cordis installed)
    app = tmp_path / "needs_config.rvl"
    app.write_text(NEEDS_CONFIG, encoding="utf-8")
    code = main(["serve", "--http", str(app)])
    assert code == 1
    err = capsys.readouterr().err
    assert "missing required config" in err
    assert "url" in err


def test_mcp_and_http_are_mutually_exclusive(tmp_path, capsys):
    app = tmp_path / "cache.rvl"
    app.write_text(CACHE, encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["serve", "--mcp", "--http", str(app)])


# ------------------------------------------------------ live call (needs runtime)

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="standing a live composition up needs the cordis-py runtime — install "
           "it with `sh backends/python/setup.sh`, then run under "
           "`backends/python/.venv/bin/pytest`",
)

INVENTORY = """
type Item = { sku: Str, qty: Int }
type Outcome = Found(Item) | Missing

service Inventory {
  fn lookup(sku: Str) -> Outcome
  fn maybe(sku: Str) -> Opt[Item]
  fn tally() -> Result[Int, Str]
}
component Store provides inv: Inventory {
  provide inv {
    fn lookup(sku) = Found(Item { sku: sku, qty: 7 })
    fn maybe(sku) = None
    fn tally() = Ok(3)
  }
}
"""


@needs_runtime
def test_live_round_trip_marshals_record_opt_result_and_adt():
    """The C1 exit test's round trip: a booted composition over --http, its
    record / Opt / Result / user-ADT results marshalled in the canonical shape a
    generated client reads back."""
    session = Session()
    session.load(compile_source(INVENTORY), {}, origin=None)
    face = HttpComposedServer(session, composition="app")
    httpd = build_http_server(face, "127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"

        def post(path, body):
            req = urllib.request.Request(base + path, data=body, method="POST")
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())

        # a user ADT with a record payload -> adjacently tagged {$kind,$value}
        assert post("/app/inv/lookup", b'["abc"]') == {
            "ok": True,
            "value": {"$kind": "Found",
                      "$value": {"sku": "abc", "qty": 7}}}
        # Opt None -> bare null, never tagged
        assert post("/app/inv/maybe", b'["z"]') == {"ok": True, "value": None}
        # Result Ok -> the tagged Ok object
        assert post("/app/inv/tally", b"[]") == {
            "ok": True, "value": {"$kind": "Ok", "$value": 3}}
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        session.unload()
