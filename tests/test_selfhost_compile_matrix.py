"""The self-host compile ledger is measured, stale by construction, and total.

`tools/selfhost_matrix.py` records, per self-host file and per tier, whether
that tier's reference emitter compiles the file (issue #2899). The measurement
is minutes long, so it is a committed ledger with a cheap gate rather than a
walk in CI. This file holds the gate's refusals, each constructed rather than
hoped for:

  * a ledger whose sources or emitters have moved since it was measured;
  * a cell that does not emit and is not acknowledged anywhere;
  * an acknowledgement whose cell emits now (the ratchet's other direction);
  * an acknowledgement with no issue and no recorded scope decision;
  * an acknowledgement with no reason naming the construct.

`test_the_committed_ledger_is_current` is the control: it passes on the tree
every refusal below fails on.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "tools"), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import selfhost_matrix as sm  # noqa: E402


@pytest.fixture(scope="module")
def ledger():
    data = sm.load()
    assert data is not None, (
        "tools/selfhost_compile_matrix.json is missing; measure it with "
        "`python3 tools/selfhost_matrix.py --measure`")
    return data


def _changed_problems(ledger, gaps, **kwargs):
    kwargs.setdefault("expected_digest", ledger["digest"])
    return sm.problems(ledger, gaps, **kwargs)


def _a_cell(ledger, kind):
    for name, row in ledger["cells"].items():
        for tier, cell in row.items():
            if cell["kind"] == kind:
                return f"{name}::{tier}"
    raise AssertionError(f"the committed ledger has no {kind} cell")


# ----------------------------------------------------------------- the control

def test_the_committed_ledger_is_current():
    """THE CONTROL: the gate passes on the tree every refusal below fails on."""
    assert sm.problems() == []
    assert sm.main(["--check"]) == 0


def test_the_ledger_covers_every_self_host_file_and_tier(ledger):
    assert ledger["files"] == [path.name for path in sm.sources()]
    assert ledger["tiers"] == list(sm.tiers())
    for name in ledger["files"]:
        assert set(ledger["cells"][name]) == set(ledger["tiers"]), name


# ------------------------------------------------------------------- staleness

def test_a_moved_source_or_emitter_marks_the_ledger_stale(ledger):
    found = sm.problems(ledger, sm.load_gaps(), expected_digest="0" * 64)
    assert any("different self-host, stdlib or tier emitter sources" in problem
               for problem in found)


def test_the_digest_covers_every_kind_of_source_the_measurement_reads(tmp_path):
    """A digest that quietly stopped hashing an emitter or a stdlib file would
    make the gate stop noticing the change it exists to notice."""
    (tmp_path / "selfhost").mkdir()
    (tmp_path / "stdlib").mkdir()
    (tmp_path / "backends" / "go").mkdir(parents=True)
    corpus = tmp_path / "selfhost" / "a.rvl"
    stdlib = tmp_path / "stdlib" / "s.rvl"
    emitter = tmp_path / "backends" / "go" / "emit.py"
    for path, text in ((corpus, "corpus"), (stdlib, "stdlib"), (emitter, "emitter")):
        path.write_text(text, encoding="utf-8")
    seen = {sm.digest(tmp_path)}
    for path, text in ((emitter, "emitter2"), (stdlib, "stdlib2"), (corpus, "corpus2")):
        path.write_text(text, encoding="utf-8")
        seen.add(sm.digest(tmp_path))
    assert len(seen) == 4


# -------------------------------------------------------------- acknowledgement

def test_an_unacknowledged_non_emitting_cell_is_reported(ledger):
    cell = _a_cell(ledger, sm.REFUSED)
    gaps = [entry for entry in sm.load_gaps() if entry["cell"] != cell]
    found = _changed_problems(ledger, gaps)
    assert any(cell in problem and "no acknowledgement" in problem
               for problem in found), found


def test_an_acknowledgement_whose_cell_emits_now_is_stale(ledger):
    cell = _a_cell(ledger, sm.OK)
    gaps = sm.load_gaps() + [{"cell": cell, "issue": 1, "reason": "planted"}]
    found = _changed_problems(ledger, gaps)
    assert any(cell in problem and "needs no acknowledgement now" in problem
               for problem in found), found


def test_an_acknowledgement_needs_an_issue_or_a_scope_decision(ledger):
    cell = _a_cell(ledger, sm.REFUSED)
    gaps = copy.deepcopy(sm.load_gaps())
    entry = next(entry for entry in gaps if entry["cell"] == cell)
    entry.pop("issue", None)
    entry.pop("out_of_scope", None)
    found = _changed_problems(ledger, gaps)
    assert any(cell in problem and "integer `issue`" in problem for problem in found)


def test_an_acknowledgement_needs_a_reason(ledger):
    cell = _a_cell(ledger, sm.REFUSED)
    gaps = copy.deepcopy(sm.load_gaps())
    entry = next(entry for entry in gaps if entry["cell"] == cell)
    entry["reason"] = ""
    found = _changed_problems(ledger, gaps)
    assert any(cell in problem and "`reason`" in problem for problem in found)


def test_a_frontend_refusal_is_unmeasured_and_needs_no_acknowledgement(ledger):
    cell = _a_cell(ledger, sm.OK)
    name, tier = cell.split("::")
    changed = copy.deepcopy(ledger)
    changed["cells"][name][tier] = {"kind": sm.FRONTEND, "message": "planted"}
    gaps = [entry for entry in sm.load_gaps() if entry["cell"] != cell]
    assert _changed_problems(changed, gaps) == []


def test_an_acknowledgement_for_a_cell_the_measurement_lost_is_reported(ledger):
    gaps = sm.load_gaps() + [
        {"cell": "not-a-selfhost-file.rvl::go", "issue": 1, "reason": "planted"}]
    found = _changed_problems(ledger, gaps)
    assert any("not-a-selfhost-file.rvl::go" in problem and
               "acknowledged but not measured" in problem for problem in found)


# ------------------------------------------------------------ the table derives

def test_a_tier_with_a_frontend_cell_is_unmeasured_not_a_count(ledger):
    changed = copy.deepcopy(ledger)
    tier = changed["tiers"][0]
    changed["cells"][changed["files"][0]][tier] = {"kind": sm.FRONTEND,
                                                   "message": "planted"}
    assert sm.tier_counts(changed, tier) is None


def test_tier_counts_follow_the_ledger_cells(ledger):
    for tier in ledger["tiers"]:
        kinds = [row[tier]["kind"] for row in ledger["cells"].values()]
        expected = (sum(1 for kind in kinds if kind == sm.OK), len(kinds))
        assert sm.tier_counts(ledger, tier) == expected
