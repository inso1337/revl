"""A compound value cannot cross a required service between two modules (issue #1601).

Each component is its own cordis-wasm instance with its own linear memory, and
the runtime forwards a coeffect call's arguments as the integers they are
(`provider.ops[op](*args)`). A `Str`/`List`/record/variant/`Opt`/`Result`
argument is an address in the CALLER's memory, so the provider read its own
memory at that address. Measured: a three-element list arrived with length 0,
`"hello"` with length 0, an index into the list trapped in `$list_slot`, and the
right answer appeared only when both modules happened to pool the same string
literal at the same offset. A compound RETURN is the mirror image.

The routed require (`_route_op_spec`) and the spawn instance accessor already
refuse this shape for this reason. The plain require now does too, by name,
at the consumer's call, when another component of the composition provides
the key. A key no component provides is the host's, and stays open. The provider's own export is unchanged: a host that
calls it reads the result out of the provider's exported memory, which is the
right memory.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl import run_wasm as run_wasm_mod  # noqa: E402

_REQUIRE = os.environ.get("REVL_REQUIRE_WASMTIME", "").strip().lower() not in (
    "", "0", "false", "no")

# A three-element List[Str], every element the Str "hi".
_NAMES = '''
extern pure fn names() -> List[Str] = @wasm {
  (i32.store (i32.const 6100) (i32.const 2))
  (i32.store8 (i32.const 6104) (i32.const 104))
  (i32.store8 (i32.const 6105) (i32.const 105))
  (i32.store (i32.const 6000) (i32.const 3))
  (i64.store (i32.const 6008) (i64.extend_i32_u (i32.const 6100)))
  (i64.store (i32.const 6016) (i64.extend_i32_u (i32.const 6100)))
  (i64.store (i32.const 6024) (i64.extend_i32_u (i32.const 6100)))
  (i32.const 6000)
}
'''

_PROVIDER = '''
service Front {
  fn count(src: List[Str]) -> Int
  fn width(s: Str) -> Int
  fn greet(n: Int) -> Str
  fn double(n: Int) -> Int
}

component Portal provides front: Front {
  provide front {
    fn count(src) = src.length()
    fn width(s) = s.length()
    fn greet(n) = "hello"
    fn double(n) = n * 2
  }
}
'''

# probe op -> (its body in the consumer, the value a correct crossing returns)
_PROBES = {
    "viaList": ("front.count(names())", 3),
    "viaStr": ('front.width("abc")', 3),
    "viaRet": ("front.greet(1).length()", 5),
}


def _consumer(op: str) -> str:
    body, _expected = _PROBES[op]
    return _NAMES + _PROVIDER + f'''
service Probe {{ fn {op}() -> Int }}
component Caller requires front: Front provides probe: Probe {{
  provide probe {{
    fn {op}() = {body}
  }}
}}
'''


def _emitter():
    return run_wasm_mod._wasm_emitter()


@pytest.mark.parametrize("op", sorted(_PROBES))
def test_a_compound_coeffect_crossing_is_refused_by_name(op):
    emit = _emitter()
    with pytest.raises(emit.EmitError) as caught:
        emit.emit(compile_source(_consumer(op), f"xmem_{op}.rvl"))
    message = str(caught.value)
    assert "Caller" in message and "front." in message, message
    assert "issue #1601" in message, message


def test_a_scalar_coeffect_crossing_still_emits():
    src = _NAMES + _PROVIDER + '''
service Probe { fn viaInt() -> Int }
component Caller requires front: Front provides probe: Probe {
  provide probe {
    fn viaInt() = front.double(21)
  }
}
'''
    modules = _emitter().emit(compile_source(src, "xmem_scalar.rvl"))
    assert '(import "coeffect:front" "double"' in modules["Caller"]


def test_a_compound_call_to_a_host_provided_key_still_emits():
    """A key no component of the composition provides is the host's, and
    cordis-wasm hands a host-provided coeffect the calling fiber, so the host
    reads the argument out of the caller's memory, which is the right one."""
    src = _NAMES + '''
service Front { fn count(src: List[Str]) -> Int }
service Probe { fn viaList() -> Int }
component Caller requires front: Front provides probe: Probe {
  provide probe {
    fn viaList() = front.count(names())
  }
}
'''
    modules = _emitter().emit(compile_source(src, "xmem_host.rvl"))
    assert '(import "coeffect:front" "count"' in modules["Caller"]


def test_a_provider_with_compound_operations_still_emits():
    """The defect is the consumer's crossing, not the provider's export: a
    host calling `provide:front.greet` reads the result from the provider's
    own memory, which is correct."""
    modules = _emitter().emit(compile_source(_NAMES + _PROVIDER, "xmem_provider.rvl"))
    assert '(export "provide:front.greet")' in modules["Portal"].replace("\n", " ") or \
        "provide:front.greet" in modules["Portal"]


# ---------------------------------------------------------------------------
# the measurement, on the live runtime
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def runtime():
    """The cordis-wasm `Runtime`, loaded by explicit path under its own module
    name, as backends/wasm/test_spawn_exec.py does: a plain `import runtime`
    would put it in `sys.modules["runtime"]`, where the py tier's runtime lives,
    and break every later test that imports that one."""
    try:
        import wasmtime  # noqa: F401, PLC0415
    except ImportError:
        if _REQUIRE:
            pytest.fail("wasmtime is not installed and REVL_REQUIRE_WASMTIME is set")
        pytest.skip("wasmtime Python package not installed")
    path = run_wasm_mod._cordis_wasm_dir() / "runtime.py"
    if not path.exists():
        if _REQUIRE:
            pytest.fail(f"cordis-wasm runtime not found at {path} and "
                        f"REVL_REQUIRE_WASMTIME is set")
        pytest.skip(f"cordis-wasm runtime not found at {path} (set CORDIS_WASM)")
    spec = importlib.util.spec_from_file_location("cordis_wasm_runtime", path)
    module = importlib.util.module_from_spec(spec)
    # registered before exec: the runtime's dataclasses resolve their field
    # types through sys.modules[cls.__module__].
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.Runtime


@pytest.mark.parametrize("op", sorted(_PROBES))
def test_no_crossing_answers_from_the_wrong_memory(op, runtime):
    """Refused, or the right answer. Never the provider reading its own memory
    at the caller's address, which is what every case did before."""
    ir = compile_source(_consumer(op), f"xmem_live_{op}.rvl")
    emit = _emitter()
    try:
        modules = emit.emit(ir)
    except emit.EmitError:
        return
    rt = runtime()
    for name in run_wasm_mod._load_order(ir):
        rt.plug(name, modules[name])
    assert rt.table["probe"].ops[op]() == _PROBES[op][1]
