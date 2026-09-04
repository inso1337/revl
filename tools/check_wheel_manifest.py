#!/usr/bin/env python3
"""The wheel ships exactly the tracked files, and nothing else.

WHY THIS EXISTS

`pyproject.toml` used to `force-include` the whole of `backends/` and
`stdlib/` into the wheel. `force-include` is applied after file selection and
is exempt from every exclude rule, so it copied whatever was on the builder's
disk under those directories -- `node_modules`, cargo `target/`, a cloned
`.cordis-py`, compiled runner binaries, scratch logs, and the machine-generated
key material that `.gitignore` deliberately parks under
`backends/typescript/test-secret-store/`. A developer build came out at 2933
members and 250 MB unpacked against the 523 members the commit describes.

The size was the visible half. The half that mattered is that the artifact was
a function of the BUILDER'S FILESYSTEM rather than of the revision: the same
commit built on two machines produced two different wheels, and no amount of
reading the repository told you what a given wheel contained.

`hatch_build.py` now computes the force-include entry list per file, from
`git ls-files`, and `[tool.hatch.build] exclude` states the artefact classes
for the sdist and for the hook's no-git fallback. That is the fix; this is the
proof, and it is deliberately NOT the same mechanism. The build hook could be
edited, the excludes could go stale against the sixteen nested `.gitignore`
files hatchling never reads, someone could reach for `force-include` again. So
the contract is asserted from outside the build, in the only terms that make a
distribution reproducible from the commit alone:

    the WHEEL's member list == `git ls-files` of the trees `hatch_build.TREES`
                               maps into it, plus its own `.dist-info`

    the SDIST carries no file that `git ls-files` does not track

Set equality for the wheel, both directions. An extra member is over-inclusion
(the defect above). A missing member is a wheel that is SHORT of what the commit
says it ships -- the failure mode of an exclude pattern that is too wide, which
is a mistake this file's own fix made twice before it landed.

The sdist gets the one-directional half. It is a source archive, so what it may
legitimately leave out is a policy question (docs, formal proofs, benchmarks)
that this gate has no business deciding; what it may never CONTAIN is not. Both
go to PyPI from the same job.

USAGE

    tools/check_wheel_manifest.py                 # build both, check both
    tools/check_wheel_manifest.py dist/*.whl      # check ones already built
    tools/check_wheel_manifest.py --self-test     # prove the gate has teeth

`--self-test` shows the gate catching each failure it exists to catch, because
a gate that has never been shown to catch anything is the same shape of gap as
the one it closes. It (1) hands the check a wheel carrying an untracked
`.env.local` and requires a red, (2) hands it a wheel missing a tracked module
and requires a red, (3) plants a real untracked file under `backends/`, builds,
and requires that it never even entered the wheel while the sdist check flags
it, and (4) requires a clean pass with the tree restored. Legs 1 and 2 doctor a
built wheel rather than the tree: `hatch_build.py` reads `git ls-files`, so an
untracked file cannot reach the wheel to be caught -- which is the fix working,
not the gate failing. The tree is restored on any exit path, interrupts
included.
"""

from __future__ import annotations

import argparse
import glob
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The source-tree -> wheel-prefix map, imported from the build hook that
# applies it rather than restated here. A future change to what the wheel ships
# moves the expected set with it, so the gate keeps checking the intent instead
# of a stale copy of it.
from hatch_build import TREES as _BACKEND_TREES  # noqa: E402

# `packages = ["src/revl"]` in pyproject; hatchling strips the `src/revl`
# prefix and lands the modules at `revl/`.
WHEEL_TREES: dict[str, str] = {"src/revl": "revl", **_BACKEND_TREES}

# Where --self-test plants its stray file. Untracked, matched by no exclude in
# pyproject and by no ignore rule, so the ONLY thing standing between it and
# the distributions is the mechanism under test.
STRAY = ROOT / "backends" / "_check_wheel_manifest_selftest_stray.txt"


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _project_version() -> str:
    return str(_pyproject()["project"]["version"])


def _assert_no_force_include() -> None:
    """The one edit that silently reintroduces the whole defect."""
    wheel = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]
    if wheel.get("force-include"):
        raise SystemExit(
            "pyproject's wheel target declares a directory-level force-include again.\n"
            "force-include is applied after file selection and is exempt from every "
            "exclude rule and from the ignore files, so it copies whatever is on the "
            "builder's disk -- which is how the wheel came to ship node_modules, cargo "
            "target/, compiled binaries and generated key material "
            "(GHSA-gj88-cx6q-38r2).\n"
            "hatch_build.py computes the entry list per file from `git ls-files` "
            "instead; add paths there."
        )


def _tracked(tree: str | None = None) -> list[str]:
    """`git ls-files`, optionally scoped to one tree."""
    cmd = ["git", "ls-files", "-z"]
    if tree is not None:
        cmd += ["--", tree]
    out = subprocess.run(
        cmd, cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    paths = [p for p in out.split("\0") if p]
    if not paths:
        raise SystemExit(
            f"git tracks no files under {tree!r}: this gate needs a real checkout"
        )
    return paths


def expected_wheel_members() -> set[str]:
    """Exactly the tracked files of the mapped trees, at their wheel paths."""
    expected: set[str] = set()
    for src, dest in WHEEL_TREES.items():
        prefix = src.rstrip("/") + "/"
        for path in _tracked(src):
            expected.add(dest + "/" + path[len(prefix) :])
    return expected


def build(outdir: Path, *, sdist: bool) -> Path:
    """Build a distribution the way the release does, into a throwaway dir."""
    cmd = [sys.executable, "-m", "build", "--sdist" if sdist else "--wheel",
           "--outdir", str(outdir)]
    try:
        import hatchling  # noqa: F401

        # Hatchling is already here, so skip build's ephemeral venv: it is the
        # slowest part of this check and it would install the same backend.
        cmd.append("--no-isolation")
    except ImportError:
        pass
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise SystemExit("the build failed; nothing to check")
    made = sorted(outdir.glob("*.tar.gz" if sdist else "*.whl"))
    if len(made) != 1:
        raise SystemExit(f"expected exactly one distribution in {outdir}, got {made}")
    return made[0]


def _fmt(paths: list[str], limit: int = 40) -> str:
    shown = "\n  ".join(paths[:limit])
    more = f"\n  ... and {len(paths) - limit} more" if len(paths) > limit else ""
    return "  " + shown + more


def check_wheel(wheel: Path, *, quiet: bool = False) -> list[str]:
    """Set equality: the wheel is the commit's contents, no more and no less."""
    dist_info = f"revl-{_project_version()}.dist-info/"
    with zipfile.ZipFile(wheel) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
    members = {i.filename for i in infos}
    metadata = {m for m in members if m.startswith(dist_info)}
    payload = members - metadata
    expected = expected_wheel_members()

    problems: list[str] = []
    extra = sorted(payload - expected)
    if extra:
        problems.append(
            f"{len(extra)} file(s) in the wheel that the commit does not track. This is "
            "the over-inclusion of GHSA-gj88-cx6q-38r2: the artifact depends on the "
            "builder's filesystem, so anything sitting under these trees at build time "
            "-- caches, build output, credentials -- ships to PyPI:\n" + _fmt(extra)
        )
    missing = sorted(expected - payload)
    if missing:
        problems.append(
            f"{len(missing)} tracked file(s) the wheel does NOT ship. An exclude in "
            "pyproject is too wide, or hatch_build.py is not finding them:\n"
            + _fmt(missing)
        )
    if not metadata:
        problems.append(f"the wheel has no {dist_info} -- is it a wheel?")

    if not problems and not quiet:
        size = sum(i.file_size for i in infos)
        print(
            f"{wheel.name}: {len(payload)} tracked files + {len(metadata)} metadata "
            f"files, {size / 2**20:.1f} MB unpacked -- exactly the commit's contents"
        )
    return problems


def check_sdist(sdist: Path, *, quiet: bool = False) -> list[str]:
    """One direction only: the sdist may ship LESS than the tree, never more.

    What a source archive legitimately leaves out (docs, formal proofs,
    benchmarks) is a policy question this gate has no business deciding. What
    it may never CONTAIN is not a policy question.
    """
    with tarfile.open(sdist) as tf:
        names = [m.name for m in tf.getmembers() if m.isfile()]
    # every member is prefixed with `revl-<version>/`
    root = f"revl-{_project_version()}/"
    payload = {n[len(root) :] for n in names if n.startswith(root)}
    # PKG-INFO is generated by the backend and has no counterpart in the tree.
    payload -= {"PKG-INFO"}
    extra = sorted(payload - set(_tracked()))

    if extra:
        return [
            f"{len(extra)} file(s) in the sdist that the commit does not track. The "
            "sdist goes to PyPI from the same job as the wheel and `pip install` "
            "builds from it, so an untracked file here reaches users just as surely:\n"
            + _fmt(extra)
        ]
    if not quiet:
        print(f"{sdist.name}: {len(payload)} files, all tracked")
    return []


def _check_built(wheel: Path | None, sdist: Path | None, *, quiet: bool = False):
    problems: list[str] = []
    if wheel is not None:
        problems += check_wheel(wheel, quiet=quiet)
    if sdist is not None:
        problems += check_sdist(sdist, quiet=quiet)
    return problems


def _build_and_check(tmp: Path, *, quiet: bool = False) -> list[str]:
    return _check_built(
        build(tmp / "w", sdist=False), build(tmp / "s", sdist=True), quiet=quiet
    )


def _doctored(wheel: Path, out: Path, *, add: str | None, drop: str | None) -> Path:
    """A copy of `wheel` with one member added or one removed.

    The wheel half of the gate cannot be exercised by planting a file in the
    tree the way the sdist half is: `hatch_build.py` builds its entry list from
    `git ls-files`, so an untracked file simply never reaches the wheel. That
    is the point of the fix, and it means the only honest way to show the
    ASSERTION works is to hand it a wheel that is wrong -- the same shape as
    `check_workflow_permissions.py --self-test` reintroducing its bug into a
    synthetic workflow.
    """
    with zipfile.ZipFile(wheel) as src, zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            if drop is not None and item.filename == drop:
                continue
            dst.writestr(item, src.read(item.filename))
        if add is not None:
            dst.writestr(add, b"AWS_SECRET_ACCESS_KEY=hunter2\n")
    return out


def self_test() -> int:
    """Show the gate catching each failure it exists to catch."""
    _assert_no_force_include()
    if STRAY.exists():
        raise SystemExit(f"{STRAY} already exists; refusing to clobber it")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        wheel = build(tmp / "w", sdist=False)

        # 1. over-inclusion, the defect itself: a file the commit does not
        #    track, sitting in the wheel.
        stray = "revl/backends/typescript/.env.local"
        doctored = _doctored(wheel, tmp / "extra.whl", add=stray, drop=None)
        problems = check_wheel(doctored, quiet=True)
        if not any(stray in p for p in problems):
            print(
                f"SELF-TEST FAILED: a wheel carrying an untracked {stray} passed the "
                "check. The gate does not bite, so it is not protecting the release.",
                file=sys.stderr,
            )
            return 1
        print(f"self-test: caught an untracked {stray} in the wheel")

        # 2. the other direction: an exclude that is too wide, leaving the
        #    wheel SHORT of what the commit says it ships. Both fixes of this
        #    advisory got this wrong once before landing.
        victim = sorted(expected_wheel_members())[0]
        doctored = _doctored(wheel, tmp / "short.whl", add=None, drop=victim)
        problems = check_wheel(doctored, quiet=True)
        if not any(victim in p for p in problems):
            print(
                f"SELF-TEST FAILED: a wheel MISSING the tracked {victim} passed the "
                "check; an over-wide exclude would ship silently.",
                file=sys.stderr,
            )
            return 1
        print(f"self-test: caught a missing {victim}")

        # 3. the real build, with a real untracked file planted in the tree.
        #    The sdist must catch it. The WHEEL must not even contain it --
        #    `hatch_build.py` reads `git ls-files`, so an untracked file cannot
        #    reach the wheel at all, and asserting that is asserting the fix.
        try:
            STRAY.write_text(
                "planted by tools/check_wheel_manifest.py --self-test\n",
                encoding="utf-8",
            )
            planted_wheel = build(tmp / "pw", sdist=False)
            if check_wheel(planted_wheel, quiet=True):
                print(
                    "SELF-TEST FAILED: an untracked file under backends/ reached the "
                    "wheel. hatch_build.py is no longer deriving its entry list from "
                    "git, and the wheel is a function of the filesystem again.",
                    file=sys.stderr,
                )
                return 1
            problems = check_sdist(build(tmp / "ps", sdist=True), quiet=True)
            if not any(STRAY.name in p for p in problems):
                print(
                    f"SELF-TEST FAILED: the planted {STRAY.name} reached no finding; "
                    "the sdist check is not looking at untracked files.",
                    file=sys.stderr,
                )
                return 1
            print(
                f"self-test: the planted {STRAY.name} never entered the wheel, and the "
                "sdist check flagged it"
            )
        finally:
            STRAY.unlink(missing_ok=True)

        # 4. and green on a clean tree, or none of the above proved anything.
        problems = _check_built(wheel, build(tmp / "s", sdist=True), quiet=True)
        if problems:
            print(
                "SELF-TEST FAILED: the stray file is gone and the distributions still "
                "do not match the tracked set:\n" + "\n\n".join(problems),
                file=sys.stderr,
            )
            return 1
    print("self-test: the gate passes on a clean tree")
    return 0


def _resolve(patterns: list[str]) -> tuple[Path | None, Path | None]:
    """Sort the given paths/globs into (wheel, sdist)."""
    wheel = sdist = None
    for pattern in patterns:
        matches = sorted(glob.glob(pattern)) or [pattern]
        for match in matches:
            path = Path(match)
            if not path.is_file():
                raise SystemExit(f"no such distribution: {match}")
            if path.name.endswith(".whl"):
                wheel = path
            elif path.name.endswith(".tar.gz"):
                sdist = path
            else:
                raise SystemExit(f"not a wheel or an sdist: {match}")
    return wheel, sdist


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "dist",
        nargs="*",
        help="built distributions to check; omit to build them into a temp dir",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="prove the gate catches an untracked file, then passes without one",
    )
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    _assert_no_force_include()
    if args.dist:
        problems = _check_built(*_resolve(args.dist))
    else:
        with tempfile.TemporaryDirectory() as tmpdir:
            problems = _build_and_check(Path(tmpdir))

    if problems:
        print(
            "The built distribution is not the commit's contents "
            "(GHSA-gj88-cx6q-38r2):\n",
            file=sys.stderr,
        )
        print("\n\n".join(problems), file=sys.stderr)
        print(
            "\nFix it in `hatch_build.py` (what the wheel force-includes, per file) or "
            "in `[tool.hatch.build] exclude` (what no distribution may carry). Never "
            "reach for a directory-level `force-include`.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
