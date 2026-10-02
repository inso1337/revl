"""A placement probe's Int argument reaches the java tier as an Int (issue #1559).

The java runner parses the probe text itself, so it was never on the
stringly-typed path the go and rust runners shared. This is the guard that
keeps it off it: with `ops.run(n) = s.bump(n) * 10` and `s.bump(n) = n + 1`,
both components on java in two processes, the probe `ops.run(41)` answers 420
on main and here.
"""

from __future__ import annotations

import os
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
backend = "java"
components = ["P"]

[processes.consumer]
backend = "java"
components = ["C"]
probe = ["ops.run(41)", "s.bump(41)"]
"""


sys.path.insert(0, str(Path(__file__).resolve().parent))
import javac_gate  # noqa: E402

needs_jdk = pytest.mark.skipif(javac_gate.JAVAC is None, reason=javac_gate.NO_JDK)
# The in-repo stub runner, even where real cordis4j classes are present (CI):
# `revl run --placement` picks RealPlacementRunner then, and that runner is
# consumer-only. It serves no key, so a java provider process answers nothing
# and the consumer's probe fails on a missing socket before any argument
# crosses. The typed-argument path under test is the same in both runners.
ENV = dict(os.environ, REVL_CORDIS4J_CLASSES="")
if javac_gate.JAVA:
    ENV["JAVA_HOME"] = str(Path(javac_gate.JAVA).parents[1])


@needs_jdk
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
    assert "| => 420" in probes[0], out    # ops.run(41), across the seam
    assert probes[1].rstrip().endswith("| => 42"), out   # s.bump(41)
