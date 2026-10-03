"""A `revl run --watch` reload keeps the ambient provisions (issue #1569).

The reload used to run the full teardown, which withdraws the ambient host
provisions (`revl dev`'s WebUI, the `--providers` model hosts) and never
provides them again, so after the first edit every component that required
one sat PENDING on an unmet requirement. Measured on the base before the fix:

    == change detected — recompile ==
      note   | Classifier        | PENDING — unmet requirement (llm)

What is pinned:

* after a reload, an ambient host object and a `--providers` model host are
  still provided, and a component of the new generation reaches them;
* a managed model member is kept loaded across the reload (one load), and is
  unloaded once at the final teardown with no residue;
* the final teardown still withdraws every ambient provision;
* an edit the bound model hosts cannot serve (a changed operation, or a
  configuration edited to another binding) is refused, and the running
  generation is left as it was;
* the same through the real `revl run --watch` loop.

Every model endpoint is a loopback fake; nothing reaches a real provider.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _load_by_path import load_by_path  # noqa: E402

from revl import model_placement as mp  # noqa: E402
from revl import placement as _placement  # noqa: E402
from revl.compiler import compile_files  # noqa: E402
from revl.providers import bind_for_run  # noqa: E402

_provision = load_by_path("test_model_provision_515",
                          ROOT / "tests" / "test_model_provision_515.py")
FakeOllama = _provision.FakeOllama
MODEL_APP = _provision.APP

needs_cordis = pytest.mark.skipif(
    not _placement._cordis_py_installed(),
    reason="cordis-py runtime not installed (sh backends/python/setup.sh); "
           "these drive the real py runtime")

HOST_APP = """
service Clock { fn now() -> Str }
service Echo { fn now() -> Str }

component Front requires clock: Clock provides out: Echo {
  provide out { fn now() = clock.now() }
}
"""


class Clock:
    """An ambient host object, the shape `revl dev` provides for its WebUI."""

    def now(self) -> str:
        return "tick"


@pytest.fixture
def ollama():
    server = FakeOllama()
    yield server
    server.close()


@pytest.fixture
def schedule():
    before = mp.installed()
    mp.uninstall()
    try:
        yield mp.install
    finally:
        mp.uninstall()
        if before is not None:
            mp.install(before["host"], before["resident"])


def _driver(ir, ambient, ambient_check=None):
    """A `_Driver` on the real cordis-py backend, wired as `run_command`
    wires it."""
    from revl._paths import backends_root, python_backend_emitter  # noqa: PLC0415
    from revl.run import _Driver  # noqa: PLC0415

    backend_dir = backends_root() / "python"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    import runtime as runtime_mod  # noqa: PLC0415
    from cordis import Context  # noqa: PLC0415
    from cordis.fiber import FiberState  # noqa: PLC0415

    extra = {} if ambient_check is None else {"ambient_check": ambient_check}
    return _Driver(ir, {}, python_backend_emitter(), runtime_mod, Context,
                   FiberState, ambient=ambient, **extra)


def _held(store, name: str) -> bool:
    return any(getattr(key, "name", key) == name for key in store)


async def _call(driver, key: str, method: str, *args):
    service = driver.root.get(key)
    assert service is not None, f"`{key}` is not provided"
    value = getattr(service, method)(*args)
    return await value if asyncio.iscoroutine(value) else value


def _edit(path: Path, text: str) -> None:
    """Rewrite `path` so its mtime moves even on a coarse clock."""
    before = path.stat().st_mtime_ns
    path.write_text(text, encoding="utf-8")
    if path.stat().st_mtime_ns == before:
        os.utime(path, ns=(before + 1_000_000, before + 1_000_000))


def _config(tmp_path, ollama, managed: bool, model="tiny:1b") -> str:
    entry = ({"provider": "ollama", "base_url": ollama.base, "model": model,
              "devices": {"cpu0": {"num_gpu": 0}}} if managed else
             {"provider": "openai-compatible", "base_url": ollama.base + "/v1",
              "model": model})
    cfg = tmp_path / "providers.json"
    cfg.write_text(json.dumps({"roles": {"small": entry}}))
    return str(cfg)


def _model_driver(tmp_path, ollama, managed: bool):
    from revl.run import _model_host_check  # noqa: PLC0415
    app = tmp_path / "app.rvl"
    app.write_text(MODEL_APP, encoding="utf-8")
    cfg = _config(tmp_path, ollama, managed)
    ir = compile_files([str(app)])
    hosts = bind_for_run(ir, [str(app)], cfg)
    args = SimpleNamespace(files=[str(app)], providers=cfg)
    return app, cfg, ir, _driver(ir, hosts, _model_host_check(args, hosts))


# --------------------------------------------------------------------------
# 1. the driver's reload
# --------------------------------------------------------------------------

@needs_cordis
def test_an_ambient_host_object_survives_a_reload(tmp_path, capsys):
    app = tmp_path / "app.rvl"
    app.write_text(HOST_APP, encoding="utf-8")
    ir = compile_files([str(app)])

    async def scenario():
        driver = _driver(ir, {"clock": Clock()})
        await driver._load(ir, driver._emit_module(ir))
        assert await _call(driver, "out", "now") == "tick"
        _edit(app, HOST_APP + "\n// an edit\n")
        await driver._reload([str(app)])
        assert _held(driver.root.reflect.store, "clock")
        after = await _call(driver, "out", "now")
        await driver._teardown()
        return driver, after

    driver, after = asyncio.run(scenario())
    out = capsys.readouterr().out
    assert after == "tick"
    assert "unmet requirement" not in out
    # the final teardown still withdraws it, and proves no residue
    assert not _held(driver.root.reflect.store, "clock")
    assert driver._ambient_disposers == []
    assert "no residue — the composition left nothing behind" in out


@needs_cordis
def test_a_model_host_survives_a_reload(tmp_path, ollama, capsys):
    app, _, ir, driver = _model_driver(tmp_path, ollama, managed=False)

    async def scenario():
        await driver._load(ir, driver._emit_module(ir))
        before = await _call(driver, "out", "classify", "a")
        _edit(app, MODEL_APP + "\n// an edit\n")
        await driver._reload([str(app)])
        after = await _call(driver, "out", "classify", "b")
        tagged = await _call(driver, "tg", "tags", "c")
        await driver._teardown()
        return before, after, tagged

    before, after, tagged = asyncio.run(scenario())
    out = capsys.readouterr().out
    assert (before, after, tagged) == ("shim:a", "shim:b", "shim:c")
    assert "unmet requirement" not in out
    assert "REJECTED" not in out
    assert "no residue — the composition left nothing behind" in out


@needs_cordis
def test_a_loaded_member_is_kept_across_a_reload(tmp_path, ollama, schedule,
                                                 capsys):
    """The managed member is loaded once for the run, not once per
    generation, and unloaded once at the final teardown."""
    schedule("local", {"small": "cpu0"}, {"cpu0": "cpu"})
    app, _, ir, driver = _model_driver(tmp_path, ollama, managed=True)

    async def scenario():
        await driver._load(ir, driver._emit_module(ir))
        _edit(app, MODEL_APP + "\n// an edit\n")
        await driver._reload([str(app)])
        after = await _call(driver, "out", "classify", "b")
        loads_mid = len(ollama.calls("load"))
        unloads_mid = len(ollama.calls("unload"))
        await driver._teardown()
        return after, loads_mid, unloads_mid

    after, loads_mid, unloads_mid = asyncio.run(scenario())
    out = capsys.readouterr().out
    assert after == "label:b"
    assert (loads_mid, unloads_mid) == (1, 0)
    assert len(ollama.calls("load")) == 1
    assert len(ollama.calls("unload")) == 1
    assert ollama.loaded == {}
    assert "models" in out and "RESIDUE LEFT" not in out


@needs_cordis
def test_an_edit_the_model_hosts_cannot_serve_is_refused(tmp_path, ollama,
                                                         capsys):
    """An edit that changes a model operation would leave a host checked
    against the old program serving the new one, so it is refused and the
    running generation keeps answering."""
    app, _, ir, driver = _model_driver(tmp_path, ollama, managed=False)
    changed = MODEL_APP.replace(
        "service Tagging { emission[model.small] fn tag(text: Str) -> Str }",
        "service Tagging { emission[model.small] fn tag(text: Str, n: Int) "
        "-> Str }").replace("emit tagger.tag(text)", "emit tagger.tag(text, 1)")
    assert changed != MODEL_APP

    async def scenario():
        await driver._load(ir, driver._emit_module(ir))
        _edit(app, changed)
        await driver._reload([str(app)])
        still = await _call(driver, "tg", "tags", "c")
        await driver._teardown()
        return still

    still = asyncio.run(scenario())
    out = capsys.readouterr().out
    assert "REJECTED" in out
    assert "the edit changes the model hosts the composition needs" in out
    assert "running composition untouched" in out
    assert still == "shim:c"


@needs_cordis
def test_a_configuration_edited_to_another_binding_is_refused(
        tmp_path, ollama, capsys):
    app, cfg, ir, driver = _model_driver(tmp_path, ollama, managed=False)

    async def scenario():
        await driver._load(ir, driver._emit_module(ir))
        _config(tmp_path, ollama, managed=False, model="other:2b")
        _edit(app, MODEL_APP + "\n// an edit\n")
        await driver._reload([str(app)])
        still = await _call(driver, "out", "classify", "a")
        await driver._teardown()
        return still

    still = asyncio.run(scenario())
    out = capsys.readouterr().out
    assert "REJECTED" in out and "restart the run to rebind them" in out
    assert still == "shim:a"
    chats = [r for r in ollama.requests if r["path"] == "/v1/chat/completions"]
    assert {r["body"]["model"] for r in chats} == {"tiny:1b"}


# --------------------------------------------------------------------------
# 2. the real `revl run --watch` loop
# --------------------------------------------------------------------------

def _reader(stream, lines: queue.Queue) -> None:
    for line in stream:
        lines.put(line)
    lines.put(None)


def _wait_for(lines: queue.Queue, seen: list, *needles: str,
              timeout: float = 120.0) -> bool:
    """Read lines until one contains every needle; False on EOF or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            continue
        if line is None:
            return False
        seen.append(line)
        if all(n in line for n in needles):
            return True
    return False


@needs_cordis
def test_revl_run_watch_keeps_the_model_host_across_a_reload(tmp_path,
                                                             ollama):
    app = tmp_path / "app.rvl"
    app.write_text(MODEL_APP, encoding="utf-8")
    cfg = _config(tmp_path, ollama, managed=False)
    # unbuffered: the watch banner is a plain print, block-buffered on a pipe
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [sys.executable, "-m", "revl", "run", str(app), "--watch",
         "--providers", cfg],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, env=env)
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, lines),
                     daemon=True).start()
    seen: list = []
    try:
        assert _wait_for(lines, seen, "== watching"), "".join(seen)
        _edit(app, MODEL_APP + "\n// an edit\n")
        assert _wait_for(lines, seen, "change detected"), "".join(seen)
        # the last component of the new generation comes up
        assert _wait_for(lines, seen, "Tagger", "LOADING -> ACTIVE"), \
            "".join(seen)
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
    _wait_for(lines, seen, "\0", timeout=30)    # drain to EOF
    out = "".join(seen)
    after = out.split("change detected", 1)[1]
    assert "unmet requirement" not in after, out
    assert "no residue — the composition left nothing behind" in out, out
