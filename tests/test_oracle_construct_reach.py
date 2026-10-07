"""Non-vacuity, shape and ratchet checks for the construct-reach report."""

import copy
import inspect
import json
from pathlib import Path

import pytest
from _load_by_path import load_by_path


ROOT = Path(__file__).resolve().parents[1]


def _tool():
    module = load_by_path(
        "oracle_construct_reach",
        ROOT / "tools" / "oracle_construct_reach.py")
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
    oracle = load_by_path(
        "selfhost_lower_oracle",
        ROOT / "tests" / "test_selfhost_lower.py")
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

    oracle = load_by_path(
        "compile_oracle_corpus",
        ROOT / "tests" / "test_selfhost_compile.py")
    expected = {str((ROOT / "tests" / "fixtures" / subdir / name).relative_to(ROOT))
                for _tier, subdir, name in
                [*oracle.NATIVE_CORPUS, *oracle.COMPONENT_CORPUS]}
    assert set(report["corpus"]) == expected
    assert tool._compile_corpus()


# ------------------------------------------------------ the triage gate (#2099)
# Roadmap item 533's second half, and the residue its tracking issue left
# behind: the row's head read STILL OPEN while #1203 had closed on 2026-09-20,
# because the ratchet held the unreached set and nothing held the OBLIGATION.
# Every entry carried a name and no decision, so the count was a number with no
# owner and the number was a hand copy of a measurement.
#
# `--check` is total now: an entry nobody decided fails it, and so does a
# disposition that outlived its entry. The tests below each fail on the tree
# where that is not so, and the last one holds the row's own count against the
# ledger it describes, which is the half a reader of the roadmap sees.


def test_every_ledger_entry_carries_a_written_disposition(data):
    """Totality, measured entry by entry. A disposition is DERIVED when the
    sibling reason-first ledger already decides the construct for a corpus this
    oracle shares -- recomputed by inverting that file, never restated -- and
    RECORDED when this tree's dispositions file writes the decision down."""
    tool = _tool()
    ledger = tool._ledger()
    decided, recorded = tool._blind_spots(), tool._dispositions()

    sources = {"DERIVED": [], "RECORDED": []}
    undisposed = []
    for oracle, constructs in sorted(ledger.items()):
        for construct in sorted(constructs):
            found = tool.triage(oracle, construct, decided, recorded)
            if found is None:
                undisposed.append(f"{oracle}: {construct}")
                continue
            sources[found[0]].append((oracle, construct))

    assert undisposed == [], undisposed
    total = sum(len(constructs) for constructs in ledger.values())
    assert len(sources["DERIVED"]) + len(sources["RECORDED"]) == total
    assert sources["DERIVED"], "no entry is disposed of by the sibling ledger"
    assert sources["RECORDED"], "no entry is disposed of by this tree's file"
    assert tool.check(data) == []


def test_a_disposition_is_a_reason_and_not_a_label():
    """The floor the sibling reason-first ledger sets for its own reasons, held
    for the six no tier of it decides. A construct both implementations can
    dispatch and nothing exercises is a DECISION, and `out of scope` is not
    one: it says nothing a reader can act on or argue with."""
    for oracle, rows in sorted(_tool()._dispositions().items()):
        assert rows, f"{oracle}: an oracle with no dispositions"
        for construct, reason in sorted(rows.items()):
            assert len(reason) > 40, (
                f"{oracle}: `{construct}` is disposed of by `{reason}`, which "
                f"is a label and not a reason")


def test_an_undisposed_entry_fails_the_check(data, monkeypatch):
    """The regression this half exists for. Take an entry the sibling ledger
    decides, drop that construct from the inverted map -- which is what a
    reason-first entry being deleted does -- and the entry is owed again."""
    tool = _tool()
    decided = tool._blind_spots()
    oracle, construct = next(
        (oracle, construct)
        for oracle, constructs in sorted(tool._ledger().items())
        for construct in sorted(constructs)
        if construct in decided.get(oracle, {}))
    decided[oracle].pop(construct)
    monkeypatch.setattr(tool, "_blind_spots", lambda: decided)

    problems = tool.check(data)
    assert any(construct in p and "NO written disposition" in p
               for p in problems), problems
    # the other half of the pair: a RECORDED row is a disposition too, so the
    # entry stays disposed while the reason-first map has stopped deciding it.
    assert not [p for p in problems if "NEWLY UNREACHED" in p], problems


def test_a_disposition_that_outlived_its_entry_fails_the_check(data, monkeypatch):
    """The shrink direction, one level up. A row whose construct is no longer a
    recorded gap is the same rot as a stale ledger entry: a decision about a
    hole that has been filled, left where the next reader takes it for live."""
    tool = _tool()
    recorded = tool._dispositions()
    recorded["compile"]["a section nobody owes"] = (
        "this line disposes of no entry in the ledger, and must be deleted "
        "rather than left to look like a decision")
    monkeypatch.setattr(tool, "_dispositions", lambda: recorded)

    problems = tool.check(data)
    assert any("no longer a recorded gap" in p for p in problems), problems


def test_an_empty_disposition_fails_the_check(data, monkeypatch):
    """`--check`'s empty clause, kept apart from the stale one: a row that is
    still about a live entry and says nothing at all."""
    tool = _tool()
    recorded = tool._dispositions()
    recorded["compile"]["tests"] = "   "
    monkeypatch.setattr(tool, "_dispositions", lambda: recorded)

    problems = tool.check(data)
    assert any("EMPTY disposition" in p for p in problems), problems


def test_the_triage_report_names_every_entry_and_its_source(data, capsys):
    """`--triage` is the reader's half: the number the row quotes, broken down
    into the decision behind each line. It prints no UNDISPOSED line, because
    the ledger has none, and it is a flag `main` accepts."""
    tool = _tool()
    tool.triage_report(data)
    out = capsys.readouterr().out
    assert "UNDISPOSED" not in out, out
    total = sum(len(entry["unreached"]) for entry in data.values())
    for oracle, entry in data.items():
        assert f"{oracle}: {len(entry['unreached'])} unreached" in out
        for construct in entry["unreached"]:
            assert f" {construct}\n" in out, construct
    assert f"{total} disposed:" in out
    assert " DERIVED " in out and " RECORDED " in out

    with pytest.raises(SystemExit) as exit_code:
        tool.main(["--help"])
    assert exit_code.value.code == 0
    assert "--triage" in capsys.readouterr().out


def test_the_gate_census_unreached_tags_are_elicitable(data, census):
    """The five `gate_census` dispositions are dispositions of a MEASURED fact
    and not of an absence, which is the difference between this ledger and a
    list of things nobody looked at.

    The row's corpus is `_census_documents`, which is documents only; the
    census's `ACCEPTED_PROGRAMS` / `REJECTED_PROGRAMS` / `ADMISSION_PROGRAMS`
    are strings in `tests/test_selfhost_lower.py` and are excluded there by
    construction. So four of the five tags read UNREACHED while the census's own
    gate issues them the moment it is handed those programs -- which is what
    this test does, over the tables themselves. HOST-ARITY is the fifth and the
    one still owed a document: no table carries it, so it is driven here by the
    program `tests/test_selfhost_lower.py` already pins its sentence for."""
    tool = _tool()
    oracle = load_by_path(
        "selfhost_lower_programs",
        ROOT / "tests" / "test_selfhost_lower.py")
    where = ROOT / "tests" / "test_selfhost_lower.py"
    documents = [(where, src) for _name, src in oracle.ACCEPTED_PROGRAMS]
    documents += [(where, entry[1]) for entry in oracle.REJECTED_PROGRAMS]
    documents += [(where, src) for _name, src in census.ADMISSION_PROGRAMS]

    reached = set(tool._census_reach(census, documents))
    assert {"BOOT", "HANDOFF", "ROUTE", "SPAWN"} <= reached, (
        f"the census's gate no longer issues {sorted({'BOOT', 'HANDOFF', 'ROUTE', 'SPAWN'} - reached)} "
        f"for any program in its own tables, so those dispositions name a "
        f"reach that does not exist")

    assert "HOST-ARITY" not in reached, (
        "HOST-ARITY is elicited by an in-memory table now. The ledger entry is "
        "still a gap -- no corpus DOCUMENT draws it -- but the disposition "
        "says no table carries it, so correct that line")
    arity = tool._census_reach(
        census, [(where, 'fn f() { let p = Pool.open("dsn") }\n')])
    assert set(arity) == {"HOST-ARITY"}, arity

    # and the entry is still a gap, which is why it is in the ledger at all.
    assert {"BOOT", "HANDOFF", "HOST-ARITY", "ROUTE", "SPAWN"} == \
        set(data["gate_census"]["unreached"])


def test_the_roadmap_row_agrees_with_the_measurement(data):
    """The anti-staleness half, and the one a reader of the roadmap depends on.
    Item 533's row quoted a count measured on a different day, so it went on
    reading STILL OPEN after the work behind it had closed. The row is held
    against the ledger it describes here, so a ledger that shrinks without the
    row being edited fails instead of ageing."""
    markers = load_by_path(
        "check_roadmap_markers_for_item_533",
        ROOT / "tools" / "check_roadmap_markers.py")
    text = (ROOT / "docs" / "v2.0-roadmap.md").read_text(encoding="utf-8")
    rows = [item for item in markers.items(text) if item["number"] == "533"]
    assert len(rows) == 1, [item["line"] for item in rows]
    row = rows[0]["body"]

    tool = _tool()
    decided, recorded = tool._blind_spots(), tool._dispositions()
    derived = recorded_count = 0
    for oracle, constructs in tool._ledger().items():
        for construct in constructs:
            source = tool.triage(oracle, construct, decided, recorded)[0]
            derived += source == "DERIVED"
            recorded_count += source == "RECORDED"
    measured = derived + recorded_count
    assert measured == sum(len(entry["unreached"]) for entry in data.values())

    assert f"ledger's {measured} entries" in row, (
        f"the row does not quote the measured {measured}")
    assert f"{derived} are DERIVED" in row, (
        f"the row does not quote the measured {derived} DERIVED")
    assert f"the {recorded_count} no tier" in row, (
        f"the row does not quote the measured {recorded_count} RECORDED")
    assert "issue #2099" in row, "the row does not name the issue that owns it"
