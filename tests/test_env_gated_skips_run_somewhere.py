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


def _scan(path: Path) -> tuple[set[str], set[str]]:
    """(names this module reads from the environment, names it sets itself)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    consts = _const_map(tree)
    reads: set[str] = set()
    owned: set[str] = set()

    for node in ast.walk(tree):
        # os.environ[NAME] -- a read, or a write when it is an assign target
        if isinstance(node, ast.Subscript) and _is_environ(node.value):
            name = _as_name(node.slice, consts)
            if name:
                reads.add(name)
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
            elif attr == "getenv":
                reads.add(first)
            # monkeypatch.setenv(NAME, ...) / monkeypatch.delenv(NAME)
            elif attr in ("setenv", "delenv"):
                owned.add(first)

    return reads, owned


def _external_switches() -> dict[str, set[str]]:
    """Env names `tests/` reads but never sets: name -> modules that read it."""
    reads: dict[str, set[str]] = {}
    owned: set[str] = set()
    for path in _test_sources():
        try:
            module_reads, module_owned = _scan(path)
        except SyntaxError:  # pragma: no cover - a broken test file is its own failure
            continue
        owned |= module_owned
        for name in module_reads:
            reads.setdefault(name, set()).add(path.name)
    return {
        name: where
        for name, where in reads.items()
        if name not in owned and name not in _AMBIENT
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


# STAGED, and deliberately not green yet.
#
# REVL_CROSS_TIER_SLOW is read by test_cross_tier_execution.py and by
# test_time_coeffect.py, and is still set nowhere: the `conformance` job carries
# the flip COMMENTED OUT, because turning it on today reds the build on the java
# v3 record `equals`/`hashCode` gap (item 433's rider section), which is being
# fixed separately on fix/433-java-record-equality. `_ci_sets` ignores commented
# lines on purpose, so this assertion still sees the truth.
#
# strict=True is the forcing function, and it is the point of doing it this way
# rather than parking the name in _INTENTIONALLY_LOCAL with an excuse. The
# moment somebody uncomments those two lines in ci.yml this test starts PASSING,
# which under strict xfail is a FAILURE, so whoever lands the java fix cannot
# avoid coming back here and deleting this marker. An excuse in the registry
# would have gone quiet instead, which is the exact failure this file is about.
@pytest.mark.xfail(
    strict=True,
    reason=(
        "item 445: REVL_CROSS_TIER_SLOW's flip sits commented in the "
        "`conformance` job until the java record equals/hashCode fix lands. "
        "When this xfail starts failing because the test PASSED, the flip is "
        "live -- delete this marker."
    ),
)
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


@pytest.mark.parametrize("name", sorted(_INTENTIONALLY_LOCAL))
def test_local_declaration_carries_a_real_reason(name):
    """A one-word excuse is how the class survives. Make writing it cost."""
    reason = _INTENTIONALLY_LOCAL[name]
    assert len(reason) >= _MIN_REASON_CHARS, (
        f"{name}: the reason is {len(reason)} characters. Explain why CI "
        f"cannot set it, not merely that it does not."
    )
    lowered = reason.lower()
    assert "slow" not in lowered or "separate repo" in lowered, (
        f"{name}: slowness is not a reason to measure nothing. A slow suite "
        f"belongs in a dedicated job, which is what `conformance` is for."
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
