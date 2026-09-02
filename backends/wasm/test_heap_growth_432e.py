"""The emitted heap grows instead of faulting — roadmap item 432(e).

Run with:
    .venv/bin/pytest backends/wasm/test_heap_growth_432e.py -q

Never in the same pytest process as `backends/python/tests/` (roadmap 419(a)).

Before the fix the emitter declared one 64 KiB page, `$alloc` only bumped
`$__hp`, and `grep -c memory.grow` over `emit.py` plus `canonical.py` was 0.
Two things followed, both re-run here as tests rather than argued:

  1. A single canonical call carrying a `Str` bigger than the page failed:
     the HOST's own placement of the argument was already out of bounds,
     before any revl code ran.
  2. Even with every argument well inside the page, a few dozen calls with a
     1 KiB string exhausted an instance, because a bump allocator that never
     grows has a fixed lifetime budget.

Item 432(a)/(c)/(f) landed first and moved both numbers without removing
either cliff, so the thresholds asserted below were re-bisected on that base
rather than taken from the audit text: 65520 bytes was the largest argument
one call could carry and 65521 failed (`realloc return: beyond end of
memory`), and 64 calls worked where 65 trapped at `memory fault at wasm
address 0x1020c`.

The probes those came from are `bench/codegen/wasm/probe_heap.py`; these
tests are the same two facts, plus the two refusal paths the growth fix
introduces (a host that caps the memory, and a size that wraps i32).
"""

import importlib.util
import os
import pathlib
import subprocess
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
GOLDEN = BACKEND / "golden"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(BACKEND))

from revl import compile_source  # noqa: E402

_REQUIRE = os.environ.get("REVL_REQUIRE_WASMTIME", "").strip().lower() not in (
    "", "0", "false", "no")

_SRC = (GOLDEN / "canonical_echoer.revl").read_text(encoding="utf-8")
_SERVICE = "Echoer"

#: Replays what one canonical `Str` call does to the heap as the emitter
#: writes it today, kept in step with `bench/codegen/wasm/probe_heap.py`:
#: `cabi_realloc` takes one buffer with item 432(a)'s header headroom and the
#: lift copies nothing into a second one, and item 432(f) made the return area
#: static, so exactly one allocation per call is left. Measured on the CORE
#: module because the component CLI re-instantiates per `--invoke`, which
#: would hide a per-instance leak entirely.
_DRIVER = """  (func (export "drive") (param $n i32) (param $len i32) (result i32)
    (local $i i32) (local $p i32)
    (block $done
      (loop $go
        (br_if $done (i32.ge_u (local.get $i) (local.get $n)))
        (local.set $p (i32.add
          (call $alloc (i32.add (local.get $len) (i32.const 8)))
          (i32.const 8)))
        (drop (call $__canon_lift_str (local.get $p) (local.get $len)))
        (local.set $i (i32.add (local.get $i) (i32.const 1)))
        (br $go)))
    (global.get $__hp))
  (func (export "alloc1") (param $n i32) (result i32)
    (call $alloc (local.get $n)))
"""


def _canonical():
    spec = importlib.util.spec_from_file_location(
        "revl_wasm_canonical", BACKEND / "canonical.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _emit():
    return _canonical().emit_component(compile_source(_SRC), service=_SERVICE)


def _toolchain_or_skip(canonical):
    if canonical.wasm_tools_binary() is None or canonical.wasmtime_binary() is None:
        if _REQUIRE:
            pytest.fail(
                "wasm-tools and/or wasmtime absent, so the heap cannot be "
                "grown under a real runtime. REVL_REQUIRE_WASMTIME is set "
                "(CI), so this fails instead of skipping.", pytrace=False)
        pytest.skip("wasm-tools/wasmtime not installed "
                    "(set REVL_REQUIRE_WASMTIME=1 to make this a failure)")


def _core_module(canonical, core_wat: str, out: pathlib.Path,
                 memory: str | None = None) -> pathlib.Path:
    """Compile the emitted core WAT, with the heap driver spliced in, to a
    core wasm file. `memory` replaces the emitted memory declaration, which is
    how the capped-host case is set up without touching the emitter."""
    wat = core_wat.replace("\n  (func $echo ", "\n" + _DRIVER + "  (func $echo ", 1)
    assert "(export \"drive\")" in wat, "driver splice point moved"
    if memory is not None:
        wat = wat.replace('  (memory (export "memory") 1)\n', memory + "\n", 1)
        assert memory in wat, "memory declaration splice point moved"
    out.mkdir(parents=True, exist_ok=True)
    wat_path, wasm_path = out / "probe.wat", out / "probe.wasm"
    wat_path.write_text(wat, encoding="utf-8")
    subprocess.run([canonical.wasm_tools_binary(), "parse", str(wat_path),
                    "-o", str(wasm_path)],
                   check=True, capture_output=True, timeout=120)
    return wasm_path


def _run_core(canonical, module: pathlib.Path, func: str,
              *args: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [canonical.wasmtime_binary(), "run", "--invoke", func, str(module),
         *[str(a) for a in args]],
        capture_output=True, text=True, timeout=300)


# --------------------------------------------------------------------------- #
# Emit level — runs everywhere, no toolchain needed.
# --------------------------------------------------------------------------- #

def test_the_emitted_allocator_can_grow_the_memory():
    """The finding's own measure: `memory.grow` was absent from every emitted
    module. It is the whole difference between a fault and a refusal."""
    core = _emit()["core_wat"]
    assert "memory.grow" in core
    assert "(func $__heap_grow (param $end i32)" in core
    # every path into the heap goes through $alloc, so the guard belongs there
    assert "(call $__heap_grow (local.get $end))" in core
    # ... and cabi_realloc, the host's own allocator, is backed by $alloc
    # (item 432(a) added the header headroom to the request; the point here is
    # only that the host's placement goes through the growing allocator).
    assert '(func (export "cabi_realloc")' in core
    assert "(call $alloc (i32.add (local.get $new_size)" in core


def test_growth_failure_has_a_named_refusal_site():
    """`memory.grow` answers -1 rather than trapping, so the emitter has to
    decide. It refuses at a function whose NAME is the diagnosis, so the
    operator reads `__heap_exhausted` off the backtrace."""
    core = _emit()["core_wat"]
    assert "(func $__heap_exhausted\n    (unreachable))" in core
    assert core.count("(call $__heap_exhausted)") == 3   # 2 wrap guards + grow


def test_the_memory_declaration_leaves_room_to_grow():
    """`(memory ... 1)` with no maximum keeps the wasm32 default ceiling, so
    the host, not the emitter, decides how large this instance may get."""
    core = _emit()["core_wat"]
    assert '(memory (export "memory") 1)' in core


# --------------------------------------------------------------------------- #
# Execute under a real runtime — the two proofs from the audit, plus the two
# refusal paths.
# --------------------------------------------------------------------------- #

def test_a_string_argument_larger_than_one_page_round_trips(tmp_path):
    """Proof 1, re-bisected on the (a)/(c)/(f) base: 65520 was the largest
    argument that worked and 65521 was refused by wasmtime's own canonical
    check. 200000 and 1000000 are far past any single-page story, so they can
    only pass by the memory actually growing."""
    canonical = _canonical()
    _toolchain_or_skip(canonical)
    res = _emit()
    component = canonical.build_component(
        res["core_wat"], res["wit"], tmp_path, res["world"], name="echoer")
    for size in (16384, 40000, 65520, 65521, 65536, 200000, 1000000):
        arg = "a" * size
        assert canonical.run_component_str(component, "echo", arg) == arg


def test_sustained_calls_do_not_exhaust_an_instance(tmp_path):
    """Proof 2. 64 calls with a 1 KiB string worked and 65 trapped. Step over
    that boundary and then run two orders of magnitude past it on ONE
    instance, and require the heap to have ended far past the old fixed
    ceiling so the test cannot pass by the allocations having quietly
    shrunk instead."""
    canonical = _canonical()
    _toolchain_or_skip(canonical)
    module = _core_module(canonical, _emit()["core_wat"], tmp_path / "core")
    for calls in (32, 64, 65, 128, 4096):
        res = _run_core(canonical, module, "drive", calls, 1024)
        assert res.returncode == 0, (res.stderr + res.stdout)
    assert int(res.stdout.strip()) > 4_000_000


def test_a_host_that_caps_the_memory_gets_a_named_refusal(tmp_path):
    """Growth failure is real: a host may cap this instance. `memory.grow`
    then returns -1, and the emitted code must refuse where the operator can
    see it rather than write past the end. Capped here by pinning the maximum
    at the one page the emitter starts with, so `memory.grow` cannot succeed."""
    canonical = _canonical()
    _toolchain_or_skip(canonical)
    module = _core_module(canonical, _emit()["core_wat"], tmp_path / "capped",
                          memory='  (memory (export "memory") 1 1)')
    res = _run_core(canonical, module, "drive", 128, 1024)
    assert res.returncode != 0
    err = res.stderr + res.stdout
    # the refusal, not a memory fault at an address
    assert "__heap_exhausted" in err, err
    assert "memory fault" not in err, err


def test_a_size_that_wraps_i32_is_refused_rather_than_wrapped(tmp_path):
    """`cabi_realloc` takes its `new_size` from the HOST. A size whose
    round-to-8 wraps past 2^32 would otherwise hand back a pointer with zero
    bytes behind it and no growth at all — the silent version of the same
    defect."""
    canonical = _canonical()
    _toolchain_or_skip(canonical)
    module = _core_module(canonical, _emit()["core_wat"], tmp_path / "core")
    res = _run_core(canonical, module, "alloc1", -1)
    assert res.returncode != 0
    err = res.stderr + res.stdout
    assert "__heap_exhausted" in err, err
    # and an ordinary size on the same module still allocates
    ok = _run_core(canonical, module, "alloc1", 4096)
    assert ok.returncode == 0, (ok.stderr + ok.stdout)
