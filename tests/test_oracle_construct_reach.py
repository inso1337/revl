"""Non-vacuity, shape and ratchet checks for the construct-reach report."""

import copy
import importlib.util
import inspect
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _tool():
    spec = importlib.util.spec_from_file_location(
        "oracle_construct_reach", ROOT / "tools" / "oracle_construct_reach.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_every_oracle_has_named_constructs_and_a_corpus():
    data = _tool().survey()
    assert set(data) == {"emit_py", "emit_ts", "emit_go", "emit_java", "emit_rust",
                         "emit_wasm", "lower_ir", "compile", "gate_census"}
    for name, report in data.items():
        assert report["reference"], f"{name} reference construct table is empty"
        assert report["corpus"], f"{name} corpus is empty"
        assert set(report["reached"]) | set(report["unreached"]) == set(report["reference"])


def test_emitter_report_names_the_extern_reach():
    report = _tool().survey()["emit_rust"]
    assert any("externs.rvl" in document for document in report["corpus"])
    assert report["reached"]


def test_new_reference_construct_is_not_silent(monkeypatch):
    coverage = _tool()._load_coverage()
    original = coverage.reference_constructs

    def with_new_construct(path):
        found = original(path)
        found["kind=issue_301_probe"] = 0
        return found

    monkeypatch.setattr(coverage, "reference_constructs", with_new_construct)
    problems = coverage.check(coverage.survey())
    assert any("kind=issue_301_probe" in problem for problem in problems)


# ------------------------------------------------- the shrink-only ratchet
# Issue #1203: the report printed 249 UNREACHED rows and `--check` could not
# fail on any of them, so these four tests are the gate. Each one is written
# to FAIL on a tree without the ledger: they assert an exit code and a named
# problem, not the presence of a printout.


@pytest.fixture(scope="module")
def data():
    return _tool().survey()


def test_the_committed_ledger_matches_this_tree(data):
    assert _tool().check(data) == []


def test_a_newly_unreached_construct_fails_the_check(data):
    """The regression this gate exists for: a reference dispatch nothing in the
    corpus spells any more. Take a construct the corpus really does reach and
    move it to `unreached`, exactly as deleting its only corpus document would."""
    tool = _tool()
    oracle = "emit_py"
    construct = next(iter(sorted(data[oracle]["reached"])))
    perturbed = copy.deepcopy(data)
    perturbed[oracle]["reached"].pop(construct)
    perturbed[oracle]["unreached"] = sorted(
        [*perturbed[oracle]["unreached"], construct])

    problems = tool.check(perturbed)
    assert any(construct in p and "NEWLY UNREACHED" in p for p in problems), problems
    assert tool.main(["--check"]) == 0  # and the real tree is still green


def test_a_stale_ledger_entry_fails_the_check(data, monkeypatch):
    """The other direction, which is what makes it shrink-only: a construct the
    corpus now reaches, still listed. The fix is to delete the line."""
    tool = _tool()
    oracle = "emit_py"
    reached = next(iter(sorted(data[oracle]["reached"])))
    ledger = tool._ledger()
    ledger[oracle] = sorted([*ledger[oracle], reached])
    monkeypatch.setattr(tool, "_ledger", lambda: ledger)

    problems = tool.check(data)
    assert any(reached in p and "only shrinks" in p for p in problems), problems


def test_a_vacuous_report_fails_even_when_the_ledger_matches(data, monkeypatch):
    """Why `--check` keeps its vacuity condition. The ratchet compares unreached
    SETS, so an oracle whose unreached set is empty is outside it entirely: empty
    the reference table and the empty set still matches the empty ledger entry.
    Vacuity is the one failure the ratchet structurally cannot see.

    The ledger entry is emptied alongside the report because no oracle carries
    an empty one today -- `gate_census`'s was empty until issue #1215, for the
    wrong reason -- and it is the entry that decides whether the ratchet has
    anything to say. With a non-empty one the collapse reds as a stale entry,
    which is the ratchet working, not the condition under test."""
    tool = _tool()
    perturbed = copy.deepcopy(data)
    perturbed["gate_census"]["reference"] = []
    perturbed["gate_census"]["reached"] = {}
    perturbed["gate_census"]["unreached"] = []
    ledger = tool._ledger()
    ledger["gate_census"] = []
    monkeypatch.setattr(tool, "_ledger", lambda: ledger)

    problems = tool.check(perturbed)
    assert [p for p in problems if "vacuous" in p], problems
    assert not [p for p in problems if "UNREACHED" in p or "shrinks" in p], problems


def test_the_ledger_records_names_only():
    """CI runs python 3.11 and the developer venv is 3.14. The ledger holds
    construct NAMES and no counts, totals or line numbers, so a `--write` from
    either interpreter is the same file -- the exposure that corrupted the
    self-host coverage ledger's ungated totals does not exist here."""
    raw = json.loads(
        (ROOT / "tests" / "fixtures" / "oracle_construct_reach_ledger.json").read_text())
    for key, value in raw.items():
        if key.startswith("_"):
            continue
        assert isinstance(value, list)
        assert all(isinstance(name, str) for name in value), key


# ------------------------------------------------- the gate_census row (#1215)
# The row took its reference set from the KEYS of the census baseline's
# `buckets` map and its reach from that same map's VALUES, so a construct was
# reached exactly when it was in the reference set: `0 unreached` on any tree,
# by construction rather than by evidence, and the one ledger entry the ratchet
# could never fire on. Each of these fails on that spelling.


@pytest.fixture(scope="module")
def census():
    return _tool()._load_census()


def test_the_gate_census_row_does_not_read_the_census_baseline(data):
    """The two halves have no shared input, held without a live divergence.

    Until 2026-09-24 this compared the row's reference set against the bucket
    names in `tools/gate_reference_census_baseline.json` and asserted that file
    had buckets to compare against. PR #1396 recorded `{}` there (item 391) and
    the check went vacuous in both directions at once: there is nothing left to
    intersect, and the defect's own output would be empty too, so no comparison
    against that file can see the defect any more. A guard that only works
    while something is broken is a second copy of the bug.

    The two vocabularies are held against each other instead. The reference set
    is the census's guarantee vocabulary, and a census bucket name is whatever
    `census.bucket()` returns; both are derived here, so neither half depends
    on what the baseline happens to record today.
    """
    tool = _tool()
    report = data["gate_census"]

    # The positive half, and the one that fires on the defect: the old spelling
    # took the reference set from the baseline's `buckets` KEYS, which on this
    # tree would leave it empty.
    guarantees = tool._census_guarantees()
    assert guarantees, "the census can name no guarantee at all"
    assert set(report["reference"]) == guarantees

    # The defect's signature, stated on the vocabularies rather than on the
    # day's entries. `census.bucket()` is asked for the name of every kind of
    # divergence a guarantee tag can produce, which is the shape `--record`
    # writes and the shape the emptied baseline used to carry.
    naming = tool._load_census()
    staged = {naming.bucket((tag, "m"), ("no_objection", ""))
              for tag in guarantees}
    staged |= {naming.bucket((tag, "m"), ("refused", ("OTHER", "m")))
               for tag in guarantees}
    staged |= {naming.bucket((tag, "m"), ("refused", (tag, "other")))
               for tag in guarantees}
    assert len(staged) >= len(guarantees), staged
    assert not set(report["reference"]) & staged, \
        "the gate_census reference set is the baseline's bucket names again"

    # ... and the reference set is the census's guarantee vocabulary, read from
    # the classifier the census imports. Held against the module itself, so the
    # static reading cannot drift from the function it claims to be reading.
    spec = importlib.util.spec_from_file_location(
        "selfhost_lower_oracle", ROOT / "tests" / "test_selfhost_lower.py")
    oracle = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = oracle
    spec.loader.exec_module(oracle)
    source = inspect.getsource(oracle._classify)
    for tag in report["reference"]:
        assert f'"{tag}"' in source, f"{tag} is not a tag _classify can name"


def test_the_gate_census_gap_is_measured_not_constructed(data):
    """A reached set that is the reference set is the defect. The row has to be
    able to report a gap, and on this tree it reports one."""
    report = data["gate_census"]
    assert report["reached"], "no guarantee is reached; the row is vacuous"
    assert report["unreached"], (
        "the gate_census gap is empty again. Either every guarantee the census "
        "can name is now drawn by a corpus document -- delete the ledger "
        "entries, this is the shrink direction -- or the row derives its reach "
        "from its own reference set once more (issue #1215).")
    assert set(report["reached"]) != set(report["reference"])


def test_the_gate_census_corpus_is_the_one_the_census_runs_over(data, census):
    """Not `examples/**.rvl`, which the old row declared and never read."""
    report = data["gate_census"]
    expected = [str(path.relative_to(ROOT))
                for path, _ in _tool()._census_documents(census)]
    assert report["corpus"] == expected
    assert len(report["corpus"]) > 300
    assert all(any(entry.startswith(f"{sub}/") for sub in census.CORPUS_DIRS)
               for entry in report["corpus"])
    assert {entry for entry in report["corpus"]
            if not entry.startswith("examples/")}, \
        "the corpus is examples/ only again"

    # every document credited with a guarantee is in the corpus and really is
    # refused under that guarantee by the gate the census runs.
    corpus = set(report["corpus"])
    for tag, documents in report["reached"].items():
        assert documents
        assert set(documents) <= corpus


def test_a_guarantee_its_last_document_stops_drawing_fails_the_check(data, census):
    """THE gate. Take a guarantee exactly one corpus document draws, drop that
    document from the corpus the row measures -- what deleting or fixing the
    document does -- and the construct must go unreached and fail `--check`.

    The reach half is RUN here, not perturbed: `_census_reach` builds the
    census's fast engine and asks it about real documents, so a reach that came
    from the reference set again would not move when the document goes."""
    tool = _tool()
    report = data["gate_census"]
    solo = sorted(tag for tag, documents in report["reached"].items()
                  if len(documents) == 1)
    assert solo, "no guarantee rests on a single document; pick another lever"
    tag = solo[0]
    document = ROOT / report["reached"][tag][0]

    only = tool._census_reach(census, [(document, document.read_text())])
    assert set(only) == {tag}, (
        f"asked about {document.name} alone the reach answered {sorted(only)}: "
        f"it is not a function of the documents handed to it")

    without = copy.deepcopy(data)
    without["gate_census"]["reached"].pop(tag)
    without["gate_census"]["unreached"] = sorted(
        [*without["gate_census"]["unreached"], tag])
    problems = tool.check(without)
    assert any(tag in p and "NEWLY UNREACHED" in p for p in problems), problems

    # the control: the tree as it stands is still green.
    assert tool.check(data) == []


def test_the_compile_row_measures_the_corpus_its_oracle_runs_on(data):
    """The compile row read 5 of 5 unreached because it compared IR section
    names against `field=value` reach keys over a corpus scraped from every
    quoted `.rvl` literal in the oracle's source. Both halves are wired to the
    same vocabulary now, so the row is a measurement."""
    tool = _tool()
    report = data["compile"]
    assert set(report["reached"]) >= {"functions", "components", "externs", "types"}

    spec = importlib.util.spec_from_file_location(
        "compile_oracle_corpus", ROOT / "tests" / "test_selfhost_compile.py")
    oracle = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = oracle
    spec.loader.exec_module(oracle)
    expected = {str((ROOT / "tests" / "fixtures" / subdir / name).relative_to(ROOT))
                for _tier, subdir, name in
                [*oracle.NATIVE_CORPUS, *oracle.COMPONENT_CORPUS]}
    assert set(report["corpus"]) == expected
    assert tool._compile_corpus()
