"""`Str.length` PROPERTY form on a multibyte literal, EXECUTED via the CLI.

Companion to `test_str_literal_length_exec.py` (which drives the cordis-wasm
Python runtime, skipping when the `wasmtime` *Python package* is absent). This
one runs the same item-104 property-form cases through the `wasm-tools` and
`wasmtime` **command-line binaries**, so the execution guard still fires in an
environment that has the CLI toolchain but not the Python package — for example
this repo's default dev box.

Item 104: `"café".length` (the PROPERTY form, no parens) lowered as a `len` IR
node through `_len_expr`, which read the u32 byte-length prefix directly and so
answered 5 (the UTF-8 byte count) instead of 4 (the code-point count). The fix
routes a `Str` `len` node through `$str_cp_length`, exactly like the method
form. This test PINS the fix BY EXECUTION: a regression to the byte-length load
flips café -> 5, 日本語 -> 9, naïve -> 6 and goes red on real wasmtime.
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


def _emitter():
    spec = importlib.util.spec_from_file_location("revl_wasm_emit", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wasmtime_binary() -> str | None:
    """`wasmtime` from PATH, falling back to the default install dir (the
    installer only edits interactive shell profiles, so a non-login shell can
    have a working wasmtime that is not on PATH). Mirrors tools/validate.py."""
    found = shutil.which("wasmtime")
    if found:
        return found
    fallback = Path(os.path.expanduser("~/.wasmtime/bin/wasmtime"))
    return str(fallback) if fallback.is_file() and os.access(fallback, os.X_OK) else None


def _require_toolchain() -> tuple[str, str]:
    wasm_tools = shutil.which("wasm-tools")
    if not wasm_tools:
        pytest.skip("wasm-tools not found on PATH")
    wasmtime = _wasmtime_binary()
    if not wasmtime:
        pytest.skip("wasmtime not found (PATH or ~/.wasmtime/bin)")
    return wasm_tools, wasmtime


def _run_property_length(literal: str, tmp: Path) -> int:
    """Emit `fn M() -> Int { return "<literal>".length }` (property form), parse
    it to a module with wasm-tools, and execute M on the wasmtime CLI."""
    wasm_tools, wasmtime = _require_toolchain()
    ir = compile_source(f'fn M() -> Int {{ return "{literal}".length }}')
    module = _emitter().emit({
        "ir_version": 3,
        "types": ir.get("types") or {},
        "functions": ir["functions"],
        "externs": [],
        "tests": [],
    })
    wat = tmp / "len.wat"
    wasm = tmp / "len.wasm"
    wat.write_text(module["functions"], encoding="utf-8")
    subprocess.run([wasm_tools, "parse", str(wat), "-o", str(wasm)],
                   check=True, capture_output=True, text=True, timeout=120)
    subprocess.run([wasm_tools, "validate", "--features", "all", str(wasm)],
                   check=True, capture_output=True, text=True, timeout=120)
    result = subprocess.run(
        [wasmtime, "run", "--invoke", "M", str(wasm)],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(f"wasmtime run failed: {result.stderr.strip()}")
    # `--invoke` prints the result on stdout (a deprecation note goes to stderr).
    return int(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    "literal,code_points,byte_len",
    [
        ("café", 4, 5),
        ("日本語", 3, 9),
        ("naïve", 5, 6),
        ("abc", 3, 3),
    ],
)
def test_property_length_counts_code_points_on_cli(literal, code_points, byte_len, tmp_path):
    got = _run_property_length(literal, tmp_path)
    assert got == code_points, (
        f'"{literal}".length (property form) executed to {got}; expected '
        f"{code_points} code points (byte-count regression would give {byte_len})"
    )
    if byte_len != code_points:
        assert got != byte_len, (
            f'"{literal}".length (property form) returned its UTF-8 byte count '
            f"{byte_len} — the `len` node folded to the byte-length load instead "
            "of routing through $str_cp_length (item 104)"
        )
