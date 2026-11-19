#!/usr/bin/env python3
"""Summarise a ``bench/agentdojo/run.py`` result directory (issue #2683).

    python3 bench/agentdojo/report.py --label pilot-banking

Reads ``records.jsonl`` and writes ``report.md`` beside it. The report counts
what AgentDojo itself counts (attack success and utility) and separates the
things the revl arm adds: admission refusals and the steps and tokens each
admitted turn cost. A refusal is reported, never scored as a success: a
component the profile refused never ran, and a rate computed as if it had would
be a different measurement.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

BENCH = Path(__file__).resolve().parent
RESULTS = BENCH.parents[1] / "bench" / "results" / "agentdojo"


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _pct(values: list[bool]) -> str:
    return f"{sum(values)}/{len(values)}" if values else "-"


def summarise(records: list[dict]) -> str:
    lines = ["# AgentDojo baseline vs admitted revl", ""]
    meta = records[0].get("_meta") if records else None
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in records:
        grouped[(row["arm"], row["model"])].append(row)

    lines.append("## Per arm and model")
    lines.append("")
    lines.append(
        "| arm | model | digest | benign utility | utility under attack | "
        "attack success | admission refusals | mean wall s | mean steps |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for (arm, model), rows in sorted(grouped.items()):
        benign = [r for r in rows if r["condition"] == "benign" and r.get("utility") is not None]
        attack = [r for r in rows if r["condition"] == "attack" and r.get("security") is not None]
        refusals = sum((r.get("stats") or {}).get("admission_refusals", 0) for r in rows)
        steps = [ (r.get("stats") or {}).get("steps") for r in rows ]
        steps = [s for s in steps if s is not None]
        mean_steps = _mean(steps)
        lines.append(
            f"| {arm} | `{model}` | `{(rows[0].get('model_digest') or 'unpinned')[:12]}` | "
            f"{_pct([bool(r['utility']) for r in benign])} | "
            f"{_pct([bool(r['utility']) for r in attack])} | "
            f"{_pct([bool(r['security']) for r in attack])} | "
            f"{refusals} | {_mean([r['duration_s'] for r in rows if r.get('duration_s')])} | "
            f"{mean_steps if mean_steps is not None else '-'} |"
        )

    lines.append("")
    lines.append("## Cost")
    lines.append("")
    lines.append("| arm | model | mean wall s | mean steps | mean prompt tokens | mean completion tokens |")
    lines.append("|---|---|---|---|---|---|")
    for (arm, model), rows in sorted(grouped.items()):
        steps = [ (r.get("stats") or {}).get("steps") for r in rows ]
        steps = [s for s in steps if s is not None]
        pt = [ (r.get("stats") or {}).get("prompt_tokens") for r in rows ]
        pt = [v for v in pt if v]
        ct = [ (r.get("stats") or {}).get("completion_tokens") for r in rows ]
        ct = [v for v in ct if v]
        lines.append(
            f"| {arm} | `{model}` | "
            f"{_mean([r['duration_s'] for r in rows if r.get('duration_s')])} | "
            f"{_mean(steps) if steps else '-'} | "
            f"{_mean(pt) if pt else '-'} | {_mean(ct) if ct else '-'} |"
        )
    if any(r["arm"] == "baseline" for r in records):
        lines.append("")
        lines.append(
            "The baseline arm runs AgentDojo's reference `OpenAILLM`, which does "
            "not expose usage, so its token columns are not reported. Wall time "
            "is the comparable figure across the two arms."
        )

    lines.append("")
    lines.append("## Per run (the run-to-run spread)")
    lines.append("")
    lines.append("| arm | model | run | benign utility | utility under attack | attack success | mean wall s |")
    lines.append("|---|---|---|---|---|---|---|")
    by_run: dict[tuple[str, str, int], list[dict]] = defaultdict(list)
    for row in records:
        by_run[(row["arm"], row["model"], row["run"])].append(row)
    for (arm, model, run), rows in sorted(by_run.items()):
        benign = [r for r in rows if r["condition"] == "benign" and r.get("utility") is not None]
        attack = [r for r in rows if r["condition"] == "attack" and r.get("security") is not None]
        lines.append(
            f"| {arm} | `{model}` | {run} | "
            f"{_pct([bool(r['utility']) for r in benign])} | "
            f"{_pct([bool(r['utility']) for r in attack])} | "
            f"{_pct([bool(r['security']) for r in attack])} | "
            f"{_mean([r['duration_s'] for r in rows if r.get('duration_s')])} |"
        )

    lines.append("")
    lines.append("## Attack success by injection task")
    lines.append("")
    lines.append("| arm | model | injection task | attack successes | records |")
    lines.append("|---|---|---|---|---|")
    by_inj: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in records:
        if row["condition"] == "attack" and row.get("security") is not None:
            by_inj[(row["arm"], row["model"], row.get("injection_task") or "-")].append(row)
    for (arm, model, inj), rows in sorted(by_inj.items()):
        lines.append(
            f"| {arm} | `{model}` | `{inj}` | {_pct([bool(r['security']) for r in rows])} | {len(rows)} |"
        )

    lines.append("")
    lines.append("## Refusals of the admitted arm")
    lines.append("")
    refused = [r for r in records if (r.get("stats") or {}).get("admission_refusals")]
    if not refused:
        lines.append("No admission refusals were recorded.")
    else:
        benign_refused = sum(1 for r in refused if r["condition"] == "benign")
        lines.append(
            f"Refused records: {len(refused)} total, {benign_refused} on benign tasks, "
            f"{len(refused) - benign_refused} on attacked tasks. A `None` code is the "
            "compiler's generic refusal for malformed source; it is not an admission-"
            "profile refusal."
        )
        lines.append("")
    for row in refused:
        stats = row.get("stats") or {}
        messages = stats.get("refusal_messages") or []
        profile = sum(
            1 for m in messages
            if "granted" in m or "forbids new" in m or "declares host code" in m
        )
        lines.append(
            f"- {row['model']} run{row['run']} `{row['task']}` {row['condition']}: "
            f"{stats.get('admission_refusals')} refusal(s), "
            f"{profile} about an ungranted reach or host code, "
            f"{len(messages) - profile} compile refusal(s); codes {stats.get('refusal_codes')}"
        )
        for message in messages:
            lines.append(f"  - `{message}`")

    errors = [r for r in records if r.get("error")]
    if errors:
        lines.append("")
        lines.append("## Errors")
        lines.append("")
        for row in errors[:20]:
            lines.append(
                f"- {row['arm']} {row['model']} `{row['task']}` {row['condition']}: {row['error']}"
            )
    if meta:
        lines.append("")
        lines.append("## Run")
        lines.append("")
        lines.append(f"```json\n{json.dumps(meta, indent=2)}\n```")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--label", required=True)
    args = parser.parse_args(argv)
    directory = RESULTS / args.label
    records = [
        json.loads(line)
        for line in (directory / "records.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    meta_path = directory / "meta.json"
    if meta_path.exists():
        for row in records:
            row["_meta"] = json.loads(meta_path.read_text(encoding="utf-8"))
    text = summarise(records)
    (directory / "report.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
