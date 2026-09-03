"""The typescript tier's typecheck must cover EMITTED output, in CI (issue #198).

`backends/typescript/tsconfig.json` included `runtime.ts`, `demo.ts` and
`golden/**` and nothing else. The modules `scripts/emit-fixtures.ts` writes into
`tests/generated/` — the ones the vitest suites import and execute — were
compiled by nobody. For a compiler backend that is most of what the target
language buys us: the emitter can produce type-incorrect TypeScript and stay
green as long as the code happens to RUN. Widening it found 20 real type errors
across four emitted modules on the first run.

Issue #223 closed the other half of the same gap: the tier's HAND-WRITTEN
TypeScript — the vitest suites, `bridge.ts`, `revl_fs_ts.ts`,
`placement_runner.ts` — was in no tsconfig either, and probing it found 9 more
real errors. `scripts/typecheck-handwritten.mjs` covers it, with a coverage
guard that fails if any `.ts` in the tier is matched by no tsconfig at all.

Three halves have to stay true or the gate goes quiet again, and none can be
checked from inside the tier's own suite:

1. `backend-typescript` must actually invoke `scripts/typecheck-generated.mjs`,
   AFTER the step that writes `tests/generated/`. Ordering is load-bearing: the
   directory is gitignored, so a step placed before `vitest run` would find it
   cold. (The script fails loudly in that case rather than checking nothing —
   this test is what keeps it from being introduced at all.)
2. `tsconfig.generated.json` must keep matching `tests/generated/`, and the
   script must keep cross-checking its file list against the fixture list. That
   cross-check is what makes "0 modules checked, all good" impossible; without
   it, one gitignored directory rename returns the tier to where it started.
3. `backend-typescript` must also invoke `scripts/typecheck-handwritten.mjs`,
   after the same step, and that script must keep asking whether every `.ts` in
   the tier is covered by SOME tsconfig. Dropping the coverage question is how
   #223 happened the first time: two configs that each looked complete, and a
   third of the tier's TypeScript between them.

Like `tests/test_java_javac_gate_runs_in_ci.py`, this is static: it needs no
node and no toolchain, so it runs in the `frontend` job with everything else in
`tests/`. A runtime probe could not police it — the probe would live inside the
very job that might stop running the step.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
TS = ROOT / "backends" / "typescript"
JOB = "backend-typescript"
GATE = "scripts/typecheck-generated.mjs"
HAND_GATE = "scripts/typecheck-handwritten.mjs"


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


def _fixture_outputs() -> list[str]:
    """The `.ts` filenames `scripts/emit-fixtures.ts` writes into
    `tests/generated/`. Matched on the CALL shape, not on a function name, so
    the aliased calls the fixture list uses (`emitRouterModule(...)` and
    friends, which exist to keep a module off `generated_coverage.test.ts`'s
    scan) are counted too."""
    src = (TS / "scripts" / "emit-fixtures.ts").read_text(encoding="utf-8")
    pairs = re.findall(r"\(\s*'([^']+\.ir\.json)'\s*,\s*'([^']+\.ts)'\s*\)", src)
    return sorted({out for _, out in pairs})


# --- anti-vacuity ---------------------------------------------------------- #
def test_the_gate_and_its_config_exist():
    """If either half is deleted, say so here rather than passing vacuously
    below. Re-derive this file against whatever replaced them."""
    assert (TS / GATE).is_file(), f"backends/typescript/{GATE} is gone"
    assert (TS / "tsconfig.generated.json").is_file(), (
        "backends/typescript/tsconfig.generated.json is gone; the emitted "
        "modules are back to being typechecked by nobody (issue #198)."
    )
    assert (TS / HAND_GATE).is_file(), f"backends/typescript/{HAND_GATE} is gone"
    assert (TS / "tsconfig.handwritten.json").is_file(), (
        "backends/typescript/tsconfig.handwritten.json is gone; the tier's "
        "hand-written TypeScript is back to being typechecked by nobody "
        "(issue #223)."
    )


def test_the_fixture_list_is_not_empty():
    """Everything below is a claim about a set of emitted modules. An empty set
    would make all of it true and none of it useful."""
    assert len(_fixture_outputs()) >= 10, (
        "scripts/emit-fixtures.ts emits fewer modules than expected — either "
        "the list shrank drastically or its call shape changed and this "
        "regex now sees nothing."
    )


# --- half 1: the step runs in CI, in the right order ----------------------- #
def test_backend_typescript_runs_the_generated_typecheck():
    steps = _run_steps(_job_block(JOB))
    assert any(GATE in s for s in steps), (
        f"the `{JOB}` job does not run {GATE}. Emitted TypeScript under "
        "tests/generated/ is executed by vitest but typechecked by nobody — "
        "that is issue #198, reopened."
    )


def test_the_generated_typecheck_runs_after_the_fixtures_are_emitted():
    steps = _run_steps(_job_block(JOB))
    gate_at = next(i for i, s in enumerate(steps) if GATE in s)
    emit_at = next(
        (i for i, s in enumerate(steps) if "vitest run" in s),
        None,
    )
    assert emit_at is not None, (
        f"the `{JOB}` job no longer runs `vitest run`, which is what writes "
        "tests/generated/ (scripts/emit-fixtures.ts runs at vitest config "
        "load). Whatever populates that directory now must run before the "
        "typecheck step."
    )
    assert emit_at < gate_at, (
        "the generated-output typecheck runs BEFORE the step that emits "
        "tests/generated/. That directory is gitignored, so on a fresh runner "
        "the gate would find it cold."
    )


def test_the_plain_tsc_project_check_still_runs():
    """Widening to emitted output must not cost the check that was already
    there (`runtime.ts` and the `demo.ts` that drives it)."""
    steps = _run_steps(_job_block(JOB))
    assert any("tsc --noEmit" in s for s in steps), (
        f"the `{JOB}` job no longer runs `tsc --noEmit`"
    )


# --- half 3: the hand-written half runs too, in the right order (#223) ------ #
def test_backend_typescript_runs_the_hand_written_typecheck():
    steps = _run_steps(_job_block(JOB))
    assert any(HAND_GATE in s for s in steps), (
        f"the `{JOB}` job does not run {HAND_GATE}. The vitest suites, "
        "bridge.ts and revl_fs_ts.ts are executed but typechecked by "
        "nobody — that is issue #223, reopened."
    )


def test_the_hand_written_typecheck_runs_after_the_fixtures_are_emitted():
    """Same ordering constraint as the emitted half, for a different reason:
    the suites IMPORT `tests/generated/`, so with that directory cold every one
    of them fails to resolve."""
    steps = _run_steps(_job_block(JOB))
    gate_at = next(i for i, s in enumerate(steps) if HAND_GATE in s)
    emit_at = next((i for i, s in enumerate(steps) if "vitest run" in s), None)
    assert emit_at is not None and emit_at < gate_at, (
        "the hand-written typecheck runs BEFORE the step that emits "
        "tests/generated/, which the suites import."
    )


def test_the_hand_written_gate_asks_whether_every_file_is_covered():
    """The guard that makes this gate hard to reopen: it is not enough for the
    listed files to typecheck, every `.ts` in the tier has to be matched by
    SOME tsconfig. Delete that question and a new file can go uncovered exactly
    the way the suites did."""
    src = (TS / HAND_GATE).read_text(encoding="utf-8")
    assert "tsconfig.handwritten.json" in src, (
        f"{HAND_GATE} no longer reads tsconfig.handwritten.json"
    )
    for config in ("tsconfig.json", "tsconfig.generated.json"):
        assert config in src, (
            f"{HAND_GATE} no longer counts {config} towards coverage, so the "
            "files it owns now read as covered by nobody — or, worse, the "
            "coverage guard was dropped and nothing asks at all (issue #223)."
        )


def test_no_typescript_file_in_the_tier_is_covered_by_no_tsconfig():
    """The static twin of the gate's own coverage guard, so the claim holds in
    the `frontend` job too — with no node, and on a cold checkout where
    `tests/generated/` does not exist and the gate cannot run at all.

    Reads the `include` globs out of the three configs and matches them against
    the tier's committed `.ts` files. Deliberately cruder than the real guard:
    it exists to notice a file added to a directory no config mentions."""
    import fnmatch

    includes: list[str] = []
    for name in ("tsconfig.json", "tsconfig.generated.json",
                 "tsconfig.handwritten.json"):
        text = (TS / name).read_text(encoding="utf-8")
        body = re.search(r'"include"\s*:\s*\[(.*?)\]', text, re.S)
        assert body, f"{name} has no include array"
        includes += re.findall(r'"([^"]+)"', body.group(1))
    assert len(includes) >= 4, "the three configs between them include almost nothing"

    def covered(rel: str) -> bool:
        for pattern in includes:
            if pattern == rel:
                return True
            # tsconfig treats `dir/**/*.ts` as "at any depth" and `dir/*.ts` as
            # "this level only"; fnmatch's `*` crosses `/`, so anchor the
            # single-star form on the segment count.
            if "**" in pattern:
                if fnmatch.fnmatch(rel, pattern.replace("**/", "*")):
                    return True
            elif fnmatch.fnmatch(rel, pattern) and rel.count("/") == pattern.count("/"):
                return True
        return False

    tracked = [
        p.relative_to(TS).as_posix()
        for p in TS.rglob("*.ts")
        if "node_modules" not in p.parts and "tests/generated/" not in
        p.relative_to(TS).as_posix()
    ]
    assert len(tracked) >= 40, "found suspiciously few .ts files to check"
    uncovered = sorted(f for f in tracked if not covered(f))
    assert not uncovered, (
        "these TypeScript files are matched by no tsconfig include, so nothing "
        f"typechecks them: {uncovered}. That is issue #223 — add each to the "
        "config that should own it."
    )


# --- half 2: the gate's coverage cannot go vacuous ------------------------- #
def test_the_generated_config_targets_the_emitted_directory():
    text = (TS / "tsconfig.generated.json").read_text(encoding="utf-8")
    assert "tests/generated" in text, (
        "tsconfig.generated.json no longer mentions tests/generated/; it is "
        "the directory the emitted modules land in."
    )


def test_the_gate_cross_checks_the_fixture_list():
    """The script must keep reading `scripts/emit-fixtures.ts` and comparing it
    against what it is about to check. Drop that and a directory rename turns
    the whole gate into a silent no-op."""
    src = (TS / GATE).read_text(encoding="utf-8")
    assert "emit-fixtures.ts" in src, (
        f"{GATE} no longer cross-checks scripts/emit-fixtures.ts. Without "
        "that, checking zero modules reports success — the exact failure mode "
        "issue #198 is about."
    )
    assert "tsconfig.generated.json" in src, (
        f"{GATE} no longer reads tsconfig.generated.json, so the compiler "
        "options it checks with are no longer the ones that file documents."
    )


def test_every_emitted_module_lands_under_the_checked_directory():
    """`tests/generated/` is gitignored, so this cannot look at the files. What
    it can check is that the emitter writes them where the gate looks: a
    fixture routed anywhere else would be executed by vitest and typechecked by
    nobody, one file at a time."""
    src = (TS / "scripts" / "emit-fixtures.ts").read_text(encoding="utf-8")
    assert re.search(r"'tests',\s*'generated'", src), (
        "scripts/emit-fixtures.ts no longer writes into tests/generated/. "
        "Point tsconfig.generated.json's include at wherever it writes now."
    )
