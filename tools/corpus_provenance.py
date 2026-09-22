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

THE FAILURE DIRECTION: UNDECLARED IS NOT HAND-WRITTEN
-----------------------------------------------------
A provenance field that defaults to "hand-written" when unknown is the
fail-open shape, and it makes the whole measurement a lie: dropping an
undeclared document into a globbed corpus would then RAISE the measured
independence. So an undeclared document resolves to `UNDECLARED`, which is
counted as model-authored at every threshold, and `--check` names it as an
error in its own right. Adding a corpus document therefore costs one line in a
manifest, and forgetting it makes the tree LOOK WORSE rather than better. That
is the only arrangement under which the number can be trusted.

Note that the resolution is not "the current generation". `current_generation`
is 0 today, so resolving an unknown to it would be indistinguishable from
resolving it to hand-written, and the fail-closed property would silently not
hold until the loop had run once. `UNDECLARED` is model-authored regardless of
what generation the tree is on.

GENERATION ZERO IS A DECLARATION, NOT A MEASUREMENT
---------------------------------------------------
The generation-zero set records the tree as it stood before this file existed.
No mechanism proves those documents were hand-written; the claim is asserted by
the operator who recorded them, once, in a diff. What the mechanism holds from
here on is different and checkable: every document that ARRIVES must name its
generation, the set can only change in a diff somebody reads, and the fraction
is computed rather than claimed.

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
# it must compare as model-authored at EVERY threshold, including thresholds
# above any generation the tree has reached.
UNDECLARED = "undeclared"

# The threshold the FLOOR is evaluated at. Generation 1 is the first output of
# the loop, so "independent at generation 1" means "predates the loop
# entirely". Re-basing this is an operator decision and belongs in the manifest
# beside the floor values, not here.
FLOOR_SINCE = 1

MANIFEST_NOTE = (
    "Which generation authored each scoring document (roadmap item 542, issue "
    "#1221). Generation 0 is hand-written / pre-loop. A name absent from every "
    "list is UNDECLARED, which counts as model-authored at every threshold and "
    "is an error under `python3 tools/corpus_provenance.py --check`: an "
    "unknown provenance must never read as hand-written, or a document dropped "
    "into a globbed corpus would raise the measured independence. Names only, "
    "no counts and no fractions, for the same reason "
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
        # The hand-written boundary programs, which are the inputs written to
        # sit ON a guarantee edge and so the densest evidence in the tree.
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
    lines = [f"corpus provenance: model-authored at or after generation {since}",
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
    return "\n".join(lines)


def bucket_report(buckets: dict[str, list[str]], prov: Provenance,
                  *, since: int = FLOOR_SINCE) -> str:
    """The per-bucket table `tools/gate_reference_census.py` prints.

    The census's buckets are the shape of the agreement itself, so this is the
    answer to the question the item asks: of the programs behind
    `agree-refuse/G4`, how many did the engine write about itself.
    """
    total_ids = [cid for ids in buckets.values() for cid in ids]
    model_all, _, unknown_all = prov.split(total_ids, since)
    lines = [f"provenance: model-authored at or after generation {since}", ""]
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
                f"unknown provenance counts as model-authored; it is never "
                f"read as hand-written.")
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
                f"({len(model)} of {len(ids)} programs are model-authored).")
    for name in sorted(prov.declared() - live):
        problems.append(
            f"STALE provenance entry: {name} is in no scoring corpus -- "
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
                    help="count a document as model-authored at or after this "
                         "generation (reporting only; the gate uses "
                         f"{FLOOR_SINCE})")
    args = ap.parse_args(argv)

    if args.since < 1:
        ap.error("--since must be at least 1: generation 0 is the hand-written "
                 "stock, and counting it as model-authored makes the fraction "
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
