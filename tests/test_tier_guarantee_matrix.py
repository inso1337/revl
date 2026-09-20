"""The guarantee x tier matrix is generated, total, and its gate can fire.

`tools/tier_guarantees.py` derives, per guarantee and per tier, one of `proved`
/ `divergence` / `no reproducer` / `unimplemented` from the checker's own
registers (issue #1197, roadmap item 523). A generated support table is only
worth more than a hand-written one if the thing that generates it REFUSES the
cases a hand-written one would have rendered quietly, so most of this file is
the refusals, each constructed rather than hoped for:

  * a guarantee added with no reproducer and no acknowledgement;
  * an acknowledgement left behind after the gap it excused closed;
  * a parity subject added to the marker check with no decision here;
  * a runtime divergence pinned with no guarantee to hang it on;
  * a hand edit to the committed block.

Each of those has a test that asserts the failure, and
`test_the_unmodified_tree_generates` is the control: it passes on the same tree
every one of them fails on, so a broken fixture cannot masquerade as a working
gate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "tools"), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import conformance  # noqa: E402
import tier_guarantees as tg  # noqa: E402


@pytest.fixture(scope="module")
def data():
    return tg.matrix()


# ------------------------------------------------------------------ the shape

def test_the_unmodified_tree_generates(data):
    """THE CONTROL. The generator succeeds, with a cell for every guarantee on
    every tier, on exactly the tree every refusal test below fails on."""
    from revl.diagnostics import GUARANTEES

    assert {row["code"] for row in data["rows"]} == set(GUARANTEES)
    assert data["tiers"][-1] == tg.SELFHOST_TIER
    assert len(data["tiers"]) == len(conformance.TIERS) + 1
    for row in data["rows"]:
        for tier in data["tiers"]:
            cell = row["cells"][tier]
            assert cell["verdict"] in tg._CELL, (row["code"], tier)
            assert cell["why"].strip(), (
                f"{row['code']} on {tier} has a verdict and no reason; a cell "
                "that cannot say why is the blank row this gate exists to stop")


def test_every_cell_links_or_explains_its_evidence(data):
    """A verdict with no path to check it against is an opinion."""
    for row in data["rows"]:
        evidence = row["evidence"]
        assert evidence, f"{row['code']} has no evidence path"
        assert (ROOT / evidence).exists(), (row["code"], evidence)


def test_the_rows_are_the_compilers_own_register():
    """Not a copy of it. A code added to `revl.diagnostics.GUARANTEES` shows up
    here with no edit to the matrix, which is what makes a missing row
    impossible rather than merely unlikely."""
    from revl.diagnostics import GUARANTEES

    assert tg.guarantee_codes() and set(tg.guarantee_codes()) == set(GUARANTEES)


def test_the_block_carries_no_line_numbers(data):
    """A roadmap line number in the block would re-stale docs/conformance.md on
    every unrelated roadmap edit, and a generated file that reds the build for
    somebody else's commit gets regenerated without being read."""
    block = tg.markdown(data)
    assert "L4" not in block and "L1" not in block


# ------------------------------------------------- the gate, made to fire

def test_a_guarantee_with_no_row_fails_generation(monkeypatch):
    """THE CASE THE ISSUE NAMES: a guarantee arrives and some tier has nothing
    to say about it. The generator must refuse, not render a blank."""
    from revl import diagnostics

    monkeypatch.setitem(diagnostics.GUARANTEES, "G99",
                        "a guarantee nothing in the tree enforces")
    with pytest.raises(tg.MatrixError) as caught:
        tg.matrix()
    message = str(caught.value)
    assert "G99" in message
    assert "ACKNOWLEDGED" in message


def test_a_stale_acknowledgement_fails_generation(monkeypatch):
    """The other direction. An excuse that outlives its reason is exactly how a
    support table rots, so the ratchet refuses a spent entry too."""
    monkeypatch.setitem(tg.ACKNOWLEDGED, "G1", "a gap that has since closed")
    with pytest.raises(tg.MatrixError) as caught:
        tg.matrix()
    assert "G1" in str(caught.value)


def test_a_parity_subject_with_no_decision_fails_generation(monkeypatch):
    """`--check-tier-parity` gaining a subject must force a decision here.

    Otherwise the marker check would start speaking about a guarantee this
    matrix silently ignores, which is the same invisible-gap bug one level up.
    """
    import check_roadmap_markers as markers

    monkeypatch.setattr(markers, "TIER_SUBJECTS",
                        markers.TIER_SUBJECTS + ("quarantine",))
    with pytest.raises(tg.MatrixError) as caught:
        tg.parity_divergences(conformance.TIERS)
    assert "quarantine" in str(caught.value)
    assert "SUBJECT_CODES" in str(caught.value)


def test_an_unmapped_runtime_divergence_fails_generation(monkeypatch):
    """The pinned cross-tier register is EMPTY on main, which is the state in
    which a register silently stops being read. This proves the path is armed
    while it has nothing to say: put an entry in and generation refuses it."""
    import importlib.util
    import types

    fake = types.ModuleType("fake_xtier")
    fake.DIVERGENCES = {"a pinned runtime behaviour": ("src", {"java": "fail"})}

    real = importlib.util.spec_from_file_location
    def spec_from(name, location, *a, **k):
        if name == "tier_guarantees_xtier":
            class _Loader:
                @staticmethod
                def exec_module(module):
                    module.DIVERGENCES = fake.DIVERGENCES
            return types.SimpleNamespace(loader=_Loader(), name=name,
                                         submodule_search_locations=None,
                                         origin=str(location))
        return real(name, location, *a, **k)

    monkeypatch.setattr(importlib.util, "spec_from_file_location", spec_from)
    monkeypatch.setattr(importlib.util, "module_from_spec",
                        lambda spec: types.ModuleType(spec.name))
    with pytest.raises(tg.MatrixError) as caught:
        tg.runtime_divergences()
    assert "RUNTIME_DIVERGENCE_CODES" in str(caught.value)


def test_a_parity_finding_moves_the_cells_it_names():
    """The register is wired to the cells, on a roadmap text this test writes.

    Driven by a synthetic document rather than the live roadmap: the live one
    is being edited constantly, so a test that asserted today's findings would
    be a tripwire for other people's commits rather than a check on this code.
    """
    import check_roadmap_markers as markers

    backends = set(markers.TIER_ALIASES)
    text = ("## Open, in rough priority order\n\n"
            "999. **A CAPABILITY AUDIT (2026-09-19).** Findings below.\n\n"
            "**F1 HIGH, \u2705 FIXED 2026-09-19** (`fix/999-secret-scrub`). "
            "The scrub is placed at the one choke point in "
            "`backends/rust/emit.py`, so a declared secret marking redacts the "
            "taint before the capability boundary.\n")

    records = markers.tier_parity_records(text, backends)
    assert records, "the synthetic finding did not reach the marker check"
    assert records[0]["tier"] == "rust"
    assert "secret" in records[0]["subjects"]

    # and the map turns that record into cells on every tier it does not name
    moved = {code for subject in records[0]["subjects"]
             for code in tg.SUBJECT_CODES[subject]}
    assert moved, "a secret/redaction finding must move at least one guarantee"
    assert moved <= set(tg.guarantee_codes())
    assert set(records[0]["others"]) == backends - {"rust"}

    # the escape hatch the gate documents: name the other tier and it stops
    named = text.replace(
        "before the capability boundary.",
        "before the capability boundary. The ts tier has no marking pass at "
        "all and is out of scope here; it is filed separately.")
    assert markers.tier_parity_records(named, backends) == []


def test_every_parity_subject_maps_to_real_guarantee_codes():
    """A mapping to a code that does not exist would move no cell and look
    fine. Every non-empty mapping has to name a live register key."""
    codes = set(tg.guarantee_codes())
    for subject, mapped in tg.SUBJECT_CODES.items():
        for code in mapped:
            assert code in codes, (subject, code)


# ----------------------------------------------------- the committed block

def _fresh_block() -> str:
    return tg.block()


def test_the_committed_block_matches_a_fresh_generation():
    """A hand edit to the generated block fails here and in
    `python3 tools/conformance.py --check-readme`."""
    doc = (ROOT / "docs" / "conformance.md").read_text(encoding="utf-8")
    start, end = tg.START, tg.END
    assert start in doc and end in doc, (
        "the GUARANTEE-TIER-MATRIX markers are missing from docs/conformance.md")
    committed = doc[doc.index(start):doc.index(end) + len(end)]
    assert committed == _fresh_block(), (
        "docs/conformance.md's guarantee x tier matrix is stale — run "
        "`python3 tools/conformance.py --write-readme` and commit")


def test_generation_is_deterministic():
    """Two generations byte-identical, or the staleness gate flaps."""
    assert _fresh_block() == _fresh_block()
