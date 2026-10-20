#!/usr/bin/env python3
"""The generated-artifact checks the fast pre-PR loop used to omit.

`tools/pre_pr.sh` catches the cheap drifts (ruff, syntax, docs, both gate
crates), but it ran neither of the two checks that dominate failed pull-request
runs (issue #2645): the committed census records and the emitted goldens. Both
checks already exist; this only decides when to run them, and prints the exact
command that fixes a drift.

Census: `.github/workflows/ci.yml` asks `census_artifact.py --moved-inputs`
whether a pull request's diff moves an input of the committed artifact, and
only re-runs the census when one did. The same filter is used here, and the
re-run is `census_artifact.py --check`, the check that keeps the records
current. When `--moved-inputs` says no input moved (exit 1) the re-run is
skipped, so a branch that touches neither artifact pays only a comparison. Its
exit 2 (it could not decide) checks anyway: a filter that cannot answer is not
a filter that says no.

Goldens: `regen_goldens.py --check --strict`, the command CI's lint job runs,
over the emitted tiers. The two gate crates are not repeated here because
`pre_pr.sh` already checks them with their own `--check`.

Neither check redefines what a census input or a golden is; both call the tool
that owns the definition.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

BASE_REF = "origin/main"
CENSUS_TOOL = "tools/census_artifact.py"
GOLDENS_TOOL = "tools/regen_goldens.py"

# The emitted tiers. The gate crates are deliberately absent: pre_pr.sh already
# checks both with build_gate_crate.py --check and build_gate_wasm.py --check,
# and the go tier is checked in its own CI job because it needs gofmt.
GOLDEN_TARGETS = ("python", "typescript", "rust", "java", "wasm")

CENSUS_NOTHING_MOVED = 1

CENSUS_FIX = "python3 tools/census_artifact.py --write"
GOLDENS_FIX = "python3 " + GOLDENS_TOOL + " " + " ".join(GOLDEN_TARGETS)


def census_warrants_check(moved_inputs_status: int) -> bool:
    """Whether `--moved-inputs`'s answer requires the records check.

    Exit 1 is the only "nothing moved" answer. Exit 0 means at least one input
    moved; anything else is an answer this tool does not define (2 is "could
    not decide"), and an undefined answer must check rather than skip."""
    return moved_inputs_status != CENSUS_NOTHING_MOVED


def census_command(python: str = sys.executable) -> list[str]:
    """The check that fails when the committed records have drifted."""
    return [python, CENSUS_TOOL, "--check"]


def goldens_command(python: str = sys.executable) -> list[str]:
    """The drift check CI's lint job runs, over the emitted tiers."""
    return [python, GOLDENS_TOOL, "--check", "--strict", *GOLDEN_TARGETS]


def _changed_paths(base: str, run) -> list[str] | None:
    """Repo-relative paths the branch changed against `base`, or None when the
    diff cannot be read (no such ref, not a repository); None checks anyway."""
    proc = run(["git", "diff", "--name-only", base],
               capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _census_check(run, python: str) -> int:
    print("census artifact: re-running the records check; this takes a minute.",
          flush=True)
    proc = run(census_command(python))
    if proc.returncode == 0:
        return 0
    print("census artifact: the records check failed. If the committed records "
          "are stale, fix with:")
    print(f"  {CENSUS_FIX}")
    return 1


def census_gate(base: str = BASE_REF, run=subprocess.run,
                python: str = sys.executable) -> int:
    """Run the census records check only if the diff moves one of its inputs."""
    paths = _changed_paths(base, run)
    if paths is None:
        print(f"census artifact: could not read the diff against {base}; "
              "checking anyway.")
        return _census_check(run, python)
    if not paths:
        print(f"census artifact: no change against {base}; nothing moved, "
              "not re-run.")
        return 0

    proc = run([python, CENSUS_TOOL, "--moved-inputs"],
               input="\n".join(paths) + "\n", capture_output=True, text=True)
    if not census_warrants_check(proc.returncode):
        print(f"census artifact: the {len(paths)} changed path(s) move no input "
              "of the artifact; not re-run.")
        return 0
    if proc.returncode != 0:
        print(f"census artifact: --moved-inputs could not decide (exit "
              f"{proc.returncode}); checking anyway.")
    for rel in proc.stdout.splitlines():
        if rel.strip():
            print(f"  moved input: {rel.strip()}")
    if proc.stderr.strip():
        print(proc.stderr.rstrip(), file=sys.stderr)
    return _census_check(run, python)


def goldens_gate(run=subprocess.run, python: str = sys.executable) -> int:
    """Byte-compare every emitted golden against a fresh generation."""
    print("goldens: checking " + ", ".join(GOLDEN_TARGETS) + " for drift.",
          flush=True)
    proc = run(goldens_command(python))
    if proc.returncode == 0:
        return 0
    if proc.returncode == 1:
        print("goldens: a checked-in golden differs from a fresh generation. "
              "Regenerate with:")
        print(f"  {GOLDENS_FIX}")
        print("  then review the diff and commit it, never bend the emitter "
              "back to the old bytes.")
    else:
        print(f"goldens: could not check every target (exit {proc.returncode}); "
              "this is not a drift report. Fix the producer or the machine.")
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("check", choices=("census", "goldens"),
                    help="which generated-artifact check to run")
    ap.add_argument("--base", default=BASE_REF,
                    help=f"the ref the census filter compares against "
                         f"(default: {BASE_REF})")
    args = ap.parse_args(argv)
    if args.check == "census":
        return census_gate(args.base)
    return goldens_gate()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
