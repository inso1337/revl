"""A placement probe's Int argument reaches the rust tier as an Int (issue #1559).

On main the rust-side probe path sent every argument as a string. Measured
on main with `ops.run(n) = s.bump(n) * 10` and `s.bump(n) = n + 1`, both
components on rust in two processes, the probe `ops.run(41)` answered
10. It answers 420 now: `placement._parse_probe` types each argument
by the parameter type the operation declares.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

SOURCE = """\
service S { fn bump(x: Int) -> Int }
component P provides s: S { provide s { fn bump(x) { return x + 1 } } }
service Ops { fn run(x: Int) -> Int }
component C requires s: S provides ops: Ops {
  provide ops { fn run(x) { return s.bump(x) * 10 } }
}
"""

PLACEMENT = """\
[processes.provider]
backend = "rust"
components = ["P"]

[processes.consumer]
backend = "rust"
components = ["C"]
probe = ["ops.run(41)", "s.bump(41)"]
"""


needs_cargo = pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo is not installed")
ENV = dict(os.environ)


@needs_cargo
def test_an_int_probe_argument_crosses_as_an_int(tmp_path):
    (tmp_path / "probe.rvl").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "probe.toml").write_text(PLACEMENT, encoding="utf-8")
    ran = subprocess.run(
        [sys.executable, "-m", "revl", "run", str(tmp_path / "probe.rvl"),
         "--placement", str(tmp_path / "probe.toml"), "--once"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=1800,
        cwd=ROOT, env=ENV)
    out = ran.stdout + ran.stderr
    assert ran.returncode == 0, out
    probes = [line for line in out.splitlines() if "] probe" in line]
    assert len(probes) == 2, out
    assert "-> 420" in probes[0], out    # ops.run(41), across the seam
    assert probes[1].rstrip().endswith("-> 42"), out   # s.bump(41)
