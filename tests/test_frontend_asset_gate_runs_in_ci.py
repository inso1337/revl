"""The frontend must be BUILT and TYPECHECKED by a job (gap G5, item 459).

`tests/test_app_frontend_725.py` already carried the two legs that compile the
exemplary app's frontend: a real `vite build` whose emitted source map has to
name `entry.client.ts`, `NotesConsole.vue` and `notes.client.ts`, and `vue-tsc`
over the project's strict tsconfig. Both were gated on a cold `node_modules`,
and no workflow installed it — so on every push, on every PR and on main they
reported `skipping`, which in a job summary is indistinguishable from a pass.

That is the whole of gap G5 in docs/webapp-competitiveness-report.md, and it is
not hypothetical: a frontend that did not compile at all shipped and sat that
way. `entry.client.ts` passed a `fields` option `@cordisjs/client`'s `ctx.page`
does not accept and ran `useRpc` outside a component `setup` where its injection
is absent — invisible to every file-shape assertion in the suite, and immediately
obvious to `vue-tsc` (docs/design/525-webapp-slice5-one-command.md D1). Item
459's exit clause is "a real TS frontend behind a typed boundary ... with source
maps pointing at the original files", and a boundary is only typed once something
compiles it.

Three things have to stay true, and none of them can be asserted from inside the
job that might stop doing them:

1. A job installs the frontend's pinned tree (`npm ci` in
   `examples/app/frontend`). Without it the legs have no vite and no vue-tsc.
2. That job runs the suite with `REVL_REQUIRE_FRONTEND_TOOLCHAIN=1`, which turns
   the suite's toolchain skip OFF. This is what stops the job from being
   vacuous: with the skip in force a job could install nothing, run the suite,
   skip both legs and report green — G5 with a green check on top of it.
3. The suite keeps the skip it lifts, and keeps the legs it lifts them for.

Static, like `tests/test_ts_typecheck_gate_runs_in_ci.py` and
`tests/test_site_wheel_gate_runs_in_ci.py`: it reads the workflow as text, so it
needs no node, no PyYAML (not a declared dependency) and no frontend install, and
it rides the plain `pytest tests/ -q` in the `frontend` job.

Scope note, deliberately stated: this file asserts the job EXISTS and is
non-vacuous. Whether it is a REQUIRED check is branch-protection configuration,
which lives outside the repository and which no test here can read or claim.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
SUITE = ROOT / "tests" / "test_app_frontend_725.py"
FRONTEND = ROOT / "examples" / "app" / "frontend"
JOB = "frontend-assets"
ENV_FLAG = "REVL_REQUIRE_FRONTEND_TOOLCHAIN"

#: the two legs the job exists to run, by test name.
LEGS = (
    "test_the_frontend_really_builds_and_maps_to_the_originals",
    "test_the_frontend_typechecks_against_the_real_cordis_client",
)


def _job_block(name: str) -> str:
    """The lines of one job in ci.yml, by indentation. ci.yml is read as TEXT on
    purpose: PyYAML is not a declared dependency, so a yaml-parsing guard would
    `importorskip` in exactly the environments it is meant to police."""
    text = CI.read_text(encoding="utf-8")
    start = re.search(rf"^  {re.escape(name)}:\s*$", text, re.M)
    assert start, (
        f"no `{name}` job in {CI.relative_to(ROOT)}. It is the only thing that "
        "builds or typechecks the frontend; without it the legs in "
        f"{SUITE.relative_to(ROOT)} skip everywhere (gap G5). If the job was "
        "renamed, point this test at its new name."
    )
    rest = text[start.end():]
    end = re.search(r"^  \S", rest, re.M)
    return rest[: end.start()] if end else rest


def _steps(block: str) -> list[str]:
    """The job's `run:` command lines, in order, comments dropped."""
    lines = [ln for ln in block.splitlines() if not ln.lstrip().startswith("#")]
    return [m.strip() for m in re.findall(r"run:\s*(.+)", "\n".join(lines))]


# --- anti-vacuity: the things the job is about still exist ----------------- #
def test_the_suite_and_its_legs_still_exist():
    """Every claim below is about a job that runs these legs. If they are gone,
    re-derive this file against whatever replaced them rather than passing on an
    empty premise."""
    assert SUITE.is_file(), f"{SUITE.relative_to(ROOT)} is gone"
    src = SUITE.read_text(encoding="utf-8")
    for leg in LEGS:
        assert f"def {leg}(" in src, f"{leg} is gone from {SUITE.name}"
    assert (FRONTEND / "package-lock.json").is_file(), (
        "examples/app/frontend/package-lock.json is gone, so `npm ci` has "
        "nothing to install from."
    )


def test_the_legs_are_still_skippable_without_a_toolchain():
    """The other half of the premise. The job's env flag only means something
    while there is a skip for it to lift; if the legs stopped being gated, this
    file should be rewritten rather than asserting a flag nothing reads."""
    src = SUITE.read_text(encoding="utf-8")
    assert "needs_frontend_toolchain" in src
    assert "pytest.mark.skipif" in src
    for leg in LEGS:
        block = src.split(f"def {leg}(")[0]
        assert block.rstrip().endswith("@needs_frontend_toolchain"), (
            f"{leg} is no longer gated by @needs_frontend_toolchain"
        )


# --- half 1: a job installs the frontend and runs the legs ----------------- #
def test_a_job_installs_the_frontends_pinned_tree():
    block = _job_block(JOB)
    installs = [s for s in _steps(block) if "npm ci" in s]
    assert installs, (
        f"the `{JOB}` job no longer runs `npm ci`, so vite and vue-tsc are "
        "absent and both legs skip: the job reports green having compiled "
        "nothing."
    )
    assert any("examples/app/frontend" in s for s in installs), (
        f"`{JOB}` installs some other project's node_modules; the legs read "
        "examples/app/frontend/node_modules."
    )
    # `npm install` would resolve past the committed lockfile the suite pins.
    for step in installs:
        assert "npm install" not in step, (
            "`npm install` can drift from the committed lockfile that "
            "test_the_frontend_tree_is_pinned_and_the_lockfile_does_not_drift "
            "asserts. Use `npm ci`."
        )


def test_the_job_runs_the_frontend_asset_suite():
    steps = _steps(_job_block(JOB))
    runs = [s for s in steps if "test_app_frontend_725.py" in s]
    assert runs, (
        f"the `{JOB}` job installs the frontend and then never runs the suite "
        "that builds and typechecks it."
    )


# --- half 2: the job cannot be vacuous ------------------------------------- #
def test_the_job_turns_the_toolchain_skip_off():
    """The load-bearing line. Without this flag a job that installed nothing —
    or one whose `npm ci` silently produced an unusable tree — still runs the
    suite, skips both legs and reports success. That is gap G5 with a green
    check on top of it, which is worse than no job."""
    block = _job_block(JOB)
    assert f"{ENV_FLAG}:" in block, (
        f"the `{JOB}` job does not set {ENV_FLAG}. With the skip in force the "
        "build and typecheck legs report `skipping` and the job is green "
        "whether or not the frontend compiles."
    )
    assert re.search(rf'{ENV_FLAG}:\s*"?1"?\s*$', block, re.M), (
        f"{ENV_FLAG} is set to something other than 1; the suite lifts the "
        "skip only for the exact value 1."
    )


def test_the_suite_reads_the_flag_the_job_sets():
    """Both ends of the same contract, so a rename on either side reds here
    rather than silently restoring the skip."""
    src = SUITE.read_text(encoding="utf-8")
    assert ENV_FLAG in src, (
        f"{SUITE.name} no longer reads {ENV_FLAG}, so the CI job's env var "
        "lifts nothing and the legs skip again."
    )


def test_the_job_does_not_swallow_its_own_failure():
    """`continue-on-error`, `|| true` or an `if: false` turns the gate back into
    a note nobody reads."""
    block = _job_block(JOB)
    assert "continue-on-error" not in block, (
        f"`{JOB}` sets continue-on-error; a frontend that does not compile "
        "would then be a green run with a warning."
    )
    for step in _steps(block):
        assert "|| true" not in step and "|| :" not in step, (
            f"a step of `{JOB}` swallows its exit status: {step}"
        )


def test_the_typecheck_leg_carries_no_named_exemption():
    """Gap G4 was a single diagnostic filtered out of the typecheck assertion by
    name (`notes.client.ts(66,63): error TS6138`, the unread `transport` on a
    fully routed generated client). It is fixed in the generator, so the
    assertion is unconditional again. A new by-name exemption would make the leg
    green while shipping a diagnostic in a generated artifact, which is how G4
    lasted: add the exemption here with a reason, or fix the generator."""
    src = SUITE.read_text(encoding="utf-8")
    assert "_G4" not in src, (
        "the typecheck leg filters a diagnostic by name again. If a new "
        "generator gap needs an exemption, record it in "
        "docs/webapp-competitiveness-report.md and update this test."
    )
    client = (FRONTEND / "notes.client.ts").read_text(encoding="utf-8")
    assert "private readonly transport" not in client, (
        "examples/app/frontend/notes.client.ts declares a transport nothing "
        "reads (gap G4). Regenerate it: `revl export client --lang ts "
        "--service NotesApi` over examples/app/notes.rvl."
    )
