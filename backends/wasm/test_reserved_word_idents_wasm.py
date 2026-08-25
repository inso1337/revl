"""Roadmap item 165: a valid revl identifier that collides with a target
reserved word emits and RUNS on the wasm tier.

Unlike the textual tiers, the wasm backend is *structurally* immune: every user
identifier is emitted as a WAT identifier in the sigil namespace (`$p_<param>`,
`$l_<local>`, `$<fn>`), which is disjoint from WAT's bare keyword tokens
(`func`, `param`, `local`, `i64`, …). A revl parameter named `func` becomes
`$p_func`, which cannot collide with the WAT keyword `func`. So no reserved-word
set or mangling is needed here — the sigil prefix IS the universal, collision-
free rename. These tests pin that property and, where the `wasmtime` binary is
present, EXECUTE a keyword-named function to prove it end to end.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402


def _emitter():
    spec = importlib.util.spec_from_file_location("revl_wasm_emit_rw", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROGRAM = """
pub fn probe(func: Int, chan: Int) -> Int {
  let range = func
  let map = chan
  return range
}
"""


def _functions_wat() -> str:
    modules = _emitter().emit(compile_source(PROGRAM))
    return modules["functions"]


def test_keyword_identifiers_are_sigil_namespaced():
    wat = _functions_wat()
    # the exported name is the source spelling (a string, never a WAT token);
    # the param/local identifiers live in the disjoint sigil namespace
    assert '(func $probe (export "probe")' in wat
    assert "(param $p_func i64)" in wat
    assert "(param $p_chan i64)" in wat
    assert "(local $l_range i64)" in wat
    assert "(local $l_map i64)" in wat
    # no bare WAT-keyword identifier was ever emitted for a user name
    assert "(param $func " not in wat
    assert "(local $range " not in wat


def _wasmtime() -> str | None:
    found = shutil.which("wasmtime")
    if found:
        return found
    fallback = Path(os.path.expanduser("~/.wasmtime/bin/wasmtime"))
    return str(fallback) if fallback.exists() else None


def test_wasmtime_runs_keyword_named_function():
    """Execute `probe(5, 7)` on the wasmtime CLI (it parses `.wat` directly) —
    the keyword-named parameter `func` flows through the keyword-named local
    `range` and back, returning 5. Skips cleanly without wasmtime."""
    wasmtime = _wasmtime()
    if not wasmtime:
        pytest.skip("wasmtime not found (PATH or ~/.wasmtime/bin)")
    with tempfile.TemporaryDirectory() as d:
        wat = Path(d) / "functions.wat"
        wat.write_text(_functions_wat(), encoding="utf-8")
        result = subprocess.run(
            [wasmtime, "run", "--invoke", "probe", str(wat), "5", "7"],
            capture_output=True, text=True, timeout=120,
        )
    assert result.returncode == 0, result.stderr
    # `--invoke` prints the result on stdout (warnings go to stderr)
    assert result.stdout.strip().splitlines()[-1] == "5", result.stdout
