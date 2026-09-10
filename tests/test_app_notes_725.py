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

`revl dev` (issue #724) adds two contracts this file owns. The first is the
frontend port: the command prints the URL the frontend serves on, so the port
it names has to be the port Vite actually binds. The second is settlement: the
WebUI `revl dev` installs is an ambient host provision, and the command claims
that provision is withdrawn even when a revl-owned component's teardown raises.
Both are checked here, the port pair without npm or a runtime and the
settlement on the real driver.
"""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import revl.dev as dev_mod  # noqa: E402
from revl import compile_files, compile_source  # noqa: E402
from revl.mcp.http_face import HttpComposedServer  # noqa: E402
from revl.dev import DevWebUI, _preflight  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="driving the real driver needs the cordis-py runtime; install it "
           "with `sh backends/python/setup.sh`, then run under its venv",
)

APP = ROOT / "examples" / "app" / "notes.rvl"
FRONTEND = ROOT / "examples" / "app" / "frontend"
CORDIS_PY = ROOT / "backends" / "python" / ".venv" / "bin" / "python"


def test_dev_webui_rejects_escaping_entry_paths():
    host = DevWebUI(ROOT / "examples" / "app")
    for bad in ("../secrets.txt", "/etc/passwd", "..", "sub/../../x"):
        with pytest.raises(RuntimeError):
            host.add_entry(bad, "m", ["/notes"])


def test_dev_webui_records_the_declared_entry():
    host = DevWebUI(ROOT / "examples" / "app")
    assert (
        host.add_entry("./frontend/entry.client.ts", "m", ["/notes"])
        == "./frontend/entry.client.ts"
    )
    assert host.entries == [("./frontend/entry.client.ts", "m", ("/notes",))]


def test_dev_preflight_names_the_source_line():
    import tempfile

    broken = Path(tempfile.mkdtemp()) / "broken.rvl"
    broken.write_text("component Broken {\n  emit nosuchfn(\n}\n")
    assert _preflight([str(broken)]) != 0
    assert _preflight([str(APP)]) == 0


# --------------------------------------------------------------- frontend / IR


def test_app_compiles_with_a_derived_route_table():
    ir = compile_files([str(APP)])
    ops = ir["services"]["NotesApi"]["methods"]
    routed = {op: m["route"] for op, m in ops.items() if m.get("route")}
    assert (routed["get_note"]["method"], routed["get_note"]["path"]) == (
        "get",
        "/notes/{id}",
    )
    assert (routed["list_notes"]["method"], routed["list_notes"]["path"]) == (
        "get",
        "/notes",
    )
    assert (routed["create_note"]["method"], routed["create_note"]["path"]) == (
        "post",
        "/notes",
    )
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
    provide = next(
        s
        for s in comp["body"]
        if s.get("step") == "provide" and s.get("name") == "store"
    )
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
    face = _face(
        Err({"status": 404, "code": "not_found", "message": "no note with that id"})
    )
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


# ------------------------------------------------------ dev frontend port
#
# `revl dev` announces the URL the frontend is served on, so the port in that
# banner has to be the port Vite actually binds. Vite's own default is the
# opposite: an occupied port makes it auto-increment to the next free one, and
# the operator opens a URL that belongs to some other process while the
# composition is served somewhere else. The checks below pin the three legs of
# the invariant without needing npm or a runtime: Vite is told not to move,
# the banner appears only once the child has survived its boot, and a port no
# child can name is refused up front.


class _FakeVite:
    """The parts of `subprocess.Popen` that `_vite_dead` / `_stop` read, with
    the child's boot behaviour scripted: `dies=True` is a Vite whose boot
    failed on an occupied port (what `--strictPort` produces), `dies=False` is
    a Vite that is still serving."""

    def __init__(self, *, dies: bool, output: str = "") -> None:
        self._output = output
        self.returncode = 1 if dies else None

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("vite", timeout or 0.0)
        return self.returncode

    def communicate(self, timeout: float | None = None):
        return self._output, ""

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def _dev_args(*extra: str):
    """`revl dev` parsed exactly as the CLI parses it, so the flags under test
    are the shipped ones."""
    from revl.cli.parser import build_parser

    return build_parser().parse_args(["dev", *extra, str(APP)])


def test_dev_tells_vite_not_to_move_off_an_occupied_port(monkeypatch):
    """A port clash has to fail the boot rather than silently move the
    frontend, so the child is launched with `--strictPort`: the port `revl dev`
    names is the port Vite binds, or Vite exits and the boot check sees it."""
    launched: list[list[str]] = []

    def _popen(argv, **_kwargs):
        launched.append(list(argv))
        return _FakeVite(dies=False)

    monkeypatch.setattr(dev_mod, "shutil",
                        SimpleNamespace(which=lambda _name: "/usr/bin/npm"))
    monkeypatch.setattr(dev_mod.subprocess, "Popen", _popen)

    dev_mod._start_vite(FRONTEND, "127.0.0.1", 5173)

    assert len(launched) == 1
    argv = launched[0]
    assert argv[:4] == ["/usr/bin/npm", "run", "dev", "--"]
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--port") + 1] == "5173"
    assert "--strictPort" in argv


def test_dev_prints_no_banner_for_a_port_vite_did_not_bind(monkeypatch, capsys):
    """The banner is the operator's only statement of where the frontend is,
    so it may appear only once the child has survived its boot. A Vite that
    died on the port is the case that used to advertise a URL serving someone
    else's process: the command has to name the port it failed on and exit
    non-zero instead."""
    monkeypatch.setattr(
        dev_mod, "_start_vite",
        lambda *_: _FakeVite(dies=True, output="Port 5173 is already in use\n"))

    code = dev_mod.dev_command(_dev_args("--once"))

    captured = capsys.readouterr()
    assert code == 3
    assert "5173" in captured.err
    assert "in use" in captured.err
    assert "http://" not in captured.out


def test_dev_refuses_port_zero_before_spawning_vite(monkeypatch, capsys):
    """`--port 0` reads to Vite as "bind any free port", and only the OS knows
    which one it picked, so no banner could name where the frontend is served.
    It is a usage error (exit 2) refused before any child is spawned, not a URL
    the command cannot vouch for."""
    started: list[tuple] = []
    monkeypatch.setattr(
        dev_mod, "_start_vite",
        lambda *args: (started.append(args), _FakeVite(dies=False))[1])
    monkeypatch.setattr(dev_mod, "run_command", lambda args: 0)

    code = dev_mod.dev_command(_dev_args("--port", "0"))

    captured = capsys.readouterr()
    assert code == 2
    assert started == []
    assert "port 0" in captured.err
    assert "http://" not in captured.out


# --------------------------------------------------------------- runtime (gated)


@pytest.mark.skipif(
    not CORDIS_PY.exists(),
    reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)",
)
def test_dev_once_boots_the_app_and_proves_no_residue():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [
            str(CORDIS_PY),
            "-P",
            "-m",
            "revl",
            "dev",
            "--once",
            "--no-frontend",
            str(APP),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "entry ./frontend/entry.client.ts -> /notes" in result.stdout
    assert "no residue" in result.stdout


@pytest.mark.skipif(
    not CORDIS_PY.exists(),
    reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)",
)
def test_crud_persists_and_reverts_residue_free_on_the_runtime():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [str(CORDIS_PY), "-m", "revl", "test", str(APP)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS create then get returns the stored note" in result.stdout
    assert "PASS get of an absent id is a typed 404 ApiError" in result.stdout
    assert (
        "PASS list returns created rows and a reloaded store is empty" in result.stdout
    )
    # the differentiated half's baseline lifecycle test (the hot-swap legs need
    # `Session.swap`, so they live in tests/test_app_hotswap_725.py, not here).
    assert (
        "PASS ranker records engagement, scores by strategy, reverts "
        "residue-free" in result.stdout
    )
    assert "[py] pass: 4 test(s) passed" in result.stdout


# ---------------------------------------------------- ambient settlement (gated)

# A two-component composition whose consumer is disposed FIRST (consumers
# before providers), so a fault at its disposer leaves the provider's own
# teardown UNATTEMPTED. `revl dev` runs exactly this driver with an ambient
# WebUI provision attached.
AMBIENT_TWO = """
service Cache { fn get(key: Str) -> Opt[Str] }
component Store provides cache: Cache {
  let rows = effect Map.new() undo rows.drop()
  provide cache { fn get(key) = rows.get(key) }
}
component Front requires cache: Cache {
  let seen = effect Map.new() undo seen.drop()
}
"""


async def _raise_from_the_native_disposer():
    raise RuntimeError("native disposer fault")


def _ambient_driver(ir, ambient):
    """A `_Driver` on the real cordis-py backend, wired exactly as
    `run_command` wires it, carrying the ambient host provision `revl dev`
    installs for its WebUI."""
    from revl._paths import backends_root  # noqa: PLC0415
    from revl.run import _Driver  # noqa: PLC0415

    backend_dir = backends_root() / "python"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    import emit  # noqa: PLC0415
    import runtime as runtime_mod  # noqa: PLC0415
    from cordis import Context  # noqa: PLC0415
    from cordis.fiber import FiberState  # noqa: PLC0415

    return _Driver(ir, {}, emit, runtime_mod, Context, FiberState,
                   ambient=ambient)


def _held(store, name: str) -> bool:
    """Cordis keys its provision store by `UniqueSymbol`, not by the plain
    service name, so a live `webui` provision is held under `Symbol('webui')`."""
    return any(getattr(key, "name", key) == name for key in store)


@needs_cordis
def test_ambient_settlement_survives_a_raising_component_teardown(capsys):
    """The ambient provision `revl dev` installs is settled even when a
    revl-owned teardown raises. Drive that path for real: with the provision
    live, fault the consumer's disposer, then prove that after `_teardown` the
    provision was withdrawn (gone from the store, the disposer list cleared)
    and the no-residue proof still ran and reported what the fault left
    unattempted, while the original fault still propagates to the caller."""
    ir = compile_source(AMBIENT_TWO, "<ambient>.rvl")
    settled: list[str] = []

    async def scenario():
        driver = _ambient_driver(ir, {"webui": object()})
        await driver._load(ir, driver._emit_module(ir))
        assert _held(driver.root.reflect.store, "webui")  # the provision is live
        # A marker on the same ambient list the WebUI provision sits on, so the
        # settlement is observable in the output rather than inferred from the
        # absence of something.
        driver._ambient_disposers.append(
            lambda: settled.append("ambient provision settled"))
        driver.fibers["Front"].dispose = _raise_from_the_native_disposer
        with pytest.raises(RuntimeError, match="native disposer fault"):
            await driver._teardown()
        return driver

    driver = asyncio.run(scenario())
    out = capsys.readouterr().out
    # print the settlement so `-s` shows it ran, not merely that the assertion
    # about it passed
    print(f"ambient settlement: {settled!r}")
    print(out.splitlines()[-1].strip() if out.strip() else "<no teardown output>")

    # the ambient withdrawal ran even though the component loop raised ...
    assert settled == ["ambient provision settled"]
    assert not _held(driver.root.reflect.store, "webui")
    assert driver._ambient_disposers == []
    # ... and the proof that the composition is not clean still ran, naming the
    # provider the fault left unattempted instead of losing it to the traceback.
    assert "RESIDUE LEFT" in out
