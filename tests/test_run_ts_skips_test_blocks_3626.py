"""`revl run --backend ts` leaves `test` blocks out of the program it runs.

The emitted ts module imports vitest (`expect`, `it`) whenever the IR carries a
`test` block, and `it(...)` outside a vitest worker throws "Vitest failed to
find the current suite", so a composition holding a file with in-file tests
died before boot on ts while the py tier booted it (issue #3626). The run path
drops the test blocks at its emit step (`placement._emit_ts_module`); the test
path (`revl test --backend ts`) still emits and runs them.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.run_ts import ts_runtime_reason  # noqa: E402

_TS_REASON = ts_runtime_reason()
needs_node = pytest.mark.skipif(
    _TS_REASON is not None, reason=f"needs node + cordis-ts: {_TS_REASON}")
needs_vitest = pytest.mark.skipif(
    not (ROOT / "backends" / "typescript" / "node_modules" / ".bin" / "vitest").exists(),
    reason="vitest not installed in backends/typescript")

SOURCE = """\
extern emission fn say(text: Str) -> Str
  = @py {
print(text)
return "ok"
  }
  = @ts {
console.log(text)
return "ok"
  }

fn twice(n: Int) -> Int {
  return n * 2
}

test "twice doubles" {
  assert twice(2) == 4
}

component Hello {
  emit say("hello")
}
"""


def _cli(verb: str, args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run([sys.executable, "-m", "revl", verb, *args],
                          capture_output=True, text=True, env=env, check=False)


@pytest.fixture
def source(tmp_path) -> str:
    path = tmp_path / "a.rvl"
    path.write_text(SOURCE, encoding="utf-8")
    return str(path)


@needs_node
def test_run_ts_boots_a_file_that_holds_a_test_block(source):
    result = _cli("run", ["--once", "--backend", "ts", source])
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "hello" in result.stdout
    assert "Vitest failed" not in out


@needs_vitest
def test_test_ts_still_runs_the_test_block(source):
    result = _cli("test", ["--backend", "ts", source])
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "1 test(s) passed" in out
