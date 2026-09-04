"""What the DOC-STATUS inventory is allowed to be a function of (issue #296).

`tools/docgen.py --check` runs in the required `frontend` job, so a stale
`doc-status` block reds every open PR at once and not only the branch that
caused it. The block was already stale at #267's own merge commit, which is
the cost being described: everyone pays for it, on every landing that moves
it.

That cost is worth paying for the claim the block exists to make. Which docs
there are, and how many em-dashes each carries, is the AI-tell signal the docs
style pass is for, and it is load-bearing: over the 40 most recent first-parent
landings on main, 18 changed an em-dash count and 17 of those RAISED one, so
the gate is firing on real style regressions rather than on noise.

It is not worth paying for a per-doc number that moves whenever a doc is edited
at all and shows nothing a diff does not already show: a line count, a byte
count, a word count, a checksum. Issue #296 proposed dropping such a column.
There is none to drop, and measuring the counterfactual says why not to add
one: a line-count column would have rewritten the block on 23 of those same 40
landings against 18 for em-dashes alone, for no detection at all.

These tests are what keeps that true. Both directions of #296's exit test are
asserted below, against a stand-in docs tree so they pin the RULE rather than
the repo's current inventory:

  * editing a doc's body without touching its prose style does NOT move the
    block, however much the body changes;
  * adding a doc, removing one, or changing a doc's em-dash count DOES.

If a later change makes the block depend on anything else about a doc's bytes,
`test_a_body_rewrite_that_keeps_the_em_dash_count_does_not_move_the_block`
fails and names the column that did it.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# docgen is a tool, not a package module. Load it by path, the way CI runs it.
_spec = importlib.util.spec_from_file_location(
    "revl_docgen_under_test", REPO / "tools" / "docgen.py"
)
docgen = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = docgen
_spec.loader.exec_module(docgen)


# The committed table the generation carries human judgement across. `alpha.md`
# has a status and a note to prove both survive; `beta.md` has neither.
COMMITTED = (
    "| doc | status | em-dashes | tier-limit notes |\n"
    "|---|---|---|---|\n"
    "| alpha.md | current | 1 | keep me |\n"
    "| beta.md | needs-work | 0 |  |"
)

ONE_EM_DASH = "alpha has one — dash.\n"
NO_EM_DASH = "beta has none.\n"


@pytest.fixture
def docs(tmp_path, monkeypatch):
    """A stand-in `docs/` tree. Returns a writer; the caller builds the tree it
    wants and calls `render()` to get the block body docgen would generate."""
    (tmp_path / "docs").mkdir()
    monkeypatch.setattr(docgen, "ROOT", tmp_path)

    def write(name: str, text: str) -> None:
        (tmp_path / "docs" / name).write_text(text, encoding="utf-8")

    write("alpha.md", ONE_EM_DASH)
    write("beta.md", NO_EM_DASH)
    return write


def render() -> str:
    return docgen.block_doc_status(COMMITTED)


def rows(block: str) -> dict[str, list[str]]:
    return {
        k: v for k, v in docgen.parse_rows(block).items() if k.endswith(".md")
    }


# --------------------------------------------------------------------------- #
# Direction one: a doc's body may change freely without moving the block.      #
# --------------------------------------------------------------------------- #
def test_a_body_rewrite_that_keeps_the_em_dash_count_does_not_move_the_block(docs):
    before = render()

    # Same two docs, same em-dash counts, wholly different bytes: 400 lines
    # where there was one, different words, different length, different
    # everything a line/byte/word count or a checksum would see.
    docs("alpha.md", "".join(f"rewritten line {i}\n" for i in range(400)) + ONE_EM_DASH)
    docs("beta.md", "beta, rewritten at a different length entirely.\n" * 37)

    assert render() == before, (
        "the doc-status block moved on an edit that changed no doc's em-dash "
        "count and no doc's existence. Some column now tracks a doc's bytes; "
        "that column reds every open PR on every doc edit and detects nothing "
        "a diff does not already show (issue #296)."
    )


def test_carried_judgement_survives_a_body_rewrite(docs):
    docs("alpha.md", "a totally different body, still one — dash.\n")
    row = rows(render())["alpha.md"]
    assert row[1] == "current"
    assert row[3] == "keep me"


def test_the_row_carries_membership_em_dashes_and_judgement_and_nothing_else(docs):
    block = render()
    header, rule, *body = block.splitlines()
    assert header == "| doc | status | em-dashes | tier-limit notes |"
    assert rule == "|---|---|---|---|"
    for line in body:
        cells = [c.strip() for c in line.strip("|").split("|")]
        assert len(cells) == 4, (
            f"the inventory grew a column: {line!r}. Every per-doc number here "
            "is paid for by every open PR (issue #296)."
        )
    assert rows(block)["alpha.md"][2] == "1"
    assert rows(block)["beta.md"][2] == "0"


def test_generation_is_idempotent(docs):
    once = render()
    assert docgen.block_doc_status(once) == once


# --------------------------------------------------------------------------- #
# Direction two: membership and prose style DO move it.                        #
# --------------------------------------------------------------------------- #
def test_adding_a_doc_moves_the_block(docs):
    before = render()
    docs("gamma.md", "a new doc.\n")
    after = render()
    assert after != before
    assert "gamma.md" in rows(after)
    # A doc with no committed row arrives un-audited, not silently blessed.
    assert rows(after)["gamma.md"][1] == "needs-work"


def test_removing_a_doc_moves_the_block(docs, tmp_path):
    before = render()
    (tmp_path / "docs" / "beta.md").unlink()
    after = render()
    assert after != before
    assert "beta.md" not in rows(after)


def test_changing_a_doc_em_dash_count_moves_the_block(docs):
    before = render()
    docs("alpha.md", ONE_EM_DASH + "and now a second — dash.\n")
    after = render()
    assert after != before
    assert rows(after)["alpha.md"][2] == "2"


def test_a_clean_doc_gaining_its_first_em_dash_moves_the_block(docs):
    before = render()
    docs("beta.md", "beta gained an AI tell — here.\n")
    after = render()
    assert after != before
    assert rows(after)["beta.md"][2] == "1"


# --------------------------------------------------------------------------- #
# Membership is `docs/*.md` minus the documented exclusions.                   #
# --------------------------------------------------------------------------- #
def test_doc_files_honours_the_exclusion_list(docs):
    docs("DOC-STATUS.md", "the inventory itself.\n")
    docs("v2.0-roadmap.md", "the reasoning of record.\n")
    docs("gamma.md", "a doc.\n")
    assert docgen.doc_files() == ["alpha.md", "beta.md", "gamma.md"]


def test_the_roadmap_stays_excluded():
    """The roadmap is appended to by nearly every PR. Over the 40 most recent
    first-parent landings on main its own em-dash count changed on 13 of them
    (it carries ~1330), so re-joining the inventory would add roughly a third
    again as much churn as the whole rest of docs/ produces. Issue #296 asked
    whether it could rejoin once the churn source was gone. It is not: the
    roadmap IS a churn source in its own right."""
    assert "v2.0-roadmap.md" in docgen.DOC_STATUS_EXCLUDED
