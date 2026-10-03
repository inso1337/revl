#!/usr/bin/env python3
"""Generate the publishable gate/reference census artifact (roadmap item 560).

WHAT THIS IS FOR
----------------
`tools/gate_reference_census.py` runs two independently written implementations
of revl's semantics over the same corpus and classifies every disagreement:
`src/revl/*.py` (the reference) against `selfhost/*.rvl` behind the native
gate's own guards. Agreement is on TAG AND MESSAGE, not merely on verdict.

The publishable property is not the corpus size. It is that the bucket that
matters CANNOT BE WRITTEN. `false-admission` sits in the census's
`NEVER_BASELINED` tuple, so `--record` drops it on the way into the baseline and
`--check` fails on any member however the baseline reads. The allowance in the
direction that matters cannot be raised by re-recording, which is the one move
that cooks every other benchmark.

That claim is worth stating outside this repository. This tool is what lets it
be stated without a person retyping a number.

WHY IT IS A GENERATOR AND NOT A DOCUMENT
----------------------------------------
A published table with a hand-transcribed number is a mirror that rots against
what it mirrors, and this repository has been bitten by that shape repeatedly.
So every count, every named residual and every fraction in the report comes
from a run. Nothing in it is typed by hand.

WHAT IS COMMITTED, AND WHAT IS RENDERED (issue #1768)
------------------------------------------------------
The repository commits the RECORDS of a run, not the report. They live in
`docs/census-artifact/`:

  * `cases.jsonl` - one `[case id, bucket]` row per program run, sorted by
    case id;
  * `pins.jsonl`  - one `[group, file]` row per file a verdict or the report
    depends on, sorted;
  * `facts.json`  - the handful of measured facts that are neither a row nor a
    pin: the engine, the admissions the gate issued, the reference faults and
    the driven `NEVER_BASELINED` probe.

One record per line, a blank line between records, and nothing derived. The
sha256 of each program and of each pinned file, the counts, the bucket table,
`n`, the run id, the compiler digest, the checker version, the claims and the
markdown are all functions of the records and the checkout, so they are
computed when the report is rendered and never stored. A digest is a property
of the tree; storing one meant every pull request that edited a pinned module
rewrote its line even when no verdict moved.
A stored aggregate is a line every pull request that moves the corpus rewrites,
so two independent pull requests used to conflict on it after every landing.
Records merge under git's ordinary line merge unless two pull requests move the
same program's verdict, which is a real conflict.

The report itself, `EVAL-REPORT-1` JSON or markdown, is rendered on demand:

    python3 tools/census_artifact.py                    # markdown, fresh run
    python3 tools/census_artifact.py --json             # JSON, fresh run
    python3 tools/census_artifact.py --from-records     # markdown, no run

`docs/census-artifact.md` is a hand-written page about the artifact and holds
no number.

WHAT IT REFUSES TO DO
---------------------
It does not publish. It writes two files into this repository and stops. Sending
them anywhere is a separate, human decision.

It does not assert the `NEVER_BASELINED` property in prose. It DRIVES both
halves of the mechanism with a synthetic `false-admission` member and records
what happened:

  * the record half  - `record_payload` is handed a synthetic member and the
    payload it returns is searched for it;
  * the check  half  - `compare` is handed that member AND a baseline that
    lists it, and the problems it returns are searched for the refusal.

A run in which either probe came out the other way writes a report that says so.

NO WALL CLOCK, NO COMMIT SHA
----------------------------
Nothing in the artifact is a timestamp or a git commit, so `--check` measures
staleness of the NUMBERS rather than of the calendar. Identity is by content
digest instead: `compiler_commit` names a sha256 over the pinned
`src/revl/**/*.py` modules (file name and sha256 of each) and `run` names a
sha256 over the `(case id, sha256 of the source)` rows of the corpus the run
actually read. Both are stronger than a commit sha, because a commit that moved
neither does not move them, and both are recomputable from the records alone.

Every file name that goes into a digest is REPO-RELATIVE. An absolute name makes
the digest depend on the directory the clone happens to sit in, which is the one
way a content digest can stop being recomputable by the person it was published
for; `tests/test_census_artifact.py` holds that property from a second checkout.

USAGE
-----
    python3 tools/census_artifact.py                     # render to stdout
    python3 tools/census_artifact.py --json              # the report as JSON
    python3 tools/census_artifact.py --from-records      # render the records
    python3 tools/census_artifact.py --write             # write the records
    python3 tools/census_artifact.py --check             # fail if they drifted
    python3 tools/census_artifact.py --verify [REPORT]   # judge a published copy
    python3 tools/census_artifact.py --verify --strict   # CI: is the committed copy current
    git diff --name-only BASE | python3 tools/census_artifact.py --moved-inputs
    python3 tools/census_artifact.py --crate-json C.json --record-reproduction

THE REPRODUCTION, AND WHY IT IS RECORDED RATHER THAN RE-RUN
-----------------------------------------------------------
`--engine crate` asks the REAL `crates/revl-gate`, built by cargo, the same
questions the fast engine answers. It takes about fourteen minutes and needs a
rust toolchain, so `--check` cannot run it and neither can a CI job that is
allowed to be cheap. The result is recorded instead, in
`tests/fixtures/census_crate_reproduction/`, beside the CHECKER VERSION it was
taken at: `reproduction.json` holds the version, the tracked buckets and the
false admissions, and `programs.jsonl` the programs the run covered, one per
line, sorted. The count is derived from that list rather than stored, because a
stored count was the one line every corpus-moving pull request rewrote, so two
of them always conflicted on it (issue #1768). A program the census now runs
and the recorded reproduction did not is counted and reported.

A recorded result rots, so it is not trusted blind: every run compares the
recorded checker version against the current one, and a reproduction taken at a
different version is reported as stale and DOES NOT lift any claim. The rung is
computed from the evidence that is actually current, never declared.

Refresh it with:

    python3 tools/gate_reference_census.py --engine crate --check \\
        --no-provenance --json C.json
    python3 tools/census_artifact.py --crate-json C.json --record-reproduction
    python3 tools/census_artifact.py --write

Exit status is 1 when `--check` finds drift, 2 on unusable input.

VERIFYING A PUBLISHED COPY
--------------------------
`--verify` is the reader's command, and `docs/design/560-census-artifact.md`
carries the argument. It compares the published `census.pins` against this
checkout (every file a census run opens is pinned, measured by an audit hook),
then re-runs the census and compares `census.cases` row by row. Exit 0
reproduced, 1 refuted, 2 unusable, 3 not a full check (inputs differ, or
published programs were edited or removed).

THE REPOSITORY KEEPS ITS OWN COPY CURRENT
-----------------------------------------
CI's `census-artifact` job runs `--verify --strict` on the committed copy
whenever a pull request's diff moves one of its inputs, as `--moved-inputs`
decides from the diff: a pinned file, a program, or a new `.rvl` under a census
directory. `--strict` fails unless every published program reproduced from
byte-identical inputs, no program in the corpus is missing from the file, and
no report input moved. A pull request that moves an input regenerates the
artifact in the same diff:

    python3 tools/census_artifact.py --write

`--check` is then green too, because the records' bytes are a function of
exactly those inputs (issue #1572). `--strict` compares the record files byte
for byte against a fresh run as well, so a record that is out of order, edited
by hand or left behind by a conflict resolution fails it.

RESOLVING A MERGE CONFLICT IN THE RECORDS
-----------------------------------------
Two pull requests conflict in `docs/census-artifact/` only when both moved the
verdict of the same program, added programs at the same place, or changed which
files the run reads. Resolve by regenerating, never by hand:

    python3 tools/census_artifact.py --write
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The committed records of a run (issue #1768). The report is rendered from
# them and is not committed; see "WHAT IS COMMITTED, AND WHAT IS RENDERED".
RECORDS = ROOT / "docs" / "census-artifact"
CASES_RECORDS = "cases.jsonl"
PINS_RECORDS = "pins.jsonl"
FACTS_RECORDS = "facts.json"
RECORD_FILES = (CASES_RECORDS, PINS_RECORDS, FACTS_RECORDS)
RECORDS_SCHEMA = "GATE-CENSUS-RECORDS-1"

# The pin groups `pins.jsonl` carries, in file order: `build_pins`'s groups.
VERDICT_PIN_GROUPS = ("decides_verdicts", "reference", "report_inputs")

# The recorded `--engine crate` reproduction. In tests/fixtures/ rather than in
# docs/ because it is an input to the artifact, not part of it. A directory
# since issue #1768: the facts in one file, the covered programs one per line.
CRATE_REPRODUCTION = ROOT / "tests" / "fixtures" / "census_crate_reproduction"
REPRODUCTION_FACTS = "reproduction.json"
REPRODUCTION_PROGRAMS = "programs.jsonl"

# The frozen schemas this artifact is written against. `EVAL-REPORT-1` is
# `docs/design/478-eval-honesty-protocol.md`'s, validated by
# `tools/check_eval_report.py`; `GATE-CENSUS-1` is this artifact's own section
# inside it, which that checker neither reads nor constrains.
REPORT_SCHEMA = "EVAL-REPORT-1"
EVAL_PROTOCOL = "EVAL-1"
CENSUS_SCHEMA = "GATE-CENSUS-1"

# The files whose bytes decide what the census DOES. The checker version is a
# digest over exactly these, so it moves when the measurement moves and stays
# put when an unrelated commit lands. `selfhost/lower.rvl` is in the list
# because it is the implementation under test; the corpus is NOT, because the
# corpus is identified separately by `run`.
CHECKER_SOURCES = (
    "tools/gate_reference_census.py",
    "tools/corpus_provenance.py",
    "tests/test_gate_reference_census.py",
    "tools/build_gate_crate.py",
    "selfhost/lower.rvl",
)

# The reference implementation. Its pins and its digest (named
# `compiler_commit` in the report, because `EVAL-REPORT-1` requires that key)
# cover the modules under this prefix that the census run OPENED, measured by
# the same audit hook as `decides_verdicts`. They used to cover the whole glob,
# which made the artifact stale on every change to a module the census never
# imports: of the 19 `src/revl` files that changed between PR 1469 and
# `67fc027b7`, one was a module the run opens (issue #1572). A module the run
# does not open cannot decide a verdict it computes, so it is not an input.
REFERENCE_GLOB = "src/revl/**/*.py"

# The synthetic case id the NEVER_BASELINED probes use. It is not a real path
# and never reaches a baseline on disk: both probes run against values, and the
# committed baseline is only ever read.
PROBE_CASE = "census-artifact-probe:synthetic-false-admission"


# --------------------------------------------------------------- loading

def _load(rel: str, name: str):
    """A tool in this repository as a module, by path."""
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _plural(count: int, one: str, many: str) -> str:
    """`f"{count} {one|many}"`. The artifact is read by people, and a generated
    report that says "6 id(s)" reads like a report nobody read."""
    return f"{count} {one if count == 1 else many}"


def _repo_relative(path) -> str:
    """`path` as a forward-slash name relative to the checkout root.

    Raises rather than falling back, because a silent fallback to an absolute
    name is exactly the defect this function exists to remove.
    """
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        raise SystemExit(
            f"census_artifact: {resolved} is outside the checkout at {ROOT}; "
            f"every digested file has to be in the tree, or the digest is not "
            f"recomputable by anyone else") from None


def _digest(paths) -> str:
    """sha256 over the named files, in the order given, each length-prefixed so
    concatenation cannot be forged by moving a byte across a boundary.

    The name that goes into the hash is the path RELATIVE TO THE CHECKOUT, and
    always with forward slashes. It used to be the absolute path, which made
    every digest a function of WHERE the clone sits: two byte-identical
    checkouts at two different directories produced two different checker
    versions, `--check` reported drift that was not there, and the recorded
    crate reproduction read as stale for no reason but the directory name. An
    artifact whose identity an outsider cannot recompute is not an artifact,
    so the name is repo-relative and the digest is a property of the contents.
    """
    h = hashlib.sha256()
    for path in paths:
        blob = Path(path).read_bytes()
        h.update(f"{_repo_relative(path)}:{len(blob)}\n".encode())
        h.update(blob)
    return h.hexdigest()


def checker_version() -> tuple[str, dict[str, str]]:
    """`(version, {rel: sha256})` over `CHECKER_SOURCES`."""
    per_file = {rel: hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
                for rel in CHECKER_SOURCES}
    whole = _digest([ROOT / rel for rel in CHECKER_SOURCES])
    return f"{CENSUS_SCHEMA}+{whole[:12]}", per_file


def reference_digest(reference: dict[str, str]) -> str:
    """sha256 over the reference modules the census run opened: each one's
    repo-relative name and sha256, in sorted order (`build_pins`'s `reference`
    group). Computed from the pins rather than from the files, so it is a
    function of the committed records and is never stored beside them."""
    h = hashlib.sha256()
    for rel in sorted(reference):
        h.update(f"{rel}:{reference[rel]}\n".encode())
    return h.hexdigest()


def corpus_digest(rows) -> str:
    """sha256 over the corpus this run actually read: every `[case id, sha256
    of the source, bucket]` row's id and source digest, sorted, a repeated id
    once per occurrence. Each id is length-prefixed, because ids contain `:`."""
    h = hashlib.sha256()
    for case_id, sha in sorted((row[0], row[1]) for row in rows):
        h.update(f"{len(case_id)}:{case_id}:{sha}\n".encode(
            "utf-8", "surrogatepass"))
    return h.hexdigest()


# ------------------------------------------------------------------ the pins
#
# `checker_version` digests a DECLARED list of files, and a declared list is
# only as complete as the person who wrote it. Measured on the tree this
# section landed on: a census run reads `backends/python/emit.py` (which turns
# `selfhost/lower.rvl` into the python the fast engine executes) and
# `tests/test_selfhost_lower.py` (whose `_classify` maps a reference error to
# the tag the census compares). Neither is in `CHECKER_SOURCES`, so either
# could change a verdict without moving any published identity.
#
# The pins below are therefore MEASURED, not declared. The generator records
# every file under the checkout that the census run opens, through an audit
# hook, and pins each one by sha256. The verifier measures the same thing on
# the reader's side, so a file that decides a verdict and is missing from the
# published pins is reported by name instead of being trusted by omission.

# Inputs to the REPORT that the census run itself does not read. They decide
# what the artifact says about the allowance, the provenance and the
# reproduction, not what any program's verdict is, so they are a separate
# group and a change to one of them never reads as a refutation.
REPORT_INPUTS = (
    "tools/gate_reference_census_baseline.json",
    "tests/fixtures/corpus_provenance.json",
    "tests/fixtures/census_crate_reproduction/reproduction.json",
    "tests/fixtures/census_crate_reproduction/programs.jsonl",
)

# The shipped gate, which the crate engine builds with cargo in a subprocess,
# so the audit hook never sees it. Pinned by glob; `build_gate_crate.py
# --check` is what ties it to `selfhost/lower.rvl`.
GATE_CRATE_GLOBS = ("crates/revl-gate/Cargo.toml",
                    "crates/revl-gate/src/**/*.rs")

# One stack of open sets, fed by one audit hook. An audit hook cannot be
# removed once added, so it is added at most once per process and does
# nothing while the stack is empty; `recording_reads` is the only writer.
_READS: list[set[str]] = []
_HOOK_ADDED = False


def _audit(event: str, args) -> None:
    if event != "open" or not _READS or not args:
        return
    target = args[0]
    if isinstance(target, (str, bytes, os.PathLike)):
        _READS[-1].add(os.fsdecode(target))


@contextlib.contextmanager
def recording_reads():
    """Collect every path opened inside the block. Nested blocks each see
    only their own opens; the hook is inert once the outermost one exits."""
    global _HOOK_ADDED
    if not _HOOK_ADDED:
        sys.addaudithook(_audit)
        _HOOK_ADDED = True
    seen: set[str] = set()
    _READS.append(seen)
    try:
        yield seen
    finally:
        _READS.pop()


def tree_file(path: str) -> str | None:
    """`path` as a repo-relative source name, or None when it is not a file
    in the checkout. A byte-compiled module is mapped back to its source,
    because the source is what a reader has and what the pin is about."""
    try:
        rel = Path(path).resolve().relative_to(ROOT)
    except (ValueError, OSError):
        return None
    parts = rel.parts
    if len(parts) >= 2 and parts[-2] == "__pycache__":
        rel = Path(*parts[:-2], parts[-1].split(".", 1)[0] + ".py")
    if not (ROOT / rel).is_file():
        return None
    return rel.as_posix()


def _sha(rel: str) -> str:
    return hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()


def _is_reference(rel: str) -> bool:
    return rel.startswith("src/revl/") and rel.endswith(".py")


def case_rows(measured: dict) -> list[list[str]]:
    """`[case id, sha256 of its source, bucket]` for every program, sorted by
    case id.

    The verifier compares verdicts case by case, so a reader whose corpus has
    grown can still check every published case whose bytes did not change. A
    repeated case id carries the same source both times, so it is assigned the
    buckets it landed in, in order, and the sort is stable so those rows keep
    run order between themselves. Sorted rather than in run order so that two
    pull requests adding different programs insert rows at different places
    in `cases.jsonl` (issue #1768).
    """
    pending: dict[str, list[str]] = {}
    for name, ids in measured["buckets"].items():
        for case_id in ids:
            pending.setdefault(case_id, []).append(name)
    rows = []
    for case_id, source in measured["cases"]:
        digest = hashlib.sha256(
            source.encode("utf-8", "surrogatepass")).hexdigest()
        rows.append([case_id, digest, pending[case_id].pop(0)])
    return sorted(rows, key=lambda row: row[0])


PINS_NOTE = (
    "Every file the published verdicts depend on, by sha256. "
    "`decides_verdicts` is MEASURED: it is the set of files under the "
    "checkout that the census run opened, recorded by an audit hook, "
    "minus the corpus (pinned per case in `cases`) and the reference "
    "(pinned here per file). A file that decides a verdict cannot be "
    "left out of this list by forgetting to name it, and "
    "`tools/census_artifact.py --verify` measures it again on the "
    "reader's side.")


def build_pins(measured: dict, reads: set[str]) -> dict:
    """Every input the published verdicts depend on, each by sha256.

    Four groups, because a change to each means something different:

      * `corpus`: carried per case in `census.cases`; the aggregate is `run`.
      * `reference`: the `src/revl/**/*.py` modules the run OPENED, measured
        like `decides_verdicts`, and the set `compiler_tree_digest` digests.
      * `decides_verdicts`: every other file the census run OPENED, measured.
      * `report_inputs`: `REPORT_INPUTS` and the shipped crate, which shape the
        report but are not read by the run that produces a verdict.
    """
    corpus_files = {case_id for case_id, _ in measured["cases"]}
    opened = {rel for rel in map(tree_file, reads) if rel is not None}
    deciding = sorted(rel for rel in opened
                      if rel not in corpus_files and not _is_reference(rel))
    reference = sorted(rel for rel in opened if _is_reference(rel))
    if not reference:
        # The reference was imported from outside this checkout (a wheel, or
        # another clone on sys.path), so nothing under `src/revl` was opened
        # and the pins would claim a run over no reference at all.
        raise SystemExit(
            "census_artifact: the census run opened no file under src/revl in "
            f"{ROOT}; revl was imported from somewhere else. Install this "
            "checkout editable (`pip install -e .`) and run again.")
    crate = sorted({p.relative_to(ROOT).as_posix()
                    for pattern in GATE_CRATE_GLOBS for p in ROOT.glob(pattern)
                    if p.is_file()})
    report_inputs = sorted(set(REPORT_INPUTS) | set(crate))
    return {
        "note": PINS_NOTE,
        "decides_verdicts": {rel: _sha(rel) for rel in deciding},
        "reference": {rel: _sha(rel) for rel in reference},
        "report_inputs": {rel: _sha(rel) for rel in report_inputs},
    }


# ----------------------------------------------- the never-baselined probes

def probe_never_baselined(census) -> dict:
    """Drive both halves of the zero-tolerance mechanism and report what they did.

    This is the load-bearing part of the artifact. The claim being published is
    that a `false-admission` cannot be granted tolerance, and a claim of that
    shape is worth what its demonstration is worth. So neither half is asserted:
    each is executed against a synthetic member, and the report carries the
    outcome, including an outcome that would contradict the claim.
    """
    bucket = census.ADMISSION
    buckets = {bucket: [PROBE_CASE]}
    details = {PROBE_CASE: {
        "bucket": bucket,
        "reference": {"tag": "G3", "message": "held providers disagree"},
        "gate": {"kind": "admitted", "code": "", "message": ""},
    }}

    # RECORD half: the payload `--record` would write, from a census whose only
    # finding is the synthetic false admission.
    payload = census.record_payload(buckets, details)
    recorded_buckets = payload.get("buckets", {})
    recorded_details = payload.get("details", {})
    record_dropped = (bucket not in recorded_buckets
                      and PROBE_CASE not in recorded_details)

    # CHECK half: a baseline that lists the member, which is the most generous
    # baseline that could exist. A hand-edited baseline gets no more than this.
    problems = census.compare(buckets, {"buckets": {bucket: [PROBE_CASE]}})
    check_refused = any(PROBE_CASE in p and "FALSE ADMISSION" in p
                        for p in problems)

    # The committed baseline, read only: no never-baselined key may be in it.
    committed = json.loads(census.BASELINE.read_text(encoding="utf-8"))
    stray = sorted(k for k in committed.get("buckets", {})
                   if k.split("/", 1)[0] in census.NEVER_BASELINED)

    return {
        "never_baselined": list(census.NEVER_BASELINED),
        "bucket": bucket,
        "probe_case": PROBE_CASE,
        "record_refuses_to_write_it": record_dropped,
        "check_fails_against_a_baseline_that_lists_it": check_refused,
        "check_message": next((p for p in problems if PROBE_CASE in p), ""),
        "committed_baseline_never_baselined_keys": stray,
        "holds": bool(record_dropped and check_refused and not stray),
    }


# ----------------------------------------------------------- the measurement

def measure(census, engine_name: str) -> dict:
    """One census pass: buckets, details, the corpus, and the issued admissions.

    The issued-admission count is measured rather than inferred, because an
    empty `false-admission` bucket over a corpus the gate admits NOTHING from is
    a vacuum that reads exactly like a pass. `ADMISSION_PROGRAMS` exists so the
    number is above zero; this records what it actually was.
    """
    reference, oracle = census._reference()
    cases = census.load_corpus(oracle)
    engine = census.ENGINES[engine_name]()
    buckets, details = census.run(cases, engine, reference)

    issued = [case_id for (case_id, _), verdict
              in zip(cases, engine.verdicts(src for _, src in cases))
              if verdict[0] == "admitted"]

    # The corpus is reached by rglob plus three inline program lists, and the
    # inline lists are keyed by a human-written name, so the same name can
    # appear twice. A program counted twice inflates every n the report states.
    # Both numbers are published: `n` is programs RUN, `n_distinct` is distinct
    # case ids, and the repeated ids are named so the gap is checkable rather
    # than asserted.
    seen: dict[str, int] = {}
    for case_id, _ in cases:
        seen[case_id] = seen.get(case_id, 0) + 1
    repeated = sorted(cid for cid, count in seen.items() if count > 1)

    return {"cases": cases, "buckets": buckets, "details": details,
            "issued_admissions": sorted(issued), "engine": engine_name,
            "n_distinct": len(seen), "repeated_case_ids": repeated,
            "reference_faults": reference_faults(details, buckets, cases)}


def reference_faults(details: dict, buckets: dict, cases) -> list[str]:
    """Programs on which the REFERENCE itself faulted.

    `census.run` catches a reference exception and turns it into an
    `OUT:reference fault ...` tag rather than crashing, which is right for a
    census and wrong for a report that wants to say the compiler decided every
    program. So they are counted here by name.
    """
    return sorted(cid for cid, d in details.items()
                  if str(d.get("reference", {}).get("tag", ""))
                  .startswith("OUT:reference fault"))


# ------------------------------------------------------------- the provenance

def provenance_rows(provenance) -> list[dict]:
    """`tools/corpus_provenance.py`'s table, as rows rather than as text."""
    prov = provenance.Provenance.load()
    corpora = provenance.enumerate_corpora()
    rows = []
    for name in sorted(corpora):
        ids = corpora[name]
        model, _, undeclared = prov.split(ids, since=provenance.FLOOR_SINCE)
        floor = prov.floors.get(name, 0)
        permille = provenance.independent_permille(len(model), len(ids))
        # The second axis (issue #1397). `loop_authored` is what the floor
        # gates; `human_authored` is the question a reader will think the
        # first column answered, and it is published beside it so the two
        # cannot be read as one.
        _, human = prov.human_split(ids)
        human_permille = provenance.independent_permille(
            len(ids) - len(human), len(ids))
        rows.append({
            "corpus": name,
            "loop_authored": len(model),
            "human_authored": len(human),
            "total": len(ids),
            "undeclared": len(undeclared),
            "independent_permille": permille,
            "independent_percent": f"{permille / 10:.1f}",
            "human_authored_percent": f"{human_permille / 10:.1f}",
            "floor_percent": floor,
            "crosses_floor": provenance.crosses_floor(
                len(model), len(ids), floor),
        })
    return rows


# ------------------------------------------------------------- the report

def _bucket_table(buckets: dict[str, list[str]]) -> list[dict]:
    return [{"bucket": name, "count": len(buckets[name])}
            for name in sorted(buckets, key=lambda k: (-len(buckets[k]), k))]


def allowance(census, buckets: dict[str, list[str]]) -> dict:
    """The standing `false-admit` allowance, with every residual named.

    Named, never counted. A count lets the list churn without a reader; the
    names have to be edited in a diff somebody reads, which is the whole
    discipline `tests/test_gate_reference_census.py::
    test_the_open_bypass_surface_is_exactly_the_named_list` enforces.

    TWO LISTS, NOT ONE. `families` is what THIS RUN measured. `baselined` is
    what `tools/gate_reference_census_baseline.json` records. They are
    published side by side because publishing only the first lets a NON-EMPTY
    baseline read as an empty allowance: a baselined member that stopped
    diverging leaves the measured list empty while the committed file still
    grants tolerance for it. `tools/gate_reference_census.py --check` fails in
    that direction, but a reader holding only this artifact cannot see it, and
    an artifact a reader has to take on trust is the thing this file exists not
    to be. So the divergence between the two lists is computed here, named, and
    rendered; `agrees_with_committed_baseline` is false whenever they differ in
    either direction.
    """
    families = {}
    for name, ids in sorted(buckets.items()):
        if name.split("/", 1)[0] == census.HARD:
            families[name] = sorted(ids)

    committed = json.loads(census.BASELINE.read_text(encoding="utf-8"))
    baselined = {name: sorted(ids)
                 for name, ids in sorted(committed.get("buckets", {}).items())
                 if name.split("/", 1)[0] == census.HARD}

    def _flat(table):
        return {(name, case) for name, ids in table.items() for case in ids}

    measured_set, baselined_set = _flat(families), _flat(baselined)
    return {
        "bucket_prefix": census.HARD,
        "families": families,
        "total": sum(len(v) for v in families.values()),
        # Repo-relative when it is the committed baseline, which it is in every
        # run that writes the artifact. A test points it elsewhere to drive the
        # disagreement arms, and a crash there would be a test harness detail
        # published as a tool defect.
        "baseline_file": (
            census.BASELINE.relative_to(ROOT).as_posix()
            if census.BASELINE.is_relative_to(ROOT) else census.BASELINE.name),
        "baselined": baselined,
        "baselined_total": sum(len(v) for v in baselined.values()),
        # Baselined and no longer diverging: the direction that would let a
        # non-empty baseline be published as an empty allowance.
        "baselined_but_not_measured": sorted(
            f"{name}: {case}" for name, case in baselined_set - measured_set),
        # Measured and not baselined: a NEW bypass.
        "measured_but_not_baselined": sorted(
            f"{name}: {case}" for name, case in measured_set - baselined_set),
        "agrees_with_committed_baseline": measured_set == baselined_set,
        "capped_by_name_in": "tests/test_gate_reference_census.py",
    }


def build_report(census, provenance, measured: dict,
                 crate: dict | None = None, *, probe: dict | None = None,
                 checker: tuple[str, dict[str, str]] | None = None) -> dict:
    """The artifact, in `EVAL-REPORT-1` shape with a `GATE-CENSUS-1` section.

    Everything it reads from `measured` is in the committed records
    (`measured_from_records` rebuilds it), so the report a fresh run builds
    and the report the records render are the same report. `probe` and
    `checker` are the recorded probe and checker digests when rendering from
    records; a fresh run drives the probe and reads the checkout."""
    buckets = measured["buckets"]
    rows = measured["case_rows"]
    version, per_file = checker if checker is not None else checker_version()
    run_id = f"census-{measured['engine']}-{corpus_digest(rows)[:12]}"
    compiler = (f"src/revl@sha256:"
                f"{reference_digest(measured['pins']['reference'])[:12]}")

    if probe is None:
        probe = probe_never_baselined(census)
    admissions = census.false_admissions(buckets)
    faults = list(measured["reference_faults"])
    admits = sorted(buckets.get("agree-admit", []))

    # The recorded reproduction: the REAL rust crate, on a different toolchain,
    # asked the same questions. Agreement is on the tracked buckets, which are
    # the only ones a divergence can hide in.
    reproduction = None
    if crate is not None:
        ours = {k: sorted(v) for k, v in buckets.items()
                if k.split("/", 1)[0] in census.TRACKED}
        theirs = {k: sorted(v) for k, v in crate.get("tracked_buckets", {}).items()}
        recorded_at = crate.get("checker_version", "")
        current = recorded_at == version
        reproduction = {
            "by": "tools/gate_reference_census.py --engine crate",
            "what": ("the committed crates/revl-gate, built by cargo and asked "
                     "through a standalone consumer, over the same corpus"),
            "recorded_in": str(CRATE_REPRODUCTION.relative_to(ROOT)),
            "recorded_at_checker_version": recorded_at,
            "current_checker_version": version,
            "is_current": current,
            "n": crate.get("n"),
            "census_programs_not_in_reproduction": (
                None if crate.get("programs") is None else len(
                    {row[0] for row in measured["case_rows"]}
                    - set(crate["programs"]))),
            "tracked_buckets_agree": theirs == ours,
            "crate_tracked_buckets": {k: len(v) for k, v in sorted(theirs.items())},
            "crate_false_admissions": sorted(crate.get("false_admissions", [])),
            "does_not_establish": (
                "The crate is BUILT from selfhost/lower.rvl by "
                "tools/build_gate_crate.py, so it is not a second "
                "specification: it is the same rvl source through a different "
                "emitter, toolchain and runtime. What this reproduction rules "
                "out is the fast engine's python mirror of the native guards "
                "being wrong. The independence that carries the census is the "
                "OTHER axis, src/revl against selfhost/, and it is in the "
                "measurement rather than in this reproduction."),
        }

    reproduced_ok = bool(reproduction and reproduction["is_current"]
                         and reproduction["tracked_buckets_agree"]
                         and not reproduction["crate_false_admissions"])

    evidence = {
        "compiler_commit": compiler,
        "run": run_id,
        "protocol": EVAL_PROTOCOL,
    }
    if reproduced_ok:
        evidence["reproduced_by"] = (
            "tools/gate_reference_census.py --engine crate (crates/revl-gate, "
            "built by cargo)")
    rung = "demonstrated" if reproduced_ok else "measured"

    n = sum(len(v) for v in buckets.values())
    n_distinct = measured["n_distinct"]
    repeated = measured["repeated_case_ids"]
    alw = allowance(census, buckets)
    prov_rows = provenance_rows(provenance)
    census_row = next((r for r in prov_rows if r["corpus"] == "census"), None)

    claims = [
        {
            "text": (
                f"Over {n_distinct} distinct programs ({n} runs; "
                f"{_plural(len(repeated), 'case id appears', 'case ids appear')}"
                f" more than once and are named "
                f"in the report), the gate issued no admission the reference "
                f"refuses: the `{census.ADMISSION}` bucket is empty, and it is "
                f"in NEVER_BASELINED, so `--record` cannot write one into the "
                f"baseline and `--check` fails on any member however the "
                f"baseline reads."),
            "rung": rung,
            "public": True,
            "evidence": dict(evidence),
        },
        {
            "text": (
                (f"The gate's standing false-admit allowance is empty: this "
                 f"run measured no `false-admit` member, and "
                 f"{alw['baseline_file']} records none."
                 if not alw["total"] and not alw["baselined_total"] else
                 f"The gate's standing false-admit allowance is "
                 f"{alw['total']} programs, every one of them named in this "
                 f"report ("
                 + "; ".join(f"{k} ({len(v)})"
                             for k, v in sorted(alw["families"].items()))
                 + f"), against {alw['baselined_total']} recorded in "
                   f"{alw['baseline_file']}.")
                + " The allowance is capped by name in "
                  "tests/test_gate_reference_census.py, and `--check` fails "
                  "in both directions, so it can only shrink in a diff "
                  "somebody reads."),
            "rung": rung,
            "public": True,
            "evidence": dict(evidence),
        },
        {
            "text": (
                f"{census_row['loop_authored']} of {census_row['total']} "
                f"census programs were produced by a generation of the "
                f"self-improvement loop at or after generation "
                f"{provenance.FLOOR_SINCE} "
                f"({census_row['independent_percent']}% independent of the "
                f"loop, {census_row['undeclared']} undeclared), measured by "
                f"tools/corpus_provenance.py over "
                f"tests/fixtures/corpus_provenance.json. The loop has never "
                f"run, which is why this figure is what it is."),
            "rung": "measured",
            "public": True,
            # The provenance number is produced by a different tool over a
            # different input, and the crate engine says nothing about it, so
            # it carries no reproduction and stands at `measured`.
            "evidence": {"compiler_commit": compiler, "run": run_id,
                         "protocol": EVAL_PROTOCOL},
        },
        {
            # Issue #1397. The claim above used to be worded as a claim about
            # MODEL authorship, which is a different and much stronger thing
            # than what the manifest holds. This one is published beside it so
            # a reader cannot take the first for the second.
            "text": (
                f"{census_row['human_authored']} of {census_row['total']} "
                f"census programs are declared in "
                f"tests/fixtures/corpus_provenance.json as typed by a person "
                f"({census_row['human_authored_percent']}%). This report makes "
                f"no claim that its corpus is human-written, and the figure "
                f"above is not that claim: it is the share no generation of "
                f"the self-improvement loop produced."),
            "rung": "measured",
            "public": True,
            "evidence": {"compiler_commit": compiler, "run": run_id,
                         "protocol": EVAL_PROTOCOL},
        },
    ]

    return {
        "protocol": EVAL_PROTOCOL,
        "report_schema": REPORT_SCHEMA,
        "generator": {
            # What is being GRADED: the self-host gate's verdicts.
            "name": "selfhost/lower.rvl admit_src behind crates/revl-gate",
            "tool": f"tools/gate_reference_census.py --engine {measured['engine']}",
            "run": run_id,
        },
        "grader": {
            # What DOES the grading: the reference compiler, a separately
            # written implementation that never sees the gate's answer.
            "kind": "compiler",
            "tool": "revl.compile_source",
            "name": "src/revl reference compiler",
        },
        "briefs": [
            {
                "spec": "census/agree-admit",
                "hard_gate": "compiles",
                "result": "pass" if not faults else "fail",
                "n": len(admits),
                "note": (
                    f"The {len(admits)} census programs the reference admits "
                    f"compile under the current checker with no error and no "
                    f"compiler crash, and the gate raised no objection to any "
                    f"of them. Reference faults over the whole corpus: "
                    f"{len(faults)}."),
                "reference_faults": faults,
            },
        ],
        "briefs_note": (
            "One brief. The frozen EVAL-1 gate set is defined for bench specs "
            "in bench/specs.json, and `compiles` is the only one of the three "
            "that means anything over a differential census corpus. The census "
            "results that have no frozen gate are in the census section below "
            "and in the claims, where they are rated on the ladder rather than "
            "dressed as a gate they are not."),
        "claims": claims,
        "census": {
            "schema": CENSUS_SCHEMA,
            "checker_version": version,
            "checker_sources": per_file,
            "compiler_tree_digest": compiler,
            "compiler_tree_digest_note": (
                "EVAL-REPORT-1 names this field `compiler_commit`. It is not a "
                "commit. It is a sha256 over the " + REFERENCE_GLOB + " modules "
                "the census run opened, listed in `pins.reference`, which is "
                "narrower than a commit (a commit that touched no module the "
                "run reads does not move it) and recomputable by anyone with a "
                "checkout."),
            "run": run_id,
            "engine": measured["engine"],
            "n": n,
            "n_distinct": n_distinct,
            "repeated_case_ids": repeated,
            "n_note": (
                f"`n` is programs RUN. `n_distinct` is distinct case ids: "
                f"{_plural(len(repeated), 'case id reaches', 'case ids reach')}"
                f" the corpus twice, once from each of two entries that spell "
                f"the same name, so every bucket count and `n` itself carry "
                f"them twice. The distinct number is the one to quote."),
            "corpus_dirs": list(census.CORPUS_DIRS),
            "buckets": _bucket_table(buckets),
            "tracked_bucket_prefixes": list(census.TRACKED),
            "false_admission": {
                "bucket": census.ADMISSION,
                "members": admissions,
                "count": len(admissions),
                "issued_admissions_over_the_corpus": len(
                    measured["issued_admissions"]),
                "non_vacuity_note": (
                    "An empty false-admission bucket proves nothing if the gate "
                    "admits nothing. The gate issued "
                    f"{len(measured['issued_admissions'])} admissions over this "
                    "corpus, every one of which the reference also admits."),
                "mechanism": probe,
            },
            "false_admit_allowance": alw,
            "provenance": {
                "tool": "tools/corpus_provenance.py",
                "manifest": "tests/fixtures/corpus_provenance.json",
                "since_generation": provenance.FLOOR_SINCE,
                "corpora": prov_rows,
            },
            "reproduction": reproduction,
            "pins": measured.get("pins"),
            "cases": measured.get("case_rows"),
            "cases_note": (
                "One row per program run, sorted by case id (a repeated id "
                "keeps run order): case id, sha256 of the source bytes, "
                "bucket. `tools/census_artifact.py --verify` "
                "re-runs the census and compares these rows one by one, so a "
                "published verdict is checkable on its own and not only as "
                "part of a bucket count."),
            "not_established": [
                "This report says nothing about any implementation other than "
                "revl's own two. It is not a comparison and carries no "
                "comparative claim.",
                "The gate covers one layer of a larger language. A "
                "`no-objection-out-of-slice` result is the reference refusing "
                "for a reason the gate does not decide, and is by design.",
                "Generation zero in the provenance manifest is a declaration "
                "about the tree as it stood, not a measurement. What holds from "
                "there on is that an arriving document must name its "
                "generation, and that an undeclared one counts as "
                "loop-authored.",
                "Generation zero does not mean a person typed it. Issue #1397 "
                "measured the generation-zero set against the commits that "
                "introduced it: 153 of 850 entries arrived on a branch named "
                "agent/*, 132 more on a commit carrying an AI co-author "
                "trailer, 415 on commits pushed to the trunk with no branch to "
                "read, and the repository's root commit carries such a trailer "
                "itself. Both signals are lower bounds. The honest reading is "
                "that this corpus is model-written and pre-loop, and the floors "
                "gate the second word, not the first.",
                "A mislabelled provenance entry defeats the provenance "
                "measurement exactly as re-recording the baseline would defeat "
                "the census. Neither is detected by a tool; both are edits in a "
                "diff somebody reads.",
                (f"The corpus holds "
                 f"{_plural(len(repeated), 'case id that appears', 'case ids that appear')}"
                 f" twice, so `n` ({n}) counts "
                 f"{_plural(n - n_distinct, 'program', 'programs')} twice and "
                 f"`n_distinct` ({n_distinct}) is the honest size. "
                 f"The repeats are named in the report. They are not "
                 f"deduplicated here: dropping one would move bucket counts "
                 f"and the recorded baseline, which is a change to the census "
                 f"rather than to the way it is reported."
                 if repeated else
                 "Every case id in this run's corpus is distinct, so `n` and "
                 "`n_distinct` are the same number."),
            ],
        },
    }


# ------------------------------------------------------------- the rendering

def _pct(row: dict) -> str:
    return f"{row['independent_percent']}%"


def render_markdown(report: dict) -> str:
    c = report["census"]
    fa = c["false_admission"]
    mech = fa["mechanism"]
    alw = c["false_admit_allowance"]
    out: list[str] = []
    w = out.append

    w("# The gate/reference census")
    w("")
    w("RENDERED REPORT. Every number below comes from a run of")
    w("`tools/census_artifact.py`, which is the only thing that writes it. The")
    w("repository commits the records of the run (`docs/census-artifact/`), not")
    w("this report: `python3 tools/census_artifact.py --from-records` renders it")
    w("from them, and `python3 tools/census_artifact.py` renders it from a fresh")
    w("run. `python3 tools/census_artifact.py --check` fails when the committed")
    w("records drifted; regenerate them with")
    w("`python3 tools/census_artifact.py --write`.")
    w("")
    w("## What is being measured")
    w("")
    w("revl has two independently written implementations of its own semantics.")
    w("`src/revl/*.py` is the reference compiler. `selfhost/*.rvl`, compiled into")
    w("`crates/revl-gate`, is the self-host gate. This census runs both over the")
    w("same corpus and classifies every disagreement. Agreement means the same")
    w("TAG and the same MESSAGE, not merely the same verdict.")
    w("")
    w(f"Distinct programs: **{c['n_distinct']}**. Programs run: **{c['n']}**.")
    w(f"Checker version: `{c['checker_version']}`.")
    w(f"Engine: `{c['engine']}`.")
    w(f"Run: `{c['run']}`.")
    w("")
    if c["repeated_case_ids"]:
        w("The two numbers differ because "
          + _plural(len(c["repeated_case_ids"]), "case id reaches",
                    "case ids reach"))
        w("the corpus twice, from two entries that spell the same")
        w("name. They are run twice and counted twice, in `n` and in every")
        w("bucket below. The number to quote is the distinct one. The repeats,")
        w("named so the gap is checkable rather than asserted:")
        w("")
        for case_id in c["repeated_case_ids"]:
            w(f"- `{case_id}`")
        w("")
    w("Neither identity is a commit or a clock. `run` is a sha256 over the")
    w("`(case id, sha256 of the source)` rows of the corpus this run read; the")
    w("checker version is a sha256 over five named files the")
    w("crate reproduction is keyed on. Neither is the complete list of what")
    w("decides a verdict: that list is measured, not named, and is pinned file by")
    w("file in `census.pins` (see \"Verifying a published copy\" below). Every")
    w("file name inside a digest is relative to the checkout root, so all of them")
    w("are recomputable from any clone, at any path.")
    w("")
    w("## The claim, and why it is not the corpus size")
    w("")
    w("The interesting property is not that the two agree over "
      f"{c['n_distinct']} programs.")
    w("It is that the bucket that matters **cannot be written**.")
    w("")
    w(f"`{fa['bucket']}` is an issued admission for a program the reference")
    w("refuses: the gate telling a host the reference said yes when it said no.")
    w(f"That bucket is in the census's `NEVER_BASELINED` tuple "
      f"(`{', '.join(mech['never_baselined'])}`),")
    w("and that has two consequences a re-record cannot undo:")
    w("")
    w("1. `--record` drops it on the way into the baseline, so no run can produce")
    w("   a baseline that grants one tolerance.")
    w("2. `--check` fails on any member regardless of what the baseline says,")
    w("   including a baseline hand-edited to list it.")
    w("")
    w("Every other benchmark is cooked by adjusting what counts as acceptable.")
    w("Here the adjustment is not available in the direction that matters. The")
    w("rest of this file is a table; that sentence is the artifact.")
    w("")
    w("### The mechanism, driven rather than asserted")
    w("")
    w("This tool does not take the paragraph above on trust. It hands both halves")
    w("of the mechanism a synthetic member and records what they did.")
    w("")
    w("| probe | what was driven | outcome |")
    w("|---|---|---|")
    w(f"| record | `record_payload` over a census whose only finding is "
      f"`{mech['probe_case']}` | "
      f"{'dropped, not written' if mech['record_refuses_to_write_it'] else 'WRITTEN (the mechanism did not hold)'} |")
    w(f"| check | `compare` against a baseline that lists that member | "
      f"{'refused' if mech['check_fails_against_a_baseline_that_lists_it'] else 'ACCEPTED (the mechanism did not hold)'} |")
    w(f"| baseline | committed baseline scanned for a never-baselined key | "
      f"{'none present' if not mech['committed_baseline_never_baselined_keys'] else ', '.join(mech['committed_baseline_never_baselined_keys'])} |")
    w("")
    if mech["check_message"]:
        w("The refusal, verbatim:")
        w("")
        w("```")
        w(mech["check_message"])
        w("```")
        w("")
    w(f"Mechanism holds this run: **{'yes' if mech['holds'] else 'NO'}**.")
    w("")
    w("### Observed, and why it is not a vacuum")
    w("")
    w(f"`{fa['bucket']}` members this run: **{fa['count']}**.")
    w("")
    w("An empty bucket proves nothing if the gate never admits anything, which")
    w("is what the state looked like before the admission arm opened. So the arm")
    w("is measured for non-vacuity too: the gate ISSUED "
      f"**{fa['issued_admissions_over_the_corpus']}**")
    w("admissions over this corpus, and the reference admits every one of them.")
    w("")
    w("## The standing false-admit allowance")
    w("")
    w("`false-admit` is the other direction: the reference refuses under a")
    w("guarantee the gate claims to decide, and the gate raises no objection. It")
    w("is a bypass, it is baselined, and it is published as it stands, with")
    w("every residual named.")
    w("")
    w("Two numbers, not one. The first is what this run MEASURED. The second")
    w(f"is what `{alw['baseline_file']}`")
    w("RECORDS. Publishing only the first would let a non-empty baseline read")
    w("as an empty allowance, because a baselined")
    w("member that stopped diverging leaves the measured list empty while the")
    w("committed file still grants it tolerance. So both are here, and so is")
    w("every name on which they differ.")
    w("")
    w(f"Measured this run: **{alw['total']}**. "
      f"Recorded in the baseline: **{alw['baselined_total']}**. "
      f"Capped by name in `{alw['capped_by_name_in']}`.")
    w("")
    if alw["total"] or alw["baselined_total"]:
        w("| bucket | program | measured this run | in the baseline |")
        w("|---|---|---|---|")
        rows = sorted({(family, case)
                       for table in (alw["families"], alw["baselined"])
                       for family, ids in table.items() for case in ids})
        for family, case_id in rows:
            here = case_id in alw["families"].get(family, [])
            there = case_id in alw["baselined"].get(family, [])
            w(f"| `{family}` | `{case_id}` | {'yes' if here else 'NO'} "
              f"| {'yes' if there else 'NO'} |")
    else:
        w("No member on either side: the run measured none and the committed")
        w("baseline records none. The allowance is empty, not merely unreported.")
    w("")
    if alw["agrees_with_committed_baseline"]:
        w("The two agree.")
    else:
        w("**The two do NOT agree, and this report is the wrong place to learn")
        w("that.** Every name below is a disagreement between what this run")
        w("measured and what the committed baseline records:")
        w("")
        for entry in alw["baselined_but_not_measured"]:
            w(f"- `{entry}` is baselined and no longer diverges. The baseline")
            w("  still grants it tolerance it does not need.")
        for entry in alw["measured_but_not_baselined"]:
            w(f"- `{entry}` diverges and is not baselined. It is a new bypass.")
        w("")
        w("`python3 tools/gate_reference_census.py --check` fails on each of")
        w("them.")
    w("")
    w("A count would let this list churn unread. Names have to be edited, and")
    w("`--check` fails in BOTH directions: on a new member, and on a baselined")
    w("member that no longer diverges. The second direction is the one that")
    w("matters for honesty, because it is what stops a fix from quietly widening")
    w("the allowance instead of closing it.")
    w("")
    w("## Corpus provenance")
    w("")
    w("A benchmark whose corpus was written by the thing it grades is worth")
    w("nothing. `tools/corpus_provenance.py` names the generation that authored")
    w("every scoring document, from `tests/fixtures/corpus_provenance.json`. A")
    w("document with no entry resolves to UNDECLARED and counts as")
    w("loop-authored at every threshold, because the other default would make")
    w("dropping an unknown file into a globbed corpus RAISE the measured")
    w("independence.")
    w("")
    w("Read the two columns below as two different questions, because they "
      "are:")
    w("")
    w("- **loop-authored** is: produced by a generation of the "
      "self-improvement")
    w(f"  loop at or after generation "
      f"{c['provenance']['since_generation']}. That loop has never run in this")
    w("  repository, which is the whole of why the column reads as it does,")
    w("  and it is the column the floors gate.")
    w("- **human-authored** is: declared in the manifest as typed by a person.")
    w("  Nothing here claims this corpus was written by people, and issue")
    w("  #1397 records the measurement that says it was not: of the 850")
    w("  generation-zero entries, 153 arrived on a branch named `agent/*` and")
    w("  132 more on a commit carrying an AI co-author trailer, both of which")
    w("  are lower bounds, and the repository's root commit carries one too.")
    w("")
    w("| corpus | loop-authored | human-authored | total | independent of the "
      "loop | floor | undeclared | verdict |")
    w("|---|---|---|---|---|---|---|---|")
    for row in c["provenance"]["corpora"]:
        verdict = "BELOW FLOOR" if row["crosses_floor"] else "ok"
        w(f"| `{row['corpus']}` | {row['loop_authored']} | "
          f"{row['human_authored']} | {row['total']} | "
          f"{_pct(row)} | {row['floor_percent']}% | {row['undeclared']} | "
          f"{verdict} |")
    w("")
    w("## Full bucket table")
    w("")
    w("| bucket | count |")
    w("|---|---|")
    for row in c["buckets"]:
        mark = " (tracked)" if row["bucket"].split("/", 1)[0] in \
            c["tracked_bucket_prefixes"] else ""
        w(f"| `{row['bucket']}`{mark} | {row['count']} |")
    w("")
    w("## Reproduction")
    w("")
    rep = c["reproduction"]
    if rep is None:
        w("This run carries no recorded reproduction, so every claim in")
        w("this report stands at `measured` and no higher.")
    else:
        w("The fast engine is a python mirror of the native gate's guards, so it")
        w("could in principle be wrong in the same direction as the thing it")
        w("mirrors. The reproduction asks the REAL crate, built by cargo, and is")
        w(f"recorded in `{rep['recorded_in']}` because it needs a rust toolchain")
        w("and minutes rather than seconds.")
        w("")
        w(f"- programs: **{rep['n']}**")
        if rep.get("census_programs_not_in_reproduction") is not None:
            w("- census programs this run read that the reproduction did not: "
              f"**{rep['census_programs_not_in_reproduction']}**")
        w("- tracked buckets agree: "
          f"**{'yes' if rep['tracked_buckets_agree'] else 'NO'}**")
        w(f"- crate `{fa['bucket']}` members: "
          f"**{len(rep['crate_false_admissions'])}**")
        w(f"- recorded at checker version: `{rep['recorded_at_checker_version']}`")
        w("- current for this run: "
          f"**{'yes' if rep['is_current'] else 'NO, stale: it lifts no claim'}**")
        w("")
        w(f"What it does not establish: {rep['does_not_establish']}")
    w("")
    w("## Running it yourself")
    w("")
    w("No account, no key, no hosted service. From a checkout:")
    w("")
    w("```")
    w("python3 -m venv .venv && .venv/bin/pip install -e .")
    w(".venv/bin/python tools/gate_reference_census.py --check")
    w(".venv/bin/python tools/corpus_provenance.py --check")
    w(".venv/bin/python tools/census_artifact.py --check")
    w("```")
    w("")
    w("Those three need nothing but python and take seconds. The crate engine,")
    w("`tools/gate_reference_census.py --engine crate --check`, additionally")
    w("needs cargo, builds the shipped gate, and is the slow half.")
    w("")
    w("### Checking the numbers in this file rather than trusting them")
    w("")
    w("`--check` re-runs the census and compares the committed records, which")
    w("every number here is computed from, against it, so a passing `--check`")
    w("in your own clone is the whole verification: the table is yours, not")
    w("ours. It exits 1 and names each record file that moved.")
    w("Nothing in the digests depends on where you cloned to, so the values")
    w("below are the values you should get.")
    w("")
    w("| number here | what recomputes it |")
    w("|---|---|")
    w(f"| distinct programs, {c['n_distinct']} | distinct case ids from "
      "`load_corpus` in `tools/gate_reference_census.py` |")
    w(f"| programs run, {c['n']} | the length of the same list, repeats "
      "included |")
    w(f"| run `{c['run']}` | sha256 over every `(case id, sha256 of the "
      "source)` row the run read, ids repo-relative |")
    w(f"| checker version `{c['checker_version']}` | sha256 over the "
      f"{len(c['checker_sources'])} files in `census.checker_sources`, each "
      "listed there with its own sha256 |")
    w(f"| `{c['compiler_tree_digest']}` | sha256 over the name and sha256 of "
      "each `" + REFERENCE_GLOB
      + "` module the run opened, listed in `census.pins.reference` |")
    w("| every bucket count | `tools/gate_reference_census.py --json out.json` "
      "|")
    w(f"| the false-admit allowance | `{alw['baseline_file']}`, which is in "
      "the tree |")
    w(f"| the provenance columns | `tools/corpus_provenance.py` over "
      f"`{c['provenance']['manifest']}` |")
    w("")
    w("The corpus is the repository. There is no download, no server and no")
    w("hosted copy to go stale against this one: the programs the census runs")
    w("are the `.rvl` files in the directories named above plus the inline")
    w("program lists in `tests/test_selfhost_lower.py` and")
    w("`tools/gate_reference_census.py`, and `--json` writes out the per-case")
    w("classification if you want to audit an individual verdict.")
    w("")
    render_verify_section(c, w)
    w("## What this does not establish")
    w("")
    for line in c["not_established"]:
        w(f"- {line}")
    w("")
    w("## Schema")
    w("")
    w("`python3 tools/census_artifact.py --json` prints the machine copy. It is")
    w("an `EVAL-REPORT-1` document under the frozen eval-honesty protocol")
    w("(`docs/design/478-eval-honesty-protocol.md`) and")
    w("`tools/check_eval_report.py` passes on it, which is a statement about")
    w("over-claiming and not about correctness:")
    w("every public claim in it names the rung its own evidence reaches. The")
    w(f"census results live in its `census` section under `{c['schema']}`, which")
    w("that checker does not read.")
    w("")
    return "\n".join(out)


# ------------------------------------------------------------- the records
#
# What the repository commits (issue #1768). One record per line, a blank line
# between records, sorted, and nothing that is a function of something else:
# not of other records, and not of the checkout. A file's sha256 is a property
# of the tree it sits in, so the records name WHICH files the verdicts depend
# on and WHICH bucket each program landed in, and the digests are computed
# when the report is rendered. A pull request that edits a pinned module or a
# program without moving a verdict therefore leaves the records alone, and two
# pull requests conflict here only when both moved the same program's verdict
# or added programs at the same place. The blank line is what keeps an edit to
# one record from touching its neighbour's hunk.

FACTS_NOTE = (
    "The measured facts of a census run that are neither a per-case verdict "
    "(cases.jsonl) nor a pinned file (pins.jsonl). Written by "
    "`python3 tools/census_artifact.py --write`; every count, digest and claim "
    "in the report is computed from these three files and the checkout when it "
    "is rendered, and none is stored. On a merge conflict, run that command "
    "again.")


def _record_text(records) -> str:
    """One JSON array per line, separated by a blank line."""
    return "\n\n".join(json.dumps(r) for r in records) + "\n"


def record_texts(measured: dict, mechanism: dict) -> dict[str, str]:
    """`{file name: bytes}` for `docs/census-artifact/`, from one measurement."""
    pins = [[group, rel] for group in VERDICT_PIN_GROUPS
            for rel in sorted(measured["pins"][group])]
    facts = {
        "schema": RECORDS_SCHEMA,
        "note": FACTS_NOTE,
        "engine": measured["engine"],
        "issued_admissions": sorted(measured["issued_admissions"]),
        "reference_faults": sorted(measured["reference_faults"]),
        "mechanism": mechanism,
    }
    rows = sorted(([row[0], row[2]] for row in measured["case_rows"]),
                  key=lambda row: row[0])
    return {
        CASES_RECORDS: _record_text(rows),
        PINS_RECORDS: _record_text(pins),
        FACTS_RECORDS: json.dumps(facts, indent=1, sort_keys=True) + "\n",
    }


def _read_records(path: Path, width: int) -> list[list[str]]:
    rows = []
    for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if (not isinstance(row, list) or len(row) != width
                or not all(isinstance(x, str) for x in row)):
            raise ValueError(f"{path.name}:{lineno}: not a {width}-field record")
        rows.append(row)
    return rows


def load_records(base: Path = RECORDS) -> dict:
    """The committed records, parsed: `cases` as `[case id, bucket]` rows,
    `pins` as `{group: [file, ...]}`, `facts` as written. Raises
    `OSError`/`ValueError` on a file that is absent or not in the record
    format, so a caller decides whether that is unusable input (`--verify`)
    or every path moving (the filter)."""
    base = Path(base)
    cases = _read_records(base / CASES_RECORDS, 2)
    pins: dict[str, list[str]] = {g: [] for g in VERDICT_PIN_GROUPS}
    for group, rel in _read_records(base / PINS_RECORDS, 2):
        if group not in pins:
            raise ValueError(f"{PINS_RECORDS}: unknown pin group {group!r}")
        if rel in pins[group]:
            raise ValueError(f"{PINS_RECORDS}: {group} pins {rel} twice")
        pins[group].append(rel)
    facts = json.loads((base / FACTS_RECORDS).read_text(encoding="utf-8"))
    if not isinstance(facts, dict) or facts.get("schema") != RECORDS_SCHEMA:
        raise ValueError(f"{FACTS_RECORDS} is not a {RECORDS_SCHEMA} file")
    return {"cases": cases, "pins": pins, "facts": facts}


class StaleRecords(Exception):
    """The committed records do not describe this checkout."""


def hydrate(records: dict, sources: dict[str, list[str]]) -> dict:
    """The records with the digests the checkout supplies: every pinned file's
    sha256 read from the tree, and every program's from `sources` (case id to
    its source text, once per occurrence, in run order). Raises
    `StaleRecords` when a pinned file is gone or a recorded program is not in
    the corpus, because a report rendered over either would name inputs the
    run did not have."""
    pins: dict[str, dict[str, str]] = {}
    for group in VERDICT_PIN_GROUPS:
        pins[group] = {}
        for rel in records["pins"][group]:
            if not (ROOT / rel).is_file():
                raise StaleRecords(f"{rel} is pinned and not in the tree")
            pins[group][rel] = _sha(rel)
    taken: dict[str, int] = {}
    rows = []
    for case_id, bucket in records["cases"]:
        index = taken.get(case_id, 0)
        taken[case_id] = index + 1
        here = sources.get(case_id, [])
        if index >= len(here):
            raise StaleRecords(f"{case_id} is recorded and not in the corpus")
        rows.append([case_id, hashlib.sha256(
            here[index].encode("utf-8", "surrogatepass")).hexdigest(), bucket])
    return {"cases": rows, "pins": pins, "facts": records["facts"]}


def corpus_sources(cases) -> dict[str, list[str]]:
    """`{case id: [source, ...]}` from a `load_corpus` list, run order kept."""
    out: dict[str, list[str]] = {}
    for case_id, source in cases:
        out.setdefault(case_id, []).append(source)
    return out


def measured_from_records(records: dict) -> dict:
    """The part of a `measure_pinned` result `build_report` reads, rebuilt
    from HYDRATED records (`hydrate`). Every aggregate is recomputed here."""
    rows = [list(r) for r in records["cases"]]
    buckets: dict[str, list[str]] = {}
    seen: dict[str, int] = {}
    for case_id, _sha, bucket in rows:
        buckets.setdefault(bucket, []).append(case_id)
        seen[case_id] = seen.get(case_id, 0) + 1
    facts = records["facts"]
    return {
        "buckets": buckets,
        "case_rows": rows,
        "engine": facts["engine"],
        "issued_admissions": list(facts["issued_admissions"]),
        "reference_faults": list(facts["reference_faults"]),
        "n_distinct": len(seen),
        "repeated_case_ids": sorted(c for c, n in seen.items() if n > 1),
        "pins": {"note": PINS_NOTE,
                 **{g: dict(records["pins"][g]) for g in VERDICT_PIN_GROUPS}},
    }


def published_from_records(records: dict) -> dict:
    """HYDRATED records as the `census` section `judge` reads."""
    rows = [list(r) for r in records["cases"]]
    counts: dict[str, int] = {}
    for _, _, bucket in rows:
        counts[bucket] = counts.get(bucket, 0) + 1
    return {
        "schema": CENSUS_SCHEMA,
        "cases": rows,
        "buckets": [{"bucket": k, "count": v} for k, v in sorted(counts.items())],
        "false_admission": {
            "members": sorted({cid for cid, _, b in rows
                               if b == "false-admission"}),
            "mechanism": records["facts"]["mechanism"],
        },
        "pins": {g: dict(records["pins"][g]) for g in VERDICT_PIN_GROUPS},
    }


def filter_view(records: dict) -> dict:
    """Unhydrated records as `moved_inputs` reads them: which files are
    pinned and which programs are carried."""
    return {"census": {
        "cases": [[cid, "", bucket] for cid, bucket in records["cases"]],
        "pins": {g: {rel: "" for rel in records["pins"][g]}
                 for g in VERDICT_PIN_GROUPS},
    }}


def report_from_records(base: Path = RECORDS,
                        crate_json: Path | None = None) -> dict:
    """The full report, rendered from the committed records with no census
    run. The digests come from the checkout, so the checkout has to be the one
    the records describe; `--verify --strict` is what holds that on main."""
    records = load_records(base)
    census = _load("tools/gate_reference_census.py", "artifact_records_census")
    _reference, oracle = census._reference()
    try:
        hydrated = hydrate(records, corpus_sources(census.load_corpus(oracle)))
    except StaleRecords as exc:
        raise SystemExit(
            f"census_artifact: the committed records do not describe this "
            f"checkout ({exc}); regenerate them: "
            f"python3 tools/census_artifact.py --write") from None
    provenance = _load("tools/corpus_provenance.py",
                       "artifact_records_provenance")
    return build_report(census, provenance, measured_from_records(hydrated),
                        _read_crate(crate_json),
                        probe=records["facts"]["mechanism"])


def record_problems(fresh: dict[str, str], base: Path = RECORDS) -> list[str]:
    """Each committed record file that is not byte for byte what `fresh`
    says. Pure apart from reading `base`."""
    problems = []
    for name in RECORD_FILES:
        path = Path(base) / name
        rel = (path.relative_to(ROOT).as_posix()
               if path.is_relative_to(ROOT) else path.name)
        if not path.is_file():
            problems.append(f"{rel} does not exist")
        elif path.read_text(encoding="utf-8") != fresh[name]:
            problems.append(
                f"{rel} differs from a fresh run; it was hand-edited, merged "
                f"by hand, or the corpus, the checker or the baseline moved "
                f"under it")
    return problems


# ------------------------------------------------------------ the verifier
#
# `--check` answers "is the committed file what this tree produces today", which
# is the right question for the repository and the wrong one for a reader who
# holds a PUBLISHED copy: the corpus grows every week, so `--check` reds on a
# reader's clone for a reason that says nothing about whether the published
# numbers were true. `--verify` answers the reader's question instead. It
# compares the pins first, so a difference in the INPUTS is never reported as a
# difference in the RESULT, and then compares verdicts case by case on every
# published program whose bytes did not change.

VERDICT_EXIT = {"reproduced": 0, "refuted": 1, "partial": 3,
                "different-inputs": 3}


def _pin_diff(published: dict, local: dict) -> dict:
    return {
        "moved": sorted(k for k in published
                        if k in local and local[k] != published[k]),
        "missing": sorted(k for k in published if k not in local),
        "added": sorted(k for k in local if k not in published),
    }


def _bucket_counts(rows) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _, _, name in rows:
        counts[name] = counts.get(name, 0) + 1
    return counts


def judge(published: dict, local: dict) -> dict:
    """Compare a published `census` section against a local measurement.

    `local` carries `pins`, `cases` (rows as `case_rows` builds them) and
    `mechanism_holds`. Pure: it runs nothing, so every arm of the verdict is
    testable without a census run.

    The verdict is one of:

      * `refuted`: the published file contradicts itself, or the inputs that
        decide a verdict are byte-identical and some unchanged program landed
        in a different bucket, or the zero-tolerance mechanism does not hold.
      * `reproduced`: those inputs are identical and every published program
        is present, unchanged, and in the bucket the file says.
      * `partial`: those inputs are identical and every published program that
        is still present and unchanged reproduced, but some are gone or edited.
      * `different-inputs`: a file that decides a verdict moved, or the run
        read a file the publication does not pin. This is not a check of the
        published numbers, and the per-case agreement is reported as
        information only.
    """
    pub_pins = published.get("pins") or {}
    loc_pins = local["pins"]
    rows = published.get("cases") or []

    # The published file against itself: the bucket table must be what the
    # rows add up to, and the zero-tolerance bucket must be empty in both.
    table = {row["bucket"]: row["count"] for row in published.get("buckets", [])}
    derived = _bucket_counts(rows)
    self_contradictions = []
    if table != derived:
        names = sorted(set(table) | set(derived))
        self_contradictions += [
            f"bucket {n}: table says {table.get(n, 0)}, rows add up to "
            f"{derived.get(n, 0)}" for n in names
            if table.get(n, 0) != derived.get(n, 0)]
    published_admissions = sorted(
        {cid for cid, _, b in rows if b == "false-admission"}
        | set(published.get("false_admission", {}).get("members", [])))
    if published_admissions:
        self_contradictions.append(
            "the published file lists false-admission members: "
            + ", ".join(published_admissions))
    if not published.get("false_admission", {}).get("mechanism", {}).get("holds"):
        self_contradictions.append(
            "the published file does not record the mechanism as holding")

    deciding = _pin_diff(pub_pins.get("decides_verdicts", {}),
                         loc_pins["decides_verdicts"])
    reference = _pin_diff(pub_pins.get("reference", {}), loc_pins["reference"])
    report_inputs = _pin_diff(pub_pins.get("report_inputs", {}),
                              loc_pins["report_inputs"])
    # A file the local run opened that the publication does not pin at all is
    # an input the published identity never covered. It is not a local change;
    # it is a hole in the publication, and it is named as one.
    unpinned = deciding["added"]
    inputs_same = not (deciding["moved"] or deciding["missing"] or unpinned
                       or reference["moved"] or reference["missing"]
                       or reference["added"])

    local_rows: dict[str, list[tuple[str, str]]] = {}
    for cid, sha, name in local["cases"]:
        local_rows.setdefault(cid, []).append((sha, name))
    agree, differ, edited, gone = [], [], [], []
    seen: dict[str, int] = {}
    for cid, sha, name in rows:
        i = seen.get(cid, 0)
        seen[cid] = i + 1
        here = local_rows.get(cid, [])
        if i >= len(here):
            gone.append(cid)
            continue
        local_sha, local_name = here[i]
        if local_sha != sha:
            edited.append(cid)
        elif local_name == name:
            agree.append(cid)
        else:
            differ.append({"case": cid, "published": name, "local": local_name})
    published_ids = {cid for cid, _, _ in rows}
    new = sorted({cid for cid in local_rows if cid not in published_ids})
    local_admissions = sorted({cid for cid, _, b in local["cases"]
                               if b == "false-admission"})

    if self_contradictions:
        verdict = "refuted"
    elif not inputs_same:
        verdict = "different-inputs"
    elif differ or not local["mechanism_holds"] or local_admissions:
        verdict = "refuted"
    elif edited or gone:
        verdict = "partial"
    else:
        verdict = "reproduced"

    return {
        "verdict": verdict,
        "exit": VERDICT_EXIT[verdict],
        "self_contradictions": self_contradictions,
        "decides_verdicts": deciding,
        "unpinned_inputs": unpinned,
        "reference": reference,
        "report_inputs": report_inputs,
        "published_cases": len(rows),
        "checked": len(agree) + len(differ),
        "agree": len(agree),
        "differ": differ,
        "edited": sorted(set(edited)),
        "gone": sorted(set(gone)),
        "new": new,
        "local_false_admissions": local_admissions,
        "local_mechanism_holds": local["mechanism_holds"],
    }


# ------------------------------------------------- the repository's own gate
#
# `--verify` is the reader's question, and a reader's clone is expected to have
# moved. The repository asks a stricter one of its OWN committed copy, in CI, on
# every pull request whose diff moves an input of the artifact (issue #1572):
# is the committed file what this tree produces? That is a full reproduction
# with nothing left over: every published program recomputed from byte-identical
# inputs, no program in the corpus that the file does not carry, and no report
# input moved under it. A pull request that fails it regenerates the artifact
# with `--write` in the same diff, the way a golden is regenerated.

# The files that are inputs of the artifact whatever its pins say: the tool, the
# artifact itself (a hand edit is a change the gate must see), and the declared
# lists above.
OWN_FILES = ("tools/census_artifact.py",
             *(f"docs/census-artifact/{name}" for name in RECORD_FILES))


def current_problems(result: dict) -> list[str]:
    """Why a `judge` result on the COMMITTED artifact is not a current one.

    Empty means the committed file is what this tree produces, program by
    program. Pure, so each arm is testable without a census run."""
    problems = []
    if result["verdict"] != "reproduced":
        problems.append(f"the verdict is {result['verdict']}, not reproduced")
    if result["new"]:
        problems.append(
            f"{_plural(len(result['new']), 'program', 'programs')} in the "
            f"corpus {'is' if len(result['new']) == 1 else 'are'} not in the "
            f"committed artifact")
    moved = result["report_inputs"]
    for kind in ("moved", "missing", "added"):
        for rel in moved[kind]:
            problems.append(f"report input {kind}: {rel}")
    return problems


def _glob_regex(pattern: str) -> str:
    """A `Path.glob` pattern as an anchored regular expression over a
    repo-relative name: `**/` is any number of directories, `*` stays within
    one."""
    import re  # noqa: PLC0415
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return "^" + "".join(out) + "$"


def moved_inputs(paths, committed: dict | None) -> list[str]:
    """The changed repo-relative `paths` that are inputs of the artifact.

    An input is a file the committed artifact pins (a deciding file, a
    reference module, a report input), a program it carries, a file in the
    declared lists, a gate crate source, or a `.rvl` under a census directory,
    which is how a NEW program enters the corpus. `committed` is
    `filter_view(load_records())` for the committed records;
    None (absent or unreadable) makes every path an input, so a broken
    artifact cannot switch its own gate off."""
    import re  # noqa: PLC0415
    paths = [p.strip() for p in paths if p.strip()]
    if committed is None:
        return paths
    c = committed.get("census") or {}
    pins = c.get("pins") or {}
    named = set(OWN_FILES) | set(CHECKER_SOURCES) | set(REPORT_INPUTS)
    for group in ("decides_verdicts", "reference", "report_inputs"):
        named |= set(pins.get(group) or {})
    named |= {row[0] for row in c.get("cases") or []}
    crate = [re.compile(_glob_regex(g)) for g in GATE_CRATE_GLOBS]
    census = _load("tools/gate_reference_census.py", "artifact_moved_census")
    # A census directory can be nested (`tests/fixtures`), so it is a prefix.
    corpus_dirs = tuple(d + "/" for d in census.CORPUS_DIRS)
    skip = set(census._SKIP_DIRS)
    moved = []
    for rel in paths:
        parts = rel.split("/")
        if (rel in named or any(r.match(rel) for r in crate)
                or (rel.endswith(".rvl") and rel.startswith(corpus_dirs)
                    and not skip & set(parts))):
            moved.append(rel)
    return moved


def render_verdict(result: dict) -> str:
    """The judgement as text a reader can act on, every difference named."""
    out: list[str] = []
    w = out.append
    w(f"census verify: {result['verdict'].upper()}")
    w(f"  published programs: {result['published_cases']}; checked "
      f"(same bytes on both sides): {result['checked']}; same bucket: "
      f"{result['agree']}")
    for c in result["self_contradictions"]:
        w(f"  THE PUBLISHED FILE CONTRADICTS ITSELF: {c}")
    for group in ("decides_verdicts", "reference", "report_inputs"):
        diff = result[group]
        for kind in ("moved", "missing", "added"):
            if group == "decides_verdicts" and kind == "added":
                continue
            for rel in diff[kind]:
                w(f"  {group}: {kind}: {rel}")
    for rel in result["unpinned_inputs"]:
        w(f"  UNPINNED INPUT: the census read {rel}, which the publication "
          f"does not pin")
    for d in result["differ"]:
        w(f"  verdict differs: {d['case']}: published {d['published']}, "
          f"here {d['local']}")
    for label, key in (("edited since publication", "edited"),
                       ("absent here", "gone"),
                       ("new here, not in the publication", "new")):
        if result[key]:
            w(f"  {label}: {len(result[key])}")
            for cid in result[key][:10]:
                w(f"    {cid}")
            if len(result[key]) > 10:
                w(f"    ... and {len(result[key]) - 10} more")
    for cid in result["local_false_admissions"]:
        w(f"  FALSE ADMISSION in this run: {cid}")
    if not result["local_mechanism_holds"]:
        w("  the NEVER_BASELINED mechanism does NOT hold in this checkout")
    meaning = {
        "reproduced": ("every published verdict was recomputed here from "
                       "byte-identical inputs and matched."),
        "partial": ("the inputs that decide a verdict are byte-identical and "
                    "every published program still present reproduced; the "
                    "ones named above were edited or removed and were not "
                    "checked."),
        "different-inputs": ("a file that decides a verdict differs from the "
                             "published one, so this run is a new measurement "
                             "and not a check of the published numbers. Check "
                             "out a tree whose files match the published pins "
                             "to check them."),
        "refuted": ("the published numbers do not hold on the inputs they "
                    "name."),
    }[result["verdict"]]
    w(f"  meaning: {meaning}")
    return "\n".join(out)


def _published(path: Path, sources: dict[str, list[str]] | None = None
               ) -> tuple[dict | None, str]:
    """`(census section, error)` for a published copy at `path`: the records
    directory (hydrated from this checkout and `sources`), or a rendered
    `EVAL-REPORT-1` JSON report."""
    path = Path(path)
    if path.is_dir():
        try:
            records = load_records(path)
            if sources is None:
                return {"schema": CENSUS_SCHEMA}, ""
            return published_from_records(hydrate(records, sources)), ""
        except StaleRecords as exc:
            return {"schema": CENSUS_SCHEMA, "stale": str(exc)}, ""
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return None, f"census verify: cannot read the records in {path}: {exc}"
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"census verify: cannot read {path}: {exc}"
    published = bundle.get("census") or {}
    if published.get("schema") != CENSUS_SCHEMA:
        return None, (f"census verify: {path} carries no {CENSUS_SCHEMA} "
                      f"section")
    if not published.get("pins") or not published.get("cases"):
        return None, (f"census verify: {path} predates per-case rows and pins, "
                      f"so there is nothing to verify it against; regenerate "
                      f"it with --write")
    return published, ""


def verify(path: Path, engine: str = "selfhost",
           strict: bool = False) -> tuple[int, str]:
    """Re-measure this checkout and judge the published copy at `path`, the
    committed records directory or a rendered JSON report.

    The records carry no digest, so for them the pins and the source digests
    are this checkout's: what is judged is the SET of pinned files and every
    program's bucket. `strict` is the repository's gate on its own committed
    copy: on top of the reader's verdict it fails on anything
    `current_problems` names and, for a records directory, on any record file
    that is not byte for byte what this run writes."""
    published, error = _published(path)
    if published is None:
        return 2, error
    census, measured = measure_pinned(engine)
    probe = probe_never_baselined(census)
    if Path(path).is_dir():
        published, error = _published(path, corpus_sources(measured["cases"]))
    local = {"pins": measured["pins"], "cases": measured["case_rows"],
             "mechanism_holds": probe["holds"]}
    if published.get("stale"):
        result, text = None, (f"census verify: the committed records name an "
                              f"input this run did not read: "
                              f"{published['stale']}")
        problems = [published["stale"]]
        code = 3
    else:
        result = judge(published, local)
        text = render_verdict(result)
        if not strict:
            return result["exit"], text
        problems = current_problems(result)
        code = result["exit"] or 3
    if not strict:
        return code, text
    if Path(path).is_dir():
        problems += record_problems(record_texts(measured, probe), Path(path))
    if not problems:
        return 0, text + "\ncensus verify --strict: the committed artifact is current."
    lines = [text, "census verify --strict: the committed artifact is NOT "
             "current:"]
    lines += [f"  {p}" for p in problems]
    lines.append("  regenerate it in this pull request: "
                 "python3 tools/census_artifact.py --write")
    return code, "\n".join(lines)


def render_verify_section(c: dict, w) -> None:
    """How a reader holding a published copy checks it, and what each way of
    cooking this benchmark runs into. The second half is the argument, so
    each row names whether a tool closes the move or only public history does.
    """
    pins = c.get("pins") or {}
    deciding = sorted(pins.get("decides_verdicts", {}))
    w("## Verifying a published copy")
    w("")
    w("`--check` asks whether this file is what today's tree produces, and it")
    w("fails as soon as the corpus grows. A reader holding a copy published")
    w("earlier needs a different question answered: were the numbers true on")
    w("the inputs they name? That is `--verify`:")
    w("")
    w("```")
    w(".venv/bin/python tools/census_artifact.py --verify path/to/census-artifact.json")
    w("```")
    w("")
    w("It re-runs the census in your clone and compares in two steps. First the")
    w("pins: every file the published verdicts depend on, each by sha256. Then")
    w(f"the verdicts, one row per program: this file carries {len(c.get('cases') or [])}")
    w("rows of case id, sha256 of the source and bucket, and every row whose")
    w("source is byte-identical in your clone is recomputed and compared.")
    w("")
    w("| verdict | exit | meaning |")
    w("|---|---|---|")
    w("| reproduced | 0 | the inputs that decide a verdict are byte-identical and every published row matched |")
    w("| refuted | 1 | same inputs, different verdict; or the file contradicts itself; or a false admission; or the mechanism does not hold |")
    w("| partial | 3 | same inputs, every row still present matched, but some programs were edited or removed since |")
    w("| different-inputs | 3 | a file that decides a verdict differs, so the run is a new measurement and not a check |")
    w("")
    w("The files that decide a verdict are MEASURED rather than listed. The")
    w("generator records, through a Python audit hook, every file under the")
    w("checkout the census run opens, and pins each one. For this run that is")
    w(f"the corpus (per row), the `{REFERENCE_GLOB}` modules it opened ("
      f"{_plural(len(pins.get('reference', {})), 'file', 'files')}), and:")
    w("")
    for rel in deciding:
        w(f"- `{rel}`")
    w("")
    w("`--verify` measures the same set on your side, and a file your run")
    w("opened that the publication does not pin is reported by name as an")
    w("UNPINNED INPUT. A list someone wrote down can forget a file. This one")
    w("cannot, short of the file being opened by something the hook does not")
    w("see, such as a subprocess.")
    w("")
    w("### What each way of cooking this runs into")
    w("")
    w("| move | what stops it | by the tool, or by history |")
    w("|---|---|---|")
    w("| record a `false-admission` into the baseline | `--record` drops it (`NEVER_BASELINED`) | tool |")
    w("| hand-edit the baseline to tolerate one | `--check` fails on any member whatever the baseline says | tool |")
    w("| edit a count in `census-artifact.json` | its bucket table must equal the sum of its per-case rows, and `--verify` recomputes every row | tool |")
    w("| edit a row and the count together | `--verify` recomputes the row from pinned inputs and reports the case by name | tool |")
    w("| measure with one gate, emitter or classifier and publish another | each is pinned by sha256, measured; a reader's run names any file that moved or was never pinned | tool |")
    w("| quote the fast engine where the real crate disagrees | the crate run is recorded at a checker version, and a stale one lifts no claim | tool |")
    w("| drop hard programs from the corpus before publishing | the corpus is every `.rvl` under fixed directories plus three inline lists, globbed and not selected; a removal is a public diff | history |")
    w("| bend the reference until it agrees with the gate | the reference is pinned, so the bent version is the one published and readable | history |")
    w("| mislabel a document's provenance | nothing; stated below | history |")
    w("")
    w("The rows marked tool are closed by construction: no edit to the")
    w("published JSON survives `--verify` on the pinned inputs, and no edit to")
    w("the baseline can tolerate a `false-admission`. The rows marked history are closed only because the repository")
    w("is public and every one of those moves is a diff somebody can read.")
    w("")


# ------------------------------------------------------------------ the CLI

def trim_reproduction(census, raw: dict) -> dict:
    """A raw `--engine crate --json` census, reduced to what is recorded.

    Only the tracked buckets and the programs the run covered: the untracked
    buckets are where the two engines are ALLOWED to be described
    differently, and recording them would make the fixture churn on changes
    that cannot hide a divergence. The programs are a sorted list, a
    repeated case id once per run of it, and the count is derived from them.
    """
    if raw.get("engine") != "crate":
        raise SystemExit(
            f"census_artifact: --crate-json names a run of engine "
            f"{raw.get('engine')!r}, not 'crate'; the reproduction must come "
            f"from the real crate or it is not one")
    buckets = raw.get("buckets", {})
    version, _ = checker_version()
    return {
        "note": ("A recorded `tools/gate_reference_census.py --engine crate` "
                 "run, read by tools/census_artifact.py. It is recorded rather "
                 "than re-run because it needs cargo and takes minutes. The "
                 "checker version it was taken at is recorded with it, and a "
                 "reproduction taken at a different version is reported as "
                 "stale and lifts no claim."),
        "engine": "crate",
        "checker_version": version,
        "tracked_buckets": {k: sorted(v) for k, v in sorted(buckets.items())
                            if k.split("/", 1)[0] in census.TRACKED},
        "false_admissions": sorted(buckets.get(census.ADMISSION, [])),
        "programs": sorted(cid for ids in buckets.values() for cid in ids),
    }


def reproduction_texts(recorded: dict) -> dict[str, str]:
    """`{file name: bytes}` for `tests/fixtures/census_crate_reproduction/`."""
    # `n` is derived on load and `programs` has its own file; neither is a fact.
    facts = {k: v for k, v in recorded.items() if k not in ("programs", "n")}
    return {
        REPRODUCTION_FACTS: json.dumps(facts, indent=1, sort_keys=True) + "\n",
        REPRODUCTION_PROGRAMS: _record_text(sorted(recorded["programs"])),
    }


def load_reproduction(base: Path = CRATE_REPRODUCTION) -> dict | None:
    """The recorded reproduction, with `n` derived from its program list.
    None when nothing is recorded."""
    base = Path(base)
    facts_path = base / REPRODUCTION_FACTS
    if not facts_path.is_file():
        return None
    recorded = json.loads(facts_path.read_text(encoding="utf-8"))
    programs = []
    for lineno, line in enumerate(
            (base / REPRODUCTION_PROGRAMS).read_text(encoding="utf-8")
            .splitlines(), 1):
        if not line.strip():
            continue
        case_id = json.loads(line)
        if not isinstance(case_id, str):
            raise ValueError(f"{REPRODUCTION_PROGRAMS}:{lineno}: not a case id")
        programs.append(case_id)
    recorded["programs"] = programs
    recorded["n"] = len(programs)
    return recorded


def _read_crate(crate_json: Path | None) -> dict | None:
    """The recorded crate reproduction, or `crate_json` in its place."""
    if crate_json is None:
        return load_reproduction()
    return json.loads(Path(crate_json).read_text(encoding="utf-8"))


def generate(engine: str,
             crate_json: Path | None) -> tuple[dict, dict[str, str]]:
    """`(report, record texts)` from one fresh census run."""
    # The measurement goes FIRST, before anything else is imported, so every
    # file it depends on is opened inside the recording.
    census, measured = measure_pinned(engine)
    provenance = _load("tools/corpus_provenance.py", "artifact_provenance")
    probe = probe_never_baselined(census)
    checker = checker_version()
    report = build_report(census, provenance, measured, _read_crate(crate_json),
                          probe=probe, checker=checker)
    return report, record_texts(measured, probe)


def measure_pinned(engine: str):
    """`(census module, measure(...))`, with every file the run opens pinned.

    The census module is loaded INSIDE the recording, so the files it and the
    reference pull in are measured rather than assumed. Call it before
    anything else in the process imports the reference: a module that is
    already imported is not opened again, and would be missing from the pins.
    """
    with recording_reads() as reads:
        census = _load("tools/gate_reference_census.py", "artifact_census")
        measured = measure(census, engine)
    measured["pins"] = build_pins(measured, reads)
    measured["case_rows"] = case_rows(measured)
    return census, measured


def _serialise(report: dict) -> str:
    return json.dumps(report, indent=1, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", default="selfhost",
                    choices=("selfhost", "crate"),
                    help="the engine the census runs (default: selfhost)")
    ap.add_argument("--crate-json", type=Path,
                    help="a raw `--engine crate --json` census, in place of "
                         "the recorded reproduction")
    ap.add_argument("--record-reproduction", action="store_true",
                    help="trim --crate-json into "
                         "tests/fixtures/census_crate_reproduction/ "
                         "and stop")
    ap.add_argument("--write", action="store_true",
                    help="write the records in docs/census-artifact/")
    ap.add_argument("--check", action="store_true",
                    help="fail when the committed records have drifted")
    ap.add_argument("--json", action="store_true",
                    help="print the report as EVAL-REPORT-1 JSON instead of "
                         "markdown")
    ap.add_argument("--from-records", action="store_true",
                    help="render the report from the committed records "
                         "instead of a fresh census run")
    ap.add_argument("--verify", nargs="?", const=str(RECORDS),
                    metavar="REPORT",
                    help="re-run the census here and judge a published copy "
                         "(a rendered census-artifact.json, or a records "
                         "directory) against it, pin by pin and case by case "
                         "(default: the committed records). Exit 0 "
                         "reproduced, 1 refuted, 2 unusable input, 3 not a "
                         "full check (inputs differ, or programs were edited "
                         "or removed)")
    ap.add_argument("--strict", action="store_true",
                    help="with --verify: also fail unless the copy is CURRENT, "
                         "a full reproduction with no program missing from it, "
                         "no report input moved and, for the records, every "
                         "record file byte-identical to a fresh run. The CI "
                         "gate on the committed copy (issue #1572)")
    ap.add_argument("--moved-inputs", action="store_true",
                    help="read changed repo-relative paths on stdin, print the "
                         "ones that are inputs of the committed artifact. Exit "
                         "0 when at least one is, 1 when none is. CI uses it to "
                         "decide whether a pull request runs --verify --strict")
    args = ap.parse_args(argv)

    if args.moved_inputs:
        try:
            committed = filter_view(load_records())
        except (OSError, ValueError, KeyError, TypeError):
            committed = None
        moved = moved_inputs(sys.stdin.read().splitlines(), committed)
        for rel in moved:
            print(rel)
        return 0 if moved else 1

    if args.strict and args.verify is None:
        ap.error("--strict is a mode of --verify")

    if args.verify is not None:
        code, text = verify(Path(args.verify), args.engine, strict=args.strict)
        print(text)
        return code

    if args.write and args.check:
        ap.error("--write and --check are opposites; pick one")
    if args.from_records and (args.write or args.check):
        ap.error("--from-records renders the committed records; it writes "
                 "and checks nothing")

    if args.record_reproduction:
        if args.crate_json is None:
            ap.error("--record-reproduction needs --crate-json")
        census = _load("tools/gate_reference_census.py", "artifact_census")
        raw = json.loads(args.crate_json.read_text(encoding="utf-8"))
        CRATE_REPRODUCTION.mkdir(parents=True, exist_ok=True)
        for name, text in reproduction_texts(
                trim_reproduction(census, raw)).items():
            (CRATE_REPRODUCTION / name).write_text(text, encoding="utf-8")
        print(f"recorded {CRATE_REPRODUCTION.relative_to(ROOT)}/")
        return 0

    if args.from_records:
        report = report_from_records(RECORDS, args.crate_json)
        print(_serialise(report) if args.json else render_markdown(report),
              end="" if args.json else "\n")
        return 0

    report, records = generate(args.engine, args.crate_json)

    if args.write:
        RECORDS.mkdir(parents=True, exist_ok=True)
        for name in RECORD_FILES:
            (RECORDS / name).write_text(records[name], encoding="utf-8")
            print(f"wrote {(RECORDS / name).relative_to(ROOT)}")
        return 0

    if args.check:
        drift = record_problems(records)
        if drift:
            print("census artifact FAILED:")
            for line in drift:
                print(f"  {line}")
            print("  regenerate: python3 tools/census_artifact.py --write")
            return 1
        print("census artifact: the records in docs/census-artifact/ are "
              "current.")
        return 0

    if args.json:
        print(_serialise(report), end="")
    else:
        print(render_markdown(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
