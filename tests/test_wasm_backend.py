"""Substrate-tier backend: WAT emission (pure) and the end-to-end demo
against the cordis-wasm runtime (skips when that project/venv is absent)."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, compile_source  # noqa: E402

CORDIS_WASM_PY = Path.home() / "Projects" / "cordis-wasm" / ".venv" / "bin" / "python"


def _emitter():
    spec = importlib.util.spec_from_file_location("revl_wasm_emit", ROOT / "backends" / "wasm" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_beacon_emits_goldens():
    ir = compile_files([str(ROOT / "examples" / "beacon.rvl")])
    modules = _emitter().emit(ir)
    for name in ("Beacon", "Auditor"):
        golden = (ROOT / "backends" / "wasm" / "golden" / f"{name}.wat").read_text()
        assert modules[name] == golden


def test_pulse_await_lowering():
    """A1 on the substrate: the await segment launches the async host op and
    the boundary yield structure survives in the golden."""
    ir = compile_files([str(ROOT / "examples" / "pulse.rvl")])
    pulse = _emitter().emit(ir)["Pulse"]
    golden = (ROOT / "backends" / "wasm" / "golden" / "Pulse.wat").read_text()
    assert pulse == golden
    assert '(import "host" "job_run"' in pulse
    assert "(call $host_job_run (i32.const 42))" in pulse
    # the effect after the await is a separate segment: divert can skip it
    assert pulse.index("job_run (i32.const 42)") < pulse.index("(i32.const 2) (i32.const 22)")


def test_import_section_is_the_coeffect_specification():
    ir = compile_files([str(ROOT / "examples" / "beacon.rvl")])
    beacon = _emitter().emit(ir)["Beacon"]
    assert '(import "coeffect:kv" "get"' in beacon
    assert '(import "coeffect:kv" "set"' in beacon
    assert '(export "provide:status.shared")' in beacon
    # the accumulator: inverses guarded by completed-step count, LIFO
    assert beacon.index("i32.const 8) (i32.const 0)") < beacon.index("i32.const 7) (i32.const 0)")


def test_v2_realms_lower_to_realm_namespaces():
    ir = compile_source(
        """
        service Kv {
          fn get(k: Int) -> Int
          fn set(k: Int, v: Int)
        }
        component StoreA provides kv: Kv {
          isolate kv in realm("tenant_a")
          provide kv {
            fn get(k) { return k }
            fn set(k, v) { }
          }
        }
        component AppA requires kv: Kv {
          isolate kv in realm("tenant_a")
          intercept kv with { quota: 5 }
          effect kv.set(1, 10) undo kv.set(1, 0)
        }
        """
    )
    modules = _emitter().emit(ir)
    assert '(import "coeffect:tenant_a/kv" "set"' in modules["AppA"]
    assert '(export "provide:tenant_a/kv.get")' in modules["StoreA"]
    assert '(@custom "revl:isolate" "{\\"kv\\": \\"tenant_a\\"}")' in modules["AppA"]
    assert '(@custom "revl:intercept" "{\\"kv\\": {\\"quota\\": 5}}")' in modules["AppA"]


def test_tier_restrictions_are_hard_errors():
    emitter = _emitter()
    # user_cache is string-shaped and configured: must be rejected (the
    # config check fires first), never silently degraded
    ir = compile_files([str(ROOT / "examples" / "user_cache.rvl")])
    with pytest.raises(emitter.EmitError, match="not lowerable"):
        emitter.emit(ir)
    # await steps: the runtime implements the sync base calculus
    ir = {
        "ir_version": 1,
        "services": {},
        "components": [{
            "name": "Waiter", "config": [], "requires": {}, "provides": {},
            "body": [{"step": "await", "expr": {"kind": "lit", "value": 1}}],
        }],
    }
    with pytest.raises(emitter.EmitError, match="await"):
        emitter.emit(ir)


@pytest.mark.skipif(not CORDIS_WASM_PY.exists(), reason="cordis-wasm venv not available")
def test_demo_runs_on_the_real_substrate():
    result = subprocess.run(
        [str(CORDIS_WASM_PY), str(ROOT / "backends" / "wasm" / "demo.py")],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all checks passed" in result.stdout
    assert "[FAIL]" not in result.stdout
