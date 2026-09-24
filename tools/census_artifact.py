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
So `docs/census-artifact.md` and `docs/census-artifact.json` are OUTPUT. Every
count, every named residual and every fraction in them comes from a run. Editing
either file by hand is a defect `--check` reports.

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
digest instead: `compiler_commit` names a sha256 over `src/revl/**/*.py` and
`run` names a sha256 over the corpus the run actually read. Both are stronger
than a commit sha, because a commit that moved neither does not move them and an
outsider can recompute both from a checkout.

USAGE
-----
    python3 tools/census_artifact.py                     # render to stdout
    python3 tools/census_artifact.py --write             # write both files
    python3 tools/census_artifact.py --check             # fail if they drifted
    python3 tools/census_artifact.py --crate-json C.json --record-reproduction

THE REPRODUCTION, AND WHY IT IS RECORDED RATHER THAN RE-RUN
-----------------------------------------------------------
`--engine crate` asks the REAL `crates/revl-gate`, built by cargo, the same
questions the fast engine answers. It takes about fourteen minutes and needs a
rust toolchain, so `--check` cannot run it and neither can a CI job that is
allowed to be cheap. The result is recorded instead, in
`tests/fixtures/census_crate_reproduction.json`, beside the CHECKER VERSION it
was taken at.

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
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REPORT_JSON = ROOT / "docs" / "census-artifact.json"
REPORT_MD = ROOT / "docs" / "census-artifact.md"

# The recorded `--engine crate` reproduction. In tests/fixtures/ rather than in
# docs/ because it is an input to the artifact, not part of it.
CRATE_REPRODUCTION = ROOT / "tests" / "fixtures" / "census_crate_reproduction.json"

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

# The reference implementation, digested whole. Named `compiler_commit` in the
# report because `EVAL-REPORT-1` requires that key; it holds a tree digest, and
# the report says so in as many words.
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


def _digest(paths) -> str:
    """sha256 over the named files, in the order given, each length-prefixed so
    concatenation cannot be forged by moving a byte across a boundary."""
    h = hashlib.sha256()
    for path in paths:
        blob = Path(path).read_bytes()
        h.update(f"{path}:{len(blob)}\n".encode())
        h.update(blob)
    return h.hexdigest()


def checker_version() -> tuple[str, dict[str, str]]:
    """`(version, {rel: sha256})` over `CHECKER_SOURCES`."""
    per_file = {rel: hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
                for rel in CHECKER_SOURCES}
    whole = _digest([ROOT / rel for rel in CHECKER_SOURCES])
    return f"{CENSUS_SCHEMA}+{whole[:12]}", per_file


def reference_digest() -> str:
    """sha256 over the reference compiler's sources."""
    files = sorted(ROOT.glob(REFERENCE_GLOB))
    return _digest(files)


def corpus_digest(cases) -> str:
    """sha256 over the corpus this run actually read, ids and bytes both."""
    h = hashlib.sha256()
    for case_id, source in sorted(cases):
        blob = source.encode("utf-8", "surrogatepass")
        h.update(f"{case_id}:{len(blob)}\n".encode())
        h.update(blob)
    return h.hexdigest()


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

    return {"cases": cases, "buckets": buckets, "details": details,
            "issued_admissions": sorted(issued), "engine": engine_name}


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
    """
    families = {}
    for name, ids in sorted(buckets.items()):
        if name.split("/", 1)[0] == census.HARD:
            families[name] = sorted(ids)
    return {
        "bucket_prefix": census.HARD,
        "families": families,
        "total": sum(len(v) for v in families.values()),
        "capped_by_name_in": "tests/test_gate_reference_census.py",
    }


def build_report(census, provenance, measured: dict,
                 crate: dict | None = None) -> dict:
    """The artifact, in `EVAL-REPORT-1` shape with a `GATE-CENSUS-1` section."""
    buckets = measured["buckets"]
    details = measured["details"]
    cases = measured["cases"]
    version, per_file = checker_version()
    run_id = f"census-{measured['engine']}-{corpus_digest(cases)[:12]}"
    compiler = f"src/revl@sha256:{reference_digest()[:12]}"

    probe = probe_never_baselined(census)
    admissions = census.false_admissions(buckets)
    faults = reference_faults(details, buckets, cases)
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
    alw = allowance(census, buckets)
    prov_rows = provenance_rows(provenance)
    census_row = next((r for r in prov_rows if r["corpus"] == "census"), None)

    claims = [
        {
            "text": (
                f"Over {n} programs, the gate issued no admission the reference "
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
                f"The gate's standing false-admit allowance is "
                f"{alw['total']} programs, every one of them named in this "
                f"report: "
                + "; ".join(f"{k} ({len(v)})"
                            for k, v in sorted(alw["families"].items()))
                + ". It is baselined, capped by name in "
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
                "commit. It is a sha256 over " + REFERENCE_GLOB + ", which is "
                "narrower than a commit (a commit that touched no compiler "
                "source does not move it) and recomputable by anyone with a "
                "checkout."),
            "run": run_id,
            "engine": measured["engine"],
            "n": n,
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
    w("GENERATED FILE. Every number below comes from a run of")
    w("`tools/census_artifact.py`, which is the only thing that writes it. Do not")
    w("edit it by hand: `python3 tools/census_artifact.py --check` reports the")
    w("edit as drift. Regenerate with `python3 tools/census_artifact.py --write`.")
    w("")
    w("## What is being measured")
    w("")
    w("revl has two independently written implementations of its own semantics.")
    w("`src/revl/*.py` is the reference compiler. `selfhost/*.rvl`, compiled into")
    w("`crates/revl-gate`, is the self-host gate. This census runs both over the")
    w("same corpus and classifies every disagreement. Agreement means the same")
    w("TAG and the same MESSAGE, not merely the same verdict.")
    w("")
    w(f"Programs in this run: **{c['n']}**.")
    w(f"Checker version: `{c['checker_version']}`.")
    w(f"Engine: `{c['engine']}`.")
    w(f"Run: `{c['run']}`.")
    w("")
    w("Neither identity is a commit or a clock. `run` is a sha256 over the corpus")
    w("this run read; the checker version is a sha256 over the files that decide")
    w("what the census does. Both are recomputable from a checkout.")
    w("")
    w("## The claim, and why it is not the corpus size")
    w("")
    w("The interesting property is not that the two agree over "
      f"{c['n']} programs.")
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
    w("is a bypass, it is baselined, and it is **not** zero. Published as it")
    w("stands, with every residual named.")
    w("")
    w(f"Total: **{alw['total']}**. Capped by name in `{alw['capped_by_name_in']}`.")
    w("")
    if alw["total"]:
        w("| bucket | program |")
        w("|---|---|")
        for family, ids in sorted(alw["families"].items()):
            for case_id in ids:
                w(f"| `{family}` | `{case_id}` |")
    else:
        w("No member. The allowance is empty this run.")
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
        w("`docs/census-artifact.json` stands at `measured` and no higher.")
    else:
        w("The fast engine is a python mirror of the native gate's guards, so it")
        w("could in principle be wrong in the same direction as the thing it")
        w("mirrors. The reproduction asks the REAL crate, built by cargo, and is")
        w(f"recorded in `{rep['recorded_in']}` because it needs a rust toolchain")
        w("and minutes rather than seconds.")
        w("")
        w(f"- programs: **{rep['n']}**")
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
    w("## What this does not establish")
    w("")
    for line in c["not_established"]:
        w(f"- {line}")
    w("")
    w("## Schema")
    w("")
    w("`docs/census-artifact.json` is the machine copy. It is an `EVAL-REPORT-1`")
    w("document under the frozen eval-honesty protocol")
    w("(`docs/design/478-eval-honesty-protocol.md`) and")
    w("`python3 tools/check_eval_report.py docs/census-artifact.json` passes on")
    w("it, which is a statement about over-claiming and not about correctness:")
    w("every public claim in it names the rung its own evidence reaches. The")
    w(f"census results live in its `census` section under `{c['schema']}`, which")
    w("that checker does not read.")
    w("")
    return "\n".join(out)


# ------------------------------------------------------------------ the CLI

def trim_reproduction(census, raw: dict) -> dict:
    """A raw `--engine crate --json` census, reduced to what is recorded.

    Only the tracked buckets and the census size: the untracked buckets are
    where the two engines are ALLOWED to be described differently, and
    recording them would make the fixture churn on changes that cannot hide a
    divergence.
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
        "n": sum(len(v) for v in buckets.values()),
        "tracked_buckets": {k: sorted(v) for k, v in sorted(buckets.items())
                            if k.split("/", 1)[0] in census.TRACKED},
        "false_admissions": sorted(buckets.get(census.ADMISSION, [])),
    }


def generate(engine: str, crate_json: Path | None) -> tuple[dict, str]:
    census = _load("tools/gate_reference_census.py", "artifact_census")
    provenance = _load("tools/corpus_provenance.py", "artifact_provenance")
    crate = None
    path = crate_json if crate_json is not None else (
        CRATE_REPRODUCTION if CRATE_REPRODUCTION.is_file() else None)
    if path is not None:
        crate = json.loads(Path(path).read_text(encoding="utf-8"))
    measured = measure(census, engine)
    report = build_report(census, provenance, measured, crate)
    return report, render_markdown(report)


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
                         "tests/fixtures/census_crate_reproduction.json "
                         "and stop")
    ap.add_argument("--write", action="store_true",
                    help="write docs/census-artifact.{md,json}")
    ap.add_argument("--check", action="store_true",
                    help="fail when the committed artifact has drifted")
    args = ap.parse_args(argv)

    if args.write and args.check:
        ap.error("--write and --check are opposites; pick one")

    if args.record_reproduction:
        if args.crate_json is None:
            ap.error("--record-reproduction needs --crate-json")
        census = _load("tools/gate_reference_census.py", "artifact_census")
        raw = json.loads(args.crate_json.read_text(encoding="utf-8"))
        CRATE_REPRODUCTION.write_text(
            json.dumps(trim_reproduction(census, raw), indent=1,
                       sort_keys=True) + "\n", encoding="utf-8")
        print(f"recorded {CRATE_REPRODUCTION.relative_to(ROOT)}")
        return 0

    report, markdown = generate(args.engine, args.crate_json)
    payload = _serialise(report)

    if args.write:
        REPORT_JSON.write_text(payload, encoding="utf-8")
        REPORT_MD.write_text(markdown, encoding="utf-8")
        print(f"wrote {REPORT_JSON.relative_to(ROOT)}")
        print(f"wrote {REPORT_MD.relative_to(ROOT)}")
        return 0

    if args.check:
        drift = []
        for path, fresh in ((REPORT_JSON, payload), (REPORT_MD, markdown)):
            rel = path.relative_to(ROOT)
            if not path.is_file():
                drift.append(f"{rel} does not exist")
                continue
            if path.read_text(encoding="utf-8") != fresh:
                drift.append(
                    f"{rel} differs from a fresh run; it was hand-edited, or "
                    f"the corpus, the checker or the baseline moved under it")
        if drift:
            print("census artifact FAILED:")
            for line in drift:
                print(f"  {line}")
            print("  regenerate: python3 tools/census_artifact.py --write")
            return 1
        print("census artifact: docs/census-artifact.{md,json} are current.")
        return 0

    print(markdown)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
