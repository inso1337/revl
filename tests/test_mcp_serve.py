"""The fourth quadrant: serve a composition's OWN operations as MCP tools.

`revl mcp serve` serves the compiler's tools; this serves *one booted
composition's* provided operations. The load-bearing claim is the same one the
projection makes, now on the wire of a live server: a served tool's
`readOnlyHint`/`destructiveHint` is DERIVED by the compiler from the checked
emission classification, so it cannot lie about side effects — unlike every
author-asserted MCP hint.

Two halves, gated the way `test_mcp_session.py` gates: the projection-onto-the
-wire and the config preflight are pure frontend and run everywhere; only the
test that a call actually reaches the live operation needs the cordis-py
runtime, so that one carries `@needs_runtime` rather than a module skip.
"""

import importlib.util
import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.mcp.composed import ComposedServer, _positional  # noqa: E402
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
    compiled IR (so the projection runs) and records `call` instead of booting
    a cordis Context."""

    def __init__(self, source: str):
        self.ir = compile_source(source)
        self.calls: list[tuple] = []

    def call(self, key, method, args):
        self.calls.append((key, method, args))
        return {"result": {"key": key, "method": method, "args": args}, "trace": []}

    def state(self, drain: bool = False):
        return {"loaded": True}


def _server(source: str) -> tuple[ComposedServer, _StubSession]:
    session = _StubSession(source)
    return ComposedServer(session, composition="app"), session


def _rpc(server: ComposedServer, method: str, params=None, request_id=1):
    return server.handle({"jsonrpc": "2.0", "id": request_id,
                          "method": method, "params": params or {}})


# ------------------------------------------------------ projection onto the wire

def test_served_tools_advertise_provided_ops_with_derived_hints():
    server, _ = _server(CACHE)
    listed = _rpc(server, "tools/list")["result"]["tools"]
    by_name = {t["name"]: t for t in listed}
    assert set(by_name) == {"app.cache.get", "app.cache.size"}
    get = by_name["app.cache.get"]
    assert get["annotations"]["readOnlyHint"] is True
    assert get["annotations"]["destructiveHint"] is False
    # the hint is the compiler's, not an author's
    assert get["x-revl"]["annotationsDerivedFrom"] == "compiler"


def test_a_destructive_emission_op_is_flagged_on_the_wire():
    server, _ = _server(BUS)
    by_name = {t["name"]: t for t in _rpc(server, "tools/list")["result"]["tools"]}
    send = by_name["app.bus.send"]
    assert send["annotations"]["destructiveHint"] is True
    assert send["annotations"]["readOnlyHint"] is False
    assert send["x-revl"]["classification"] == "emission"
    # its read-only sibling on the same service stays read-only
    assert by_name["app.bus.depth"]["annotations"]["readOnlyHint"] is True


def test_initialize_names_the_live_composition():
    server, _ = _server(CACHE)
    result = _rpc(server, "initialize")["result"]
    assert result["protocolVersion"]
    assert result["serverInfo"]["name"] == "revl:app"
    assert "MemCache" in result["instructions"]


def test_a_call_routes_named_args_to_a_positional_session_call():
    server, session = _server(CACHE)
    result = _rpc(server, "tools/call",
                  {"name": "app.cache.get", "arguments": {"key": "k9"}})["result"]
    # the named MCP argument became a positional call on the live session
    assert session.calls == [("cache", "get", ["k9"])]
    assert result["isError"] is False
    assert result["structuredContent"]["ok"] is True


def test_positional_mapping_preserves_declared_order():
    # named args, any order in, declared order out; trailing omitted dropped
    names = ["a", "b", "c"]
    assert _positional({"b": 2, "a": 1, "c": 3}, names) == [1, 2, 3]
    assert _positional({"a": 1}, names) == [1]
    assert _positional({"a": 1, "c": 3}, names) == [1, None, 3]


def test_unknown_tool_is_a_protocol_error():
    server, _ = _server(CACHE)
    response = _rpc(server, "tools/call", {"name": "app.cache.nope", "arguments": {}})
    assert response["error"]["code"] == -32602


def test_notifications_get_no_response():
    server, _ = _server(CACHE)
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_serve_loop_reads_newline_delimited_jsonrpc():
    server, _ = _server(CACHE)
    stdin = io.StringIO(
        '{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
        "\n"  # blank lines are skipped
        '{"jsonrpc":"2.0","id":2,"method":"tools/call",'
        '"params":{"name":"app.cache.size","arguments":{}}}\n'
    )
    stdout = io.StringIO()
    assert server.serve(stdin, stdout) == 0
    lines = [ln for ln in stdout.getvalue().splitlines() if ln]
    assert len(lines) == 2


# ------------------------------------------------------ config-to-boot preflight

def test_config_preflight_refuses_a_misconfigured_boot(tmp_path, capsys):
    app = tmp_path / "needs_config.rvl"
    app.write_text(NEEDS_CONFIG, encoding="utf-8")
    # no --config: the required `url` field is unmet, so the boot is refused
    # before any runtime is imported (works with no cordis installed)
    code = main(["serve", "--mcp", str(app)])
    assert code == 1
    err = capsys.readouterr().err
    assert "missing required config" in err
    assert "url" in err


def test_serve_requires_a_transport(tmp_path, capsys):
    app = tmp_path / "cache.rvl"
    app.write_text(CACHE, encoding="utf-8")
    code = main(["serve", str(app)])
    assert code == 2
    assert "--mcp" in capsys.readouterr().err


# ------------------------------------------------------ live call (needs runtime)

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="serving a live composition needs the cordis-py runtime — install it "
           "with `sh backends/python/setup.sh`, then run under "
           "`backends/python/.venv/bin/pytest`",
)


@needs_runtime
def test_a_served_call_reaches_the_live_operation():
    session = Session()
    session.load(compile_source(CACHE), {})
    try:
        server = ComposedServer(session, composition="app")
        result = _rpc(server, "tools/call",
                      {"name": "app.cache.size", "arguments": {}})["result"]
        assert result["isError"] is False
        assert result["structuredContent"]["result"] == 0
    finally:
        session.unload()


APPROVAL = """
extern emission fn announce(sink: Str, msg: Str) = @py {
    with open(sink, 'a') as _f:
        _f.write('announce:' + msg + '\\n')
    return
}
service Ops { emission fn shout(sink: Str, msg: Str) }
component Agent provides ops: Ops {
  provide ops { fn shout(sink, msg) { emit announce(sink, msg) } }
}
"""


def test_a_class_c_crossing_returns_the_ticket_not_an_opaque_fault():
    """Roadmap 425 F4, second surface. `ApprovalRequired` is not a
    `SessionError`, so it landed in `_call_tool`'s broad `except Exception` and
    came back as `{"raised": true, ... "ApprovalRequired: approval required for
    a class-(c) crossing"}` — no ticket, no hash. Fail-CLOSED (nothing fired)
    but unapprovable: this wire carries only the composition's own operations,
    so with no hash to relay the crossing could never be allowed at all."""
    from revl.mcp.approval import ApprovalRequired

    class _Raising(_StubSession):
        def call(self, key, method, args):
            raise ApprovalRequired({"key": key, "method": method,
                                    "hash": "sha256:deadbeef",
                                    "capabilities": ["announce"]})

    session = _Raising(BUS)
    server = ComposedServer(session, composition="app")
    result = _rpc(server, "tools/call",
                  {"name": "app.bus.send", "arguments": {"msg": "hi"}})["result"]
    payload = result["structuredContent"]
    assert result["isError"] is True
    assert payload["approvalRequired"] is True
    assert payload["ticket"]["hash"] == "sha256:deadbeef"
    assert "raised" not in payload
    # item 274: the refusal names what the caller can do instead, and this wire
    # has no approve verb of its own to point at.
    assert "no approve verb on this wire" in payload["note"]
    assert "revl_approve" in payload["note"]


@needs_runtime
def test_a_live_class_c_crossing_surfaces_the_real_ticket(tmp_path):
    """The same thing against the real chokepoint rather than a stub: the
    ticket comes from `Session.call`'s own decision, carries a real `hash`, and
    the emission does not fire."""
    session = Session()
    session.approval_policy = "auto"
    session.load(compile_source(APPROVAL), {}, record=True)
    sink = tmp_path / "sink.log"
    try:
        server = ComposedServer(session, composition="app")
        result = _rpc(server, "tools/call",
                      {"name": "app.ops.shout",
                       "arguments": {"sink": str(sink), "msg": "hi"}})["result"]
        payload = result["structuredContent"]
        assert payload["approvalRequired"] is True
        assert payload["ticket"]["hash"].startswith("sha256:")
        assert not sink.exists()
    finally:
        session.unload()


@needs_runtime
def test_serve_composition_boots_and_serves_over_stdio():
    from revl.mcp.composed import serve_composition

    stdin = io.StringIO(
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/call",'
        '"params":{"name":"app.cache.size","arguments":{}}}\n'
    )
    stdout = io.StringIO()
    assert serve_composition(compile_source(CACHE), {}, composition="app",
                             stdin=stdin, stdout=stdout) == 0
    assert '"result": 0' in stdout.getvalue() or '"result":0' in stdout.getvalue()
