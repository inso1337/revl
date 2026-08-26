"""Render a :class:`~tck.runner.Report` as text or JSON.

The text form is the conformance report a "tier seven" PR would paste: one line
per requirement, its cases, and the overall verdict. The JSON form is for CI
gates.
"""

from __future__ import annotations

import json

from .runner import CaseResult, Outcome, Report

_MARK = {
    Outcome.PASS: "PASS",
    Outcome.DIVERGENCE: "DIV ",
    Outcome.PENDING: "PEND",
    Outcome.FAIL: "FAIL",
}


def _line(r: CaseResult) -> str:
    head = f"  [{_MARK[r.outcome]}] {r.case.requirement:<3} {r.case.id}"
    return f"{head}\n         {r.detail}"


def to_text(report: Report) -> str:
    lines: list[str] = []
    lines.append("revl runtime TCK — conformance report")
    lines.append(f"runtime : {report.adapter_name} ({report.runtime_version})")
    counts = report.counts()
    lines.append(
        f"summary : {counts['pass']} pass, {counts['divergence']} pinned "
        f"divergence, {counts['pending']} pending, {counts['fail']} fail")
    lines.append(f"verdict : {'OK' if report.ok else 'FAILED'} "
                 f"(pending is not pass; a stale pin fails)")
    lines.append("")
    for req, results in report.by_requirement().items():
        for r in results:
            lines.append(_line(r))
    # spell out the pinned divergences so the report is self-explaining
    divs = [r for r in report.results if r.outcome == Outcome.DIVERGENCE]
    if divs:
        lines.append("")
        lines.append("pinned divergences (expected, recorded):")
        for r in divs:
            lines.append(f"  * {r.case.requirement} {r.case.id}: "
                         f"{r.divergence.label}")
            if r.divergence.note:
                lines.append(f"      {r.divergence.note}")
    fails = [r for r in report.results if r.outcome == Outcome.FAIL]
    if fails:
        lines.append("")
        lines.append("failures:")
        for r in fails:
            lines.append(f"  * {r.case.requirement} {r.case.id}: {r.detail}")
    return "\n".join(lines)


def to_json(report: Report) -> dict:
    return {
        "runtime": report.adapter_name,
        "runtime_version": report.runtime_version,
        "ok": report.ok,
        "counts": report.counts(),
        "cases": [
            {
                "id": r.case.id,
                "requirement": r.case.requirement,
                "kind": r.case.kind,
                "outcome": r.outcome.value,
                "detail": r.detail,
                "divergence": (r.divergence.label if r.divergence else None),
            }
            for r in report.results
        ],
    }


def to_json_str(report: Report) -> str:
    return json.dumps(to_json(report), indent=2)
