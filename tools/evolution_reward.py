#!/usr/bin/env python3
"""The self-evolution reward: a verdict read off repository artifacts.

WHY THIS EXISTS
---------------
Roadmap item 536 (issue #1206) states the requirement the external review put
first: the reward must be based on REPOSITORY FACTS, not on the candidate's
explanation of its own work. It has two halves.

  * The reward is computed from artifacts the repository owns, and no component
    reads the candidate's prose about itself.
  * A trajectory is RETAINED only when every component verified. A trajectory
    that partially passed is evidence about the engine, not a training example.

Eight components already had machinery in this tree. Nothing folded them into a
verdict and nothing stated the retention criterion. This module is that fold.

THE REWARD IS A CONJUNCTION, NOT A SCALAR
-----------------------------------------
No weighted score is computed here, and none is exported. The reward is the
conjunction of the eight component verdicts, and the retention rule is that same
conjunction. Three reasons, in the order they decided it:

  1. The floor the repository enforces is not a magnitude. `false-admission` is
     in `gate_reference_census.NEVER_BASELINED`: `--record` refuses to write it
     and `--check` fails on any member regardless of the baseline. Under any
     weighting with positive weight on the other seven, a candidate could commit
     a false admission and still clear a threshold. A reward satisfiable in a way
     the underlying gate refuses is a defect, not a design choice.
  2. The components are not commensurable. "the census bucket sets are unchanged"
     and "the goldens are byte-identical" are equalities on sets and on bytes.
     There is no unit in which 0.3 of one trades against 0.7 of the other.
  3. A scalar rewards the fail-open shape this repository has already measured
     six times: a check that ran on every PR and could not fail. A candidate on a
     machine with no cargo would score 7 of 8 and clear a 0.8 bar. Under the
     conjunction it is simply not retained, which is the right answer, because
     nobody verified that it compiles.

What the scalar would have permitted, concretely: re-recording the census
baseline to absorb a new bypass costs one component and buys every other, so a
weighted candidate that also adds forty passing tests outscores a clean no-op.
Under the conjunction it is not retained, and `probe_no_new_false_admits` below
additionally refuses the re-record outright.

FAILURE DIRECTION
-----------------
Fail-closed, everywhere, with no exception. A component is `verified` only when a
tool the repository owns ran to completion and reported success. Every other
outcome is `failed`:

  * the tool is missing from the tree                  -> failed
  * the tool exits non-zero                            -> failed
  * the tool times out                                 -> failed
  * the tool raises, or the subprocess cannot start    -> failed
  * the component has no probe implemented yet         -> failed

There is no `unknown` verdict and no `skipped` verdict, because a third value is
where a fail-open default hides. `verified` means somebody's artifact said yes.

WHAT IS IMPLEMENTED (slice 1)
-----------------------------
Four of the eight components read their real artifacts:

    no-new-false-admits   tools/gate_reference_census.py --check, PLUS a read of
                          the baseline diff that refuses a grown allowance
    artifact-stability    tools/regen_goldens.py --all --check
    documentation         tools/docgen.py --check, tools/check_roadmap_claims.py
    scope                 the changed-file set against the declared scope

The other four (`compiles`, `tests`, `conformance`, `formal`) have no probe and
therefore FAIL. That is the honest state: until they are implemented, nothing is
retained, and the scorecard names them as the blockers. See
`docs/design/531-evolution-reward.md` for the slice plan.

USAGE
-----
    python3 tools/evolution_reward.py --candidate candidate.json
    python3 tools/evolution_reward.py --candidate candidate.json --json out.json

The candidate record is a JSON object. Exactly three keys are read:

    {"tree": "/path/to/worktree", "base": "origin/main",
     "scope": ["tools/**", "docs/design/**"]}

Every other key is IGNORED and reported by name under `prose_ignored`, so a
reader of the scorecard can see that the candidate's narrative was present and
was not consulted. Exit status is 0 only when the trajectory is retained.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The eight components, in the order roadmap item 536 lists them.
COMPONENTS = (
    "compiles",
    "tests",
    "no-new-false-admits",
    "conformance",
    "artifact-stability",
    "formal",
    "scope",
    "documentation",
)

# The census baseline. `probe_no_new_false_admits` reads this file on both sides
# of the candidate's change, which is the half of the item's "can only shrink in
# a diff somebody reads" that no tool performed.
CENSUS_BASELINE = "tools/gate_reference_census_baseline.json"

# Buckets that must never appear in a recorded baseline at all. Mirrors
# `gate_reference_census.NEVER_BASELINED`; a baseline listing one has been
# hand-edited, because `--record` filters them out.
NEVER_BASELINED = ("false-admission",)

DEFAULT_TIMEOUT = 900


# ------------------------------------------------------------------ verdicts

@dataclass(frozen=True)
class Verdict:
    """One component's answer. `verified` is the only value that is not a fail."""

    component: str
    verified: bool
    reason: str
    evidence: tuple = ()

    def as_dict(self) -> dict:
        return {
            "component": self.component,
            "verdict": "verified" if self.verified else "failed",
            "reason": self.reason,
            "evidence": list(self.evidence),
        }


def failed(component: str, reason: str, evidence=()) -> Verdict:
    return Verdict(component, False, reason, tuple(evidence))


def verified(component: str, reason: str, evidence=()) -> Verdict:
    return Verdict(component, True, reason, tuple(evidence))


# ----------------------------------------------------------------- candidate

# The ONLY keys read off a candidate record. The whitelist is the mechanism that
# makes "no component reads the candidate's prose" structural rather than a
# convention: a key that is not here never reaches a probe, whatever it says.
RECORD_KEYS = ("tree", "base", "scope")


@dataclass(frozen=True)
class Candidate:
    """A trajectory's scorable facts. Carries no prose field, by construction."""

    tree: Path
    base: str
    scope: tuple = ()
    prose_ignored: tuple = ()


def load_candidate(record: dict) -> Candidate:
    """Build a `Candidate` from a record, dropping every other key by name.

    Refuses rather than guesses: a record with no `tree`, no `base` or no `scope`
    cannot be scored, and an unscorable candidate is not a passing one.
    """
    missing = [k for k in RECORD_KEYS if k not in record]
    if missing:
        raise ValueError(
            "candidate record is missing required key(s): " + ", ".join(missing))
    scope = record["scope"]
    if not isinstance(scope, (list, tuple)):
        raise ValueError("candidate `scope` must be a list of path globs")
    return Candidate(
        tree=Path(record["tree"]),
        base=str(record["base"]),
        scope=tuple(str(s) for s in scope),
        prose_ignored=tuple(sorted(k for k in record if k not in RECORD_KEYS)),
    )


# ------------------------------------------------------------- running tools

@dataclass
class Run:
    ok: bool
    detail: str
    stdout: str = ""


def run_tool(candidate: Candidate, argv, timeout: int = DEFAULT_TIMEOUT) -> Run:
    """Run a repository tool inside the candidate tree. Fail-closed on everything.

    `argv[0]` is a tree-relative path to a python tool. It is resolved against
    the CANDIDATE's tree, never against this checkout, so the scorer measures the
    candidate's artifacts and not its own.
    """
    tool = candidate.tree / argv[0]
    if not tool.is_file():
        return Run(False, f"{argv[0]} is not present in the candidate tree")
    cmd = [sys.executable, str(tool)] + [str(a) for a in argv[1:]]
    env_src = candidate.tree / "src"
    env = dict(os.environ)
    env["PYTHONPATH"] = (
        str(env_src) + os.pathsep + env.get("PYTHONPATH", "")).rstrip(os.pathsep)
    try:
        proc = subprocess.run(
            cmd, cwd=str(candidate.tree), env=env, timeout=timeout,
            capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return Run(False, f"{argv[0]} timed out after {timeout}s")
    except OSError as exc:
        return Run(False, f"{argv[0]} could not be run: {exc}")
    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-4:]
        return Run(False,
                   f"{argv[0]} exited {proc.returncode}: " + " | ".join(tail),
                   proc.stdout)
    return Run(True, f"{argv[0]} exited 0", proc.stdout)


def run_git(candidate: Candidate, args, timeout: int = 120):
    """`(ok, text)` for a read-only git command in the candidate tree."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(candidate.tree)] + [str(a) for a in args],
            timeout=timeout, capture_output=True, text=True)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"git {' '.join(str(a) for a in args)} failed: {exc}"
    if proc.returncode != 0:
        return False, proc.stderr.strip() or f"git exited {proc.returncode}"
    return True, proc.stdout


def changed_files(candidate: Candidate):
    """`(ok, paths_or_error)`: the candidate's changed-file set against `base`.

    Committed and uncommitted changes both count, because a trajectory is scored
    on the tree it produced and not on how tidily it was committed.
    """
    ok, out = run_git(candidate, ["diff", "--name-only", candidate.base])
    if not ok:
        return False, out
    paths = {line.strip() for line in out.splitlines() if line.strip()}
    ok, out = run_git(candidate, ["ls-files", "--others", "--exclude-standard"])
    if not ok:
        return False, out
    paths |= {line.strip() for line in out.splitlines() if line.strip()}
    return True, sorted(paths)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# -------------------------------------------------------------------- probes

def probe_no_new_false_admits(candidate: Candidate) -> Verdict:
    """The census, read in BOTH of the ways it can be defeated.

    Half one is the gate itself: `tools/gate_reference_census.py --check` fails
    on a divergence that is not in the baseline AND on a baseline entry that no
    longer diverges, so a candidate can neither introduce a bypass nor delete the
    work that retired one.

    Half two is the part the tool cannot do for itself. `--check` compares the
    run against the baseline FILE, and a candidate that runs `--record` first
    makes `--check` pass. The item's own words are that the allowance "can only
    shrink in a diff somebody reads"; this reads that diff. Every bucket's id set
    in the candidate's baseline must be a SUBSET of the same bucket at `base`. An
    added id fails the component even though `--check` is green, which is the
    only way the re-record path is closed mechanically.
    """
    name = "no-new-false-admits"
    run = run_tool(candidate, ["tools/gate_reference_census.py", "--check"])
    if not run.ok:
        return failed(name, run.detail, ["tools/gate_reference_census.py"])

    baseline_path = candidate.tree / CENSUS_BASELINE
    if not baseline_path.is_file():
        return failed(name, f"{CENSUS_BASELINE} is not present in the candidate tree")
    try:
        head = json.loads(baseline_path.read_text())
    except (OSError, ValueError) as exc:
        return failed(name, f"{CENSUS_BASELINE} is unreadable: {exc}")

    head_buckets = head.get("buckets", {})
    hand_edited = sorted(
        b for b in head_buckets
        if b.split("/", 1)[0] in NEVER_BASELINED and head_buckets[b])
    if hand_edited:
        return failed(
            name,
            "the baseline lists a never-baselined bucket, so it was hand-edited: "
            + ", ".join(hand_edited),
            [CENSUS_BASELINE])

    ok, base_text = run_git(candidate, ["show", f"{candidate.base}:{CENSUS_BASELINE}"])
    if not ok:
        return failed(
            name, f"cannot read {CENSUS_BASELINE} at {candidate.base}: {base_text}")
    try:
        base_buckets = json.loads(base_text).get("buckets", {})
    except ValueError as exc:
        return failed(name, f"{CENSUS_BASELINE} at {candidate.base} is unreadable: {exc}")

    grown = []
    for bucket, ids in sorted(head_buckets.items()):
        added = sorted(set(ids) - set(base_buckets.get(bucket, [])))
        grown += [f"{bucket}: {case}" for case in added]
    if grown:
        return failed(
            name,
            "the census allowance GREW against " + candidate.base
            + " (a re-record, not a fix): " + "; ".join(grown[:6]),
            [CENSUS_BASELINE])

    total = sum(len(v) for v in head_buckets.values())
    return verified(
        name,
        f"census --check clean and the allowance did not grow "
        f"({total} baselined divergence(s))",
        ["tools/gate_reference_census.py --check",
         f"{CENSUS_BASELINE}@{_sha256(baseline_path)}"])


def probe_artifact_stability(candidate: Candidate) -> Verdict:
    """Every generated artifact matches a fresh generation.

    `tools/regen_goldens.py --all --check` covers the six backend golden trees
    and both gate crates, so `build_gate_crate.py --check` and
    `build_gate_wasm.py --check` are inside it rather than beside it. A golden
    the candidate legitimately regenerated passes here and is judged by `scope`
    instead, which is where "unrelated" is decided.
    """
    name = "artifact-stability"
    run = run_tool(candidate, ["tools/regen_goldens.py", "--all", "--check"])
    if not run.ok:
        return failed(name, run.detail, ["tools/regen_goldens.py --all --check"])
    return verified(name, "every generated artifact matches a fresh generation",
                    ["tools/regen_goldens.py --all --check"])


def probe_documentation(candidate: Candidate) -> Verdict:
    """The citation gate and the generated-block gate, both of them."""
    name = "documentation"
    evidence = []
    for argv in (["tools/docgen.py", "--check"],
                 ["tools/check_roadmap_claims.py", "--check", "--quiet"]):
        run = run_tool(candidate, argv)
        if not run.ok:
            return failed(name, run.detail, evidence + [argv[0]])
        evidence.append(" ".join(argv))
    return verified(name, "generated doc blocks current and every citation resolves",
                    evidence)


def probe_scope(candidate: Candidate) -> Verdict:
    """The changed-file set against the declared scope.

    Fail-closed in three directions. An empty scope declaration cannot be
    satisfied, because a candidate that declares nothing has not constrained
    itself. An empty change set cannot be satisfied either: there is no work to
    retain. And a path matching no declared glob fails, listing the paths, so the
    verdict is readable without re-running git.
    """
    name = "scope"
    if not candidate.scope:
        return failed(name, "the candidate declared no scope, so nothing bounds it")
    ok, paths = changed_files(candidate)
    if not ok:
        return failed(name, f"the changed-file set could not be read: {paths}")
    if not paths:
        return failed(name, f"no file changed against {candidate.base}")
    stray = [p for p in paths if not _in_scope(p, candidate.scope)]
    if stray:
        return failed(
            name,
            f"{len(stray)} changed path(s) outside the declared scope: "
            + ", ".join(stray[:6]),
            [f"git diff --name-only {candidate.base}"])
    return verified(
        name,
        f"{len(paths)} changed path(s), all inside the declared scope",
        [f"git diff --name-only {candidate.base}", "scope=" + ";".join(candidate.scope)])


def _glob_to_regex(pattern: str):
    """A path glob with the separator semantics a scope declaration needs.

    `fnmatch` is not usable here: its `*` crosses `/`, so a scope of
    `tools/*.py` would silently admit `tools/sub/deep.py`, which is a WIDER
    scope than the candidate declared. A scope check that widens the scope is
    the fail-open shape, so the translation is explicit:

        ``**``  any number of segments      ``?``  one character, not ``/``
        ``*``   one segment, not ``/``      rest   literal
    """
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _in_scope(path: str, scope) -> bool:
    for pattern in scope:
        if _glob_to_regex(pattern).match(path):
            return True
        # `tools/**` names the directory as well as everything under it.
        if pattern.endswith("/**") and path == pattern[:-3]:
            return True
    return False


def _unimplemented(component: str, note: str):
    def probe(candidate: Candidate) -> Verdict:
        return failed(component, f"no probe implemented: {note}")
    return probe


# The registry. A component with no probe FAILS; it is never absent from the
# scorecard and never defaults to pass.
PROBES = {
    "compiles": _unimplemented(
        "compiles",
        "the crate build and the six-tier matrix are slice 2 "
        "(docs/design/531-evolution-reward.md)"),
    "tests": _unimplemented(
        "tests",
        "the affected suite is slice 2 (docs/design/531-evolution-reward.md)"),
    "no-new-false-admits": probe_no_new_false_admits,
    "conformance": _unimplemented(
        "conformance",
        "roadmap item 536 names tools/gate_verdict_parity.py, which does not "
        "exist anywhere in this tree; slice 3 has to build the parity read "
        "before the component can carry a verdict"),
    "artifact-stability": probe_artifact_stability,
    "formal": _unimplemented(
        "formal",
        "the formal/ ledger read is slice 3 "
        "(docs/design/531-evolution-reward.md)"),
    "scope": probe_scope,
    "documentation": probe_documentation,
}


# ----------------------------------------------------------------- the score

@dataclass
class Scorecard:
    candidate: Candidate
    verdicts: list = field(default_factory=list)

    @property
    def retained(self) -> bool:
        """The retention rule, stated once: every component verified.

        Not a threshold, not a majority, not a weighted sum. `all()` over the
        eight, and `all()` of an incomplete list is not reachable because
        `score()` always emits one verdict per component in `COMPONENTS`.
        """
        by_name = {v.component: v for v in self.verdicts}
        return all(
            name in by_name and by_name[name].verified for name in COMPONENTS)

    @property
    def blockers(self):
        return tuple(v.component for v in self.verdicts if not v.verified)

    def as_dict(self) -> dict:
        return {
            "retained": self.retained,
            "blockers": list(self.blockers),
            "components": [v.as_dict() for v in self.verdicts],
            "prose_ignored": list(self.candidate.prose_ignored),
            "base": self.candidate.base,
            "scope": list(self.candidate.scope),
        }

    def render(self) -> str:
        lines = [f"evolution reward over {self.candidate.tree}",
                 f"base {self.candidate.base}", ""]
        for v in self.verdicts:
            mark = "ok  " if v.verified else "FAIL"
            lines.append(f"  {mark} {v.component:<22} {v.reason}")
        lines.append("")
        if self.retained:
            lines.append("RETAIN: every component verified.")
        else:
            lines.append(
                "DO NOT RETAIN: " + ", ".join(self.blockers)
                + " did not verify. A partially verified trajectory is evidence "
                  "about the engine, not a training example.")
        if self.candidate.prose_ignored:
            lines.append(
                "candidate keys ignored (never read by any component): "
                + ", ".join(self.candidate.prose_ignored))
        return "\n".join(lines)


def score(candidate: Candidate, probes=None) -> Scorecard:
    """One verdict per component in `COMPONENTS`, in that order.

    A probe that RAISES is a failed component, not a crashed scorer: an exception
    inside a probe is exactly the case where a `try`-less implementation would
    abort the run and leave a human to decide what it meant.
    """
    table = PROBES if probes is None else probes
    verdicts = []
    for name in COMPONENTS:
        probe = table.get(name)
        if probe is None:
            verdicts.append(failed(name, "no probe registered for this component"))
            continue
        try:
            verdict = probe(candidate)
        except Exception as exc:  # fail-closed: a raising probe is a failure
            verdict = failed(name, f"probe raised {type(exc).__name__}: {exc}")
        if verdict.component != name:
            verdict = failed(
                name, f"probe answered for {verdict.component!r}, not {name!r}")
        verdicts.append(verdict)
    return Scorecard(candidate, verdicts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--candidate", type=Path, required=True,
                    help="JSON record with tree/base/scope; every other key is ignored")
    ap.add_argument("--json", type=Path, help="write the scorecard here")
    args = ap.parse_args(argv)

    try:
        record = json.loads(args.candidate.read_text())
    except (OSError, ValueError) as exc:
        print(f"evolution_reward: cannot read {args.candidate}: {exc}", file=sys.stderr)
        return 1
    try:
        candidate = load_candidate(record)
    except ValueError as exc:
        print(f"evolution_reward: {exc}", file=sys.stderr)
        return 1

    card = score(candidate)
    print(card.render())
    if args.json:
        args.json.write_text(json.dumps(card.as_dict(), indent=1, sort_keys=True) + "\n")
    return 0 if card.retained else 1


if __name__ == "__main__":
    raise SystemExit(main())
