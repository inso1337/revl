"""The revl gate/compiler reachable as a bridge SERVICE (roadmap item 144,
Path A — first proving slice).

`src/revl/truc/components/gatekeeper.rvl` exposes revl's admission gate
*in-process*: a `@py` extern body calls `compile_files(sources,
manifest=running)` and a consumer in the same Python process reads the verdict.
This suite pins the same gate made reachable *across a process seam* — a py
`Gate` provider that a consumer on another tier (ts) admits a candidate through,
over the interop bridge (item 56).

Four levels, coarsest-runtime-need last:

1. the `Gate` service is transport-safe (the property that lets it cross a
   seam at all) — pure frontend, no runtime;
2. the conductor drops the gate's `@py`-only extern from the *ts* module and
   the consumer reaches the gate as a proxy instead (the design fork's fix) —
   pure frontend + the ts emitter, no runtime;
3. a candidate admits / a G2 collision is refused **over the real bridge
   transport**, verdict + why-trace surviving the round trip — asyncio + a UDS,
   no cordis;
4. the full cross-tier placement (a node consumer probing the py gate) — real
   node + both cordis runtimes; skips cleanly when any is absent, never a false
   pass.
"""

import asyncio
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl import placement as _placement  # noqa: E402
from revl.distribute import distributability  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.gate_service import admit, admit_case  # noqa: E402

GATE_RVL = str(ROOT / "examples" / "gate_service.rvl")
GATE_TOML = str(ROOT / "examples" / "placement" / "gate_pyts.toml")


def _emit(backend: str, ir: dict):
    spec = importlib.util.spec_from_file_location(
        f"emit_{backend}_gate", ROOT / "backends" / backend / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.emit(ir)


# --------------------------------------------------------------------------
# 1. the service is transport-safe (docs/interop-bridge.md §4)
# --------------------------------------------------------------------------

def test_gate_service_is_transport_safe():
    """Only a transport-safe service — every op `async fn`, value-typed — may
    cross a process seam. The gate qualifies precisely because an in-memory
    compile is effect-free (the extern is `pure`), so the op stays `async fn`
    rather than the `emission` truc's in-process gate uses."""
    ir = compile_files([GATE_RVL])
    verdict = distributability(ir)["Gate"]
    assert verdict["verdict"] == "transport-safe", verdict


# --------------------------------------------------------------------------
# 2. the design fork and its fix: the ts tier cannot emit the @py gate extern,
#    so the conductor drops it and the consumer reaches the gate as a proxy.
# --------------------------------------------------------------------------

def test_full_ir_ts_emit_refuses_the_py_only_gate_extern():
    """The fork: `run_placement` compiles ONE composition; handed the *whole*
    IR, the ts emitter rightly refuses `host_gate_admit` (no `@ts` body)."""
    ir = compile_files([GATE_RVL])
    with pytest.raises(Exception) as excinfo:
        _emit("typescript", ir)
    assert "host_gate_admit" in str(excinfo.value)
    assert "@ts body" in str(excinfo.value)


def test_ts_safe_ir_drops_the_gate_provider_and_its_py_extern():
    """The fix: the tier-emittable slice keeps the consumer + the service
    interface and drops the py-only provider body, so the ts module emits and
    the consumer reaches the gate through a bridge proxy (spec `proxies`)."""
    ir = compile_files([GATE_RVL])
    safe = _placement.ts_safe_ir(ir)
    assert [c["name"] for c in safe["components"]] == ["GateUser"]
    assert safe["externs"] == []
    emitted = _emit("typescript", safe)
    assert "GateUser" in emitted
    assert "host_gate_admit" not in emitted


def test_ts_safe_ir_is_a_noop_without_py_only_externs():
    """A composition the ts tier can already emit whole is returned unchanged —
    existing node placements are byte-for-byte unaffected."""
    plain = compile_files([str(ROOT / "examples" / "user_cache.rvl")])
    assert _placement.ts_safe_ir(plain) is plain


# --------------------------------------------------------------------------
# 3. a real admission over the bridge transport (asyncio + a UDS; no cordis).
#    This is the same JSON-over-socket wire a ts `makeProxy` speaks, so it
#    proves the verdict crosses a process seam tier-agnostically.
# --------------------------------------------------------------------------

def _bridge():
    spec = importlib.util.spec_from_file_location(
        "revl_gate_bridge", ROOT / "backends" / "python" / "bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _GateService:
    """The py provider object served over the bridge — the same two operations
    `service Gate` declares, delegating to the in-process gate."""

    def admit(self, sources_json, manifest_json):
        return admit(sources_json, manifest_json)

    def admit_case(self, case_name):
        return admit_case(case_name)


class _Ctx:
    def __init__(self, service):
        self._service = service

    def get(self, key):
        return self._service if key == "gate" else None


def _serve_and_call(requests):
    """Serve the gate on a real UDS and issue `requests` over the wire,
    returning the parsed verdicts (the bridge reply's `value`)."""
    bridge = _bridge()
    directory = tempfile.mkdtemp(prefix="revl_gate_bridge_")
    sock = str(Path(directory) / "gate.sock")

    async def scenario():
        server = await bridge.serve(_Ctx(_GateService()),
                                    {"gate": ["admit", "admit_case"]}, sock)
        reader, writer = await asyncio.open_unix_connection(sock)

        async def rpc(request):
            writer.write((json.dumps(request) + "\n").encode())
            await writer.drain()
            return json.loads(await reader.readline())

        replies = [await rpc(r) for r in requests]
        writer.close()
        server.close()
        await server.wait_closed()
        return replies

    try:
        return asyncio.run(scenario())
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _running_manifest():
    """A running composition (one provider of key `thing`) as the manifest a
    candidate is admitted against."""
    sources = {
        "/gate_test/base.rvl":
            "service Thing { fn ping() -> Int }\n"
            "component Base provides thing: Thing { provide thing { fn ping() = 1 } }\n",
    }
    ir = compile_files(list(sources), sources=sources)
    return json.dumps({"manifest": ir.get("manifest") or {},
                       "services": ir.get("services") or {}})


def test_clean_candidate_admits_over_the_bridge():
    running = _running_manifest()
    candidate = json.dumps({
        "/gate_test/extra.rvl":
            "component Extra provides other: Thing { provide other { fn ping() = 3 } }\n"})
    [reply] = _serve_and_call([
        {"key": "gate", "method": "admit", "args": [candidate, running]}])
    assert reply["ok"] is True, reply
    verdict = json.loads(reply["value"])
    assert verdict["ok"] is True
    assert verdict["admitted"] == ["Extra"]


def test_g2_collision_is_refused_over_the_bridge_with_its_why_trace():
    """The proving exit: a candidate colliding on a provided key is refused, and
    the refusal — G2, naming the conflicting key and the why-trace — survives
    the round trip back across the seam."""
    running = _running_manifest()
    candidate = json.dumps({
        "/gate_test/dup.rvl":
            "component Dup provides thing: Thing { provide thing { fn ping() = 2 } }\n"})
    [reply] = _serve_and_call([
        {"key": "gate", "method": "admit", "args": [candidate, running]}])
    assert reply["ok"] is True, reply  # the seam call itself succeeded
    verdict = json.loads(reply["value"])
    assert verdict["ok"] is False                       # the candidate is refused
    assert "(G2)" in verdict["diagnostic"]
    assert "thing" in verdict["diagnostic"]             # the conflicting key
    assert "why" in verdict["diagnostic"]               # the why-trace crossed
    assert verdict["admitted"] == []


def test_admit_case_probe_path_crosses_the_bridge_both_ways():
    """The fixture path a placement *probe* drives: `collide` refused, `clean`
    admitted — the exact two calls the node consumer makes in gate_pyts.toml."""
    collide, clean = _serve_and_call([
        {"key": "gate", "method": "admit_case", "args": ["collide"]},
        {"key": "gate", "method": "admit_case", "args": ["clean"]}])
    collide_v = json.loads(collide["value"])
    clean_v = json.loads(clean["value"])
    assert collide_v["ok"] is False and "(G2)" in collide_v["diagnostic"]
    assert clean_v["ok"] is True and clean_v["admitted"] == ["Extra"]


def test_the_gate_object_never_raises_across_the_boundary():
    """A refusal returns as a verdict, not an exception — the seam stays total
    (a provider that raised would look like a peer fault, not a refusal)."""
    with pytest.raises(RevlError):
        # in-process, the compiler raises...
        compile_files(["/x/dup.rvl"], sources={
            "/x/dup.rvl": "component Bad provides p: Nope { provide p { fn f() = 1 } }"})
    # ...but through the gate body it is a value:
    verdict = json.loads(admit(json.dumps({
        "/x/dup.rvl": "component Bad provides p: Nope { provide p { fn f() = 1 } }"}), ""))
    assert verdict["ok"] is False and verdict["diagnostic"]


# --------------------------------------------------------------------------
# 4. placement wiring: the node consumer gets a proxy for `gate`, the py
#    provider serves it — asserted on the specs the conductor writes, with the
#    real ts emit run (so the filter is exercised end to end), Popen mocked.
# --------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, name):
        import io
        self.stdout = io.StringIO(f"[{name}] UP\n")
        self.stdin = io.StringIO()

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


def test_placement_gives_the_ts_consumer_a_proxy_and_the_py_gate_serves(monkeypatch):
    """The conductor wires the seam: `user` (node) receives a proxy for `gate`
    with the declared method allowlist; `gate` (py) serves that key. The node
    module is emitted from the tier-safe slice (no `@py` extern)."""
    monkeypatch.setattr(_placement, "_cordis_py_installed", lambda: True)
    # node is required only for the preflight; skip it (this test asserts
    # wiring, not that a runtime booted). The real ts emit still runs below.
    monkeypatch.setattr(_placement, "_preflight", lambda *a, **k: None)

    specs = {}
    real_popen = _placement.subprocess.Popen

    def fake_popen(cmd, **kwargs):
        # only intercept the per-process runner spawns (a `*.spec.json` argv);
        # let the real ts-emit subprocess (`emit.py ... ir.json`) run so the
        # tier-safe filter is exercised end to end.
        if not str(cmd[-1]).endswith(".spec.json"):
            return real_popen(cmd, **kwargs)
        spec = json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))
        specs[spec["name"]] = spec
        return _FakeProc(spec["name"])

    monkeypatch.setattr(_placement.subprocess, "Popen", fake_popen)
    rc = _placement.run_placement([GATE_RVL], GATE_TOML, once=True)
    assert rc == 0, specs

    # the consumer crosses to the gate as a proxy, with exactly `service Gate`'s
    # operations as the forwarded allowlist
    proxy = specs["user"]["proxies"]["gate"]
    assert sorted(proxy["methods"]) == ["admit", "admit_case"]
    # a compiler-service call is bounded like any seam call: the proxy inherits
    # the bridge deadline (item 54/56), so a wedged gate breaches a SeamDeadline
    # rather than blocking the consumer forever.
    assert proxy["deadline"] == _placement.DEFAULT_SEAM_DEADLINE
    assert specs["user"]["backend"] == "node"
    assert specs["user"]["components"] == ["GateUser"]
    assert "GateProvider" not in specs["user"]["components"]

    # the py provider serves the key the consumer proxies
    serve = specs["gate"]["serve"]
    assert serve["keys"] == ["gate"]
    assert sorted(serve["methods"]["gate"]) == ["admit", "admit_case"]

    # the consumer was handed a real emitted node module (the conductor ran the
    # ts emit through `ts_safe_ir` above; what that slice contains is pinned by
    # test_ts_safe_ir_drops_the_gate_provider_and_its_py_extern).
    assert specs["user"]["module"].endswith(".ts")


# --------------------------------------------------------------------------
# 5. the full cross-tier boot: a node consumer probes the py gate for real.
#    Skips cleanly when node or either cordis runtime is absent.
# --------------------------------------------------------------------------

def _cross_tier_ready():
    if shutil.which("node") is None:
        return "node is not on PATH"
    if not _placement._cordis_py_installed():
        return "cordis-py runtime not installed (sh backends/python/setup.sh)"
    if not (_placement._TS_DIR / "node_modules" / "cordis").is_dir():
        return "cordis-ts runtime not installed (cd backends/typescript && npm install)"
    return None


def test_ts_consumer_admits_against_the_py_gate_over_the_bridge(capfd):
    """The definition-of-done proof: run the placement for real — a node
    `GateUser` probes the py `Gate` across the interop bridge. The G2 collision
    is refused and the clean candidate admitted, cross-tier, in the log."""
    reason = _cross_tier_ready()
    if reason:
        pytest.skip(reason)
    rc = _placement.run_placement([GATE_RVL], GATE_TOML, once=True)
    out = capfd.readouterr().out
    assert rc == 0, out
    # the probe verdicts crossed back to the node process and were logged there
    assert "collide" in out and "clean" in out
    assert "(G2)" in out                     # the refusal's guarantee crossed
    # the clean admit crossed. The verdict is a `Str`, so each consumer tier
    # prints it in its own quoting convention: the py tier's repr(str) keeps the
    # inner double quotes (`"ok": true`); a node/go consumer quotes the whole
    # string and escapes the inner quotes (`\"ok\": true`, the form this
    # node-tier `GateUser` emits); a dict verdict would print as `'ok': True`.
    # Any of the three proves the ok=true verdict rode back over the seam.
    assert ('"ok": true' in out or "'ok': True" in out
            or r'\"ok\": true' in out), out                # the clean admit crossed
