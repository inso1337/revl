"""Time as a coeffect — `every`/`after` timers (roadmap item 57).

A timer is a **textbook revertible effect**: `every 30s { … }` / `after 5m { … }`
acquire a *schedule whose inverse is cancellation*, derived teardown like any
other effect, so unloading the component provably cancels its timers (no
orphaned interval — the residue probe, item 18, would otherwise catch the
leak). The body runs at activation-time stratum with the component's declared
capabilities, so the audit sees a firing's reach like any other reach (G4/G8).
And under `revl test`/replay the **clock is a coeffect the harness provides**,
so a firing is a deterministic timeline step, not a wall-clock race.

These tests pin all four claims on the reference tiers (py + ts):

  * syntax parses, lowers to an additive `timer` step (`ir_version` stays 3),
    and typechecks;
  * a timer body reaching an undeclared emission is refused (G4), and the reach
    it *does* have surfaces to `revl audit` (G8) as component reach;
  * the schedule is revertible — cancellation is the derived inverse, and a
    torn-down timer leaves no residue and never fires again;
  * the clock is a coeffect — injected advances make firings deterministic
    timeline steps ("fires on the 3rd tick");

and the documented follow-on: wasm still refuses timers honestly (go and rust
lower them as of item 99).
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.lower import _collect_emit_caps  # noqa: E402
from revl.parser import Parser, TimerStmt, EmitStmt, AdvanceStmt  # noqa: E402


def _emitter(backend: str):
    spec = importlib.util.spec_from_file_location(
        f"{backend}_emit", ROOT / "backends" / backend / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HEARTBEAT = """
service Log { emission fn write(msg: Str) }

component Heartbeat requires log: Log {
  every 30s { emit log.write("tick") }
  after 5m { emit log.write("warmup done") }
}
"""


# ---------------------------------------------------------------- syntax + IR

def test_every_and_after_parse():
    prog = Parser(HEARTBEAT, "<t>").parse()
    body = prog.components[0].body
    timers = [s for s in body if isinstance(s, TimerStmt)]
    assert [t.mode for t in timers] == ["every", "after"]
    assert [t.interval_ms for t in timers] == [30_000, 300_000]
    # a timer body is a list of `emit` statements (the audited reach)
    assert all(isinstance(e, EmitStmt) for t in timers for e in t.body)


@pytest.mark.parametrize("src,ms", [
    ("every 250ms { emit log.write(\"x\") }", 250),
    ("every 5s { emit log.write(\"x\") }", 5_000),
    ("after 2m { emit log.write(\"x\") }", 120_000),
    ("after 1h { emit log.write(\"x\") }", 3_600_000),
    ("every 1d { emit log.write(\"x\") }", 86_400_000),
])
def test_duration_units_resolve_to_ms(src, ms):
    prog = Parser(
        f"service Log {{ emission fn write(msg: Str) }}\n"
        f"component C requires log: Log {{ {src} }}", "<t>").parse()
    assert prog.components[0].body[0].interval_ms == ms


def test_timer_lowers_to_additive_step_at_v3():
    ir = compile_source(HEARTBEAT, "<t>")
    assert ir["ir_version"] == 3  # additive — no bump beyond 3
    steps = ir["components"][0]["body"]
    every = next(s for s in steps if s.get("step") == "timer" and s["mode"] == "every")
    assert every["interval_ms"] == 30_000
    assert every["body"] == [
        {"step": "emit", "expr": {"kind": "call",
                                  "target": {"kind": "req", "name": "log"},
                                  "method": "write",
                                  "args": [{"kind": "lit", "value": "tick"}]}}]


def test_timer_has_no_undo_slot():
    """The schedule is not an emission (crossing time is not crossing the
    system boundary); its inverse is the runtime's cancellation, derived — so
    the IR step carries no `undo` (unlike an `effect`/`let-effect` step)."""
    ir = compile_source(HEARTBEAT, "<t>")
    for step in ir["components"][0]["body"]:
        if step.get("step") == "timer":
            assert "undo" not in step


def test_bad_duration_is_refused():
    with pytest.raises(RevlError, match="duration unit"):
        compile_source("service L { emission fn w(m: Str) }\n"
                        "component C requires l: L { every 30 { emit l.w(\"x\") } }", "<t>")
    with pytest.raises(RevlError, match="whole-number delay"):
        compile_source("service L { emission fn w(m: Str) }\n"
                        "component C requires l: L { every s { emit l.w(\"x\") } }", "<t>")


def test_empty_timer_body_is_refused():
    with pytest.raises(RevlError, match="records emissions|empty"):
        compile_source("service L { emission fn w(m: Str) }\n"
                        "component C requires l: L { every 5s { } }", "<t>")


def test_timer_only_in_activation_body_not_a_method():
    with pytest.raises(RevlError, match="only allowed in a component activation body"):
        compile_source(
            "service L { emission fn w(m: Str) }\n"
            "component C requires l: L provides p: L {\n"
            "  provide p { fn w(m) { every 5s { emit l.w(\"x\") } } }\n"
            "}", "<t>")


def test_timer_after_provide_is_refused():
    """A timer is an acquisition (its schedule is armed at activation and
    reverted on teardown), so it cannot follow a `provide` — it would be
    cancelled while dependents can still fire it (linker rule A2)."""
    with pytest.raises(RevlError, match="acquisition after `provide`"):
        compile_source(
            "service L { emission fn w(m: Str) }\n"
            "component C requires l: L provides p: L {\n"
            "  provide p { fn w(m) = emit l.w(m) }\n"
            "  every 5s { emit l.w(\"x\") }\n"
            "}", "<t>")


# ---------------------------------------------------------- capability audit

def test_timer_body_reaching_undeclared_emission_is_refused():
    """G4: the firing runs with the component's declared capabilities — a body
    reaching a service the component does not require is refused exactly like a
    top-level `emit` would be."""
    with pytest.raises(RevlError, match="`bus` is not a declared requirement"):
        compile_source(
            "service Log { emission fn write(m: Str) }\n"
            "service Bus { emission fn send(x: Str) }\n"
            "component C requires log: Log { every 10s { emit bus.send(\"x\") } }", "<t>")


def test_timer_reach_surfaces_to_the_capability_analysis():
    """G8: the boundaries a firing crosses are component reach — the same
    `_collect_emit_caps` the G4 machinery and `revl audit` read must see them."""
    ir = compile_source(
        "service Log { emission fn write(m: Str) }\n"
        "component C requires log: Log { every 10s { emit log.write(\"x\") } }", "<t>")
    caps: set = set()
    _collect_emit_caps(ir["components"][0]["body"], caps)
    assert caps == {"log"}


def test_timer_reach_shows_in_audit(capsys):
    from revl.__main__ import main  # noqa: PLC0415
    path = ROOT / "examples" / "heartbeat.rvl"
    assert path.exists(), "the item-57 example must ship"
    main(["audit", str(path)])
    out = capsys.readouterr().out
    # the timer's emission is a boundary crossing on the audit surface
    assert "log.write" in out


# ------------------------------------------ runtime: revertible + clock coeffect
#
# These drive the real cordis-py *runtime scheduler* (backends/python/runtime.py)
# directly — no cordis-py install needed — so the revertibility and determinism
# proofs run in the default `pytest tests/`.

@pytest.fixture()
def rt():
    spec = importlib.util.spec_from_file_location(
        "revl_runtime_57", ROOT / "backends" / "python" / "runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.Clock.reset()
    return module


def test_clock_is_a_coeffect_firings_are_deterministic(rt):
    """Time does not pass on its own: a firing happens only when the harness
    advances the clock, and it lands on an exact tick — 'fires on the 3rd
    tick', not a wall-clock race."""
    seen = []
    rt.schedule_every(10, lambda: seen.append(rt.Clock.now()))
    assert rt.Clock.now() == 0 and seen == []          # nothing fires unbidden
    fired = rt.Clock.advance(35)                        # inject 35ms of time
    assert fired == 3 and seen == [10, 20, 30]          # deterministic steps
    assert rt.Clock.firings()[2] == (1, 30)             # the 3rd firing, at 30ms


def test_unload_cancels_an_every_timer_no_residue_no_orphan_firing(rt):
    """The pinned revertibility test: arm an `every` timer, tear the activation
    down (run the yielded inverse), and assert no residue and no orphaned
    firing — the schedule leaves nothing behind."""
    seen = []
    handle = rt.schedule_every(10, lambda: seen.append(rt.Clock.now()))
    rt.Clock.advance(25)                                # fires at 10, 20
    assert seen == [10, 20]
    assert rt.Clock.pending() == 1                      # live schedule = residue
    # teardown runs the derived inverse (the emitted `yield lambda: h.cancel()`)
    assert handle.cancel() is True
    assert rt.Clock.pending() == 0                      # no residue
    rt.Clock.advance(1000)                              # lots more time passes
    assert seen == [10, 20]                             # but no orphaned firing


def test_residue_trace_pairs_schedule_with_cancel(rt):
    """The no-residue proof reuses the same acquire/release trace Pool.open/close
    uses: `schedule` is balanced by `cancel`, so an uncancelled timer is caught
    by the existing residue machinery (`_LIFECYCLE_ACQUIRE`)."""
    log = []
    unsub = rt.add_trace(log.append)
    try:
        h = rt.schedule_every(10, lambda: None)
        assert any(e.endswith(".schedule every 10ms") for e in log)
        h.cancel()
        assert any(e == f"{h._tag}.cancel" for e in log)
    finally:
        unsub()


def test_after_is_one_shot_spent_after_firing(rt):
    """`after` fires once and is spent — no residue, and the teardown's own
    `cancel()` is a clean no-op."""
    seen = []
    handle = rt.schedule_after(50, lambda: seen.append("boom"))
    assert rt.Clock.advance(49) == 0 and seen == []     # not yet
    assert rt.Clock.advance(11) == 1 and seen == ["boom"]  # fires at 50
    assert rt.Clock.pending() == 0                       # spent — no residue
    assert handle.cancel() is False                      # teardown no-op
    assert rt.Clock.advance(1000) == 0                   # never fires again


def test_multiple_timers_fire_in_deterministic_time_order(rt):
    """Firings interleave across timers in true time order, ties broken by arm
    order — a total, reproducible timeline the fault sweep can index into."""
    order = []
    rt.schedule_every(10, lambda: order.append("a"))    # a: 10,20,30
    rt.schedule_every(15, lambda: order.append("b"))    # b: 15,30
    rt.Clock.advance(30)
    assert order == ["a", "b", "a", "a", "b"]           # 10a 15b 20a 30a 30b


# --------------------------------------------------------- emitted-code shape

def test_python_emit_schedules_and_yields_cancel():
    ir = compile_source(HEARTBEAT, "<t>")
    src = _emitter("python").emit(ir)
    assert "from runtime import" in src
    assert "schedule_every(30000" in src
    assert "schedule_after(300000" in src
    # the derived inverse joins the same LIFO disposer stack as every effect
    assert ".cancel()" in src and "yield lambda:" in src


def test_typescript_emit_schedules_and_yields_cancel():
    ir = compile_source(HEARTBEAT, "<t>")
    src = _emitter("typescript").emit(ir)
    assert "host.scheduleEvery(30000" in src
    assert "host.scheduleAfter(300000" in src
    assert ".cancel()" in src and "yield () =>" in src


def test_emitted_python_body_reverts_cleanly():
    """End-to-end on the emitted module: install its activation-body generator
    against the real runtime scheduler + a stub Frame, fire the timer, then run
    the yielded inverses — assert the schedule is gone and never fires again."""
    ir = compile_source(
        "service Log { emission fn write(m: Str) }\n"
        "component Beat requires log: Log { every 10s { emit log.write(\"tick\") } }",
        "<t>")
    src = _emitter("python").emit(ir)

    real_rt = importlib.util.module_from_spec(
        importlib.util.spec_from_file_location(
            "revl_runtime_57e", ROOT / "backends" / "python" / "runtime.py"))
    real_rt.__spec__.loader.exec_module(real_rt)
    real_rt.Clock.reset()

    captured: dict = {}

    class _StubFrame:
        def __init__(self, ctx, name):
            self.ctx = ctx
        def install(self, body):
            captured["body"] = body
        def begin(self):
            return None
        def drain(self):
            return None

    # the emitted module does `from runtime import Frame, schedule_*`; shadow it
    fake = types.ModuleType("runtime")
    fake.Frame = _StubFrame
    fake.schedule_every = real_rt.schedule_every
    fake.schedule_after = real_rt.schedule_after
    saved = sys.modules.get("runtime")
    sys.modules["runtime"] = fake
    try:
        ns: dict = {}
        exec(compile(src, "<emitted>", "exec"), ns)
        writes = []
        ctx = types.SimpleNamespace(log=types.SimpleNamespace(write=writes.append))
        ns["Beat"]["apply"](ctx, {})
        # drive the activation-body generator: collect its yielded inverses
        disposers = []
        for value in captured["body"]():
            if callable(value) and getattr(value, "__name__", "") == "<lambda>":
                disposers.append(value)
        real_rt.Clock.advance(25_000)                # timer fires at 10s, 20s
        assert writes == ["tick", "tick"]
        assert real_rt.Clock.pending() == 1          # schedule is live residue
        for dispose in reversed(disposers):          # teardown, LIFO
            dispose()
        assert real_rt.Clock.pending() == 0          # revertible: no residue
        real_rt.Clock.advance(1_000_000)
        assert writes == ["tick", "tick"]            # no orphaned firing
    finally:
        if saved is not None:
            sys.modules["runtime"] = saved
        else:
            del sys.modules["runtime"]


# --------------------------------------------- documented follow-on: other tiers

@pytest.mark.parametrize("tier", ["wasm"])
def test_other_tiers_refuse_timers_honestly(tier):
    """wasm remains a documented follow-on: its emitter refuses a `timer` step
    rather than silently mis-lowering it (docs/time-coeffect.md). go and rust
    now lower timers (`test_go_rust_lower_timers`)."""
    ir = compile_source(
        "service Log { emission fn write(m: Str) }\n"
        "component C requires log: Log { every 10s { emit log.write(\"x\") } }", "<t>")
    emit = _emitter(tier)
    with pytest.raises(Exception) as exc:
        emit.emit(ir)
    assert "timer" in str(exc.value)


@pytest.mark.parametrize("tier", ["go", "rust"])
def test_go_rust_lower_timers(tier):
    """go and rust are no longer a follow-on: their emitters lower `every`/
    `after` to a schedule/cancel effect on the clock coeffect (item 57). The
    executable proofs — deterministic firing + unload-cancels-no-residue on the
    real stc-go / cordis-rs runtimes — live in backends/{go,rust}. Here we pin
    that the emitter accepts the step and wires the schedule into the effect
    ledger with cancellation as its inverse."""
    ir = compile_source(
        "service Log { emission fn write(m: Str) }\n"
        "component C requires log: Log { every 10s { emit log.write(\"x\") } }", "<t>")
    src = _emitter(tier).emit(ir)
    if tier == "go":
        assert "revlScheduleEvery(10000, func() {" in src
        assert ".Cancel(); return nil }" in src
    else:
        assert "revl_schedule_every(10000, move || {" in src
        assert "revl_cancel(" in src


def test_test_harness_reports_a_clean_follow_on_skip(monkeypatch):
    """`revl test` surfaces a tier's timer follow-on as a clean skip-with-reason,
    never a false pass or an opaque dump.

    Only wasm remains the timer follow-on: item 99 taught go and rust to lower
    timers, so the harness now routes them to real execution like py/ts rather
    than reporting a stale "not yet lowerable" skip. We force the go/rust
    toolchains absent so the suite does not shell out to `go test` / `cargo
    test`; the point is that the resulting skip is *toolchain-absent*, never the
    timer follow-on."""
    from revl import test as test_module  # noqa: PLC0415
    from revl.test import run_go, run_rust, run_wasm  # noqa: PLC0415
    ir = compile_source(
        "service Log { emission fn write(m: Str) }\n"
        "component C requires log: Log { every 10s { emit log.write(\"x\") } }", "<t>")

    # wasm is still the honest timer follow-on: its emitter does not lower the
    # step yet (docs/time-coeffect.md).
    verdict, reason = run_wasm(ir)
    assert verdict == "skip"
    assert "not yet lowerable" in reason and "wasm" in reason

    # go and rust lower timers now (item 99): they route to real execution and
    # must never report the timer follow-on. With the toolchains absent the
    # skip is a plain "not installed", not "not yet lowerable".
    monkeypatch.setattr(test_module.shutil, "which", lambda name: None)
    for runner, tier in [(run_go, "go"), (run_rust, "rust")]:
        verdict, reason = runner(ir)
        assert verdict == "skip"
        assert "not yet lowerable" not in reason
        assert "not installed" in reason


# =====================================================================
# item 102: `advance` — driving the clock coeffect from inside a lifecycle test
# =====================================================================
#
# Item 57 made the clock a coeffect and proved the *revertible-schedule* half
# inside a `lifecycle test`. But a firing was not expressible in the language:
# nothing in a lifecycle test could move the clock. Item 102 adds the `advance
# <n><unit>` lifecycle statement, so a timer's firing becomes an assertable
# timeline step ("fires on the 3rd tick") on the reference tiers (py + ts).

EXAMPLES = ROOT / "examples"
CORDIS_PY = ROOT / "backends" / "python" / ".venv" / "bin" / "python"
VITEST = ROOT / "backends" / "typescript" / "node_modules" / ".bin" / "vitest"

_ADVANCE_DOC = """
service Counter { fn count() -> Int  emission fn tick() }
component TickCounter provides counter: Counter {
  let store = effect Map.new() undo store.drop()
  provide counter {
    fn count() = store.size()
    fn tick() {
      let key = `k${store.size()}`
      effect store.insert(key, "x")
      undo   store.remove(key)
    }
  }
}
component Beat requires counter: Counter { every 10s { emit counter.tick() } }
lifecycle test "beat fires on each tick" {
  load TickCounter
  load Beat
  advance 35s
  let n = call counter.count()
  assert n == 3
  unload Beat
  unload TickCounter
  assert no_residue
}
"""


def _advance_stmt(src: str) -> AdvanceStmt:
    prog = Parser(f'lifecycle test "t" {{ {src} }}', "<t>").parse()
    return prog.tests[0].body[0]


# ---------------------------------------------------------------- parse + lower

@pytest.mark.parametrize("src,ms", [
    ("advance 250ms", 250),
    ("advance 30s", 30_000),
    ("advance 5m", 300_000),
    ("advance 2h", 7_200_000),
    ("advance 1d", 86_400_000),
])
def test_advance_parses_with_item57_duration_units(src, ms):
    stmt = _advance_stmt(src)
    assert isinstance(stmt, AdvanceStmt)
    assert stmt.ms == ms


def test_advance_lowers_to_additive_clock_step():
    ir = compile_source(_ADVANCE_DOC, "<t>")
    lc = next(t for t in ir["tests"] if t.get("lifecycle"))
    advances = [s for s in lc["body"] if s.get("step") == "advance"]
    assert advances == [{"step": "advance", "ms": 35_000}]
    # additive: the IR version is unchanged by the new lifecycle step
    assert ir.get("ir_version") == 3


def test_advance_only_in_a_lifecycle_body():
    with pytest.raises(RevlError, match="only allowed in a `lifecycle test` body"):
        compile_source('test "t" { advance 30s }', "<t>")


@pytest.mark.parametrize("src,match", [
    ("advance 30", "duration unit"),
    ("advance s", "whole-number duration"),
    ("advance 0s", "must be positive"),
])
def test_bad_advance_is_refused(src, match):
    with pytest.raises(RevlError, match=match):
        compile_source(f'lifecycle test "t" {{ {src} }}', "<t>")


# --------------------------------------------------------- emitted-code shape

def test_python_emit_advances_and_resets_the_clock():
    src = _emitter("python").emit(compile_source(_ADVANCE_DOC, "<t>"))
    # the clock coeffect is imported, reset for isolation, and advanced
    assert "from runtime import" in src and "Clock" in src
    assert "Clock.reset()" in src
    assert "Clock.advance(35000)" in src


def test_typescript_emit_advances_and_resets_the_clock():
    src = _emitter("typescript").emit(compile_source(_ADVANCE_DOC, "<t>"))
    assert "host.clockReset()" in src
    assert "host.clockAdvance(35000)" in src


def test_a_timer_free_lifecycle_test_is_unchanged_by_item102():
    """The `Clock` import and per-test reset appear only for a test that
    actually advances the clock — a plain lifecycle test is byte-identical to
    its pre-item-102 output."""
    plain = ("service S { fn ping() -> Int }\n"
             "component C provides s: S { provide s { fn ping() = 1 } }\n"
             'lifecycle test "t" { load C  let x = call s.ping()  assert x == 1  '
             "unload C  assert no_residue }")
    for backend, needle in [("python", "Clock"), ("typescript", "clock")]:
        src = _emitter(backend).emit(compile_source(plain, "<t>"))
        assert needle not in src


# --------------------------------------------- execution: firing is assertable

@pytest.mark.skipif(not CORDIS_PY.exists(),
                    reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")
def test_py_lifecycle_test_observes_deterministic_firing():
    """The exit test on the py reference tier: an `every`/`after` timer's body
    runs after the clock is advanced, and unload cancels residue-free."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [str(CORDIS_PY), "-m", "revl", "test", str(EXAMPLES / "lifecycle_timer.rvl")],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS an every-timer fires on each advanced tick" in result.stdout
    assert "PASS an after-timer fires once when its delay elapses" in result.stdout
    assert "[py] pass: 2 test(s) passed" in result.stdout


@pytest.mark.skipif(not CORDIS_PY.exists(),
                    reason="cordis-py runtime not installed")
def test_py_a_wrong_firing_count_is_actually_caught(tmp_path):
    """An assertion that can only pass is not an assertion: assert the wrong
    tick count and the lifecycle test must FAIL."""
    src = (EXAMPLES / "lifecycle_timer.rvl").read_text(encoding="utf-8")
    broken = src.replace("assert ticks == 3", "assert ticks == 99")
    assert broken != src
    path = tmp_path / "broken_timer.rvl"
    path.write_text(broken, encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [str(CORDIS_PY), "-m", "revl", "test", str(path)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAIL an every-timer fires on each advanced tick" in result.stdout


@pytest.mark.skipif(not VITEST.exists(),
                    reason="vitest not installed in backends/typescript")
def test_ts_lifecycle_test_observes_deterministic_firing():
    """The exit test on the ts reference tier — the same firing, driven by
    `host.clockAdvance` on cordis-ts."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-m", "revl", "test", "--backend", "ts",
         str(EXAMPLES / "lifecycle_timer.rvl")],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[ts] pass:" in result.stdout


# ------------------------------------ documented follow-on: non-reference tiers

@pytest.mark.parametrize("tier", ["wasm"])
def test_advance_refuses_honestly_on_non_reference_tiers(tier):
    """wasm is the remaining follow-on: it has no live-composition lifecycle
    machinery, so an `advance`-bearing lifecycle test is refused wholesale
    rather than silently mis-lowered. Both halves of item 112 have landed — go
    lowers `advance` to its Clock (`test_advance_lowers_to_the_go_clock`) and
    rust lowers it too (item 114, `test_advance_lowers_to_the_clock_on_rust`) —
    so neither tier appears here any more."""
    ir = compile_source(
        "service S { fn ping() -> Int }\n"
        "component C provides s: S { provide s { fn ping() = 1 } }\n"
        'lifecycle test "t" { load C  advance 5s  unload C  assert no_residue }',
        "<t>")
    with pytest.raises(Exception) as exc:
        _emitter(tier).emit(ir)
    # wasm refuses the whole lifecycle test (it cannot drive a live composition),
    # which subsumes the advance step — an honest refusal, not a mis-lowering.
    assert "wasm tier" in str(exc.value)


def test_advance_lowers_to_the_clock_on_rust():
    """Item 112 (rust half, item 114): the rust lifecycle emitter lowers an
    `advance` step to `revl_clock_advance(ms)` (item 99's Clock) instead of
    refusing it — so a rust lifecycle test can drive the clock and assert timer
    firings, like the py/ts reference tiers. A timer-arming component pulls in
    the clock preamble; the runtime behaviour is proven end-to-end by cargo in
    backends/rust/test_emit_rust.py::test_advance_lifecycle_runs_on_real_cordis_rs."""
    ir = compile_source(
        "service Counter { fn count() -> Int  emission fn tick() }\n"
        "component TickCounter provides counter: Counter {\n"
        "  let store = effect Map.new() undo store.drop()\n"
        "  provide counter {\n"
        "    fn count() = store.size()\n"
        "    fn tick() {\n"
        "      let key = `k-${store.size()}`\n"
        "      effect store.insert(key, \"fired\")\n"
        "      undo   store.remove(key)\n"
        "    }\n"
        "  }\n"
        "}\n"
        "component Heartbeat requires counter: Counter {\n"
        "  every 10s { emit counter.tick() }\n"
        "}\n"
        'lifecycle test "fires" { load TickCounter  load Heartbeat  '
        "advance 25s  let n = call counter.count()  assert n == 2  "
        "unload Heartbeat  unload TickCounter  assert no_residue }",
        "<t>")
    src = _emitter("rust").emit(ir)
    assert "revl_clock_advance(25000)" in src
    assert "pub fn revl_clock_advance(ms: i64) -> usize" in src
    # the clock is reset at test start so an advance sees only this test's timers
    assert "revl_clock_reset();" in src


def test_advance_lowers_to_the_go_clock():
    """item 102 (go half): the go lifecycle emitter lowers `advance <n><unit>`
    to RevlClockAdvance(N) (resetting the clock at test start), driving the same
    deterministic Clock item 99 gave go — no more `unknown lifecycle step`."""
    ir = compile_source(
        "service Counter { fn count() -> Int  emission fn tick() }\n"
        "component TickCounter provides counter: Counter {\n"
        "  let store = effect Map.new() undo store.drop()\n"
        "  provide counter {\n"
        "    fn count() = store.size()\n"
        "    fn tick() { let k = `t-${store.size()}`  effect store.insert(k, \"x\")  undo store.remove(k) }\n"
        "  }\n"
        "}\n"
        "component Heartbeat requires counter: Counter { every 10s { emit counter.tick() } }\n"
        'lifecycle test "t" {\n'
        "  load TickCounter  load Heartbeat\n"
        "  advance 35s\n"
        "  let n = call counter.count()\n"
        "  assert n == 3\n"
        "  unload Heartbeat  unload TickCounter  assert no_residue\n"
        "}",
        "<t>")
    src = _emitter("go").emit(ir)
    assert "RevlClockAdvance(35000)" in src
    assert "RevlClockReset()" in src
    assert "func RevlClockAdvance(ms int64) int" in src


@pytest.mark.skipif(shutil.which("go") is None, reason="go toolchain not installed")
def test_go_lifecycle_test_observes_deterministic_firing(tmp_path):
    """The exit test on the go tier: `revl test --backend go` on the timer
    lifecycle doc advances the go Clock and asserts the firing counts — the go
    analog of the py/ts reference-tier exit tests above."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-m", "revl", "test", "--backend", "go",
         str(EXAMPLES / "lifecycle_timer.rvl")],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[go] pass:" in result.stdout


# ============================================================================
# item 170: async timer bodies — the `Async[T]` in-flight window for a timer
# ============================================================================
#
# Item 57's timer bodies could reach a REQUIRED service only *synchronously*
# (G4), so a scheduled automation firing an `emission async fn` — the harness's
# `every 60s { emit agent.run_in(...) }` — was unexpressible. Item 170 gives the
# timer body the same async in-flight window an async provide method gets: a
# timer body reaching an async op is ADMITTED and coloured async (frontend), its
# firing spawns the suspension into a tracked in-flight window awaited by the
# harness (py emit), and unload cancels the pending timer PLUS any in-flight
# async work — residue-free (R4/A8). Sync timer bodies are unchanged.

_ASYNC_AGENT = """
service Agent { emission async fn run_in(session: Str, prompt: Str) }
component Cron requires agent: Agent {
  every 60s { emit agent.run_in("cron", "brief") }
}
"""

_ASYNC_EXTERN_TIMER = """
extern emission async fn ping(url: Str) -> Str = @py { return url }
service Log { emission fn write(m: Str) }
component Beat requires log: Log {
  every 30s { emit ping("http://x") }
}
"""

_SYNC_TIMER = """
service Log { emission fn write(m: Str) }
component Beat requires log: Log {
  every 30s { emit log.write("tick") }
}
"""


def _timer_step(src: str):
    ir = compile_source(src, "<t>")
    for comp in ir["components"]:
        for step in comp["body"]:
            if step.get("step") == "timer":
                return step
    raise AssertionError("no timer step lowered")


# ---------------------------------------------------------------- frontend

def test_timer_reaching_a_req_async_op_is_admitted_and_coloured_async():
    """The scheduled-agent-run shape: `every 60s { emit agent.run_in(...) }`
    reaching an `emission async fn` through a required key is ADMITTED (not
    refused) and the timer step is coloured `async` (item 170)."""
    step = _timer_step(_ASYNC_AGENT)
    assert step["async"] is True


def test_timer_reaching_an_async_extern_is_admitted_and_coloured_async():
    """An async extern reached from a timer body was refused pre-170
    (`reaches async extern … in a setup/activation body`); now it is admitted
    and coloured async, exactly like the req-async-op path."""
    step = _timer_step(_ASYNC_EXTERN_TIMER)
    assert step["async"] is True


def test_a_sync_timer_body_carries_no_async_colour():
    """The sync path is unchanged: a timer body reaching only sync emissions
    carries no `async` key on its step (byte-identical IR)."""
    step = _timer_step(_SYNC_TIMER)
    assert "async" not in step


# ---------------------------------------------------------------- py emit

def test_py_emits_a_sync_timer_byte_identically():
    """A sync timer body still emits the plain `def _timer_N` closure + a
    `lambda: handle.cancel()` inverse — no asyncio, no in-flight set."""
    src = _emitter("python").emit(compile_source(_SYNC_TIMER, "<t>"))
    assert "def _timer_1():" in src
    assert "_revl_schedule_every(30000, _timer_1)" in src
    assert "yield lambda: _timer_1_h.cancel()" in src
    assert "_revl_asyncio" not in src            # no asyncio for a sync timer
    assert "_inflight" not in src


def test_py_emits_an_async_timer_with_a_tracked_in_flight_window():
    """An async-coloured timer spawns each async emission as a tracked task and
    yields an inverse that cancels the schedule AND every in-flight task."""
    src = _emitter("python").emit(compile_source(_ASYNC_AGENT, "<t>"))
    assert "import asyncio as _revl_asyncio" in src
    assert "_timer_1_inflight = set()" in src
    assert "_revl_task = _revl_asyncio.ensure_future(_revl_ctx.agent.run_in('cron', 'brief'))" in src
    assert "_timer_1_inflight.add(_revl_task)" in src
    assert "_revl_task.add_done_callback(_timer_1_inflight.discard)" in src
    # the inverse cancels the schedule and drains the in-flight window
    assert "def _timer_1_cancel():" in src
    assert "_timer_1_h.cancel()" in src
    assert "for _revl_task in list(_timer_1_inflight):" in src
    assert "_revl_task.cancel()" in src
    assert "yield _timer_1_cancel" in src


# ------------------------------- runtime: async firing + in-flight cancellation
#
# These drive the real cordis-py runtime scheduler (backends/python/runtime.py)
# + real asyncio directly on the *emitted* module — no cordis-py install needed —
# so the await-on-tick and cancel-in-flight proofs run in the default suite.

def _install_async_timer_module(rt):
    """Compile+emit the async-agent composition, exec it, and drive its
    activation body against `rt`'s scheduler with an async `agent.run_in` stub
    that records each (session, prompt) it is awaited with. Returns
    (run_log, disposers, fire_via_advance)."""
    import asyncio
    src = _emitter("python").emit(compile_source(_ASYNC_AGENT, "<t>"))
    captured: dict = {}

    class _StubFrame:
        def __init__(self, ctx, name):
            self.ctx = ctx
        def install(self, body):
            captured["body"] = body
        def begin(self):
            return None
        def drain(self):
            return None

    fake = types.ModuleType("runtime")
    fake.Frame = _StubFrame
    fake.schedule_every = rt.schedule_every
    fake.schedule_after = rt.schedule_after
    saved = sys.modules.get("runtime")
    sys.modules["runtime"] = fake

    run_log: list = []

    async def _run_in(session, prompt):
        await asyncio.sleep(0)     # a genuine suspension in the in-flight window
        await asyncio.sleep(0)
        run_log.append((session, prompt))

    ctx = types.SimpleNamespace(agent=types.SimpleNamespace(run_in=_run_in))
    try:
        ns: dict = {}
        exec(compile(src, "<emitted>", "exec"), ns)
        # apply() builds the Frame and installs the activation body (the stub
        # Frame captures it); the body is a zero-arg generator closing over ctx.
        ns["Cron"]["apply"](ctx, {})
    finally:
        if saved is not None:
            sys.modules["runtime"] = saved
        else:
            sys.modules.pop("runtime", None)

    disposers: list = []
    for value in captured["body"]():
        if callable(value):
            disposers.append(value)
    return run_log, disposers


def test_async_timer_fires_and_is_awaited_on_each_advanced_tick(rt):
    """The async body fires on each tick the advance crosses and is *awaited*
    within the in-flight window: the recorded runs equal the firing count."""
    import asyncio

    async def _main():
        run_log, disposers = _install_async_timer_module(rt)
        rt.Clock.advance(180_000)                 # fires at 60s, 120s, 180s
        for _ in range(20):                        # settle the in-flight window
            await asyncio.sleep(0)
        assert run_log == [("cron", "brief")] * 3  # awaited three times
        assert rt.Clock.pending() == 1             # schedule still live
        for dispose in reversed(disposers):        # unload, LIFO
            dispose()
        assert rt.Clock.pending() == 0             # schedule cancelled — no residue
        rt.Clock.advance(1_000_000)
        for _ in range(20):
            await asyncio.sleep(0)
        assert run_log == [("cron", "brief")] * 3  # no orphaned firing

    asyncio.run(_main())


def test_unload_while_async_work_is_in_flight_cancels_it_no_residue(rt):
    """R4/A8 for the async case: unload *while a fired body's async work is
    still in flight* cancels both the schedule and the in-flight task, so the
    suspended work never completes and leaves no side effect — no orphaned
    in-flight async work after withdraw."""
    import asyncio

    async def _main():
        run_log, disposers = _install_async_timer_module(rt)
        rt.Clock.advance(60_000)                   # fires once -> spawns a task
        await asyncio.sleep(0)                      # partial progress, NOT settled
        assert run_log == []                        # the run has not completed yet
        for dispose in reversed(disposers):         # UNLOAD mid-flight
            dispose()
        for _ in range(20):                         # drain
            await asyncio.sleep(0)
        assert rt.Clock.pending() == 0              # schedule cancelled
        assert run_log == []                        # in-flight work cancelled — no effect

    asyncio.run(_main())


# --------------------------------------------- execution: end-to-end on cordis

@pytest.mark.skipif(not CORDIS_PY.exists(),
                    reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")
def test_py_async_timer_lifecycle_runs_end_to_end():
    """The exit test for item 170 on the py reference tier: an async timer body
    (`every 10s { emit counter.tick() }` reaching an `emission async fn`)
    compiles, admits, RUNS — firing + awaited on each advanced tick — and
    unload is residue-free (examples/async_timer.rvl)."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [str(CORDIS_PY), "-m", "revl", "test", str(EXAMPLES / "async_timer.rvl")],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS an async every-timer fires and is awaited on each tick" in result.stdout
    assert "PASS an async after-timer fires once when its delay elapses" in result.stdout
    assert "[py] pass: 2 test(s) passed" in result.stdout


@pytest.mark.skipif(not CORDIS_PY.exists(),
                    reason="cordis-py runtime not installed")
def test_py_async_timer_wrong_firing_count_is_caught(tmp_path):
    """An assertion that can only pass is not an assertion: assert the wrong
    awaited-firing count and the async-timer lifecycle test must FAIL."""
    src = (EXAMPLES / "async_timer.rvl").read_text(encoding="utf-8")
    broken = src.replace("assert ticks == 3", "assert ticks == 99")
    assert broken != src
    path = tmp_path / "broken_async_timer.rvl"
    path.write_text(broken, encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [str(CORDIS_PY), "-m", "revl", "test", str(path)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAIL an async every-timer fires and is awaited on each tick" in result.stdout


# ============================================================================
# item 223: async timer bodies on the other tiers (ts + rust/go)
# ============================================================================
#
# Item 170 landed the async-timer contract on the py reference tier. The IR flag
# (`"async": true` on the lowered `timer` step, stamped by the shared frontend)
# is tier-independent, so ts/rust/go all receive it. This slice mirrors the py
# in-flight/cancel contract where a tier can express it:
#
#   * ts — async service ops are REAL (an emission returns `Promise<void>`), so a
#     sync firing closure would call one UN-AWAITED. The async-coloured `_timer`
#     now spawns each async emission as a tracked in-flight `Promise` (a per-timer
#     Set), the harness's `_revl_settle` after an advance drains it, and the
#     inverse cancels the schedule + drops in-flight (the py contract, mirrored).
#   * rust/go — these tiers have NO async-fn machinery: an `Async[T]`/async op
#     ERASES to its synchronous form (`_erase_async`; the emitted trait method is
#     `fn run_in(..) -> ()` / `RunIn(..)`, no future). A timer body reaching such
#     an op therefore fires SYNCHRONOUSLY to completion inside the deterministic
#     advance — there is no coroutine to spawn, no in-flight window to drain, and
#     no in-flight work to cancel. The existing sync firing + schedule-cancel
#     inverse is already the complete, residue-free contract, so the `async` flag
#     is correctly a NO-OP there: spawning a goroutine/JoinHandle would only
#     inject nondeterminism into a timeline step the clock coeffect exists to keep
#     deterministic. No emit change is needed — and `examples/async_timer.rvl`
#     still RUNS on both (the async firing collapses to a correct synchronous one,
#     the every/after timelines land count==3/count==1, teardown is residue-free).
#     The one dimension only py expresses — cancelling work *mid-flight* on unload
#     — is vacuous where nothing suspends. These tests lock the no-op emit AND the
#     end-to-end run in.


# ---------------------------------------------------------------- ts emit

def test_ts_emits_a_sync_timer_byte_identically():
    """A sync timer body still emits the plain `() => {…}` firing closure + a
    `() => handle.cancel()` inverse — no Set, no tracked Promise (item 223)."""
    src = _emitter("typescript").emit(compile_source(_SYNC_TIMER, "<t>"))
    assert "const $revl_timer_1 = () => {" in src
    assert "host.scheduleEvery(30000, $revl_timer_1)" in src
    assert "yield () => $revl_timer_1_h.cancel()" in src
    assert "_inflight" not in src                 # no in-flight set for a sync timer
    assert "Promise.resolve" not in src


def test_ts_emits_an_async_timer_with_a_tracked_in_flight_window():
    """An async-coloured timer spawns each async emission as a tracked in-flight
    Promise and yields an inverse that cancels the schedule AND drops in-flight
    (the py `_timer` in-flight/cancel contract, mirrored on ts)."""
    src = _emitter("typescript").emit(compile_source(_ASYNC_AGENT, "<t>"))
    assert "const $revl_timer_1_inflight = new Set<Promise<void>>()" in src
    assert 'Promise.resolve(ctx.agent.run_in("cron", "brief"))' in src
    assert "$revl_timer_1_inflight.add(_revl_task)" in src
    assert "$revl_timer_1_inflight.delete(_revl_task)" in src
    # the inverse cancels the schedule and drops the in-flight window
    assert "yield () => { $revl_timer_1_h.cancel(); $revl_timer_1_inflight.clear() }" in src
    # the async emission is NOT fired un-awaited/inline
    assert "\n        ctx.agent.run_in(" not in src


def test_ts_async_timer_scopes_to_the_req_async_op_reach():
    """ts async-timer support covers the primary shape: a timer firing an
    `emission async fn` through a REQUIRED key (`every 60s { emit
    agent.run_in(...) }` — the scheduled-automation motivating case). The
    async-EXTERN reach a timer body may also take remains unemittable on ts —
    the pre-existing `_fn_call` guard refuses an async extern in a sync closure
    (it predates item 223 and is unchanged) — so that reach is a documented
    follow-on, not a regression."""
    src = _emitter("typescript").emit(compile_source(_ASYNC_AGENT, "<t>"))
    assert "Promise.resolve(ctx.agent.run_in(" in src   # req-async-op reach works


# ---------------------------------------------------------------- ts execution

def test_ts_async_timer_lifecycle_runs_end_to_end():
    """The exit test for item 223 on the ts tier: `examples/async_timer.rvl`
    (an async every-timer + after-timer reaching an `emission async fn`)
    emits, and — where vitest is installed — RUNS: each firing is spawned +
    settled on every advanced tick and unload is residue-free."""
    from revl.test import run_ts  # noqa: PLC0415
    ir = compile_source((EXAMPLES / "async_timer.rvl").read_text(encoding="utf-8"),
                        "async_timer.rvl")
    outcome, message = run_ts(ir)
    if outcome == "skip":
        pytest.skip(f"ts: {message}")
    assert outcome == "pass", message


def test_ts_async_timer_wrong_firing_count_is_caught():
    """An assertion that can only pass is not an assertion: assert the wrong
    awaited-firing count and the ts async-timer lifecycle must FAIL (where the
    toolchain runs it)."""
    from revl.test import run_ts  # noqa: PLC0415
    src = (EXAMPLES / "async_timer.rvl").read_text(encoding="utf-8")
    broken = src.replace("assert ticks == 3", "assert ticks == 99")
    assert broken != src
    outcome, message = run_ts(compile_source(broken, "broken_async_timer.rvl"))
    if outcome == "skip":
        pytest.skip(f"ts: {message}")
    assert outcome == "fail", message


# ------------------------------------------------- rust/go: async erases to sync

@pytest.mark.parametrize("tier", ["rust", "go"])
def test_rust_and_go_erase_an_async_timer_to_a_sync_firing(tier):
    """rust/go have no async-fn machinery: an async op erases to its sync form
    (`_erase_async`), so a timer reaching one fires synchronously to completion
    inside the deterministic advance. The emitted timer is a plain schedule +
    cancel with NO in-flight/spawn machinery — the `async` IR flag is a correct
    no-op, and this is byte-identical to a sync timer body (item 223)."""
    emit = _emitter(tier)
    async_src = emit.emit(compile_source(_ASYNC_AGENT, "<t>"))
    # no tracked-in-flight / async-executor machinery leaks in
    for token in ("inflight", "in_flight", "JoinHandle", "tokio::spawn",
                  "go func(", "goroutine", "ensure_future", "Promise"):
        assert token not in async_src, f"{tier}: unexpected {token!r} in async-timer emit"
    # the plain sync schedule + cancel inverse is what a sync timer emits, too
    if tier == "rust":
        assert "revl_schedule_every(60000, move ||" in async_src
        assert "revl_cancel(_revl_timer_1)" in async_src
    else:
        assert "revlScheduleEvery(60000, func()" in async_src
        assert "_revlTimer1.Cancel()" in async_src


def test_go_async_timer_lifecycle_runs_end_to_end():
    """`examples/async_timer.rvl` RUNS on go: the async firing collapses to a
    correct synchronous one (RunIn is erased to a sync method), so the every/
    after timelines land count==3 / count==1 and teardown is residue-free."""
    from revl.test import run_go  # noqa: PLC0415
    ir = compile_source((EXAMPLES / "async_timer.rvl").read_text(encoding="utf-8"),
                        "async_timer.rvl")
    outcome, message = run_go(ir)
    if outcome == "skip":
        pytest.skip(f"go: {message}")
    assert outcome == "pass", message


def test_go_async_timer_wrong_firing_count_is_caught():
    """The go run is a real assertion: break the expected firing count and the
    emitted lifecycle test must FAIL under `go test`."""
    from revl.test import run_go  # noqa: PLC0415
    broken = (EXAMPLES / "async_timer.rvl").read_text(encoding="utf-8").replace(
        "assert ticks == 3", "assert ticks == 99")
    outcome, message = run_go(compile_source(broken, "broken_async_timer.rvl"))
    if outcome == "skip":
        pytest.skip(f"go: {message}")
    assert outcome == "fail", message


@pytest.mark.skipif(not os.environ.get("REVL_CROSS_TIER_SLOW"),
                    reason="set REVL_CROSS_TIER_SLOW=1 (cargo is slow / needs crates.io)")
def test_rust_async_timer_lifecycle_runs_end_to_end():
    """`examples/async_timer.rvl` RUNS on rust too (async erased to sync), gated
    behind REVL_CROSS_TIER_SLOW like the other cargo-driven cross-tier probes."""
    from revl.test import run_rust  # noqa: PLC0415
    ir = compile_source((EXAMPLES / "async_timer.rvl").read_text(encoding="utf-8"),
                        "async_timer.rvl")
    outcome, message = run_rust(ir)
    if outcome == "skip":
        pytest.skip(f"rust: {message}")
    assert outcome == "pass", message
