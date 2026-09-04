"""Every environment switch `tests/` reads must be set in CI, or declared local.

Roadmap item 445. This is the systemic half of that item; flipping one switch
was the artifact.

The failure mode this guards is not a bug, it is a *shape*. A suite is written,
it is correct, it is gated behind an environment variable so it does not slow
the default run down, and then nobody ever sets the variable. The suite still
collects, still reports, and every one of its tests reports SKIPPED, which is
green. The coverage is gone and the dashboard does not show it, because a skip
and a pass are the same colour.

It has now happened twice in this repository, in the same quarter:

  - item 430: `pytest tests/` ran in exactly one job, which installed no
    cordis-py, so 398 tests across 81 modules executed nowhere in CI.
  - item 433: `REVL_CROSS_TIER_SLOW` was set in no workflow, no Makefile and
    no ci/ script, so the rust and java halves of the cross-tier semantic
    floor executed nowhere. Two live java correctness defects (`Int / Int`
    doing integer division, `==` on Float inverting NaN and -0.0) sat under
    assertions that already covered them, for as long as Float has existed.

So the rule: a variable that `tests/` READS but never SETS is a switch owned by
something outside the suite. Either CI throws it, or somebody writes down why
it is a developer-machine-only switch. Silence is no longer one of the options.

AND ONE STEP FURTHER, because "is it set in CI" is the wrong question on its
own. `REVL_CORDIS4J_CLASSES` is set in the `backend-java` job, which runs only
`backends/java/test_emit_java.py`; the test that READS it lives in
`tests/test_realm_conformance.py`, and every job that collects `tests/` leaves
it unset. So a naive audit answers "yes, it is set" while the probe has still
never executed. That is item 430's shape exactly, and it is the variant most
likely to survive a review, so it gets its own check below:
`test_a_ci_set_gate_is_set_in_a_job_that_actually_runs_it` matches each
setting step's pytest targets against the files that read the variable.

TOOLCHAIN PROBES, the other half (issue #266). This file used to end here, with
a paragraph saying plainly that it checked env-var gates ONLY: that a skip
guarded on a FILESYSTEM probe -- `shutil.which("cargo")`,
`node_modules/.bin/vitest` existing, `find_spec("cordis")` -- was exactly as
invisible and was not covered.

It was not covered, and it bit. `tests/test_163_match_payload_bind_scope.py
::test_payload_bind_scope_executes[ts]` calls `RUNNERS["ts"]`, which answers
("skip", "vitest not installed") wherever backends/typescript/node_modules is
absent -- every fresh worktree, and every CI job that does not run `npm ci`. A
live `TypeError: Cannot mix BigInt and other types` sat under that green skip
until two agents who happened to have node_modules tripped over it, and each
read it as pre-existing, because from a clean checkout it is invisible.

So the second half of the file, below the env-var checks, closes that class for
tier tests: `REVL_REQUIRE_TIERS` (src/revl/test.py) makes an absent toolchain a
FAILURE on the tiers a job has provisioned, and
`test_every_tier_test_is_required_to_actually_run_in_some_ci_job` checks that
every file driving `RUNNERS` is run by a step that sets it -- the same
"is it set in the job that runs the file" question the env-var half asks,
against the same `_pytest_targets`/`_covers` helpers.

STILL NOT CAUGHT, stated plainly so nobody reads more into a green run than is
there: a toolchain-probe skip that does NOT go through a `revl.test` tier
runner. tests/test_time_coeffect.py probing for backends/python/.venv by hand is
one; `pytest.importorskip` anywhere is another. Those remain per-file, and the
`backend-wasm` job's junit skip audit remains the strongest tool for a whole
job at once.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
# A HARD import, deliberately (issue #266). This was `pytest.importorskip("yaml")`
# inside `_steps_setting`, which made the two strongest checks in this file --
# the ones that pair a gate against the job that actually runs it -- skip
# wherever PyYAML was absent. PyYAML is not in the base install, so they skipped
# in the `frontend` matrix: a guard against silent skips, silently skipping.
# `pyyaml` is now in the `test` extra (pyproject.toml) and in
# backends/python/setup.sh, so every job that collects this file has it, and its
# absence is a loud collection error rather than three quiet green skips.
import yaml

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
WORKFLOWS = ROOT / ".github" / "workflows"
CI_DIR = ROOT / "ci"
MAKEFILE = ROOT / "Makefile"

# Variables the operating system or the toolchain owns. Nothing in CI should
# have to declare these, and a test reading one is not gating on a switch.
_AMBIENT = frozenset({
    "HOME", "PATH", "PYTHONPATH", "TMPDIR", "TEMP", "TMP", "USER", "SHELL",
    "CI", "GITHUB_ACTIONS", "VIRTUAL_ENV", "PWD", "LANG", "LC_ALL",
    "PYTEST_CURRENT_TEST", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
})

# Switches that are deliberately developer-machine-only. A name belongs here
# ONLY with a reason that survives the question "why can CI not just set it?".
# "it is slow" is not a reason on its own -- slow is what a dedicated job is
# for, and item 433 is what happens when slow is allowed to mean unmeasured.
#
# This registry may only shrink. `test_no_stale_local_declarations` below fails
# if an entry stops being read, or starts being set in CI, so a name cannot
# quietly rot here after the gap it describes is closed.
_INTENTIONALLY_LOCAL: dict[str, str] = {
    "REVL_CONFORMANCE_PY": (
        "An OVERRIDE, not a gate. `_python_with_cordis` falls back to "
        "backends/python/.venv, which `sh backends/python/setup.sh` creates in "
        "the `frontend-cordis` and `conformance` jobs, so the test it guards "
        "DOES execute in CI without it. It exists so a developer with the "
        "runtime somewhere else can point at it. Unset in CI is correct."
    ),
    "REVL_CONFORMANCE_TS": (
        "Same override shape as REVL_CONFORMANCE_PY: `_ts_backend_dir` falls "
        "back to backends/typescript, and the real gate is whether "
        "node_modules/cordis is installed there. Item 445 fixed the CI half by "
        "running tests/test_realm_conformance.py in the `conformance` job, "
        "which is the one job that runs `npm ci`. The variable itself stays a "
        "developer convenience."
    ),
}

# Reading one of these is a read of the CHILD process's environment being
# built, or of a name the suite hands to emitted code, not a switch the suite
# is gated on. Detected structurally below where possible; listed here where
# the shape is a plain string inside generated source.
_MIN_REASON_CHARS = 80

# A BARE read -- `os.environ.get(NAME)` with no default, or `os.environ[NAME]`
# -- is the signature of a gate: nothing supplies a value if CI does not. A read
# WITH a default is an override, and unset is its correct state. Some bare reads
# still fall back further down the function, so they are overrides in fact if
# not in shape; those are named here rather than guessed at.
_OVERRIDE_NOT_GATE: dict[str, str] = {
    "REVL_PY": (
        "Read bare in test_seam_value_leaks_421_f5f6.py, but the helper falls "
        "back to backends/python/.venv immediately after, exactly as "
        "ci/placement_smoke.sh does. test_seam2_same_tier_readmission.py reads "
        "it WITH a default and shows the intent plainly. Unset is correct."
    ),
    "REVL_CONFORMANCE_PY": _INTENTIONALLY_LOCAL["REVL_CONFORMANCE_PY"],
    "REVL_CONFORMANCE_TS": _INTENTIONALLY_LOCAL["REVL_CONFORMANCE_TS"],
}

# Gates that ARE set in CI, but in a job that does not run the test reading
# them. This is the item-430 shape once more and the nastiest variant of the
# class, because "is it set anywhere in CI" answers YES while the test has
# still never executed. An entry here is an ENUMERATED hole, not an excuse:
# it must name the job, the reading test, and why it is not closed yet.
_KNOWN_WRONG_JOB: dict[str, str] = {
    "REVL_CORDIS4J_CLASSES": (
        "Set in the `backend-java` job, which runs ONLY "
        "backends/java/test_emit_java.py. The test that reads it is "
        "tests/test_realm_conformance.py::test_cordis4j_realm_conformance, and "
        "every job that collects tests/ (`frontend`, `frontend-cordis`, and the "
        "item-445 step in `conformance`) leaves it unset, so that probe has "
        "skipped in CI as completely as the REVL_CROSS_TIER_SLOW ones did. "
        "Closing it means cloning and compiling cordis4j-core in `conformance` "
        "the way `backend-java` already does; it is left off here because it "
        "could not be executed on the audit machine to prove it green first, "
        "and an unverified CI step is how a red build happens. Roadmap 445."
    ),
}


def _test_sources() -> list[Path]:
    return sorted(p for p in TESTS.rglob("*.py") if "__pycache__" not in p.parts)


def _const_map(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal"` bindings, so a gate written through an
    indirection (`_TARGET_ENV = "REVL_WIT_TARGET"`) still resolves."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        out[target.id] = node.value.value
    return out


def _as_name(node: ast.expr | None, consts: dict[str, str]) -> str | None:
    """The env-var name a subscript/argument denotes, if it is statically one."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def _is_environ(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "environ"


def _scan(path: Path) -> tuple[set[str], set[str], set[str]]:
    """(names read, names the module sets itself, names read with NO default).

    The third set is what separates a gate from an override: a read with a
    default cannot be starved by CI, a bare one can.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    consts = _const_map(tree)
    reads: set[str] = set()
    owned: set[str] = set()
    bare: set[str] = set()

    for node in ast.walk(tree):
        # os.environ[NAME] -- a read, or a write when it is an assign target
        if isinstance(node, ast.Subscript) and _is_environ(node.value):
            name = _as_name(node.slice, consts)
            if name:
                reads.add(name)
                bare.add(name)
        # os.environ[NAME] = ... -- the suite owns the variable
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and _is_environ(target.value):
                    name = _as_name(target.slice, consts)
                    if name:
                        owned.add(name)
        if isinstance(node, ast.Call):
            func = node.func
            attr = func.attr if isinstance(func, ast.Attribute) else None
            first = _as_name(node.args[0], consts) if node.args else None
            if first is None:
                continue
            # os.environ.get(NAME) / os.getenv(NAME)
            if attr == "get" and isinstance(func, ast.Attribute) and _is_environ(func.value):
                reads.add(first)
                if len(node.args) == 1:
                    bare.add(first)
            elif attr == "getenv":
                reads.add(first)
                if len(node.args) == 1:
                    bare.add(first)
            # monkeypatch.setenv(NAME, ...) / monkeypatch.delenv(NAME)
            elif attr in ("setenv", "delenv"):
                owned.add(first)

    return reads, owned, bare


def _scan_all() -> tuple[dict[str, set[Path]], set[str], set[str]]:
    """(name -> reading files, names the suite owns, names read with no default)."""
    reads: dict[str, set[Path]] = {}
    owned: set[str] = set()
    bare: set[str] = set()
    for path in _test_sources():
        try:
            module_reads, module_owned, module_bare = _scan(path)
        except SyntaxError:  # pragma: no cover - a broken test file is its own failure
            continue
        owned |= module_owned
        bare |= module_bare
        for name in module_reads:
            reads.setdefault(name, set()).add(path)
    return reads, owned, bare


def _external_switches() -> dict[str, set[str]]:
    """Env names `tests/` reads but never sets: name -> modules that read it."""
    reads, owned, _ = _scan_all()
    return {
        name: {p.name for p in where}
        for name, where in reads.items()
        if name not in owned and name not in _AMBIENT
    }


def _external_gate_files() -> dict[str, set[Path]]:
    """Bare-read external switches: name -> the test files that read them.

    Bare means no default, so nothing but CI can supply a value. Names listed
    in `_OVERRIDE_NOT_GATE` fall back further down their own helper and are
    excluded by hand, because that fallback is not visible at the call site.
    """
    reads, owned, bare = _scan_all()
    return {
        name: where
        for name, where in reads.items()
        if name in bare
        and name not in owned
        and name not in _AMBIENT
        and name not in _OVERRIDE_NOT_GATE
    }


def _ci_sets() -> set[str]:
    """Env names something in CI actually assigns."""
    found: set[str] = set()
    sources: list[Path] = []
    if WORKFLOWS.is_dir():
        sources += sorted(WORKFLOWS.rglob("*.yml")) + sorted(WORKFLOWS.rglob("*.yaml"))
    if CI_DIR.is_dir():
        sources += sorted(p for p in CI_DIR.rglob("*") if p.is_file())
    if MAKEFILE.is_file():
        sources.append(MAKEFILE)

    # A YAML `env:` entry, a shell export/assignment, or a `VAR=value cmd`
    # prefix. Deliberately ignores `${VAR:-default}` READS, which are the
    # consuming side and prove nothing about the variable being set.
    yaml_env = re.compile(r"^\s+([A-Z][A-Z0-9_]{2,}):\s", re.MULTILINE)
    shell_set = re.compile(r"(?:^|\s|export\s+)([A-Z][A-Z0-9_]{2,})=", re.MULTILINE)

    for path in sources:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover
            continue
        # A commented-out line is a STAGED switch, not a set one. Item 445's
        # own flip sits commented in ci.yml behind the java record fix, and it
        # must not count as satisfied until it is uncommented.
        live = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        found |= set(yaml_env.findall(live))
        found |= set(shell_set.findall(live))
    return found


def _pytest_targets(run: str) -> list[str]:
    """Repo-relative paths a `run:` block hands to pytest.

    A step that runs pytest with no path at all collects the whole rootdir, so
    it covers everything; that is returned as a bare ".".
    """
    targets: list[str] = []
    for line in run.splitlines():
        if "pytest" not in line:
            continue
        prefix = ""
        # `cd backends/python && .venv/bin/pytest -q` runs in a subdirectory,
        # so its paths are relative to that, not to the repo root.
        cd_match = re.search(r"cd\s+([\w./-]+)\s*&&", line)
        if cd_match:
            prefix = cd_match.group(1).rstrip("/") + "/"
        args = line.split("pytest", 1)[1].split()
        paths = [
            a for a in args
            if not a.startswith("-") and (a.endswith(".py") or "/" in a)
        ]
        if not paths:
            targets.append(prefix or ".")
        targets += [prefix + p for p in paths]
    return targets


def _covers(target: str, rel: str) -> bool:
    """Does a pytest target collect the test file at repo-relative `rel`?"""
    if target in (".", ""):
        return True
    target = target.rstrip("/")
    return rel == target or rel.startswith(target + "/")


def _steps_setting(name: str) -> list[tuple[str, str]]:
    """(job, run-command) for every workflow step whose `env:` sets `name`."""
    out: list[tuple[str, str]] = []
    for path in sorted(WORKFLOWS.rglob("*.yml")) + sorted(WORKFLOWS.rglob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job, spec in (doc.get("jobs") or {}).items():
            job_env = (spec or {}).get("env") or {}
            for step in (spec or {}).get("steps") or []:
                step_env = {**job_env, **((step or {}).get("env") or {})}
                if name in step_env:
                    out.append((job, str((step or {}).get("run", ""))))
    return out


# This assertion was xfail(strict=True) while the REVL_CROSS_TIER_SLOW flip sat
# commented out in ci.yml behind the java v3 record equals/hashCode fix. That
# fix landed, the flip went live in the `conformance` job, and the strict xfail
# did its job: it turned red the moment the test started passing, which is what
# forced this marker to be removed rather than left behind. The check now
# stands on its own and any newly-dead switch reds it directly.
def test_every_env_gate_in_tests_is_set_in_ci_or_declared_local():
    """The item-445 guard: no switch may be silently unset everywhere."""
    external = _external_switches()
    ci = _ci_sets()

    undeclared = {
        name: sorted(where)
        for name, where in sorted(external.items())
        if name not in ci and name not in _INTENTIONALLY_LOCAL
    }

    assert not undeclared, (
        "these environment switches are read by tests/ but set NOWHERE in "
        ".github/workflows/, ci/ or the Makefile, and are not declared "
        "developer-machine-only:\n"
        + "\n".join(f"  {name}: read by {', '.join(w)}" for name, w in undeclared.items())
        + "\n\nA test gated on an unset variable SKIPS, and a skip is green, so "
        "the suite reports success while measuring nothing. That is roadmap "
        "items 430 and 433, twice. Either set it in the job that has the "
        "toolchain for it (`conformance` is the one place node, rust, java, go "
        "and wasmtime all exist at once), or add it to _INTENTIONALLY_LOCAL "
        "above with a reason that is not 'it is slow'."
    )


def test_no_stale_local_declarations():
    """The registry may only shrink: no entry that is dead or now CI-set."""
    external = _external_switches()
    ci = _ci_sets()

    unread = sorted(n for n in _INTENTIONALLY_LOCAL if n not in external)
    assert not unread, (
        f"declared developer-machine-only but no longer read by any test: "
        f"{unread}. Drop the entry."
    )

    now_in_ci = sorted(n for n in _INTENTIONALLY_LOCAL if n in ci)
    assert not now_in_ci, (
        f"declared developer-machine-only but now SET in CI: {now_in_ci}. "
        "The gap closed; drop the entry rather than leaving the excuse behind."
    )


def test_a_ci_set_gate_is_set_in_a_job_that_actually_runs_it():
    """"Set in CI" is not the question. "Set in the job that runs the test" is.

    This is the nastiest variant of the class and the one item 430 was: the
    variable IS assigned somewhere in the workflow, so every naive audit --
    including `test_every_env_gate_in_tests_is_set_in_ci_or_declared_local`
    above -- answers YES, while the test that reads it still executes nowhere
    because the assignment lives in a job that never collects that file.

    `REVL_CORDIS4J_CLASSES` is exactly that today and is enumerated in
    `_KNOWN_WRONG_JOB` rather than left silent.
    """
    ci = _ci_sets()
    stranded: dict[str, str] = {}

    for name, files in sorted(_external_gate_files().items()):
        if name not in ci or name in _KNOWN_WRONG_JOB:
            continue
        rels = sorted(f.relative_to(ROOT).as_posix() for f in files)
        setting = _steps_setting(name)
        covered = any(
            _covers(target, rel)
            for _job, run in setting
            for target in _pytest_targets(run)
            for rel in rels
        )
        if not covered:
            jobs = sorted({job for job, _ in setting}) or ["<no step env>"]
            stranded[name] = f"set in {jobs}, but read by {rels}, which no such step runs"

    assert not stranded, (
        "these gates are set in CI but in a job that does not run the test "
        "reading them, so the test still skips everywhere:\n"
        + "\n".join(f"  {n}: {why}" for n, why in stranded.items())
        + "\n\nSet the variable in the job that actually collects the file, or "
        "enumerate it in _KNOWN_WRONG_JOB with the job, the test, and why it is "
        "not closed. Roadmap items 430 and 445."
    )


def test_no_stale_wrong_job_declarations():
    """The enumerated-hole registry may only shrink, same as the local one."""
    gates = _external_gate_files()
    ci = _ci_sets()
    for name in sorted(_KNOWN_WRONG_JOB):
        assert name in gates, (
            f"{name} is enumerated as a wrong-job hole but is no longer a "
            f"bare-read gate in tests/. Drop the entry."
        )
        assert name in ci, (
            f"{name} is enumerated as a wrong-job hole but is no longer set in "
            f"CI at all, so it belongs in _INTENTIONALLY_LOCAL or nowhere."
        )


@pytest.mark.parametrize(
    "name", sorted({**_INTENTIONALLY_LOCAL, **_KNOWN_WRONG_JOB, **_OVERRIDE_NOT_GATE})
)
def test_local_declaration_carries_a_real_reason(name):
    """A one-word excuse is how the class survives. Make writing it cost."""
    reason = {**_INTENTIONALLY_LOCAL, **_KNOWN_WRONG_JOB, **_OVERRIDE_NOT_GATE}[name]
    assert len(reason) >= _MIN_REASON_CHARS, (
        f"{name}: the reason is {len(reason)} characters. Explain why CI "
        f"cannot set it, not merely that it does not."
    )
    # Look for "slow" as an English word offered as the justification, not as
    # part of an identifier: REVL_CROSS_TIER_SLOW is legitimately named in
    # several of these reasons, and it is a name, not an excuse.
    prose = re.sub(r"`[^`]*`", " ", reason)          # drop backticked code
    prose = re.sub(r"\b[A-Z][A-Z0-9_]{2,}\b", " ", prose)  # drop SHOUTY identifiers
    assert not re.search(r"\bslow(ness|ly)?\b", prose, re.IGNORECASE), (
        f"{name}: slowness is not a reason to measure nothing. A slow suite "
        f"belongs in a dedicated job, which is what `conformance` is for. "
        f"Give the reason CI cannot set it."
    )


def test_the_two_known_instances_stay_covered():
    """REVL_CROSS_TIER_SLOW is item 433's instance and the reason this file
    exists. It must never be silently absent from BOTH sides again: either CI
    sets it, or it is declared local with a reason. It may not simply vanish
    from the tests, which would be the third way to lose the coverage."""
    external = _external_switches()
    ci = _ci_sets()
    name = "REVL_CROSS_TIER_SLOW"
    assert name in external or name in ci, (
        f"{name} is no longer read by any test in tests/. If the cross-tier "
        "slow probes were removed or ungated, say so on roadmap item 445 and "
        "delete this test deliberately. Do not let it lapse by accident."
    )


# --------------------------------------------------------------------------
# THE OTHER HALF: skips gated on a TOOLCHAIN, not on an env var (issue #266).
#
# Everything above audits env-var gates, and the module docstring says plainly
# what it does not catch: "A skip guarded on a FILESYSTEM probe --
# `shutil.which("cargo")`, `node_modules/.bin/vitest` existing,
# `find_spec("cordis")` -- is exactly as invisible and is not covered here."
#
# It stayed uncovered until it bit. `tests/test_163_match_payload_bind_scope.py
# ::test_payload_bind_scope_executes[ts]` reads like it executes on the ts tier.
# It calls `RUNNERS["ts"]`, which answers ("skip", "vitest not installed")
# wherever backends/typescript/node_modules is absent -- so in every fresh
# worktree, and in every CI job that does not run `npm ci`. A skip is green.
# Underneath it sat a live `TypeError: Cannot mix BigInt and other types`,
# reproduced independently by two agents who happened to have node_modules and
# read as pre-existing by everyone who did not. Nothing on any dashboard said
# the ts tier had not run.
#
# The fix has two halves and this is the second one:
#
#   1. `REVL_REQUIRE_TIERS` (src/revl/test.py) names the tiers a job has
#      provisioned. On those, "the toolchain is absent" stops being a skip and
#      becomes a failure -- and ONLY that kind of skip flips, never a tier's
#      by-design refusal of a document (see `revl.test.Absent`).
#   2. This check, which makes the switch impossible to forget: every test file
#      in `tests/` that drives a tier runner must be RUN BY A CI STEP THAT SETS
#      IT, for every tier that file names. Same question the env-var half asks
#      -- "is it set in the job that actually runs the file" -- reused verbatim
#      (`_pytest_targets` / `_covers`), because the failure is the same failure.
# --------------------------------------------------------------------------

#: Every tier `revl.test.RUNNERS` can drive.
_TIERS = frozenset({"py", "ts", "rust", "java", "wasm", "go"})

#: The switch itself. Read in src/revl/, not in tests/, so the scanners above
#: never see it -- it gets its own pairing check below.
_REQUIRE_ENV = "REVL_REQUIRE_TIERS"

# Tiers no CI job requires yet. Per TIER, not per test, because the reason is
# per tier: it is about what a job can provision and what has been proven green
# there, not about any one file. Same contract as every other registry in this
# file -- it may only shrink, `test_no_stale_tier_exemptions` sees to that, and
# `test_local_declaration_carries_a_real_reason`'s rules apply to the prose.
_TIER_NOT_REQUIRED_IN_CI: dict[str, str] = {
    "rust": (
        "`conformance` installs a rust toolchain, but `RUNNERS['rust']` also "
        "reaches for crates.io to resolve cordis-rs and answers an absent-"
        "toolchain skip when the index is unreachable. Requiring the tier would "
        "turn a network blip into a red build on an unrelated PR, so closing "
        "this means vendoring or caching the crate registry in that job first. "
        "The tier still executes there when the index answers; what is missing "
        "is the guarantee, not the coverage."
    ),
    "java": (
        "`RUNNERS['java']` compiles against in-repo stubs unless "
        "REVL_CORDIS4J_CLASSES points at real cordis4j classes, and that "
        "variable is set only in `backend-java`, which never collects tests/ -- "
        "the hole already enumerated in _KNOWN_WRONG_JOB above. Requiring the "
        "java tier before that pairing is fixed would demand the stub path in "
        "the job that has a JDK while the real path stays unmeasured, which "
        "buys a green tick and no coverage. Close it with _KNOWN_WRONG_JOB, "
        "in one change, not before."
    ),
    "wasm": (
        "The wasm tier needs BOTH wasmtime and a cordis-wasm checkout, and only "
        "`backend-wasm` provisions the second (a pinned clone plus its own "
        "venv). That job already runs the stricter check this switch imitates: "
        "a junit audit that fails on ANY unexpected skip, with the two version "
        "gates enumerated verbatim. Requiring the tier in a job that has no "
        "cordis-wasm would red the build for a substrate it was never given; "
        "the coverage is real and it lives there."
    ),
    "go": (
        "`conformance` and `frontend-cordis` both pin go 1.26, so the tier does "
        "execute in CI, and tools/conformance.py --execute --require-execution "
        "already fails that job when the go runtime is missing. What is not "
        "wired is this switch on the go-driving files, which needs the same "
        "green-first proof the py/ts step below got before being turned on. It "
        "is a follow-on of one step, not a missing toolchain."
    ),
}


def _tier_names_in(tree: ast.Module) -> set[str]:
    """Tiers a test module drives a `revl.test` runner with.

    Three spellings, all of them live in `tests/` today:

      * ``RUNNERS["ts"](ir)``                     -- a literal subscript
      * ``RUNNERS[tier]`` under
        ``@pytest.mark.parametrize("tier", [...])`` -- the common shape
      * ``from revl.test import run_py, run_ts``  -- the direct import
    """
    found: set[str] = set()
    consts = _const_map(tree)

    for node in ast.walk(tree):
        # RUNNERS["ts"] / RUNNERS[_TIER]
        if isinstance(node, ast.Subscript):
            base = node.value
            name = (base.id if isinstance(base, ast.Name)
                    else base.attr if isinstance(base, ast.Attribute) else None)
            if name == "RUNNERS":
                tier = _as_name(node.slice, consts)
                if tier in _TIERS:
                    found.add(tier)
        # from revl.test import run_ts
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("test"):
            for alias in node.names:
                if alias.name.startswith("run_") and alias.name[4:] in _TIERS:
                    found.add(alias.name[4:])
    return found


def _const_seq_map(tree: ast.Module) -> dict[str, set[str]]:
    """Module-level ``NAME = ("py", "ts")`` bindings.

    Needed because the tier lists are almost never written inline:
    tests/test_cross_tier_execution.py parametrizes over `FAST_TIERS`,
    `SLOW_TIERS` and `BOUNDED_TIERS`, and reading only inline literals silently
    missed the py and ts halves of that file -- an audit that under-reports is
    the same failure it is auditing.
    """
    out: dict[str, set[str]] = {}
    for node in tree.body:
        if not (isinstance(node, ast.Assign)
                and isinstance(node.value, (ast.Tuple, ast.List, ast.Set))):
            continue
        values = {e.value for e in node.value.elts
                  if isinstance(e, ast.Constant) and isinstance(e.value, str)}
        for target in node.targets:
            if isinstance(target, ast.Name) and values:
                out[target.id] = values
    return out


def _parametrized_tiers(tree: ast.Module) -> set[str]:
    """Tier names a module parametrizes over, inline or via a module constant.

    Only consulted for a module that already drives `RUNNERS` -- a bare list of
    strings elsewhere in a file proves nothing about what it runs.
    """
    found: set[str] = set()
    seqs = _const_seq_map(tree)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "parametrize" or not node.args:
            continue
        argnames = node.args[0]
        if not (isinstance(argnames, ast.Constant)
                and isinstance(argnames.value, str)
                and ("tier" in argnames.value or "backend" in argnames.value)):
            continue
        for arg in node.args[1:]:
            for item in ast.walk(arg):
                if isinstance(item, ast.Constant) and item.value in _TIERS:
                    found.add(item.value)
                elif isinstance(item, ast.Name):
                    found |= seqs.get(item.id, set()) & _TIERS
    return found


def _tier_test_files() -> dict[Path, set[str]]:
    """Test file -> the tiers it actually drives a runner on."""
    out: dict[Path, set[str]] = {}
    for path in _test_sources():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken test file is its own failure
            continue
        text = path.read_text(encoding="utf-8")
        if "RUNNERS" not in text and "revl.test import run_" not in text:
            continue
        tiers = _tier_names_in(tree)
        # `RUNNERS[tier]` with a parametrized `tier`: the literal subscript scan
        # finds nothing, so fall back to what the module parametrizes over.
        if "RUNNERS[" in text and not tiers:
            tiers = _parametrized_tiers(tree)
        elif "RUNNERS[" in text:
            tiers |= _parametrized_tiers(tree)
        if tiers:
            out[path] = tiers
    return out


def _require_steps() -> list[tuple[str, str, set[str]]]:
    """(job, run-command, required tiers) for each step setting the switch."""
    out: list[tuple[str, str, set[str]]] = []
    for path in sorted(WORKFLOWS.rglob("*.yml")) + sorted(WORKFLOWS.rglob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job, spec in (doc.get("jobs") or {}).items():
            job_env = (spec or {}).get("env") or {}
            for step in (spec or {}).get("steps") or []:
                step_env = {**job_env, **((step or {}).get("env") or {})}
                if _REQUIRE_ENV not in step_env:
                    continue
                raw = str(step_env[_REQUIRE_ENV])
                names = {n for n in raw.replace(",", " ").split() if n}
                tiers = set(_TIERS) if "all" in names else names
                out.append((job, str((step or {}).get("run", "")), tiers))
    return out


def test_every_tier_test_is_required_to_actually_run_in_some_ci_job():
    """No tier test may pass by never running (issue #266).

    A test that calls `RUNNERS[tier]` and skips on a missing toolchain reports
    green in every environment that lacks the tier. That is exactly items 430,
    433 and 445 again, one layer down: not an unset variable this time, but an
    uninstalled toolchain, which no audit in this file could see.

    So: for every (test file, tier) pair, some CI step must both RUN the file
    and set `REVL_REQUIRE_TIERS` to a value covering that tier -- which turns
    the absent-toolchain skip into a failure there. Anything else is an
    enumerated hole in `_TIER_NOT_REQUIRED_IN_CI`, with a reason.
    """
    steps = _require_steps()
    missing: list[str] = []

    for path, tiers in sorted(_tier_test_files().items()):
        rel = path.relative_to(ROOT).as_posix()
        for tier in sorted(tiers):
            if tier in _TIER_NOT_REQUIRED_IN_CI:
                continue
            covered = any(
                tier in required and _covers(target, rel)
                for _job, run, required in steps
                for target in _pytest_targets(run)
            )
            if not covered:
                missing.append(f"  {rel} [{tier}]")

    assert not missing, (
        "these tier tests are not run by any CI step that sets "
        f"{_REQUIRE_ENV} for the tier they drive, so wherever that toolchain is "
        "absent they SKIP, and a skip is green:\n"
        + "\n".join(missing)
        + "\n\nA test named `..._executes[ts]` that executes nothing and reports "
        "success is issue #266, and items 430/433/445 before it. Add the file to "
        f"the CI step that provisions the tier and sets {_REQUIRE_ENV} (the "
        "`conformance` job installs the cordis-py runtime and runs `npm ci`), or "
        "enumerate the tier in _TIER_NOT_REQUIRED_IN_CI with a reason."
    )


def test_no_stale_tier_exemptions():
    """The tier-exemption registry may only shrink, like every other one here."""
    driven = set().union(*_tier_test_files().values()) if _tier_test_files() else set()
    for tier in sorted(_TIER_NOT_REQUIRED_IN_CI):
        assert tier in _TIERS, f"{tier} is not a revl tier. Drop the entry."
        assert tier in driven, (
            f"{tier} is exempted from {_REQUIRE_ENV} but no test in tests/ drives "
            f"it any more. Drop the entry rather than leaving the excuse behind."
        )
    for _job, _run, required in _require_steps():
        now_required = sorted(set(required) & set(_TIER_NOT_REQUIRED_IN_CI))
        assert not now_required, (
            f"{now_required} are exempted from {_REQUIRE_ENV} but a CI step now "
            "sets it for them. The gap closed; drop the entries."
        )


def test_the_switch_is_wired_in_ci_at_all():
    """`REVL_REQUIRE_TIERS` is read in src/revl/, not tests/, so the scanners at
    the top of this file cannot see it. Check it directly: it must be set by at
    least one step, and every tier it names must be a real tier."""
    steps = _require_steps()
    assert steps, (
        f"{_REQUIRE_ENV} is set by no CI step. It is the only thing standing "
        "between a tier test and a green run that executed nothing (issue #266); "
        "if it is genuinely gone, delete this test deliberately and say so on "
        "the roadmap. Do not let it lapse."
    )
    for job, _run, required in steps:
        bogus = sorted(set(required) - _TIERS)
        assert not bogus, (
            f"the `{job}` job sets {_REQUIRE_ENV} to unknown tier(s) {bogus}. "
            f"A misspelled tier name requires nothing and reads as if it does."
        )


def test_this_guard_cannot_itself_skip():
    """A guard that skips when the thing it guards skips is worth nothing.

    This file audits silent skips, and it reached for PyYAML through
    `pytest.importorskip` -- so on the `frontend` matrix, which installs no
    PyYAML, the two checks that pair a gate against the job running it reported
    SKIPPED, which is green (issue #266). The import is hard now.

    Keep it that way: nothing in this module may make its own checks
    conditional. Every dependency it needs is in the `test` extra.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    banned = {"importorskip", "skip", "xfail", "skipif", "importorskip_module"}
    used: set[str] = set()
    for node in ast.walk(tree):
        # a CALL to pytest.skip/importorskip/xfail, or a REFERENCE to
        # pytest.mark.skip/skipif/xfail as a decorator. Prose and comments
        # naming them (this docstring does) are not code and do not count.
        if isinstance(node, ast.Attribute) and node.attr in banned:
            used.add(node.attr)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in banned:
            used.add(node.func.id)
    assert not used, (
        f"this file uses {sorted(used)}, which lets one of its own checks "
        "report SKIPPED. A skip here is indistinguishable from a pass and "
        "hides exactly the class this file exists to catch. Make the "
        "dependency hard (add it to the `test` extra in pyproject.toml) or "
        "compute the answer without it."
    )
    # and the extra really does carry the one dependency it imports
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r"^test = \[[^\]]*pyyaml", pyproject, re.MULTILINE), (
        "PyYAML is imported at the top of this file but is not in the `test` "
        "extra, so a plain `pip install -e '.[test]'` job cannot even collect "
        "it. Put it back in pyproject.toml."
    )
