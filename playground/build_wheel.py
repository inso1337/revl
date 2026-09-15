#!/usr/bin/env python3
"""Build a pure-Python wheel for revl, straight from the in-tree source.

The playground loads the compiler *in-process* under Pyodide (micropip
installs this wheel), so it needs a wheel but the environment's pip/build
may be unusable. revl is pure Python, so a wheel is just a zip with a
`.dist-info` — this constructs it directly from `src/revl`, no build
backend required.

Run from anywhere:  python3 playground/build_wheel.py
Output:             playground/vendor/revl-<version>-py3-none-any.whl
"""
from __future__ import annotations

import base64
import hashlib
import re
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = Path(__file__).resolve().parent / "vendor"

# The trees this wheel vendors, as (repo-relative tree, glob, arcname prefix).
# This table is the SINGLE SOURCE OF TRUTH for "what makes the committed wheel
# stale", in both directions: `main()` builds the wheel from exactly this, and
# `tools/affected_tests.py` is held to `wheel_inputs()` by
# tests/test_affected_tests.py, so a tree added here without a matching gate
# rule reds that test instead of quietly rotting the committed wheel.
#
# The backends glob is TOP-LEVEL `*.py` on purpose, not a recursive walk:
# `revl/backends/python/<name>.py` is the flat layout `_paths.backends_root()`
# expects in an installed wheel, so subdirectories (golden/, tests/) are not
# vendored and are not inputs.
SOURCE_TREES = (
    ("src/revl", "**/*.py", "revl/"),
    ("backends/python", "*.py", "revl/backends/python/"),
)

# Files the wheel does not vendor but whose content still changes its bytes:
# pyproject supplies the version that names every `.dist-info` member, and this
# builder decides the member set and the metadata text.
META_INPUTS = ("pyproject.toml", "playground/build_wheel.py")


def _tracked(tree: Path) -> set[str] | None:
    """The repo-relative paths git tracks under `tree`, or None without git.

    GHSA-gj88-cx6q-38r2 was the PyPI wheel force-including whatever sat on the
    builder's disk. This wheel is a different artifact with a different
    distribution path (it is committed, and `tools/check_site_wheel.py` gates it
    against a fresh build), but the selection had the same shape: `rglob("*.py")`
    is a question about the filesystem, not about the commit, so a developer's
    untracked scratch module under `src/revl/` — or a stray `__pycache__/x.py` —
    rode into the wheel the playground serves. Asking git instead makes this
    wheel a function of the commit too.

    Returning None (no git, not a checkout) leaves the caller on its glob, which
    is what this file did before; the committed-wheel drift gate is the backstop
    there.
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--", str(tree.relative_to(ROOT))],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    paths = {p for p in proc.stdout.split("\0") if p}
    return paths or None


def _members() -> list[tuple[Path, str]]:
    """(source path, arcname) for every tracked file the wheel vendors.

    In wheel order, and filtered through `_tracked` exactly as before: an
    untracked module under one of the trees is on the filesystem but not in the
    commit, so it is not an input and does not ride into the wheel.
    """
    out: list[tuple[Path, str]] = []
    for rel_tree, pattern, prefix in SOURCE_TREES:
        tree = ROOT / rel_tree
        tracked = _tracked(tree)
        for path in sorted(tree.glob(pattern)):
            rel = path.relative_to(ROOT).as_posix()
            if tracked is not None and rel not in tracked:
                continue
            out.append((path, prefix + path.relative_to(tree).as_posix()))
    return out


def wheel_inputs() -> list[str]:
    """Repo-relative paths whose content this builder reads.

    A change to any of them makes the committed `playground/vendor` and
    `site/vendor` wheels stale, so `tools/affected_tests.py` has to select the
    `site-wheel` gate for each one. Derived from the same table `main()` builds
    from, so the two cannot drift: this is not a second copy of the list.
    """
    return [p.relative_to(ROOT).as_posix() for p, _ in _members()] + list(META_INPUTS)


def _version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "0.0.0"


def _record_line(arcname: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return f"{arcname},sha256={digest.decode()},{len(data)}"


def main() -> None:
    version = _version()
    dist_info = f"revl-{version}.dist-info"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    wheel_path = OUT_DIR / f"revl-{version}-py3-none-any.whl"

    metadata = (
        "Metadata-Version: 2.1\n"
        "Name: revl\n"
        f"Version: {version}\n"
        "Summary: A research language for spatiotemporal composability (Cordis paradigm)\n"
        "Requires-Python: >=3.11\n"
    )
    wheel_meta = (
        "Wheel-Version: 1.0\n"
        "Generator: revl-playground-build_wheel\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    )

    records: list[str] = []
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as whl:
        # `src/revl/**` plus the py-tier runtime glue (emit/runtime/replay/...),
        # the latter packaged where `_paths.backends_root()` finds it in an
        # installed wheel (revl/backends) — the same layout pyproject's wheel
        # target ships, scoped to the one tier the in-browser session can boot.
        # With the cordis wheel installed beside it, `revl.mcp.session.Session`
        # runs load/call/swap/unload entirely client-side.
        for path, arcname in _members():
            data = path.read_bytes()
            whl.writestr(arcname, data)
            records.append(_record_line(arcname, data))

        for arcname, text in (
            (f"{dist_info}/METADATA", metadata),
            (f"{dist_info}/WHEEL", wheel_meta),
        ):
            data = text.encode("utf-8")
            whl.writestr(arcname, data)
            records.append(_record_line(arcname, data))

        record_name = f"{dist_info}/RECORD"
        records.append(f"{record_name},,")
        whl.writestr(record_name, "\n".join(records) + "\n")

    print(f"wrote {wheel_path.relative_to(ROOT)} ({wheel_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
