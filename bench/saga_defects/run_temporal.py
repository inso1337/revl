#!/usr/bin/env python3
"""The Temporal side of the seeded-defect saga corpus (issue #2736, second slice).

`defects.json` holds, for every row, what the Temporal TypeScript version of
that saga looks like as data: a list of text edits to the clean port in
`temporal/src/workflows.ts` and `temporal/src/activities.ts`, or the reason the
defect has no counterpart there. This file derives each port from its edits,
type-checks it with `tsc`, runs it on a real Temporal server once with no fault
and once with a failure injected after each step, judges what the fake world
and the activity journal show, and prints the verdict per row:

- `compile`         `tsc` refused the port
- `fault-test`      the port compiled and an assertion failed at a seeded fault
- `missed`          the port compiled and every assertion held at every step
- `no-counterpart`  no Temporal program is the same saga with this defect
                    (not measured; the data gives the reason)
- `clean`           a control with no seeded change passed every assertion

The assertions are the three that revl's py-tier sweep makes and that apply to
one workflow (docs/fault-tests.md section 9): the saga failed; the world is back
to what it was and no compensation raised; the compensations ran newest first.
`siblings unaffected` has no counterpart here. They are analogues, not the same
code: see "What the numbers are not" in the results file for where they differ,
chiefly that the harness holds no list of what a port registered, so it cannot
check that a compensation was owed.

A runner problem is never a verdict. A missing server, a missing install, a
clean run that fails, a port with no failure points, a run that fails with
anything but the injected fault (a timeout, a worker or server fault, a
non-determinism error), a server that does not answer within `--timeout`, a
crash of the harness and an edit that does not apply exactly as often as it
says are errors (exit 1 or 2), so a broken toolchain cannot read as `missed` or
`fault-test`.

Usage:
    REVL_TEMPORAL_DEV_SERVER=127.0.0.1:7233 python3 bench/saga_defects/run_temporal.py
    python3 bench/saga_defects/run_temporal.py --address 127.0.0.1:7233 --write
    python3 bench/saga_defects/run_temporal.py --list
    python3 bench/saga_defects/run_temporal.py --emit DIR   # the derived ports, no server
    python3 bench/saga_defects/run_temporal.py --only ID --address HOST:PORT
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PROJECT = HERE / "temporal"
GENERATED = PROJECT / ".generated"
RESULTS = ROOT / "bench" / "results" / "saga-defect-corpus-temporal.md"
REVL_RESULTS = ROOT / "bench" / "results" / "saga-defect-corpus.md"
ADDRESS_ENV = "REVL_TEMPORAL_DEV_SERVER"
ROW_TIMEOUT = 300.0
SOURCES = ("workflows.ts", "activities.ts", "state.ts")

VERDICTS = ("compile", "fault-test", "missed", "no-counterpart", "clean")
MEASURED = ("compile", "fault-test", "missed")

# A compensation activity and the forward activity it undoes. The order check
# reads only these names, so it judges the order of the compensations that
# ran, never whether a saga should have had more of them: the harness holds no
# list of what a port registered, so "owed" is not checked.
PAIRS = {
    "release": "reserve",
    "refund": "charge",
    "reverse": "settle",
    "lockRelease": "lockAcquire",
    "logDelete": "logInsert",
}
# What counts as residue after a failed saga: reversible resources only.
RESIDUE_STORES = ("inventory", "payments", "locks")


class CorpusError(Exception):
    """The data, the toolchain or a run is unusable; never a verdict."""


class ToolchainMissing(Exception):
    """No server address, no node or no install: the runner cannot run (exit 2)."""


# ------------------------------------------------------------------ loading

def _load_revl_runner():
    spec = importlib.util.spec_from_file_location(
        "saga_defects_revl_run", HERE / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_units() -> tuple[dict, list[dict]]:
    """The corpus and every Temporal unit: corpus rows, the Temporal-only rows
    and the controls, each with the shape `derive` and `measure` read."""
    data = _load_revl_runner().load_corpus()
    units: list[dict] = []
    for row in data["defects"]:
        spec = row.get("temporal")
        if not isinstance(spec, dict):
            raise CorpusError(f"{row['id']}: no `temporal` entry; every row needs one")
        units.append(_unit(row["id"], "row", row["kind"], row["description"], spec))
    for row in data.get("temporalOnly", []):
        units.append(_unit(row["id"], "temporal-only", row["kind"],
                           row["description"], row["temporal"]))
    for control in data.get("temporalControls", []):
        units.append(_unit(control["id"], "control", "control", control["description"],
                           {"expected": "clean", "workflow": control["workflow"],
                            "edits": control.get("edits", [])}))
    ids = [unit["id"] for unit in units]
    if len(ids) != len(set(ids)):
        raise CorpusError("a Temporal unit id repeats a defect id")
    return data, units


def _unit(ident: str, source: str, kind: str, description: str, spec: dict) -> dict:
    expected = spec.get("expected")
    if expected not in VERDICTS:
        raise CorpusError(f"{ident}: temporal expected {expected!r} not in {VERDICTS}")
    if expected == "no-counterpart":
        if not str(spec.get("reason", "")).strip():
            raise CorpusError(f"{ident}: a no-counterpart row must say why")
    else:
        if not spec.get("workflow"):
            raise CorpusError(f"{ident}: a measured unit needs its `workflow`")
        edits = spec.get("edits")
        if not isinstance(edits, list):
            raise CorpusError(f"{ident}: `edits` must be a list")
        if source != "control" and not edits:
            raise CorpusError(f"{ident}: a seeded row needs at least one edit")
        for item in edits:
            if item.get("file") not in SOURCES[:2] or "find" not in item or "replace" not in item:
                raise CorpusError(f"{ident}: an edit needs `file`, `find` and `replace`")
    return {"id": ident, "source": source, "kind": kind, "description": description,
            "expected": expected, "workflow": spec.get("workflow"),
            "edits": spec.get("edits", []), "summary": spec.get("summary", ""),
            "reason": spec.get("reason", "")}


# ---------------------------------------------------------------- derivation

def derive(unit: dict, dest: Path) -> None:
    """Write the port for *unit* under *dest*: the clean sources with its edits
    applied. Each edit must apply exactly `count` times (default once), so a
    drifted clean port fails here instead of quietly seeding nothing."""
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "src").mkdir(parents=True)
    for name in SOURCES:
        shutil.copyfile(PROJECT / "src" / name, dest / "src" / name)
    shutil.copyfile(PROJECT / "tsconfig.json", dest / "tsconfig.json")
    for item in unit["edits"]:
        target = dest / "src" / item["file"]
        text = target.read_text(encoding="utf-8")
        want = int(item.get("count", 1))
        found = text.count(item["find"])
        if found != want:
            raise CorpusError(
                f"{unit['id']}: the edit {item['find'][:48]!r} in {item['file']} "
                f"matches {found} time(s), not {want}; the clean port and the "
                "data have drifted")
        target.write_text(text.replace(item["find"], item["replace"]), encoding="utf-8")


# ------------------------------------------------------------------- judging

def judge(outcome: dict) -> list[str]:
    """The assertions, over one harness outcome; empty means every one held.

    Pure: it reads the JSON the harness printed and nothing else, so it is
    tested with fabricated outcomes and needs no server.
    """
    problems: list[str] = []
    for run in outcome["runs"]:
        step = run["step"]
        world = run["world"]
        journal = world["journal"]
        if not run["failed"]:
            problems.append(f"after step {step}: the saga did not fail; the injected "
                            "fault was swallowed")
            continue
        for store in RESIDUE_STORES:
            if world[store]:
                problems.append(f"after step {step}: residue in {store}: {_held(world[store])}")
        for call in journal:
            if call["name"] in PAIRS and not call["ok"]:
                problems.append(f"after step {step}: the compensation `{call['name']}` raised")
        order = _order_problem(journal)
        if order:
            problems.append(f"after step {step}: {order}")
    return problems


def _held(entries: list) -> str:
    """A store's contents: `key=value` for a map entry, the value for a set member."""
    return ", ".join(f"{entry[0]}={entry[1]}" if isinstance(entry, list) else str(entry)
                     for entry in entries)


def _order_problem(journal: list[dict]) -> str | None:
    """Compensations must run newest first among the forward steps that
    committed. Decidable only when each compensation runs at most once; a
    repeated one cannot be placed against its forward step, so no verdict."""
    ran = [call["name"] for call in journal if call["name"] in PAIRS]
    if len(set(ran)) != len(ran):
        return None
    forward = [call["name"] for call in journal
               if call["name"] in PAIRS.values() and call["ok"]]
    expected = [comp for step in reversed(forward) for comp, partner in PAIRS.items()
                if partner == step and comp in ran]
    if ran != expected:
        return (f"the compensations ran in the order {', '.join(ran)}; "
                f"newest first is {', '.join(expected)}")
    return None


def classify(outcome: dict) -> dict:
    """A port that compiled and ran: `fault-test` or `missed`, with evidence."""
    if not outcome["steps"]:
        raise CorpusError("the port has no failure points, so there is nothing to sweep")
    if outcome["control"]["failed"]:
        raise CorpusError(
            "the port fails with no fault injected "
            f"({outcome['control']['message']}); it is not a working saga")
    for run in outcome["runs"]:
        injected = f"injected fault after step {run['step']}"
        if run["failed"] and injected not in str(run["message"]):
            raise CorpusError(
                f"after step {run['step']} the saga failed with {run['message']!r}, not the "
                "injected fault; a timeout, a worker or server fault or a workflow error is "
                "not a verdict")
    problems = judge(outcome)
    if problems:
        return {"verdict": "fault-test", "detail": problems[0],
                "steps": outcome["steps"], "problems": len(problems)}
    return {"verdict": "missed", "steps": outcome["steps"],
            "detail": f"compiled; every assertion held after each of {outcome['steps']} step(s)"}


# ----------------------------------------------------------------- measuring

def _node() -> str:
    node = shutil.which("node")
    if node is None:
        raise ToolchainMissing("node is not on PATH")
    return node


def _tsc() -> Path:
    tsc = PROJECT / "node_modules" / ".bin" / "tsc"
    if not tsc.exists():
        raise ToolchainMissing(
            f"the Temporal SDK is not installed; run `npm ci` in {PROJECT.relative_to(ROOT)}")
    return tsc


def compile_port(dest: Path) -> str | None:
    """`tsc` over the derived port: None when it type-checks, else its first error."""
    done = subprocess.run([str(_tsc()), "-p", str(dest / "tsconfig.json")],
                          capture_output=True, text=True, check=False, cwd=str(dest))
    if done.returncode == 0:
        return None
    lines = [line.strip() for line in (done.stdout + done.stderr).splitlines() if line.strip()]
    return lines[0] if lines else f"tsc exited {done.returncode}"


def run_harness(dest: Path, workflow: str, address: str, timeout: float) -> dict:
    argv = [_node(), str(PROJECT / "harness.js"), address, str(dest), workflow]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              check=False, cwd=str(PROJECT))
    except subprocess.TimeoutExpired as error:
        raise CorpusError(f"timed out after {timeout:g}s and was stopped; no verdict") from error
    if done.returncode != 0:
        tail = (done.stderr.strip().splitlines() or ["no output"])[-1]
        raise CorpusError(f"the harness exited {done.returncode}: {tail}")
    _, marker, payload = done.stdout.partition("@@RESULT@@")
    if not marker:
        raise CorpusError("the harness printed no result")
    try:
        return json.loads(payload)
    except json.JSONDecodeError as error:
        raise CorpusError(f"the harness result is not JSON: {error}") from error


def measure(unit: dict, address: str, timeout: float = ROW_TIMEOUT) -> dict:
    if unit["expected"] == "no-counterpart":
        return {"verdict": "no-counterpart", "detail": unit["reason"]}
    dest = GENERATED / unit["id"]
    try:
        derive(unit, dest)
        error = compile_port(dest)
        if error is not None:
            return {"verdict": "compile", "detail": f"tsc: {error}"}
        outcome = classify(run_harness(dest, unit["workflow"], address, timeout))
    except CorpusError as failure:
        raise CorpusError(f"{unit['id']}: {failure}") from failure
    if unit["source"] == "control":
        if outcome["verdict"] != "missed":
            raise CorpusError(
                f"{unit['id']}: the clean control failed an assertion: {outcome['detail']}")
        return {"verdict": "clean", "steps": outcome["steps"],
                "detail": f"every assertion held after each of {outcome['steps']} step(s)"}
    return outcome


def disagreements(unit: dict, outcome: dict) -> list[str]:
    if outcome["verdict"] != unit["expected"]:
        return [f"expected {unit['expected']}, measured {outcome['verdict']}"]
    return []


# -------------------------------------------------------------------- output

def revl_verdicts() -> dict[str, str]:
    """The revl verdict per row from the committed revl results table."""
    try:
        text = REVL_RESULTS.read_text(encoding="utf-8")
    except OSError as error:
        raise CorpusError(f"cannot read {REVL_RESULTS}: {error}") from error
    found = {}
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("| `") and len(cells) >= 6:
            found[cells[0].strip("`")] = cells[4].strip("*")
    return found


def _cell(text: str) -> str:
    return " ".join(str(text).replace("|", "\\|").split())


def tally(units: list[dict], outcomes: dict[str, dict]) -> dict:
    """Totals over the corpus rows only; controls and the Temporal-only row
    are reported on their own lines."""
    counts = {verdict: 0 for verdict in VERDICTS}
    for unit in units:
        if unit["source"] == "row":
            counts[outcomes[unit["id"]]["verdict"]] += 1
    rows = sum(counts.values())
    ported = rows - counts["no-counterpart"]
    return {"rows": rows, "ported": ported, **counts,
            "caught": counts["compile"] + counts["fault-test"]}


def _summary(totals: dict) -> str:
    return (f"{totals['rows']} corpus rows: {totals['ported']} have a Temporal counterpart "
            f"({totals['compile']} refused by tsc, {totals['fault-test']} caught by a fault "
            f"test, {totals['missed']} missed) and {totals['no-counterpart']} have none")


def _comparison(units: list[dict], outcomes: dict[str, dict], revl: dict[str, str]) -> dict:
    """On the rows with a counterpart on both sides, how each side scored."""
    both = [unit for unit in units
            if unit["source"] == "row" and outcomes[unit["id"]]["verdict"] != "no-counterpart"]
    def side(pick) -> dict:
        counts = {"compile": 0, "fault-test": 0, "missed": 0}
        for unit in both:
            counts[pick(unit)] += 1
        counts["caught"] = counts["compile"] + counts["fault-test"]
        return counts
    return {"n": len(both),
            "revl": side(lambda unit: revl[unit["id"]]),
            "temporal": side(lambda unit: outcomes[unit["id"]]["verdict"])}


def print_table(units: list[dict], outcomes: dict[str, dict]) -> None:
    width = max(len(unit["id"]) for unit in units)
    print(f"{'id':<{width}}  {'source':<13}  {'expected':<14}  {'verdict':<14}  evidence")
    for unit in units:
        outcome = outcomes[unit["id"]]
        print(f"{unit['id']:<{width}}  {unit['source']:<13}  {unit['expected']:<14}  "
              f"{outcome['verdict']:<14}  {_cell(outcome['detail'])[:110]}")
    print()
    print(_summary(tally(units, outcomes)))


def _versions() -> str:
    def one(argv: list[str]) -> str:
        try:
            return subprocess.run(argv, capture_output=True, text=True,
                                  check=False).stdout.strip() or "unknown"
        except OSError:
            return "unknown"
    sdk = "unknown"
    try:
        package = json.loads((PROJECT / "node_modules" / "@temporalio" / "worker"
                              / "package.json").read_text(encoding="utf-8"))
        sdk = package["version"]
    except (OSError, KeyError, json.JSONDecodeError):
        pass
    return f"node {one(['node', '--version'])}; @temporalio/worker {sdk}"


def render_results(units: list[dict], outcomes: dict[str, dict], data: dict,
                   revl: dict[str, str], server: str) -> str:
    totals = tally(units, outcomes)
    compare = _comparison(units, outcomes, revl)
    lines = [
        "# Seeded-defect saga corpus: the Temporal TypeScript port (issue #2736)",
        "",
        "Generated by `python3 bench/saga_defects/run_temporal.py --write`; the table is",
        "measured, never hand-edited. Do not edit it: rerun the command. The revl column",
        "is read from `saga-defect-corpus.md`, measured by `run.py`.",
        "",
        f"{_versions()}; Temporal server: {server}.",
        "",
        "Each row of the corpus is an analogous saga written as a Temporal TypeScript",
        "workflow with the row's seeded defect, not the same program. The clean ports are",
        "`bench/saga_defects/temporal/src/workflows.ts` (the `checkout` saga the corpus",
        "seeds, and `examples/migrator.rvl`); each row is that file with the text edits",
        "recorded under the row's `temporal` key in `defects.json`, applied by the runner.",
        "A row that has no Temporal counterpart says why instead of being scored.",
        "",
        "The ports are scored on a real Temporal dev server. The runner runs each port once",
        "with no fault, to count its failure points, and once with a failure injected after",
        "each step. The assertions are the three that revl's py-tier sweep makes and that",
        "apply to one workflow: the saga failed; the fake world (stock, payment",
        "authorization, advisory lock) is empty again and no compensation raised; the",
        "compensations ran newest first. `compile` means `tsc` refused the port;",
        "`fault-test` means an assertion failed at a seeded fault; `missed` means every",
        "assertion held at every step.",
        "",
        "Failure model: " + data.get("failureModel", ""),
        "",
        "| defect | kind | revl | Temporal expected | Temporal | evidence (Temporal) |",
        "|---|---|---|---|---|---|",
    ]
    for unit in units:
        if unit["source"] != "row":
            continue
        outcome = outcomes[unit["id"]]
        lines.append(
            f"| `{unit['id']}` | {unit['kind']} | {revl[unit['id']]} | {unit['expected']} | "
            f"**{outcome['verdict']}** | {_cell(outcome['detail'])} |")
    lines += ["", f"**{_summary(totals)}.**", ""]
    lines += [
        f"On the {compare['n']} rows that exist on both sides, revl refused "
        f"{compare['revl']['compile']} at compile time, caught {compare['revl']['fault-test']} "
        f"by a fault test and missed {compare['revl']['missed']} "
        f"({compare['revl']['caught']}/{compare['n']} caught); the Temporal ports were refused by "
        f"`tsc` {compare['temporal']['compile']} times, caught {compare['temporal']['fault-test']} by "
        f"a fault test and missed {compare['temporal']['missed']} "
        f"({compare['temporal']['caught']}/{compare['n']} caught).",
        "",
        "## Rows with no Temporal counterpart",
        "",
    ]
    for unit in units:
        if unit["source"] == "row" and outcomes[unit["id"]]["verdict"] == "no-counterpart":
            lines.append(f"- `{unit['id']}`: {unit['reason']}")
    lines += ["", "## A defect Temporal can express and revl cannot", ""]
    for unit in units:
        if unit["source"] == "temporal-only":
            outcome = outcomes[unit["id"]]
            lines.append(f"- `{unit['id']}` (expected {unit['expected']}): "
                         f"**{outcome['verdict']}**, {_cell(outcome['detail'])}. {unit['description']}")
    lines += ["", "## Controls", "",
              "A control is a clean port. It must pass every assertion, or the run is an error.",
              ""]
    for unit in units:
        if unit["source"] == "control":
            outcome = outcomes[unit["id"]]
            lines.append(f"- `{unit['id']}`: **{outcome['verdict']}**, {outcome['detail']}. "
                         f"{unit['description']}")
    lines += [
        "",
        "## What the numbers are not",
        "",
        "- The ports, the fake world and the assertions were written by the same people who",
        "  wrote the revl corpus, as analogues of revl's sweep. Same-author bias is not removed,",
        "  and the Temporal harness is a harness for this corpus, not Temporal's own tooling.",
        "  Sagas written by people outside the checker's authors are still not in the corpus.",
        "- The clean ports use Temporal's documented saga idiom: a stack of compensations,",
        "  each registered before its forward activity. A typed helper that pairs every",
        "  forward step with its compensation would move the `missing-undo` rows from the",
        "  fault test into `tsc`. The numbers compare revl with that idiom, not with every",
        "  library a Temporal user could write.",
        "- A failure is injected at an activity boundary the port itself marks with a",
        "  `faultPoint` call, the counterpart of revl's spliced `fail` step. The case where a",
        "  forward activity commits and then reports failure is not injected, on either side.",
        "- The harness holds no list of what a port registered, so it does not check that a",
        "  compensation was owed. `missing-compensate` is missed because no compensation",
        "  ran that could be wrong, the same outcome as revl but not the same cause: in revl",
        "  nothing is declared for the emission, here nothing is observable about the",
        "  absence. A test that hard-codes the intended compensations would catch it.",
        "- The order assertion applies only when each compensation runs at most once; a",
        "  repeated one cannot be placed against its forward step. `double-release` repeats",
        "  `release`, so its order is not judged, and it is missed in part by construction.",
        "- A run that fails with anything but the injected fault (a timeout, a worker or",
        "  server fault, a non-determinism error) stops the runner; it is never scored.",
        "- The fake world is readable by the test, so a wrong inverse is visible. For",
        "  `wrong-refund-order` the payment store is made opaque to match the revl row, and",
        "  that row is missed on both sides by construction; it measures what a test can see,",
        "  not the two systems.",
        "- The seeded defects are seeded, not found in real code, and one saga shape is not",
        "  a sample of sagas. A rate over real code is not measured.",
        "",
        "## Reproduce",
        "",
        "```sh",
        "# a Temporal dev server, for example `temporal server start-dev --headless`",
        "cd bench/saga_defects/temporal && npm ci && cd -",
        "REVL_TEMPORAL_DEV_SERVER=127.0.0.1:7233 python3 bench/saga_defects/run_temporal.py --write",
        "python3 bench/saga_defects/run_temporal.py --emit /tmp/ports   # the derived ports",
        "```",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------- main

def _shown(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score the Temporal ports of the seeded-defect saga corpus (issue #2736).")
    parser.add_argument("--address", default=None, metavar="HOST:PORT",
                        help=f"the Temporal server (default: ${ADDRESS_ENV})")
    parser.add_argument("--write", action="store_true",
                        help=f"refresh {_shown(RESULTS)} when no unit drifted")
    parser.add_argument("--list", action="store_true", help="list the units and measure nothing")
    parser.add_argument("--emit", metavar="DIR", default=None,
                        help="write every derived port under DIR and measure nothing")
    parser.add_argument("--only", action="append", default=None, metavar="ID",
                        help="measure only the named unit (repeatable)")
    parser.add_argument("--server-label", default="a Temporal dev server", metavar="TEXT",
                        help="how the results file names the server it measured against")
    parser.add_argument("--timeout", type=float, default=ROW_TIMEOUT, metavar="SECONDS",
                        help=f"stop a unit that runs longer (default {ROW_TIMEOUT:g})")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        print("saga-defect temporal: --timeout must be positive", file=sys.stderr)
        return 2

    try:
        data, units = load_units()
        if args.only:
            unknown = set(args.only) - {unit["id"] for unit in units}
            if unknown:
                raise CorpusError(f"no unit {sorted(unknown)}")
            units = [unit for unit in units if unit["id"] in set(args.only)]
        if args.write and args.only:
            print("saga-defect temporal: --write needs every unit, not --only", file=sys.stderr)
            return 2
        if args.list:
            for unit in units:
                print(f"{unit['id']:<24} {unit['source']:<13} {unit['expected']}")
            return 0
        if args.emit:
            out = Path(args.emit)
            for unit in units:
                if unit["expected"] != "no-counterpart":
                    derive(unit, out / unit["id"])
            print(f"wrote {sum(u['expected'] != 'no-counterpart' for u in units)} port(s) under {out}")
            return 0

        address = args.address or os.environ.get(ADDRESS_ENV)
        if not address:
            raise ToolchainMissing(
                f"no Temporal server: pass --address or set {ADDRESS_ENV} (a skip is not a verdict)")
        _node()
        _tsc()
        outcomes = {unit["id"]: measure(unit, address, args.timeout) for unit in units}
        revl = revl_verdicts()
    except CorpusError as error:
        print(f"saga-defect temporal: {error}", file=sys.stderr)
        return 1
    except ToolchainMissing as error:
        print(f"saga-defect temporal: {error}", file=sys.stderr)
        return 2

    print_table(units, outcomes)
    drift = [f"{unit['id']}: " + "; ".join(problems)
             for unit in units if (problems := disagreements(unit, outcomes[unit["id"]]))]
    if drift:
        print("\nsaga-defect temporal: expected catch point drifted:", file=sys.stderr)
        for line in drift:
            print(f"  {line}", file=sys.stderr)
        if args.write:
            print(f"saga-defect temporal: {_shown(RESULTS)} was not written", file=sys.stderr)
        return 1
    if args.write:
        RESULTS.parent.mkdir(parents=True, exist_ok=True)
        RESULTS.write_text(render_results(units, outcomes, data, revl, args.server_label),
                           encoding="utf-8")
        print(f"\nwrote {_shown(RESULTS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
