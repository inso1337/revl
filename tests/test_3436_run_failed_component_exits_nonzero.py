"""Issue #3436: `revl run` exits nonzero when a component ends FAILED.

WHAT WAS WRONG. A composition whose component failed at boot (or whose `undo`
raised at teardown) printed the FAILED transition and the `cause` line
(#3473) but still exited 0: `hold_repl`/`withdraw_once`/`watch` returned 0
unconditionally after teardown, and an inverse that failed to replay was only
counted in the residue proof. A script, service manager or deploy step reads
exit 0 as success and carries on.

WHAT HOLDS NOW. `_Driver` records every fiber that settles FAILED
(`_failed_fibers`, fed by `_on_fiber`) and the run's exit points
(`hold_repl` once + interactive, `withdraw_once`, `watch`) return
`_run_exit_code()`: nonzero, naming the component(s) on stderr, when any
fiber ended FAILED or any inverse failed to replay (`_compensation_residue`,
which also names the component). A clean run is byte-identical: exit 0, no
new output.

Checked here (each a real `python -m revl run --once` subprocess, py tier):
  * boot failure: the issue's `Ok(true)` host-body repro exits nonzero and
    names `Once` on stderr (`component(s) FAILED: Once`);
  * teardown failure: an `undo` host body that raises exits nonzero and names
    `Once` on stderr (`inverse(s) failed to replay on Once`);
  * clean composition: exit 0, nothing new on stderr.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the py tier needs the cordis-py runtime (backends/python/setup.sh)",
)

#: boot failure: the @py body spells `true` (a JS name), so the host body
#: raises NameError and the `Once` fiber settles FAILED while loading.
BOOT_FAIL = """pub extern emission[store] fn broken() -> Result[Bool, Str]
  = @py {
    return Ok(true)
  }
service Run { emission fn once() -> Result[Bool, Str] }
component R provides run: Run {
  provide run {
    fn once() { return emit broken() }
  }
}
component Once requires run: Run { emit run.once() }
"""

#: teardown failure: the acquire lands, the `undo` host body raises when the
#: disposer replays it, so the run ends with compensation residue on `Once`.
UNDO_FAIL = """pub extern pure fn ok() -> Int = @py { return 1 }
pub extern pure fn boom() -> Int = @py { raise RuntimeError("undo blew up") }
component Once {
  let h = effect ok() undo boom()
}
"""

#: a composition that boots and tears down cleanly must still exit 0.
CLEAN = """service Run { fn once() -> Int }
component R provides run: Run {
  provide run {
    fn once() = 1
  }
}
"""

DEADLINE_S = 60


def _run_once(tmp_path, source: str) -> subprocess.CompletedProcess:
    rvl = tmp_path / "app.rvl"
    rvl.write_text(source, encoding="utf-8")
    env = {**os.environ,
           "PYTHONPATH": os.pathsep.join(
               p for p in (str(ROOT / "src"), os.environ.get("PYTHONPATH", "")) if p)}
    return subprocess.run(
        [sys.executable, "-m", "revl", "run", "--once", str(rvl)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=DEADLINE_S, cwd=str(tmp_path), env=env)


def test_boot_failure_exits_nonzero_and_names_the_component(tmp_path):
    proc = _run_once(tmp_path, BOOT_FAIL)
    assert proc.returncode != 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "UNLOADING -> FAILED" in proc.stdout, proc.stdout
    assert "component(s) FAILED: Once" in proc.stderr, proc.stderr


def test_teardown_undo_failure_exits_nonzero_and_names_the_component(tmp_path):
    proc = _run_once(tmp_path, UNDO_FAIL)
    assert proc.returncode != 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "RESIDUE LEFT" in proc.stdout, proc.stdout
    assert "inverse(s) failed to replay on Once" in proc.stderr, proc.stderr


def test_clean_run_still_exits_zero(tmp_path):
    proc = _run_once(tmp_path, CLEAN)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "FAILED" not in proc.stderr and "error:" not in proc.stderr, proc.stderr
