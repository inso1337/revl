"""`&&`/`||` short-circuit on wasm, EXECUTED (roadmap item 458, issue #721).

The static half is `tests/test_458_logical_short_circuit.py`; this is the half
that proves the answer rather than the instruction.  wasm has no
short-circuiting instruction, and `i32.and`/`i32.or` are strict, so the three
ordinary guards below reached a trapping right operand even when the left one
had already decided the result:

    d != 0 && n % d == 0            `i64.rem_s` traps on a zero divisor
    i < xs.length() && xs[i] > 0    a slot load past the end of the list
    big && n * n > 0                the checked `$int_mul` at the Int edge

Every other tier short-circuits through its host operator and answers 0.  On
this tier the module TRAPPED — the same program, a different outcome, and no
diagnostic anywhere.  Measured on real `wasmtime`: pre-fix these invocations
exit non-zero with `wasm trap: integer divide by zero` /
`out of bounds memory access` / `wasm \\`unreachable\\` instruction executed`;
post-fix they return the reference tier's number.

Both wasm renderers are covered, because the tier has two: a module `fn` goes
through the v3 engine's `_bin_expr`, a provide-method body through
`_ComponentEmitter._scalar_operator`.

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
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

_REQUIRE = os.environ.get("REVL_REQUIRE_WASMTIME", "").strip().lower() not in (
    "", "0", "false", "no")


def _emitter():
    spec = importlib.util.spec_from_file_location(
        "revl_wasm_emit_458", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wasmtime_binary() -> str | None:
    """`wasmtime` from PATH, falling back to the default install dir — the
    installer only edits interactive shell profiles (mirrors tools/validate.py
    and test_str_literal_length_cli_exec.py)."""
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
                "wasm-tools/wasmtime are missing, so the short-circuit guard "
                "cannot be executed. REVL_REQUIRE_WASMTIME is set (CI), so "
                "this fails instead of skipping.", pytrace=False)
        pytest.skip("wasm-tools/wasmtime not installed "
                    "(set REVL_REQUIRE_WASMTIME=1 to make this a failure)")
    return wasm_tools, wasmtime


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

COMPONENT_SOURCE = """
service Bucket { fn pick(n: Int, d: Int) -> Int }
component B provides bucket: Bucket {
  provide bucket {
    fn pick(n, d) {
      if (d != 0 && n % d == 0) { return 1 }
      return 0
    }
  }
}
"""


def _build(source: str, tmp: Path, name: str, component: bool) -> tuple[str, Path]:
    wasm_tools, wasmtime = _toolchain()
    ir = compile_source(source)
    if component:
        emitted = _emitter().emit(ir)
    else:
        emitted = _emitter().emit({
            "ir_version": 3,
            "types": ir.get("types") or {},
            "functions": ir["functions"],
            "externs": [],
            "tests": [],
        })
    text = "\n".join(v for v in emitted.values() if isinstance(v, str))
    wat, wasm = tmp / f"{name}.wat", tmp / f"{name}.wasm"
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
    # `--invoke` prints the result on stdout (a deprecation note goes to stderr)
    return int(result.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def module_fns(tmp_path_factory):
    return _build(SOURCE, tmp_path_factory.mktemp("sc458"), "fns", component=False)


@pytest.mark.parametrize("export,args,expected", [
    # the guard fails: the right operand must not run
    ("bucket", (7, 0), 0),
    ("either", (7, 0), 1),
    ("ovf", (4000000000, 0), 0),
    # far enough past the end that the slot load leaves linear memory: a near
    # miss reads a neighbouring cell, and `i32.and` of a false left operand
    # still answers false, so only this index makes the strict form observable
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
def test_the_guard_answers_what_the_reference_tier_answers(module_fns, export,
                                                           args, expected):
    wasmtime, wasm = module_fns
    assert _invoke(wasmtime, wasm, export, *args) == expected


def test_a_provide_method_guard_answers_too(tmp_path):
    wasmtime, wasm = _build(COMPONENT_SOURCE, tmp_path, "comp", component=True)
    assert _invoke(wasmtime, wasm, "provide:bucket.pick", 7, 0) == 0
    assert _invoke(wasmtime, wasm, "provide:bucket.pick", 6, 3) == 1
