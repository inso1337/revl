"""Canonical-ABI WASI Preview 2 component emission — item 41 slice-3.

Run with:
    .venv/bin/pytest backends/wasm/test_canonical_abi.py -q

The emit/golden/refusal tests run everywhere. The build+execute test needs the
standard toolchain (`wasm-tools` to wrap the core module into a component,
`wasmtime` to run it under the component model); it skips when they are absent
unless REVL_REQUIRE_WASMTIME is set (CI), matching the wasm tier's existing
policy in test_v3_emit.py — a missing runtime is a hard failure in CI, a quiet
skip in local dev.
"""

import importlib.util
import os
import pathlib
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
GOLDEN = BACKEND / "golden"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(BACKEND))

from revl import compile_source  # noqa: E402


def _canonical():
    spec = importlib.util.spec_from_file_location(
        "revl_wasm_canonical", BACKEND / "canonical.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_REQUIRE = os.environ.get("REVL_REQUIRE_WASMTIME", "").strip().lower() not in (
    "", "0", "false", "no")

# The fixed source and its goldens are the single source of truth; regenerate
# with `python3 backends/wasm/golden/regen_canonical.py`.
_SRC = (GOLDEN / "canonical_echoer.revl").read_text(encoding="utf-8")
_SERVICE = "Echoer"


def _emit():
    return _canonical().emit_component(compile_source(_SRC), service=_SERVICE)


# --------------------------------------------------------------------------- #
# Emit + golden — runs everywhere, no toolchain needed.
# --------------------------------------------------------------------------- #

def test_canonical_core_wat_matches_golden():
    res = _emit()
    golden = (GOLDEN / "canonical_echoer.core.wat").read_text(encoding="utf-8")
    assert res["core_wat"] == golden


def test_canonical_wit_matches_golden():
    res = _emit()
    golden = (GOLDEN / "canonical_echoer.wit").read_text(encoding="utf-8")
    assert res["wit"] == golden


def test_canonical_core_has_the_boundary_machinery():
    """The three things a standard component-model host needs that the custom
    tier never emitted: cabi_realloc, an interface-qualified export name, and
    the bare->internal string lift."""
    core = _emit()["core_wat"]
    assert '(func (export "cabi_realloc")' in core
    assert '(func (export "revl:exported/echoer#echo")' in core
    assert "$__canon_lift_str" in core


def test_canonical_wit_interface_is_export_wit_verbatim():
    """The component's exported interface must be exactly what `revl export wit`
    (slice-1) prints — the binary and the interface documentation agree."""
    from revl.export_wit import export_wit  # noqa: PLC0415
    res = _emit()
    ir = compile_source(_SRC)
    synthetic = {"services": {_SERVICE: {"methods": {
        fn["name"]: {"params": fn.get("params") or [], "returns": fn.get("returns")}
        for fn in ir.get("functions") or [] if fn.get("returns") == "Str"
        and all(p.get("type") == "Str" for p in fn.get("params") or [])}}},
        "types": {}, "externs": []}
    interface = export_wit(synthetic, service=_SERVICE, package="revl:exported")
    # every non-empty line of the exported interface appears verbatim in the WIT
    assert interface.rstrip() in res["wit"]


def test_refuses_when_no_str_boundary_function():
    canonical = _canonical()
    ir = compile_source("fn add(a: Int, b: Int) -> Int { return a + b }")
    with pytest.raises(canonical.EmitError, match="no canonical-ABI-emittable"):
        canonical.emit_component(ir, service="Math")


def test_non_str_functions_stay_off_the_interface_but_in_the_core():
    """A helper with a non-Str signature is not on the component interface, yet
    it remains in the core module so a boundary function can still call it."""
    canonical = _canonical()
    ir = compile_source(
        "fn n2s(n: Int) -> Str { return `${n}` }\n"
        "fn tag(s: Str) -> Str { return `[${s}]` }\n")
    res = canonical.emit_component(ir, service="Tagger")
    assert res["functions"] == ["tag"]              # only the Str->Str one
    assert "$n2s" in res["core_wat"]                # helper still present
    assert 'export "revl:exported/tagger#n2s"' not in res["core_wat"]


# --------------------------------------------------------------------------- #
# Build + execute under wasmtime's COMPONENT MODEL — the real proof.
# --------------------------------------------------------------------------- #

def _toolchain_or_skip(canonical):
    if canonical.wasm_tools_binary() is None or canonical.wasmtime_binary() is None:
        if _REQUIRE:
            pytest.fail(
                "wasm-tools and/or wasmtime absent, so the canonical-ABI "
                "component cannot be built and run. REVL_REQUIRE_WASMTIME is "
                "set (CI), so this fails instead of skipping.", pytrace=False)
        pytest.skip("wasm-tools/wasmtime not installed "
                    "(set REVL_REQUIRE_WASMTIME=1 to make this a failure)")


def test_component_builds_validates_and_round_trips(tmp_path):
    canonical = _canonical()
    _toolchain_or_skip(canonical)
    res = _emit()
    component = canonical.build_component(
        res["core_wat"], res["wit"], tmp_path, res["world"], name="echoer")
    # loads as a valid component under the component model
    canonical.validate_component(component)
    # the canonical string ABI round-trips both directions, run by wasmtime's
    # component model (wasmtime --invoke only accepts a component here)
    assert canonical.run_component_str(component, "echo", "world") == "world"
    assert canonical.run_component_str(component, "shout", "hi") == "hi!"
    assert canonical.run_component_str(component, "greet", "revl") == "Hello, revl!"
    # empty string is a real canonical case (ptr valid, len 0)
    assert canonical.run_component_str(component, "echo", "") == ""
