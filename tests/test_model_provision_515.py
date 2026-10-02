"""A model role's provision: one load and one unload for N consumers, on the
device the model schedule chose, with `no_residue` at teardown (roadmap item
515, issue #1189, slice S2).

What is pinned:

* a role is provisioned per ROLE, not per consumer: every model host whose
  operations route to it shares one loaded member, so N consumers make one
  load and one unload (the issue's exit evidence);
* the load goes to exactly the device `revl.model_placement` answers, which
  is the scheduler's decision (#1478), through the adapter's load options for
  that device; no schedule, a role placed elsewhere, or a device the binding
  cannot load on is refused by name and loads nothing;
* the server is asked what it holds: a member it reports on the wrong device
  class is unloaded again and refused, and `residue()` reports a member it
  still holds after the last release;
* acquire and release pair per consumer;
* `revl run --placement --providers` loads in the child before any component
  activates, serves the model crossings, unloads after the last component is
  gone, and its per-process residue proof includes the models.

Every endpoint is a loopback fake that speaks Ollama's native API and records
what it was sent; nothing reaches a real provider. The one live test is
opt-in (`REVL_LIVE_OLLAMA_PROVISION_MODEL`) and skips unless the local server
holds nothing, so it cannot evict another user's model.

Programs are inline for the census reason `tests/test_model_portfolio_515.py`
gives.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import model_placement as mp  # noqa: E402
from revl import placement as _placement  # noqa: E402
from revl.compiler import compile_files  # noqa: E402
from revl.providers import (  # noqa: E402
    Adapter, CompletionRequest, ProviderConfigError, ProviderError,
    ProvisionRefused, Provisions, bind_for_run, close_hosts,
    open_hosts, parse_config, provision_residue,
)
from revl.providers.provision import gpu_share  # noqa: E402


# --------------------------------------------------------------------------
# a fake Ollama server
# --------------------------------------------------------------------------

#: The fake's model size, and the GPU bytes a CPU load still holds for its
#: compute graph (0.3%, which `ollama ps` prints as "100% CPU").
SIZE = 2_000_000
GRAPH = 6_000


class FakeOllama:
    """Speaks the four native endpoints a provision uses and records every
    request. `loaded` is what the server holds: `{model: size_vram}`.

    `mode`: "ok"; "misplace" (every load lands in GPU memory, whatever the
    options say); "spill" (a GPU load only half fits); "sticky" (an unload is
    acknowledged and ignored).

    A CPU load still holds a little GPU memory, as a real server does for its
    compute graph."""

    def __init__(self) -> None:
        self.requests: list = []
        self.loaded: dict = {}
        self.mode = "ok"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.requests.append({"method": "GET", "path": self.path})
                if self.path == "/api/ps":
                    self._send({"models": [
                        {"name": m, "model": m, "size": SIZE,
                         "size_vram": v} for m, v in outer.loaded.items()]})
                else:
                    self._send({"error": "unknown"}, 404)

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"null")
                outer.requests.append({"method": "POST", "path": self.path,
                                       "body": body})
                model = body.get("model")
                if self.path == "/api/generate":
                    if body.get("keep_alive") == 0:
                        if outer.mode != "sticky":
                            outer.loaded.pop(model, None)
                        self._send({"model": model, "done": True,
                                    "done_reason": "unload"})
                        return
                    outer._load(model, body.get("options") or {})
                    self._send({"model": model, "done": True,
                                "done_reason": "load",
                                "load_duration": 4_200_000})
                    return
                if self.path == "/v1/chat/completions":
                    # the OpenAI-compatible shim: serves, loads nothing here
                    text = body["messages"][-1]["content"]
                    self._send({"model": model, "choices": [{
                        "message": {"content": f"shim:{text}"},
                        "finish_reason": "stop"}]})
                    return
                if self.path == "/api/chat":
                    if model not in outer.loaded:
                        # what a real server does: load it where it likes
                        outer._load(model, body.get("options") or {})
                    text = body["messages"][-1]["content"]
                    self._send({"model": model, "done": True,
                                "done_reason": "stop",
                                "message": {"role": "assistant",
                                            "content": f"label:{text}",
                                            "thinking": "hmm"},
                                "prompt_eval_count": 5, "eval_count": 3})
                    return
                self._send({"error": "unknown"}, 404)

            def _send(self, payload, code=200):
                data = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _load(self, model, options) -> None:
        on_cpu = options.get("num_gpu") == 0 and self.mode != "misplace"
        if on_cpu:
            self.loaded[model] = GRAPH       # rounds to 0% on the GPU
        else:
            self.loaded[model] = SIZE // 2 if self.mode == "spill" else SIZE

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def calls(self, kind: str) -> list:
        """The load, unload or chat requests, by body."""
        out = []
        for r in self.requests:
            if r["method"] != "POST":
                continue
            if kind == "chat" and r["path"] == "/api/chat":
                out.append(r["body"])
            elif r["path"] == "/api/generate":
                unload = r["body"].get("keep_alive") == 0
                if (kind == "unload") == unload and kind in ("load", "unload"):
                    out.append(r["body"])
        return out

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def ollama():
    server = FakeOllama()
    yield server
    server.close()


@pytest.fixture
def schedule():
    """Install a schedule for the test and leave the process as found."""
    before = mp.installed()
    mp.uninstall()
    try:
        yield mp.install
    finally:
        mp.uninstall()
        if before is not None:
            mp.install(before["host"], before["resident"])


def _binding(ollama, **extra):
    entry = {"provider": "ollama", "base_url": ollama.base, "model": "tiny:1b",
             "devices": {"cpu0": {"num_gpu": 0}, "gpu0": {}}}
    entry.update(extra)
    return parse_config({"roles": {"small": entry}}, "providers.json") \
        .binding("small")


def _provisions(ollama, **extra):
    return Provisions({"small": Adapter(_binding(ollama, **extra))})


# --------------------------------------------------------------------------
# 1. the configuration
# --------------------------------------------------------------------------

def test_an_ollama_binding_is_managed_and_on_device(ollama):
    b = _binding(ollama)
    assert b.managed and b.residence == "on_device"
    assert b.device_options("cpu0") == {"num_gpu": 0}
    assert b.device_options("gpu0") == {}
    assert b.device_options("gpu1") is None
    oa = parse_config({"roles": {"r": {
        "provider": "openai-compatible", "base_url": ollama.base + "/v1",
        "model": "m"}}}).binding("r")
    assert not oa.managed and oa.devices == ()
    # `devices` is an ollama field: an endpoint revl does not load refuses it
    with pytest.raises(ProviderConfigError, match="unknown field"):
        parse_config({"roles": {"r": {
            "provider": "openai-compatible", "base_url": ollama.base + "/v1",
            "model": "m", "devices": {"cpu0": {}}}}})


def test_an_ollama_binding_without_devices_is_refused(ollama):
    with pytest.raises(ProviderConfigError, match="`devices` is required"):
        parse_config({"roles": {"small": {
            "provider": "ollama", "base_url": ollama.base, "model": "m"}}})


@pytest.mark.parametrize("devices, message", [
    ({"cpu0": {"num_thread": 4}}, "unknown option"),
    ({"cpu0": {"num_gpu": -1}}, "at least 0"),
    ({"cpu0": 0}, "must be a table"),
    ({"cpu-0": {}}, "device name"),
])
def test_a_malformed_device_table_is_refused(ollama, devices, message):
    with pytest.raises(ProviderConfigError, match=message):
        _binding(ollama, devices=devices)


def test_an_ollama_endpoint_off_the_machine_is_off_device(ollama):
    b = parse_config({"roles": {"r": {
        "provider": "ollama", "base_url": "https://ollama.example",
        "model": "m", "devices": {"cpu0": {}}}}}).binding("r")
    assert b.residence == "off_device"


# --------------------------------------------------------------------------
# 2. one load and one unload for N consumers, on the scheduled device
# --------------------------------------------------------------------------

def test_n_consumers_share_one_load_and_one_unload(ollama, schedule):
    schedule("edge", {"small": "cpu0"}, {"cpu0": "cpu"})
    provisions = _provisions(ollama)
    consumers = ["llm", "tagger", "summary"]
    for c in consumers:
        assert provisions.open(c, {"small"}) == ("small",)
    assert len(ollama.calls("load")) == 1
    for c in consumers[:-1]:
        provisions.close(c, ("small",))
        assert ollama.calls("unload") == []       # still held
    provisions.close(consumers[-1], ("small",))
    assert len(ollama.calls("load")) == 1
    assert len(ollama.calls("unload")) == 1
    small = provisions.get("small")
    assert (small.loads, small.unloads) == (1, 1)
    assert small.consumers == consumers
    assert provisions.residue() == {}              # no_residue
    assert ollama.loaded == {}


def test_the_load_goes_to_the_scheduled_device(ollama, schedule):
    schedule("edge", {"small": "cpu0"}, {"cpu0": "cpu"})
    provisions = _provisions(ollama)
    provisions.open("llm", {"small"})
    (load,) = ollama.calls("load")
    assert load == {"model": "tiny:1b", "keep_alive": -1,
                    "options": {"num_gpu": 0}}
    assert provisions.get("small").device == "cpu0"
    provisions.close("llm", ("small",))


def test_a_different_schedule_loads_on_a_different_device(ollama, schedule):
    schedule("edge", {"small": "gpu0"}, {"gpu0": "gpu"})
    provisions = _provisions(ollama)
    provisions.open("llm", {"small"})
    (load,) = ollama.calls("load")
    assert load["options"] == {}
    assert ollama.loaded == {"tiny:1b": SIZE}
    provisions.close("llm", ("small",))
    assert provisions.residue() == {}


def test_no_schedule_loads_nothing(ollama, schedule):
    provisions = _provisions(ollama)
    with pytest.raises(mp.ModelPlacementRefused,
                       match="handed no model schedule"):
        provisions.open("llm", {"small"})
    assert ollama.requests == []


def test_a_role_scheduled_elsewhere_is_not_loaded_here(ollama, schedule):
    schedule("edge", {"fast": "gpu0"}, {"gpu0": "gpu"})
    provisions = _provisions(ollama)
    assert provisions.open("llm", {"small"}) == ()
    assert ollama.requests == []


def test_a_device_the_binding_cannot_load_on_is_refused(ollama, schedule):
    schedule("edge", {"small": "npu0"}, {"npu0": "npu"})
    provisions = _provisions(ollama)
    with pytest.raises(ProvisionRefused,
                       match=r"places role `small` on device `npu0`.*"
                             r"names cpu0, gpu0"):
        provisions.open("llm", {"small"})
    assert ollama.requests == []


def test_a_member_the_server_misplaces_is_unloaded_and_refused(
        ollama, schedule):
    schedule("edge", {"small": "cpu0"}, {"cpu0": "cpu"})
    ollama.mode = "misplace"
    provisions = _provisions(ollama)
    with pytest.raises(ProvisionRefused,
                       match=r"a `cpu` device, but after the load the server "
                             r"holds 100% of it in GPU memory"):
        provisions.open("llm", {"small"})
    assert len(ollama.calls("load")) == 1
    assert len(ollama.calls("unload")) == 1
    assert ollama.loaded == {}
    assert provisions.residue() == {}


def test_a_member_that_spills_off_the_gpu_is_refused(ollama, schedule):
    schedule("edge", {"small": "gpu0"}, {"gpu0": "gpu"})
    ollama.mode = "spill"
    provisions = _provisions(ollama)
    with pytest.raises(ProvisionRefused,
                       match=r"a `gpu` device, but after the load the server "
                             r"holds 50% of it in GPU memory"):
        provisions.open("llm", {"small"})
    assert ollama.loaded == {}


def test_residue_names_a_member_the_server_still_holds(ollama, schedule):
    schedule("edge", {"small": "cpu0"}, {"cpu0": "cpu"})
    ollama.mode = "sticky"
    provisions = _provisions(ollama)
    provisions.open("llm", {"small"})
    provisions.close("llm", ("small",))
    assert provisions.residue() == {
        "small": ["the server still reports `tiny:1b` loaded"]}


def test_acquire_and_release_pair_per_consumer(ollama, schedule):
    schedule("edge", {"small": "cpu0"}, {"cpu0": "cpu"})
    small = _provisions(ollama).get("small")
    small.acquire("llm")
    with pytest.raises(ProvisionRefused, match="acquired model role `small` "
                                               "twice"):
        small.acquire("llm")
    with pytest.raises(ProvisionRefused, match="which it does not hold"):
        small.release("tagger")
    small.release("llm")
    assert (small.loads, small.unloads) == (1, 1)
    assert small.residue() == []


def test_the_timeline_records_each_load_and_unload(ollama, schedule):
    schedule("edge", {"small": "cpu0"}, {"cpu0": "cpu"})
    small = _provisions(ollama).get("small")
    small.acquire("llm")
    small.release("llm")
    load, unload = small.timeline
    assert load["event"] == "load" and load["device"] == "cpu0"
    assert load["server_load_ns"] == 4_200_000
    assert load["load_seconds"] >= 0
    assert unload["event"] == "unload" and unload["device"] == "cpu0"
    assert unload["at"] >= load["at"]


def test_an_unloaded_managed_adapter_refuses_to_complete(ollama):
    with pytest.raises(ProviderError, match="the model is not loaded"):
        Adapter(_binding(ollama)).complete(CompletionRequest(prompt="hi"))
    assert ollama.requests == []


# --------------------------------------------------------------------------
# 3. the model hosts are the consumers
# --------------------------------------------------------------------------

APP = """
model role small on_device device cpu memory 512 quant int8 reaches []

service Model { emission[model.small] fn label(text: Str) -> Str }
service Tagging { emission[model.small] fn tag(text: Str) -> Str }
service Answer { emission fn classify(text: Str) -> Str }
service Summary { emission fn summarize(text: Str) -> Str }
service Tags { emission fn tags(text: Str) -> Str }

component Classifier requires llm: Model provides out: Answer {
  route model on classify { confidential -> small }
  provide out {
    fn classify(text) {
      let a = emit llm.label(text)
      return a
    }
  }
}

component Summarizer requires llm: Model provides sum: Summary {
  route model on summarize { * -> small }
  provide sum {
    fn summarize(text) {
      let a = emit llm.label(text)
      return a
    }
  }
}

component Tagger requires tagger: Tagging provides tg: Tags {
  route model on tags { * -> small }
  provide tg {
    fn tags(text) {
      let a = emit tagger.tag(text)
      return a
    }
  }
}
"""


def _config_file(tmp_path, ollama, devices=None):
    cfg = tmp_path / "providers.json"
    cfg.write_text(json.dumps({"roles": {"small": {
        "provider": "ollama", "base_url": ollama.base, "model": "tiny:1b",
        "devices": devices or {"cpu0": {"num_gpu": 0}}}}}))
    return str(cfg)


def _hosts(tmp_path, ollama):
    app = tmp_path / "app.rvl"
    app.write_text(APP)
    ir = compile_files([str(app)])
    return bind_for_run(ir, [str(app)], _config_file(tmp_path, ollama))


def test_two_model_keys_share_one_provision(tmp_path, ollama, schedule):
    schedule("edge", {"small": "cpu0"}, {"cpu0": "cpu"})
    hosts = _hosts(tmp_path, ollama)
    assert sorted(hosts) == ["llm", "tagger"]
    assert hosts["llm"]._revl_provisions is hosts["tagger"]._revl_provisions
    open_hosts(hosts)
    assert len(ollama.calls("load")) == 1
    assert close_hosts(hosts) == []
    assert len(ollama.calls("unload")) == 1
    assert provision_residue(hosts) == {}


def test_every_call_carries_the_scheduled_device(tmp_path, ollama, schedule):
    schedule("edge", {"small": "cpu0"}, {"cpu0": "cpu"})
    hosts = _hosts(tmp_path, ollama)
    open_hosts(hosts)
    try:
        assert hosts["llm"].label("hello") == "label:hello"
        assert hosts["tagger"].tag("x") == "label:x"
    finally:
        close_hosts(hosts)
    chats = ollama.calls("chat")
    assert len(chats) == 2
    for chat in chats:
        assert chat["keep_alive"] == -1
        assert chat["options"]["num_gpu"] == 0
    assert len(ollama.calls("load")) == 1          # no implicit reload


def test_a_call_on_a_role_scheduled_elsewhere_is_refused(
        tmp_path, ollama, schedule):
    schedule("edge", {"fast": "gpu0"}, {"gpu0": "gpu"})
    hosts = _hosts(tmp_path, ollama)
    open_hosts(hosts)
    with pytest.raises(mp.ModelPlacementRefused,
                       match="model role `small` is not scheduled on host "
                             "`edge`"):
        hosts["llm"].label("hello")
    assert ollama.requests == []


def test_a_refused_open_leaves_nothing_loaded(tmp_path, ollama, schedule):
    schedule("edge", {"small": "cpu0"}, {"cpu0": "cpu"})
    ollama.mode = "misplace"
    hosts = _hosts(tmp_path, ollama)
    with pytest.raises(ProvisionRefused):
        open_hosts(hosts)
    assert ollama.loaded == {}
    assert provision_residue(hosts) == {}


# --------------------------------------------------------------------------
# 4. `revl run` end to end
# --------------------------------------------------------------------------

needs_cordis = pytest.mark.skipif(
    not _placement._cordis_py_installed(),
    reason="cordis-py runtime not installed (sh backends/python/setup.sh); "
           "these boot real py children")

HOST = """
[processes.edge]
components = ["Classifier", "Summarizer", "Tagger"]
probe = ["out.classify(\\"hello\\")", "tg.tags(\\"hi\\")"]

[[processes.edge.devices]]
name = "cpu0"
device = "cpu"
memory_mib = 1024
quantisation = ["int8"]
"""


def _write(tmp_path, placement=HOST):
    app = tmp_path / "app.rvl"
    app.write_text(APP, encoding="utf-8")
    plc = tmp_path / "placement.toml"
    plc.write_text(placement, encoding="utf-8")
    return str(app), str(plc)


_PROBE = re.compile(r"\] probe \| (.+?)\s*\| (.*)$")


@needs_cordis
def test_a_placement_loads_once_serves_and_unloads_once_with_no_residue(
        tmp_path, ollama, capfd):
    """The exit evidence: three components, two model keys, one role. The
    child loads the member once, on the scheduled `cpu0`, before anything
    activates; both probes reach it; teardown unloads it once, and the
    per-process residue proof says the server holds nothing."""
    app, plc = _write(tmp_path)
    rc = _placement.run_placement([app], plc, once=True,
                                  providers=_config_file(tmp_path, ollama))
    out = capfd.readouterr().out
    assert rc == 0, out
    loads, unloads = ollama.calls("load"), ollama.calls("unload")
    assert len(loads) == 1 and len(unloads) == 1, ollama.requests
    assert loads[0]["options"] == {"num_gpu": 0}
    assert len(ollama.calls("chat")) == 2
    probes = {m.group(1): m.group(2).strip()
              for m in map(_PROBE.search, out.splitlines()) if m}
    assert probes['out.classify("hello")'] == "=> 'label:hello'"
    assert probes['tg.tags("hi")'] == "=> 'label:hi'"
    assert "model role `small`: 2 consumer(s) (llm, tagger), 1 load(s) on " \
           "cpu0, 1 unload(s)" in out
    assert re.search(r"\[edge\] residue no residue \|.* models=unloaded", out)
    assert "[edge] DOWN" in out
    assert ollama.loaded == {}
    # the load happened before any component activated
    lines = out.splitlines()
    first_load = next(i for i, l in enumerate(lines) if "provision" in l)
    first_component = next(i for i, l in enumerate(lines) if "| load " in l
                           or "] load " in l)
    assert first_load < first_component


@needs_cordis
def test_an_unmanaged_role_is_served_in_the_child_and_loads_nothing(
        tmp_path, ollama, capfd):
    """`--providers` under `--placement` used to be ignored, leaving a model
    key "provided by no process". An endpoint that manages its own residency
    is now served in the child too, and revl loads and unloads nothing."""
    app, plc = _write(tmp_path)
    cfg = tmp_path / "providers.json"
    cfg.write_text(json.dumps({"roles": {"small": {
        "provider": "openai-compatible", "base_url": ollama.base + "/v1",
        "model": "tiny:1b"}}}))
    rc = _placement.run_placement([app], plc, once=True, providers=str(cfg))
    out = capfd.readouterr().out
    assert rc == 0, out
    probes = {m.group(1): m.group(2).strip()
              for m in map(_PROBE.search, out.splitlines()) if m}
    assert probes['out.classify("hello")'] == "=> 'shim:hello'"
    assert ollama.calls("load") == [] and ollama.calls("unload") == []
    assert "[edge] residue no residue |" in out
    assert "models=" not in out


@needs_cordis
def test_a_device_the_binding_cannot_load_on_refuses_before_spawning(
        tmp_path, ollama, capfd):
    app, plc = _write(tmp_path)
    cfg = _config_file(tmp_path, ollama, devices={"gpu0": {}})
    rc = _placement.run_placement([app], plc, once=True, providers=cfg)
    captured = capfd.readouterr()
    assert rc != 0
    assert ("the model schedule places role `small` on device `cpu0`"
            in captured.err)
    assert "[edge] UP" not in captured.out
    assert ollama.requests == []


@needs_cordis
def test_a_misplaced_load_refuses_the_boot(tmp_path, ollama, capfd):
    ollama.mode = "misplace"
    app, plc = _write(tmp_path)
    rc = _placement.run_placement([app], plc, once=True,
                                  providers=_config_file(tmp_path, ollama))
    out = capfd.readouterr().out
    assert rc != 0
    assert "[edge] BOOT REFUSED" in out
    assert "holds 100% of it in GPU memory" in out
    assert "[edge] UP" not in out
    assert ollama.loaded == {}


def test_a_single_process_run_has_no_schedule_to_load_on(tmp_path, ollama):
    """`revl run --providers` with no placement declares no host, so nothing
    says where a managed role is loaded: the boot is refused and the server
    is never asked to load anything."""
    app, _ = _write(tmp_path)
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    done = subprocess.run(
        [sys.executable, "-m", "revl", "run", app, "--providers",
         _config_file(tmp_path, ollama)],
        capture_output=True, text=True, input="", env=env, check=False,
        timeout=240)
    if "cordis" in done.stderr and "not installed" in done.stderr:
        pytest.skip("needs the cordis-py runtime")
    assert done.returncode == 1, done.stdout + done.stderr
    assert "handed no model schedule" in done.stderr
    assert ollama.calls("load") == []


# --------------------------------------------------------------------------
# 5. opt-in, against a local Ollama that holds nothing
# --------------------------------------------------------------------------

LIVE_MODEL = os.environ.get("REVL_LIVE_OLLAMA_PROVISION_MODEL")
LIVE_BASE = "http://127.0.0.1:11434"


def _live_ps():
    try:
        with urllib.request.urlopen(LIVE_BASE + "/api/ps", timeout=2) as r:
            return json.loads(r.read())
    except OSError:
        return None


@pytest.mark.skipif(not LIVE_MODEL,
                    reason="opt-in: set REVL_LIVE_OLLAMA_PROVISION_MODEL to a "
                           "pulled Ollama model tag")
def test_live_one_load_on_the_cpu_and_no_residue(schedule):
    ps = _live_ps()
    if ps is None:
        pytest.skip(f"no Ollama answering at {LIVE_BASE}")
    if ps.get("models"):
        pytest.skip("the local Ollama already holds a model; loading another "
                    "could evict it from under whoever is using it")
    schedule("local", {"small": "cpu0"}, {"cpu0": "cpu"})
    binding = parse_config({"roles": {"small": {
        "provider": "ollama", "base_url": LIVE_BASE, "model": LIVE_MODEL,
        "timeout": 900, "max_tokens": 16,
        "devices": {"cpu0": {"num_gpu": 0}}}}}).binding("small")
    adapter = Adapter(binding)
    provisions = Provisions({"small": adapter})
    opened = []
    try:
        for consumer in ("a", "b"):
            provisions.open(consumer, {"small"})
            opened.append(consumer)
        entry = adapter.residency()
        assert entry is not None and gpu_share(entry) == 0
        reply = adapter.complete(CompletionRequest(prompt="Say: ready"))
        assert reply.text.strip() or reply.reasoning.strip()
    finally:
        for consumer in reversed(opened):
            provisions.close(consumer, ("small",))
    small = provisions.get("small")
    assert (small.loads, small.unloads) == (1, 1)
    assert provisions.residue() == {}


# --------------------------------------------------------------------------
# 5. a swap successor and --providers
# --------------------------------------------------------------------------
#
# The boot path serves a model key inside the process that requires it and
# builds no proxy for it, so the successor of a swapped component must carry
# the same `providers` entry, or nothing serves the key after the cutover.
# `tests/test_swap_ref_pins.py` pins that the key is set on the successor at
# all; these pin what it is set to and when the swap refuses instead.

def test_a_successor_carries_the_predecessors_providers():
    old = {"providers": "/cfg/providers.json"}
    assert _placement._successor_providers(old, [], "Classifier", "py") == (
        "/cfg/providers.json", None)


def test_a_successor_with_no_model_keys_gets_no_providers():
    assert _placement._successor_providers({}, [], "Classifier", "node") == (
        None, None)


def test_a_successor_off_the_py_tier_refuses_the_swap():
    path, refusal = _placement._successor_providers(
        {"providers": "/cfg/providers.json"}, [], "Classifier", "node")
    assert path is None
    assert "binds no model host" in refusal and "py tier only" in refusal


def test_a_successor_of_a_managed_role_refuses_the_swap():
    """A provision belongs to one process: the successor would load the
    member and the predecessor's teardown would unload it again."""
    path, refusal = _placement._successor_providers(
        {"providers": "/cfg/providers.json"}, ["small"], "Classifier", "py")
    assert path is None
    assert "small" in refusal and "unload the member" in refusal
