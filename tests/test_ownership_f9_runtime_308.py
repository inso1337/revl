"""F9 — method-scope early release, proven at RUNTIME (roadmap item 308).

Design: `docs/design/308-effect-ownership-modes.md`, "F9, decided".

The frontend half of F9 lives in `tests/test_ownership_modes_308.py`. This file
pins the half a checker cannot state: WHAT EARLY RELEASE DOES TO TEARDOWN.

The claim under test is that revl's early release releases a SCOPE and never
DISCHARGES AN ENTRY. A `spawn` inside a provide method is a child fiber — its
own nested teardown scope — and `w.dispose()` unloads it now; the frame-adopted
safety net stays registered on the enclosing activation's LIFO for its whole
life, and `SpawnHandle.dispose` is idempotent, so the entry set G7 walks is
exactly what it was. Four traces, one per position the fault can land in:

  * no release            -> the instance is reclaimed at the SPAWNER's unload
                             (the leak-until-unload F9 names), inverse once;
  * early release         -> reclaimed during the request; the entry is STILL
                             registered (count goes UP, not down) and its later
                             replay is a no-op;
  * fault BEFORE release  -> the entry is registered and unrun; the fault is not
                             an activation abort, so it runs at teardown.
                             NOTHING IS MISSED;
  * fault AFTER release   -> the entry runs and finds the instance already gone.
                             NOTHING RUNS TWICE.

and one entry-level test for the third verdict: under item 443's `halted`, an
entry whose effect was already released early is STRANDED like any other —
registered, not run, not dropped. That is the whole reason early release must
not discharge: a discharge would drop the descriptor `revl recover` reads back,
which is what would contradict `RevL.G7.estop_strands_everything`. Stranding an
already-released entry OVER-reports one no-op replay, which is the R4-safe
direction; under-reporting residue is the bug.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import runtime as rt  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the teardown accounting is proven against a live cordis-py "
           "composition — install it with `sh backends/python/setup.sh` and "
           "run under its venv",
)

_TRACE_ENV = "REVL_F9_TRACE"


def _source(*, release: bool, fault: bool) -> str:
    """A supervisor whose provide method spawns a request-scoped instance,
    optionally releases it early, and optionally faults afterwards."""
    body = "      let w = effect spawn Worker with { } undo w.dispose()\n"
    if release:
        # the early-release surface, in the grammar revl already had
        body += "      effect w.dispose() undo w.dispose()\n"
    if fault:
        body += "      let z = boom()\n"
    body += "      return 1\n"
    return (
        'extern pure fn note(tag: Str) -> Int = @py {\n'
        '    import os\n'
        '    with open(os.environ["REVL_F9_TRACE"], "a") as fh:\n'
        '        fh.write(tag + "\\n")\n'
        '    return 1\n'
        '}\n'
        'extern pure fn boom() -> Int = @py {\n'
        '    raise RuntimeError("mid-scope fault")\n'
        '}\n'
        "component Worker {\n"
        '  let up = effect note("worker-up") undo note("worker-down")\n'
        "}\n"
        "service S { fn go() -> Int }\n"
        "component Sup provides s: S {\n"
        '  let a = effect note("sup-up") undo note("sup-down")\n'
        "  provide s {\n    fn go() {\n" + body + "    }\n  }\n}\n")


class _Run:
    """One session: the trace at the moment the call returned, the adopted-entry
    count at that moment, and the trace after a clean unload."""

    def __init__(self, mid, adopted, after, faulted, call_trace):
        self.mid = mid
        self.adopted = adopted
        self.after = after
        self.faulted = faulted
        self.call_trace = call_trace


def _run(tmp_path, monkeypatch, *, release: bool, fault: bool) -> _Run:
    from revl.mcp.session import Session

    path = tmp_path / "f9.trace"
    monkeypatch.setenv(_TRACE_ENV, str(path))
    session = Session()
    session.load(compile_source(_source(release=release, fault=fault), "f9.rvl"))
    faulted = None
    call_trace: list = []
    try:
        call_trace = session.call("s", "go", []).get("trace") or []
    except Exception as exc:  # the mid-scope fault propagates to the caller
        faulted = str(exc)
    driver = session._driver
    ((_name, fiber),) = driver.fibers.items()
    frame = driver.runtime._frame_for_ctx(fiber.ctx)
    mid = path.read_text().split()
    adopted = len(frame._adopted)
    session.unload()
    return _Run(mid, adopted, path.read_text().split(), faulted, call_trace)


# ---------------------------------------------------------------------------
# 1. no release: the instance is reclaimed at the SPAWNER's unload
# ---------------------------------------------------------------------------

@needs_cordis
def test_without_an_early_release_the_instance_lives_until_the_spawner_unloads(
        tmp_path, monkeypatch):
    """This is the lifetime concern F9 was filed for, stated as a fact rather
    than a worry: one adopted entry per call, and the instance's own teardown
    waits for the component's."""
    run = _run(tmp_path, monkeypatch, release=False, fault=False)
    assert run.faulted is None
    assert run.adopted == 1
    assert "worker-down" not in run.mid, "reclaimed before the spawner unloaded"
    # LIFO: the instance's bracket runs before the spawner's own activation one
    assert run.after == ["sup-up", "worker-up", "worker-down", "sup-down"]


# ---------------------------------------------------------------------------
# 2. early release: the scope goes, the ENTRY stays
# ---------------------------------------------------------------------------

@needs_cordis
def test_early_release_reclaims_during_the_request_and_discharges_nothing(
        tmp_path, monkeypatch):
    """The load-bearing observation. The child fiber is unloaded inside the
    call, and the adopted-entry count goes UP (the release registers its own
    bracket) rather than down — nothing is discharged, so G7's LIFO walk still
    sees every entry it saw before."""
    run = _run(tmp_path, monkeypatch, release=True, fault=False)
    assert run.faulted is None
    assert run.adopted == 2, "an early release must not discharge an entry"
    disposed = [e for e in run.call_trace
                if e.get("subject") == "Worker" and "DISPOSED" in (e.get("detail") or "")]
    assert disposed, "the instance was not reclaimed during the request"
    # reclaimed before it ever activated, so its bracket never registered; the
    # point is the count of runs, which is never two
    assert run.after.count("worker-down") == 0


# ---------------------------------------------------------------------------
# 3. + 4. what a fault MID-SCOPE does, on each side of the release
# ---------------------------------------------------------------------------

@needs_cordis
def test_a_fault_before_the_release_still_runs_the_inverse_at_teardown(
        tmp_path, monkeypatch):
    """NOTHING IS MISSED. A provide-method fault is not an activation abort —
    it propagates to the caller and leaves the registered entry alone — so the
    instance is still reclaimed, in LIFO position, at the component's teardown."""
    run = _run(tmp_path, monkeypatch, release=False, fault=True)
    assert run.faulted is not None and "mid-scope fault" in run.faulted
    assert run.adopted == 1, "the fault dropped a registered entry"
    assert "worker-down" not in run.mid
    assert run.after.count("worker-down") == 1
    assert run.after[-1] == "sup-down"


@needs_cordis
def test_a_fault_after_the_release_never_runs_the_inverse_twice(
        tmp_path, monkeypatch):
    """NOTHING RUNS TWICE. Both entries are still registered and both replay at
    teardown; `SpawnHandle.dispose` is idempotent, so the second and third
    dispose are no-ops on an instance that is already gone."""
    run = _run(tmp_path, monkeypatch, release=True, fault=True)
    assert run.faulted is not None and "mid-scope fault" in run.faulted
    assert run.adopted == 2
    assert run.after.count("worker-down") == 0
    assert run.after.count("sup-down") == 1


# ---------------------------------------------------------------------------
# 5. the third verdict: an early-released entry is STRANDED, not dropped
# ---------------------------------------------------------------------------

class _Ctx:
    """The minimum a `Frame` reads off its context: no timeline, so no WAL —
    the shape a run without `--wal` really has (mirrors tests/test_estop_443)."""


@pytest.fixture
def clean_halt():
    def _reset():
        rt.clear_estop()
        rt.arm_estop_latch(None)
        rt._LIVE_FRAMES.clear()
    _reset()
    yield
    _reset()


def test_an_already_released_entry_is_stranded_by_a_halt_not_dropped(clean_halt):
    """`halted` and early release are adjacent ideas that must not contradict.

    They do not, because they act on different things: the halt disposes of
    ENTRIES (registered, not run, not dropped) and the release disposes of the
    EFFECT. An entry whose instance was already reclaimed is stranded like any
    other and its recorded replay is a no-op — an OVER-report, which is the
    direction R4 requires. Discharging on release is what would break this: the
    descriptor `revl recover` reads back would be gone."""
    ran: list = []
    frame = rt.Frame(_Ctx(), "Sup")

    # the frame-adopted safety net, standing in for `lambda w: w.dispose()` on
    # a handle whose instance is already gone: idempotent, hence a no-op
    disposed = {"done": False}

    def _safety_net():
        if disposed["done"]:
            return None
        disposed["done"] = True
        ran.append("dispose")
        return None

    guarded = frame._guard(_safety_net)

    _safety_net()                       # the EARLY RELEASE, mid-request
    assert ran == ["dispose"]

    rt.estop("halt", operator="alice")
    guarded()                           # the halt reaches the standing entry

    assert ran == ["dispose"], "the halt replayed an inverse"
    assert [r["kind"] for r in frame.estop_residue] == ["estop-stranded"]
    assert frame.estop_residue[0]["entry"] == "bracket"
