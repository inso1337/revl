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

WHAT THIS DOES NOT CATCH, stated plainly so nobody reads more into a green run
than is there. This checks env-var gates only. A skip guarded on a FILESYSTEM
probe -- `shutil.which("cargo")`, `node_modules/.bin/vitest` existing,
`find_spec("cordis")` -- is exactly as invisible and is not covered here. Item
430 was one of those, and so was the TypeScript half of the cross-tier suite
found while writing this. Those need the per-job skip audit the `backend-wasm`
job runs over its junit report, which is the right tool and a bigger job.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

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
    "CORDIS_WASM": (
        "Points at a checkout of the cordis-wasm substrate, which is a "
        "SEPARATE REPOSITORY and an explicit prototype, not a pinned "
        "dependency of this one. CI provisions the wasm tier through wasmtime "
        "plus backends/wasm instead, and the `backend-wasm` job already names "
        "this skip's exact reason string in its ALLOWED list so the exemption "
        "cannot widen silently. Setting it in CI would mean pinning a "
        "prototype's revision, which is a decision, not an oversight."
    ),
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
    yaml = pytest.importorskip(
        "yaml", reason="PyYAML is needed to read which job sets which variable"
    )
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
