#!/usr/bin/env python3
"""Seeded-defect saga corpus and verdict runner (issue #2736, first slice).

The corpus is `defects.json`: one row per saga defect, each naming a small
saga in `sagas/` and the catch point the corpus expects. This file
measures the revl side of that claim:

- `compile`    the checker refused the saga; the defect cannot reach a run
- `fault-test` the saga compiled and the fault sweep failed at a seeded fault
- `missed`     the saga compiled and every sweep unit passed

It prints one line per row and exits non-zero when a row has no verdict, when
the actual verdict disagrees with the expected one, or when the data file is
malformed. A skip is never a pass: with no cordis-py runtime the sweep cannot
run and the command exits 2 instead of downgrading the runtime rows.

Usage:
    python3 bench/saga_defects/run.py              # measure, print the table
    python3 bench/saga_defects/run.py --write      # also refresh
                                                   # bench/results/saga-defect-corpus.md
    python3 bench/saga_defects/run.py --list       # show the rows, measure nothing
    python3 bench/saga_defects/run.py --only ID    # one row, for debugging

The Temporal port and sagas written by people outside the checker's authors
are the second slice of issue #2736; they are not scored here, and the results
file says so.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

VERDICTS = ("compile", "fault-test", "missed")
GROUPS = ("project", "outside")
FIELDS = ("id", "group", "kind", "saga", "file", "description", "expected")
RESULTS = ROOT / "bench" / "results" / "saga-defect-corpus.md"


class CorpusError(Exception):
    """The data file or a row is unusable; never a verdict."""


# ------------------------------------------------------------------ loading

def load_corpus() -> dict:
    try:
        data = json.loads((HERE / "defects.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CorpusError(f"cannot read {HERE / 'defects.json'}: {error}") from error
    rows = data.get("defects")
    if not isinstance(rows, list) or not rows:
        raise CorpusError("defects.json needs a non-empty `defects` list")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise CorpusError(f"a defect row is not an object: {row!r}")
        missing = [field for field in FIELDS if field not in row]
        if missing:
            raise CorpusError(f"{row.get('id', '<unnamed>')}: missing {missing}")
        if row["id"] in seen:
            raise CorpusError(f"duplicate defect id {row['id']!r}")
        seen.add(row["id"])
        if row["group"] not in GROUPS:
            raise CorpusError(f"{row['id']}: group {row['group']!r} not in {GROUPS}")
        if row["expected"] not in VERDICTS:
            raise CorpusError(f"{row['id']}: expected {row['expected']!r} not in {VERDICTS}")
        if row["expected"] == "compile" and not row.get("expectedCode"):
            raise CorpusError(
                f"{row['id']}: a compile row must record its `expectedCode`")
        path = HERE / row["file"]
        if not path.is_file():
            raise CorpusError(f"{row['id']}: no saga at {path}")
        if row["expected"] != "compile" and not row.get("component"):
            raise CorpusError(
                f"{row['id']}: a runtime verdict needs the `component` to sweep")
    return data


def row_by_id(data: dict, ident: str) -> dict:
    for row in data["defects"]:
        if row["id"] == ident:
            return row
    raise CorpusError(f"no defect {ident!r} in defects.json")


# ----------------------------------------------------------------- outcomes

def _compile(path: Path):
    """Return (ir, None) when the saga compiles, (None, RevlError) when refused."""
    from revl.compiler import compile_files
    from revl.errors import RevlError

    try:
        return compile_files([str(path)]), None
    except RevlError as error:
        return None, error


def _sweep(ir: dict, component: str):
    from revl.fault import run_sweep

    names = {one.get("name") for one in ir.get("components") or []}
    if component not in names:
        raise CorpusError(
            f"component {component!r} is not in the compiled saga; "
            f"the sagas define {sorted(name for name in names if name)}")
    failed, dossier = run_sweep(ir, out=lambda _line: None, only=component)
    problems = [problem
                for section in dossier["components"]
                for step in section["steps"]
                for problem in step["problems"]]
    return failed, dossier["counts"], problems


def measure(row: dict) -> dict:
    """One row's verdict: compile refusal, a sweep failure, or neither."""
    path = HERE / row["file"]
    ir, refusal = _compile(path)
    if refusal is None and row["expected"] == "compile":
        raise CorpusError(
            f"{row['id']}: expected a compile refusal, but the saga compiled; "
            "the corpus expected catch point has drifted")
    if refusal is not None:
        code = refusal.code
        if code is None:
            # The checker sets some codes during diagnostic rendering; classify
            # here so an in-process run sees the same code the CLI prints.
            from revl.diagnostics import classify

            code = classify(refusal)["code"]
        return {
            "verdict": "compile",
            "code": code,
            "line": refusal.line,
            "detail": " ".join(str(refusal.message).split()),
        }
    failed, counts, problems = _sweep(ir, row["component"])
    if failed or problems:
        return {
            "verdict": "fault-test",
            "detail": " ".join(str(problems[0]).split()) if problems else
                      f"{failed} sweep unit(s) failed",
            "failures": failed,
            "counts": counts,
        }
    return {
        "verdict": "missed",
        "detail": (f"compiled; every sweep unit for {row['component']} passed "
                   f"({counts['steps']} step(s))"),
        "counts": counts,
    }


def disagreements(row: dict, outcome: dict) -> list[str]:
    """The corpus is ground truth: report drift instead of smoothing it over."""
    problems: list[str] = []
    if outcome["verdict"] != row["expected"]:
        problems.append(
            f"expected {row['expected']}, measured {outcome['verdict']}")
    expected_code = row.get("expectedCode")
    if expected_code and outcome.get("code") != expected_code:
        problems.append(
            f"expected code {expected_code}, got {outcome.get('code')}")
    return problems


# ------------------------------------------------------------------ output

def _cell(text: str) -> str:
    return " ".join(str(text).replace("|", "\\|").split())


def _evidence(outcome: dict) -> str:
    if outcome["verdict"] == "compile":
        code = outcome.get("code") or "no code"
        return _cell(f"{code}: {outcome['detail']}")
    if outcome["verdict"] == "fault-test":
        return _cell(outcome["detail"])
    return _cell(outcome["detail"])


def print_table(rows: list[dict], outcomes: dict[str, dict]) -> None:
    width = max(len(row["id"]) for row in rows)
    print(f"{'id':<{width}}  {'group':<7}  {'expected':<10}  "
          f"{'verdict':<10}  evidence")
    for row in rows:
        outcome = outcomes[row["id"]]
        print(f"{row['id']:<{width}}  {row['group']:<7}  {row['expected']:<10}  "
              f"{outcome['verdict']:<10}  {_evidence(outcome)}")
    counts = {verdict: 0 for verdict in VERDICTS}
    for outcome in outcomes.values():
        counts[outcome["verdict"]] += 1
    total = len(rows)
    caught = counts["compile"] + counts["fault-test"]
    print()
    print(f"{total} row(s): {counts['compile']} refused at compile, "
          f"{counts['fault-test']} caught by a fault test, "
          f"{counts['missed']} missed by both "
          f"({caught}/{total} caught)")
    groups: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = groups.setdefault(row["group"], {v: 0 for v in VERDICTS})
        bucket[outcomes[row["id"]]["verdict"]] += 1
    for group, bucket in sorted(groups.items()):
        print(f"  {group}: {bucket['compile']} refused, "
              f"{bucket['fault-test']} fault-test, {bucket['missed']} missed")


def _revl_version() -> str:
    try:
        from revl._version import version

        return str(version())
    except Exception:  # noqa: BLE001 - a missing version must not hide the run
        return "unknown"


def _cordis_pin() -> str:
    setup = ROOT / "backends" / "python" / "setup.sh"
    try:
        text = setup.read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r"CORDIS_PY_PIN:-([0-9a-f]{40})", text)
    return match.group(1) if match else "unknown"


def render_results(rows: list[dict], outcomes: dict[str, dict], data: dict) -> str:
    counts = {verdict: 0 for verdict in VERDICTS}
    for outcome in outcomes.values():
        counts[outcome["verdict"]] += 1
    total = len(rows)
    caught = counts["compile"] + counts["fault-test"]
    pin = _cordis_pin()
    lines = [
        "# Seeded-defect saga corpus (issue #2736)",
        "",
        "Generated by `python3 bench/saga_defects/run.py --write`; the table is",
        "measured, never hand-edited. Do not edit it: rerun the command.",
        "",
        f"revl {_revl_version()}; cordis-py @ {pin[:12]}; "
        f"{platform.platform()}.",
        "",
        "Each saga is a small composition with one known saga defect, seeded",
        "from the row's description before the saga was written. `compile`",
        "means the checker refused the saga; `fault-test`",
        "means the saga compiled and the fault sweep failed at a seeded fault;",
        "`missed` means it compiled and every sweep unit passed. The verdict is",
        "the catch point for the defect, not a statement that the saga is",
        "otherwise correct.",
        "",
        "Failure model: " + data.get("failureModel", ""),
        "",
        "| defect | group | kind | expected | verdict | evidence |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        outcome = outcomes[row["id"]]
        lines.append(
            f"| `{row['id']}` | {row['group']} | {row['kind']} | "
            f"{row['expected']} | **{outcome['verdict']}** | {_evidence(outcome)} |")
    lines += [
        "",
        f"**{total} rows: {counts['compile']} refused at compile, "
        f"{counts['fault-test']} caught by a fault test, "
        f"{counts['missed']} missed by both ({caught}/{total} caught).**",
        "",
        "| catch point | rows |",
        "|---|---|",
        f"| refused at compile time | {counts['compile']} |",
        f"| caught only by a fault test | {counts['fault-test']} |",
        f"| missed by both | {counts['missed']} |",
        "",
        "## What the numbers are not",
        "",
        "- The sagas are the checker authors' own. Same-author bias is not",
        "  removed, so the rows cannot be read as an independent measure.",
        "  `defects.json` has an `outside` group for sagas the checker's",
        "  authors did not write; it is empty in this slice.",
        "- The defects are seeded, not found in real code. The table measures",
        "  which seeded defect each mechanism catches, not how often real code",
        "  contains one.",
        "- The Temporal TypeScript port and its score are the second slice of",
        "  issue #2736. The `temporal` column is absent rather than guessed.",
        "- The fault sweep is sequential, single-fault and top-level only",
        "  (`docs/fault-tests.md` sections 9.3 and 9.5). A missed row here does",
        "  not say whether a deeper or concurrent failure would expose it.",
        "- Provider state is visible to the sweep only where the provider is a",
        "  revl `Map`. `wrong-release-sku` is caught because its provider keeps the",
        "  sku in a Map; `wrong-refund-order` is the same kind of wrong target",
        "  behind an opaque host body and is missed. A caught row is therefore",
        "  optimistic for an external service.",
        "",
        "## Reproduce",
        "",
        "```sh",
        "# needs cordis-py at the pin in backends/python/setup.sh",
        "python3 bench/saga_defects/run.py",
        "python3 bench/saga_defects/run.py --write",
        "```",
        "",
    ]
    return "\n".join(lines)


# -------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure the seeded-defect saga corpus (issue #2736).")
    parser.add_argument("--write", action="store_true",
                        help=f"refresh {RESULTS.relative_to(ROOT)}")
    parser.add_argument("--list", action="store_true",
                        help="list the corpus rows and measure nothing")
    parser.add_argument("--only", action="append", default=None, metavar="ID",
                        help="measure only the named defect (repeatable)")
    args = parser.parse_args(argv)

    try:
        data = load_corpus()
        rows = data["defects"]
        if args.only:
            wanted = set(args.only)
            unknown = wanted - {row["id"] for row in rows}
            if unknown:
                raise CorpusError(f"no defect {sorted(unknown)}")
            rows = [row for row in rows if row["id"] in wanted]
    except CorpusError as error:
        print(f"saga-defect corpus: {error}", file=sys.stderr)
        return 1

    if args.write and args.only:
        print("saga-defect corpus: --write needs the whole corpus, not --only",
              file=sys.stderr)
        return 2

    if args.list:
        for row in rows:
            print(f"{row['id']:<28} {row['group']:<7} {row['expected']:<10} "
                  f"{row['file']}")
        return 0

    outcomes: dict[str, dict] = {}
    try:
        for row in rows:
            outcomes[row["id"]] = measure(row)
    except CorpusError as error:
        print(f"saga-defect corpus: {error}", file=sys.stderr)
        return 1
    except ModuleNotFoundError as error:
        if error.name != "cordis" and "cordis" not in str(error):
            raise
        print(
            "saga-defect corpus: the fault sweep needs the cordis-py runtime "
            "at the pin in backends/python/setup.sh; install it with "
            "`sh backends/python/setup.sh` (a skip is not a verdict)",
            file=sys.stderr)
        return 2

    print_table(rows, outcomes)

    drift: list[str] = []
    for row in rows:
        problems = disagreements(row, outcomes[row["id"]])
        if problems:
            drift.append(f"{row['id']}: " + "; ".join(problems))
    if args.write:
        RESULTS.parent.mkdir(parents=True, exist_ok=True)
        RESULTS.write_text(render_results(rows, outcomes, data), encoding="utf-8")
        print(f"\nwrote {RESULTS.relative_to(ROOT)}")

    if drift:
        print("\nsaga-defect corpus: expected catch point drifted:", file=sys.stderr)
        for line in drift:
            print(f"  {line}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
