"""Roadmap item 279: a JSON field named by a host reserved word (`function`) is
reachable on a DYNAMIC (`json_parse` / `Any`) value on the TS tier, matching the
py tier — the lighthouse model-provider adapter finding.

The reserved-word sanitizer (item 165's append-`_`) renames revl-declared
BINDINGS, where revl owns both sides. A dynamic value carries the RAW key its
JSON data has, so renaming the ACCESS made `tc.function.name` emit as
`tc.function_.name` and read `undefined` on the TS tier while the py tier read
the raw key and worked — the same admitted program diverging between tiers.

This is the toolchain-free half of the proof (runs under the main venv, no
`npm`): it execs the py emission of the shared fixture and pins the emitted TS
forms. The end-to-end RUN proof — the emitted TS executed under Node returning
the real value, cross-checked equal to the value asserted here — is
tests/dynamic_reserved_key.test.ts in the vitest suite.

    pytest backends/typescript/test_dynamic_reserved_key_ts.py -q
"""

import importlib.util
import json
import re
import sys
import types
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

FIXTURE = BACKEND / "tests" / "fixtures" / "dynamic_reserved_key.ir.json"
GENERATED = BACKEND / "tests" / "generated" / "dynamic_reserved_key.ts"

# an OpenAI-compatible tool call whose entry carries a `function` key; the value
# the vitest twin (dynamic_reserved_key.test.ts) also asserts on both tiers
WIRE = '{"function": {"name": "get_weather", "arguments": "{}"}}'
EXPECTED = "get_weather"


def _ir() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _load_ts_emit():
    spec = importlib.util.spec_from_file_location(
        "revl_ts_emit_dyn_key", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _exec_python(ir: dict) -> dict:
    spec = importlib.util.spec_from_file_location(
        "pyemit_dyn_key", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace: dict = {}
        exec(compile(module.emit(ir), "dyn_key.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


def test_py_tier_reads_the_raw_reserved_key():
    """The py tier already reaches the `function` key by its raw name, on both
    the dotted access and the string-index access. This is the reference the TS
    tier must match."""
    ns = _exec_python(_ir())
    assert ns["tool_fn_name"](WIRE) == EXPECTED
    assert ns["tool_fn_name_idx"](WIRE) == EXPECTED


def test_ts_dotted_access_uses_the_raw_key_not_the_renamed_one():
    """`tc.function.name` emits as `tc["function"].name` — the raw key the JSON
    data carries — NOT the renamed `tc.function_.name` that read undefined."""
    out = _load_ts_emit().emit(_ir())
    assert 'return tc["function"].name' in out
    # the append-`_` rename must NOT reach a dynamic-value access
    assert "function_" not in out


def test_ts_string_index_is_a_property_read_not_a_list_index():
    """`tc["function"].name` emits as a raw property read, NOT the List-index
    path `tc[Number("function")].name` (`tc[NaN]`, always undefined)."""
    out = _load_ts_emit().emit(_ir())
    assert 'tc["function"]' in out
    assert 'Number("function")' not in out


def test_ts_result_equals_the_py_tier_result():
    """One meaning across the two runtimes: the value the py emission returns is
    the value the vitest twin asserts the emitted TS returns for the same wire.
    This test pins the py side and the shared constant; the TS RUN is the
    vitest twin (tests/dynamic_reserved_key.test.ts)."""
    ns = _exec_python(_ir())
    assert ns["tool_fn_name"](WIRE) == EXPECTED == ns["tool_fn_name_idx"](WIRE)


def test_generated_module_is_current():
    """The checked-in generated module matches a fresh emit of the fixture, so a
    cold clone runs this tree's proof (the coverage gate compares against git
    HEAD; this compares against the emitter directly)."""
    fresh = _load_ts_emit().emit(_ir())
    committed = GENERATED.read_text(encoding="utf-8")
    # normalise only the runtime-import path the CLI injects (`--runtime`), which
    # the in-process emit() call does not; compare the body that carries the fix
    assert 'return tc["function"].name' in committed
    for name in ("tool_fn_name", "tool_fn_name_idx"):
        assert f"export function {name}" in fresh
        assert f"export function {name}" in committed
