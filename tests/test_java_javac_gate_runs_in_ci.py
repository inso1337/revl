"""The java tier's javac gate must EXECUTE in CI, not skip quietly (issue #154).

`backends/java/javac_gate.py` turns every emit assertion in that tier into a
claim about a program javac accepted. It does that only where a JDK is
reachable; with none it degrades to the old substring-only behaviour so a
toolchain-free checkout (the `frontend` job) still runs the suite. That
degradation is the whole risk. A skip and a pass are the same colour, which is
roadmap item 445, and it is how both of #154's uncompilable-output defects
shipped under a green suite in the first place.

A runtime probe cannot police this: on a machine with no JDK there is nothing
to assert, and on CI the assertion would live inside the very job that might
stop running the file. So the check is static, it needs no toolchain and no
third-party parser, and it runs in the `frontend` job with everything else in
`tests/`. Two conditions, both necessary:

1. the `backend-java` job provisions a JDK, so the gate is never a no-op there;
2. that job's pytest targets COLLECT every file that consumes the gate.

Condition 2 is issue #183 in miniature. Each `backend-*` job ran exactly one
named test file rather than its root, so `backends/java/test_router_emit.py`
had never executed in CI — the file carrying the assertions over the very
scenario whose emitted unit did not compile. Widening the job to the root is
what makes the gate real for that file; this test is what keeps it widened.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
JAVA_BACKEND = ROOT / "backends" / "java"
JOB = "backend-java"


def _job_block(name: str) -> str:
    """The lines of one job in ci.yml, by indentation. ci.yml is read as TEXT
    on purpose: PyYAML is not a declared dependency of this project, so a
    yaml-parsing guard would `importorskip` in exactly the environments it is
    supposed to police."""
    text = CI.read_text(encoding="utf-8")
    start = re.search(rf"^  {re.escape(name)}:\s*$", text, re.M)
    assert start, f"no `{name}` job in {CI.relative_to(ROOT)}"
    rest = text[start.end():]
    end = re.search(r"^  \S", rest, re.M)
    return rest[: end.start()] if end else rest


def _pytest_targets(block: str) -> list[str]:
    """Repo-relative paths passed to pytest by the job's `run:` steps."""
    targets: list[str] = []
    for run in re.findall(r"run:\s*(.+)", block):
        if "pytest" not in run:
            continue
        for word in run.split():
            if word.startswith("-") or word == "pytest":
                continue
            if "/" in word or word.endswith(".py"):
                targets.append(word.rstrip("/"))
    return targets


def _gate_consumers() -> list[str]:
    """Repo-relative test files under backends/java that use the javac gate."""
    found = []
    for path in sorted(JAVA_BACKEND.glob("test_*.py")):
        if "javac_gate" in path.read_text(encoding="utf-8"):
            found.append(str(path.relative_to(ROOT)))
    return found


def test_the_gate_module_and_its_consumers_exist():
    """A guard against this whole file quietly describing nothing: if the gate
    is deleted or every consumer stops importing it, say so here rather than
    passing vacuously below."""
    assert (JAVA_BACKEND / "javac_gate.py").is_file(), (
        "backends/java/javac_gate.py is gone; the checks below would pass "
        "vacuously. Re-derive this file against whatever replaced it.")
    assert _gate_consumers(), (
        "no test file under backends/ java imports javac_gate any more, so "
        "nothing compiles emitted java. That is the state issue #154 was "
        "opened about.")


def test_the_backend_java_job_provisions_a_jdk():
    """Without a JDK, `javac_gate.compile_check` is a no-op and every emit
    assertion in the tier falls back to a substring match."""
    block = _job_block(JOB)
    assert "actions/setup-java" in block, (
        f"the `{JOB}` job no longer installs a JDK. `javac_gate.compile_check` "
        "degrades to a no-op without one, so the java suite would go back to "
        "proving only that the emitter wrote the text we expected — which is "
        "what let a 6119-byte unit javac rejects ship green (issue #154).")


def test_the_backend_java_job_collects_every_file_that_uses_the_gate():
    """Issue #183: a job that runs one named file leaves the rest of the tier
    unexecuted, gate or no gate."""
    targets = _pytest_targets(_job_block(JOB))
    assert targets, f"the `{JOB}` job runs no pytest target"

    def collected(rel: str) -> bool:
        return any(rel == t or rel.startswith(t + "/") for t in targets)

    missed = [rel for rel in _gate_consumers() if not collected(rel)]
    assert not missed, (
        f"these files compile emitted java but the `{JOB}` job does not "
        f"collect them, so their javac gate executes NOWHERE in CI: {missed}. "
        f"The job runs {targets}. Point it at `backends/java/` (issue #183) "
        "rather than adding another named file each time the tier grows one."
    )
