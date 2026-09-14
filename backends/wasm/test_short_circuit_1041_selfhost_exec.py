"""The SELF-HOSTED wasm emitter's `&&`/`||` guards, EXECUTED (issue #1041).

`test_short_circuit_458_exec.py` beside this file runs the guards emitted by the
reference `backends/wasm/emit.py`. This one runs the guards emitted by
`selfhost/emit_wasm.rvl` — the revl port of that emitter, and the thing a
self-hosted compiler would actually be built from. The port kept lowering
`&&`/`||` to the strict `i32.and`/`i32.or` after the reference stopped, so a
wasm emitter compiled from the self-host source still produced a module that
TRAPPED on `d != 0 && n % d == 0` where the other five tiers answer 0.

The byte-agreement oracle could not see it: no corpus document spelled a
trapping right operand, so both emitters agreed on the strict form everywhere it
looked. `tests/fixtures/emit_wasm_corpus/shortcircuit.rvl` now spells one, and
this file states the consequence in wasmtime's own words rather than in bytes.

The toolchain gate follows this backend's policy (test_canonical_abi.py): a
missing `wasm-tools`/`wasmtime` is FATAL under REVL_REQUIRE_WASMTIME (CI) and a
named skip otherwise, never a silent pass.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, compile_source  # noqa: E402

_REQUIRE = os.environ.get("REVL_REQUIRE_WASMTIME", "").strip().lower() not in (
    "", "0", "false", "no")


def _wasmtime_binary() -> str | None:
    found = shutil.which("wasmtime")
    if found:
        return found
    fallback = Path(os.path.expanduser("~/.wasmtime/bin/wasmtime"))
    return str(fallback) if fallback.is_file() and os.access(fallback, os.X_OK) else None


def _toolchain() -> tuple[str, str]:
    wasm_tools = shutil.which("wasm-tools")
    wasmtime = _wasmtime_binary()
    if not wasm_tools or not wasmtime:
        if _REQUIRE:
            pytest.fail(
                "wasm-tools/wasmtime are missing, so the self-hosted emitter's "
                "short-circuit guard cannot be executed. REVL_REQUIRE_WASMTIME "
                "is set (CI), so this fails instead of skipping.", pytrace=False)
        pytest.skip("wasm-tools/wasmtime not installed "
                    "(set REVL_REQUIRE_WASMTIME=1 to make this a failure)")
    return wasm_tools, wasmtime


def _selfhost_emit_wasm_src():
    """`emit_wasm_src` as produced by compiling selfhost/emit_wasm.rvl through
    the python backend — the same build the byte-agreement oracle exercises."""
    ir = compile_files([str(ROOT / "selfhost" / "emit_wasm.rvl")])
    spec = importlib.util.spec_from_file_location(
        "selfhost_wasm_python_backend_1041", ROOT / "backends" / "python" / "emit.py")
    backend = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backend)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace: dict = {}
        exec(compile(backend.emit(ir), "selfhost_emit_wasm.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace["emit_wasm_src"]


SOURCE = """
fn bucket(n: Int, d: Int) -> Int {
  if (d != 0 && n % d == 0) { return 1 }
  return 0
}
fn either(n: Int, d: Int) -> Int {
  if (d == 0 || n % d == 0) { return 1 }
  return 0
}
fn ovf(n: Int, big: Bool) -> Int {
  if (big && n * n > 0) { return 1 }
  return 0
}
fn at(i: Int) -> Int {
  var ys = [3]
  if (i < ys.length() && ys[i] > 0) { return 1 }
  return 0
}
fn cmps(a: Int, b: Int) -> Int {
  if (a < b && a <= b) { return 1 }
  return 0
}
"""


@pytest.fixture(scope="module")
def module_fns(tmp_path_factory):
    wasm_tools, wasmtime = _toolchain()
    ir = compile_source(SOURCE)
    text = _selfhost_emit_wasm_src()({
        "ir_version": 3,
        "types": ir.get("types") or {},
        "functions": ir["functions"],
        "externs": [],
        "tests": [],
    })
    tmp = tmp_path_factory.mktemp("sc1041")
    wat, wasm = tmp / "fns.wat", tmp / "fns.wasm"
    wat.write_text(text, encoding="utf-8")
    subprocess.run([wasm_tools, "parse", str(wat), "-o", str(wasm)],
                   check=True, capture_output=True, text=True, timeout=120)
    subprocess.run([wasm_tools, "validate", "--features", "all", str(wasm)],
                   check=True, capture_output=True, text=True, timeout=120)
    return wasmtime, wasm


def _invoke(wasmtime: str, wasm: Path, export: str, *args: int) -> int:
    result = subprocess.run(
        [wasmtime, "run", "--invoke", export, str(wasm), *[str(a) for a in args]],
        capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise AssertionError(
            f"{export}({', '.join(str(a) for a in args)}) did not return: "
            f"{result.stderr.strip().splitlines()[-1] if result.stderr.strip() else '?'}")
    return int(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("export,args,expected", [
    # the guard fails: the right operand must not run
    ("bucket", (7, 0), 0),
    ("either", (7, 0), 1),
    ("ovf", (4000000000, 0), 0),
    # far enough past the end that the slot load leaves linear memory
    ("at", (100000000,), 0),
    # the guard holds: the right operand must still run and decide
    ("bucket", (6, 3), 1),
    ("bucket", (7, 3), 0),
    ("either", (7, 3), 0),
    ("ovf", (3, 1), 1),
    ("at", (0,), 1),
    # the control: a right operand that cannot trap keeps the strict form and
    # must of course still answer correctly
    ("cmps", (1, 2), 1),
    ("cmps", (2, 1), 0),
])
def test_the_self_hosted_guard_answers_what_the_reference_tier_answers(
        module_fns, export, args, expected):
    wasmtime, wasm = module_fns
    assert _invoke(wasmtime, wasm, export, *args) == expected
