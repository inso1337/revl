#!/usr/bin/env python3
"""Content-keyed result cache for selected pytest files (issue #2857).

WHY THIS EXISTS. `tools/affected_tests.py` answers WHICH tests a diff can
affect. Rerunning that same selection pays for every selected test again even
when neither the test nor anything it observes has changed. This tool runs a
selection through the repository's pytest invocation and remembers a GREEN
result keyed by content, so a second unchanged run executes nothing.

DECLARING INPUTS. A test module opts in with a module-level assignment:

    CACHE_INPUTS = ("tests/fixtures/input.json", "src/revl/parser.py")

Paths are repository-relative regular files. `CACHE_INPUTS = ()` declares that
the module observes nothing beyond its own source. A module with no declaration
has UNKNOWN inputs and always reruns; so does a module whose declaration is not
a single literal tuple/list of strings, has any other static binding of the
name anywhere in the module, or names a missing, absolute, escaping, symlinked,
directory, or unreadable input. That is the acceptance-critical direction: if
an input cannot be hashed, rerun.

THE KEY. The digest covers the test file's content hash, the declared input
hashes, the pytest configuration and conftest/__init__ files pytest loads for
that test, the checkout root, and the Python and pytest versions. A green entry
is served only when its stored key material still matches the freshly computed
one; an entry whose input hash changed cannot be served. Failures write no
entry, and an entry for a target that fails is removed. A target that collected
no test, or whose every collected test skipped, is not green and is not cached.

The child pytest process drops every PYTEST_* environment control, so an option
that deselects a failing test cannot produce a green entry for the file. A
checkout with a pytest config or conftest above --root is not cacheable at all:
pytest can load that file and it is outside the checkout's key, so the tool
reruns rather than guess.

SCOPE. This slice is a standalone tool. `tools/pre_merge.sh --affected` still
calls pytest directly; wiring the cache into that gate would also have to carry
its receipt and shard behaviour, so it is left alone here. To drive this tool
from the selector:

    python3 tools/affected_tests.py --format machine > selection.txt
    python3 tools/result_cache.py --selection selection.txt \
        --cache-dir .revl-test-cache

`--selection` also takes test targets directly. A single non-`.py` file is read
as a selection file: its `PYTEST` line when it is selector machine output, or
one or more targets per line otherwise. A test target that is not one `.py`
file (a directory, or a `file.py::node` id) runs but is never cached.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

SCHEMA = 1
KIND = "revl-test-result-cache"
DECLARATION = "CACHE_INPUTS"
EMPTY = "test-result-cache:"

#: Files at the checkout root that change how pytest collects or runs a module.
CONFIG_FILES = (
    "pyproject.toml", "pytest.ini", "pytest.toml", ".pytest.toml", ".pytest.ini",
    "setup.cfg", "tox.ini",
)
#: Pytest loads both of these from every ancestor directory of a test.
ANCESTOR_FILES = ("conftest.py", "__init__.py")


@dataclass(frozen=True)
class CacheKey:
    target: str
    material: dict
    digest: str


@dataclass(frozen=True)
class Plan:
    raw: str
    rel: str | None
    key: CacheKey | None
    note: str


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def hash_regular_file(path: Path) -> str | None:
    """The sha256 of a regular file, or None when it cannot be hashed.

    A symlink is refused even when its target is regular. The declaration is
    about a path in this checkout; following a link widens what the entry
    depends on without saying so, and the conservative answer is to rerun.
    """
    try:
        if path.is_symlink():
            return None
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _inside_root(path: Path, root: Path) -> Path | None:
    try:
        return path.resolve().relative_to(root)
    except (OSError, ValueError):
        return None


def _binding_count(tree: ast.Module) -> int:
    """How many static bindings of CACHE_INPUTS the module contains."""
    count = 0
    for node in ast.walk(tree):
        if (isinstance(node, ast.Name) and node.id == DECLARATION
                and isinstance(node.ctx, (ast.Store, ast.Del))):
            count += 1
        elif isinstance(node, ast.arg) and node.arg == DECLARATION:
            count += 1
        elif isinstance(node, ast.alias) and (
                node.asname == DECLARATION
                or (node.asname is None and node.name == DECLARATION)):
            count += 1
        elif (isinstance(node, ast.ExceptHandler)
              and node.name == DECLARATION):
            count += 1
        elif (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.ClassDef))
              and node.name == DECLARATION):
            count += 1
        elif (isinstance(node, ast.ImportFrom)
              and any(alias.name == "*" for alias in node.names)):
            count += 1
    return count


def declared_inputs(test_path: Path) -> tuple[str, ...] | None:
    """The test module's `CACHE_INPUTS`, or None when the declaration is unknown.

    Only a single top-level assignment to a tuple/list of string constants is
    understood. Anything dynamic, repeated, annotated away, or not a string is
    UNKNOWN, not an empty declaration.
    """
    try:
        text = test_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        tree = ast.parse(text, filename=str(test_path))
    except SyntaxError:
        return None
    values = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [target.id for target in node.targets
                     if isinstance(target, ast.Name)]
            if DECLARATION in names:
                values.append(node.value)
        elif isinstance(node, ast.AnnAssign):
            if (isinstance(node.target, ast.Name)
                    and node.target.id == DECLARATION
                    and node.value is not None):
                values.append(node.value)
    # The name must have exactly one static binding in the whole module: the
    # top-level assignment above. A nested reassignment, a parameter, an import
    # alias, a function or class name, a destructuring target or a star import
    # can change what the module actually observes, and none of them is visible
    # in tree.body alone.
    if len(values) != 1 or _binding_count(tree) != 1:
        return None
    value = values[0]
    if not isinstance(value, (ast.Tuple, ast.List)):
        return None
    inputs = []
    for element in value.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            return None
        if not element.value:
            return None
        inputs.append(element.value)
    return tuple(dict.fromkeys(inputs))


def _resolve_input(root: Path, declared: str) -> Path | None:
    candidate = Path(declared)
    if candidate.is_absolute():
        return None
    target = _inside_root(root / candidate, root)
    if target is None:
        return None
    if (root / candidate).is_symlink():
        return None
    return root / target


def _ancestor_dirs(root: Path, test_rel: Path) -> list[Path]:
    start = (root / test_rel).parent
    directories = [start]
    while start != root and root in start.parents:
        start = start.parent
        directories.append(start)
    return directories


def runner_inputs(root: Path, test_rel: Path) -> tuple[str, ...]:
    """Files pytest loads around a test besides the test and its declared inputs.

    These are not declared by the test author: pytest can read a config and a
    conftest/__init__ file from any ancestor of the test. Hashing every one
    means an edit there invalidates the entries it can affect. A file that
    exists but cannot be hashed makes the whole key unknown, which reruns.
    """
    found: set[str] = set()
    for directory in _ancestor_dirs(root, test_rel):
        # Pytest can select an inifile in any ancestor of the test, not only at
        # the checkout root, so every accepted config name is hashed at every
        # level between the test and the root.
        for name in CONFIG_FILES:
            path = directory / name
            if path.is_symlink() or path.exists():
                found.add(path.relative_to(root).as_posix())
        for name in ANCESTOR_FILES:
            path = directory / name
            if path.is_symlink() or path.exists():
                found.add(path.relative_to(root).as_posix())
    return tuple(sorted(found))


def external_runner_config(root: Path) -> Path | None:
    """A pytest config or conftest above `root`, or None when there is none.

    Pytest loads conftest files from the ancestors of a test path, and can pick
    up an inifile there too. Those files are outside the checkout root, so the
    key does not cover them. Refusing to cache is the conservative answer.
    """
    current = root.parent
    while True:
        for name in (*CONFIG_FILES, "conftest.py"):
            candidate = current / name
            if candidate.is_symlink() or candidate.exists():
                return candidate
        if current.parent == current:
            return None
        current = current.parent


def _pytest_version() -> str:
    try:
        return metadata.version("pytest")
    except metadata.PackageNotFoundError:
        return "unknown"


def build_key(rel: str, root: Path) -> tuple[CacheKey | None, str]:
    """The content key for one test file, or (None, reason) when it is unknown."""
    external = external_runner_config(root)
    if external is not None:
        return None, f"pytest config above root: {external}"
    test_path = root / rel
    test_digest = hash_regular_file(test_path)
    if test_digest is None:
        return None, "test file cannot be hashed"
    declared = declared_inputs(test_path)
    if declared is None:
        return None, f"no {DECLARATION} declaration"
    declared_hashes: dict[str, str] = {}
    for item in declared:
        resolved = _resolve_input(root, item)
        if resolved is None:
            return None, f"declared input cannot be resolved: {item}"
        digest = hash_regular_file(resolved)
        if digest is None:
            return None, f"declared input cannot be hashed: {item}"
        declared_hashes[item] = digest
    runner_hashes: dict[str, str] = {}
    for item in runner_inputs(root, Path(rel)):
        digest = hash_regular_file(root / item)
        if digest is None:
            return None, f"runner input cannot be hashed: {item}"
        runner_hashes[item] = digest
    material = {
        "schema": SCHEMA,
        "kind": KIND,
        "root": str(root),
        "target": rel,
        "test_sha256": test_digest,
        "declared_inputs": declared_hashes,
        "runner_inputs": runner_hashes,
        "python": sys.version,
        "pytest": _pytest_version(),
    }
    digest = hashlib.sha256(_canonical(material)).hexdigest()
    return CacheKey(target=rel, material=material, digest=digest), ""


def _entry_path(cache_dir: Path, digest: str) -> Path:
    return cache_dir / f"{digest}.json"


def load_green(cache_dir: Path, key: CacheKey) -> bool:
    """True only for a well-formed green entry whose stored material matches."""
    try:
        raw = _entry_path(cache_dir, key.digest).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    return (data.get("schema") == SCHEMA
            and data.get("kind") == KIND
            and data.get("outcome") == "green"
            and data.get("key") == key.material)


def write_green(cache_dir: Path, key: CacheKey, seconds: float) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "schema": SCHEMA,
        "kind": KIND,
        "outcome": "green",
        "key": key.material,
        "duration_seconds": round(seconds, 3),
    }
    target = _entry_path(cache_dir, key.digest)
    temporary = target.with_name(f"{target.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n",
                         encoding="utf-8")
    os.replace(temporary, target)


def remove_entry(cache_dir: Path, key: CacheKey) -> None:
    try:
        _entry_path(cache_dir, key.digest).unlink()
    except OSError:
        pass


def still_current(root: Path, key: CacheKey) -> bool:
    fresh, _ = build_key(key.target, root)
    return fresh is not None and fresh.digest == key.digest


def _target_rel(raw: str, root: Path) -> tuple[str | None, str]:
    if "::" in raw:
        return None, "a node id is not one test file"
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    if candidate.is_symlink():
        return None, "test file is a symlink"
    if not candidate.is_file():
        return None, "selection is not one test file"
    try:
        rel = candidate.resolve().relative_to(root)
    except (OSError, ValueError):
        return None, "test file is outside the checkout"
    if rel.suffix != ".py":
        return None, "selection is not a .py test file"
    return rel.as_posix(), ""


def plan_target(raw: str, root: Path, cache_dir: Path) -> Plan:
    rel, reason = _target_rel(raw, root)
    if rel is None:
        return Plan(raw, None, None, reason)
    key, reason = build_key(rel, root)
    if key is None:
        return Plan(raw, rel, None, reason)
    if load_green(cache_dir, key):
        return Plan(raw, rel, key, "green entry")
    return Plan(raw, rel, key, "no green entry for the current inputs")


def _selection_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    machine: list[str] = []
    saw_pytest = False
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("PYTEST "):
            saw_pytest = True
            machine.extend(shlex.split(line[len("PYTEST "):]))
        elif line.startswith(("FULL ", "REASON ", "BACKENDS ", "GATES ")):
            continue
        else:
            lines.extend(shlex.split(line))
    return machine if saw_pytest else lines


def expand_selection(values: list[str]) -> list[str]:
    """Targets, or the contents of a single non-`.py` selection file."""
    if len(values) == 1:
        candidate = Path(values[0])
        if candidate.is_file() and candidate.suffix != ".py":
            return _selection_file(candidate)
    return list(values)


def _pytest_env(root: Path) -> dict[str, str]:
    # PYTEST_ADDOPTS can deselect a failing test, PYTEST_PLUGINS can change
    # collection, and the rest can change the runner. Dropping every control
    # keeps one command line behind every green entry.
    env = {name: value for name, value in os.environ.items()
           if not name.startswith("PYTEST_")}
    source = root / "src"
    if source.is_dir():
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(source) + (
            os.pathsep + existing if existing else "")
    return env


def _junit_outcomes(xml_path: Path, root: Path) -> dict[str, dict[str, int]]:
    outcomes: dict[str, dict[str, int]] = {}
    try:
        tree = ET.parse(xml_path)
    except (OSError, ET.ParseError):
        return outcomes
    for case in tree.iter("testcase"):
        raw = case.get("file") or ""
        if not raw:
            continue
        path = Path(raw)
        if path.is_absolute():
            try:
                name = path.resolve().relative_to(root).as_posix()
            except (OSError, ValueError):
                continue
        else:
            name = os.path.normpath(raw).replace(os.sep, "/")
        if case.find("failure") is not None:
            outcome = "failed"
        elif case.find("error") is not None:
            outcome = "errors"
        elif case.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        counts = outcomes.setdefault(name, {
            "passed": 0, "failed": 0, "errors": 0, "skipped": 0})
        counts[outcome] += 1
    return outcomes


def _summary(reused: int, executed: int, uncacheable: int, wrote: int) -> None:
    print(f"{EMPTY} reused={reused} executed={executed} "
          f"uncacheable={uncacheable} wrote={wrote}")


def run(selection: list[str], root: Path, cache_dir: Path) -> int:
    tokens = expand_selection(selection)
    if not tokens:
        print(f"{EMPTY} FAIL no test targets in the selection")
        return 2
    plans: list[Plan] = []
    seen: set[str] = set()
    for raw in tokens:
        identity = raw
        plan = plan_target(raw, root, cache_dir)
        if plan.rel is not None:
            identity = plan.rel
        if identity in seen:
            continue
        seen.add(identity)
        plans.append(plan)

    reused: list[Plan] = []
    execute: list[Plan] = []
    for plan in plans:
        # Checked again here, after planning: a file that moved between the two
        # reads must run rather than be served an entry for content that is no
        # longer on disk.
        if (plan.key is not None and load_green(cache_dir, plan.key)
                and still_current(root, plan.key)):
            reused.append(plan)
        else:
            execute.append(plan)
    uncacheable = [plan for plan in execute if plan.key is None]

    print(f"{EMPTY} root={root} cache={cache_dir}")
    for plan in reused:
        print(f"{EMPTY} reuse {plan.raw}")
    for plan in execute:
        if plan.key is None:
            print(f"{EMPTY} uncacheable {plan.raw}: {plan.note}")
        else:
            print(f"{EMPTY} execute {plan.raw} ({plan.note})")

    if not execute:
        _summary(len(reused), 0, 0, 0)
        print(f"{EMPTY} executed nothing; {len(reused)} green entr"
              f"{'y' if len(reused) == 1 else 'ies'} reused")
        return 0

    xml: Path | None = None
    with tempfile.TemporaryDirectory(prefix="revl-test-cache-") as temporary:
        xml = Path(temporary) / "results.xml"
        command = [
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            f"--rootdir={root}", "-o", "junit_family=xunit1",
            f"--junitxml={xml}",
            *[plan.rel if plan.rel is not None else plan.raw
              for plan in execute],
        ]
        started = time.monotonic()
        proc = subprocess.run(command, cwd=root, env=_pytest_env(root),
                              text=True, capture_output=True, check=False)
        elapsed = time.monotonic() - started
        outcomes = _junit_outcomes(xml, root) if xml.is_file() else {}

    failed_cases = sum(c["failed"] + c["errors"] for c in outcomes.values())
    if proc.returncode != 0 or failed_cases:
        print(f"{EMPTY} FAIL pytest exited {proc.returncode} "
              f"with {failed_cases} failed/error case(s)")
        if proc.stdout.strip():
            print("\n".join(proc.stdout.strip().splitlines()[-20:]))
        if proc.stderr.strip():
            print("\n".join(proc.stderr.strip().splitlines()[-20:]))
        for plan in execute:
            if plan.key is not None:
                remove_entry(cache_dir, plan.key)
        _summary(len(reused), len(execute), len(uncacheable), 0)
        return proc.returncode or 1

    passed = sum(counts["passed"] for counts in outcomes.values())
    if passed == 0:
        print(f"{EMPTY} FAIL no test passed; a green entry needs an executed, "
              "passing test")
        _summary(len(reused), len(execute), len(uncacheable), 0)
        return 1

    wrote = 0
    for plan in execute:
        if plan.key is None or plan.rel is None:
            continue
        counts = outcomes.get(plan.rel, {})
        if not counts.get("passed"):
            print(f"{EMPTY} not cached {plan.raw}: no passing test collected "
                  "for that file")
            continue
        if not still_current(root, plan.key):
            print(f"{EMPTY} not cached {plan.raw}: its inputs changed during "
                  "the run")
            continue
        write_green(cache_dir, plan.key, elapsed)
        wrote += 1

    _summary(len(reused), len(execute), len(uncacheable), wrote)
    return 0


def _default_root() -> Path:
    cwd = Path.cwd()
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=cwd,
            text=True, capture_output=True, check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return Path(proc.stdout.strip()).resolve()
    except OSError:
        pass
    return cwd.resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reuse green pytest results keyed by test and input content.")
    parser.add_argument("--selection", required=True, nargs="+", action="extend",
                        metavar="TARGET_OR_FILE",
                        help="test files/node ids, or one selection file")
    parser.add_argument("--cache-dir", required=True,
                        help="directory for content-keyed green entries")
    parser.add_argument("--root", default=None,
                        help="checkout root (default: git toplevel, else cwd)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve() if args.root else _default_root()
    cache_dir = Path(args.cache_dir).resolve()
    try:
        return run(list(args.selection), root, cache_dir)
    except (OSError, ValueError) as exc:
        print(f"{EMPTY} FAIL {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
