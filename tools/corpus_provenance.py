#!/usr/bin/env python3
"""Which generation authored each scoring document, and how much of the
evidence is the engine's own work.

WHY THIS EXISTS (issue #1221, roadmap item 542)
-----------------------------------------------
`src/revl/*.py` and `selfhost/*.rvl` are two implementations held to
byte-agreement, and item 146 is worth the effort it costs BECAUSE the two were
written independently. Agreement between two implementations is evidence in
proportion to how independent they are; agreement between an implementation and
a restatement of itself is evidence of nothing.

A repository-grounded self-improvement loop writes model-authored code into the
tree, and that tree is at the same time the next generation's corpus, its
differential oracle and its scoring set. Independence is not a property the
loop preserves. It is a STOCK the loop spends, and two mechanisms already here
make the spend invisible:

  * `tests/fixtures/emit_*_corpus/` is GLOBBED, so a document dropped anywhere
    under it enters every byte-agreement projection with no list to edit.
  * `CORPUS_DIRS` in `tools/gate_reference_census.py` is walked with `rglob`
    over eight directories, which is how the census reaches ~850 programs
    without anyone maintaining a list.

Both are good properties. Both also mean a generated document is
indistinguishable from a hand-written one at the point where it is used as
evidence. The failure this file addresses is not that a model writes a corpus
document. It is that nothing could report what fraction of the evidence behind
"the gate agrees with the reference" is evidence the engine authored about
itself.

THE MANIFEST, AND WHY IT IS A SIDECAR
-------------------------------------
Provenance lives in ONE file, `tests/fixtures/corpus_provenance.json`, which
lists document names under the generation that authored them. Four places it
could have lived instead, and what each gets wrong:

  * GIT HISTORY (the introducing commit's author). Attractive, and wrong here
    twice over. First, this repository SQUASH-MERGES: a document added on a
    branch has an introducing commit that is the squash, dated and authored at
    merge time, and a rebase or a reformat rewrites it again, so the answer is
    not stable under operations the repository performs routinely. Second and
    worse, the signal was deliberately erased: every commit in this history is
    authored under a human identity by policy, so `git log --diff-filter=A`
    over the emit corpus returns the same two human names for every document
    and cannot separate the two populations even in principle. A field that
    returns one value for all inputs is not a measurement.

  * A COMMENT HEADER IN EACH DOCUMENT. It changes the BYTES of documents whose
    whole job is byte-agreement, so every emit golden on six tiers is rewritten
    to record a fact about authorship, and the claim that this is harmless
    rests on six emitters stripping comments identically, which is an
    assumption and not a checked one. It also rots in the one direction that
    matters: a new document copied from a generation-zero document inherits its
    header, and nothing cross-checks the header against anything.

  * A DIRECTORY CONVENTION (`.../gen1/foo.rvl`). Provenance would then live in
    the PATH, and the path is the identity every baseline and ledger in this
    tree keys on: `gate_reference_census_baseline.json`, `LOWER_GAP_DOCS`,
    `NATIVE_GATE_GAPS`. Re-declaring a document's provenance would rewrite
    those records, which means the cost of correcting a provenance claim is
    paid in artifacts that have nothing to do with provenance. It also cannot
    express a hand-written document a later generation substantially rewrote.

  * NOTHING, AND ASK THE MODEL. The reward is then a function of the
    candidate's prose about itself, which is exactly what item 536 removed
    everywhere else.

The sidecar's own weakness, stated rather than hidden: it can go stale, because
nothing in the filesystem forces an entry to exist. That is what `--check`
is for, and it is why the failure direction below is the load-bearing part of
this design.

THE FAILURE DIRECTION: UNDECLARED IS NOT PRE-LOOP
-------------------------------------------------
A provenance field that defaults to "pre-loop" when unknown is the
fail-open shape, and it makes the whole measurement a lie: dropping an
undeclared document into a globbed corpus would then RAISE the measured
independence. So an undeclared document resolves to `UNDECLARED`, which is
counted as loop-authored at every threshold, and `--check` names it as an
error in its own right. Adding a corpus document therefore costs one line in a
manifest, and forgetting it makes the tree LOOK WORSE rather than better. That
is the only arrangement under which the number can be trusted.

Note that the resolution is not "the current generation". `current_generation`
is 0 today, so resolving an unknown to it would be indistinguishable from
resolving it to pre-loop, and the fail-closed property would silently not
hold until the loop had run once. `UNDECLARED` is loop-authored regardless of
what generation the tree is on.

GENERATION ZERO IS A DECLARATION, NOT A MEASUREMENT
---------------------------------------------------
The generation-zero set records the tree as it stood before this file existed.
No mechanism proves anything about those documents; the claim is asserted by
the operator who recorded them, once, in a diff. What the mechanism holds from
here on is different and checkable: every document that ARRIVES must name its
generation, the set can only change in a diff somebody reads, and the fraction
is computed rather than claimed.

GENERATION IS NOT AUTHORSHIP (issue #1397)
------------------------------------------
This file used to read generation 0 as "hand-written / pre-loop", which made
the reported figure sound like a claim about who typed the bytes. It is not,
and the tree does not support that reading. Measured on `origin/main` at
1a44b34c5, by taking each generation-zero entry's introducing commit
(`git log --diff-filter=A`), resolving the first-parent landing commit for it
and reading the branch off the squash subject or the merge subject:

  * 153 of the 850 entries were added by a commit that landed from a branch
    named `agent/*`.
  * 132 more were added by a commit carrying an explicit `Co-Authored-By:
    Claude` or `Co-authored-by: Copilot` trailer, with no overlap with the
    first set: 285 by either signal.
  * 415 were added by commits pushed straight to the trunk with no pull
    request at all, so no branch name exists to read.
  * The repository's ROOT commit, 12778014, itself carries a
    `Co-Authored-By: Claude Fable 5` trailer. There is no pre-model era here
    to be the stock that generation 0 describes.

Both branch-name and trailer signals are LOWER BOUNDS: the trailer stops
appearing after 2026-09-13, and 415 entries have no branch to read. The
enclosing fact is that the whole of this corpus, 565 documents totalling
70,858 lines of `.rvl` plus 285 oracle programs written inline, arrived in 39
calendar days alongside 3,821 commits.

So generation 0 means PRE-LOOP and nothing more, and the number this file
gates is the share of the evidence that no generation of the self-improvement
loop produced. That is a real and worthwhile number: the loop is the mechanism
that would spend independence invisibly, and it has not run. It is NOT the
share a person typed.

To re-run the measurement, for each name in `generations["0"]` that ends in
`.rvl`, take the newest `git log --diff-filter=A` commit for that path; walk
`git log --ancestry-path <sha>..origin/main` down to the oldest commit that is
also on `git log --first-parent origin/main`; read the branch off that
commit's subject, which is either `Merge pull request #N from <owner>/<branch>`
or a squash subject ending `(#N)` whose branch is `gh pr view N
--json headRefName`. The 285 entries that are not paths are oracle programs
written inline in `tests/test_selfhost_lower.py`; date them by scanning
`git log --reverse -p -- tests/test_selfhost_lower.py` for the added line that
first spells the quoted program name. Pin the ref: a stale local `main` makes
the ancestry walk return nothing for a third of the set.

That second question gets its own axis, `human_authored`, which is a flat list
of case ids and is reported beside the generation table. It is fail-closed the
same way: a document not named there reads as model-authored. It carries no
floor, because a floor on it would be a floor at zero, and a floor at zero is
not a floor. Recording it as a list rather than a flag keeps the two axes
orthogonal, so declaring a document hand-typed never moves a generation and
never rewrites a baseline that keys on the generation.

NAMES ONLY
----------
The manifest records document names and nothing else. No counts, no fractions,
no line numbers, for the reason PR #1203's shrink-only ledger records names
only: a derived number in a recorded artifact rots against the thing it was
derived from, and a count written under one python is a diff under another. The
fractions here are computed at read time from the names and the tree.

Usage:
    python3 tools/corpus_provenance.py                       # the report
    python3 tools/corpus_provenance.py --check               # the gate (exit 1)
    python3 tools/corpus_provenance.py --write --generation N
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests" / "fixtures" / "corpus_provenance.json"

# The generation an undeclared document resolves to. Not an integer on purpose:
# it must compare as loop-authored at EVERY threshold, including thresholds
# above any generation the tree has reached.
UNDECLARED = "undeclared"

# The threshold the FLOOR is evaluated at. Generation 1 is the first output of
# the loop, so "independent at generation 1" means "predates the loop
# entirely". Re-basing this is an operator decision and belongs in the manifest
# beside the floor values, not here.
FLOOR_SINCE = 1

MANIFEST_NOTE = (
    "Which generation authored each scoring document (roadmap item 542, issue "
    "#1221). Generation 0 is PRE-LOOP: it predates the self-improvement loop, "
    "which is what the floors are about. It is not a claim that a person typed "
    "it, and issue #1397 measured why not. A name absent from every list is "
    "UNDECLARED, which counts as loop-authored at every threshold and is an "
    "error under `python3 tools/corpus_provenance.py --check`: an unknown "
    "provenance must never read as pre-loop, or a document dropped into a "
    "globbed corpus would raise the measured independence. `human_authored` is "
    "the second, orthogonal axis: the case ids a person typed, which a person "
    "names one at a time. It is fail-closed the same way, a document not named "
    "there reads as model-authored, and it carries no floor. Names only, no "
    "counts and no fractions, for the same reason "
    "tests/fixtures/oracle_construct_reach_ledger.json records names only: a "
    "derived number in a recorded artifact rots against what it was derived "
    "from. `floors` is the minimum percent of each scoring corpus that must "
    "remain independent of generation 1 and up; it is an operator decision and "
    "the tool holds whatever is written here."
)


# ------------------------------------------------------------- the corpora

def _census():
    """`tools/gate_reference_census.py` as a module, for its corpus tables.

    Loaded by path rather than restated, so the census and this file cannot
    disagree about which directories the census walks or which programs it
    holds. The import is lazy on BOTH sides -- the census imports this file
    from inside `main`/`report` -- so neither module executes the other while
    it is still executing itself.
    """
    spec = importlib.util.spec_from_file_location(
        "provenance_gate_reference_census", ROOT / "tools" / "gate_reference_census.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _documents(dirs, skip) -> list[str]:
    out: list[str] = []
    for sub in dirs:
        base = ROOT / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.rvl")):
            if skip & set(path.parts):
                continue
            out.append(str(path.relative_to(ROOT)))
    return out


def enumerate_corpora() -> dict[str, list[str]]:
    """`{corpus name: [case id]}` for every SCORING corpus.

    A case id is the census's: a repo-relative path for a document on disk, and
    `oracle-accept:`/`oracle-reject:`/`admission:` plus the program's name for
    the three program lists, which are oracle inputs with no file of their own.
    Using the census's ids is what lets the census report a fraction per bucket
    without a second lookup table.

    The `census` corpus comes from the census's OWN `load_corpus`, not from a
    restated walk. An earlier version read the two oracle lists with `ast` to
    avoid importing a pytest module, and silently missed the 16 programs that
    list appends in a loop rather than spelling as literal tuples -- 16 oracle
    inputs measured by nothing, which is the exact failure shape this file
    exists to close. Reusing the function costs a few seconds and cannot drift.
    """
    census = _census()
    skip = set(census._SKIP_DIRS)

    try:
        import test_selfhost_lower as oracle_module
    except ImportError as exc:  # pragma: no cover - environment, not logic
        raise SystemExit(
            f"corpus_provenance: cannot import the oracle program lists from "
            f"tests/test_selfhost_lower.py ({exc}). It needs pytest on the "
            f"path; run this from the repo with the dev environment.")

    census_cases = [cid for cid, _ in census.load_corpus(oracle_module)]
    prefixes = ("oracle-accept:", "oracle-reject:", "admission:")
    oracle = [cid for cid in census_cases if cid.startswith(prefixes)]

    corpora: dict[str, list[str]] = {
        # The evidence behind "the gate agrees with the reference": every input
        # `tools/gate_reference_census.py --check` runs, file-backed or not.
        "census": census_cases,
        # The boundary programs: the inputs written to sit ON a guarantee
        # edge, and so the densest evidence in the tree. "Written to" is about
        # their purpose, not their author; see GENERATION IS NOT AUTHORSHIP.
        "selfhost_oracle": oracle,
    }
    # Each globbed byte-agreement projection, per tier. A floor here is tighter
    # than the aggregate one and is where a tier's corpus would drift first.
    for tier in ("py", "ts", "go", "rust", "java", "wasm"):
        sub = f"tests/fixtures/emit_{tier}_corpus"
        if (ROOT / sub).is_dir():
            corpora[f"emit_{tier}_corpus"] = sorted(_documents((sub,), skip))
    return corpora


# ------------------------------------------------------------- the manifest

class Provenance:
    """The manifest, resolved. `generation(case_id)` is the whole contract."""

    def __init__(self, data: dict):
        self.current_generation = int(data.get("current_generation", 0))
        self.floors = {k: int(v) for k, v in (data.get("floors") or {}).items()}
        # The orthogonal axis (issue #1397): who TYPED the document, as opposed
        # to which loop generation produced it. Absent means loop-authored,
        # the same fail-closed direction as an absent generation.
        self.human_authored: set[str] = set(data.get("human_authored") or ())
        self._by_case: dict[str, int] = {}
        for key, names in (data.get("generations") or {}).items():
            gen = int(key)
            for name in names:
                self._by_case[name] = gen

    @staticmethod
    def load(path: Path = MANIFEST) -> "Provenance":
        return Provenance(json.loads(path.read_text(encoding="utf-8")))

    def generation(self, case_id: str):
        """The authoring generation, or `UNDECLARED`. Never a default of 0."""
        return self._by_case.get(case_id, UNDECLARED)

    def is_model_authored(self, case_id: str, since: int = FLOOR_SINCE) -> bool:
        """True when this case is the engine's own work at or after `since`.

        UNDECLARED is True at every threshold. That is the fail-closed
        direction and the reason the number means anything.
        """
        gen = self.generation(case_id)
        return gen == UNDECLARED or gen >= since

    def declared(self) -> set[str]:
        return set(self._by_case)

    def split(self, case_ids, since: int = FLOOR_SINCE):
        """`(model, independent, undeclared)` as sorted lists."""
        model, independent, unknown = [], [], []
        for case_id in case_ids:
            gen = self.generation(case_id)
            if gen == UNDECLARED:
                unknown.append(case_id)
                model.append(case_id)
            elif gen >= since:
                model.append(case_id)
            else:
                independent.append(case_id)
        return sorted(model), sorted(independent), sorted(unknown)


    def is_human_authored(self, case_id: str) -> bool:
        """True only when a person is NAMED as having typed this document.

        There is no inference here and there cannot be. Issue #1397 measured
        the four signals git offers -- the introducing commit's author, its
        branch, its co-author trailers, and the date -- and none of them
        separates the two populations: the author is one human identity by
        policy, 415 of the 850 generation-zero entries landed with no branch
        to read, the trailer stops appearing after 2026-09-13, and the root
        commit itself carries one.
        """
        return case_id in self.human_authored

    def human_split(self, case_ids):
        """`(model, human)` as sorted lists, on the authorship axis."""
        model, human = [], []
        for case_id in case_ids:
            (human if self.is_human_authored(case_id) else model).append(case_id)
        return sorted(model), sorted(human)


def independent_permille(model: int, total: int) -> int:
    """Independent share in tenths of a percent, floored, integer-only.

    No float anywhere on the gate path: a threshold compared through a float is
    a threshold whose edge case depends on the platform's rounding.
    """
    if total == 0:
        return 0
    return ((total - model) * 1000) // total


def crosses_floor(model: int, total: int, floor_percent: int) -> bool:
    """True when this corpus has crossed its declared independence floor.

    An EMPTY corpus crosses every floor above zero. A scoring corpus that
    vanished is not a corpus that passed -- that is the same vacuity the
    census's own docstring calls out, one tier up.
    """
    if total == 0:
        return floor_percent > 0
    return (total - model) * 100 < floor_percent * total


# --------------------------------------------------------------- the report

def report(corpora: dict[str, list[str]], prov: Provenance,
           *, since: int = FLOOR_SINCE) -> str:
    lines = [f"corpus provenance: loop-authored at or after generation {since}",
             "",
             f"{'corpus':22s} {'model':>6s} {'total':>6s} {'indep':>8s} "
             f"{'floor':>6s}  verdict"]
    for name in sorted(corpora):
        ids = corpora[name]
        model, _, unknown = prov.split(ids, since)
        floor = prov.floors.get(name)
        share = independent_permille(len(model), len(ids))
        if floor is None:
            verdict = "no floor declared"
        elif crosses_floor(len(model), len(ids), floor):
            verdict = "BELOW FLOOR"
        else:
            verdict = "ok"
        if unknown:
            verdict += f" ({len(unknown)} undeclared)"
        lines.append(
            f"{name:22s} {len(model):6d} {len(ids):6d} {share / 10:7.1f}% "
            f"{'--' if floor is None else str(floor) + '%':>6s}  {verdict}")
    lines.append("")
    lines.append(authorship_report(corpora, prov))
    return "\n".join(lines)


def authorship_report(corpora: dict[str, list[str]], prov: Provenance) -> str:
    """The second axis: how much of the evidence a person typed.

    Separate from the table above on purpose. That one answers "how much of
    this did the loop write", which is what the floors gate. This one answers
    "how much of this did a human write", which is the question a reader of
    the published census will think the first table answered. They are
    different numbers and issue #1397 exists because they were conflated.
    """
    lines = ["authorship: documents a person is declared to have typed",
             "",
             f"{'corpus':22s} {'human':>6s} {'total':>6s} {'human':>8s}"]
    for name in sorted(corpora):
        ids = corpora[name]
        _, human = prov.human_split(ids)
        share = independent_permille(len(ids) - len(human), len(ids))
        lines.append(f"{name:22s} {len(human):6d} {len(ids):6d} "
                     f"{share / 10:7.1f}%")
    every = sorted({cid for ids in corpora.values() for cid in ids})
    _, human = prov.human_split(every)
    share = independent_permille(len(every) - len(human), len(every))
    lines.append(f"{'ALL':22s} {len(human):6d} {len(every):6d} "
                 f"{share / 10:7.1f}%")
    lines.append("")
    lines.append("This axis carries no floor. It is reported so the "
                 "loop-authorship figure above")
    lines.append("cannot be read as a claim about human authorship; see "
                 "issue #1397.")
    return "\n".join(lines)


def bucket_report(buckets: dict[str, list[str]], prov: Provenance,
                  *, since: int = FLOOR_SINCE) -> str:
    """The per-bucket table `tools/gate_reference_census.py` prints.

    The census's buckets are the shape of the agreement itself, so this is the
    answer to the question the item asks: of the programs behind
    `agree-refuse/G4`, how many a generation of the loop wrote about itself.

    The header says LOOP-authored, not model-authored. It said the second until
    issue #1397, and that was the wrong word for what `generations` holds: a
    document at generation 0 is pre-loop, which is not a claim that a person
    typed it. The authorship question has its own axis, `human_authored`, and
    `report` prints it as a separate table. A caller printing this table beside
    a census total should not be able to read it as an authorship figure.
    """
    total_ids = [cid for ids in buckets.values() for cid in ids]
    model_all, _, unknown_all = prov.split(total_ids, since)
    lines = [f"provenance: loop-authored at or after generation {since}", ""]
    for name in sorted(buckets, key=lambda k: (-len(buckets[k]), k)):
        ids = buckets[name]
        model, _, _ = prov.split(ids, since)
        share = independent_permille(len(model), len(ids))
        lines.append(f"   {len(model):6d} / {len(ids):<6d} {share / 10:6.1f}% "
                     f"independent  {name}")
    share = independent_permille(len(model_all), len(total_ids))
    lines.append("")
    lines.append(f"   {len(model_all):6d} / {len(total_ids):<6d} {share / 10:6.1f}% "
                 f"independent  ALL, {len(unknown_all)} undeclared")
    return "\n".join(lines)


# ----------------------------------------------------------------- the gate

def check(corpora: dict[str, list[str]], prov: Provenance,
          *, since: int = FLOOR_SINCE) -> list[str]:
    """Every problem, in three directions.

    1. UNDECLARED: a document in a scoring corpus with no generation. The fix
       is one manifest line, never a default.
    2. STALE: a manifest name that is in no scoring corpus any more. Same
       shape as the census baseline's "no longer diverges" arm: an allowance
       that outlives what it described is an allowance nobody rereads.
    3. BELOW FLOOR: a scoring corpus whose independent share has crossed the
       floor declared for it.
    """
    problems: list[str] = []
    live: set[str] = set()
    for name in sorted(corpora):
        ids = corpora[name]
        live |= set(ids)
        model, _, unknown = prov.split(ids, since)
        for case_id in unknown:
            problems.append(
                f"UNDECLARED provenance in {name}: {case_id} -- add it to a "
                f"generation in tests/fixtures/corpus_provenance.json. An "
                f"unknown provenance counts as loop-authored; it is never "
                f"read as pre-loop.")
        floor = prov.floors.get(name)
        if floor is None:
            problems.append(
                f"NO FLOOR declared for scoring corpus {name} -- add one to "
                f"`floors` in tests/fixtures/corpus_provenance.json.")
            continue
        if crosses_floor(len(model), len(ids), floor):
            share = independent_permille(len(model), len(ids))
            problems.append(
                f"BELOW FLOOR {name}: {share / 10:.1f}% independent of "
                f"generation {since} and up, floor is {floor}% "
                f"({len(model)} of {len(ids)} programs are loop-authored).")
    for name in sorted(prov.declared() - live):
        problems.append(
            f"STALE provenance entry: {name} is in no scoring corpus -- "
            f"delete the line.")
    for name in sorted(prov.human_authored - live):
        problems.append(
            f"STALE human_authored entry: {name} is in no scoring corpus -- "
            f"delete the line.")
    return problems


def write(corpora: dict[str, list[str]], prov: Provenance, generation: int,
          path: Path = MANIFEST) -> int:
    """Declare every currently-undeclared case at `generation`. Returns the
    count. Deliberately requires the generation to be named: there is no
    `--write` that guesses, because the guess a caller wants is 0 and that is
    the fail-open one."""
    live = {cid for ids in corpora.values() for cid in ids}
    generations: dict[str, set[str]] = {}
    for case_id in sorted(live):
        gen = prov.generation(case_id)
        generations.setdefault(
            str(generation if gen == UNDECLARED else gen), set()).add(case_id)
    added = len(live - prov.declared())
    path.write_text(json.dumps(
        {"note": MANIFEST_NOTE,
         "current_generation": max(prov.current_generation, generation),
         "floors": dict(sorted(prov.floors.items())),
         "human_authored": sorted(prov.human_authored),
         "generations": {k: sorted(v) for k, v in sorted(
             generations.items(), key=lambda kv: int(kv[0]))}},
        indent=1, sort_keys=False) + "\n", encoding="utf-8")
    return added


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="fail on an undeclared document, a stale entry, or a "
                         "corpus below its declared independence floor")
    ap.add_argument("--write", action="store_true",
                    help="declare every undeclared case at --generation")
    ap.add_argument("--generation", type=int,
                    help="the generation --write assigns; required with --write")
    ap.add_argument("--since", type=int, default=FLOOR_SINCE,
                    help="count a document as loop-authored at or after this "
                         "generation (reporting only; the gate uses "
                         f"{FLOOR_SINCE})")
    args = ap.parse_args(argv)

    if args.since < 1:
        ap.error("--since must be at least 1: generation 0 is the PRE-LOOP "
                 "stock, and counting it as loop-authored makes the fraction "
                 "meaningless")
    if args.write and args.generation is None:
        ap.error("--write requires --generation N. There is no default: the "
                 "default a caller wants is 0, and silently declaring an "
                 "unknown document hand-written is the failure this tool exists "
                 "to prevent")

    prov = Provenance.load()
    corpora = enumerate_corpora()

    if args.write:
        added = write(corpora, prov, args.generation)
        print(f"declared {added} case(s) at generation {args.generation} in "
              f"{MANIFEST.relative_to(ROOT)}")
        return 0

    print(report(corpora, prov, since=args.since))

    if args.check:
        problems = check(corpora, prov, since=FLOOR_SINCE)
        if problems:
            print("\ncorpus provenance FAILED:")
            for line in problems:
                print(f"  {line}")
            return 1
        print("\ncorpus provenance: every scoring document is declared and "
              "every corpus is above its floor.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
