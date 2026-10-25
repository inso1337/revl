"""Issue #2872: the push-to-main path filters cover every input of their job.

A push to main ran six workflows against one runner pool. Three of them now run
on a push only when it touches their inputs: the two smoke lanes (`x64-smoke`,
`arm64-smoke`) and `site wheel`. A path filter that misses an input does not
fail, it silently stops testing that input on push, so each filter is checked
here against inputs DERIVED from what the job runs, not against a second
hand-written copy:

* the smokes: the test files named in the job's pytest command, their import
  closure inside the repo (helpers and conftests), every file they reach through
  a literal `ROOT / "..."` path, the trees the compiler reads at run time
  (`revl._paths`: `stdlib/`, `backends/`; `selfhost/` is compiled), and the
  install inputs.
* site wheel: `playground/build_wheel.py`'s `wheel_inputs()` (the same table the
  builder vendors from) and `tools/check_site_wheel.py`'s CONSUMERS,
  PAGES_WORKFLOW, BUILDERS and TARGETS, plus every script the job runs.

Each filter's list is also pinned exactly, so widening or narrowing it is a
deliberate edit here too, and each workflow must keep its schedule as the
backstop that still runs it on every tree.
"""
from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

SMOKE_PATHS = [
    "src/**",
    "stdlib/**",
    "selfhost/**",
    "backends/**",
    "tests/fixtures/**",
    "tests/noncensus_corpus/**",
    "examples/**",
    "bench/codegen/go/probe2.rvl",
    "bench/codegen/python/programs/maps.rvl",
    "bench/results/rerun-deepseek-v4-pro-20260826/12-replicator/v2/attempt-1.rvl",
    "tests/_*.py",
    "tests/conftest.py",
    "tests/test_v2_emit.py",
    "tests/test_selfhost_lower_ir.py",
    "tests/test_selfhost_emit_py.py",
    "tests/test_py_comment_only_blocks_2284.py",
    "conftest.py",
    "pyproject.toml",
    "hatch_build.py",
]
SITE_WHEEL_PATHS = [
    "src/revl/**",
    "backends/python/*.py",
    "pyproject.toml",
    "playground/**",
    "site/js/playground.js",
    "site/build.py",
    "site/vendor/**",
    "tools/check_site_wheel.py",
    ".github/workflows/pages.yml",
    ".github/workflows/site-wheel.yml",
]
FILTERED = {
    "x64-smoke.yml": SMOKE_PATHS + [".github/workflows/x64-smoke.yml"],
    "arm64-smoke.yml": SMOKE_PATHS + [".github/workflows/arm64-smoke.yml"],
    "site-wheel.yml": SITE_WHEEL_PATHS,
}
# Trees the compiler reads off the checkout at run time (revl._paths
# backends_root/stdlib_root) or that the smoke tests compile.
SMOKE_RUNTIME_TREES = ("src", "stdlib", "backends", "selfhost")
SMOKE_INSTALL_INPUTS = ("pyproject.toml", "hatch_build.py")


# --- GitHub's filter-pattern glob ------------------------------------------ #
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """`*` is any run without `/`, `**` any run at all, `?` one char."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def matches(path: str, patterns: list[str]) -> bool:
    return any(_glob_regex(p).match(path) for p in patterns)


def test_glob_matcher_follows_github_semantics():
    assert matches("src/revl/a/b.py", ["src/**"])
    assert matches("backends/python/emit.py", ["backends/python/*.py"])
    assert not matches("backends/python/golden/x.py", ["backends/python/*.py"])
    assert matches("tests/_compiled_ir.py", ["tests/_*.py"])
    assert not matches("tests/test_x.py", ["tests/_*.py"])
    assert not matches("docs/src/x.md", ["src/**"])


# --- workflow and repo readers ---------------------------------------------- #
@lru_cache(maxsize=None)
def _workflow(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    if True in data and "on" not in data:  # YAML 1.1 reads a bare `on` as True
        data["on"] = data.pop(True)
    return data


def _push_paths(name: str) -> list[str] | None:
    push = _workflow(name)["on"].get("push") or {}
    return push.get("paths")


def _run_text(name: str) -> str:
    return "\n".join(
        step.get("run", "")
        for job in _workflow(name)["jobs"].values()
        for step in job.get("steps", []))


@lru_cache(maxsize=None)
def _tracked() -> tuple[str, ...]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return tuple(p for p in out.split("\0") if p)


def _tracked_under(rel: str) -> list[str]:
    rel = rel.rstrip("/")
    return [p for p in _tracked() if p == rel or p.startswith(rel + "/")]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- the smokes' inputs ----------------------------------------------------- #
_ROOT_PATH_RE = re.compile(r'\bROOT((?:\s*/\s*"[^"]+")+)')
_UP_RE = re.compile(r'"((?:\.\./)+[\w/.-]+)"')


def _literal_root_paths(source: str) -> list[str]:
    return ["/".join(re.findall(r'"([^"]+)"', chain))
            for chain in _ROOT_PATH_RE.findall(source)]


def _local_imports(path: Path) -> list[Path]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        for name in names:
            top = name.split(".")[0]
            for cand in (ROOT / "tests" / f"{top}.py", ROOT / f"{top}.py"):
                if cand.is_file():
                    found.append(cand)
    return found


def smoke_inputs(name: str) -> set[str]:
    run = _run_text(name)
    tests = sorted(set(re.findall(r"\btests/[\w/]+\.py\b", run)))
    assert tests, f"{name} no longer names its test files; this pin is stale"
    seeds = [ROOT / t for t in tests] + [ROOT / "conftest.py",
                                         ROOT / "tests" / "conftest.py"]
    seen: set[Path] = set()
    todo = [p for p in seeds if p.is_file()]
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        todo.extend(_local_imports(path))
    inputs = {p.relative_to(ROOT).as_posix() for p in seen}
    for path in seen:
        source = path.read_text(encoding="utf-8")
        dirs = []
        for rel in _literal_root_paths(source):
            # `ROOT / "tests"` alone is a sys.path entry: what it reaches is the
            # import closure above, not every file under tests/.
            if rel != "tests":
                inputs.update(_tracked_under(rel))
                if (ROOT / rel).is_dir():
                    dirs.append(ROOT / rel)
        # Corpus entries reach outside their directory with `../` names
        # (`"../../noncensus_corpus/x.rvl"`); resolve them against each one.
        for up in _UP_RE.findall(source):
            for base in dirs:
                target = (base / up).resolve()
                if target.is_file():
                    inputs.add(target.relative_to(ROOT).as_posix())
    for tree in SMOKE_RUNTIME_TREES:
        inputs.update(_tracked_under(tree))
    inputs.update(SMOKE_INSTALL_INPUTS)
    inputs.add(f".github/workflows/{name}")
    return inputs


@pytest.mark.parametrize("name", ["x64-smoke.yml", "arm64-smoke.yml"])
def test_smoke_filter_covers_every_input(name):
    paths = _push_paths(name)
    assert paths, f"{name}: push to main is not path-filtered (issue #2872)"
    inputs = smoke_inputs(name)
    assert len(inputs) > 100, f"{name}: input derivation found almost nothing"
    missing = sorted(p for p in inputs if not matches(p, paths))
    assert not missing, (
        f"{name} reads these on a push but its `paths:` filter does not cover "
        f"them, so a change to one would no longer be smoke-tested:\n  "
        + "\n  ".join(missing[:40]))


# --- site wheel's inputs ---------------------------------------------------- #
def site_wheel_inputs() -> set[str]:
    bw = _load("build_wheel_2872", ROOT / "playground" / "build_wheel.py")
    csw = _load("check_site_wheel_2872", ROOT / "tools" / "check_site_wheel.py")
    inputs = set(bw.wheel_inputs())
    inputs.update(Path(p).as_posix() for p in csw.CONSUMERS)
    inputs.add(Path(csw.PAGES_WORKFLOW).as_posix())
    inputs.update(cmd.split()[0] for cmd in csw.BUILDERS)
    # The check writes the build into, and globs the wheel back out of, these.
    for target in csw.TARGETS:
        rel = Path(target).relative_to(ROOT).as_posix()
        inputs.add(f"{rel}/revl-0-py3-none-any.whl")
        inputs.update(_tracked_under(rel))
    inputs.update(re.findall(r"python3?\s+([\w/.-]+\.py)", _run_text("site-wheel.yml")))
    inputs.add("playground/build_wheel.py")
    inputs.add(".github/workflows/site-wheel.yml")
    return inputs


def test_site_wheel_filter_covers_every_input():
    paths = _push_paths("site-wheel.yml")
    assert paths, "site-wheel.yml: push to main is not path-filtered (issue #2872)"
    inputs = site_wheel_inputs()
    assert any(p.startswith("src/revl/") for p in inputs)
    assert any(p.startswith("backends/python/") for p in inputs)
    missing = sorted(p for p in inputs if not matches(p, paths))
    assert not missing, (
        "site-wheel.yml's check reads these but its `paths:` filter does not "
        "cover them, so a change to one would no longer be checked on push:\n  "
        + "\n  ".join(missing[:40]))


# --- shape pins ------------------------------------------------------------- #
@pytest.mark.parametrize("name", sorted(FILTERED))
def test_filter_list_is_pinned(name):
    assert _push_paths(name) == FILTERED[name]


@pytest.mark.parametrize("name", sorted(FILTERED))
def test_filtered_workflow_keeps_its_schedule_backstop(name):
    on = _workflow(name)["on"]
    assert on.get("schedule"), f"{name} lost the schedule that runs it on every tree"
    assert "workflow_dispatch" in on
    assert (on.get("push") or {}).get("branches") == ["main"]


@pytest.mark.parametrize("name", sorted(FILTERED))
def test_docs_only_push_skips_it(name):
    """The issue's acceptance: a docs-only push runs no smoke or packaging job."""
    paths = _push_paths(name)
    assert paths, f"{name}: push to main is not path-filtered (issue #2872)"
    for path in ("docs/ci.md", "README.md", "docs/research/x/draft.md"):
        assert not matches(path, paths), (name, path)
