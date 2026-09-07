#!/usr/bin/env python3
"""Validate an eval report against the frozen eval-honesty protocol (issue #478).

This is the claim-side counterpart of `bench/rescore.py`'s `assert_model_free`.
That guard sits where a number is *produced* and proves the grading was
model-free. This checker sits where a number is *stated*: it reads one eval
report (a JSON document) and decides whether the report obeys the protocol
before any of its numbers or claims reach a README, a site, or a commit message.

It reads nothing but the report. It does not re-run the compiler, read the
corpus, or trust any number. It decides only whether a report is *internally*
honest under the protocol:

  1. noSelfScore  — the grader is the compiler and is not the generator
                    (docs/eval-protocol.md section 3; design doc section 3).
  2. named gates  — every brief names a hard gate from the frozen set
                    (docs/eval-protocol.md section 4; design doc section 4).
  3. the ladder   — every claim stands no higher than the rung its own
                    evidence justifies (design doc section 5).

The frozen schema this checker validates against is EVAL-REPORT-1, specified in
docs/design/478-eval-honesty-protocol.md. Changing the accepted gate set or the
ladder is a new report-schema version, cut in a separate reviewed change.

A green result means "this report does not over-claim on its face." It does NOT
mean "these numbers are correct" — that is EVAL-1's re-score job — and it cannot
prove the written-down grader identity truly graded, or that a named review was
genuine. See design doc section 6 for what this checker can and cannot decide.

Usage:
  python3 tools/check_eval_report.py report.json
  python3 tools/check_eval_report.py report.json --json      # machine-readable
  cat report.json | python3 tools/check_eval_report.py -     # read from stdin

Exit status is 0 when the report is clean and 1 when it carries any violation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# The frozen report-schema version this checker enforces.
REPORT_SCHEMA = "EVAL-REPORT-1"

# The frozen set of named hard gates, mirroring docs/eval-protocol.md section 4.
# A brief must name exactly one of these.
HARD_GATES = frozenset({"compiles", "pinnedInterfaces", "noResidueForRawBaseline"})

# The revl compiler entrypoint that is the only valid grader (section 3 item 2).
GRADER_TOOL = "revl.compile_source"
GRADER_KIND = "compiler"

# The evidence ladder, lowest to highest (design doc section 5). The index is
# the rung's height; a claim's declared rung may not exceed the height its
# evidence justifies.
LADDER = ("claimed", "measured", "demonstrated", "supported")
RUNG_INDEX = {name: i for i, name in enumerate(LADDER)}


class ReportError(Exception):
    """The report is malformed — not a protocol violation but unusable input."""


def _generator_identity(generator: dict) -> set[str]:
    """The set of strings that name the generating party.

    A grader that names any of these is the generator grading itself.
    """
    ident = set()
    for key in ("model", "run", "driver", "tool", "name"):
        val = generator.get(key)
        if isinstance(val, str) and val.strip():
            ident.add(val.strip())
    return ident


def check_no_self_score(report: dict) -> list[str]:
    """noSelfScore at the report level (design doc section 3).

    The grader must be the revl compiler and must not be the generator.
    Returns a list of violation strings (empty when clean).
    """
    violations: list[str] = []
    grader = report.get("grader")
    generator = report.get("generator")
    if not isinstance(grader, dict):
        raise ReportError("report has no 'grader' object")
    if not isinstance(generator, dict):
        raise ReportError("report has no 'generator' object")

    kind = grader.get("kind")
    tool = grader.get("tool")
    if kind != GRADER_KIND:
        violations.append(
            f"noSelfScore: grader.kind must be {GRADER_KIND!r}, got {kind!r}")
    if tool != GRADER_TOOL:
        violations.append(
            f"noSelfScore: grader.tool must be {GRADER_TOOL!r} "
            f"(the revl compiler entrypoint), got {tool!r}")

    gen_ident = _generator_identity(generator)
    # Any grader field that names the generating party is self-scoring.
    for key in ("model", "run", "driver", "name"):
        val = grader.get(key)
        if isinstance(val, str) and val.strip() in gen_ident:
            violations.append(
                f"noSelfScore: grader.{key}={val!r} is the generator; "
                "the generator must never grade its own output")
    return violations


def check_named_gates(report: dict) -> list[str]:
    """Every brief names a hard gate from the frozen set (design doc section 4)."""
    violations: list[str] = []
    briefs = report.get("briefs")
    if not isinstance(briefs, list) or not briefs:
        raise ReportError("report has no non-empty 'briefs' list")

    for i, brief in enumerate(briefs):
        if not isinstance(brief, dict):
            raise ReportError(f"briefs[{i}] is not an object")
        where = brief.get("spec") or f"index {i}"
        gate = brief.get("hard_gate")
        if gate is None:
            violations.append(
                f"named gate: brief {where!r} names no hard_gate; a report may "
                "not say a brief passed without naming the gate it cleared")
        elif gate not in HARD_GATES:
            violations.append(
                f"named gate: brief {where!r} names hard_gate {gate!r}, which is "
                f"not in the frozen set {sorted(HARD_GATES)}")
        result = brief.get("result")
        if result not in ("pass", "fail"):
            violations.append(
                f"named gate: brief {where!r} has result {result!r}; a hard gate "
                "is pass or fail")
    return violations


def justified_rung(claim: dict, report_is_model_free: bool) -> int:
    """The highest ladder rung the claim's evidence justifies (design section 5).

    A claim declares a rung; this returns the height its evidence actually
    reaches. The caller refuses the claim when the declared rung is higher.
    """
    evidence = claim.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    generator = claim.get("_generator_identity", set())

    # measured: the re-score triple, and the report as a whole is model-free.
    has_triple = all(
        isinstance(evidence.get(k), str) and evidence[k].strip()
        for k in ("compiler_commit", "run", "protocol"))
    if not (has_triple and report_is_model_free):
        # Cannot even reach 'measured'; the floor is 'claimed'.
        return RUNG_INDEX["claimed"]

    # demonstrated: measured plus an independent reproduction.
    reproduced_by = evidence.get("reproduced_by")
    reproduced_ok = (
        isinstance(reproduced_by, str)
        and reproduced_by.strip()
        and reproduced_by.strip() not in generator)
    if not reproduced_ok:
        return RUNG_INDEX["measured"]

    # supported: demonstrated plus a promotion review by an independent reviewer.
    review = evidence.get("review")
    review_ok = (
        isinstance(review, dict)
        and isinstance(review.get("protocol"), str)
        and review.get("protocol").strip()
        and isinstance(review.get("reviewer"), str)
        and review.get("reviewer").strip()
        and review.get("reviewer").strip() not in generator)
    if not review_ok:
        return RUNG_INDEX["demonstrated"]

    return RUNG_INDEX["supported"]


def check_ladder(report: dict, report_is_model_free: bool) -> list[str]:
    """Every claim stands no higher than its evidence justifies (section 5)."""
    violations: list[str] = []
    claims = report.get("claims")
    if claims is None:
        # A report may carry only briefs and no public claims.
        return violations
    if not isinstance(claims, list):
        raise ReportError("'claims' must be a list when present")

    generator = _generator_identity(report.get("generator", {}))
    for i, claim in enumerate(claims):
        if not isinstance(claim, dict):
            raise ReportError(f"claims[{i}] is not an object")
        text = claim.get("text") or f"index {i}"
        rung = claim.get("rung")
        if rung not in RUNG_INDEX:
            violations.append(
                f"ladder: claim {text!r} declares rung {rung!r}, not one of "
                f"{list(LADDER)}")
            continue

        is_public = claim.get("public", True)
        # The floor rung asserts nothing measurable and may never be public.
        if rung == "claimed" and is_public:
            violations.append(
                f"ladder: claim {text!r} is at the 'claimed' floor and may not "
                "be public; a public claim needs at least 'measured' evidence")
            continue

        claim = {**claim, "_generator_identity": generator}
        justified = justified_rung(claim, report_is_model_free)
        declared = RUNG_INDEX[rung]
        if declared > justified:
            violations.append(
                f"ladder: claim {text!r} declares rung {rung!r} but its evidence "
                f"justifies only {LADDER[justified]!r}; a claim may not stand "
                "higher than its evidence")
    return violations


def check_report(report: dict) -> list[str]:
    """Run every check. Returns a flat list of violation strings (empty = clean).

    Raises ReportError on malformed input (a different failure than a violation).
    """
    if not isinstance(report, dict):
        raise ReportError("report is not a JSON object")

    protocol = report.get("protocol")
    if not isinstance(protocol, str) or not protocol.strip():
        raise ReportError("report names no 'protocol' version")
    schema = report.get("report_schema")
    if schema != REPORT_SCHEMA:
        raise ReportError(
            f"report_schema must be {REPORT_SCHEMA!r}, got {schema!r}")

    violations: list[str] = []
    self_score = check_no_self_score(report)
    violations += self_score
    violations += check_named_gates(report)
    # The ladder's 'measured' rung depends on the report being model-free; a
    # report that failed noSelfScore cannot justify any measured-or-higher claim.
    report_is_model_free = not self_score
    violations += check_ladder(report, report_is_model_free)
    return violations


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report", help="path to the eval report JSON, or '-' for stdin")
    ap.add_argument("--json", action="store_true",
                    help="print the verdict as JSON instead of text")
    args = ap.parse_args(argv)

    if args.report == "-":
        raw = sys.stdin.read()
        source = "<stdin>"
    else:
        path = Path(args.report)
        if not path.is_file():
            print(f"no such report: {path}", file=sys.stderr)
            return 2
        raw = path.read_text()
        source = str(path)

    try:
        report = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"{source}: not valid JSON: {exc}", file=sys.stderr)
        return 2

    try:
        violations = check_report(report)
    except ReportError as exc:
        print(f"{source}: malformed report: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"source": source, "ok": not violations,
                          "violations": violations}, indent=2))
    else:
        if not violations:
            print(f"{source}: OK — report obeys {REPORT_SCHEMA}; no over-claim.")
        else:
            print(f"{source}: {len(violations)} violation(s) under {REPORT_SCHEMA}:")
            for v in violations:
                print(f"  - {v}")
    return 0 if not violations else 1


if __name__ == "__main__":
    raise SystemExit(main())
