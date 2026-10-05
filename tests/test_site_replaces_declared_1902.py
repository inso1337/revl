"""One compensation per crossing: a site-spelled `compensate` REPLACES the one
the extern declares (issue #1902).

An emission extern can declare its own reversal (item 254), and an `emit` can
spell one at the site:

    extern emission fn put_row(body: Str) -> Int compensate restore_row() = ...
    emit put_row("s") compensate plain_restore()

WHAT WAS WRONG. Every tier registered BOTH for that crossing, so an abort ran
two offsets for one emission (two refunds for one charge). py, ts, go and rust
ran `restore_row` then `plain_restore`; java ran them the other way round.
The sweep's owed list (`fault._owed_compensations`) already took the
site-spelled one alone, so `revl test --sweep` reported every tier DIVERGED,
py included.

WHAT HOLDS NOW. The declared compensation is the default, used only when the
site spells none. Each tier registers exactly the site-spelled one, at the
activation body and in a provide method, and the sweep agrees. On the py tier
a computer-use crossing registers its own compensation through
`declared_crossing`; a site-spelled one is handed to it and registered in its
place, also when the crossing raises (538 §10's failing-step rule).

The emitted-code tests need no toolchain. The executed ones run each tier's
real runtime and SKIP without it; a skip is not a pass.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl import fault as fault_mod  # noqa: E402


def _bodies(py: str, ts: str, go: str, java: str, rs: str) -> str:
    return (f"  = @py {{ {py} }}\n  = @ts {{ {ts} }}\n  = @go {{ {go} }}\n"
            f"  = @java {{ {java} }}\n  = @rs {{ {rs} }}\n")


_ONE = _bodies("return 1", "return 1n", "return 1", "return 1L;", "1")

EXTERNS = (
    "extern emission fn restore_row() -> Int\n" + _ONE
    + "extern emission fn plain_restore() -> Int\n" + _ONE
    + "extern emission fn put_row(body: Str) -> Int compensate restore_row()\n" + _ONE
    # go and rust bridge at least one service
    + "service Ping {\n  fn ping() -> Int\n}\n"
    "component Pinger provides p: Ping {\n  provide p { fn ping() = 1 }\n}\n"
)

#: Both spelled, at the activation body and in a provide method.
BOTH = EXTERNS + (
    "service Rows {\n  emission fn put(m: Str)\n}\n"
    "component Store provides rows: Rows {\n"
    "  provide rows {\n"
    "    fn put(m) { emit put_row(m) compensate plain_restore() }\n"
    "  }\n"
    "}\n"
    "component SiteAbort {\n"
    '  emit put_row("s") compensate plain_restore()\n'
    "}\n"
)

#: The control: the same crossings with no site spelling owe the declared one.
DECLARED_ONLY = BOTH.replace(" compensate plain_restore()", "")

_BACKENDS = ["python", "typescript", "go", "rust", "java"]


def _emit(backend: str, source: str) -> str:
    ir = compile_source(source, f"site_replaces_{backend}.rvl")
    path = ROOT / "backends" / backend / "emit.py"
    spec = importlib.util.spec_from_file_location(f"revl_emit_1902_{backend}", path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
    return module.emit(ir)


def _calls(src: str, name: str) -> int:
    """`name()` occurrences, its own definition included."""
    return len(re.findall(rf"\b{name}\(\)", src))


# ------------------------------------------------------------ emitted code


@pytest.mark.parametrize("backend", _BACKENDS)
def test_a_site_spelled_compensate_is_the_only_one_registered(backend):
    src = _emit(backend, BOTH)
    # only `restore_row`'s own definition: no site registers it
    assert _calls(src, "restore_row") == 1, src
    # the definition and the two site registrations
    assert _calls(src, "plain_restore") >= 3


@pytest.mark.parametrize("backend", _BACKENDS)
def test_with_no_site_spelling_the_declared_one_is_registered(backend):
    src = _emit(backend, DECLARED_ONLY)
    assert _calls(src, "restore_row") >= 3
    assert _calls(src, "plain_restore") == 1


# ------------------------------------------------------------ executed


def _ran_at_site_abort(record: dict) -> list:
    [point] = [p for p in record["points"] if p["component"] == "SiteAbort"]
    return point["compensations"]["ran"]


def _sweep_ir() -> dict:
    """The activation-body crossing alone, so the faulted component is the
    only one that owes anything."""
    source = EXTERNS + 'component SiteAbort {\n  emit put_row("s") compensate plain_restore()\n}\n'
    return compile_source(source, "site_replaces_sweep.rvl")


@pytest.mark.skipif(importlib.util.find_spec("cordis") is None,
                    reason="cordis-py runtime not installed (sh backends/python/setup.sh)")
def test_the_py_tier_runs_only_the_site_spelled_one_and_the_sweep_agrees():
    record = fault_mod._py_tier_sweep(_sweep_ir())
    assert _ran_at_site_abort(record) == ["plain_restore"]
    assert record["status"] == "executed", record["reason"]


def _needs(tier: str):
    if tier == "go" and shutil.which("go") is None:
        return pytest.mark.skipif(True, reason="go not installed")
    _runner, reason_of = fault_mod._once_runner(tier)
    reason = reason_of()
    return pytest.mark.skipif(reason is not None, reason=f"{tier}: {reason}")


@pytest.mark.parametrize("tier", [
    pytest.param("ts", marks=[_needs("ts")]),
    pytest.param("go", marks=[_needs("go")]),
    pytest.param("rust", marks=[_needs("rust")]),
    pytest.param("java", marks=[_needs("java")]),
])
def test_a_compiled_tier_runs_only_the_site_spelled_one_and_the_sweep_agrees(tier):
    record = fault_mod._compiled_tier_sweep(tier, _sweep_ir(), {}, [], None)
    assert _ran_at_site_abort(record) == ["plain_restore"]
    assert record["status"] == "executed", record["reason"]


# ------------------------------------------------- py computer-use crossing


def _ui_module():
    path = ROOT / "tests" / "test_ui_transaction_runtime_1369.py"
    spec = importlib.util.spec_from_file_location("revl_ui_runtime_1369_for_1902", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_UI = _ui_module()

#: A computer-use crossing whose extern declares `clear_amount()` and whose
#: site spells `site_clear()`, then a second crossing to fail at.
UI_BOTH = _UI.UI_TARGET + _UI.DECLARATIONS + (
    f"extern pure fn site_clear() = {_UI._host(repr('compensate:site_clear'))}\n"
    "service Ops { emission fn run(region: Str) -> Int }\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn run(region) {\n"
    "      let pane1 = emit read_pane(region)\n"
    '      let amount = emit locate(pane1, "Amount")\n'
    '      emit type_amount(amount, "10") compensate site_clear()\n'
    "      let pane2 = emit read_pane(region)\n"
    '      let memo = emit locate(pane2, "Memo")\n'
    '      emit type_memo(memo, "m")\n'
    "      return 1\n"
    "    }\n"
    "  }\n"
    "}\n"
)

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the unit runs against a live cordis-py composition; install the "
           "pinned fork with `sh backends/python/setup.sh`")


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    log = tmp_path / "desktop.log"
    monkeypatch.setenv("REVL_UI_LOG", str(log))
    monkeypatch.delenv("REVL_UI_FAIL", raising=False)

    def lines() -> list[str]:
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return lines


def _ui_session():
    from revl.mcp.session import Session  # noqa: PLC0415
    session = Session()
    session.load(compile_source(UI_BOTH, "site_replaces_ui_1902.rvl"))
    return session


def test_the_ui_site_hands_its_compensation_to_the_crossing():
    src = _emit("python", UI_BOTH)
    assert "with _revl_site_compensation(lambda: site_clear()" in src
    # the decorator is the one registrar; the site adds no second entry
    assert "_revl_frame.compensation_method(lambda: site_clear()" not in src


@needs_cordis
def test_a_failed_ui_call_runs_the_site_spelled_compensation_not_the_declared_one(
        desktop, monkeypatch):
    monkeypatch.setenv("REVL_UI_FAIL", "type_memo")
    session = _ui_session()
    with pytest.raises(Exception):
        session.call("ops", "run", ["r"])
    assert _UI._compensations(desktop()) == ["clear_memo", "site_clear"]
    session.unload()


@needs_cordis
def test_a_raising_ui_crossing_registers_its_site_spelled_compensation(
        desktop, monkeypatch):
    """538 §10: the failing computer-use step registers its own compensation.
    Its own is the site-spelled one when the site spells it."""
    monkeypatch.setenv("REVL_UI_FAIL", "type_amount")
    session = _ui_session()
    with pytest.raises(Exception):
        session.call("ops", "run", ["r"])
    assert _UI._compensations(desktop()) == ["site_clear"]
    session.unload()
