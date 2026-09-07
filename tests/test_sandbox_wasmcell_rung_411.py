"""The `wasm-cell` isolation rung's runtime driver (roadmap item 411, toward #107).

Slice 2 made the `container` rung real. This file covers the SECOND rung of the
ladder — the in-process wasm cell — at the level this slice implements it: the
driver establishes and verifies the cell SUBSTRATE (the launch + health half),
and refuses to boot a placement component in it because the py runner's cell
mode (instantiating the component's emitted wasm module with a generated import
set) is item 411 Stage 4 and not built. The one rung then left driverless is
`microvm` (it needs a hypervisor / `/dev/kvm`).

The cell is NOT an OS boundary, so unlike the container rung it derives no
fs/net flags: its confinement is a generated import set, and the fs/net/`*`
refusals for a cell already fire at PLAN time (`_normalize_sandbox_table`,
`_sandbox_capability_gate`), covered in test_sandbox_placement_411.py, not here.

Levels:

1. plan-layer, with NO wasm substrate at all: the rung resolves to a driver,
   the PURE `evaluate_cell` judge over synthetic in-cell canary reports (good
   and every tampered shape), and `preflight`'s refusals — substrate absent,
   wrong backend, a stray OS envelope, an unconfirmed cell, and the headline
   "substrate verified but Stage 4 unbuilt" refusal that carries the evidence.
   These need no wasmtime and run everywhere.
2. against a REAL wasm substrate, gated on `run_wasm.wasm_runtime_reason()` (the
   same gate the wasm TIER uses — the cordis-wasm venv that carries wasmtime, so
   this rung adds no new environment switch, item 445): the in-cell canary boots
   two probe modules and CONFIRMS the cell grants zero ambient authority and
   fails an ungranted import at instantiation. It skips wherever the wasm tier
   skips (every default checkout and CI job without cordis-wasm), which is
   stated plainly rather than hidden behind a green run.
"""

import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import placement as _placement  # noqa: E402
from revl import sandbox_runtime as _sb  # noqa: E402
from revl.run_wasm import wasm_runtime_reason  # noqa: E402

# A cell-eligible component: a plain emission with no host body and no `*` reach.
_APP = """
service Job { emission fn run() -> Str }
component Lonely provides job: Job {
  provide job { fn run() = "ok" }
}
"""

# A canary report that a correctly-confined cell produces.
_GOOD = {"IMPORTS": "0", "AMBIENT": "blocked:WasmtimeError",
         "WTVERSION": "21.0.0", "CELL": "done"}


# ==========================================================================
# 1. plan-layer: no wasm substrate needed
# ==========================================================================

def test_the_wasm_cell_rung_now_resolves_to_a_driver():
    driver = _sb.resolve_driver("wasm-cell")
    assert isinstance(driver, _sb.WasmCellDriver)
    assert driver.rung == "wasm-cell"
    # microvm is still the one driverless rung and must not be downgraded.
    assert _sb.resolve_driver("microvm") is None


def test_evaluate_cell_confirms_a_well_formed_report():
    evidence, err = _sb.evaluate_cell("p", _GOOD)
    assert err is None
    assert any("0 imports" in ln for ln in evidence)
    assert any("ungranted host import fails at instantiation" in ln for ln in evidence)
    assert any("21.0.0" in ln for ln in evidence)


def test_evaluate_cell_refuses_a_cell_that_links_an_ungranted_import():
    # the defining property did NOT hold: an ungranted host import instantiated.
    evidence, err = _sb.evaluate_cell("p", {**_GOOD, "AMBIENT": "linked"})
    assert evidence == []
    assert "ambient authority" in err and "refused" in err


def test_evaluate_cell_refuses_an_unreported_ambient_probe():
    evidence, err = _sb.evaluate_cell("p", {**_GOOD, "AMBIENT": "unreported"})
    assert evidence == [] and "unconfirmed" in err


def test_evaluate_cell_refuses_nonzero_imports_on_the_empty_module():
    evidence, err = _sb.evaluate_cell("p", {**_GOOD, "IMPORTS": "1"})
    assert evidence == [] and "ambient authority" in err


def test_evaluate_cell_refuses_when_wasmtime_did_not_import():
    evidence, err = _sb.evaluate_cell("p", {"WTIMPORT": "absent:ImportError",
                                            "CELL": "done"})
    assert evidence == [] and "wasmtime" in err and "downgraded" in err


def test_evaluate_cell_refuses_a_canary_that_did_not_complete():
    evidence, err = _sb.evaluate_cell("p", {"IMPORTS": "0"})  # no CELL=done
    assert evidence == [] and "did not complete" in err


def _driver(*, reason=None, report=None):
    """A driver with the substrate hooks injected, so the whole preflight is
    exercised with no wasmtime present."""
    return _sb.WasmCellDriver(
        runtime_reason=lambda: reason,
        probe=(lambda pname: (report, None)) if report is not None else None)


def test_preflight_refuses_when_the_substrate_is_absent():
    driver = _driver(reason="the cordis-wasm runtime was not found")
    achieved, err = driver.preflight("Web", {"fs": [], "net": "none"},
                                     {"backend": "py"})
    assert achieved is None
    assert "wasm substrate is not available" in err
    assert "never downgraded to an unconfined process" in err


def test_preflight_refuses_a_non_py_backend():
    driver = _driver(reason=None, report=_GOOD)
    achieved, err = driver.preflight("Web", {"fs": [], "net": "none"},
                                     {"backend": "rust"})
    assert achieved is None and "hosted inside a py placement process" in err


def test_preflight_refuses_a_stray_os_envelope_defensively():
    driver = _driver(reason=None, report=_GOOD)
    achieved, err = driver.preflight("Web", {"fs": ["/data:rw"], "net": "none"},
                                     {"backend": "py"})
    assert achieved is None and "grants nothing through an" in err


def test_preflight_refuses_an_unconfirmed_cell():
    driver = _driver(reason=None, report={**_GOOD, "AMBIENT": "linked"})
    achieved, err = driver.preflight("Web", {"fs": [], "net": "none"},
                                     {"backend": "py"})
    assert achieved is None and "ambient authority" in err


def test_preflight_verifies_the_substrate_then_refuses_naming_stage_4():
    """The headline of the slice: a confirmed cell substrate is not yet a hosted
    component, so the launch refuses — carrying the in-cell evidence so the
    progress is auditable, and naming Stage 4 and the container rung."""
    driver = _driver(reason=None, report=_GOOD)
    achieved, err = driver.preflight("Web", {"fs": [], "net": "none"},
                                     {"backend": "py"})
    assert achieved is None
    # the verified-substrate evidence rides along in the refusal
    assert "substrate is established and verified in-cell" in err
    assert "0 imports" in err
    # and it names the one unbuilt step + the alternative
    assert "Stage 4" in err and "cell mode" in err
    assert "`container` rung" in err
    assert "nothing confines" in err


def test_wrap_is_unreachable_and_fails_loudly():
    # preflight refuses before a command is built; if a launch is ever reached
    # without the runner's cell mode it must blow up, not run unconfined.
    driver = _sb.WasmCellDriver()
    with pytest.raises(AssertionError, match="unconfined"):
        driver.wrap("Web", ["python3", "x"], None, {})


def test_teardown_is_idempotent_and_clears_bookkeeping():
    driver = _sb.WasmCellDriver()
    driver._cells["Web"] = "cell-1"
    driver.teardown("Web")
    assert "Web" not in driver._cells
    driver._cells["A"] = "cell-a"
    driver.teardown()  # all
    assert driver._cells == {}
    driver.teardown()  # again, nothing to do


def test_audit_names_the_wasm_cell_driver(tmp_path):
    from revl import compile_files
    app = tmp_path / "app.rvl"
    app.write_text(_APP, encoding="utf-8")
    ir = compile_files([str(app)])
    lines, err = _placement.sandbox_audit_view(
        ir, {"default_tier": "py",
             "sandbox": {"Lonely": {"isolation": "wasm-cell"}}})
    assert err is None
    assert any("rung wasm-cell has a runtime driver" in ln for ln in lines)


def test_placement_refuses_a_wasm_cell_and_spawns_nothing(tmp_path, capsys):
    """End to end through `run_placement`: with the substrate injected as present
    and confirmed, a wasm-cell placement still refuses (Stage 4 unbuilt) and
    spawns nothing — never a body booted unconfined in-process."""
    app = tmp_path / "app.rvl"
    app.write_text(_APP, encoding="utf-8")
    toml = tmp_path / "p.toml"
    toml.write_text('default_tier = "py"\n[sandbox]\n'
                    'Lonely = { isolation = "wasm-cell" }\n', encoding="utf-8")
    spawned = []

    def spy(cmd, **kw):  # pragma: no cover - asserted never to run
        spawned.append(cmd)
        raise AssertionError("a wasm-cell placement must not spawn a body")

    fake = _sb.WasmCellDriver(runtime_reason=lambda: None,
                              probe=lambda pname: (_GOOD, None))
    with mock.patch.object(_placement, "resolve_sandbox_driver",
                           lambda rung: fake if rung == "wasm-cell" else None), \
         mock.patch.object(_placement, "_cordis_py_installed", lambda: True), \
         mock.patch.object(_placement, "_preflight", lambda *a, **k: None), \
         mock.patch.object(_placement.subprocess, "Popen", spy):
        rc = _placement.run_placement([str(app)], str(toml), once=True)
    assert rc == 1
    assert spawned == []
    err = capsys.readouterr().err
    assert "sandbox refused (item 411)" in err
    assert "Stage 4" in err


# ==========================================================================
# 2. against a real wasm substrate (gated: run_wasm.wasm_runtime_reason())
# ==========================================================================
#
# This is a TOOLCHAIN probe, not an env-var gate, and deliberately reuses the
# wasm tier's own availability check: the cell is a wasmtime instance and the
# only interpreter that carries wasmtime is the cordis-wasm venv. So it runs
# exactly where the wasm tier runs (the `backend-wasm` job's pinned checkout, or
# a developer with cordis-wasm) and skips everywhere else — which is honest
# rather than hidden, because a skipped level here is stated, and the pure judge
# above (`evaluate_cell`) has already been driven against every report shape
# this level can produce.

_WASM_REASON = wasm_runtime_reason()


@pytest.mark.skipif(_WASM_REASON is not None,
                    reason=f"wasm substrate unavailable: {_WASM_REASON}")
def test_live_cell_canary_confirms_confinement():
    driver = _sb.WasmCellDriver()
    report, err = driver._run_probe("Web")
    assert err is None, err
    evidence, verr = _sb.evaluate_cell("Web", report)
    assert verr is None, verr
    assert report["IMPORTS"] == "0"
    assert report["AMBIENT"].startswith("blocked:")


@pytest.mark.skipif(_WASM_REASON is not None,
                    reason=f"wasm substrate unavailable: {_WASM_REASON}")
def test_live_preflight_verifies_then_refuses_naming_stage_4():
    driver = _sb.WasmCellDriver()
    achieved, err = driver.preflight("Web", {"fs": [], "net": "none"},
                                     {"backend": "py"})
    assert achieved is None
    assert "substrate is established and verified in-cell" in err
    assert "Stage 4" in err
