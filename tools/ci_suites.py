"""The CI-only suites of `tools/pre_merge.sh`, read from ci.yml (issue #2210).

`make pre-merge` runs the frontend suite, the backend emit/golden files and the
generated-artifact gates. CI runs more than that: whole backend directories
(`pytest backends/<tier>/`), vitest, and the conformance steps that set
`REVL_CROSS_TIER_SLOW`, `REVL_REQUIRE_TIERS` or `REVL_SWEEP_ALL_TIERS`. Before
this tool the local gate ran none of those and did not mention them either, so
a green `make pre-merge` said nothing about some 1,000 backend tests, all of
vitest and the rust/java semantic floor.

The list is read from `.github/workflows/ci.yml` at run time, so a suite CI
adds is listed here the day it lands, with no second list to keep in step. A
CI `run:` step is a CI-only suite when it

* runs pytest on a target under `backends/` (after its leading `cd`), or
* runs vitest, or
* sets `REVL_CROSS_TIER_SLOW`, `REVL_SWEEP_ALL_TIERS` or a `REVL_REQUIRE_*`
  switch in its own `env:`, or runs pytest in a job whose `env:` sets one
  (the frontend job's `pytest tests/` runs under `REVL_REQUIRE_TIERS=go`; the
  gate's own frontend step does not set it).

Each one is opt-in by its CI job name (`--with=backend-go,conformance` or
`--with=all`), because together they take far longer than the default gate and
fire cargo, javac, wasmtime and node on a shared box. An opted-in suite runs
only when the local toolchain its job installs is present; otherwise it is
SKIPPED with what is missing. A job with no local recipe below is SKIPPED with
that reason. Nothing is dropped silently.

    python3 tools/ci_suites.py --list [--with NAMES] [--pytest P] [--pytest-python PY]
    python3 tools/ci_suites.py --check-with NAMES
    python3 tools/ci_suites.py --run ID [--pytest P]

`--list` prints one tab-separated line per suite: `id state label detail`,
where state is `run`, `skip` (opted in, toolchain missing), `optout` (not
opted in) or `covered` (an earlier pre-merge step already runs it). When
ci.yml cannot be read and nothing is opted in, `--list` prints one `skip` line
saying so and exits 0; with a suite opted in it exits 2. `--check-with` exits 2
for a name that is no CI job with a CI-only suite. `--run` executes one
suite's `run:` text as written (line by line, under `bash -eo pipefail` as CI
does) with its CI switches and exits with its code.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

#: Step-level switches that make a step a CI-only suite on their own.
SWITCHES = ("REVL_CROSS_TIER_SLOW", "REVL_SWEEP_ALL_TIERS")
SWITCH_PREFIX = "REVL_REQUIRE_"

#: CI steps an earlier tools/pre_merge.sh step already runs as written,
#: keyed by (job, run text with whitespace collapsed), valued by that step.
COVERED = {
    ("backend-python", "cd backends/python && .venv/bin/pytest -q"):
        "backend-python (.venv pytest)",
}

#: Jobs whose CI-only steps are a slice of another listed suite and cannot run
#: on their own here, valued by what runs them instead.
COVERED_JOBS = {
    "root-suite-affected": "a sharded slice of the frontend job's `pytest tests/`, "
                           "which --with=frontend runs whole",
    # Issue #2642's PR job runs the selector's cordis half, a slice of the
    # guarded suite `frontend-cordis` runs whole (same runtime, same go pin),
    # so `--with=frontend-cordis` reproduces it on this machine.
    "cordis-affected": "a named slice of frontend-cordis's guarded `pytest tests/`, "
                       "which --with=frontend-cordis runs whole",
}


@dataclass(frozen=True)
class Recipe:
    """What a CI job installs, as checks that can be made on this machine."""

    tools: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    node: int = 0
    py_modules: tuple[str, ...] = ()
    local_env: tuple[str, ...] = ()
    hint: str = ""


TS_HINT = "put node >= 22 on PATH and run `cd backends/typescript && npm ci`"
PY_HINT = "run `sh backends/python/setup.sh`"

RECIPES = {
    "backend-python": Recipe(paths=("backends/python/.venv/bin/pytest",), hint=PY_HINT),
    "backend-typescript": Recipe(node=22, paths=("backends/typescript/node_modules",),
                                 hint=TS_HINT),
    "backend-wasm": Recipe(tools=("wasmtime", "wasm-tools"), py_modules=("wasmtime",),
                           hint="install the wasmtime and wasm-tools CLIs, `pip install wasmtime` "
                                "and check out cordis-wasm at $CORDIS_WASM"),
    "backend-rust": Recipe(tools=("cargo",)),
    "backend-java": Recipe(tools=("javac",), local_env=("REVL_CORDIS4J_CLASSES",),
                           hint="set it to cordis4j classes built as ci.yml's backend-java job "
                                "builds them"),
    "backend-go": Recipe(tools=("go",)),
    "backend-roots-combined": Recipe(),
    "frontend": Recipe(tools=("go",)),
    "frontend-cordis": Recipe(tools=("go",), paths=("backends/python/.venv/bin/python",),
                              hint=PY_HINT),
    "conformance": Recipe(tools=("go", "cargo"), node=22,
                          paths=("backends/typescript/node_modules",
                                 "backends/python/.venv/bin/python"),
                          hint=f"{PY_HINT} and {TS_HINT}"),
    "frontend-assets": Recipe(tools=("npm",), node=22,
                              paths=("examples/app/frontend/node_modules",),
                              hint="run `cd examples/app/frontend && npm ci`"),
    "temporal-exit": Recipe(node=22, paths=("tests/temporal_exit/node_modules",),
                            local_env=("REVL_TEMPORAL_DEV_SERVER",),
                            hint="start a Temporal dev server, set its address, and run "
                                 "`cd tests/temporal_exit && npm ci`"),
}


@dataclass
class Suite:
    id: str
    job: str
    run: str
    env: dict[str, str] = field(default_factory=dict)
    targets: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        what = "vitest" if "vitest" in self.run else " ".join(self.targets)
        if len(what) > 60:
            what = what[:57] + "..."
        return f"ci-only {self.id}: {what}"


def _flat(text: str) -> str:
    return " ".join(str(text).split())


def pytest_targets(run: str) -> tuple[str, ...]:
    """The paths each pytest call in `run` names, relative to the repo root.

    A leading `cd <dir> &&` moves the base; a pytest call with no path runs
    its directory. Options and their values (`-q`, `-p no:cacheprovider`)
    carry no `/` and no `.py`, so they are not paths.
    """
    out: list[str] = []
    cwd = "."
    for part in re.split(r"&&|;|\n", re.sub(r"\\\n", " ", run)):
        try:
            words = shlex.split(part, comments=True)
        except ValueError:
            words = part.split("#", 1)[0].split()
        if words[:1] == ["cd"] and len(words) > 1:
            cwd = words[1]
            continue
        idx = next((i for i, w in enumerate(words)
                    if w == "pytest" or w.endswith("/pytest")), None)
        if idx is None:
            continue
        paths = [w for w in words[idx + 1:]
                 if not w.startswith("-") and ("/" in w or w.endswith(".py"))]
        for p in paths or ["."]:
            out.append(posixpath.normpath(posixpath.join(cwd, p)).rstrip("/"))
    return tuple(out)


def _switched(env: dict) -> bool:
    return any(k in SWITCHES or k.startswith(SWITCH_PREFIX) for k in env)


def _is_ci_only(run: str, step_env: dict, job_env: dict) -> bool:
    targets = pytest_targets(run)
    if any(t == "backends" or t.startswith("backends/") for t in targets):
        return True
    if re.search(r"\bvitest\b", run):
        return True
    return _switched(step_env) or bool(targets and _switched(job_env))


def _env_value(v) -> str:
    # YAML `true` is a bool; the runner exports it as "true", not "True".
    return str(v).lower() if isinstance(v, bool) else str(v)


def _str_env(env) -> dict[str, str]:
    # An `env:` that is an expression string sets nothing this tool can read.
    if not isinstance(env, dict):
        return {}
    return {str(k): _env_value(v) for k, v in env.items()}


class CiShapeError(Exception):
    """ci.yml is not the shape this tool reads (no `jobs:` mapping)."""


def ci_suites(ci_yml: Path) -> list[Suite]:
    import yaml  # PyYAML is in the `[test]` extra, like the suite itself

    doc = yaml.safe_load(ci_yml.read_text(encoding="utf-8"))
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict):
        raise CiShapeError(f"{ci_yml} has no `jobs:` mapping")
    suites = []
    for job, spec in jobs.items():
        if not isinstance(spec, dict):
            continue  # a reusable-workflow call or similar: no steps to read
        job_env = _str_env(spec.get("env"))
        steps = spec.get("steps")
        for i, step in enumerate(steps if isinstance(steps, list) else []):
            run = step.get("run") if isinstance(step, dict) else None
            if not run:
                continue
            step_env = _str_env(step.get("env"))
            if not _is_ci_only(str(run), step_env, job_env):
                continue
            suites.append(Suite(id=f"{job}/{i}", job=job, run=str(run),
                                env={**job_env, **step_env},
                                targets=pytest_targets(str(run))))
    return suites


def _node_major() -> int:
    node = shutil.which("node")
    if not node:
        return 0
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True,
                             timeout=30, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    m = re.match(r"v(\d+)", out.strip())
    return int(m.group(1)) if m else 0


def missing(suite: Suite, root: Path, pytest: str, pytest_python: str) -> str:
    """Why `suite` cannot run here, or "" when its job's toolchain is present."""
    recipe = RECIPES.get(suite.job)
    if recipe is None:
        return (f"no local recipe for CI job {suite.job}: add one to "
                f"tools/ci_suites.py RECIPES")
    gaps = []
    if re.search(r"(^|&&|;)\s*pytest\b", suite.run) and not pytest:
        gaps.append("no pytest")
    gaps += [f"no {t}" for t in recipe.tools if not shutil.which(t)]
    gaps += [f"no {p}" for p in recipe.paths if not (root / p).exists()]
    gaps += [f"{v} unset" for v in recipe.local_env if not os.environ.get(v)]
    if recipe.node and _node_major() < recipe.node:
        gaps.append(f"node on PATH is not >= {recipe.node}")
    for mod in recipe.py_modules:
        ok = pytest_python and subprocess.run(
            [pytest_python, "-c", f"import {mod}"], capture_output=True,
            timeout=60, check=False).returncode == 0
        if not ok:
            gaps.append(f"python module {mod} missing")
    if not gaps:
        return ""
    why = ", ".join(gaps)
    return f"{why}; to run it, {recipe.hint}" if recipe.hint else why


def _is_path(value: str) -> bool:
    return value.startswith("/") or "${{" in value


def switches(suite: Suite) -> dict[str, str]:
    """The suite's CI environment that is copied here: every value except a
    path (`/tmp/cordis4j-classes`, a `${{ }}` expression), which names the
    runner's disk, and the variables its recipe says this machine supplies."""
    local = RECIPES.get(suite.job, Recipe()).local_env
    return {k: v for k, v in suite.env.items()
            if not _is_path(v) and k not in local}


def run_env(suite: Suite, gocache: str = "") -> dict[str, str]:
    """This machine's environment plus the suite's CI switches. With no
    GOCACHE exported, `gocache` (a private directory) replaces go's per-user
    default, which concurrent runs on a shared box corrupt."""
    env = {**os.environ, **switches(suite)}
    if gocache and not os.environ.get("GOCACHE"):
        env["GOCACHE"] = gocache
    return env


def command(suite: Suite, pytest: str) -> str:
    """The CI `run:` text, line by line as CI runs it, with a bare `pytest`
    pointed at the gate's pytest and no `--junitxml` (CI's report path is a
    fixed /tmp file every run would share). Never flattened: a `#` comment
    line or a `\\` continuation means what it means in the original."""
    lines = []
    for line in str(suite.run).splitlines():
        line = re.sub(r"[ \t]--junitxml(=|[ \t]+)\S+", "", line)
        if pytest:
            line = re.sub(r"(^\s*|&&\s*|;\s*)pytest(?=\s|$)",
                          lambda m: m.group(1) + shlex.quote(pytest), line)
        lines.append(line)
    return "\n".join(lines)


def _one_line(cmd: str) -> str:
    """`cmd` for a one-line listing: each newline shown as a literal \\n."""
    return cmd.strip().replace("\n", "\\n")


def _wanted(job: str, chosen: set[str]) -> bool:
    return "all" in chosen or job in chosen


def list_suites(suites, root, chosen, pytest, pytest_python):
    for s in suites:
        covered = COVERED.get((s.job, _flat(s.run)))
        if not covered and s.job in COVERED_JOBS:
            covered = COVERED_JOBS[s.job]
        if covered:
            state, detail = "covered", f"runs above as {covered}"
        elif not _wanted(s.job, chosen):
            state, detail = "optout", f"opt in: --with={s.job}"
        else:
            why = missing(s, root, pytest, pytest_python)
            shown = " ".join(f"{k}={v}" for k, v in sorted(switches(s).items()))
            cmd = _one_line(command(s, pytest))
            cmd = f"{shown} {cmd}" if shown else cmd
            state, detail = ("skip", why) if why else ("run", cmd)
        print("\t".join((s.id, state, s.label, detail.replace("\t", " "))))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--root", default=".")
    ap.add_argument("--ci", default=None, help="workflow file (default: ci.yml)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--run", metavar="ID")
    ap.add_argument("--check-with", metavar="NAMES", default=None)
    ap.add_argument("--with", dest="chosen", default="",
                    help="comma-separated CI job names, or all")
    ap.add_argument("--pytest", default="")
    ap.add_argument("--pytest-python", default="")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    ci = Path(args.ci) if args.ci else root / ".github" / "workflows" / "ci.yml"
    names = args.chosen if args.check_with is None else args.check_with
    chosen = {c.strip() for c in names.split(",") if c.strip()}
    try:
        suites = ci_suites(ci)
    except Exception as exc:  # any unreadable ci.yml: say so, never crash
        why = f"cannot read {ci}: {type(exc).__name__}: {exc}"
        if args.list and not chosen:
            # Nothing opted in: the default gate stays green but says loudly
            # that the CI-only suites were not even listed.
            print("\t".join(("ci.yml", "skip", "ci-only suites (from ci.yml)",
                             _one_line(why))))
            return 0
        print(why, file=sys.stderr)
        return 2
    unknown = sorted(chosen - {s.job for s in suites} - {"all"})
    if unknown:
        jobs = ", ".join(sorted({s.job for s in suites}))
        print(f"--with names no CI job with a CI-only suite: {', '.join(unknown)}. "
              f"Known: all, {jobs}", file=sys.stderr)
        return 2
    if args.check_with is not None:
        return 0
    pytest = args.pytest
    if pytest and "/" in pytest and not os.path.isabs(pytest):
        pytest = str(root / pytest)  # a step may `cd` before calling it
    if args.list:
        list_suites(suites, root, chosen, pytest, args.pytest_python)
        return 0
    if args.run:
        suite = next((s for s in suites if s.id == args.run), None)
        if suite is None:
            print(f"no CI-only suite {args.run} in {ci}", file=sys.stderr)
            return 2
        with tempfile.TemporaryDirectory(prefix="revl-premerge-gocache-") as gocache:
            # GitHub runs a `run:` block as `bash -e {0}`, with pipefail.
            return subprocess.run(
                ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c",
                 command(suite, pytest)],
                cwd=root, env=run_env(suite, gocache), check=False).returncode
    ap.error("pass --list, --check-with NAMES or --run ID")
    return 2


if __name__ == "__main__":
    sys.exit(main())
