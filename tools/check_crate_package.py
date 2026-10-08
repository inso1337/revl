#!/usr/bin/env python3
"""The crate ships the commit, and cargo can build it from the package.

WHY THIS EXISTS

`cargo add revl-gate` is item 338's headline, and until this gate existed the
crate had never been *packaged*: `crates/revl-gate/Cargo.toml` was a valid
manifest, CI compiled the crate, and nothing in `.github/workflows` or `tools/`
ran `cargo package` or `cargo publish`. The wheel tier paid for that lesson
already (issue #191): a release path that has never executed is a release path
that does not work, and you find out on the day you need it.
`tools/check_wheel_manifest.py` rehearses the wheel; this file rehearses the
crate. It is the same gate with the same teeth, on the artifact that
`cargo package` would actually upload.

WHAT IT CHECKS

1. cargo's package file list equals the crate's git file list, in both
   directions. Over-inclusion ships a stray file to every consumer forever
   (crates.io versions are immutable); under-inclusion ships a crate that
   builds here and not for them, which is the failure nobody can reproduce.
2. The preconditions cargo and crates.io enforce, asserted here so a red gate
   names the reason instead of surfacing it as an upload that silently does not
   happen: a name, a version, a description, a license, an edition, no
   `publish = false`, and no dependency that is a path or a workspace
   inheritance without a version of its own.
3. `--build` additionally runs the real `cargo package` and reads the `.crate`
   tarball, so the file list is checked against the artifact rather than
   against cargo's report of it.

WHAT IT DOES NOT CHECK

The license string being a valid SPDX expression, the crate's semver
obligations, and anything about the registry account. Those belong to the
upload, which is the owner's step: this gate rehearses, it does not publish.

USAGE

    python3 tools/check_crate_package.py             # file list + preconditions
    python3 tools/check_crate_package.py --check     # the same thing, spelled out
    python3 tools/check_crate_package.py --build     # also package for real
    python3 tools/check_crate_package.py --self-test # prove the checks bite

Exit status is 0 when the crate is releasable and 1 otherwise; problems go to
stderr. `--self-test` needs no rust toolchain, so the half of this gate that
can run anywhere does run everywhere (tests/test_gate_crate_release_path.py);
the half that needs cargo runs in `.github/workflows/release-dryrun-crate.yml`,
where the Actions log shows it instead of hiding it behind a skip.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CRATE = ROOT / "crates" / "revl-gate"

# Members `cargo package` writes that no commit can contain: the manifest as
# cargo rewrites it, the lockfile it resolves for the package, and the vcs
# stamp it adds. They are not drift, so they are not compared.
CARGO_GENERATED = frozenset({
    "Cargo.toml.orig",
    "Cargo.lock",
    ".cargo_vcs_info.json",
})

# Members cargo drops that the commit does track. `.gitignore` is a rule for
# this checkout, not part of the artifact.
CARGO_OMITS = frozenset({".gitignore"})

# The offline-first cargo policy is `tools/validate.py`'s, imported rather than
# copied: resolve `--offline` first, fall back to the networked resolve ONLY
# when the offline attempt failed for a *resolution* reason, and never
# reclassify a real failure as retryable. A cold `~/.cargo` on a machine with no
# route to the index must not be laundered into a green gate. This file used to
# carry its own copies of that vocabulary, which made it a fifth declaration of
# a closed vocabulary and a third of another; `tools/check_vocabulary_mirrors.py`
# (issue #1285) refuses exactly that, and it was right to.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import validate as _validate  # noqa: E402


def _cargo(subcommand: str, cwd: Path,
           *extra: str) -> tuple[subprocess.CompletedProcess | None, str | None]:
    """`cargo <subcommand>` — `validate._cargo`, plus the toolchain probe.

    `validate` answers "is cargo here" in `Validator.unavailable()` before it
    ever calls its own `_cargo`, so its copy assumes the caller looked. This
    gate asks the same question the same way, so a machine with no rust
    toolchain gets "cargo not on PATH" instead of a `FileNotFoundError`.
    """
    if shutil.which("cargo") is None:
        return None, "cargo not on PATH"
    return _validate._cargo(subcommand, cwd, *extra)


def manifest_text(crate_dir: Path = CRATE) -> str:
    return (crate_dir / "Cargo.toml").read_text(encoding="utf-8")


def manifest(crate_dir: Path = CRATE) -> dict:
    return tomllib.loads(manifest_text(crate_dir))


def package_root(crate_dir: Path = CRATE) -> str:
    """The single directory every member of the `.crate` sits under."""
    package = manifest(crate_dir)["package"]
    return f"{package['name']}-{package['version']}/"


def _strip_root(names, root: str) -> set[str]:
    """Members relative to the package root, tolerating output without it.

    `cargo package --list` and the tarball disagree about whether the root is
    printed; the comparison should not care which one this cargo does.
    """
    members = set()
    for name in names:
        name = name.strip()
        if name.startswith(root):
            name = name[len(root):]
        if name:
            members.add(name)
    return members


def expected_members(crate_dir: Path = CRATE) -> set[str]:
    """The files cargo will package: the crate's git file set, by cargo's rule.

    Not a convenience over `git ls-files`. This *is* cargo's documented rule -
    the tracked files under the crate directory, plus the untracked files git
    does not ignore - so reimplementing it here could only ever make the two
    lists disagree about a file neither of them ships.
    """
    rel = crate_dir.relative_to(ROOT).as_posix()
    proc = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard",
         "--", rel],
        cwd=ROOT, text=True, capture_output=True, timeout=60,
    )
    if proc.returncode != 0:
        raise SystemExit(f"git ls-files failed in {ROOT}: {proc.stderr.strip()}")
    prefix = rel + "/"
    members = {p[len(prefix):] for p in proc.stdout.split("\0")
               if p.startswith(prefix)}
    if not members:
        raise SystemExit(f"git lists no files under {rel!r}: this gate needs a "
                         "real checkout")
    return members


def _fmt(paths, limit: int = 40) -> str:
    shown = "\n".join(f"  {p}" for p in paths[:limit])
    if len(paths) > limit:
        shown += f"\n  ... and {len(paths) - limit} more"
    return shown


def check_members(packaged: set[str], expected: set[str], *,
                  label: str) -> list[str]:
    """The two directions that matter, named the way the failure reads."""
    problems = []
    extra = sorted(packaged - expected - CARGO_GENERATED)
    if extra:
        problems.append(
            f"{label} packages {len(extra)} file(s) the commit does not track "
            "- crates.io versions are immutable, so a stray file ships to every "
            f"consumer forever:\n{_fmt(extra)}")
    missing = sorted((expected - CARGO_OMITS) - packaged)
    if missing:
        problems.append(
            f"{label} omits {len(missing)} tracked file(s) - the crate would "
            f"build here and not for a consumer:\n{_fmt(missing)}")
    return problems


def cargo_members(crate_dir: Path = CRATE) -> tuple[set[str] | None, str | None]:
    """What cargo says it would package, without building it."""
    proc, reason = _cargo("package", crate_dir, "--list")
    if proc is None:
        return None, reason
    if proc.returncode != 0:
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise SystemExit("`cargo package --list` failed: the crate cannot be "
                         "packaged as it stands")
    return _strip_root(proc.stdout.splitlines(), package_root(crate_dir)), None


def archive_members(crate_path: Path, root: str) -> set[str]:
    """The members of a real `.crate` tarball."""
    with tarfile.open(crate_path) as tar:
        names = [member.name for member in tar.getmembers() if member.isfile()]
    return _strip_root(names, root)


def build_package(crate_dir: Path = CRATE) -> tuple[Path | None, str | None]:
    """Run the real `cargo package` and return the tarball it wrote.

    `--allow-dirty` because this gate is about the artifact, not about the
    checkout being clean: an uncommitted edit is exactly when you want to know
    whether it packages. CI checks out clean, so there it is a no-op.
    """
    proc, reason = _cargo("package", crate_dir, "--allow-dirty")
    if proc is None:
        return None, reason
    if proc.returncode != 0:
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise SystemExit("`cargo package` failed: the crate does not build from "
                         "the package it produces")
    crate_path = (crate_dir / "target" / "package"
                  / f"{package_root(crate_dir).rstrip('/')}.crate")
    if not crate_path.is_file():
        raise SystemExit(f"cargo package reported success but wrote no "
                         f"{crate_path}")
    return crate_path, None


def _dependency_tables(data: dict,
                       prefix: str = "") -> list[tuple[str, dict]]:
    """Every dependency table in the manifest, `[target.'cfg(..)']` included."""
    found = []
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        if key.endswith("dependencies"):
            found.append((f"{prefix}{key}", value))
        elif key == "target":
            for target, tables in value.items():
                if isinstance(tables, dict):
                    found.extend(
                        _dependency_tables(tables, prefix=f"target.{target}."))
    return found


def check_preconditions(manifest_source: str) -> list[str]:
    """The preconditions cargo and crates.io enforce, named before upload day.

    Each of these is otherwise surfaced as an upload that does not happen, at
    the one moment nobody wants to be reading a manifest.
    """
    data = tomllib.loads(manifest_source)
    package = data.get("package", {})
    problems = []
    for field, why in (
        ("name", "the crate has no name"),
        ("version", "cargo refuses to package a crate with no version"),
        ("description", "crates.io rejects an upload with no description"),
        ("edition", "a package with no edition is read as 2015 by consumers"),
    ):
        if not package.get(field):
            problems.append(f"Cargo.toml [package] has no `{field}`: {why}")
    if not (package.get("license") or package.get("license-file")):
        problems.append(
            "Cargo.toml [package] has neither `license` nor `license-file`: "
            "crates.io rejects an upload without one")
    if package.get("publish", True) is False:
        problems.append("Cargo.toml sets `publish = false`: crates.io will "
                        "reject the upload")
    for table, deps in _dependency_tables(data):
        for dep, spec in deps.items():
            if not isinstance(spec, dict):
                continue
            if spec.get("workspace") is True:
                problems.append(
                    f"[{table}] {dep} inherits `workspace = true`: a packaged "
                    "crate must name the version itself")
            elif "path" in spec and not spec.get("version"):
                problems.append(
                    f"[{table}] {dep} is a path dependency with no version: it "
                    "packages into a crate no consumer can resolve")
    return problems


def self_test() -> int:
    """Prove the checks bite, on synthetic artifacts and without cargo.

    A gate nobody has seen fail is a gate nobody can trust. This is the half
    of the crate release path that runs on any machine.
    """
    expected = {"Cargo.toml", "src/lib.rs", "tests/admit.rs", "README.md"}
    failures = []

    stray = check_members(expected | {"notes.md"}, expected, label="synthetic")
    if not stray or "notes.md" not in stray[0]:
        failures.append("a stray file in the package was not reported")
    dropped = check_members(expected - {"src/lib.rs"}, expected,
                            label="synthetic")
    if not dropped or "src/lib.rs" not in dropped[0]:
        failures.append("a tracked file missing from the package was not "
                        "reported")
    if check_members(expected | set(CARGO_GENERATED), expected,
                     label="synthetic"):
        failures.append("cargo's own generated members were reported as stray")
    if check_members(expected, expected | CARGO_OMITS, label="synthetic"):
        failures.append("a member cargo legitimately drops was reported as "
                        "missing")

    clean = ("[package]\nname = \"x\"\nversion = \"0.1.0\"\nedition = \"2021\"\n"
             "description = \"d\"\nlicense = \"MIT\"\n")
    if check_preconditions(clean):
        failures.append(f"a releasable manifest was rejected: "
                        f"{check_preconditions(clean)}")
    if not check_preconditions(clean + "publish = false\n"):
        failures.append("`publish = false` was not reported")
    path_only = clean + "\n[dependencies]\nother = { path = \"../other\" }\n"
    if not check_preconditions(path_only):
        failures.append("a path dependency with no version was not reported")
    versioned = (clean + "\n[dependencies]\n"
                 "other = { path = \"../other\", version = \"1\" }\n")
    if check_preconditions(versioned):
        failures.append(f"a versioned path dependency was rejected: "
                        f"{check_preconditions(versioned)}")
    inherited = clean + "\n[dependencies]\nother = { workspace = true }\n"
    if not check_preconditions(inherited):
        failures.append("a `workspace = true` dependency was not reported")

    with tempfile.TemporaryDirectory() as tmp:
        payload = Path(tmp) / "payload"
        payload.write_text("x\n", encoding="utf-8")
        members = sorted(expected | {"Cargo.toml.orig"})
        fake = Path(tmp) / "x-0.1.0.crate"
        with tarfile.open(fake, "w") as tar:
            for member in members:
                tar.add(payload, arcname=f"x-0.1.0/{member}")
        if archive_members(fake, "x-0.1.0/") != set(members):
            failures.append("a tarball's members were not read back as the "
                            "package's own files")

    if failures:
        for failure in failures:
            print(f"self-test FAILED: {failure}", file=sys.stderr)
        return 1
    print("self-test passed: every check bites on a synthetic bad artifact")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="check that the revl-gate crate is releasable")
    parser.add_argument(
        "--check", action="store_true",
        help="the default; accepted so the documented spelling works")
    parser.add_argument(
        "--build", action="store_true",
        help="also run the real `cargo package` and read the .crate it writes")
    parser.add_argument(
        "--self-test", action="store_true",
        help="check the checks against synthetic artifacts and exit")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()

    problems = check_preconditions(manifest_text())
    expected = expected_members()

    packaged, reason = cargo_members()
    if packaged is None:
        sys.stderr.write(f"cannot package the crate: {reason}\n")
        return 1
    problems += check_members(packaged, expected, label="`cargo package --list`")

    if args.build:
        crate_path, reason = build_package()
        if crate_path is None:
            sys.stderr.write(f"cannot package the crate: {reason}\n")
            return 1
        problems += check_members(archive_members(crate_path, package_root()),
                                  expected, label=crate_path.name)

    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        print(f"\n{len(problems)} problem(s): the crate is not releasable",
              file=sys.stderr)
        return 1
    print(f"revl-gate is releasable: {len(expected)} tracked file(s), "
          f"{len(packaged)} packaged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
