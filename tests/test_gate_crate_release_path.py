"""The crate's release path is rehearsed, not assumed.

Roadmap item 338 / issue #104, the rust tier: `cargo add revl-gate` is the
epic's headline, and the crate was the one tier with an existing artifact whose
release path had never executed. Nothing in `.github/workflows` or `tools/` ran
`cargo package` or `cargo publish`; `backend-rust` in ci.yml compiles the
CHECKOUT, which is a different artifact from the one cargo would upload. The
wheel tier paid for that lesson already (issue #191) and has
`.github/workflows/release-dryrun.yml`; the crate now has
`.github/workflows/release-dryrun-crate.yml` and `tools/check_crate_package.py`.

This file is the half of that gate that needs NO rust toolchain, so it runs on
every PR in the root suite rather than as a skip that is green either way: the
rehearsal's shape (it packages the crate and cannot publish), the checker's
preconditions against the real manifest, and the checker's teeth against
synthetic artifacts. The half that needs cargo runs in the workflow, where the
Actions log shows it and where `cargo package` can build the tarball for real.

`tests/test_gate_dependency_publish_ready.py` owns the neighbouring claim —
that for every tier the upload is the owner's remaining step. This file owns
the rust half of that claim being mechanical rather than latent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml  # the `test` extra; a hard import so a missing parser errors

from _load_by_path import load_by_path

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"
CRATE = REPO / "crates" / "revl-gate"
REHEARSAL = WORKFLOWS / "release-dryrun-crate.yml"
CHECKER = REPO / "tools" / "check_crate_package.py"


@pytest.fixture(scope="module")
def checker():
    return load_by_path("revl_check_crate_package", CHECKER)


def _document(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(document: dict) -> dict:
    """The `on:` block. YAML 1.1 reads the bare key `on` as boolean true."""
    return document.get("on") or document.get(True) or {}


def _steps(document: dict, job: str) -> list[dict]:
    return document["jobs"][job]["steps"]


def _runs(document: dict, job: str) -> str:
    return "\n".join(step.get("run", "") for step in _steps(document, job))


# --------------------------------------------------------------------------- #
# The rehearsal exists, rehearses the crate, and cannot upload it.
# --------------------------------------------------------------------------- #

def test_the_crate_rehearsal_workflow_packages_the_crate():
    """A `cargo package` that actually builds the tarball is the point: the
    file list checked against cargo's own report of what it would ship is a
    weaker claim than the file list checked against the artifact."""
    assert REHEARSAL.exists(), (
        "the crate release rehearsal is missing: without it the crate's release "
        "path is latent again, which is exactly what item 338's rust tier "
        "looked like before it existed")
    document = _document(REHEARSAL)
    runs = _runs(document, "package-and-check")
    assert "tools/check_crate_package.py --build" in runs, (
        "the rehearsal must run the real `cargo package`, not only the "
        "checker's list/precondition half")
    assert "tools/check_crate_package.py --self-test" in runs, (
        "the checks must be driven against synthetic bad artifacts first: a "
        "gate nobody has seen fail is a gate nobody can trust")


def _effective_text(document: dict) -> str:
    """Everything a step could actually execute: `run`, `uses`, `with`, `env`.

    Read from the parsed document rather than the file, which is the point: a
    YAML comment is dropped by the parser, and the header comment explains at
    length why there is no upload step. Prose may say the words; a step may not
    contain them."""
    out = []
    for job in (document.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            for key in ("run", "uses", "with", "env"):
                value = step.get(key)
                if value:
                    out.append(f"{key}: {yaml.safe_dump(value)}")
    return "\n".join(out)


def test_the_crate_rehearsal_cannot_publish():
    """No upload step at all, not even a guarded one. `cargo publish` takes a
    registry token and has no dry-run mode, so the guarantee is made by the
    token's absence rather than by an `if:` that one edit could flip."""
    document = _document(REHEARSAL)
    steps = _effective_text(document)
    for forbidden in ("cargo publish", "cargo login", "CARGO_REGISTRY_TOKEN",
                      "--token"):
        assert forbidden not in steps, (
            f"the crate rehearsal must not be able to upload anything, and a "
            f"step now mentions {forbidden!r}; the upload is the maintainer's")
    # Neither the steps nor the comments: a token cannot be referenced from a
    # file that never names one.
    raw = REHEARSAL.read_text(encoding="utf-8")
    assert "id-token: write" not in raw
    assert "secrets." not in raw, (
        "the crate rehearsal must not read any secret: the point of a dry run "
        "is that there is nothing to leak and nothing to publish with")
    assert _steps(document, "package-and-check")[-1].get("name") == (
        "This job does not publish"), (
        "the rehearsal must end by saying out loud that it published nothing")


def test_the_crate_rehearsal_is_a_dry_run_not_a_required_check():
    """Dispatch and the weekly calendar, plus the PRs that edit the crate or
    the gate. A `push:` or `tags:` trigger here would turn a rehearsal into a
    release-shaped job on every merge."""
    triggers = _triggers(_document(REHEARSAL))
    assert set(triggers) == {"workflow_dispatch", "schedule", "pull_request"}
    assert "crates/revl-gate/**" in triggers["pull_request"]["paths"]
    assert "tools/check_crate_package.py" in triggers["pull_request"]["paths"]


def test_the_crate_rehearsal_runs_no_test_suite():
    """Deliberately: `tests/test_every_test_file_runs_in_ci_1465.py` fails any
    workflow that runs a test suite without being added to its `WORKFLOWS`
    list, and the cargo-free half of this gate is this file, which the root
    suite collects like any other. A step re-running it from the workflow would
    buy nothing and would need its own pairing audit."""
    assert "pytest" not in REHEARSAL.read_text(encoding="utf-8")


def test_the_release_paths_stay_separate():
    """`publish.yml` is the PyPI path and holds no crate credential, and the
    crate rehearsal is not the wheel's dry run. Two registries, two owner-run
    uploads, neither reachable from the other's workflow."""
    for name in ("publish.yml", "release-dryrun.yml"):
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert "cargo publish" not in text
        assert "CARGO_REGISTRY_TOKEN" not in text


# --------------------------------------------------------------------------- #
# The checker, against the real crate and against synthetic bad artifacts.
# --------------------------------------------------------------------------- #

def test_the_crate_declares_what_crates_io_requires(checker):
    """The preconditions crates.io enforces are asserted against the committed
    manifest, so a release-day rejection is a red gate instead of an upload
    that does not happen. The crate is a valid package today; this is what
    keeps it one."""
    assert checker.check_preconditions(checker.manifest_text()) == []


def test_the_checker_compares_against_the_crates_git_file_set(checker):
    """cargo packages the files git tracks under the crate directory (plus the
    untracked files git does not ignore), which is the same rule the checker
    re-derives. `target/` is the live proof that the ignore rules are honoured:
    the crate is built locally and in CI, and none of it is a package member."""
    expected = checker.expected_members()
    assert {"Cargo.toml", "build.rs", "src/lib.rs"} <= expected
    assert not [p for p in expected if p.startswith("target/")], (
        "the crate's build directory is not a package member: it is ignored, "
        "and `--exclude-standard` is what keeps a runner's cargo output out of "
        "the tarball")


def test_the_checker_reports_drift_in_both_directions(checker):
    """The two failures that matter, on synthetic sets so they need no cargo:
    a file in the package the commit does not track (crates.io versions are
    immutable, so it ships forever), and a tracked file the package omits (the
    crate builds here and not for a consumer)."""
    expected = {"Cargo.toml", "src/lib.rs"}
    stray = checker.check_members(expected | {"notes.md"}, expected,
                                 label="synthetic")
    assert stray and "notes.md" in stray[0]
    dropped = checker.check_members(expected - {"src/lib.rs"}, expected,
                                    label="synthetic")
    assert dropped and "src/lib.rs" in dropped[0]
    assert checker.check_members(expected, expected, label="synthetic") == []


def test_the_checker_self_test_passes_without_cargo(checker):
    """`--self-test` is what the workflow runs first and what a machine with no
    rust toolchain can still run: the whole battery of synthetic bad artifacts,
    in process, with no cargo on PATH."""
    assert checker.self_test() == 0


if __name__ == "__main__":  # pragma: no cover - convenience only
    raise SystemExit(pytest.main([__file__, "-q"]))
