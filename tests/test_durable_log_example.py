"""examples/durable_log.rvl — the acquire/undo discipline, pinned (item 83).

The example is the worked answer to roadmap item 83(a): a durable host
resource with state (a real file handle / fd) acquired, used, and released,
with the fd threaded through the component effect's `undo` so the release is
revertible with the actual handle in scope. The doc that walks through it is
docs/syntax-2.0.md §6 ("acquire and the two undos").

Two checks:

  * it COMPILES, and the IR shows the two distinct undos — the extern's
    documentary `log_close(1)` (a literal placeholder; G4 requires an
    `acquire` to carry an undo, but its teardown cannot see the real fd) and
    the component effect's real `log_close(fd)` (the acquired handle threaded
    through). This runs in the normal suite, no runtime needed.

  * it RUNS on the cordis-py runtime when that runtime is installed — the
    `lifecycle test` opens the log, records a line, unloads, and asserts
    `no_residue` (the fd was really closed). Skipped, with a reason, when the
    runtime is absent (`sh backends/python/setup.sh`).
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

EXAMPLE = ROOT / "examples" / "durable_log.rvl"
CORDIS_PY = ROOT / "backends" / "python" / ".venv" / "bin" / "python"


def _find(steps, step):
    return [s for s in steps if s.get("step") == step]


def test_durable_log_compiles():
    ir = compile_files([str(EXAMPLE)])
    names = {e["name"] for e in ir["externs"]}
    assert {"log_open", "log_write", "log_close"} <= names


def test_extern_undo_names_the_inverse_over_the_result_handle():
    """The extern's `undo log_close(result)` names the inverse over the implicit
    `result` binding — the nominal `LogHandle` the acquire returned. Item 308 R0
    requires the acquire return to be a nominal opaque handle (not a bare `Int`),
    so the descriptor threads through the undo by its handle, not a literal."""
    ir = compile_files([str(EXAMPLE)])
    log_open = next(e for e in ir["externs"] if e["name"] == "log_open")
    assert log_open["class"] == "acquire"
    assert log_open["returns"] == "LogHandle"
    undo = log_open["undo"]
    assert undo["callee"]["name"] == "log_close"
    assert undo["args"] == [{"kind": "var", "name": "result"}]


def test_component_effect_threads_the_real_fd_through_its_undo():
    """The real revertible release: `let fd = effect log_open(...) undo
    log_close(fd)` closes the descriptor that was actually opened."""
    ir = compile_files([str(EXAMPLE)])
    comp = next(c for c in ir["components"] if c["name"] == "FileAuditLog")
    (acq,) = _find(comp["body"], "let-effect")
    assert acq["bind"] == "fd"
    assert acq["acquire"]["name"] == "log_open"
    assert acq["undo"]["name"] == "log_close"
    # the undo argument is the acquired binding, not a literal — this is the
    # thread that makes the release real
    assert acq["undo"]["args"] == [{"kind": "name", "id": "fd"}]


@pytest.mark.skipif(
    not CORDIS_PY.exists(),
    reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")
def test_lifecycle_reverts_cleanly_on_the_runtime():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [str(CORDIS_PY), "-m", "revl", "test", str(EXAMPLE)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS the audit log opens, records, and closes cleanly" in result.stdout
    assert "1 test(s) passed" in result.stdout
