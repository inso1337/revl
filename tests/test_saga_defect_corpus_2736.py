"""The seeded-defect saga corpus (issue #2736): bench/saga_defects/.

The static half of the exit runs everywhere: the data file is the index, every
row names a saga that exists, every row that expects a compile refusal is
refused with the recorded code, and every row that expects a runtime verdict
compiles (so its classification is meaningful rather than a compile refusal in
disguise).

The measurement half is `run.py` itself and is slow by nature: it compiles and
fault-sweeps 16 sagas on the real runtime. It is marked `slow` and needs the
pinned cordis-py, which the full gate installs through
`backends/python/setup.sh`; the fast lanes deselect it. It checks the measured
verdicts against the committed table, so the table cannot silently go stale.
"""

import importlib
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "bench" / "saga_defects"
RESULTS = ROOT / "bench" / "results" / "saga-defect-corpus.md"
HAS_CORDIS = importlib.util.find_spec("cordis") is not None


def _load():
    spec = importlib.util.spec_from_file_location("saga_defects_bench", BENCH / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


R = _load()
DATA = R.load_corpus()
ROWS = DATA["defects"]
COMPILE_ROWS = [row for row in ROWS if row["expected"] == "compile"]
RUNTIME_ROWS = [row for row in ROWS if row["expected"] != "compile"]


def _code(error):
    if error.code is not None:
        return error.code
    return importlib.import_module("revl.diagnostics").classify(error)["code"]


def _committed_verdicts():
    text = RESULTS.read_text(encoding="utf-8")
    verdicts = {}
    for line in text.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 6:
            continue
        verdicts[cells[0].strip("`")] = cells[4].strip("*")
    return verdicts


def _committed_totals():
    text = RESULTS.read_text(encoding="utf-8")
    match = re.search(
        r"\*\*(\d+) rows: (\d+) refused at compile, (\d+) caught by a fault "
        r"test, (\d+) missed by both", text)
    assert match is not None, "the results table lost its totals line"
    return tuple(int(part) for part in match.groups())


# ------------------------------------------------------------------ static

def test_the_slice_has_between_12_and_20_defects():
    assert 12 <= len(ROWS) <= 20, (
        "the first slice of issue #2736 asks for 12 to 20 seeded defects")


def test_every_row_has_its_own_saga():
    files = [row["file"] for row in ROWS]
    assert len(files) == len(set(files)), "one defect per saga file"


def test_the_rows_cover_the_defect_classes_the_issue_names():
    kinds = {row["kind"] for row in ROWS}
    assert {"missing-undo", "unmarked-emission", "dependency-cycle",
            "manual-teardown", "wrong-lifo-order", "failing-inverse",
            "failing-compensation", "missing-compensate",
            "wrong-inverse-target"} <= kinds


def test_every_compile_row_records_its_refusal_code():
    for row in COMPILE_ROWS:
        assert row.get("expectedCode"), (
            f"{row['id']}: a compile row must record the expected refusal code")


@pytest.mark.parametrize("row", COMPILE_ROWS, ids=[row["id"] for row in COMPILE_ROWS])
def test_each_compile_row_is_refused_with_its_code(row):
    _ir, refusal = R._compile(BENCH / row["file"])
    assert refusal is not None, (
        f"{row['id']}: expected a compile refusal, but the saga compiled")
    assert _code(refusal) == row["expectedCode"], (
        f"{row['id']}: got {_code(refusal)!r}, want {row['expectedCode']!r}: "
        f"{refusal.message}")


@pytest.mark.parametrize("row", RUNTIME_ROWS, ids=[row["id"] for row in RUNTIME_ROWS])
def test_each_runtime_row_compiles_cleanly(row):
    ir, refusal = R._compile(BENCH / row["file"])
    assert refusal is None, (
        f"{row['id']}: a runtime verdict needs a compiling saga, got "
        f"{refusal.code}: {refusal.message}")
    names = {component.get("name") for component in ir["components"]}
    assert row["component"] in names


def test_the_results_table_covers_every_row():
    assert set(_committed_verdicts()) == {row["id"] for row in ROWS}


# The paper sentences that quote the corpus totals. A measured number in prose
# is a claim that can drift, so each is pinned to the committed measurement by
# line: the tuple names which totals the line states, in the order
# `_committed_totals` returns them. A line move or a wording edit that keeps the
# numbers is a test change, not a paper change; that is deliberate, because the
# pins are what make the prose checkable at all.
PAPER_TOTALS = {
    "docs/research/papers/P01-typed-revertible-effects.md": {
        7: ("rows",),
        17: ("rows", "compile", "fault-test", "missed"),
        68: ("rows", "compile", "fault-test", "missed"),
        75: ("rows", "compile", "fault-test", "missed"),
        76: ("rows",),
        87: ("rows",),
        97: ("rows",),
    },
    "docs/research/papers/P16-testing-recovery-claims-sweep-roundtrip.md": {
        7: ("rows", "fault-test", "missed"),
        73: ("rows", "fault-test", "missed"),
    },
}


def test_the_paper_totals_are_the_measured_totals():
    measured = dict(zip(("rows", "compile", "fault-test", "missed"),
                        _committed_totals()))
    drift = []
    for relative, pinned in PAPER_TOTALS.items():
        lines = (ROOT / relative).read_text(encoding="utf-8").splitlines()
        for line_no, keys in pinned.items():
            if line_no > len(lines):
                drift.append(f"{relative}:{line_no} is past the end of the paper")
                continue
            line = lines[line_no - 1]
            for key in keys:
                number = str(measured[key])
                if re.search(rf"(?<!\d){number}(?!\d)", line) is None:
                    drift.append(
                        f"{relative}:{line_no} does not state {key}={number}")
    assert not drift, "\n".join(drift)


# ------------------------------------------------------------- measurement

@pytest.mark.slow
@pytest.mark.skipif(
    not HAS_CORDIS,
    reason="the fault sweep needs the pinned cordis-py runtime; the full gate "
           "installs it through backends/python/setup.sh")
def test_the_committed_table_is_the_measured_table():
    outcomes = {row["id"]: R.measure(row) for row in ROWS}
    drift = []
    for row in ROWS:
        problems = R.disagreements(row, outcomes[row["id"]])
        if problems:
            drift.append(f"{row['id']}: " + "; ".join(problems))
    assert not drift, "\n".join(drift)

    committed = _committed_verdicts()
    stale = [row["id"] for row in ROWS
             if committed.get(row["id"]) != outcomes[row["id"]]["verdict"]]
    assert not stale, (
        "bench/results/saga-defect-corpus.md is stale for "
        f"{stale}; rerun `python3 bench/saga_defects/run.py --write`")

    counts = [sum(1 for outcome in outcomes.values()
                  if outcome["verdict"] == verdict)
              for verdict in R.VERDICTS]
    assert _committed_totals() == (len(ROWS), *counts), (
        "the committed totals disagree with the measurement")
