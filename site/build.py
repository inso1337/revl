#!/usr/bin/env python3
"""Rebuild the site's generated assets: the compiler wheel and the examples.

Reuses playground/build_wheel.py (the wheel construction is identical), then
copies the wheel into site/vendor/. Run from anywhere:

    python3 site/build.py
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
from pathlib import Path

SITE = Path(__file__).resolve().parent
ROOT = SITE.parent

# The cordis wheel's dist-info is written from these strings rather than from
# the clone's pyproject, so they are the single source of truth for what the
# committed `site/vendor/cordis-*.whl` declares. Module-level (and not inline in
# build_cordis_wheel) because `tests/test_cordis_wheel_declares_its_deps_946.py`
# asserts the committed wheel's METADATA equals CORDIS_METADATA: #946 was
# exactly the two drifting apart, one of them silently. Keep them in step with
# backends/python/setup.sh's literal `uv pip install ... pyyaml watchdog`.
#
# Both are hard requirements, and that is a finding rather than a preference.
# `cordis/include.py` imports `yaml` at module scope, and `cordis/__init__.py`
# imports `.hmr` at module scope, whose `except ImportError` branch sets
# `FileSystemEventHandler = None` for a module-level
# `class _WatchHandler(FileSystemEventHandler)` to subclass — so the
# "zero-dependency installation" `hmr.py`'s docstring promises raises
# `TypeError: NoneType takes no arguments` on `import cordis`. Verified by
# installing this wheel into a bare venv: without pyyaml it dies in include.py,
# with pyyaml and without watchdog it dies in hmr.py, with both it imports.
# `Provides-Extra: hmr` is declared anyway because `hmr.py` documents
# `cordis[hmr]` as the install form, and an undeclared extra makes that a pip
# warning; when upstream's polling fallback works, watchdog moves under it
# without changing any consumer.
CORDIS_METADATA = (
    "Metadata-Version: 2.1\nName: cordis\nVersion: 4.0.0\n"
    "Summary: Pure Python port of cordis (spatiotemporal composability runtime)\n"
    "Requires-Python: >=3.11\n"
    "Provides-Extra: hmr\n"
    "Requires-Dist: pyyaml\n"
    "Requires-Dist: watchdog\n"
)
CORDIS_WHEEL_META = (
    "Wheel-Version: 1.0\nGenerator: revl-site-build\n"
    "Root-Is-Purelib: true\nTag: py3-none-any\n"
)

# The dist-info member that records WHICH cordis-py revision produced the
# committed wheel (issue #1029). Before this, the artifact carried no such
# record at all: `backends/python/setup.sh` hard-checks-out `CORDIS_PY_PIN`,
# the wheel is built from that checkout, and nothing tied the two together —
# so bumping the pin without re-running this script left `site/vendor/` serving
# an older runtime than the suite validated against, with nothing red. The
# offline install path the docs hand a consumer
# (`pip install site/vendor/cordis-4.0.0-py3-none-any.whl`, #967) reads that
# wheel, so the stale runtime reached them with no signal either.
#
# WHY INSIDE THE WHEEL, and inside dist-info specifically.
#
#  * A sidecar beside the wheel (`site/vendor/cordis-*.whl.rev`) does not
#    survive the artifact: copy or `pip install` the wheel and the provenance
#    is gone, which is the one moment a consumer most needs it. It can also
#    drift from the wheel it describes, recreating this same class one level up.
#  * A dist-info member survives both. `pip install` writes the whole
#    dist-info, so an installed cordis can answer for itself
#    (`importlib.metadata.distribution("cordis").read_text("REVISION")`), and
#    the member is listed in RECORD like every other one — so it cannot be
#    edited in place to claim a different revision without also rewriting
#    RECORD, which `tests/test_cordis_wheel_declares_its_deps_946.py` already
#    verifies against the member bytes.
#  * Not a new METADATA field: `CORDIS_METADATA` above is a fixed declaration
#    of what the package REQUIRES, pinned byte-for-byte by #946's test. A value
#    that changes with every repin does not belong in a constant whose whole
#    point is that it does not change silently.
#
# The value is the clone's `git rev-parse HEAD` — what was actually packaged —
# never a copy of setup.sh's literal. That is what makes the gate meaningful
# rather than a restatement: `tests/test_cordis_wheel_records_its_source_revision_1029.py`
# compares this recorded revision against `CORDIS_PY_PIN`, so a repin with no
# rebuild reds, and so does a rebuild from a clone that drifted off the pin.
#
# Nothing else is recorded. The remote URL was considered and rejected: under a
# `CORDIS_PY` override it is whatever that clone's origin happens to be, which
# would make two builds of the same source disagree and turn the fresh-build
# comparison below into noise.
CORDIS_REVISION_MEMBER = "REVISION"


def _clone_revision(clone: Path, packaged: str) -> str:
    """The exact commit `clone` is checked out at, refusing a dirty `packaged`.

    A dirty tree's HEAD does not describe the bytes about to be packaged, and a
    recorded revision that does not describe the bytes is worse than none: the
    gate would pass over an artifact that disagrees with the pin. So this
    refuses rather than recording a revision it cannot stand behind.

    Dirtiness is scoped to `packaged` (the subtree the wheel actually globs)
    rather than the whole clone. `backends/python/setup.sh` installs the clone
    editable and CI runs its suite inside it, so the working copy accumulates
    build and cache output; HEAD still describes the packaged bytes exactly as
    long as that subtree is clean. Ignored files are excluded by `git status`
    already, so this is the second line of defence rather than the first.
    """
    def git(*args: str) -> str:
        proc = subprocess.run(
            ("git", "-C", str(clone), *args),
            capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            raise SystemExit(
                f"cannot read the cordis-py revision from {clone}: "
                f"`git {' '.join(args)}` failed:\n{proc.stderr.strip()}"
            )
        return proc.stdout.strip()

    revision = git("rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise SystemExit(f"{clone} did not report a commit sha: {revision!r}")
    dirty = git("status", "--porcelain", "--", packaged)
    if dirty:
        raise SystemExit(
            f"the cordis-py clone at {clone} has uncommitted changes under "
            f"{packaged}, so {revision} would not describe the packaged bytes. "
            "Commit or reset them before building the wheel:\n" + dirty
        )
    return revision


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_cordis_wheel(vendor: Path) -> None:
    """Package the pinned cordis-py clone as a wheel.

    The playground's live mode boots compositions on the real cordis-py
    runtime under Pyodide; this wheel supplies the `cordis` package beside the
    revl wheel. Source: the `backends/python/setup.sh` clone (override with
    CORDIS_PY), which that script keeps at the tested pin.

    The wheel declares cordis's own requirements (issue #946). They were
    missing because the METADATA below is a hardcoded string, so the wheel
    installed cleanly and then died on `import cordis`. Both requirements are
    hard — see CORDIS_METADATA for why the documented `cordis[hmr]` extra is
    not yet what ships. `backends/python/setup.sh` already installs both
    unconditionally, so the requirement was known to the dev setup and absent
    from the artifact.

    The wheel also records the clone's exact revision in
    `cordis-4.0.0.dist-info/REVISION` (issue #1029), so the committed artifact
    can be checked against `CORDIS_PY_PIN` instead of being taken on trust.
    """
    clone = Path(os.environ.get("CORDIS_PY", ROOT / "backends" / "python" / ".cordis-py"))
    src = clone / "src" / "cordis"
    if not src.is_dir():
        raise SystemExit(
            f"cordis-py clone not found at {clone} — run `sh backends/python/setup.sh` "
            "first, or point CORDIS_PY at an existing clone"
        )
    bw = _load("build_wheel_mod", ROOT / "playground" / "build_wheel.py")
    dist_info = "cordis-4.0.0.dist-info"
    wheel_path = vendor / "cordis-4.0.0-py3-none-any.whl"
    import zipfile

    # Read before anything is written: a clone that cannot answer for its own
    # revision must not produce a wheel at all (see _clone_revision).
    revision = _clone_revision(clone, "src/cordis")

    metadata = CORDIS_METADATA
    wheel_meta = CORDIS_WHEEL_META
    records: list[str] = []
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as whl:
        for path in sorted(src.rglob("*.py")):
            arcname = "cordis/" + path.relative_to(src).as_posix()
            data = path.read_bytes()
            whl.writestr(arcname, data)
            records.append(bw._record_line(arcname, data))
        for arcname, text in ((f"{dist_info}/METADATA", metadata),
                              (f"{dist_info}/WHEEL", wheel_meta),
                              (f"{dist_info}/{CORDIS_REVISION_MEMBER}",
                               revision + "\n")):
            data = text.encode("utf-8")
            whl.writestr(arcname, data)
            records.append(bw._record_line(arcname, data))
        record_name = f"{dist_info}/RECORD"
        records.append(f"{record_name},,")
        whl.writestr(record_name, "\n".join(records) + "\n")
    print(f"wrote site/vendor/{wheel_path.name} ({wheel_path.stat().st_size} bytes) "
          f"from cordis-py {revision}")


def main() -> None:
    build_wheel = _load("build_wheel", ROOT / "playground" / "build_wheel.py")
    build_wheel.main()  # -> playground/vendor/revl-<version>-py3-none-any.whl

    vendor = SITE / "vendor"
    vendor.mkdir(exist_ok=True)
    for whl in (ROOT / "playground" / "vendor").glob("revl-*.whl"):
        shutil.copy2(whl, vendor / whl.name)
        print(f"copied {whl.name} -> site/vendor/")

    build_cordis_wheel(vendor)

    gen = _load("gen_examples", SITE / "gen_examples.py")
    gen.main()


if __name__ == "__main__":
    main()
