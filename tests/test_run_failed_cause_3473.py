"""A fiber that settles in FAILED prints its cause on stderr (issue #3473).

`revl run` used to show only the two transitions (`LOADING -> UNLOADING ->
FAILED`) and print nothing about why: no exception name, no message, on
stdout or stderr. The cause existed at the moment the line was printed —
the fiber carries it as `_error` — so a red run now says it on one stderr
line naming the component, and the full traceback rides the causal trace
behind `--trace`.

Each case runs the issue's composition (a host `@py` body that raises)
through the real CLI. #3436 (the nonzero exit) is a separate defect.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="a run needs a live cordis-py runtime; install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)

#: the issue's reproduction: the @py body spells `true`, a Python NameError
FRICTION_NAME = """
pub extern emission[store] fn broken() -> Result[Bool, Str]
  = @py {
    return Ok(true)
  }
  = @ts { return { kind: "Ok", value: true } }
service Run { emission fn once() -> Result[Bool, Str] }
component R provides run: Run {
  provide run {
    fn once() { return emit broken() }
  }
}
component Once requires run: Run { emit run.once() }
"""

#: same shape, a different exception type and message
FRICTION_VALUE = """
pub extern emission[store] fn broken() -> Result[Bool, Str]
  = @py {
    raise ValueError("disk on fire")
  }
service Run { emission fn once() -> Result[Bool, Str] }
component R provides run: Run {
  provide run {
    fn once() { return emit broken() }
  }
}
component Once requires run: Run { emit run.once() }
"""


def _run(tmp_path: Path, source: str, *extra: str, once: bool = True) -> subprocess.CompletedProcess:
    path = tmp_path / "friction.rvl"
    path.write_text(source, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src")] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    args = [sys.executable, "-m", "revl", "run", str(path)]
    if once:
        args.append("--once")
    args.extend(extra)
    return subprocess.run(args, cwd=str(tmp_path), env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=180)


@needs_cordis
def test_a_failed_fiber_prints_its_cause_on_stderr(tmp_path):
    proc = _run(tmp_path, FRICTION_NAME)
    assert "UNLOADING -> FAILED" in proc.stdout, proc.stdout
    assert "component `Once` FAILED" in proc.stderr, proc.stderr
    assert "NameError" in proc.stderr, proc.stderr


@needs_cordis
def test_a_failed_fiber_names_the_raised_exception_type_and_message(tmp_path):
    proc = _run(tmp_path, FRICTION_VALUE)
    assert "ValueError" in proc.stderr, proc.stderr
    assert "disk on fire" in proc.stderr, proc.stderr


@needs_cordis
def test_the_cause_line_is_present_without_once_too(tmp_path):
    # stdin is closed, so the REPL exits at once; the same stderr line
    proc = _run(tmp_path, FRICTION_NAME, once=False)
    assert "component `Once` FAILED" in proc.stderr, proc.stderr
    assert "NameError" in proc.stderr, proc.stderr


@needs_cordis
def test_the_trace_carries_the_full_traceback_behind_the_flag(tmp_path):
    trace = tmp_path / "trace.jsonl"
    proc = _run(tmp_path, FRICTION_NAME, "--trace", str(trace))
    assert "NameError" in proc.stderr, proc.stderr
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines() if line.strip()]
    failed = [r for r in records if r.get("event") == "fiber-failed"]
    assert len(failed) == 1, records
    assert failed[0]["component"] == "Once"
    assert "NameError" in failed[0]["cause"]["detail"]
    assert "NameError" in failed[0]["traceback"]
