"""`revl run --backend wasm` applies the policy's wasm component limits.

Issue #3189. Before this slice the run path resolved no limits and handed the
emitted WAT to the harness, whose cordis-wasm runtime built a plain
``Engine()``/``Store``: a policy line ``wasm component limits fuel = …, memory =
…, wall = …`` was parsed and never read on the run path, and ``wasm components
confined`` with no limits (which ``limits_from_policy`` refuses) ran anyway.

What is proven here, on the REAL run path:

* a confined policy that names no limits is refused BEFORE the runtime gate, so
  the refusal is an admission decision (no runtime needed, and it fails on main
  even where cordis-wasm is absent: main would exit 3 or run);
* the memory, fuel and wall-clock ceilings each stop a composition that would
  otherwise finish cleanly. The "would finish" half is what fails on main: with
  the ceilings ignored, each program boots, tears down and exits 0;
* the byte-level import check reads the emitted module's import section and
  refuses an import outside the tier's namespaces. Its unit half runs without a
  runtime by stubbing the WAT-to-bytes conversion.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.policy import parse_policy  # noqa: E402
from revl.run_wasm import run_wasm, wasm_runtime_reason  # noqa: E402
from revl.wasm_confinement import ConfinementError  # noqa: E402

_WASM_REASON = wasm_runtime_reason()
needs_cordis_wasm = pytest.mark.skipif(
    _WASM_REASON is not None,
    reason=f"needs the cordis-wasm runtime: {_WASM_REASON}")

# One witnessed extern whose activation body runs inline WAT: the composition
# is otherwise the ordinary one-component boot. The same shape the issue's
# reproduction used (`@wasm` witnessed-extern body runs during activation).
_GROW = """
type E = { code: Str }

extern pure fn ungrow(r: Str) -> Unit = @wasm { }

extern witnessed[s] fn grow() -> Result[Str, E]
  undo ungrow(result)
  = @wasm {
    (i32.const 200)
    (memory.grow)
    (i32.const -1)
    (i32.eq)
    (if (then (unreachable)))
    (i32.const 0)
  }

component Grower {
  effect grow()
}
"""

# A finite CPU burn, so no test depends on an infinite loop: with no ceiling the
# composition completes (the "main" behaviour), and with one it traps. The
# iteration count is a parameter: `_BURN` (200M) is a fraction of a second on a
# fast host and must finish under a generous ceiling, while `_LONG_BURN` (2G,
# several seconds even on a fast host) must outlast a 1s wall ceiling.
_BURN_TEMPLATE = """
type E = { code: Str }

extern pure fn unburn(r: Str) -> Unit = @wasm { }

extern witnessed[s] fn burn() -> Result[Str, E]
  undo unburn(result)
  = @wasm {
    (local $n i32)
    (local.set $n (i32.const @COUNT@))
    (block $done
      (loop $l
        (local.set $n (i32.sub (local.get $n) (i32.const 1)))
        (br_if $l (local.get $n))))
    (i32.const 0)
  }

component Burner {
  effect burn()
}
"""
_BURN = _BURN_TEMPLATE.replace("@COUNT@", "200000000")
_LONG_BURN = _BURN_TEMPLATE.replace("@COUNT@", "2000000000")


def _run(source: str, policy_text: str | None) -> tuple[int, str]:
    """Drive the real wasm run path in-process, returning (rc, output)."""
    import contextlib
    import io

    ir = compile_source(source, "<test>.rvl")
    policy = parse_policy(policy_text) if policy_text is not None else None
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        rc = run_wasm(ir, {}, [], once=True, policy=policy)
    return rc, buffer.getvalue()


def test_a_confined_policy_with_no_limits_is_refused_before_the_gate():
    # The refusal is static: it must not need the runtime. On main the run
    # either boots (runtime present) or exits 3 with the runtime reason; this
    # exits 1 naming the missing ceilings either way.
    rc, out = _run(_BURN, "wasm components confined")
    assert rc == 1, out
    assert "names no limits" in out
    assert "fuel, memory and wall" in out


@needs_cordis_wasm
def test_the_memory_ceiling_stops_a_growth_that_would_otherwise_succeed():
    # 200 pages (~12.5 MiB) is a legal grow. Under a 1 MiB ceiling it is
    # refused by the store limiter, the program's own check traps, and the boot
    # is PARTIAL. With no ceiling (main) the same program reaches UP -> DOWN.
    rc, out = _run(_GROW,
                   "wasm component limits fuel = 1000000000000, "
                   "memory = 1MiB, wall = 120s")
    assert rc != 0, out
    assert "PARTIAL" in out, out
    assert "raised" in out, out


@needs_cordis_wasm
def test_the_fuel_ceiling_stops_a_burn_that_would_otherwise_succeed():
    # The loop needs far more than 10M instructions; on main it runs to
    # completion, so a clean boot here would mean the budget was not applied.
    rc, out = _run(_BURN,
                   "wasm component limits fuel = 10000000, "
                   "memory = 64MiB, wall = 120s")
    assert rc != 0, out
    assert "fuel consumed" in out, out


@needs_cordis_wasm
def test_the_wall_clock_ceiling_interrupts_a_burn_that_would_otherwise_succeed():
    # A long finite burn, with a huge fuel budget and a 1s wall: only the
    # epoch deadline can stop it. On main there is no deadline and it boots.
    rc, out = _run(_LONG_BURN,
                   "wasm component limits fuel = 1000000000000, "
                   "memory = 64MiB, wall = 1s")
    assert rc != 0, out
    assert "interrupt" in out, out
    assert "PARTIAL" in out, out


@needs_cordis_wasm
def test_a_loose_ceiling_does_not_refuse_a_fitting_composition():
    # The ceilings are bounds, not a refusal: the same burn completes under a
    # policy that grants it enough fuel and time.
    rc, out = _run(_BURN,
                   "wasm component limits fuel = 1000000000000, "
                   "memory = 64MiB, wall = 120s")
    assert rc == 0, out
    assert "UP" in out and "NO-RESIDUE" in out, out


# --------------------------------------------------------------------------- #
# the byte-level import check on the emitted modules
# --------------------------------------------------------------------------- #

def _uleb(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _encode_name(text: str) -> bytes:
    data = text.encode("utf-8")
    return _uleb(len(data)) + data


def _core_with_import(module: str, field: str) -> bytes:
    """A minimal core module whose import section names exactly one import."""
    payload = (_uleb(1) + _encode_name(module) + _encode_name(field)
               + b"\x00" + _uleb(0))          # func, type index 0
    return b"\0asm\x01\0\0\0" + b"\x02" + _uleb(len(payload)) + payload


def _check(monkeypatch, module: str, field: str) -> None:
    from revl.run_wasm import _check_emitted_imports  # noqa: PLC0415 - new API

    ir = compile_source("component Bad { }", "bad.rvl")
    monkeypatch.setattr("revl.run_wasm._emitted_bytes",
                        lambda modules: {"Bad": _core_with_import(module, field)})
    _check_emitted_imports(ir, {"Bad": "(module)"})


def test_an_import_outside_the_tier_namespaces_is_refused(monkeypatch):
    # The run path previously only scanned the WAT with a regex; the bytes the
    # harness compiles are now checked, and an unread host surface is refused.
    with pytest.raises(ConfinementError, match="expected namespaces"):
        _check(monkeypatch, "env", "secret")


def test_a_structural_seam_import_is_confined(monkeypatch):
    _check(monkeypatch, "host", "job_run")


# --------------------------------------------------------------------------- #
# the harness is its own session leader: an interrupt must still kill it
# --------------------------------------------------------------------------- #

class _InterruptedHarness:
    """A harness process whose first wait is interrupted (a Ctrl-C reaches the
    driver; the harness, a session leader, never sees the terminal's SIGINT)."""

    pid = 4242

    class _Stdin:
        def write(self, text):
            pass

        def flush(self):
            pass

        def close(self):
            pass

    def __init__(self):
        self.stdin = self._Stdin()
        self.stdout = iter(())
        self.waits = 0
        self.alive = True

    def wait(self, timeout=None):
        self.waits += 1
        if self.waits == 1:
            raise KeyboardInterrupt
        return -9

    def poll(self):
        return None if self.alive else -9


def test_an_interrupt_between_start_and_wait_kills_the_harness_group(monkeypatch):
    from revl import run_wasm as module  # noqa: PLC0415

    ir = compile_source((ROOT / "examples" / "counter_pair.rvl").read_text(
        encoding="utf-8"), "counter_pair.rvl")
    harness = _InterruptedHarness()
    killed = []

    def kill(proc):
        killed.append(proc)
        proc.alive = False

    monkeypatch.setattr(module, "wasm_runtime_reason", lambda: None)
    monkeypatch.setattr(module, "_check_emitted_imports",
                        lambda ir, modules: None)
    monkeypatch.setattr(module, "_cordis_wasm_python", lambda: "python")
    monkeypatch.setattr(module._childproc, "popen", lambda *a, **k: harness)
    monkeypatch.setattr(module, "_kill_process_group", kill)

    with pytest.raises(KeyboardInterrupt):
        module.run_wasm(ir, {}, [], once=True)

    # on main nothing killed it: the process would have run on, unowned
    assert killed == [harness]
    # and the driver reaped it after the kill
    assert harness.waits == 2
