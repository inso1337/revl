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
verdict and nothing stated the retention criterion. This module is that fold. A
ninth, `progress`, was added by issue #1224: the eight are all PRESERVATION
checks, so their maximum is attained by the empty diff, and the ninth is the
conjunct that is not. It is still a conjunct and still not a weight.

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

WHAT EACH COMPONENT READS
------------------------
Every component names a tool or a committed artifact the repository already
owns. None of them is a new gate, and none of them reads a candidate's prose:

    compiles              `cargo check --offline` on crates/revl-gate, PLUS
                          tools/conformance.py --json with zero REAL gaps (a
                          crash, as against a named tier limit) on all six tiers
    tests                 tools/affected_tests.py's own selection, then that
                          selection RUN, with a non-zero collected count
    no-new-false-admits   tools/gate_reference_census.py --check, PLUS a read of
                          the baseline diff that refuses a grown allowance
    conformance           tools/tier_guarantees.py --json, which is the only
                          consumer of the two divergence registers, ratcheted
                          against the committed matrix at `base`
    artifact-stability    tools/regen_goldens.py --all --check
    formal                formal/scripts/{nonvacuity,layering}_gate.py, PLUS a
                          read of the theorem ledger against `base`
    scope                 the changed-file set against the declared scope
    documentation         tools/docgen.py --check, tools/check_roadmap_claims.py
    progress              tools/evolution_progress.py's counter ledger

THE TOOL ITEM 536 NAMED FOR `conformance` HAS NEVER EXISTED
-----------------------------------------------------------
Item 536 and issue #1206 both mapped this component to
`tools/gate_verdict_parity.py`. That file has never been in this tree; its only
occurrence anywhere was the roadmap sentence itself, which passed the citation
gate every day because that gate judges `path:line` citations only (issue
#1233, roadmap item 547). Slice 1 failed the component by name rather than
awarding it, which is how the absence was made to block. The decision taken
here is NOT to build the tool: `probe_conformance` reads the registers that
actually record cross-tier divergence in this tree, which is what the roadmap
now says, and inventing a ninth tool to wrap two existing registers would add a
gate rather than read one.

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
import shutil
import subprocess
import sys
import tempfile
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
    # The ninth, and the only one that is not a preservation check. It is a
    # conjunct like the other eight, NOT a weight: see `probe_progress`.
    "progress",
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


# --------------------------------------------------- compiles, and its bounds

#: The crate whose build is the crate half of `compiles`. The gate crate only:
#: `crates/revl-gate-wasm` needs a `wasm32-wasip2` target installed and
#: `crates/revl-lsp` needs its dependencies fetched, so requiring either would
#: make the component unverifiable rather than strict, and a component that can
#: never verify is one nobody reads. Their BYTES are held by
#: `artifact-stability`; what is uncovered here is that they compile, and
#: section 9 of the design doc says so.
GATE_CRATE = "crates/revl-gate"
CARGO_TIMEOUT = 1800

#: The six host tiers `tools/conformance.py` walks. Named here rather than read
#: off the report, so a tier that silently STOPS being walked fails the
#: component instead of shrinking the matrix it is measured against.
SIX_TIERS = ("python", "typescript", "rust", "java", "wasm", "go")


def run_cargo(candidate: Candidate, args, cwd: Path,
              timeout: int = CARGO_TIMEOUT) -> Run:
    """`Run` for a cargo invocation in the candidate tree. Fail-closed.

    `CARGO_TARGET_DIR` is a scratch directory, never `crates/*/target` inside
    the candidate. A scorer that writes into the tree it is scoring can change
    that tree's own `scope` verdict, and a measurement that perturbs its subject
    is not a measurement. It costs a cold build every run, which is the right
    trade for a component whose whole claim is that this source compiles.
    """
    cargo = shutil.which("cargo")
    if cargo is None:
        return Run(False, "cargo is not on PATH, so nothing verified that the "
                          "gate crate compiles")
    env = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="evolution-reward-cargo-") as raw:
        env["CARGO_TARGET_DIR"] = raw
        try:
            proc = subprocess.run(
                [cargo] + [str(a) for a in args], cwd=str(cwd), env=env,
                timeout=timeout, capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            return Run(False, f"cargo {args[0]} timed out after {timeout}s")
        except OSError as exc:
            return Run(False, f"cargo {args[0]} could not be run: {exc}")
    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-4:]
        return Run(False, f"cargo {args[0]} exited {proc.returncode}: "
                          + " | ".join(tail), proc.stdout)
    return Run(True, f"cargo {args[0]} exited 0", proc.stdout)


def probe_compiles(candidate: Candidate) -> Verdict:
    """Item 536's two readings of `compiles`: the crate build and the matrix.

    Half one is the gate crate. `cargo check --offline` on
    `crates/revl-gate` compiles the candidate's own `selfhost.rs`, which is the
    largest generated artifact in the tree and the one a digest gate cannot
    speak for: `tools/build_gate_crate.py --check` compares BYTES, so a
    regenerated crate can be byte-correct and not compile. That has happened
    here. No cargo on the machine is a FAILURE, not a skip, and it is the exact
    case the module docstring's 7-of-8 argument is about.

    Half two is the six-tier matrix. `tools/conformance.py --json` walks every
    construct through all six emitters and labels each refusal `deliberate`
    (the emitter raised its own `EmitError`, a named tier limit) or not (the
    emitter had no case and crashed). A deliberate limit is the toolchain
    working as designed; anything else is a REAL GAP, and the bar is zero real
    gaps on every tier, which is where `origin/main` sits. A tier missing from
    a case row fails too, so the matrix cannot shrink its way to green.
    """
    name = "compiles"
    crate = candidate.tree / GATE_CRATE
    if not (crate / "Cargo.toml").is_file():
        return failed(name,
                      f"{GATE_CRATE}/Cargo.toml is not present in the candidate tree")
    run = run_cargo(candidate, ["check", "--offline", "--quiet"], cwd=crate)
    if not run.ok:
        return failed(name, run.detail, [f"cargo check --offline in {GATE_CRATE}"])

    walk = run_tool(candidate, ["tools/conformance.py", "--json"])
    if not walk.ok:
        return failed(name, walk.detail,
                      [f"cargo check --offline in {GATE_CRATE}"])
    try:
        report = json.loads(walk.stdout)
    except ValueError as exc:
        return failed(name, f"tools/conformance.py --json did not print a report: {exc}")
    cases = report.get("cases")
    if not cases:
        return failed(name, "the six-tier matrix walked no construct at all")
    for row in cases:
        missing = [t for t in SIX_TIERS if t not in (row.get("tiers") or {})]
        if missing:
            return failed(
                name,
                f"the matrix case {row.get('case')!r} carries no verdict for "
                + ", ".join(missing) + ", so that tier was not walked")
    real = []
    for tier in SIX_TIERS:
        for item in report.get("gaps", {}).get(tier, []):
            if not item.get("deliberate"):
                real.append(f"{tier}: {item.get('case')} ({item.get('message', '')[:60]})")
    if real:
        return failed(
            name,
            f"{len(real)} real emitter gap(s) in the six-tier matrix (a crash, "
            "not a named tier limit): " + "; ".join(real[:4]),
            ["tools/conformance.py --json"])
    return verified(
        name,
        f"the gate crate compiles and all six tiers emit {len(cases)} construct(s) "
        "with no real gap",
        [f"cargo check --offline in {GATE_CRATE}", "tools/conformance.py --json"])


# ------------------------------------------------------ tests, actually run

#: `tools/affected_tests.py`'s `BACKENDS` keys mapped to the pytest files
#: `tools/pre_merge.sh` runs for them. `python` and `typescript` are absent ON
#: PURPOSE: their suites need `backends/python/.venv` and a vitest install, and
#: `pre_merge.sh` SKIPS them when those are missing. A skip is the fail-open
#: shape, so a selection that names them fails this component by name instead.
BACKEND_SUITES = {
    "go": ("backends/go/test_emit_go.py",),
    "rust": ("backends/rust/test_emit_rust.py",),
    "wasm": ("backends/wasm/test_v3_emit.py", "backends/wasm/test_canonical_abi.py"),
    "java": ("backends/java/test_emit_java.py",),
}

#: A FULL selection is the whole `tests/` tree, which is what the selector means
#: by falling safe.
FULL_SELECTION = ("tests/",)

TESTS_TIMEOUT = 3600

#: `N passed` in a `-q` summary. A run with NO count collected nothing, and a
#: pytest that collected nothing exits 5 on modern pytest but exits 0 under
#: `--ignore` shapes that empty the selection. Both are failures here.
_PASSED = re.compile(r"(\d+) passed")


def _selection(candidate: Candidate):
    """`(ok, selection_or_error)` from `tools/affected_tests.py --format machine`.

    The selector's own output is READ, never assumed. It falls safe to FULL on
    an unmapped path, and a probe that guessed a narrow selection would run less
    than the repository's own gate asks for.
    """
    run = run_tool(candidate, ["tools/affected_tests.py", "--base", candidate.base,
                               "--format", "machine"], timeout=600)
    if not run.ok:
        return False, run.detail
    keys = {}
    for line in run.stdout.splitlines():
        if line.startswith("# ") or not line.strip():
            continue
        head, _, rest = line.partition(" ")
        keys[head] = rest.strip()
    if "FULL" not in keys or "PYTEST" not in keys:
        return False, ("tools/affected_tests.py printed no FULL/PYTEST line, so "
                       "the selection could not be read")
    return True, keys


def probe_tests(candidate: Candidate) -> Verdict:
    """The affected suite, selected by the repository's selector and then RUN.

    Two failure directions the design doc names, both closed here:

      * a selection that was GUESSED. `tools/affected_tests.py` is run in the
        candidate tree and its machine output is parsed; a FULL selection means
        the whole `tests/` tree, because that is what falling safe means.
      * a run that collected NOTHING. A pytest summary with no test count is a
        suite that did not run, and it is indistinguishable from a green one by
        exit status alone. The probe requires a parsed `N passed` with N > 0.

    The per-backend emit suites are added for every tier the selector names,
    because `pytest tests/` does not contain them -- that is the wave gap this
    repository has already paid for twice. A tier whose suite this probe cannot
    run (`python`, `typescript`) fails the component rather than being skipped.
    """
    name = "tests"
    ok, keys = _selection(candidate)
    if not ok:
        return failed(name, f"the affected selection could not be read: {keys}")

    full = keys["FULL"].strip() == "1"
    targets = list(FULL_SELECTION) if full else keys["PYTEST"].split()
    backends = [t for t in keys.get("BACKENDS", "").split() if t]
    unrunnable = sorted(t for t in backends if t not in BACKEND_SUITES)
    if unrunnable:
        return failed(
            name,
            "the selector asked for the " + ", ".join(unrunnable)
            + " backend suite(s), which need a toolchain this probe does not "
              "set up; `tools/pre_merge.sh` SKIPS them and a skip is not a pass",
            [f"tools/affected_tests.py --base {candidate.base}"])
    for tier in backends:
        targets += [t for t in BACKEND_SUITES[tier] if t not in targets]
    if not targets:
        return failed(
            name,
            f"the selection named no test at all (reason: {keys.get('REASON', '?')})",
            [f"tools/affected_tests.py --base {candidate.base}"])

    missing = [t for t in targets if not (candidate.tree / t.split("::")[0]).exists()]
    if missing:
        return failed(name, "the selection names path(s) not in the tree: "
                            + ", ".join(missing[:4]))

    env = dict(os.environ)
    env["PYTHONPATH"] = (str(candidate.tree / "src") + os.pathsep
                         + env.get("PYTHONPATH", "")).rstrip(os.pathsep)
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"] + targets
    try:
        proc = subprocess.run(cmd, cwd=str(candidate.tree), env=env,
                              timeout=TESTS_TIMEOUT, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return failed(name, f"the affected suite timed out after {TESTS_TIMEOUT}s")
    except OSError as exc:
        return failed(name, f"the affected suite could not be run: {exc}")
    out = proc.stdout + proc.stderr
    match = _PASSED.search(out)
    if proc.returncode != 0:
        tail = out.strip().splitlines()[-3:]
        return failed(name, f"the affected suite exited {proc.returncode}: "
                            + " | ".join(tail),
                      [f"pytest {' '.join(targets[:4])}"])
    if match is None or int(match.group(1)) == 0:
        return failed(
            name,
            "the affected suite exited 0 with no test count in its summary, so "
            "it collected nothing; a suite that ran zero tests is not a pass",
            [f"pytest {' '.join(targets[:4])}"])
    return verified(
        name,
        f"{match.group(1)} test(s) passed over the selection the repository's "
        f"own selector made ({'FULL' if full else str(len(targets)) + ' target(s)'})",
        [f"tools/affected_tests.py --base {candidate.base}",
         f"pytest {' '.join(targets[:4])}"])


# --------------------------------------------- conformance, the two registers

CONFORMANCE_DOC = "docs/conformance.md"
GUARANTEE_START = "<!-- GUARANTEE-TIER-MATRIX:START -->"
GUARANTEE_END = "<!-- GUARANTEE-TIER-MATRIX:END -->"

#: `tools/tier_guarantees.py`'s verdicts, STRONGEST FIRST. The order is the
#: whole content of the ratchet: a cell may move up it, never down.
CELL_STRENGTH = ("proved", "divergence", "no reproducer", "unimplemented")

#: The committed block renders the same four verdicts as glyphs.
CELL_GLYPHS = {"proved": "proved", "**div**": "divergence",
               "no repro": "no reproducer", "unimpl": "unimplemented"}


def _committed_matrix(text: str):
    """`(ok, cells_or_error)`: (code, tier) -> verdict from the committed block.

    The block is `tools/conformance.py --write-readme`'s rendering of
    `tools/tier_guarantees.py`, whose fourth source IS the divergence registers:
    `check_roadmap_markers.py --check-tier-parity`'s records and
    `tests/test_cross_tier_execution.py::DIVERGENCES`. Reading the committed
    block at `base` is how the candidate's fresh measurement gets something to
    be a ratchet against without checking out a second tree.
    """
    start = text.find(GUARANTEE_START)
    end = text.find(GUARANTEE_END)
    if start < 0 or end < 0:
        return False, f"{CONFORMANCE_DOC} carries no GUARANTEE-TIER-MATRIX block"
    block = text[start:end].splitlines()
    header = None
    cells = {}
    for line in block:
        if not line.startswith("|"):
            continue
        parts = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            if parts and parts[0] == "guarantee":
                header = parts[1:-1]          # tiers, dropping `evidence`
            continue
        if len(parts) != len(header) + 2 or not parts[0].startswith("`"):
            continue
        code = parts[0].strip("`")
        for tier, glyph in zip(header, parts[1:-1]):
            verdict = CELL_GLYPHS.get(glyph)
            if verdict is not None:
                cells[(code, tier)] = verdict
    if header is None or not cells:
        return False, (f"the GUARANTEE-TIER-MATRIX block in {CONFORMANCE_DOC} "
                       "holds no readable guarantee x tier table")
    return True, cells


def probe_conformance(candidate: Candidate) -> Verdict:
    """Cross-tier divergence, read off the two registers that record it.

    ITEM 536 NAMED `tools/gate_verdict_parity.py` FOR THIS COMPONENT AND THAT
    FILE HAS NEVER EXISTED (issue #1233, roadmap item 547). Slice 1 failed the
    component by name and asserted the file's absence so the decision could not
    be skipped. The decision, taken here: do NOT build it. The roadmap sentence
    now names what actually records cross-tier divergence in this tree, the
    `--check-tier-parity` records plus `tests/test_cross_tier_execution.py`'s
    `DIVERGENCES`, and inventing a ninth tool to wrap two registers that already
    exist would add a gate rather than read one, which section 7 forbids.

    Both registers are read through `tools/tier_guarantees.py`, which is their
    only consumer and is TOTAL over them: a `--check-tier-parity` subject with
    no guarantee mapping and a `DIVERGENCES` entry with no code mapping each
    raise `MatrixError` rather than being dropped, so a register that grows
    without a decision exits the tool non-zero and FAILS this component.

    The ratchet: no `(guarantee, tier)` cell may be weaker than it is at `base`.
    Weaker is `CELL_STRENGTH`'s order, so `proved -> divergence` fails, while
    `unimplemented -> divergence` (a partial port arriving) passes -- a tier
    LOSING a guarantee is the promotion-bar entry "no weakened refusal", and a
    tier gaining part of one is the work. A cell absent at `base` is a new
    guarantee, which `tools/tier_guarantees.py` already refuses to render blank.
    """
    name = "conformance"
    run = run_tool(candidate, ["tools/tier_guarantees.py", "--json"], timeout=1800)
    if not run.ok:
        return failed(name, run.detail, ["tools/tier_guarantees.py --json"])
    try:
        matrix = json.loads(run.stdout)
    except ValueError as exc:
        return failed(name, f"tools/tier_guarantees.py --json printed no matrix: {exc}")
    head = {}
    for row in matrix.get("rows", []):
        for tier, cell in (row.get("cells") or {}).items():
            head[(row.get("code"), tier)] = cell.get("verdict")
    if not head:
        return failed(name, "the guarantee x tier matrix came back with no cell, "
                            "so no register was read")

    ok, base_text = run_git(candidate, ["show", f"{candidate.base}:{CONFORMANCE_DOC}"])
    if not ok:
        return failed(name,
                      f"cannot read {CONFORMANCE_DOC} at {candidate.base}: {base_text}")
    ok, base_cells = _committed_matrix(base_text)
    if not ok:
        return failed(name, f"at {candidate.base}: {base_cells}")

    weakened = []
    for key, before in sorted(base_cells.items()):
        after = head.get(key)
        if after is None:
            weakened.append(f"{key[0]} on {key[1]}: the row is gone")
        elif (before in CELL_STRENGTH and after in CELL_STRENGTH
              and CELL_STRENGTH.index(after) > CELL_STRENGTH.index(before)):
            weakened.append(f"{key[0]} on {key[1]}: {before} -> {after}")
    if weakened:
        return failed(
            name,
            f"{len(weakened)} guarantee x tier cell(s) weaker than at "
            + candidate.base + ": " + "; ".join(weakened[:6]),
            ["tools/tier_guarantees.py --json",
             f"{CONFORMANCE_DOC}@{candidate.base}"])
    return verified(
        name,
        f"{len(head)} guarantee x tier cell(s) measured from the divergence "
        f"registers, none weaker than at {candidate.base}",
        ["tools/tier_guarantees.py --json (--check-tier-parity records + "
         "tests/test_cross_tier_execution.py::DIVERGENCES)",
         f"{CONFORMANCE_DOC}@{candidate.base}"])


# ----------------------------------------------------- formal, from the ledger

FORMAL_AXIOMS = "formal/CheckAxioms.lean"
FORMAL_NONVACUITY = "formal/scripts/nonvacuity.tsv"
_AXIOM_LINE = re.compile(r"^\s*#print axioms\s+(\S+)\s*$", re.M)

#: A `contentless` row is a FINDING in `nonvacuity_gate.py`'s own words: the
#: theorem is true by definition. A theorem DOWNGRADED to it has lost content
#: without losing its row, which a set comparison alone would not see.
CONTENTLESS = "contentless"


def _formal_ledger(text_axioms: str, text_tsv: str):
    """`(theorems, kinds)` from the two committed ledger files."""
    theorems = set(_AXIOM_LINE.findall(text_axioms))
    kinds = {}
    for line in text_tsv.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            kinds[parts[0].strip()] = parts[1].strip()
    return theorems, kinds


def probe_formal(candidate: Candidate) -> Verdict:
    """`formal/`, read as a ledger and compared against `base`.

    Item 536's promotion bar states this one negatively: "no reduced formal
    coverage". So the component is not "the proofs are green" -- building Lean
    is `make formal`'s job and needs a toolchain -- it is that the LEDGER the
    proofs are registered in did not shrink, plus the two gates over that ledger
    that are pure python and can therefore always run.

    Two gates, both fail-closed, neither needing Lean:

      * `formal/scripts/nonvacuity_gate.py`: every registered theorem carries a
        row naming the evidence its hypotheses can hold at once, every witness
        is itself registered, no theorem witnesses itself, and `CheckAxioms.lean`
        and `run_gate.sh` register the SAME set. A theorem deleted from one and
        not the other fails here.
      * `formal/scripts/layering_gate.py`: L0/L1/L2 import discipline.

    Then the coverage read against `base`: no registered theorem may disappear,
    and no theorem may be downgraded to `contentless`, which is the shape that
    keeps a row while losing what the row was worth. A theorem ADDED is the work
    and passes.
    """
    name = "formal"
    evidence = []
    for argv in (["formal/scripts/nonvacuity_gate.py"],
                 ["formal/scripts/layering_gate.py"]):
        run = run_tool(candidate, argv, timeout=600)
        if not run.ok:
            return failed(name, run.detail, evidence + [argv[0]])
        evidence.append(argv[0])

    head_files = {}
    for rel in (FORMAL_AXIOMS, FORMAL_NONVACUITY):
        path = candidate.tree / rel
        if not path.is_file():
            return failed(name, f"{rel} is not present in the candidate tree")
        try:
            head_files[rel] = path.read_text(encoding="utf-8")
        except OSError as exc:
            return failed(name, f"{rel} is unreadable: {exc}")
    base_files = {}
    for rel in (FORMAL_AXIOMS, FORMAL_NONVACUITY):
        ok, text = run_git(candidate, ["show", f"{candidate.base}:{rel}"])
        if not ok:
            return failed(name, f"cannot read {rel} at {candidate.base}: {text}")
        base_files[rel] = text

    head_theorems, head_kinds = _formal_ledger(
        head_files[FORMAL_AXIOMS], head_files[FORMAL_NONVACUITY])
    base_theorems, base_kinds = _formal_ledger(
        base_files[FORMAL_AXIOMS], base_files[FORMAL_NONVACUITY])
    if not base_theorems:
        return failed(name, f"{FORMAL_AXIOMS} at {candidate.base} registers no "
                            "theorem, so there is nothing to compare against")

    dropped = sorted(base_theorems - head_theorems)
    if dropped:
        return failed(
            name,
            f"formal coverage SHRANK against {candidate.base}: "
            f"{len(dropped)} registered theorem(s) removed from {FORMAL_AXIOMS}: "
            + ", ".join(dropped[:6]),
            evidence + [FORMAL_AXIOMS])
    downgraded = sorted(
        t for t, kind in head_kinds.items()
        if kind == CONTENTLESS and base_kinds.get(t) not in (None, CONTENTLESS))
    if downgraded:
        return failed(
            name,
            f"{len(downgraded)} theorem(s) downgraded to `{CONTENTLESS}` against "
            + candidate.base + " (the row survives, the content does not): "
            + ", ".join(downgraded[:6]),
            evidence + [FORMAL_NONVACUITY])
    return verified(
        name,
        f"{len(head_theorems)} registered theorem(s), none removed and none "
        f"downgraded against {candidate.base}; both ledger gates clean",
        evidence + [f"{FORMAL_AXIOMS}@{_sha256(candidate.tree / FORMAL_AXIOMS)}"])


# ------------------------------------------------------------ the ninth term

PROGRESS_TOOL = "tools/evolution_progress.py"


def probe_progress(candidate: Candidate) -> Verdict:
    """The progress conjunct (issue #1224, roadmap item 545), read as a tool.

    `tools/evolution_progress.py` builds a `ProgressVerdict` with this module's
    four fields and the same single truth value, so it registers in `PROBES`
    with no adaptation and retention stays `all()` over `COMPONENTS`. It is run
    as a subprocess in the candidate tree like every other component's tool,
    rather than imported, so a candidate's copy of it cannot take the scorer
    down and cannot outrun `run_tool`'s timeout.

    The tool is the conservation half only: `verified` iff every counter was
    read on BOTH sides and none regressed. Whether a candidate ADVANCED a
    counter is deliberately not a component -- it is a generation-level
    existential over the already-retained candidates, which is what keeps
    progress out of any trade against the eight conservation components. That
    quantifier lives in `evolution_progress.promote`, not here.

    Until that tool is in the tree the component FAILS with the file named,
    which is the same answer `run_tool` gives any missing tool.
    """
    name = "progress"
    with tempfile.TemporaryDirectory(prefix="evolution-reward-progress-") as raw:
        out = Path(raw) / "progress.json"
        run = run_tool(candidate, [PROGRESS_TOOL, "--tree", str(candidate.tree),
                                   "--base", candidate.base, "--json", str(out)],
                       timeout=1800)
        payload = None
        if out.is_file():
            try:
                payload = json.loads(out.read_text())
            except ValueError:
                payload = None
    block = (payload or {}).get("progress", {})
    verdict = block.get("verdict") or {}
    if verdict.get("component") == name:
        # The tool's own verdict, used verbatim in both directions.
        return Verdict(name, verdict.get("verdict") == "verified",
                       str(verdict.get("reason", "")),
                       tuple(verdict.get("evidence", ())) or (PROGRESS_TOOL,))
    return failed(name, run.detail if not run.ok else
                  f"{PROGRESS_TOOL} exited 0 but wrote no progress verdict",
                  [PROGRESS_TOOL])


# The registry. A component with no probe FAILS (see `score`); it is never
# absent from the scorecard and never defaults to pass.
PROBES = {
    "compiles": probe_compiles,
    "tests": probe_tests,
    "no-new-false-admits": probe_no_new_false_admits,
    "conformance": probe_conformance,
    "artifact-stability": probe_artifact_stability,
    "formal": probe_formal,
    "scope": probe_scope,
    "documentation": probe_documentation,
    "progress": probe_progress,
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
