#!/usr/bin/env python3
"""Seeded-defect saga corpus and verdict runner (issue #2736, first slice).

The corpus is `defects.json`: one row per saga defect, each naming a small
saga in `sagas/` and the catch point the corpus expects. This file
measures the revl side of that claim:

- `compile`    the compiler refused the saga; the defect cannot reach a run.
               The refusal is reported as one of two kinds: `checker` (a
               named rule refused it) or `parse` (the parser's generic syntax
               error, which any typo would also earn).
- `fault-test` the saga compiled and the fault sweep failed at a seeded fault
- `missed`     the saga compiled and swept at least one step, all of which passed

It prints one line per row and exits non-zero when a row has no verdict, when
the actual verdict disagrees with the expected one, or when the data file is
malformed. A skip is never a pass: with no cordis-py runtime the sweep cannot
run and the command exits 2 instead of downgrading the runtime rows. A sweep
that ran zero steps, a crash of the sweep driver, and a row that does not
finish within `--timeout` seconds are runner errors (exit 1), never verdicts.
Each row is measured in its own child process so a hung row can be stopped.

Usage:
    python3 bench/saga_defects/run.py              # measure, print the table
    python3 bench/saga_defects/run.py --write      # also refresh
                                                   # bench/results/saga-defect-corpus.md,
                                                   # only when no row drifted
    python3 bench/saga_defects/run.py --timeout 60 # bound each row (seconds)
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
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

VERDICTS = ("compile", "fault-test", "missed")
REFUSALS = ("checker", "parse")
ROW_TIMEOUT = 300.0
# The child's exit code for a missing cordis-py; not 2, which argparse uses.
_CHILD_NO_RUNTIME = 3
GROUPS = ("project", "outside")
FIELDS = ("id", "group", "kind", "saga", "file", "description", "expected")
RESULTS = ROOT / "bench" / "results" / "saga-defect-corpus.md"


class CorpusError(Exception):
    """The data file or a row is unusable; never a verdict."""


class RuntimeMissing(Exception):
    """The cordis-py runtime is absent, so the sweep cannot run (exit 2)."""


# A problem line `revl.fault` writes when the sweep driver itself failed,
# rather than when a seeded fault exposed the saga (`fault._driver_failure`).
_DRIVER_FAILURE = re.compile(
    r"^the (fault-test driver raised |py emitter refused this document)")


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
        if row["expected"] == "compile" and not isinstance(row.get("expectedLine"), int):
            raise CorpusError(
                f"{row['id']}: a compile row must record its `expectedLine`")
        if "refusal" in row and (row["expected"] != "compile"
                                 or row["refusal"] not in REFUSALS):
            raise CorpusError(
                f"{row['id']}: `refusal` is one of {REFUSALS}, on compile rows only")
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


def refusal_kind(error) -> str:
    """`parse` when the refusal is the parser's generic syntax error, else `checker`.

    The generic shapes are the ones `revl.diagnostics.classify` itself maps to
    `SYNTAX` from the message; a refusal of that shape names no rule, so a typo
    would earn it too. A named rule that happens to run in the parser (the G4
    missing-undo refusal does) is still a checker refusal.
    """
    from revl.diagnostics import _PATTERNS

    message = str(error.message)
    for pattern, code, _category in _PATTERNS:
        if code == "SYNTAX" and pattern.search(message):
            return "parse"
    return "checker"


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
            "refusal": refusal_kind(refusal),
            "code": code,
            "line": refusal.line,
            "detail": " ".join(str(refusal.message).split()),
        }
    failed, counts, problems = _sweep(ir, row["component"])
    if not counts.get("steps"):
        raise CorpusError(
            f"{row['id']}: the sweep of {row['component']} ran zero steps, so "
            "there is no verdict (a runner error, not `missed`)")
    crashes = [problem for problem in problems
               if _DRIVER_FAILURE.match(" ".join(str(problem).split()))]
    if crashes:
        raise CorpusError(
            f"{row['id']}: the sweep driver failed, so there is no verdict (a "
            f"runner error, not `fault-test`): {' '.join(str(crashes[0]).split())}")
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
    expected_line = row.get("expectedLine")
    if expected_line is not None and outcome["verdict"] == "compile" \
            and outcome.get("line") != expected_line:
        problems.append(
            f"expected the refusal on line {expected_line}, got line {outcome.get('line')}")
    if outcome["verdict"] == "compile" and row["expected"] == "compile":
        expected_refusal = row.get("refusal", "checker")
        if outcome.get("refusal") != expected_refusal:
            problems.append(
                f"expected a {expected_refusal} refusal, got {outcome.get('refusal')}")
    return problems


def _row_command(row: dict) -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), "--measure-row", row["id"]]


def measure_bounded(row: dict, timeout: float = ROW_TIMEOUT,
                    command: list[str] | None = None) -> dict:
    """`measure(row)` in a child process that is stopped after *timeout* seconds.

    A row that does not finish in time is a runner error naming the row, never
    a verdict. *command* replaces the child's command line (tests use it).
    """
    argv = command if command is not None else _row_command(row)
    try:
        done = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, cwd=str(ROOT), check=False)
    except subprocess.TimeoutExpired as error:
        raise CorpusError(
            f"{row['id']}: timed out after {timeout:g}s and was stopped; "
            "no verdict") from error
    if done.returncode == _CHILD_NO_RUNTIME:
        raise RuntimeMissing(done.stderr.strip())
    if done.returncode != 0:
        tail = (done.stderr.strip().splitlines() or ["no output"])[-1]
        raise CorpusError(
            f"{row['id']}: the row's measurement exited {done.returncode}: {tail}")
    try:
        outcome = json.loads(done.stdout)
    except json.JSONDecodeError as error:
        raise CorpusError(
            f"{row['id']}: the row's measurement printed no outcome: {error}") from error
    if not isinstance(outcome, dict) or outcome.get("verdict") not in VERDICTS:
        raise CorpusError(f"{row['id']}: the row's measurement has no verdict")
    return outcome


# ------------------------------------------------------------------ output

def _cell(text: str) -> str:
    return " ".join(str(text).replace("|", "\\|").split())


def _evidence(outcome: dict) -> str:
    if outcome["verdict"] == "compile":
        code = outcome.get("code") or "no code"
        kind = outcome.get("refusal", "checker")
        return _cell(f"{kind} refusal, {code}: {outcome['detail']}")
    if outcome["verdict"] == "fault-test":
        return _cell(outcome["detail"])
    return _cell(outcome["detail"])


def tally(rows: list[dict], outcomes: dict[str, dict]) -> dict:
    """The totals the table, the summary and the papers quote, from the rows.

    `checker` and `parse` split `compile`; `*_kinds` count the distinct defect
    kinds in a bucket, so two rows of one kind are not read as two defects.
    `shared` lists each bucket's kinds that more than one row carries.
    """
    buckets: dict[str, list[dict]] = {
        "checker": [], "parse": [], "fault-test": [], "missed": []}
    for row in rows:
        outcome = outcomes[row["id"]]
        key = (outcome.get("refusal", "checker") if outcome["verdict"] == "compile"
               else outcome["verdict"])
        buckets[key].append(row)
    totals: dict = {"rows": len(rows)}
    shared: list[tuple[str, str, list[dict]]] = []
    for key, members in buckets.items():
        totals[key] = len(members)
        kinds: dict[str, list[dict]] = {}
        for row in members:
            kinds.setdefault(row["kind"], []).append(row)
        totals[f"{key}_kinds"] = len(kinds)
        shared += [(key, kind, same) for kind, same in kinds.items() if len(same) > 1]
    totals["compile"] = totals["checker"] + totals["parse"]
    totals["caught"] = totals["compile"] + totals["fault-test"]
    totals["shared"] = shared
    return totals


_BUCKET_NAMES = {
    "checker": "checker refusals",
    "parse": "parse refusals",
    "fault-test": "fault-test catches",
    "missed": "missed rows",
}


def _shared_sentences(totals: dict) -> list[str]:
    sentences = []
    for key, kind, same in totals["shared"]:
        names = [f"`{row['id']}`" for row in same]
        ids = ", ".join(names[:-1]) + " and " + names[-1]
        sagas = {row["saga"] for row in same}
        where = (f"at {len(same)} call sites of the `{sagas.pop()}` saga"
                 if len(sagas) == 1 else f"in {len(same)} sagas")
        sentences.append(
            f"{ids} are one defect kind (`{kind}`) seeded {where}, so the "
            f"{totals[key]} {_BUCKET_NAMES[key]} cover {totals[key + '_kinds']} "
            "defect kinds.")
    return sentences


def _summary(totals: dict) -> str:
    return (f"{totals['rows']} rows: {totals['compile']} refused at compile "
            f"({totals['checker']} by a checker rule, {totals['parse']} by a "
            f"parse error), {totals['fault-test']} caught by a fault test, "
            f"{totals['missed']} missed by both "
            f"({totals['caught']}/{totals['rows']} caught)")


def print_table(rows: list[dict], outcomes: dict[str, dict]) -> None:
    width = max(len(row["id"]) for row in rows)
    print(f"{'id':<{width}}  {'group':<7}  {'expected':<10}  "
          f"{'verdict':<10}  evidence")
    for row in rows:
        outcome = outcomes[row["id"]]
        print(f"{row['id']:<{width}}  {row['group']:<7}  {row['expected']:<10}  "
              f"{outcome['verdict']:<10}  {_evidence(outcome)}")
    totals = tally(rows, outcomes)
    print()
    print(_summary(totals))
    for sentence in _shared_sentences(totals):
        print(f"  {sentence}")
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


def _changed_after(rows: list[dict]) -> str:
    changed = [f"`{row['id']}`: {row['changedAfterMeasurement']}"
               for row in rows if row.get("changedAfterMeasurement")]
    return " ".join(changed) if changed else "none."


def render_results(rows: list[dict], outcomes: dict[str, dict], data: dict) -> str:
    totals = tally(rows, outcomes)
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
        "from the row's description before the saga was written, except for",
        "the rows named under \"Changed after measurement\" below, whose saga,",
        "description or expected verdict changed after a measurement. `compile`",
        "means the compiler refused the saga, either by a checker rule or by a",
        "parse error; a parse error is the parser's generic syntax refusal,",
        "which names no rule and which any typo would also earn, so it is",
        "counted apart. `fault-test` means the saga compiled and the fault sweep",
        "failed at a seeded fault; `missed` means it compiled and every sweep",
        "unit passed. The verdict is the catch point for the defect, not a",
        "statement that the saga is otherwise correct.",
        "",
        "Failure model: " + data.get("failureModel", ""),
        "",
        "Changed after measurement: " + _changed_after(rows),
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
        f"**{_summary(totals)}.**",
        "",
        "| catch point | rows | defect kinds |",
        "|---|---|---|",
        f"| refused at compile by a checker rule | {totals['checker']} | "
        f"{totals['checker_kinds']} |",
        f"| refused at compile by a parse error | {totals['parse']} | "
        f"{totals['parse_kinds']} |",
        f"| caught only by a fault test | {totals['fault-test']} | "
        f"{totals['fault-test_kinds']} |",
        f"| missed by both | {totals['missed']} | {totals['missed_kinds']} |",
        "",
    ]
    lines += _shared_sentences(totals)
    lines += [
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
        "  behind an opaque host body and is missed. `partial-refund` is caught",
        "  because its ledger keeps the charged amount in a Map, so half a refund",
        "  leaves the other half behind. A caught row is therefore optimistic for",
        "  an external service.",
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
                        help=f"refresh {_shown(RESULTS)} when no row drifted")
    parser.add_argument("--list", action="store_true",
                        help="list the corpus rows and measure nothing")
    parser.add_argument("--only", action="append", default=None, metavar="ID",
                        help="measure only the named defect (repeatable)")
    parser.add_argument("--timeout", type=float, default=ROW_TIMEOUT,
                        metavar="SECONDS",
                        help=f"stop a row that runs longer (default {ROW_TIMEOUT:g})")
    parser.add_argument("--measure-row", metavar="ID", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.measure_row:
        return _measure_row_child(args.measure_row)
    if args.timeout <= 0:
        print("saga-defect corpus: --timeout must be positive", file=sys.stderr)
        return 2

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
            outcomes[row["id"]] = measure_bounded(row, args.timeout)
    except CorpusError as error:
        print(f"saga-defect corpus: {error}", file=sys.stderr)
        return 1
    except RuntimeMissing:
        print(_RUNTIME_MISSING, file=sys.stderr)
        return 2

    print_table(rows, outcomes)

    drift: list[str] = []
    for row in rows:
        problems = disagreements(row, outcomes[row["id"]])
        if problems:
            drift.append(f"{row['id']}: " + "; ".join(problems))
    if drift:
        # A drifting run never replaces the pinned results: the table stays
        # the last measurement that agreed with the corpus.
        print("\nsaga-defect corpus: expected catch point drifted:", file=sys.stderr)
        for line in drift:
            print(f"  {line}", file=sys.stderr)
        if args.write:
            print(f"saga-defect corpus: {_shown(RESULTS)} was not written",
                  file=sys.stderr)
        return 1
    if args.write:
        RESULTS.parent.mkdir(parents=True, exist_ok=True)
        RESULTS.write_text(render_results(rows, outcomes, data), encoding="utf-8")
        print(f"\nwrote {_shown(RESULTS)}")
    return 0


_RUNTIME_MISSING = (
    "saga-defect corpus: the fault sweep needs the cordis-py runtime "
    "at the pin in backends/python/setup.sh; install it with "
    "`sh backends/python/setup.sh` (a skip is not a verdict)")


def _shown(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _measure_row_child(ident: str) -> int:
    """Child side of `measure_bounded`: one row's outcome as JSON on stdout."""
    try:
        outcome = measure(row_by_id(load_corpus(), ident))
    except CorpusError as error:
        print(f"saga-defect corpus: {error}", file=sys.stderr)
        return 1
    except ModuleNotFoundError as error:
        if error.name != "cordis" and "cordis" not in str(error):
            raise
        print(_RUNTIME_MISSING, file=sys.stderr)
        return _CHILD_NO_RUNTIME
    print(json.dumps(outcome, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
