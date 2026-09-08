"""The operator E-Stop on the java tier — roadmap item 443, issue #122.

`docs/design/443-estop-tier-contract.md` fixes the E1-E8 contract every
honoring runtime meets identically, and records the decision that java honors
the latch like go and rust. Item 443 landed the halt on the py reference tier;
go and rust honored it next (PR #611). This suite pins the java tier honoring
it, the java analog of `backends/go/placement_runner/estop/estop_test.go` and
the rust `estop.rs` test block:

  - the latch READER reads a malformed OR unreadable latch as HALTED and an
    absent one as not-halted, byte-for-byte the rule `src/revl/estop.py::
    read_latch` applies (E1), so the tiers cannot drift on what an operator's
    armed — or corrupted — latch means;
  - the in-flight crossing REGISTRY records a crossing and clears it, so the
    inventory can name what was AMBIGUOUS when the button was hit (item 440,
    E4);
  - the halt INVENTORY / halt LINE match the merged residue schema the
    conductor's report reads (`src/revl/placement.py::_estop_halt_report`, E5).

The runtime behavior is proven by RUNNING `Estop` (compiled with the harness
`backends/java/placement/EstopChecks.java`) on a JVM, exactly as the go/rust
suites run their native code — a substring match on the emitter would prove
nothing here. The membership assertion (`java in TIERS_WITH_ESTOP`) is a plain
Python check that runs on a toolchain-free checkout too.
"""

import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import javac_gate  # noqa: E402

from revl.estop import TIERS_WITH_ESTOP  # noqa: E402

JAVAC = javac_gate.JAVAC
JAVA = javac_gate.JAVA
RELEASE = javac_gate.RELEASE
NO_JDK = javac_gate.NO_JDK

_PLACEMENT = HERE / "placement"
_ESTOP = _PLACEMENT / "Estop.java"
_CHECKS = _PLACEMENT / "EstopChecks.java"


def test_java_is_in_the_honoring_set():
    """The java tier honors the latch, so it joins `TIERS_WITH_ESTOP` alongside
    py, go and rust (the decision in docs/design/443-estop-tier-contract.md, and
    the reason the conductor carries the latch to a java child rather than
    SIGKILLing it and reporting UNKNOWN). Runs everywhere — no JDK needed."""
    assert "java" in TIERS_WITH_ESTOP
    # the tiers that already honored it are still in the set (this PR only adds)
    assert {"py", "go", "rust"}.issubset(TIERS_WITH_ESTOP)
    # wasm does NOT honor the latch itself (reported statically, a separate
    # population) and ts/node is still #769, so neither is in the set yet.
    assert "wasm" not in TIERS_WITH_ESTOP
    assert "node" not in TIERS_WITH_ESTOP


def test_the_java_estop_seam_exists_as_a_shared_class():
    """The seam vocabulary lives in one shared class both runners consult, the
    java twin of go's `estop` package. Runs everywhere."""
    assert _ESTOP.is_file(), "backends/java/placement/Estop.java must exist"
    source = _ESTOP.read_text(encoding="utf-8")
    for symbol in ("readLatch", "latchPath", "estopEngaged", "beginCrossing",
                   "endCrossing", "inFlightCrossings", "estopInventory",
                   "estopHaltLine", "publishLatch"):
        assert symbol in source, f"Estop must expose {symbol}"
    # both runners wire the seam: publish the latch, consult it, and watch it.
    for runner in ("PlacementRunner.java", "RealPlacementRunner.java"):
        rsrc = (_PLACEMENT / runner).read_text(encoding="utf-8")
        assert "Estop.publishLatch(" in rsrc, f"{runner} must publish the spec latch"
        assert "Estop.estopEngaged()" in rsrc, f"{runner} must consult the latch at its dispatch seam"
        assert "Estop.estopHaltLine(" in rsrc, f"{runner} must print the HALTED inventory line"
        assert "Runtime.getRuntime().halt(" in rsrc, f"{runner} must die where it stands (no DOWN)"
    # the accept seam is in the stub runner's serve loop.
    pr = (_PLACEMENT / "PlacementRunner.java").read_text(encoding="utf-8")
    assert 'beginCrossing(key, method, "accept")' in pr, "PlacementRunner must record accept crossings"


@pytest.mark.skipif(JAVAC is None or JAVA is None, reason=NO_JDK)
def test_java_estop_runtime_conformance(tmp_path):
    """RUN the java latch reader, registry and inventory through the same
    scenarios the go/rust native suites pin. The harness throws an AssertionError
    on any divergence and prints ESTOP_OK only when every check holds."""
    out = tmp_path / "out"
    out.mkdir()
    compiled = subprocess.run(
        [JAVAC, "--release", RELEASE, "-d", str(out), str(_ESTOP), str(_CHECKS)],
        capture_output=True, text=True, timeout=600,
    )
    assert compiled.returncode == 0, compiled.stderr
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    run = subprocess.run(
        [JAVA, "-cp", str(out), "EstopChecks", str(scratch)],
        capture_output=True, text=True, timeout=600,
    )
    assert run.returncode == 0, run.stderr + run.stdout
    assert "ESTOP_OK" in run.stdout, run.stdout + run.stderr


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
