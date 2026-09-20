#!/usr/bin/env python3
"""Held-out scoring: judge a candidate on a set of programs it could not read.

WHY THIS EXISTS
---------------
Every corpus that scores a change in this repository is a file in this
repository. The differential oracle's programs are two literal lists in
`tests/test_selfhost_lower.py`; the census corpus is the eight directories
`tools/gate_reference_census.py` names; the issued-admission programs are
`ADMISSION_PROGRAMS` in that same tool; the gate's fixtures are under
`tests/fixtures/`. A candidate that emits a diff against this tree has read
all of them, so the reward is a function of the candidate's own input.

A repository cannot hide a file from a process that has the repository. So the
thing held out here is NOT a file and NOT the mechanism. Everything below is
public and meant to be read: the grammar, the scorer, the buckets, the fence.
What is held out is the DRAW -- the specific finite set of programs one scoring
run uses. It does not exist until score time, it is never written to the tree,
and its identity is fixed by a seed that is proved absent from the repository
before a single program is generated.

See `docs/design/531-held-out-scoring.md` for the options that were rejected
and the attacks each one left open.

THE THREE ENFORCEMENTS
----------------------
1. THE SET IS NOT A FILE. `draw()` generates programs in memory. Nothing here
   writes a `.rvl` anywhere, which also keeps the draw out of the globbed
   fixture directories that would make it readable on the next run.

2. THE SEED IS PROVED ABSENT FROM THE TREE. `--seed` / `REVL_HELDOUT_SEED` is
   checked with `git grep --fixed-strings` over the tracked tree, and a seed
   that is found there is REFUSED. This is the enforcement that makes "the
   candidate cannot read it" a check instead of a promise: the repository is
   readable, so the seed is required to be outside it and that requirement is
   tested rather than asserted.

3. THE CANDIDATE'S DIFF MUST NOT REACH THE SCORER. `HELD_OUT_FENCE` lists the
   paths that decide what is drawn and what counts as a divergence. The
   candidate's changed-file set is required, and a diff that touches a fenced
   path is REFUSED rather than scored, because a candidate that edited the
   scorer has not been scored by it. Refused is not failed: it is the verdict
   that routes a semantic change to a human, which is what item 537 asks for.

THE FAILURE DIRECTION IS CLOSED
-------------------------------
Every unknown refuses. No seed, a seed found in the tree, no diff, a diff that
reaches the fence, an engine that will not build, a draw that raises -- each
exits non-zero with a named refusal and NO score. There is deliberately no
path on which this tool falls back to scoring against the in-tree corpus: a
gate that silently scores a readable set when the held-out one is unavailable
is the fail-open shape, and it is the shape this tool refuses to have.

WHAT SLICE 1 SCORES
-------------------
The zero-tolerance direction only: `false-admission` (the gate ISSUED an
admission for a program the reference refuses) and `gate-fault` (the gate
crashed). Those are the two buckets `tools/gate_reference_census.py` gives no
allowance at all. The `false-admit` bypass direction is deliberately out of
this slice: its allowance is a baselined family list, and a random draw
re-finds the families already in it -- measured, see the design doc.

USAGE
-----
    tools/heldout_scoring.py --diff-base origin/main --count 200
    REVL_HELDOUT_SEED=... tools/heldout_scoring.py --diff-paths src/revl/lower.py
    tools/heldout_scoring.py --diff-base origin/main --json verdict.json

Exit status: 0 scored clean, 1 scored and a zero-tolerance bucket is
non-empty, 2 REFUSED (no score was produced).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Exit statuses. `REFUSED` is distinct from `DIVERGENT` on purpose: a promotion
# treats both as not-a-pass, but a reader has to be able to tell "the gate ran
# and found something" from "the gate declined to run", and a single non-zero
# status collapses exactly the distinction this tool exists to make.
OK = 0
DIVERGENT = 1
REFUSED = 2

# ------------------------------------------------------------------- the fence

# The paths that decide WHAT IS DRAWN and WHAT COUNTS AS A DIVERGENCE. A
# candidate diff that touches one of these is refused rather than scored.
#
# What is NOT here is as load-bearing as what is: `selfhost/`, `src/revl/`,
# `backends/` and `crates/` are the SUBJECT. A candidate improving the gate has
# to be able to change them, and fencing them would forbid the work this loop
# exists to produce. The fence is the scorer, never the subject.
HELD_OUT_FENCE = (
    "tools/heldout_scoring.py",
    "tests/test_heldout_scoring.py",
    "tools/gate_reference_census.py",
    "tools/build_gate_crate.py",
    "tests/test_selfhost_lower.py",
)

# The subject: the top-level directories a candidate is allowed to change and
# that the gate measures. `tests` is one of them on purpose -- a candidate that
# improves the compiler changes tests with it, and the two test files the fence
# names are exempted by the fence rather than by forbidding the directory.
# Every in-repo file a scoring run loads has to fall in one category or the
# other; `tests/test_heldout_scoring.py` asserts the closure carries no third,
# so a dependency added without a decision is a RED rather than a silent hole.
SUBJECT_ROOTS = (
    "src",
    "selfhost",
    "backends",
    "crates",
    "stdlib",
    "tests",
    "examples",
    "tck",
    "dogfood",
    "demo",
)

# Repo files a scoring run loads that `sys.modules` does not show, because they
# are read as data or executed through `spec_from_file_location` without being
# registered. They are listed rather than discovered so the closure check can
# be complete; `tests/test_heldout_scoring.py` classifies each one.
DATA_INPUTS = (
    "selfhost/lower.rvl",
    "backends/python/emit.py",
    "tools/build_gate_crate.py",
)

# Repo paths the census names that a scoring run never reaches: they belong to
# `--check`, `--record`, `--fuzz` and `--engine crate`, modes `run()` does not
# call. Declared rather than assumed, so a future edit that starts reaching one
# fails the classification test instead of widening the fence silently.
SCORING_UNREACHED = (
    "tools/gate_reference_census_baseline.json",
    "tools/fuzz_frontend.py",
)


def classify(path: str):
    """`"fence"`, `"subject"`, `"unreached"` or None for a repo-relative path.

    None is the answer that matters: an in-repo file a scoring run loads and
    that is in no category is a hole in the fence, and the classification test
    turns it into a RED rather than leaving it to be noticed.
    """
    norm = path.replace("\\", "/").strip("/")
    if norm in HELD_OUT_FENCE:
        return "fence"
    if norm in SCORING_UNREACHED:
        return "unreached"
    if norm.split("/", 1)[0] in SUBJECT_ROOTS:
        return "subject"
    return None

# A seed shorter than this is not searched for in the tree: a short string
# matches somewhere by accident and the refusal would be noise rather than a
# finding. It is refused outright instead.
MIN_SEED_CHARS = 16

GRAMMAR = "admission-surface/v1"

# The rotation this slice implements, recorded in the verdict so the claim is
# in the artifact rather than in prose: one draw per invocation, its identity
# fixed by an operator-supplied seed that is not in the tree. Nothing here
# holds a seed between runs, so a repeated seed is the operator's choice and
# `seed_digest` in the verdict is what makes that choice visible.
ROTATION = "per-invocation; seed supplied by the operator, proved absent from the tracked tree"


# ------------------------------------------------------------- the refusal type

class Refusal(Exception):
    """A named reason this run produced no score."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------- the seed

def seed_material(argv_seed, env):
    """The seed and where it came from, or a refusal."""
    if argv_seed:
        return argv_seed, "argv"
    from_env = env.get("REVL_HELDOUT_SEED", "")
    if from_env:
        return from_env, "env"
    raise Refusal("no-seed")


def seed_is_in_tree(seed: str, root: Path) -> bool:
    """True when `seed` occurs verbatim in the tracked tree.

    `git grep` over the index rather than a walk of the working directory: the
    question is whether a candidate reading the repository can read the seed,
    and what a candidate reads is what is tracked.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "grep", "--fixed-strings", "--quiet",
             "--no-color", "-I", "--", seed],
            capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refusal("seed-check-unavailable:" + type(exc).__name__)
    if done.returncode == 0:
        return True
    if done.returncode == 1:
        return False
    raise Refusal("seed-check-unavailable:git-exit-" + str(done.returncode))


def checked_seed(argv_seed, env, root: Path):
    """`(seed, source)` for a seed that is usable, or a refusal.

    The three refusals here are the whole of enforcement 2, and they are all
    fail-closed: no seed refuses, a short seed refuses, a seed that is in the
    tree refuses, and a tree that cannot be searched refuses.
    """
    seed, source = seed_material(argv_seed, env)
    if len(seed) < MIN_SEED_CHARS:
        raise Refusal("seed-too-short")
    if seed_is_in_tree(seed, root):
        raise Refusal("seed-in-tree")
    return seed, source


def seed_digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------- the fence

def fence_verdict(changed, fence=HELD_OUT_FENCE):
    """The fenced paths a candidate's changed-file set reaches, in order.

    `changed` is repo-relative. A path outside the fence is not inspected: this
    is a reachability question about the scorer, not a review of the diff.
    """
    reached = []
    for path in changed:
        norm = path.replace("\\", "/").lstrip("./")
        if norm in fence:
            reached.append(norm)
    return reached


def changed_from_base(root: Path, base: str):
    """The candidate's changed-file set, from git, or a refusal.

    Two dots, not three: a three-dot diff is against the merge base, which
    hides a scorer edit that arrived by merging main. The question this fence
    asks is "what does the tree the gate is about to run differ from", and that
    is the two-dot form.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "diff", "--name-only", base + "..HEAD"],
            capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refusal("diff-unavailable:" + type(exc).__name__)
    if done.returncode != 0:
        raise Refusal("diff-unavailable:git-exit-" + str(done.returncode))
    return [line for line in done.stdout.splitlines() if line.strip()]


# ------------------------------------------------------------------- the grammar

# The admission surface: the region `crates/revl-gate` will ISSUE an admission
# for, which is the region carrying no term the reference type layer decides.
# Interface declarations and transparent scalar aliases, and nothing else.
_SCALARS = ("Int", "Int32", "Float", "Str", "Bool", "Bytes")

# Deliberately ordinary words. A generated program that is obviously generated
# invites a candidate to recognise the shape rather than to be correct over it;
# that is not a defence and the design doc says so, but there is no reason to
# hand the recognition away for free either.
_WORDS = (
    "store", "clock", "log", "cache", "queue", "meter", "vault", "index",
    "router", "ledger", "probe", "relay", "spool", "tally", "warden",
    "beacon", "cursor", "digest", "emit", "fold", "shard", "anchor",
    "ripple", "gantry", "lantern", "kernel", "pilot", "quay",
)
_SUFFIXES = ("", "_a", "_b", "_c", "_hi", "_lo", "2", "3", "x")

# The near-miss families. Each is one token outside the admission surface, and
# each is a refusal the reference issues, so an issued admission for any of
# them is a `false-admission` -- the bucket with no allowance at all. These
# generalise the fourteen hand-written `ADMISSION_PROGRAMS` in the census tool
# from a list a candidate can memorise into a space it has to be correct over.
NEAR_MISS_FAMILIES = (
    "duplicate_service",
    "duplicate_method",
    "fn_body",
    "generic_type",
    "record_alias",
    "unknown_type",
    "component",
)


def _ident(rng, used):
    for _ in range(64):
        word = rng.choice(_WORDS) + rng.choice(_SUFFIXES)
        if word not in used:
            used.add(word)
            return word
    raise Refusal("draw-exhausted-identifiers")


def _cap(word: str) -> str:
    return word[0].upper() + word[1:]


def _inside(rng):
    """A program strictly inside the admission surface."""
    used = set()
    lines = []
    aliases = []
    for _ in range(rng.randint(0, 3)):
        name = _cap(_ident(rng, used))
        aliases.append(name)
        lines.append("type " + name + " = " + rng.choice(_SCALARS))
    if aliases:
        lines.append("")
    spellings = _SCALARS + tuple(aliases)
    services = []
    for _ in range(rng.randint(1, 3)):
        name = _cap(_ident(rng, used))
        services.append(name)
        method_names = set()
        body = []
        for _ in range(rng.randint(0, 4)):
            method = _ident(rng, method_names)
            params = ", ".join(
                _ident(rng, method_names) + ": " + rng.choice(spellings)
                for _ in range(rng.randint(0, 3)))
            ret = ""
            if rng.random() < 0.6:
                ret = " -> " + rng.choice(spellings)
            body.append("  fn " + method + "(" + params + ")" + ret)
        lines.append("service " + name + " {")
        lines.extend(body)
        lines.append("}")
        lines.append("")
    if rng.random() < 0.3:
        lines.insert(0, "// " + rng.choice(_WORDS) + " surface")
    return "\n".join(lines).rstrip() + "\n", services


def _near_miss(rng, family, base, services):
    """`base` pushed one token outside the surface, or None when `base` has no
    site for `family` (a draw with no service cannot duplicate one)."""
    tag = str(rng.randint(10, 99))
    if family == "duplicate_service":
        if not services:
            return None
        name = rng.choice(services)
        return base + "\nservice " + name + " {\n  fn probe(x: Int) -> Int\n}\n"
    if family == "duplicate_method":
        marker = "\n  fn "
        if marker not in base:
            return None
        head, rest = base.split(marker, 1)
        name = rest.split("(", 1)[0]
        line = marker + name + "(dup: Str) -> Str"
        return head + line + marker + rest
    if family == "fn_body":
        return base + "\nfn twice" + tag + "(x: Int) -> Int { return x + x }\n"
    if family == "generic_type":
        return base + "\nservice Box" + tag + " {\n  fn all() -> List[Int]\n}\n"
    if family == "record_alias":
        return base + "\ntype Row" + tag + " = { id: Int }\n"
    if family == "unknown_type":
        return (base + "\nservice Edge" + tag + " {\n  fn a(x: Mystery"
                + tag + ") -> Int\n}\n")
    if family == "component":
        if not services:
            return None
        name = services[0]
        return (base + "\ncomponent Comp" + tag + " provides s: " + name
                + " {\n  provide s {\n  }\n}\n")
    raise Refusal("draw-unknown-family:" + family)


def draw(seed: str, count: int):
    """`[(case_id, source)]` -- the held-out set for this run.

    A pure function of `(seed, count)` that touches no file. An auditor handed
    the seed reproduces the draw byte for byte; a candidate without it cannot,
    and that asymmetry is the whole of the property.
    """
    if count < 2:
        raise Refusal("draw-too-small")
    rng = random.Random(int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest(), "big"))
    cases = []
    index = 0
    while len(cases) < count:
        base, services = _inside(rng)
        cases.append(("inside:" + str(index), base))
        if len(cases) == count:
            break
        family = rng.choice(NEAR_MISS_FAMILIES)
        variant = _near_miss(rng, family, base, services)
        if variant is not None:
            cases.append(("near:" + family + ":" + str(index), variant))
        index += 1
    return cases


def draw_digest(cases) -> str:
    """A digest over the draw, so a verdict's claim about what was scored is
    checkable by anyone later handed the seed."""
    hasher = hashlib.sha256()
    for case_id, source in cases:
        hasher.update(case_id.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(source.encode("utf-8"))
        hasher.update(b"\0")
    return "sha256:" + hasher.hexdigest()


# -------------------------------------------------------------------- the score

def load_census():
    """`tools/gate_reference_census.py`, imported by path.

    The bucket vocabulary, the reference and the fast gate engine are the
    census's, not a second copy: two classifiers would be free to drift into
    disagreeing about what a divergence even is, and this tool's whole claim is
    that it measures the same thing the census measures on a set the candidate
    could not read.
    """
    path = ROOT / "tools" / "gate_reference_census.py"
    spec = importlib.util.spec_from_file_location("heldout_census", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # a scorer that will not build must not score
        raise Refusal("engine-unavailable:" + type(exc).__name__)
    return module


# The buckets with no allowance anywhere in the census: `false-admission` is in
# its `NEVER_BASELINED`, and a `gate-fault` is a crash rather than a verdict.
ZERO_TOLERANCE = ("false-admission", "gate-fault")


def score(cases, census, engine=None, reference=None):
    """`(buckets, liveness)` over the draw.

    `liveness` is the half that keeps a clean verdict from being a vacuum. The
    census bucket `agree-admit` conflates two different gate answers -- an
    ISSUED admission and a mere no-objection where the reference also admits --
    so an empty `false-admission` bucket is consistent both with a sound gate
    and with a draw that never reached the admission arm at all. The counters
    below separate those, and `run()` refuses on the second.
    """
    try:
        if reference is None:
            reference, _oracle = census._reference()
        if engine is None:
            engine = census.SelfhostEngine()
    except Refusal:
        raise
    except Exception as exc:
        raise Refusal("engine-unavailable:" + type(exc).__name__)
    sources = [src for _, src in cases]
    buckets = {}
    issued = 0
    refused_by_reference = 0
    near_miss_refused = 0
    for (case_id, source), verdict in zip(cases, engine.verdicts(sources)):
        ref = reference(source)
        name = census.bucket(ref, verdict)
        buckets.setdefault(name, []).append(case_id)
        if verdict[0] == "admitted":
            issued += 1
        if ref[0] != "":
            refused_by_reference += 1
            if case_id.startswith("near:"):
                near_miss_refused += 1
    liveness = {
        "issued_admissions": issued,
        "reference_refusals": refused_by_reference,
        "near_miss_reference_refusals": near_miss_refused,
    }
    return buckets, liveness


def verdict_record(buckets, liveness, *, seed, source, count, fence,
                   changed_count):
    findings = {name: buckets.get(name, []) for name in ZERO_TOLERANCE}
    clean = not any(findings.values())
    return {
        "verdict": "clean" if clean else "divergent",
        "refusal": None,
        "grammar": GRAMMAR,
        "size": count,
        "rotation": ROTATION,
        "seed_digest": seed,
        "seed_source": source,
        "fence": list(fence),
        "candidate_changed_files": changed_count,
        "liveness": liveness,
        "zero_tolerance": findings,
        "buckets": {name: len(ids) for name, ids in sorted(buckets.items())},
    }


def refusal_record(reason: str):
    return {
        "verdict": "refused",
        "refusal": reason,
        "grammar": GRAMMAR,
        "size": 0,
        "rotation": ROTATION,
        "seed_digest": None,
        "seed_source": None,
        "fence": list(HELD_OUT_FENCE),
        "candidate_changed_files": None,
        "liveness": {},
        "zero_tolerance": {name: [] for name in ZERO_TOLERANCE},
        "buckets": {},
    }


def run(*, argv_seed, env, count, changed, root=ROOT, census=None,
        engine=None, reference=None):
    """The whole gate as one function: a verdict record and an exit status.

    Every branch that cannot answer the question returns a refusal record. No
    branch returns a score computed from anything other than the draw.
    """
    try:
        seed, source = checked_seed(argv_seed, env, root)
        if changed is None:
            raise Refusal("no-diff")
        reached = fence_verdict(changed)
        if reached:
            raise Refusal("diff-reaches-fence:" + ",".join(sorted(reached)))
        cases = draw(seed, count)
        if census is None:
            census = load_census()
        buckets, liveness = score(cases, census, engine=engine,
                                  reference=reference)
        # A clean verdict on a draw that never reached the guard is the
        # vacuous-pass shape, so it is a refusal rather than a pass. The gate
        # has to have ISSUED at least one admission (the arm ran) and the draw
        # has to carry at least one near miss the reference refuses (there was
        # something the arm could have got wrong).
        if liveness["issued_admissions"] == 0:
            raise Refusal("draw-reached-no-admission-arm")
        if liveness["near_miss_reference_refusals"] == 0:
            raise Refusal("draw-carries-no-refused-near-miss")
    except Refusal as exc:
        return refusal_record(exc.reason), REFUSED
    record = verdict_record(
        buckets,
        liveness,
        seed=seed_digest(seed),
        source=source,
        count=len(cases),
        fence=HELD_OUT_FENCE,
        changed_count=len(changed),
    )
    record["draw_digest"] = draw_digest(cases)
    return record, OK if record["verdict"] == "clean" else DIVERGENT


def render(record) -> str:
    lines = []
    if record["verdict"] == "refused":
        lines.append("held-out scoring REFUSED: " + str(record["refusal"]))
        lines.append("  no score was produced, and none was substituted.")
        return "\n".join(lines)
    lines.append("held-out scoring " + record["verdict"].upper()
                 + " over " + str(record["size"]) + " programs"
                 + " (" + record["grammar"] + ")")
    lines.append("  seed " + str(record["seed_digest"])
                 + " from " + str(record["seed_source"]))
    lines.append("  draw " + str(record.get("draw_digest")))
    live = record.get("liveness", {})
    lines.append("  liveness: " + str(live.get("issued_admissions"))
                 + " issued admissions, "
                 + str(live.get("near_miss_reference_refusals"))
                 + " near misses the reference refuses")
    for name, ids in record["zero_tolerance"].items():
        mark = "!!" if ids else "  "
        lines.append("  " + mark + " " + name + ": " + str(len(ids)))
        for case_id in ids[:6]:
            lines.append("        " + case_id)
    for name, size in record["buckets"].items():
        lines.append("     " + str(size).rjust(6) + "  " + name)
    return "\n".join(lines)


def main(argv):
    parser = argparse.ArgumentParser(
        description="score a candidate against a set it could not read")
    parser.add_argument("--seed", default="",
                        help="the held-out seed; REVL_HELDOUT_SEED otherwise")
    parser.add_argument("--count", type=int, default=200,
                        help="programs in the draw (default 200)")
    parser.add_argument("--diff-base", default="",
                        help="git ref; the candidate's changed files are "
                             "<base>..HEAD")
    parser.add_argument("--diff-paths", default="",
                        help="comma-separated changed files, instead of "
                             "--diff-base")
    parser.add_argument("--json", default="",
                        help="write the verdict record here")
    args = parser.parse_args(argv)

    changed = None
    try:
        if args.diff_paths:
            changed = [p for p in args.diff_paths.split(",") if p.strip()]
        elif args.diff_base:
            changed = changed_from_base(ROOT, args.diff_base)
        record, status = run(argv_seed=args.seed, env=os.environ,
                             count=args.count, changed=changed)
    except Refusal as exc:
        record, status = refusal_record(exc.reason), REFUSED

    print(render(record))
    if args.json:
        Path(args.json).write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
