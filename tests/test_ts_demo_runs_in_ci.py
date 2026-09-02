"""The typescript tier's acceptance demo must RUN in CI, not merely typecheck.

`backends/typescript/demo.ts` is the tier's acceptance demo: it loads the
emitted module, swaps a provider at runtime and asserts R1-R4, and its exit code
reflects the checks. The `backend-typescript` job ran `vitest run`, the emitted
typecheck script and `tsc --noEmit` -- and `tsc --noEmit` *does* cover demo.ts,
because `tsconfig.json` includes it. That is the trap: the file was type-correct
and green, and its assertions were never evaluated.

So one rotted. `cabbd931` moved the host `Map`'s absent-key answer from `null`
to `undefined` -- deliberately and correctly, because `Opt` is bare
`value | undefined` on this tier and a `null` answer made `None == None` false
through `revlEq`. That commit updated `emit.py`, `runtime.ts` and
`tests/test_lifecycle.py`. It did not update demo.ts, whose R2 "fresh store"
check still read `=== null`, an assertion that can never hold. It stayed wrong
from that day, through every green `backend-typescript` run, until somebody
typed `node demo.ts` by hand.

The python tier's demo does not have this problem, and the reason is not that
it is better written: `backend-python` runs `.venv/bin/python demo.py`. Running
it is the whole mechanism. This test is what gives the ts tier the same
property.

Note what a typecheck can and cannot buy. `tsc --noEmit` proves `demo.ts`
compiles; only executing it proves the demo still *demonstrates* anything. The
distinction is the same one `tools/validate.py` draws for the conformance
matrix, where every tier's validator stops at compile/typecheck depth and none
executes -- which is why the null/undefined answer of `Map.get` is not a claim
the conformance corpus covers either.

Like `tests/test_ts_typecheck_gate_runs_in_ci.py` and
`tests/test_java_javac_gate_runs_in_ci.py`, this is static: it needs no node and
no toolchain, so it runs in the `frontend` job with everything else in `tests/`.
A runtime probe could not police it -- the probe would live inside the very job
that might stop running the step.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
DEMO = ROOT / "backends" / "typescript" / "demo.ts"
JOB = "backend-typescript"
STEP = "node demo.ts"


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


def _run_steps(block: str) -> list[str]:
    """The job's `run:` command lines, in order."""
    return [m.strip() for m in re.findall(r"run:\s*(.+)", block)]


def test_backend_typescript_runs_the_demo() -> None:
    """The job must actually execute `node demo.ts`."""
    steps = _run_steps(_job_block(JOB))
    assert any(STEP in step for step in steps), (
        f"`{JOB}` no longer runs `{STEP}`. Typechecking demo.ts is not running "
        "it: `tsc --noEmit` already covered the file while its R2 assertion "
        "compared against `null` and could never hold. Restore the step."
    )


def test_the_demo_step_runs_after_npm_ci() -> None:
    """Ordering is load-bearing: the demo imports `cordis` from node_modules."""
    steps = _run_steps(_job_block(JOB))
    install = next(i for i, s in enumerate(steps) if "npm ci" in s)
    demo = next(i for i, s in enumerate(steps) if STEP in s)
    assert install < demo, (
        f"`{STEP}` is placed before `npm ci`; the demo imports `cordis` and "
        "would fail on a cold checkout for the wrong reason."
    )


def test_the_demo_step_is_unconditional() -> None:
    """No `if:` and no env gate -- a step that can skip is a step that can go
    quiet, which is the shape `tests/test_env_gated_skips_run_somewhere.py`
    exists to police."""
    block = _job_block(JOB)
    line = next(ln for ln in block.splitlines() if STEP in ln)
    assert "if:" not in line, f"the `{STEP}` step is conditional: {line.strip()}"
    # the step is a bare `- run:` one-liner; nothing may follow it that would
    # attach an `if:`/`env:` mapping to the same step.
    idx = block.splitlines().index(line)
    following = block.splitlines()[idx + 1: idx + 3]
    for nxt in following:
        assert not re.match(r"\s+(if|env):", nxt), (
            f"the `{STEP}` step gained a gate: {nxt.strip()}"
        )


def test_the_demo_exit_code_reflects_its_checks() -> None:
    """Running it only helps if a failed check is a non-zero exit. Without this
    the CI step would print `FAILED` and stay green -- the exact substitution of
    a report for a gate that let the R2 check rot in the first place."""
    text = DEMO.read_text(encoding="utf-8")
    assert "process.exit(failures === 0 ? 0 : 1)" in text, (
        "demo.ts no longer exits non-zero on a failed check; the CI step that "
        "runs it would report failure and pass anyway."
    )
