"""Compute, per file, what `backends/` and `stdlib/` put into the wheel.

WHY THIS FILE EXISTS (GHSA-gj88-cx6q-38r2)

`pyproject.toml` used to ship those two trees with a directory-level

    [tool.hatch.build.targets.wheel.force-include]
    "backends" = "revl/backends"

hatchling applies `force-include` AFTER file selection and exempts it from
every exclude rule and from the ignore files, by design -- it exists to inject
build output that no ignore rule should be able to suppress. Pointed at a
source directory it copies that directory VERBATIM, so the wheel contained
whatever happened to be on the builder's disk: `node_modules`, cargo
`target/debug`, a cloned `.cordis-py`, compiled runner binaries, scratch logs,
and the generated key material `.gitignore` deliberately parks under
`backends/typescript/test-secret-store/`. 2933 members and 250 MB unpacked
against the 523 members the commit describes.

The size was the visible half. The half that matters is that the published
artifact was a function of the BUILDER'S FILESYSTEM rather than of the
revision: the same commit built on two machines produced two different wheels,
and no amount of reading the repository told you what a given wheel contained.
Credentials, `.env` files or private keys anywhere under `backends/` would have
been uploaded to PyPI, permanently, with nothing in the release path to notice.

WHAT IT DOES INSTEAD

`force-include` is still the mechanism -- it is the only one that can place a
non-package directory inside the wheel's `revl/` -- but this hook computes the
ENTRY LIST itself, one path at a time, instead of naming a directory and
letting the filesystem decide:

  * in a git checkout, the list is `git ls-files`. The wheel is then literally
    the tracked content of the commit being built, which is the property the
    advisory asked for and the one `tools/check_wheel_manifest.py` asserts;
  * with no git available -- a wheel built from an unpacked sdist, which is
    what `pip install revl.tar.gz` does -- it walks the trees and applies the
    deny-list from `[tool.hatch.build] exclude`. The sdist is itself built in a
    checkout under those same excludes, so the two agree in practice.

WHY NOT `only-include` + `sources`

That is the tidier spelling and it was the first fix. It cannot be used here:
a `sources` rewrite that CHANGES a path prefix (`backends` -> `revl/backends`)
rather than removing one makes editable installs impossible --

    ValueError: Dev mode installations are unsupported when any path rewrite
    in the `sources` option changes a prefix rather than removes it

-- and `pip install -e ".[test]"` is how eleven CI jobs and every contributor
set the repository up (README.md, CONTRIBUTING.md). Keeping
`packages = ["src/revl"]` leaves editable installs exactly as they were: the
`.pth` points at `src/`, nothing is force-included into site-packages, and
`revl._backends_root()` resolves the checkout sibling the way it always has.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path

try:
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface
except ImportError:  # pragma: no cover - see below
    # `tools/check_wheel_manifest.py` imports TREES from this module so the gate
    # checks the mapping the build actually applies rather than a second copy of
    # it. That gate also runs where hatchling is NOT installed -- `publish.yml`
    # and the release dry run build with `python -m build`'s isolated env and
    # then check the finished `dist/*.whl` -- so importing this module must not
    # require the build backend. The hook class below is dead weight in that
    # case; hatchling is present whenever it is actually asked to run.
    BuildHookInterface = object  # type: ignore[assignment,misc]

ROOT = Path(__file__).resolve().parent

# source tree -> where it lands inside the wheel. `revl._backends_root()` and
# `revl._paths.stdlib_root()` look for exactly these two, in this layout, once
# the checkout is gone. `tools/check_wheel_manifest.py` imports this table so
# the gate checks the intent rather than a second copy of it that has drifted.
TREES: dict[str, str] = {
    "backends": "revl/backends",
    "stdlib": "revl/stdlib",
}


def _excludes() -> list[str]:
    """The deny-list from `[tool.hatch.build] exclude`, read at build time.

    Only the no-git fallback consults it. Reading it from pyproject rather than
    duplicating it here keeps one statement of what may never ship.
    """
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    build = data.get("tool", {}).get("hatch", {}).get("build", {})
    return [str(p) for p in build.get("exclude", [])]


def _denied(relpath: str, patterns: list[str]) -> bool:
    """Does `relpath` (posix, repo-relative) match a deny pattern?

    Deliberately simple gitignore-ish matching -- `**/x` matches any segment
    named `x` and everything under it, everything else is fnmatch against the
    path and against each of its ancestor directories. It only has to be at
    least as strict as hatchling's own exclude handling, because this branch is
    the FALLBACK: the authoritative answer comes from git.
    """
    parts = relpath.split("/")
    for pattern in patterns:
        if pattern.startswith("**/"):
            tail = pattern[3:]
            if any(fnmatch.fnmatch(part, tail) for part in parts):
                return True
            continue
        if fnmatch.fnmatch(relpath, pattern):
            return True
        # a directory pattern denies everything beneath it
        for i in range(1, len(parts)):
            if fnmatch.fnmatch("/".join(parts[:i]), pattern):
                return True
    return False


def _tracked(tree: str) -> list[str] | None:
    """`git ls-files <tree>`, or None when this is not a usable checkout."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--", tree],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    paths = [p for p in proc.stdout.split("\0") if p]
    # An empty answer means git ran but this tree is not in the index (a
    # partial clone, a sparse checkout). Fall back rather than ship nothing:
    # a silently empty `revl/backends` breaks every backend command at runtime.
    return paths or None


def _walked(tree: str, patterns: list[str]) -> list[str]:
    """Every file under `tree` that the deny-list does not reject."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(ROOT / tree):
        rel_dir = Path(dirpath).relative_to(ROOT).as_posix()
        # prune denied directories in place so a `node_modules` is never even
        # descended into
        dirnames[:] = [
            d for d in dirnames if not _denied(f"{rel_dir}/{d}", patterns)
        ]
        for name in filenames:
            rel = f"{rel_dir}/{name}"
            if not _denied(rel, patterns):
                found.append(rel)
    return found


class RevlWheelContentsHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        # Editable installs get a `.pth` pointing at `src/`, so the packaged
        # copies are neither needed nor wanted there: `revl._backends_root()`
        # finds the checkout sibling. Leaving them out also keeps
        # `pip install -e .` as fast as it was.
        if version == "editable":
            return

        patterns = _excludes()
        force_include = build_data.setdefault("force_include", {})
        for tree, dest in TREES.items():
            tracked = _tracked(tree)
            paths = tracked if tracked is not None else _walked(tree, patterns)
            prefix = tree + "/"
            for rel in paths:
                if not rel.startswith(prefix):
                    continue
                source = ROOT / rel
                if not source.is_file():
                    # a tracked path that is not on disk (sparse checkout, a
                    # submodule gitlink): skip rather than fail the build
                    continue
                force_include[str(source)] = dest + "/" + rel[len(prefix) :]
