"""The operator E-Stop on the wasm tier — roadmap item 443, issue #122.

The wasm tier is single-process and REFUSED from placement (there is no wasm
placement runner, bridge client, or stub — `src/revl/placement.py` turns a
`backend = "wasm"` process away with a redirect), so it has no conductor and no
cross-process crossing seam. Its E-Stop is therefore a SINGLE-PROCESS seam,
honored exactly where the py reference runtime honors it
(`backends/python/runtime.py::_estop_check`, called from `plug`): an activation
is a fresh batch of boundary crossings, so once an operator arms the latch the
harness refuses to START the next activation, prints its in-flight inventory on
one line, and dies with NO teardown — no LIFO unplug, no compiled inverses
replayed, no no-residue proof, no `DOWN` (docs/design/443-estop.md).

Two layers of proof:

  * the latch reader and inventory line are unit-pinned in-process (the harness
    is revl-free at module level, so importing it needs no cordis-wasm);
  * the seam itself is proven BY EXECUTION on the real cordis-wasm runtime — an
    armed latch halts the bring-up and never reaches `DOWN`, while an UNARMED
    run is byte-identical to the pre-443 harness. Without the runtime (or the
    wasmtime package) the execution layer skips with a reason, never a feint at
    passing.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _harness_module():
    """Load `run_harness.py` by path for the in-process unit layer. Its
    module-level code is json + stdlib only (the cordis-wasm runtime is imported
    inside `main`), so this needs no wasmtime."""
    path = BACKEND / "run_harness.py"
    spec = importlib.util.spec_from_file_location("revl_wasm_run_harness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HARNESS = _harness_module()


# ---------------------------------------------------------------------------
# the latch reader and the inventory line (in-process, no runtime needed)
# ---------------------------------------------------------------------------


def test_absent_latch_reads_as_not_halted(tmp_path):
    assert HARNESS._read_latch(None) is None
    assert HARNESS._read_latch(str(tmp_path / "nope.estop")) is None


def test_an_armed_latch_reads_as_the_operator_record(tmp_path):
    latch = tmp_path / "halt.estop"
    latch.write_text(json.dumps({"reason": "runaway loop", "operator": "ops@x"}),
                     encoding="utf-8")
    record = HARNESS._read_latch(str(latch))
    assert record["reason"] == "runaway loop"
    assert record["operator"] == "ops@x"


def test_a_malformed_latch_still_halts_fail_closed(tmp_path):
    """Failing open on an emergency stop is the one failure mode this feature
    exists to prevent, so a latch that EXISTS but does not parse still HALTS —
    the same rule as `revl.estop.read_latch` and the runtime seam."""
    latch = tmp_path / "halt.estop"
    latch.write_text("{not json at all", encoding="utf-8")
    record = HARNESS._read_latch(str(latch))
    assert record is not None
    assert "unreadable latch" in record["reason"]
    # a non-object JSON value (a bare list) is unreadable too
    latch.write_text("[1, 2, 3]", encoding="utf-8")
    assert HARNESS._read_latch(str(latch)) == HARNESS._unreadable()


def test_latch_path_prefers_the_spec_then_the_env(tmp_path, monkeypatch):
    monkeypatch.delenv("REVL_ESTOP_LATCH", raising=False)
    assert HARNESS._estop_latch_path({}) is None
    monkeypatch.setenv("REVL_ESTOP_LATCH", "/from/env")
    assert HARNESS._estop_latch_path({}) == "/from/env"
    # the spec wins over the ambient variable
    assert HARNESS._estop_latch_path({"estopLatch": "/from/spec"}) == "/from/spec"


def test_the_halted_line_names_active_components_as_stranded(capsys):
    """A single process crosses no seam, so nothing is AMBIGUOUS; every
    component already ACTIVE when the button lands is STRANDED, because the halt
    runs none of their inverses."""
    HARNESS._emit_halt("run", {"reason": "runaway loop", "operator": "ops@x"},
                       loaded=["Alpha", "Beta"])
    line = capsys.readouterr().out.strip()
    assert line.startswith("[run] HALTED ")
    inv = json.loads(line.split(" ", 2)[2])
    assert inv["verdict"] == "halted"
    assert inv["reason"] == "runaway loop"
    assert inv["resumable"] is False
    assert inv["inFlight"] == []
    stranded = {e["component"]: e for e in inv["stranded"]}
    assert set(stranded) == {"Alpha", "Beta"}
    assert all(e["kind"] == "estop-stranded" for e in inv["stranded"])
    assert all(e["outcome"] == "not-attempted" for e in inv["stranded"])


# ---------------------------------------------------------------------------
# the seam itself, proven by execution on the real cordis-wasm runtime
# ---------------------------------------------------------------------------

APP = """
service S { fn f() -> Int }
component C provides s: S {
  provide s { fn f() = 1 }
}
"""


def _cordis_wasm_python() -> str:
    """The wasmtime-bearing interpreter that runs the harness, or skip."""
    override = os.environ.get("REVL_CORDIS_WASM_PYTHON")
    if override and Path(override).exists():
        return override
    root = os.environ.get("CORDIS_WASM") or str(Path.home() / "Projects" / "cordis-wasm")
    venv = Path(root) / ".venv" / "bin" / "python"
    if not venv.exists():
        pytest.skip(f"cordis-wasm interpreter not found at {venv} "
                    "(set REVL_CORDIS_WASM_PYTHON or CORDIS_WASM)")
    probe = subprocess.run(
        [str(venv), "-c", "import wasmtime, sys; "
         "sys.path.insert(0, __import__('os').environ.get('CORDIS_WASM', "
         f"{str(Path(root))!r})); import runtime"],
        capture_output=True, text=True, env={**os.environ, "CORDIS_WASM": str(root)})
    if probe.returncode != 0:
        pytest.skip(f"cordis-wasm runtime not importable: "
                    f"{(probe.stderr or '').strip().splitlines()[-1:] or '?'}")
    return str(venv)


def _spec_file(tmp_path, *, estop_latch: str | None) -> Path:
    """Compile+emit APP and write a harness spec, optionally armed."""
    from revl import compile_source  # noqa: PLC0415
    from revl.run_wasm import _emit_modules, _load_order  # noqa: PLC0415

    ir = compile_source(APP)
    modules = _emit_modules(ir)
    order = _load_order(ir)
    run_spec = {"name": "run", "once": True, "record": False,
                "order": order, "modules": {n: modules[n] for n in order}}
    if estop_latch is not None:
        run_spec["estopLatch"] = estop_latch
    spec_file = tmp_path / "run.spec.json"
    spec_file.write_text(json.dumps(run_spec), encoding="utf-8")
    return spec_file


def _run_harness(tmp_path, *, estop_latch: str | None):
    python = _cordis_wasm_python()
    spec_file = _spec_file(tmp_path, estop_latch=estop_latch)
    root = os.environ.get("CORDIS_WASM") or str(Path.home() / "Projects" / "cordis-wasm")
    proc = subprocess.run(
        [python, str(BACKEND / "run_harness.py"), str(spec_file)],
        capture_output=True, text=True,
        env={**os.environ, "CORDIS_WASM": str(root)})
    return proc


def test_an_armed_latch_halts_the_bring_up_with_no_teardown(tmp_path):
    """The headline, by execution. An armed latch means the harness refuses to
    start an activation: it prints HALTED and dies where it stands, so the
    graceful `DOWN` / `NO-RESIDUE` round-trip NEVER runs."""
    latch = tmp_path / "halt.estop"
    latch.write_text(json.dumps({"halted": True, "reason": "runaway loop",
                                 "operator": "ops@example"}), encoding="utf-8")
    proc = _run_harness(tmp_path, estop_latch=str(latch))
    out = proc.stdout
    assert "[run] HALTED " in out, out + "\n---\n" + proc.stderr
    # a halt is not a teardown: none of the graceful round-trip's proofs appear
    assert "DOWN" not in out
    assert "NO-RESIDUE" not in out
    # os._exit(75), the no-teardown exit
    assert proc.returncode == HARNESS._ESTOP_EXIT
    # the operator's own words rode the inventory line
    inv = json.loads([ln for ln in out.splitlines()
                      if ln.startswith("[run] HALTED ")][0].split(" ", 2)[2])
    assert inv["reason"] == "runaway loop"
    assert inv["operator"] == "ops@example"
    assert inv["resumable"] is False


def test_an_unarmed_run_is_the_ordinary_round_trip(tmp_path):
    """UNARMED is the default: no latch key, no latch read, and the boot ->
    LIFO teardown -> no-residue proof is exactly what it was."""
    proc = _run_harness(tmp_path, estop_latch=None)
    out = proc.stdout
    assert "[run] UP" in out, out + "\n---\n" + proc.stderr
    assert "[run] NO-RESIDUE" in out
    assert "[run] DOWN" in out
    assert "HALTED" not in out
    assert proc.returncode == 0


def test_an_absent_latch_file_is_not_a_halt(tmp_path):
    """A spec that NAMES a latch which does not exist is not a halt — only a
    genuinely present file is — so the run completes normally."""
    proc = _run_harness(tmp_path, estop_latch=str(tmp_path / "never-armed.estop"))
    out = proc.stdout
    assert "[run] UP" in out, out + "\n---\n" + proc.stderr
    assert "[run] DOWN" in out
    assert "HALTED" not in out
    assert proc.returncode == 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
