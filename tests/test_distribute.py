"""Distributability verdict tests (docs/interop-bridge.md §4).

A service crosses a process seam cleanly (transport-safe) only when every
operation is `async fn` with value-typed parameters and returns; a sync
method, an emission, or a resource type in a signature pins it to one address
space. These assert the verdict the `revl audit` bridge report is built on.

The second half tests the seam itself rather than the verdict: the bridge
stub's method allowlist (an unknown method is refused, not dispatched), the
`--placement` preflight, and the housekeeping around a placement run.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.distribute import distributability  # noqa: E402


def _dist(tmp_path, source):
    src = tmp_path / "svc.rvl"
    src.write_text(source, encoding="utf-8")
    return distributability(compile_files([str(src)]))


def test_all_async_value_typed_is_transport_safe(tmp_path):
    verdicts = _dist(tmp_path, """
service Cache {
  async fn get(k: Str) -> Opt[Str]
  async fn put(k: Str, v: Str)
}
""")
    assert verdicts["Cache"]["verdict"] == "transport-safe"


def test_sync_and_emission_are_address_space_bound(tmp_path):
    verdicts = _dist(tmp_path, """
type Row = { id: Int, name: Str }
service Database {
  fn query(sql: Str) -> List[Row]
  emission fn execute(sql: Str) -> Int
}
""")
    database = verdicts["Database"]
    assert database["verdict"] == "address-space-bound"
    assert "query: not async fn" in database["reasons"]
    assert "execute: emission (sync)" in database["reasons"]


def test_resource_return_crosses_by_handle(tmp_path):
    verdicts = _dist(tmp_path, """
type Socket = { fd: Int }
extern pure fn close_sock(handle: Int)
  = @py { return None }
extern acquire fn open_sock(port: Int) -> Socket undo close_sock(0)
  = @py { return {"fd": port} }
service Net {
  async fn accept() -> Socket
}
""")
    net = verdicts["Net"]
    assert net["verdict"] == "address-space-bound"
    assert net["reasons"] == ["accept: resource type Socket crosses"]


# ---------------------------------------------------------------------------
# the seam itself: the stub's method allowlist, and the placement preflight
# ---------------------------------------------------------------------------
#
# The distributability verdict above says *which* services may cross. These say
# the crossing itself is checked: a bridge stub dispatches only the operations
# the service declares (an enumerable boundary, G8), and `--placement` refuses
# to spawn children onto a runtime that is not installed.

import asyncio  # noqa: E402
import importlib.util  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

import pytest  # noqa: E402


def _bridge():
    """backends/python/bridge.py, imported directly (it needs no cordis)."""
    spec = importlib.util.spec_from_file_location(
        "revl_test_bridge", ROOT / "backends" / "python" / "bridge.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeService:
    def __init__(self):
        self.calls = []

    def query(self, sql):
        self.calls.append(sql)
        return [{"id": 1}]

    def wipe(self, table):  # present on the object, absent from the declaration
        self.calls.append(("wipe", table))
        return "wiped"


class _FakeCtx:
    def __init__(self, service):
        self._service = service

    def get(self, key):
        return self._service if key == "db" else None


def _invoke(bridge, exports, request):
    table = bridge._export_table(exports)
    return asyncio.run(bridge._invoke(_FakeCtx(request.pop("_service")), table, request))


def test_stub_dispatches_a_declared_method():
    bridge = _bridge()
    service = _FakeService()
    reply = _invoke(bridge, {"db": ["query", "execute"]},
                    {"_service": service, "key": "db", "method": "query", "args": ["SELECT 1"]})
    assert reply == {"ok": True, "value": [{"id": 1}]}
    assert service.calls == ["SELECT 1"]


def test_stub_refuses_a_method_the_service_does_not_declare():
    """`wipe` exists on the provided object but not in `service Database`."""
    bridge = _bridge()
    service = _FakeService()
    reply = _invoke(bridge, {"db": ["query", "execute"]},
                    {"_service": service, "key": "db", "method": "wipe", "args": ["users"]})
    assert reply["ok"] is False
    assert "'wipe' is not exported" in reply["error"]
    assert "query, execute" in reply["error"].replace("execute, query", "query, execute")
    assert service.calls == []  # refused before dispatch, not after


@pytest.mark.parametrize("method", ["__init__", "__class__", "__reduce__", "_service"])
def test_stub_refuses_attribute_access_dressed_as_a_call(method):
    bridge = _bridge()
    service = _FakeService()
    reply = _invoke(bridge, {"db": ["query"]},
                    {"_service": service, "key": "db", "method": method, "args": []})
    assert reply["ok"] is False and "is not exported" in reply["error"]


def test_stub_key_only_export_still_refuses_non_methods():
    """The legacy `serve(ctx, ["db"], ...)` form derives its allowlist from the
    object's public methods: weaker than the declaration, but not unchecked."""
    bridge = _bridge()
    service = _FakeService()
    ok = _invoke(bridge, ["db"], {"_service": service, "key": "db", "method": "query", "args": ["x"]})
    assert ok["ok"] is True
    refused = _invoke(bridge, ["db"], {"_service": service, "key": "db", "method": "__reduce__", "args": []})
    assert refused["ok"] is False and "is not exported" in refused["error"]


def test_stub_still_refuses_an_unexported_key():
    bridge = _bridge()
    reply = _invoke(bridge, {"db": ["query"]},
                    {"_service": _FakeService(), "key": "secrets", "method": "query", "args": []})
    assert reply["ok"] is False and "is not exported by this process" in reply["error"]


# --- probes are parsed, not eval'd ----------------------------------------

def _process_runner():
    sys.path.insert(0, str(ROOT / "src"))
    from revl import _process_runner  # noqa: PLC0415

    return _process_runner


def test_probe_dispatches_a_literal_call():
    runner = _process_runner()
    service = _FakeService()
    assert runner._eval_probe("db.query('SELECT 1')", {"db": service}) == [{"id": 1}]


@pytest.mark.parametrize("expr", [
    "__import__('os').system('echo pwned')",   # no builtins
    "db.query(__import__('os').name)",         # arguments must be literals
    "db.query(open('/etc/passwd').read())",
    "[x for x in ()]",                         # not a call at all
    "db.query(sql='SELECT 1')",                # no keywords
])
def test_probe_refuses_anything_that_is_not_key_method_literals(expr):
    runner = _process_runner()
    with pytest.raises(ValueError):
        runner._eval_probe(expr, {"db": _FakeService()})


def test_probe_refuses_a_key_this_process_does_not_hold():
    runner = _process_runner()
    with pytest.raises(ValueError, match="not a key this process holds"):
        runner._eval_probe("other.query('x')", {"db": _FakeService()})


# --- placement: preflight, and the allowlist it ships ----------------------

def _placement():
    sys.path.insert(0, str(ROOT / "src"))
    from revl import placement  # noqa: PLC0415

    return placement


def test_placement_preflights_a_missing_cordis_before_spawning(monkeypatch, capsys):
    """Without this, both children die with a raw ModuleNotFoundError traceback."""
    placement = _placement()
    monkeypatch.setattr(placement, "_cordis_py_installed", lambda: False)
    monkeypatch.setattr(placement.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("preflight must run before any child is spawned"))
    rc = placement.run_placement([str(ROOT / "examples" / "user_cache.rvl")],
                                 str(ROOT / "examples" / "placement" / "user_cache.toml"), once=True)
    err = capsys.readouterr().err
    assert rc == 3
    assert "cordis-py runtime is not installed" in err
    assert str(ROOT / "backends" / "python" / "setup.sh") in err
    # the suggested command must be the real one, runnable verbatim
    assert f"{ROOT / 'backends' / 'python' / '.venv' / 'bin' / 'python'} -m revl run" in err
    assert "--placement" in err and "--once" in err


class _FakeProc:
    """Enough of a Popen for the conductor: comes up, then unwinds."""

    def __init__(self, name):
        # Both lines, because a child that says UP and then exits
        # without ever saying DOWN is a STRANDED teardown, not a clean
        # one, and the conductor now says so (issue 265). Every real
        # runner prints DOWN; a double that does not is modelling a
        # failure this test is not about.
        self.stdout = io.StringIO(f"[{name}] UP\n[{name}] DOWN\n")
        self.stdin = io.StringIO()

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


def test_placement_ships_the_declared_method_allowlist_to_the_stub(monkeypatch):
    """The proxy side always knew the method list; the stub side now gets it too."""
    placement = _placement()
    monkeypatch.setattr(placement, "_cordis_py_installed", lambda: True)
    specs = {}

    def fake_popen(cmd, **kwargs):
        spec = json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))
        specs[spec["name"]] = spec
        return _FakeProc(spec["name"])

    monkeypatch.setattr(placement.subprocess, "Popen", fake_popen)
    rc = placement.run_placement([str(ROOT / "examples" / "user_cache.rvl")],
                                 str(ROOT / "examples" / "placement" / "user_cache.toml"), once=True)
    assert rc == 0
    serve = specs["provider"]["serve"]
    assert serve["keys"] == ["db"]
    assert sorted(serve["methods"]["db"]) == ["execute", "query"]  # `service Database`
    # and the two sides agree: what the stub serves is what the proxy forwards
    assert sorted(specs["consumer"]["proxies"]["db"]["methods"]) == sorted(serve["methods"]["db"])


def test_placement_removes_its_temporary_socket_directory(monkeypatch):
    placement = _placement()
    monkeypatch.setattr(placement, "_cordis_py_installed", lambda: True)
    seen = []

    def fake_popen(cmd, **kwargs):
        spec = json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))
        seen.append(Path(cmd[-1]).parent)
        return _FakeProc(spec["name"])

    monkeypatch.setattr(placement.subprocess, "Popen", fake_popen)
    placement.run_placement([str(ROOT / "examples" / "user_cache.rvl")],
                            str(ROOT / "examples" / "placement" / "user_cache.toml"), once=True)
    assert seen and not any(d.exists() for d in seen)


def test_placement_does_not_put_the_current_directory_on_a_child_path(monkeypatch):
    """An empty PYTHONPATH entry is CWD on sys.path — a shadowing hazard."""
    placement = _placement()
    monkeypatch.setattr(placement, "_cordis_py_installed", lambda: True)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    envs = []

    def fake_popen(cmd, **kwargs):
        spec = json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))
        envs.append(kwargs.get("env") or {})
        return _FakeProc(spec["name"])

    monkeypatch.setattr(placement.subprocess, "Popen", fake_popen)
    placement.run_placement([str(ROOT / "examples" / "user_cache.rvl")],
                            str(ROOT / "examples" / "placement" / "user_cache.toml"), once=True)
    assert envs and all("" not in env["PYTHONPATH"].split(os.pathsep) for env in envs)


def test_stub_refuses_an_undeclared_method_over_the_wire(tmp_path):
    """End to end through `serve`, on the real socket a proxy talks to."""
    bridge = _bridge()
    service = _FakeService()
    directory = tempfile.mkdtemp(prefix="revl_bridge_test_")
    sock_path = str(Path(directory) / "provider.sock")

    async def scenario():
        server = await bridge.serve(_FakeCtx(service), {"db": ["query", "execute"]}, sock_path)
        reader, writer = await asyncio.open_unix_connection(sock_path)

        async def rpc(request):
            writer.write((json.dumps(request) + "\n").encode())
            await writer.drain()
            return json.loads(await reader.readline())

        replies = [await rpc({"key": "db", "method": "query", "args": ["SELECT 1"]}),
                   await rpc({"key": "db", "method": "wipe", "args": ["users"]})]
        writer.close()
        server.close()
        await server.wait_closed()
        return replies

    try:
        served, refused = asyncio.run(scenario())
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    assert served == {"ok": True, "value": [{"id": 1}]}
    assert refused["ok"] is False and "'wipe' is not exported" in refused["error"]
    assert service.calls == ["SELECT 1"]  # the refused call never ran
