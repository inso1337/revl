"""The emitted heap RECLAIMS what a call allocated — roadmap item 432(e).

Run with:
    .venv/bin/pytest backends/wasm/test_heap_reclaim_432e.py -q

Never in the same pytest process as `backends/python/tests/` (roadmap 419(a)).

Item 432(e) fixed growth and left reclaim open: `$alloc` only ever bumped
`$__hp`, so a long-lived component instance spent memory in proportion to how
many times it had been CALLED rather than to its working set. Measured through
the real canonical export before this change, `echo` with a 1 KiB argument left
the heap at 67 633 160 B after 65 536 calls, one page per 64 calls, for a
workload whose live set is one string.

The fix is a per-call arena: a canonical export rewinds `$__hp` to the arena
floor on the way out, so everything the call allocated — including the buffer
the HOST placed the arguments in through `cabi_realloc`, which is the larger
half — is released when the call returns. After it the same 65 536 calls leave
the heap at the floor and the memory at its initial page.

The rewind is only sound for a module that parks nothing in the heap across
calls, so `canonical._arena_safe` proves that from the emitted module rather
than assuming it, and the tests below exercise both verdicts.
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
_ECHO_EXPORT = "revl:exported/echoer#echo"

#: Drives the REAL canonical export the way a component host does — one
#: `cabi_realloc` for the argument, then the export — and reports what the heap
#: looks like afterwards. `probe_heap.py`'s older driver replays the per-call
#: ALLOCATION instead, which cannot see a rewind that lives in the wrapper.
_DRIVER = """  (func $drive_calls (export "drive_calls") (param $n i32) (param $len i32) (result i32)
    (local $i i32) (local $p i32)
    (block $done
      (loop $go
        (br_if $done (i32.ge_u (local.get $i) (local.get $n)))
        (local.set $p (call $__cabi_realloc
          (i32.const 0) (i32.const 0) (i32.const 1) (local.get $len)))
        (drop (call $__export_echo (local.get $p) (local.get $len)))
        (local.set $i (i32.add (local.get $i) (i32.const 1)))
        (br $go)))
    (global.get $__hp))
  (func (export "drive_pages") (param $n i32) (param $len i32) (result i32)
    (drop (call $drive_calls (local.get $n) (local.get $len)))
    (memory.size))
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
                "wasm-tools and/or wasmtime absent, so the arena cannot be "
                "exercised under a real runtime. REVL_REQUIRE_WASMTIME is set "
                "(CI), so this fails instead of skipping.", pytrace=False)
        pytest.skip("wasm-tools/wasmtime not installed "
                    "(set REVL_REQUIRE_WASMTIME=1 to make this a failure)")


def _core_module(canonical, core_wat: str, out: pathlib.Path) -> pathlib.Path:
    """The emitted core WAT with the export driver spliced in, as core wasm.

    Measured on the CORE module because the component CLI re-instantiates per
    `--invoke`, which would hide a per-instance leak entirely.
    """
    wat = core_wat.replace('(func (export "cabi_realloc")',
                           '(func $__cabi_realloc (export "cabi_realloc")', 1)
    wat = wat.replace(f'(func (export "{_ECHO_EXPORT}")',
                      f'(func $__export_echo (export "{_ECHO_EXPORT}")', 1)
    assert "$__cabi_realloc" in wat and "$__export_echo" in wat, \
        "driver splice points moved"
    body = wat.rstrip()
    assert body.endswith(")"), "unexpected core module shape"
    wat = body[:-1].rstrip("\n") + "\n" + _DRIVER + ")\n"
    out.mkdir(parents=True, exist_ok=True)
    wat_path, wasm_path = out / "reclaim.wat", out / "reclaim.wasm"
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
        capture_output=True, text=True, timeout=600)


# --------------------------------------------------------------------------- #
# The soundness proof — runs everywhere, no toolchain needed.
# --------------------------------------------------------------------------- #

_BARE = """(module
  (memory (export "memory") 1)
  (global $__hp (mut i32) (i32.const 8))
  (func $alloc (param $n i32) (result i32)
    (global.set $__hp (i32.add (global.get $__hp) (local.get $n)))
    (global.get $__hp))
)
"""


def test_arena_is_allowed_when_the_bump_pointer_is_the_only_mutable_global():
    """Nowhere to park an address across two calls, so the rewind is sound."""
    ok, why = _canonical()._arena_safe(_BARE)
    assert ok, why


def test_arena_is_allowed_for_a_const_written_cursor():
    """`$__step`/`$__dstep`/`$__committed` are activation bookkeeping: every
    write stores an `i32.const`, so none of them can be holding a pointer."""
    core = _BARE.replace(
        '  (global $__hp',
        '  (global $__step (mut i32) (i32.const 0))\n'
        '  (func $bump (global.set $__step (i32.const 1)))\n'
        '  (global $__hp', 1)
    ok, why = _canonical()._arena_safe(core)
    assert ok, why


def test_arena_is_refused_when_a_global_is_assigned_a_computed_value():
    """The witnessed accumulator head is the real instance of this: a cell
    allocated inside a call and linked into a list that outlives it. Rewinding
    would hand that cell's bytes to the next call."""
    core = _BARE.replace(
        '  (global $__hp',
        '  (global $__mw_head (mut i32) (i32.const 0))\n'
        '  (func $reg (local $cell i32)\n'
        '    (local.set $cell (call $alloc (i32.const 16)))\n'
        '    (global.set $__mw_head (local.get $cell)))\n'
        '  (global $__hp', 1)
    ok, why = _canonical()._arena_safe(core)
    assert not ok
    assert "$__mw_head" in why


def test_arena_is_refused_when_the_module_has_a_start_function():
    """A start function allocates before any export runs, and the floor the
    rewind targets is the initial `$__hp`, i.e. below whatever it allocated."""
    ok, why = _canonical()._arena_safe(_BARE.replace("(func $alloc", "(start 0)\n  (func $alloc", 1))
    assert not ok
    assert "start" in why


def test_a_refused_module_gets_no_arena_base_and_no_rewind():
    """The two halves stay in step: no `$__canon_arena_base` is declared and no
    wrapper references one."""
    canonical = _canonical()
    canon = canonical._Canon({})
    export = canon.canon_export(
        {"name": "echo", "params": [{"name": "s", "type": "Str"}],
         "returns": "Str"},
        "revl:exported", "echoer", arena=False)
    module = canonical._canonical_module(_BARE, canon, [export], False)
    assert "$__canon_arena_base" not in module


# --------------------------------------------------------------------------- #
# Emit level — the shape the fix has to have.
# --------------------------------------------------------------------------- #

def test_every_canonical_export_rewinds_the_bump_pointer():
    core = _emit()["core_wat"]
    assert "(global $__canon_arena_base i32" in core
    rewind = "(global.set $__hp (global.get $__canon_arena_base))"
    wrappers = core.count('(func (export "revl:exported/echoer#')
    assert wrappers >= 1
    assert core.count(rewind) == wrappers


def test_the_arena_floor_is_the_initial_bump_pointer():
    """The floor has to be exactly `$__hp`'s initial value: the return area and
    the literal data segments sit BELOW it and must survive the rewind, and
    everything at or above it was handed out by `$alloc` during a call."""
    import re
    core = _emit()["core_wat"]
    hp = re.search(r"\(global \$__hp \(mut i32\) \(i32\.const (\d+)\)\)", core)
    base = re.search(r"\(global \$__canon_arena_base i32 \(i32\.const (\d+)\)\)", core)
    assert hp is not None and base is not None
    assert base.group(1) == hp.group(1)
    ret_area = re.search(r"\(global \$__canon_ret_area i32 \(i32\.const (\d+)\)\)", core)
    if ret_area is not None:
        assert int(ret_area.group(1)) < int(base.group(1))


def test_the_rewind_runs_after_the_result_has_been_lowered():
    """Ordering matters: the result is written into the return area first, and
    the rewind is the last statement before the wrapper's tail. It is a POINTER
    move and never a write, so the bytes the host has yet to lift are still
    there; the first byte that can overwrite them belongs to the NEXT call,
    which a single-threaded instance cannot start before the host is done."""
    core = _emit()["core_wat"]
    start = core.index('(func (export "revl:exported/echoer#echo")')
    end = core.index("\n  ;; canonical export of", start + 1)
    wrapper = core[start:end].rstrip()
    lower = wrapper.index("(call $__canon_lower_str")
    rewind = wrapper.index("(global.set $__hp (global.get $__canon_arena_base))")
    assert lower < rewind, wrapper
    assert wrapper.endswith("(local.get $area))"), wrapper


# --------------------------------------------------------------------------- #
# Executed — the flat high-water mark the finding asked for.
# --------------------------------------------------------------------------- #

def test_the_heap_high_water_mark_is_flat_across_calls(tmp_path):
    """Before: 1 040 B after 1 call, 66 056 after 64, 67 633 160 after 65 536.
    After: the floor, at every call count."""
    canonical = _canonical()
    _toolchain_or_skip(canonical)
    module = _core_module(canonical, _emit()["core_wat"], tmp_path / "core")
    import re
    base = int(re.search(r"\(global \$__canon_arena_base i32 \(i32\.const (\d+)\)\)",
                         _emit()["core_wat"]).group(1))
    seen = []
    for calls in (1, 64, 1024, 65536):
        res = _run_core(canonical, module, "drive_calls", calls, 1024)
        assert res.returncode == 0, res.stderr
        seen.append((calls, int(res.stdout.strip())))
    assert all(hp == base for _n, hp in seen), seen


def test_the_memory_never_grows_past_its_first_page(tmp_path):
    """The high-water mark the ALLOCATOR reached, not just where it ended:
    `memory.size` only ever goes up, so one page after 65 536 1 KiB calls is
    proof that no call's allocation outlived it. Before the fix this was 1 033
    pages."""
    canonical = _canonical()
    _toolchain_or_skip(canonical)
    module = _core_module(canonical, _emit()["core_wat"], tmp_path / "core")
    res = _run_core(canonical, module, "drive_pages", 65536, 1024)
    assert res.returncode == 0, res.stderr
    assert int(res.stdout.strip()) == 1


def test_a_call_bigger_than_the_arena_still_grows_and_still_answers(tmp_path):
    """Reclaim must not undo item 432(e)'s growth half: one call may still need
    more than the initial page, and the rewind only runs on the way out."""
    canonical = _canonical()
    _toolchain_or_skip(canonical)
    emitted = _emit()
    comp = canonical.build_component(
        emitted["core_wat"], emitted["wit"], tmp_path / "comp",
        emitted["world"], name="echo")
    for n in (65521, 200000):
        out = canonical.run_component_str(comp, "echo", "a" * n)
        assert out == "a" * n


def test_repeated_calls_through_the_component_still_round_trip(tmp_path):
    """The rewind reuses the same addresses call after call, so a stale read
    would show up as a wrong answer rather than a trap. Drive the boundary with
    different lengths and check every one."""
    canonical = _canonical()
    _toolchain_or_skip(canonical)
    emitted = _emit()
    comp = canonical.build_component(
        emitted["core_wat"], emitted["wit"], tmp_path / "comp",
        emitted["world"], name="echo")
    for n in (0, 1, 5, 1024, 4096):
        assert canonical.run_component_str(comp, "echo", "b" * n) == "b" * n
